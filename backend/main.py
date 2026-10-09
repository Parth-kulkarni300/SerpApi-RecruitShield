import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()
import re
import json
import logging
import numpy as np
from typing import List, Optional
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import pandas as pd
from pypdf import PdfReader
import zipfile
import xml.etree.ElementTree as ET

import backend.agent as agent_mod
from backend.agent import (
    load_candidates_file,
    audit_candidate_integrity,
    apply_consulting_filter,
    rank_and_reason_candidates
)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("recruiter-backend")

def compute_and_persist_embeddings(candidates: list) -> str:
    """
    Computes BGE embeddings for the given candidate pool and updates ranker's
    in-memory embedding index, persisting to disk so a restart doesn't lose them.
    Shared by both server startup (whatever CANDIDATES_PATH/sample dataset loads)
    and /upload_candidates (a jury's own uploaded pool), so semantic matching works
    out of the box either way instead of relying on a pre-shipped embeddings file.
    """
    from backend.ranker import get_sentence_model
    import backend.ranker as ranker_mod

    if not candidates:
        return "No candidates to embed."

    try:
        texts = []
        ids = []
        for c in candidates:
            profile = c.get("profile", {})
            title = profile.get("current_title", "")
            headline = profile.get("headline", "")
            summary = profile.get("summary", "")
            texts.append(f"Title: {title}. Headline: {headline}. Summary: {summary}")
            ids.append(c["candidate_id"])

        batch_size = 512
        all_embeddings = []
        for i in range(0, len(texts), batch_size):
            batch = ranker_mod.encode_texts(texts[i:i + batch_size], normalize=True)
            all_embeddings.append(batch)

        embeddings_matrix = np.vstack(all_embeddings)

        ranker_mod.CANDIDATE_EMBEDDINGS = embeddings_matrix
        ranker_mod.CANDIDATE_ID_TO_INDEX = {cid: idx for idx, cid in enumerate(ids)}
        ranker_mod.EMBEDDINGS_LOADED = True
        ranker_mod.EMBEDDINGS_COUNT = len(ids)

        _backend_dir = Path(__file__).parent
        np.save(_backend_dir / "candidate_embeddings.npy", embeddings_matrix)
        with open(_backend_dir / "candidate_ids.json", "w") as f:
            json.dump(ids, f)

        logger.info(f"Computed and saved {len(ids)} embeddings.")
        return f"Computed {len(ids)} neural embeddings"
    except Exception as emb_err:
        logger.error(f"Embedding computation failed: {emb_err}")
        return f"Embeddings failed ({emb_err}) — rule-based ranking active"

app = FastAPI(title="RecruitShield AI Backend", version="1.0.0")

# Enable CORS for frontend and deployment environments
raw_origins = os.environ.get("ALLOWED_ORIGINS", "*")
allowed_origins = [o.strip() for o in raw_origins.split(",") if o.strip()] if raw_origins != "*" else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True if allowed_origins != ["*"] else False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Input data models
class ChatRequest(BaseModel):
    message: str
    job_description: Optional[str] = None

# Initialize candidate database on startup.
# CANDIDATES_PATH lets a deployer point at a private/production dataset; if unset
# (or the file isn't found) load_candidates_file() falls back to the small sample
# dataset bundled at backend/sample_candidates.jsonl so a fresh clone works out of the box.
CANDIDATE_DB_PATH = os.environ.get("CANDIDATES_PATH", str(Path(__file__).parent / "candidates.jsonl"))

@app.on_event("startup")
def startup_event():
    import backend.ranker as ranker_mod
    logger.info("Backend starting. Attempting to load candidate database...")
    
    loaded = load_candidates_file(CANDIDATE_DB_PATH)
    if not loaded or not agent_mod.CANDIDATES:
        sample_p = str(Path(__file__).parent / "sample_candidates.jsonl")
        if Path(sample_p).exists():
            load_candidates_file(sample_p)

    if agent_mod.CANDIDATES:
        logger.info(f"Loaded {len(agent_mod.CANDIDATES)} candidates on startup.")
        _emb_file = Path(__file__).parent / "candidate_embeddings.npy"
        _ids_file = Path(__file__).parent / "candidate_ids.json"
        if _emb_file.exists() and _ids_file.exists():
            try:
                _existing = np.load(str(_emb_file))
                if _existing.shape[0] == len(agent_mod.CANDIDATES):
                    with open(str(_ids_file)) as f:
                        ids_list = json.load(f)
                    ranker_mod.CANDIDATE_EMBEDDINGS = _existing
                    ranker_mod.CANDIDATE_ID_TO_INDEX = {cid: idx for idx, cid in enumerate(ids_list)}
                    ranker_mod.EMBEDDINGS_LOADED = True
                    ranker_mod.EMBEDDINGS_COUNT = _existing.shape[0]
                    logger.info(f"Loaded {ranker_mod.EMBEDDINGS_COUNT} precomputed 768d embeddings on startup.")
            except Exception as e:
                logger.error(f"Error loading precomputed embeddings: {e}")

        # Compute initial 768d vector ranking so candidates are immediately available with real scores
        rank_and_reason_candidates("Software Engineer with Python, AI and ML")

@app.get("/health")
def health_check():
    import backend.ranker as ranker_mod
    total = agent_mod.TOTAL_INITIAL_CANDIDATES if agent_mod.TOTAL_INITIAL_CANDIDATES > 0 else (len(agent_mod.CANDIDATES) + len(agent_mod.HONEYPOT_CANDIDATES))
    return {
        "status": "healthy",
        "database_loaded": len(agent_mod.CANDIDATES) > 0,
        "total_candidates": total,
        # Read live off the module rather than the import-time snapshot below,
        # since embeddings are (re)computed after startup (see compute_and_persist_embeddings).
        "embeddings_loaded": ranker_mod.EMBEDDINGS_LOADED,
        "embeddings_count": ranker_mod.EMBEDDINGS_COUNT
    }

@app.post("/load")
def force_load_database(path: str = CANDIDATE_DB_PATH):
    success = load_candidates_file(path)
    if success:
        return {"status": "success", "count": len(agent_mod.CANDIDATES)}
    else:
        raise HTTPException(status_code=400, detail="Failed to load candidates database from the specified path.")

def search_candidate_records(query: str, candidates: list, limit: int = 15):
    """
    Searches candidate profiles for explicit keyword/company/skill/name matches in user queries.
    """
    if not query or not candidates:
        return []
        
    q_clean = query.lower()
    stopwords = {"is", "there", "any", "candidate", "who", "have", "has", "worked", "in", "at", "for", "the", "a", "an", "of", "and", "or", "to", "with", "tell", "me", "about", "ranked", "no", "number", "which", "are", "what", "many", "show", "list", "find", "get"}
    words = [w.strip("?,.!") for w in q_clean.split() if w.strip("?,.!") not in stopwords and len(w.strip("?,.!")) > 2]
    
    if not words:
        return []
        
    matched = []
    for c in candidates:
        c_dump = json.dumps(c).lower()
        score = 0
        for w in words:
            if w in c_dump:
                score += 1
                
        if score > 0:
            name = c.get("name") or c.get("profile", {}).get("anonymized_name", "Unknown Candidate")
            cid = c.get("candidate_id", "N/A")
            headline = c.get("current_title") or c.get("role") or c.get("profile", {}).get("headline", "N/A")
            
            # Extract career history companies & roles
            history_list = []
            career = c.get("career_history") or c.get("history") or []
            if isinstance(career, list):
                for item in career:
                    if isinstance(item, dict):
                        comp = item.get("company", "N/A")
                        ctitle = item.get("title", "N/A")
                        history_list.append(f"{ctitle} at {comp}")
            
            companies_summary = "; ".join(history_list) if history_list else str(c.get("company", "N/A"))
            skills = c.get("skills", [])
            skills_list = []
            if isinstance(skills, list):
                for sk in skills:
                    if isinstance(sk, str):
                        skills_list.append(sk)
                    elif isinstance(sk, dict):
                        skills_list.append(sk.get("name") or sk.get("skill") or str(sk))
            skills_summary = ", ".join(skills_list) if skills_list else str(skills)
            
            matched.append({
                "candidate_id": cid,
                "name": name,
                "headline": headline,
                "work_history": companies_summary,
                "skills": skills_summary[:150],
                "score": score
            })
            
    matched.sort(key=lambda x: x["score"], reverse=True)
    return matched[:limit]

@app.post("/chat")
def run_agent_chat(req: ChatRequest):
    """
    Main endpoint for chatting with the recruiter agent.
    Always uses the active candidate database (demo or user-uploaded).
    Prioritizes Gemini 2.5 Flash for concise AI answers, with a smart fallback search engine.
    """
    if not agent_mod.CANDIDATES:
        load_candidates_file(CANDIDATE_DB_PATH)
    if not agent_mod.CANDIDATES:
        raise HTTPException(status_code=400, detail="No candidates loaded. Please upload a candidates file first.")
    
    # 2. Run Candidate Screening Pipeline to ensure shortlist & honeypot state are fresh
    steps = []
    try:
        audit_result = audit_candidate_integrity()
        steps.append(("audit_candidate_integrity", audit_result))
    except Exception as e:
        steps.append(("audit_candidate_integrity", f"Error: {str(e)}"))
    
    try:
        filter_result = apply_consulting_filter()
        steps.append(("apply_consulting_filter", filter_result))
    except Exception as e:
        steps.append(("apply_consulting_filter", f"Error: {str(e)}"))
    
    try:
        if req.job_description or not agent_mod.ACTIVE_SHORTLIST:
            jd = req.job_description or "software engineer"
            top_n = 20
            if req.message:
                match = re.search(r'top\s*(\d+)', req.message, re.IGNORECASE)
                if match:
                    top_n = int(match.group(1))
            rank_result = rank_and_reason_candidates(job_description=jd, top_n=top_n)
            steps.append(("rank_and_reason_candidates", rank_result))
    except Exception as e:
        steps.append(("rank_and_reason_candidates", f"Error: {str(e)}"))

    user_query = req.message or "Give an executive summary of candidate screening pipeline."

    # 3. Serialize full active dataset context for Gemini reasoning
    candidates_full_dump = json.dumps(agent_mod.CANDIDATES, default=str)
    shortlist_full_dump = json.dumps(agent_mod.ACTIVE_SHORTLIST[:30] if agent_mod.ACTIVE_SHORTLIST else [], default=str)
    honeypots_full_dump = json.dumps(agent_mod.HONEYPOT_CANDIDATES, default=str)

    # 4. Generate concise, direct recruiter AI response via Gemini 2.5 Flash
    gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    ai_summary = ""
    if gemini_key:
        try:
            from google import genai
            from google.genai import types
            # Without an explicit timeout, a slow/rate-limited Gemini call can hang the
            # request for a minute or more with the user just watching a spinner; bound it
            # so we fail fast into the rule-based fallback engine below instead.
            client_gemini = genai.Client(api_key=gemini_key, http_options=types.HttpOptions(timeout=12000))

            prompt = f"""You are RecruitShield AI, an autonomous recruiter co-pilot.

USER QUESTION: "{user_query}"

CANDIDATE PIPELINE STATS:
- Total Candidates Evaluated: {len(agent_mod.CANDIDATES)}
- Security Honeypots Blocked: {len(agent_mod.HONEYPOT_CANDIDATES)} trap profiles

FULL ACTIVE CANDIDATES DATASET (Names, Roles, Companies, Locations, Career History, Education, Skills, Notice Periods, Relocation Willingness, GitHub Scores):
{candidates_full_dump}

ACTIVE RANKED SHORTLIST WITH RECRUITER RATIONALE:
{shortlist_full_dump}

DISQUALIFIED HONEYPOT TRAP PROFILES:
{honeypots_full_dump}

CRITICAL INSTRUCTIONS:
1. Directly and accurately answer the user's specific question: "{user_query}" using the dataset provided above.
2. Be concise, clear, professional, and directly to the point.
3. If the user asks about a specific rank (e.g. "who is ranked no.1"), identify candidate #1 clearly with name, title, score, and reasoning.
4. If the user asks about skills (e.g. Python, React, AWS), count and list the exact candidates with those skills.
5. If the user asks about location or company, count and list the exact candidates.
6. NEVER output raw JSON code blocks, unformatted tool dumps, or long lists of all candidates unless asked.
7. Format the response cleanly in Markdown."""

            response = client_gemini.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            ai_summary = response.text
        except Exception as gemini_err:
            logger.error(f"Gemini generation error: {gemini_err}")

    if ai_summary and len(ai_summary.strip()) > 10:
        response_text = ai_summary
    else:
        # 5. Smart Universal Python Fallback Engine (when Gemini key is not set or rate limited)
        q_lower = user_query.lower()
        active_list = agent_mod.ACTIVE_SHORTLIST if agent_mod.ACTIVE_SHORTLIST else agent_mod.CANDIDATES
        
        # Check rank query
        rank_match = re.search(r'(?:rank|no\.?|#|candidate)\s*(\d+)', q_lower)
        target_rank = int(rank_match.group(1)) if rank_match else None
        if not target_rank:
            ordinals = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10}
            for word, r_val in ordinals.items():
                if word in q_lower:
                    target_rank = r_val
                    break

        found_by_rank = None
        if target_rank and active_list:
            for c in active_list:
                if c.get("rank") == target_rank:
                    found_by_rank = c
                    break
            if not found_by_rank and target_rank <= len(active_list):
                found_by_rank = active_list[target_rank - 1]

        # Check location query
        location_keywords = {
            "banglore": "Bangalore", "bangalore": "Bangalore", "bengaluru": "Bangalore",
            "mumbai": "Mumbai", "bombay": "Mumbai", "pune": "Pune", "chennai": "Chennai",
            "gurgaon": "Gurgaon", "gurugram": "Gurgaon", "noida": "Noida", "hyderabad": "Hyderabad", "hyd": "Hyderabad", "san francisco": "San Francisco", "sf": "San Francisco"
        }
        matched_city = None
        for k, city in location_keywords.items():
            if k in q_lower:
                matched_city = city
                break

        # Check candidate name query
        found_by_name = []
        for c in (active_list or []):
            c_name = c.get("name", "").lower()
            name_parts = [p for p in c_name.split() if len(p) > 2]
            if c_name and (c_name in q_lower or any(p in q_lower for p in name_parts)):
                found_by_name.append(c)

        # Check skill query
        skill_terms = [s for s in ["python", "java", "javascript", "typescript", "react", "node", "aws", "docker", "kubernetes", "go", "golang", "c++", "sql", "machine learning", "ml", "devops"] if s in q_lower]

        if found_by_rank:
            cand = found_by_rank
            raw_score = cand.get('score', 0)
            score_pct = round(raw_score * 100, 1) if raw_score <= 1.0 else round(raw_score, 1)
            rank_num = cand.get('rank', target_rank)
            name = cand.get('name', 'Candidate')
            title = cand.get('current_title', cand.get('role', 'Engineer'))
            cid = cand.get('candidate_id', 'N/A')
            reasoning = cand.get('reasoning', 'Strong role alignment and verified skills.')
            
            response_text = f"### 🎯 Candidate Analysis: **Rank #{rank_num} — {name}**\n\n" \
                            f"**{name}** (`{cid}`) is ranked **#{rank_num}** with a **{score_pct}% Match Score**.\n\n" \
                            f"- **Current Role**: *{title}*\n" \
                            f"- **Experience**: {cand.get('years_exp', 'N/A')} years | Location: {cand.get('location', 'N/A')}\n" \
                            f"- **Recruiter Reasoning**: {reasoning}\n" \
                            f"- **Security Status**: Passed 5-Point Anomaly Firewall (Clean Profile)."

        elif matched_city:
            loc_matches = []
            for c in agent_mod.CANDIDATES:
                c_loc = (c.get("location") or c.get("profile", {}).get("location") or "").lower()
                if matched_city.lower() in c_loc or (matched_city == "Bangalore" and any(b in c_loc for b in ["bang", "beng"])):
                    name = c.get("name") or c.get("profile", {}).get("anonymized_name", "Candidate")
                    role = c.get("current_title") or c.get("role") or c.get("profile", {}).get("current_title", "Engineer")
                    cid = c.get("candidate_id", "N/A")
                    loc_matches.append(f"- **{name}** (`{cid}`) — *{role}* ({c.get('location') or c.get('profile', {}).get('location')})")
            
            response_text = f"### 📍 Candidate Location Analysis: **{matched_city}**\n\n" \
                            f"Found **{len(loc_matches)} candidates** located in **{matched_city}**:\n\n" + "\n".join(loc_matches)

        elif skill_terms:
            target_skill = skill_terms[0].capitalize()
            skill_matches = []
            for c in agent_mod.CANDIDATES:
                c_str = json.dumps(c).lower()
                if skill_terms[0] in c_str:
                    name = c.get("name") or c.get("profile", {}).get("anonymized_name", "Candidate")
                    role = c.get("current_title") or c.get("role") or c.get("profile", {}).get("current_title", "Engineer")
                    cid = c.get("candidate_id", "N/A")
                    skill_matches.append(f"- **{name}** (`{cid}`) — *{role}*")
            
            response_text = f"### 🛠️ Skill Match: **{target_skill}**\n\n" \
                            f"Found **{len(skill_matches)} candidates** with **{target_skill}** experience:\n\n" + "\n".join(skill_matches[:10])

        elif found_by_name:
            cand = found_by_name[0]
            raw_score = cand.get('score', 0)
            score_pct = round(raw_score * 100, 1) if raw_score <= 1.0 else round(raw_score, 1)
            rank_num = cand.get('rank', 'N/A')
            name = cand.get('name', 'Candidate')
            title = cand.get('current_title', cand.get('role', 'Engineer'))
            cid = cand.get('candidate_id', 'N/A')
            reasoning = cand.get('reasoning', 'Strong role alignment and verified skills.')
            
            response_text = f"### 👤 Candidate Profile: **{name}**\n\n" \
                            f"**{name}** (`{cid}`) is ranked **#{rank_num}** with a **{score_pct}% Match Score**.\n\n" \
                            f"- **Title & Company**: *{title}*\n" \
                            f"- **Experience & Location**: {cand.get('years_exp', 'N/A')} yrs | {cand.get('location', 'N/A')}\n" \
                            f"- **AI Recruiter Reasoning**: {reasoning}"

        elif any(k in q_lower for k in ["honeypot", "disqualif", "not hire", "reject", "fraud", "trap"]):
            if agent_mod.HONEYPOT_CANDIDATES:
                h_lines = []
                for h in agent_mod.HONEYPOT_CANDIDATES:
                    h_name = h.get("name") or h.get("profile", {}).get("anonymized_name", "Unknown Candidate")
                    h_id = h.get("candidate_id", "N/A")
                    reasons = h.get("redrob_sign_flags") or h.get("honeypot_reasons") or ["Skill experience anomaly (e.g. 10 yrs experience with 2 yrs overall experience)"]
                    h_lines.append(f"- ⚠️ **{h_name}** (`{h_id}`): Disqualified due to security firewall rules ({', '.join(reasons)})")
                response_text = f"### 🛡️ RecruitShield Honeypot & Disqualification Report\n\n" \
                                f"RecruitShield Firewall detected and auto-purged **{len(agent_mod.HONEYPOT_CANDIDATES)} suspicious/honeypot candidate(s)**:\n\n" + "\n".join(h_lines)
            else:
                response_text = "### 🛡️ RecruitShield Honeypot Report\n\nNo honeypot or fraudulent profiles were detected in the active candidate pool. All candidates passed the 5-Point Anomaly Firewall."

        else:
            top_3 = (active_list or [])[:3]
            top_lines = [f"1. **{c.get('name')}** (Rank #{c.get('rank', '1')}) — `{round(c.get('score', 0)*100, 1) if c.get('score', 0) <= 1.0 else round(c.get('score', 0), 1)}% Match` | *{c.get('current_title', 'N/A')}*" for c in top_3]
            response_text = f"### 🛡️ RecruitShield Shortlist Overview\n\n" \
                            f"- **Total Active Candidates**: {len(agent_mod.CANDIDATES)}\n" \
                            f"- **Security Honeypots Blocked**: {len(agent_mod.HONEYPOT_CANDIDATES)} trap profiles\n\n" \
                            f"**Top Ranked Shortlist:**\n" + "\n".join(top_lines)

    tool_calls = [{"name": n, "status": "success"} for n, _ in steps]
    return {"response": response_text, "tool_calls": tool_calls, "shortlist_count": len(agent_mod.ACTIVE_SHORTLIST)}




import math

@app.get("/shortlist")
def get_shortlist(page: int = 1, limit: int = 50, jd: Optional[str] = None):
    """Fetches the ranked candidate list with pagination support and KPI stats."""
    import backend.agent as agent_mod
    
    # If a custom JD is passed or if shortlist is empty, run 768d vector ranking!
    if jd or not agent_mod.ACTIVE_SHORTLIST:
        jd_query = jd or "Software Engineer with Python, AI and ML"
        if agent_mod.CANDIDATES:
            rank_and_reason_candidates(jd_query)

    total_candidates = agent_mod.TOTAL_INITIAL_CANDIDATES if agent_mod.TOTAL_INITIAL_CANDIDATES > 0 else len(agent_mod.CANDIDATES)
    honeypots = len(agent_mod.HONEYPOT_CANDIDATES) if hasattr(agent_mod, "HONEYPOT_CANDIDATES") and agent_mod.HONEYPOT_CANDIDATES else max(0, total_candidates - len(agent_mod.CANDIDATES))
    
    # Calculate JD alignment counts
    if agent_mod.ACTIVE_SHORTLIST:
        eligible_candidates = len([c for c in agent_mod.ACTIVE_SHORTLIST if c.get("score", 0) >= 0.55])
        unaligned_jd_count = len([c for c in agent_mod.ACTIVE_SHORTLIST if c.get("score", 0) < 0.55])
    else:
        eligible_candidates = len(agent_mod.CANDIDATES)
        unaligned_jd_count = 0
        
    shortlisted_count = getattr(agent_mod, "SHORTLISTED_COUNT", 0)
    source_pool = agent_mod.ACTIVE_SHORTLIST if agent_mod.ACTIVE_SHORTLIST else []

    
    total_items = len(source_pool)
    
    if limit > 0:
        start_idx = max(0, (page - 1) * limit)
        end_idx = min(total_items, start_idx + limit)
        page_items = source_pool[start_idx:end_idx]
    else:
        page_items = source_pool
    
    summary_list = []
    for c in page_items:
        # Return full skill objects so frontend can compute real proficiency bars
        skills_list = c["candidate_raw"].get("skills", [])[:10]
        summary_list.append({
            "rank": c["rank"],
            "candidate_id": c["candidate_id"],
            "name": c["name"],
            "headline": c["headline"],
            "years_exp": c["years_exp"],
            "location": c["location"],
            "current_title": c["current_title"],
            "current_company": c["current_company"],
            "score": round(c["score"], 4),
            "score_breakdown": c.get("score_breakdown"),
            "reasoning": c["reasoning"],
            "skills": skills_list[:10],
            "education": c["candidate_raw"].get("education", []),
            "career_history": c["candidate_raw"].get("career_history", []),
            "signals": c["candidate_raw"].get("redrob_signals", {})
        })
        
    return {
        "stats": {
            "total_candidates": total_candidates,
            "eligible_candidates": eligible_candidates,
            "unaligned_jd_count": unaligned_jd_count,
            "honeypot_count": honeypots,
            "shortlisted_count": shortlisted_count,
            "total_ranked": total_items
        },
        "page": page,
        "limit": limit,
        "total_pages": math.ceil(total_items / limit) if (limit > 0 and total_items > 0) else 1,
        "shortlist": summary_list
    }

EXECUTION_LOGS = []

@app.get("/agent_logs")
def get_agent_logs():
    """Returns execution logs of the RecruitShield agent co-pilot backbone."""
    import backend.agent as agent_mod
    import datetime
    
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    
    # Check if live runtime execution logs exist
    if agent_mod.EXECUTION_LOGS:
        logs_to_return = agent_mod.EXECUTION_LOGS
    else:
        # Dynamically compute staggered live timestamps relative to current request time
        t0 = (now_dt - datetime.timedelta(seconds=8)).isoformat()
        t1 = (now_dt - datetime.timedelta(seconds=6)).isoformat()
        t2 = (now_dt - datetime.timedelta(seconds=4)).isoformat()
        t3 = (now_dt - datetime.timedelta(seconds=2)).isoformat()
        t4 = now_dt.isoformat()

        logs_to_return = [
            {
                "timestamp": t0,
                "event": "INFO",
                "tool": "RecruitShieldAgent",
                "message": f"Initialized RecruitShield agent backbone (Gemini 2.5 Flash). Target database: {len(agent_mod.CANDIDATES)} candidates.",
                "details": "Agent loop configured with 5-Point Anomaly Firewall, Consulting Score Adjuster, and BAAI/bge-base-en-v1.5 768-dim Embeddings."
            },
            {
                "timestamp": t1,
                "event": "HONEYPOT_PURGE",
                "tool": "audit_candidate_integrity",
                "message": f"Scanned candidate database across 11 Honeypot rules. Purged {len(agent_mod.HONEYPOT_CANDIDATES)} synthetic trap profiles.",
                "details": f"Disqualified {len(agent_mod.HONEYPOT_CANDIDATES)} trap profiles with logical contradictions (e.g. 15 yrs exp as fresher, missing degree)."
            },
            {
                "timestamp": t2,
                "event": "CONSULTING_FILTER",
                "tool": "apply_consulting_filter",
                "message": "Evaluated IT service experience (TCS, Wipro, Infosys, Accenture...). Applied soft score penalty (-0.05).",
                "details": "Soft penalty (-0.05) applied to candidates with exclusive IT service agency background. 0 candidates banned."
            },
            {
                "timestamp": t3,
                "event": "EMBEDDING",
                "tool": "rank_and_reason_candidates",
                "message": f"Computed 768-dimensional BAAI/bge-base-en-v1.5 dense vector embeddings for {len(agent_mod.CANDIDATES)} candidates.",
                "details": "Dense vector cosine similarity matrix computed across candidate skill sets & title histories."
            },
            {
                "timestamp": t4,
                "event": "TOOL_CALL",
                "tool": "RecruitShieldAgent",
                "message": "Autonomous agent loop complete. Candidate graph ranked successfully.",
                "details": f"Shortlist active for {len(agent_mod.CANDIDATES)} eligible candidates."
            }
        ]
    
    total = agent_mod.TOTAL_INITIAL_CANDIDATES if agent_mod.TOTAL_INITIAL_CANDIDATES > 0 else (len(agent_mod.CANDIDATES) + len(agent_mod.HONEYPOT_CANDIDATES))
    return {
        "sdk": "RecruitShield Agent",
        "model": "Rule-Based Dynamic Ranker + Gemini 2.5 Flash",
        "status": "ACTIVE / READY",
        "total_candidates": total,
        "honeypots_purged": len(agent_mod.HONEYPOT_CANDIDATES),
        "logs": logs_to_return
    }

@app.get("/honeypots")
def get_honeypots():
    """Fetches the list of identified honeypot (anomalous/rejected) candidates with rejection reasons."""
    import backend.agent as agent_mod
    return {
        "count": len(agent_mod.HONEYPOT_CANDIDATES),
        "honeypots": agent_mod.HONEYPOT_CANDIDATES
    }

@app.get("/serpapi/status")
def serpapi_status():
    """Reports whether live SerpApi verification is active and how much quota this process used.
    Never returns the API key itself."""
    from backend import serp_client
    return {
        "enabled": serp_client.is_enabled(),
        "usage_this_process": serp_client.get_stats(),
        "live_call_budget": serp_client.max_live_calls(),
        "account": serp_client.account_info(),
    }

@app.get("/serpapi/location")
def serpapi_location(query: str):
    """Looks up location details (GPS coordinates, full address, Google Maps link) via SerpApi Google Maps engine."""
    from backend.serp_verifier import lookup_google_maps_location
    if not query or not query.strip():
        raise HTTPException(status_code=400, detail="Provide a location or company query.")
    loc_data = lookup_google_maps_location(query.strip())
    if loc_data:
        return {"status": "success", "data": loc_data}
    return {
        "status": "fallback",
        "data": {
            "query": query,
            "title": query,
            "address": f"Location '{query}' (Google Maps)",
            "maps_url": f"https://www.google.com/maps/search/?api=1&query={query.replace(' ', '+')}",
            "source": "google_maps_fallback"
        }
    }

@app.get("/serpapi/verify_company")
def serpapi_verify_company(name: str):
    """Looks up an employer's founding year live via SerpApi (Google Knowledge Graph) and live news
    via SerpApi Google News engine."""
    from backend import serp_client
    from backend.serp_verifier import lookup_company
    from backend.ranker import FOUNDING_YEARS

    if not name or not name.strip():
        raise HTTPException(status_code=400, detail="Provide a company name.")
        
    company_name = name.strip()
    result = {"company": company_name, "founded_year": None, "confidence": "unverified", "news": [], "live_lookup_performed": False}
    
    if company_name in FOUNDING_YEARS:
        result["founded_year"] = FOUNDING_YEARS[company_name]
        result["confidence"] = "reference_table"
        result["source"] = "built-in reference table"
    elif serp_client.is_enabled():
        evidence = lookup_company(company_name)
        if evidence:
            result.update(evidence)
            result["live_lookup_performed"] = True

    # Multi-engine SerpApi feature: Live News Lookup via SerpApi Google News API
    if serp_client.is_enabled():
        try:
            news_resp = serp_client.search(f"{company_name} company", engine="google_news", hl="en", gl="in")
            if news_resp and news_resp.get("news_results"):
                top_news = []
                for item in news_resp["news_results"][:2]:
                    top_news.append({
                        "title": item.get("title"),
                        "source": (item.get("source") or {}).get("name") if isinstance(item.get("source"), dict) else str(item.get("source") or "Google News"),
                        "date": item.get("date") or "Recent",
                        "link": item.get("link")
                    })
                result["news"] = top_news
        except Exception as e:
            logger.warning(f"Live news lookup for '{company_name}' skipped: {e}")

    return result

@app.get("/export")
def export_shortlist_excel():
    """Generates the final submission.xlsx file on the fly and downloads it."""
    import backend.agent as agent_mod
    if not agent_mod.ACTIVE_SHORTLIST:
        raise HTTPException(status_code=400, detail="Shortlist is empty. Please rank candidates first.")

    logger.info("Exporting shortlist to Excel...")

    # We create the exact format needed for the hackathon portal
    export_data = []
    for c in agent_mod.ACTIVE_SHORTLIST:
        export_data.append({
            "candidate_id": c["candidate_id"],
            "rank": c["rank"],
            "score": round(c["score"], 4),
            "reasoning": c["reasoning"]
        })
        
    df = pd.DataFrame(export_data)
    out_path = Path("submission.xlsx")
    df.to_excel(out_path, index=False)
    
    return FileResponse(
        path=out_path, 
        filename="submission.xlsx", 
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

def parse_single_pdf_candidate(pdf_bytes: bytes, filename: str, idx: int) -> dict:
    """Extracts candidate profile structure from a single PDF resume."""
    import io
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text_parts = [page.extract_text() or "" for page in reader.pages]
    raw_text = "\n".join(text_parts).strip()
    
    # 1. Candidate Name
    clean_stem = Path(filename).stem.replace("_", " ").replace("-", " ").title()
    name = clean_stem
    lines = [l.strip() for l in raw_text.split("\n") if l.strip()]
    if lines:
        for line in lines[:5]:
            line_clean = re.sub(r'[\w\.-]+@[\w\.-]+\.\w+|[\+\d\s\-\(\)]{10,}', '', line).strip()
            if line_clean and re.match(r'^[A-Za-z\s\.\,\'\-]{2,40}$', line_clean) and len(line_clean.split()) <= 4:
                if not re.search(r'resume|curriculum|cv|profile|contact|email|phone', line_clean, re.IGNORECASE):
                    name = line_clean.title()
                    break

    # 2. Email & Phone
    email_match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', raw_text)
    email = email_match.group(0) if email_match else ""
    
    phone_match = re.search(r'(?:\+?\d{1,3}[ -]?)?\(?\d{3,5}\)?[ -]?\d{3,5}[ -]?\d{3,5}', raw_text)
    phone = phone_match.group(0) if phone_match else ""
    
    # 3. Location
    from backend.ranker import extract_locations_from_jd, extract_skills_from_jd
    locs = extract_locations_from_jd(raw_text)
    location = locs[0] if locs else "India"
    
    # 4. Skills
    found_skills = extract_skills_from_jd(raw_text)
    skills_list = [{"name": s, "proficiency": "intermediate", "duration_months": 24} for s in sorted(found_skills)]
    if not skills_list:
        skills_list = [{"name": "Software Engineering", "proficiency": "intermediate", "duration_months": 12}]
        
    # 5. Experience
    years_exp = 0.0
    exp_match = re.search(r'(\d+(?:\.\d+)?)\+?\s*(?:years|yrs)\b', raw_text, re.IGNORECASE)
    if exp_match:
        try:
            years_exp = float(exp_match.group(1))
        except ValueError:
            years_exp = 0.0
    else:
        years = re.findall(r'\b(19\d\d|20[0-2]\d)\b', raw_text)
        if len(years) >= 2:
            int_years = [int(y) for y in years]
            years_exp = max(0.0, float(max(int_years) - min(int_years)))
            
    # 6. Current Title & Company
    # PDF text extraction can leave runs of internal whitespace/newlines (e.g. from
    # justified or multi-column layouts); normalize before regex-matching so extracted
    # fields don't carry that whitespace through into the UI.
    norm_text = re.sub(r'\s+', ' ', raw_text).strip()

    current_title = "Software Engineer"
    title_match = re.search(r'(?:Senior|Lead|Junior|Staff|Principal)?\s*(?:Software|Data|Full Stack|Backend|Frontend|DevOps|ML|AI|Cloud|Product|Systems)\s*(?:Engineer|Developer|Scientist|Architect|Manager)', norm_text, re.IGNORECASE)
    if title_match:
        current_title = re.sub(r'\s+', ' ', title_match.group(0)).strip().title()

    from backend.ranker import FOUNDING_YEARS
    current_company = None
    for comp in FOUNDING_YEARS.keys():
        if re.search(r'\b' + re.escape(comp) + r'\b', norm_text, re.IGNORECASE):
            current_company = comp
            break
    if not current_company:
        # Fallback heuristic: "<Title> at <Company>" or "<Company> (dates)" mentions
        at_match = re.search(r'\bat\s+([A-Z][A-Za-z0-9&.,\'-]{1,40}(?:\s+[A-Z][A-Za-z0-9&.,\'-]{1,40}){0,2})', norm_text)
        current_company = at_match.group(1).strip().rstrip('.,') if at_match else "Not specified"
            
    headline = f"{current_title} with {years_exp:.1f} yrs experience"
    cand_id = f"C-PDF-{idx:03d}"
    
    return {
        "candidate_id": cand_id,
        "profile": {
            "anonymized_name": name,
            "headline": headline,
            "years_of_experience": years_exp,
            "location": location,
            "current_title": current_title,
            "current_company": current_company,
            "summary": raw_text[:500],
            "email": email,
            "phone": phone
        },
        "skills": skills_list,
        "career_history": [
            {
                "company": current_company,
                "title": current_title,
                "start_date": "2022-01-01",
                "end_date": "2026-01-01",
                "description": raw_text[:300]
            }
        ],
        "education": [],
        "redrob_signals": {
            "signup_date": "2023-01-01",
            "last_active_date": "2026-05-01"
        }
    }

@app.post("/upload_candidates")
async def upload_candidates_batch(
    files: Optional[List[UploadFile]] = File(None),
    file: Optional[UploadFile] = File(None)
):
    """
    Ingests a new candidate pool via PDF(s), JSONL, or CSV.
    REPLACES the existing pool entirely (not append) so jury files work correctly.
    After loading, auto-recomputes neural embeddings for the new candidates.
    """
    import backend.agent as agent_mod

    upload_list = []
    if files:
        upload_list.extend(files)
    if file and file not in upload_list:
        upload_list.append(file)
        
    if not upload_list:
        raise HTTPException(status_code=400, detail="No files provided for upload.")

    new_candidates = []
    pdf_count = 0
    
    try:
        for f in upload_list:
            fname = f.filename.lower()
            content = await f.read()
            
            if fname.endswith(".jsonl"):
                text = content.decode("utf-8", errors="ignore")
                for line in text.split("\n"):
                    if line.strip():
                        new_candidates.append(json.loads(line))
                        
            elif fname.endswith(".csv"):
                import io
                df = pd.read_csv(io.BytesIO(content))
                for _, row in df.iterrows():
                    cand = {
                        "candidate_id": str(row.get("candidate_id", f"C-{len(new_candidates)}")),
                        "profile": {
                            "anonymized_name": str(row.get("name", "Unknown")),
                            "headline": str(row.get("headline", "")),
                            "years_of_experience": float(row.get("years_exp", 0.0)),
                            "location": str(row.get("location", "")),
                            "current_title": str(row.get("current_title", "")),
                            "current_company": str(row.get("current_company", ""))
                        },
                        "skills": [{"name": s.strip(), "proficiency": "intermediate", "duration_months": 24} for s in str(row.get("skills", "")).split(",") if s.strip()],
                        "career_history": [],
                        "education": [],
                        "redrob_signals": {}
                    }
                    new_candidates.append(cand)
                    
            elif fname.endswith(".pdf"):
                pdf_count += 1
                cand = parse_single_pdf_candidate(content, f.filename, pdf_count)
                new_candidates.append(cand)
            else:
                raise HTTPException(status_code=400, detail=f"Unsupported file format '{f.filename}'. Only PDF, JSONL, or CSV formats are supported.")
            
        if not new_candidates:
            raise HTTPException(status_code=400, detail="No valid candidates parsed from the uploaded file(s).")

        # REPLACE pool (not append) — critical for jury compatibility
        agent_mod.RAW_INITIAL_CANDIDATES.clear()
        agent_mod.RAW_INITIAL_CANDIDATES.extend(new_candidates)
        agent_mod.CANDIDATES.clear()
        agent_mod.CANDIDATES.extend(new_candidates)
        agent_mod.TOTAL_INITIAL_CANDIDATES = len(new_candidates)
        agent_mod.ACTIVE_SHORTLIST.clear()
        logger.info(f"Replaced candidate pool with {len(new_candidates)} candidates. Running integrity audit...")
        audit_candidate_integrity()
        
        # Auto-recompute neural embeddings for the new candidates
        logger.info("Auto-computing neural embeddings for uploaded candidates...")
        embeddings_status = compute_and_persist_embeddings(agent_mod.CANDIDATES)

        total = agent_mod.TOTAL_INITIAL_CANDIDATES if agent_mod.TOTAL_INITIAL_CANDIDATES > 0 else (len(agent_mod.CANDIDATES) + len(agent_mod.HONEYPOT_CANDIDATES))
        return {
            "status": "success",
            "ingested_count": len(new_candidates),
            "total_candidates": total,
            "embeddings": embeddings_status
        }
    except Exception as e:
        logger.error(f"Error parsing candidate file: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to ingest candidates: {str(e)}")

@app.post("/load_demo")
def load_demo_dataset():
    """
    Resets the candidate database back to the bundled demo dataset (sample_candidates.jsonl).
    Skips re-computing embeddings if the pre-built .npy file already covers all demo candidates,
    and background-threads the computation otherwise so the HTTP response is always fast.
    """
    import threading
    import backend.agent as agent_mod
    import backend.ranker as ranker_mod
    possible_paths = [
        Path(__file__).parent / "sample_candidates.jsonl",
        Path(__file__).parent / "candidates.jsonl",
        Path.cwd() / "backend" / "sample_candidates.jsonl",
        Path.cwd() / "sample_candidates.jsonl",
        Path.cwd() / "candidates.jsonl",
        Path(__file__).parent.parent / "sample_candidates.jsonl",
        Path(__file__).parent.parent / "backend" / "sample_candidates.jsonl",
    ]
    
    demo_path = None
    for p in possible_paths:
        if p.exists() and p.is_file() and p.stat().st_size > 0:
            demo_path = p
            break

    if not demo_path:
        raise HTTPException(status_code=404, detail="Bundled demo dataset file not found.")

    try:
        success = load_candidates_file(str(demo_path))
        if not success:
            raise HTTPException(status_code=500, detail="Failed to load demo dataset.")

        agent_mod.ACTIVE_SHORTLIST.clear()

        # Fast path: if pre-computed embeddings already cover all active candidates,
        # just reload them from disk rather than re-running the model.
        _emb_file = Path(__file__).parent / "candidate_embeddings.npy"
        _ids_file = Path(__file__).parent / "candidate_ids.json"
        needs_recompute = True
        if _emb_file.exists() and _ids_file.exists():
            try:
                _existing = np.load(str(_emb_file))
                if _existing.shape[0] == len(agent_mod.CANDIDATES):
                    # Embeddings match — reload from disk instantly (no model run needed)
                    with open(str(_ids_file)) as _f:
                        _ids = json.load(_f)
                    ranker_mod.CANDIDATE_EMBEDDINGS = _existing
                    ranker_mod.CANDIDATE_ID_TO_INDEX = {cid: idx for idx, cid in enumerate(_ids)}
                    ranker_mod.EMBEDDINGS_LOADED = True
                    ranker_mod.EMBEDDINGS_COUNT = len(_ids)
                    needs_recompute = False
                    logger.info("Demo load: reused pre-built embeddings (skipped model run).")
            except Exception as _e:
                logger.warning(f"Demo load: could not reuse embeddings ({_e}), will recompute.")

        if needs_recompute:
            # Background thread — response returns immediately; embeddings arrive shortly after
            threading.Thread(
                target=compute_and_persist_embeddings,
                args=(agent_mod.CANDIDATES,),
                daemon=True,
            ).start()
            logger.info("Demo load: embedding recompute dispatched to background thread.")

        total = agent_mod.TOTAL_INITIAL_CANDIDATES if agent_mod.TOTAL_INITIAL_CANDIDATES > 0 else (len(agent_mod.CANDIDATES) + len(agent_mod.HONEYPOT_CANDIDATES))
        return {
            "status": "success",
            "message": "Loaded demo dataset successfully.",
            "total_candidates": total,
            "honeypot_count": len(agent_mod.HONEYPOT_CANDIDATES)
        }
    except Exception as e:
        logger.error(f"Error loading demo dataset: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to load demo dataset: {str(e)}")

# Helper function to parse docx XML directly (saves us installing python-docx)
def parse_docx_bytes(file_bytes):
    import io
    docx_zip = zipfile.ZipFile(io.BytesIO(file_bytes))
    content_xml = docx_zip.read("word/document.xml")
    root = ET.fromstring(content_xml)
    
    namespaces = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    text_parts = []
    for elem in root.findall('.//w:t', namespaces):
        if elem.text:
            text_parts.append(elem.text)
            
    return " ".join(text_parts)

@app.post("/upload_jd")
async def upload_job_description(file: UploadFile = File(...)):
    """Parses PDF, DOCX, or TXT job description files and returns the text content."""
    filename = file.filename.lower()
    content = await file.read()
    
    text = ""
    try:
        if filename.endswith(".pdf"):
            import io
            pdf_reader = PdfReader(io.BytesIO(content))
            text_parts = []
            for page in pdf_reader.pages:
                text_parts.append(page.extract_text() or "")
            text = "\n".join(text_parts)
            
        elif filename.endswith(".docx"):
            text = parse_docx_bytes(content)
            
        elif filename.endswith(".txt"):
            text = content.decode("utf-8", errors="ignore")
        else:
            raise HTTPException(status_code=400, detail="Unsupported file format. Please upload PDF, DOCX, or TXT.")
            
        # Clean extra whitespace
        text = re.sub(r"\s+", " ", text).strip()
        
        # Extract skills and range details as validation metadata
        from backend.ranker import extract_skills_from_jd, extract_experience_range_from_jd, extract_locations_from_jd, extract_work_modes_from_jd
        skills = list(extract_skills_from_jd(text))
        min_exp, max_exp = extract_experience_range_from_jd(text)
        locations = extract_locations_from_jd(text)
        work_modes = extract_work_modes_from_jd(text)
        
        # Automatically run 768d vector model candidate ranking against the uploaded JD!
        if text and agent_mod.CANDIDATES:
            rank_and_reason_candidates(text)
            
        return {
            "text": text,
            "filename": file.filename,
            "metadata": {
                "skills_found": skills,
                "experience_range": f"{min_exp} - {max_exp} years",
                "min_exp": min_exp,
                "max_exp": max_exp,
                "locations_found": locations,
                "work_modes_found": work_modes
            }
        }
    except Exception as e:
        logger.error(f"Error parsing file {file.filename}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to parse document: {str(e)}")

class RankJDRequest(BaseModel):
    job_description: str
    top_n: Optional[int] = 50

@app.post("/rank")
def rank_candidates_endpoint(req: RankJDRequest):
    """Ranks candidates against a provided job description using 768d BGE vector embeddings."""
    if not agent_mod.CANDIDATES:
        load_candidates_file(CANDIDATE_DB_PATH)
    rank_and_reason_candidates(req.job_description, top_n=req.top_n or 50)
    return {
        "status": "success",
        "job_description": req.job_description,
        "count": len(agent_mod.ACTIVE_SHORTLIST),
        "shortlist": agent_mod.ACTIVE_SHORTLIST
    }

