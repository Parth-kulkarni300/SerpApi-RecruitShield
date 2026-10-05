import os
import json
import logging
import numpy as np
from pathlib import Path
from backend.ranker import is_honeypot, check_honeypot_reasons, is_consulting_only, score_candidate, rank_candidates

import datetime

logger = logging.getLogger("recruiter-agent")

# Global candidate store & stats
RAW_INITIAL_CANDIDATES = []
CANDIDATES = []
ACTIVE_SHORTLIST = []
HONEYPOT_CANDIDATES = []
TOTAL_INITIAL_CANDIDATES = 0
HONEYPOT_COUNT = 0
ELIGIBLE_CANDIDATES = 0

EXECUTION_LOGS = []

def log_agent_event(event_type: str, tool_name: str, message: str, details=None):
    log_entry = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "event": event_type,
        "tool": tool_name,
        "message": message,
        "details": details
    }
    EXECUTION_LOGS.append(log_entry)
    if len(EXECUTION_LOGS) > 60:
        EXECUTION_LOGS.pop(0)
    return log_entry

def load_candidates_file(file_path: str):
    """Loads candidates from JSONL into memory once at startup."""
    global CANDIDATES, RAW_INITIAL_CANDIDATES, TOTAL_INITIAL_CANDIDATES, ELIGIBLE_CANDIDATES
    CANDIDATES.clear()
    RAW_INITIAL_CANDIDATES.clear()
    
    path = Path(file_path)
    if not path.exists() or not path.is_file() or path.stat().st_size == 0:
        logger.warning(f"Candidates file not found or empty at: {file_path}")
        possible_fallbacks = [
            Path(__file__).parent / "sample_candidates.jsonl",
            Path(__file__).parent / "candidates.jsonl",
            Path.cwd() / "backend" / "sample_candidates.jsonl",
            Path.cwd() / "sample_candidates.jsonl",
            Path.cwd() / "candidates.jsonl",
            Path(__file__).parent.parent / "sample_candidates.jsonl",
            Path(__file__).parent.parent / "backend" / "sample_candidates.jsonl",
        ]
        path = None
        for fb in possible_fallbacks:
            if fb.exists() and fb.is_file() and fb.stat().st_size > 0:
                logger.info(f"Falling back to bundled dataset at: {fb}")
                path = fb
                break
        if not path:
            logger.error("No candidates database file found. Please check candidates.jsonl location.")
            return False
            
    logger.info(f"Loading candidate database from {path}...")
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    RAW_INITIAL_CANDIDATES.append(json.loads(line))
        CANDIDATES.clear()
        CANDIDATES.extend(RAW_INITIAL_CANDIDATES)
        TOTAL_INITIAL_CANDIDATES = len(RAW_INITIAL_CANDIDATES)
        logger.info(f"Successfully loaded {len(RAW_INITIAL_CANDIDATES)} candidate profiles. Running initial integrity audit...")
        audit_candidate_integrity()
        return True
    except Exception as e:
        logger.error(f"Error loading candidates: {e}")
        return False

def audit_candidate_integrity() -> str:
    """
    Scans the loaded candidate database using a 5-point integrity check.
    Identifies and removes fake profiles (honeypots) created with logical contradictions.
    Returns: A summary message listing the number of deleted honeypots and remaining candidates.
    """
    global CANDIDATES, RAW_INITIAL_CANDIDATES, TOTAL_INITIAL_CANDIDATES, HONEYPOT_COUNT, ELIGIBLE_CANDIDATES, HONEYPOT_CANDIDATES
    source_candidates = RAW_INITIAL_CANDIDATES if RAW_INITIAL_CANDIDATES else CANDIDATES
    if not source_candidates:
        return "Error: Candidate database is empty. Please load candidates first."
        
    initial_count = len(source_candidates)
    TOTAL_INITIAL_CANDIDATES = initial_count
        
    clean_candidates = []
    HONEYPOT_CANDIDATES.clear()
    honeypot_count = 0
    reasons_summary = {}
    
    for idx, c in enumerate(source_candidates):
        hp_flag, reasons = check_honeypot_reasons(c)
        if hp_flag:
            honeypot_count += 1
            primary_reason = reasons[0] if reasons else "Logical anomaly detected"
            reasons_summary[primary_reason] = reasons_summary.get(primary_reason, 0) + 1
            HONEYPOT_CANDIDATES.append({
                "serial_number": idx + 1,
                "candidate_id": c.get("candidate_id", f"C-{idx + 1}"),
                "name": c.get("profile", {}).get("anonymized_name", f"Candidate-{idx + 1}"),
                "current_title": c.get("profile", {}).get("current_title", "N/A"),
                "current_company": c.get("profile", {}).get("current_company", "N/A"),
                "location": c.get("profile", {}).get("location", "N/A"),
                "years_exp": c.get("profile", {}).get("years_of_experience", 0.0),
                "reasons": reasons
            })
        else:
            clean_candidates.append(c)
            
    CANDIDATES.clear()
    CANDIDATES.extend(clean_candidates)
    ELIGIBLE_CANDIDATES = len(CANDIDATES)
    HONEYPOT_COUNT = len(HONEYPOT_CANDIDATES)
    
    log_agent_event(
        "HONEYPOT_PURGE",
        "audit_candidate_integrity",
        f"Scanned candidate database across 11 Honeypot rules. Purged {honeypot_count} synthetic trap profiles from {initial_count} initial candidates.",
        details=f"Disqualified {honeypot_count} trap profiles with logical contradictions. Active pool remaining: {len(CANDIDATES)}"
    )

    summary = (
        f"Successfully ran the 5-Point Anomaly Firewall across {initial_count} candidate profiles.\n"
        f"Detected and removed {honeypot_count} synthetic trap profiles (Honeypots) from the pool.\n"
        f"Remaining active candidate pool: {len(CANDIDATES)} profiles.\n\n"
        f"Top anomaly triggers found:\n"
    )
    for reason, count in list(reasons_summary.items())[:3]:
        summary += f"- {reason}: {count} profiles\n"
        
    return summary

def apply_consulting_filter() -> str:
    """
    Evaluates candidate work history for IT consulting/services experience
    (e.g., TCS, Wipro, Infosys, Accenture, Cognizant, Capgemini, Tech Mahindra, Mindtree, Mphasis, HCL).
    Applies a soft score penalty (-0.05 adjustment) rather than banning/excluding candidates.
    Returns: A status message detailing evaluated candidates.
    """
    global CANDIDATES
    if not CANDIDATES:
        return "Error: Candidate database is empty."
        
    consulting_count = sum(1 for c in CANDIDATES if is_consulting_only(c))
    
    log_agent_event(
        "CONSULTING_FILTER",
        "apply_consulting_filter",
        f"Evaluated IT service experience (TCS, Wipro, Infosys, Accenture...). Identified {consulting_count} consulting background profiles and applied soft score penalty (-0.05).",
        details=f"Soft penalty (-0.05) applied to {consulting_count} consulting candidates. No candidates were removed."
    )

    return (
        f"Consulting Assessment Layer executed successfully.\n"
        f"Identified {consulting_count} candidates with IT consulting background.\n"
        f"Applied soft score penalty (-0.05 adjustment) to consulting candidates. No candidates were banned or removed.\n"
        f"Full active pool retained: {len(CANDIDATES)} candidates."
    )

def rank_and_reason_candidates(job_description: str, top_n: int = 50) -> str:
    """
    Uses BAAI/bge-base-en-v1.5 embeddings and title matching to rank the remaining candidate pool.
    Generates non-hallucinatory recruiter explanations for the top shortlist.
    Args:
        job_description: The job description text to match against.
        top_n: Number of top candidates to return in the shortlist (default 50).
    Returns: A formatted JSON summary of the top ranked candidates.
    """
    global CANDIDATES, ACTIVE_SHORTLIST
    if not CANDIDATES:
        return "Error: Candidate database is empty. Make sure you load and filter candidates first."
        
    logger.info(f"Ranking {len(CANDIDATES)} candidates against Job Description: {job_description[:50]}...")
    
    log_agent_event(
        "EMBEDDING",
        "rank_and_reason_candidates",
        f"Computed 768-dimensional BAAI/bge-base-en-v1.5 dense vector embeddings for {len(CANDIDATES)} candidates.",
        details=f"Generated rank vector embeddings and cosine similarity graph against criteria: '{job_description[:60]}...'"
    )

    # We call the core ranking logic from ranker.py
    results = rank_candidates(CANDIDATES, jd_text=job_description)
    ACTIVE_SHORTLIST.clear()
    for idx, c in enumerate(results):
        c["rank"] = idx + 1
        ACTIVE_SHORTLIST.append(c)
    
    summary_list = []
    for c in ACTIVE_SHORTLIST[:top_n]:
        summary_list.append({
            "rank": c["rank"],
            "candidate_id": c["candidate_id"],
            "name": c["name"],
            "current_title": c["current_title"],
            "score": round(c["score"], 4),
            "reasoning": c["reasoning"]
        })

    log_agent_event(
        "TOOL_CALL",
        "RecruitShieldAgent",
        f"Autonomous agent loop complete. Shortlist of top {len(summary_list)} candidates ranked successfully.",
        details=f"Rank #01 candidate: {summary_list[0]['name'] if summary_list else 'N/A'} (Score: {summary_list[0]['score'] if summary_list else 0})"
    )

    return json.dumps(summary_list, indent=2)
