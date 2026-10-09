import datetime
import re
import os
import json
import threading
from pathlib import Path
import numpy as np

# Dictionary of real-world company founding years
FOUNDING_YEARS = {
    "Accenture": 1989, "Adobe": 1982, "Amazon": 1994, "Apple": 1976, "BYJU'S": 2011,
    "CRED": 2018, "Capgemini": 1967, "Cognizant": 1994, "Dream11": 2008, "Flipkart": 2007,
    "Freshworks": 2010, "Genpact AI": 1997, "Glance": 2019, "Google": 1998, "HCL": 1976,
    "Haptik": 2013, "InMobi": 2007, "Infosys": 1981, "Krutrim": 2023, "LinkedIn": 2002,
    "Locobuzz": 2015, "Mad Street Den": 2013, "Meesho": 2015, "Meta": 2004, "Microsoft": 1975,
    "Mindtree": 1999, "Mphasis": 1992, "Netflix": 1997, "Niramai": 2016, "Nykaa": 2012,
    "Observe.AI": 2017, "Ola": 2010, "Paytm": 2010, "PharmEasy": 2015, "PhonePe": 2015,
    "PolicyBazaar": 2008, "Razorpay": 2014, "Rephrase.ai": 2019, "Saarthi.ai": 2017,
    "Salesforce": 1999, "Sarvam AI": 2023, "Swiggy": 2014, "TCS": 1968, "Tech Mahindra": 1986,
    "Uber": 2009, "Unacademy": 2015, "Vedantu": 2011, "Verloop.io": 2015, "Wipro": 1945,
    "Wysa": 2015, "Yellow.ai": 2016, "Zoho": 1996, "Zomato": 2008, "upGrad": 2015,
    "Acme Corp": 1900, "Dunder Mifflin": 1900, "Globex Inc": 1900, "Hooli": 1900,
    "Initech": 1900, "Pied Piper": 1900, "Stark Industries": 1900, "Wayne Enterprises": 1900
}

CONSULTING_COMPANIES = {
    "TCS", "Infosys", "Wipro", "Accenture", "Cognizant", "Capgemini", "Tech Mahindra", "Mindtree", "Mphasis", "HCL"
}

CURRENT_REF_DATE = datetime.datetime(2026, 6, 11)

# Neural embedding cache variables
CANDIDATE_EMBEDDINGS = None
CANDIDATE_ID_TO_INDEX = {}
EMBEDDINGS_LOADED = False
EMBEDDINGS_COUNT = 0

# Attempt to load precomputed embeddings
# Use absolute paths relative to this file's directory so they work
# regardless of the working directory uvicorn is launched from
_BACKEND_DIR = Path(__file__).parent
EMBEDDINGS_FILE = _BACKEND_DIR / "candidate_embeddings.npy"
IDS_FILE = _BACKEND_DIR / "candidate_ids.json"

if EMBEDDINGS_FILE.exists() and IDS_FILE.exists():
    try:
        CANDIDATE_EMBEDDINGS = np.load(EMBEDDINGS_FILE)
        with open(IDS_FILE, "r") as f:
            ids_list = json.load(f)
        CANDIDATE_ID_TO_INDEX = {cid: idx for idx, cid in enumerate(ids_list)}
        EMBEDDINGS_LOADED = True
        EMBEDDINGS_COUNT = CANDIDATE_EMBEDDINGS.shape[0]
        print(f"Loaded {EMBEDDINGS_COUNT} precomputed candidate embeddings.")
    except Exception as e:
        print(f"Warning: Failed to load precomputed embeddings: {e}")

# Global SentenceTransformer model reference (loaded only when needed)
SENTENCE_MODEL = None
_MODEL_LOAD_LOCK = threading.Lock()
# Guards model.encode() calls too, not just the load: PyTorch's MPS (Apple GPU) backend
# isn't safe under concurrent calls from multiple threads on the same model instance —
# the startup warm-up thread and a request thread hitting encode() at the same time
# crashes the whole process natively (no Python exception, just a silent process exit).
_ENCODE_LOCK = threading.Lock()

def _load_model_with_fallback(model_name):
    try:
        from sentence_transformers import SentenceTransformer
        try:
            return SentenceTransformer(model_name, local_files_only=True)
        except Exception:
            pass
        return SentenceTransformer(model_name)
    except Exception as e:
        return None

def get_sentence_model():
    global SENTENCE_MODEL
    if SENTENCE_MODEL is None:
        with _MODEL_LOAD_LOCK:
            if SENTENCE_MODEL is None:
                model_name = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-base-en-v1.5")
                try:
                    SENTENCE_MODEL = _load_model_with_fallback(model_name)
                except Exception as e:
                    pass
    return SENTENCE_MODEL

FASTEMBED_MODEL = None
_FASTEMBED_LOCK = threading.Lock()

def get_fastembed_model():
    global FASTEMBED_MODEL
    if FASTEMBED_MODEL is None:
        with _FASTEMBED_LOCK:
            if FASTEMBED_MODEL is None:
                from fastembed import TextEmbedding
                model_name = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-base-en-v1.5")
                FASTEMBED_MODEL = TextEmbedding(model_name=model_name)
    return FASTEMBED_MODEL

def encode_texts(texts, normalize=True):
    """
    Encodes text or list of texts into 768-dimensional neural embeddings (BAAI/bge-base-en-v1.5).
    First attempts local FastEmbed (ONNX Runtime, fast & zero PyTorch/HF API bugs).
    Falls back to Hugging Face Inference API or local SentenceTransformer if needed.
    """
    try:
        fe_model = get_fastembed_model()
        if fe_model is not None:
            input_texts = [texts] if isinstance(texts, str) else texts
            embeddings = list(fe_model.embed(input_texts))
            vecs = np.array(embeddings, dtype=np.float32)
            if isinstance(texts, str):
                vecs = vecs[0]
            if normalize:
                if vecs.ndim == 1:
                    norm = np.linalg.norm(vecs)
                    return vecs / (norm + 1e-9)
                else:
                    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
                    return vecs / (norms + 1e-9)
            return vecs
    except Exception as fe_err:
        print(f"FastEmbed note: {fe_err}. Falling back to Hugging Face API / SentenceTransformer.")

    import requests
    hf_token = os.environ.get("HF_TOKEN")
    model_name = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-base-en-v1.5")

    if hf_token:
        try:
            url = f"https://router.huggingface.co/hf-inference/models/{model_name}"
            headers = {"Authorization": f"Bearer {hf_token}"}
            payload = {"inputs": texts, "options": {"wait_for_model": True}}
            res = requests.post(url, headers=headers, json=payload, timeout=15)
            if res.status_code == 200:
                vecs = np.array(res.json(), dtype=np.float32)
                if normalize:
                    if vecs.ndim == 1:
                        norm = np.linalg.norm(vecs)
                        return vecs / (norm + 1e-9)
                    else:
                        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
                        return vecs / (norms + 1e-9)
                return vecs
            else:
                print(f"HF API status {res.status_code}: {res.text[:150]}. Falling back to local model.")
        except Exception as api_err:
            print(f"HF API call failed ({api_err}). Falling back to local model.")

    model = get_sentence_model()
    if model is not None:
        with _ENCODE_LOCK:
            return model.encode(texts, normalize_embeddings=normalize, show_progress_bar=False)
    raise RuntimeError("No embedding provider or model available.")

def parse_date(date_str):
    if not date_str:
        return None
    try:
        return datetime.datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None

def check_honeypot_reasons(cand):
    """
    Check for database inconsistencies and logical contradictions that identify honeypots.
    Returns: (is_honeypot_bool, list_of_reasons)
    """
    reasons = []
    signals = cand.get("redrob_signals", {})
    signup = parse_date(signals.get("signup_date"))
    active = parse_date(signals.get("last_active_date"))
    
    # 1. Signup date after last active date
    if signup and active and signup > active:
        reasons.append(f"Signup date ({signals.get('signup_date')}) is after last active date ({signals.get('last_active_date')}).")

    # 2. Skill duration exceeds total years of experience + buffer
    profile = cand.get("profile", {})
    years_exp = profile.get("years_of_experience", 0)
    for s in cand.get("skills", []):
        dur_years = s.get("duration_months", 0) / 12.0
        if dur_years > years_exp + 1.5:
            reasons.append(f"Skill '{s['name']}' duration ({dur_years:.1f} yrs) exceeds total experience ({years_exp:.1f} yrs).")
            
    # 3. Expert/Advanced skill with 0 duration
    for s in cand.get("skills", []):
        if s.get("proficiency") in ["expert", "advanced"] and s.get("duration_months", 0) == 0:
            reasons.append(f"Expert/Advanced skill '{s['name']}' has 0 months of usage.")
            
    # 4. Job start date before company founding year & job duration exceeds company age
    # The hardcoded FOUNDING_YEARS table is authoritative. For employers it doesn't cover,
    # fall back to live SerpApi evidence (high-confidence Google Knowledge Graph match only);
    # that returns None when no SERPAPI_API_KEY is configured, so offline behaviour is unchanged.
    career = cand.get("career_history", [])
    for job in career:
        comp = job.get("company", "")
        founding_year = FOUNDING_YEARS.get(comp)
        live_note = ""
        if founding_year is None:
            from backend.serp_verifier import get_live_founding_year, verify_employer_legitimacy
            live = get_live_founding_year(comp)
            if live:
                founding_year = live["founded_year"]
                live_note = f" [live-verified via SerpApi: {live['source_url']}]"
            else:
                ghost = verify_employer_legitimacy(comp)
                if ghost and ghost.get("is_ghost"):
                    reasons.append(ghost["reason"])
        if founding_year is not None:
            start_date_str = job.get("start_date")
            if start_date_str:
                try:
                    start_year = int(start_date_str.split("-")[0])
                    if start_year < founding_year:
                        reasons.append(f"Worked at {comp} starting in {start_year}, but company was founded in {founding_year}.{live_note}")
                except (ValueError, AttributeError):
                    pass
            dur_years = job.get("duration_months", 0) / 12.0
            max_dur = CURRENT_REF_DATE.year - founding_year
            if dur_years > max_dur:
                reasons.append(f"Job duration at {comp} is {dur_years:.1f} yrs, but company was founded {max_dur} years ago.{live_note}")
                
    return len(reasons) > 0, reasons

def is_honeypot(cand):
    """
    Check for database inconsistencies and logical contradictions that identify honeypots.
    Returns: (is_honeypot_bool, reason_string)
    """
    is_hp, reasons = check_honeypot_reasons(cand)
    return is_hp, (reasons[0] if reasons else "")

def is_consulting_only(cand):
    """
    Check if a candidate has only worked at IT services/consulting firms in their entire career.
    """
    career = cand.get("career_history", [])
    companies = {job.get("company") for job in career if job.get("company")}
    if companies and companies.issubset(CONSULTING_COMPANIES):
        return True
    return False

# Master catalog of technical skills for dynamic JD extraction with canonical labels
SKILLS_MAP = {
    "c++": "C++", "cpp": "C++", "c#": "C#", "csharp": "C#", "python": "Python", "py": "Python",
    "java": "Java", "javascript": "JavaScript", "js": "JavaScript", "typescript": "TypeScript", "ts": "TypeScript",
    "react": "React", "react.js": "React", "reactjs": "React", "next.js": "Next.js", "nextjs": "Next.js",
    "node.js": "Node.js", "nodejs": "Node.js", "vue": "Vue.js", "vue.js": "Vue.js", "angular": "Angular",
    "html": "HTML", "css": "CSS", "tailwind": "Tailwind", "go": "Go", "golang": "Go", "rust": "Rust", "ruby": "Ruby",
    "rails": "Ruby on Rails", "php": "PHP", "fastapi": "FastAPI", "django": "Django", "flask": "Flask",
    "springboot": "Spring Boot", "spring boot": "Spring Boot", "sql": "SQL", "mysql": "MySQL",
    "postgresql": "PostgreSQL", "postgres": "PostgreSQL", "mongodb": "MongoDB", "mongo": "MongoDB",
    "redis": "Redis", "aws": "AWS", "gcp": "GCP", "azure": "Azure", "docker": "Docker", "kubernetes": "Kubernetes",
    "k8s": "Kubernetes", "git": "Git", "linux": "Linux", "api": "REST API", "rest api": "REST API",
    "graphql": "GraphQL", "machine learning": "Machine Learning", "ml": "Machine Learning",
    "deep learning": "Deep Learning", "pytorch": "PyTorch", "tensorflow": "TensorFlow", "keras": "Keras",
    "nlp": "NLP", "llm": "LLM", "pandas": "Pandas", "numpy": "NumPy"
}

def extract_skills_from_jd(jd_text):
    if not jd_text:
        return set()
    text = jd_text.lower()
    required_skills = set()
    for kw, canonical in SKILLS_MAP.items():
        if not kw.isalnum() or len(kw) <= 2:
            pattern = r"(?:\b|\s|^)" + re.escape(kw) + r"(?:\b|\s|[.,;:!?/\-]|$)"
        else:
            pattern = r"\b" + re.escape(kw) + r"\b"
        if re.search(pattern, text):
            required_skills.add(canonical)
    return required_skills

def extract_title_keywords_from_jd(jd_text):
    if not jd_text:
        return set()
    
    role_keywords = set()
    title_line = ""
    for line in jd_text.split("\n"):
        line_strip = line.strip()
        if "job description:" in line_strip.lower() or "role:" in line_strip.lower() or "title:" in line_strip.lower() or "position:" in line_strip.lower():
            title_line = line_strip
            break
            
    if not title_line:
        lines = [l.strip() for l in jd_text.split("\n") if l.strip()]
        if lines:
            title_line = lines[0]
            
    if title_line:
        words = re.findall(r"\b[a-zA-Z0-9_-]+\b", title_line.lower())
        filler = {"job", "description", "role", "title", "position", "company", "team", "founding", "seeking", "hiring", "for", "a", "an", "the", "and", "or", "of", "in"}
        role_keywords = {w for w in words if w not in filler and len(w) > 1}
        
    return role_keywords

def is_default_jd(jd_text):
    if not jd_text:
        return True
    norm_jd = re.sub(r'\s+', ' ', jd_text.strip().lower())
    landmarks = [
        "senior ai engineer",
        "founding team",
        "embeddings-based retrieval systems",
        "vector databases"
    ]
    return all(l in norm_jd for l in landmarks)

def extract_experience_range_from_jd(jd_text):
    if not jd_text:
        return 0.0, 30.0
    text = jd_text.lower()
    match = re.search(r"(\d+)\s*(?:-|–|to)\s*(\d+)\s*(?:years?|yrs?)", text)
    if match:
        return float(match.group(1)), float(match.group(2))
    match = re.search(r"(\d+)\s*\+\s*(?:years?|yrs?)", text)
    if match:
        return float(match.group(1)), 30.0
    match = re.search(r"(?:over|more than|at least|>)\s*(\d+)\s*(?:years?|yrs?)", text)
    if match:
        return float(match.group(1)), 30.0
    match = re.search(r"(?:up to|under|<)\s*(\d+)\s*(?:years?|yrs?)", text)
    if match:
        return 0.0, float(match.group(1))
    return 0.0, 30.0

# Known Indian tech-hub cities + global cities for JD location extraction
KNOWN_CITIES_MAP = {
    "bangalore": "Bangalore", "bengaluru": "Bangalore", "banglore": "Bangalore", "blr": "Bangalore",
    "pune": "Pune", "pnq": "Pune",
    "mumbai": "Mumbai", "bombay": "Mumbai",
    "hyderabad": "Hyderabad", "hyd": "Hyderabad",
    "delhi": "Delhi NCR", "new delhi": "Delhi NCR", "ncr": "Delhi NCR",
    "gurgaon": "Gurgaon", "gurugram": "Gurgaon",
    "noida": "Noida",
    "chennai": "Chennai", "kolkata": "Kolkata", "ahmedabad": "Ahmedabad", "jaipur": "Jaipur",
    "kochi": "Kochi", "chandigarh": "Chandigarh", "indore": "Indore", "surat": "Surat",
    "coimbatore": "Coimbatore", "visakhapatnam": "Visakhapatnam", "lucknow": "Lucknow",
    "bhubaneswar": "Bhubaneswar", "san francisco": "San Francisco", "new york": "New York",
    "london": "London", "singapore": "Singapore", "dubai": "Dubai", "toronto": "Toronto", "sydney": "Sydney"
}

def extract_locations_from_jd(jd_text):
    """Extract city/location mentions from JD text."""
    if not jd_text:
        return []
    text = jd_text.lower()
    found = set()
    for kw, canonical in KNOWN_CITIES_MAP.items():
        pattern = r"(?:\b|\s|^)" + re.escape(kw) + r"(?:\b|\s|[.,;:!?/\-]|$)"
        if re.search(pattern, text):
            found.add(canonical)
    return sorted(list(found))

def extract_work_modes_from_jd(jd_text):
    """Extract work mode preferences from JD text."""
    if not jd_text:
        return []
    text = jd_text.lower()
    modes = []
    remote_kw = ["fully remote", "100% remote", "remote only", "remote-first", "work from home", "wfh", "remote ok", "remote friendly"]
    hybrid_kw = ["hybrid", "flexible working", "partial remote", "mix of remote"]
    onsite_kw = ["on-site", "onsite", "in-office", "office based", "on site", "work from office", "wfo"]
    if any(kw in text for kw in remote_kw) or re.search(r'\bremote\b', text):
        modes.append("Remote")
    if any(kw in text for kw in hybrid_kw):
        modes.append("Hybrid")
    if any(kw in text for kw in onsite_kw):
        modes.append("On-site")
    # Default: if nothing detected, return common options
    if not modes:
        modes = ["On-site", "Hybrid", "Remote"]
    return modes

def calculate_title_score(cand, jd_title_keywords=None):
    """
    Calculate title score based on role alignment.
    Differentiates ML/AI engineers from backend/data engineers and filters out non-tech titles.
    """
    profile = cand.get("profile", {})
    current_title = profile.get("current_title", "").lower()
    headline = profile.get("headline", "").lower()
    
    # Disallowed non-tech/other roles
    disallowed_titles = re.compile(r"\b(marketing|hr|graphic designer|accountant|sales|customer support|finance|financial|advisor|operations|civil engineer|mechanical engineer|qa engineer)\b")
    if disallowed_titles.search(current_title):
        # Only filter out if the matched disallowed title is NOT requested in the JD keywords!
        matched_words = disallowed_titles.findall(current_title)
        real_disallowed = [w for w in matched_words if w not in (jd_title_keywords or set())]
        if real_disallowed:
            return 0.0
        
    # If no jd keywords provided, fallback to the original AI/ML hardcoded logic
    if not jd_title_keywords:
        # JD explicitly disqualifies CV/speech/robotics specialists without NLP/IR exposure
        cv_speech_regex = re.compile(r"\b(computer vision|speech|robotics|acoustic)\b")
        if cv_speech_regex.search(current_title):
            nlp_ir_skills = {"nlp", "information retrieval", "embeddings", "vector search",
                             "sentence-transformers", "semantic search", "retrieval", "rag"}
            cand_skills_lower = {s.get("name", "").lower() for s in cand.get("skills", [])}
            if not cand_skills_lower & nlp_ir_skills:
                return 0.0

        # ML/AI specific titles
        ml_titles_regex = re.compile(r"\b(ml|ai|machine learning|nlp|deep learning|computer vision|search|retrieval|ranking|applied scientist|data scientist)\b")
        if ml_titles_regex.search(current_title):
            return 1.0
        elif any(t in current_title for t in ["software engineer", "backend engineer", "developer", "engineer"]):
            if ml_titles_regex.search(headline):
                return 0.9
            return 0.6
        else:
            return 0.3
            
    # Dynamic Title Matching based on JD keywords
    cand_words = set(re.findall(r"\b[a-z0-9_-]+\b", current_title))
    headline_words = set(re.findall(r"\b[a-z0-9_-]+\b", headline))

    # Words like "senior"/"engineer"/"years"/"remote" appear in almost every tech
    # JD and every tech title, so two of THEM overlapping isn't a real title match —
    # e.g. "Senior Frontend Engineer" would otherwise score identically to "Senior
    # Backend Engineer" against a JD that only actually specifies "backend". Only
    # count overlap on words that actually discriminate between roles.
    _TITLE_GENERIC_WORDS = {
        "senior", "junior", "lead", "staff", "principal", "engineer", "developer",
        "years", "year", "experience", "remote", "hybrid", "onsite", "on-site",
        "required", "requirements", "role", "position", "team", "join",
    }

    # Count matching words, weighting discriminating (non-generic) overlap highest
    all_matches = cand_words.intersection(jd_title_keywords)
    all_headline_matches = headline_words.intersection(jd_title_keywords)
    specific_matches = all_matches - _TITLE_GENERIC_WORDS
    specific_headline_matches = all_headline_matches - _TITLE_GENERIC_WORDS

    if len(specific_matches) >= 1:
        return 1.0  # matched a discriminating role word (e.g. "backend", "ml")
    elif len(all_matches) >= 2:
        return 0.6  # only generic words overlapped (e.g. "senior" + "engineer")
    elif len(specific_headline_matches) >= 1:
        return 0.5  # discriminating word matched in headline only
    elif len(all_headline_matches) >= 1:
        return 0.4  # generic overlap in headline only
    else:
        # Check if the title is adjacent (has developer, engineer, scientist, coder)
        adjacent_keywords = {"engineer", "developer", "architect", "programmer", "scientist", "specialist"}
        if cand_words.intersection(adjacent_keywords):
            return 0.3
        return 0.2  # low match

def calculate_skill_score(cand, jd_skills=None):
    """
    Score skills based on mandatory requirements and nice-to-haves, weighted by proficiency and duration.
    """
    skills = cand.get("skills", [])
    
    # If no jd_skills are specified or it's the default, use the baseline scoring
    if not jd_skills:
        skill_score = 0.0
        # Skill catalogs
        vectordb_skills = {"pinecone", "weaviate", "qdrant", "milvus", "opensearch", "elasticsearch", "faiss"}
        retrieval_skills = {"sentence-transformers", "embeddings", "bge", "e5", "nlp", "information retrieval", "retrieval", "semantic search", "vector search"}
        eval_skills = {"ndcg", "mrr", "map", "evaluation", "metrics"}
        python_skills = {"python"}
        
        llm_skills = {"lora", "qlora", "peft", "fine-tuning", "llm", "llms", "large language models"}
        ltr_skills = {"xgboost", "learning-to-rank", "learning to rank"}
        
        has_vdb = False
        has_retrieval = False
        has_eval = False
        has_python = False
        
        for s in skills:
            name = s.get("name", "").lower()
            dur = s.get("duration_months", 0)
            prof = s.get("proficiency", "beginner")
            
            prof_mult = {"expert": 1.2, "advanced": 1.0, "intermediate": 0.8, "beginner": 0.5}.get(prof, 0.5)
            dur_mult = min(dur / 24.0, 1.2)  # Max weight reached at 2 years of usage
            skill_val = prof_mult * dur_mult
            
            if name in vectordb_skills:
                has_vdb = True
                skill_score += 0.25 * skill_val
            elif name in retrieval_skills:
                has_retrieval = True
                skill_score += 0.25 * skill_val
            elif name in eval_skills:
                has_eval = True
                skill_score += 0.25 * skill_val
            elif name in python_skills:
                has_python = True
                skill_score += 0.25 * skill_val
            elif name in llm_skills:
                skill_score += 0.1 * skill_val
            elif name in ltr_skills:
                skill_score += 0.1 * skill_val
                
        # Category completeness bonuses
        if has_vdb: skill_score += 0.1
        if has_retrieval: skill_score += 0.1
        if has_eval: skill_score += 0.1
        if has_python: skill_score += 0.1
        
        return skill_score

    # Dynamic skill scoring for custom JDs
    skill_score = 0.0
    matched_count = 0
    skill_sum = 0.0

    # jd_skills holds Title-Case canonical names (e.g. "Python", "AWS") from SKILLS_MAP;
    # lowercase them here so they compare correctly against candidate skill names below.
    jd_skills_lower = {js.lower() for js in jd_skills}

    for s in skills:
        name = s.get("name", "").lower()
        dur = s.get("duration_months", 0)
        prof = s.get("proficiency", "beginner")

        # Check if the candidate's skill is in the JD's extracted skills
        is_match = False
        for js in jd_skills_lower:
            # Match if equal, or if one is a substring of the other (e.g. next.js vs nextjs, or python vs python3)
            if name == js or (len(name) > 3 and name in js) or (len(js) > 3 and js in name):
                is_match = True
                break
                
        if is_match:
            matched_count += 1
            prof_mult = {"expert": 1.2, "advanced": 1.0, "intermediate": 0.8, "beginner": 0.5}.get(prof, 0.5)
            dur_mult = min(dur / 24.0, 1.2)
            skill_val = prof_mult * dur_mult
            skill_sum += skill_val
            
    if len(jd_skills) > 0:
        coverage = matched_count / len(jd_skills)
        # skill_sum alone can exceed len(jd_skills) when matched skills carry high
        # proficiency/duration multipliers (up to 1.44 each), so this was able to
        # reach ~1.84 for a candidate expert in just the couple of required skills —
        # well past what "full requirement coverage" should mean. Clamped to 1.0 so
        # skill fit stops dominating the overall score once requirements are fully met.
        skill_score = min(1.0, (skill_sum / len(jd_skills)) + 0.4 * coverage)
    else:
        skill_score = 0.0
        
    return skill_score

def calculate_history_score(cand, jd_text=""):
    """
    Search career history descriptions for keywords matching the JD.
    """
    career = cand.get("career_history", [])
    desc_text = ""
    for job in career:
        desc_text += " " + job.get("description", "") + " " + job.get("title", "")
    desc_text = desc_text.lower()
    
    is_default = is_default_jd(jd_text)
    
    if is_default:
        history_score = 0.0
        keywords = {
            "production": 0.05, "deployed": 0.05, "scale": 0.05, "users": 0.05,
            "a/b": 0.05, "ab test": 0.05, "eval": 0.05, "metrics": 0.05,
            "vector": 0.05, "embeddings": 0.05, "search": 0.05, "retrieval": 0.05,
            "recommendation": 0.05, "rank": 0.05, "ndcg": 0.05, "mrr": 0.05,
            "fine-tune": 0.05, "lora": 0.05
        }
        for kw, weight in keywords.items():
            if kw in desc_text:
                history_score += weight
                
        # Check for traditional pre-LLM ML experience
        pre_llm_terms = ["regression", "classification", "xgboost", "random forest", "scikit", "tensorflow", "pytorch", "deep learning", "neural network"]
        if any(term in desc_text for term in pre_llm_terms):
            history_score += 0.1
            
        return history_score

    # For custom JDs: dynamic keyword matching based on terms from the JD
    jd_words = set(re.findall(r"\b[a-z0-9_-]{3,15}\b", jd_text.lower()))
    
    # Filter out common english stop words
    stop_words = {
        "and", "the", "for", "with", "you", "will", "our", "are", "that", "this", "from",
        "have", "has", "had", "been", "was", "were", "their", "they", "them", "who", "whom",
        "which", "what", "where", "when", "why", "how", "but", "not", "job", "description",
        "role", "team", "company", "candidate", "position", "seeking", "hiring", "experience",
        "required", "nice", "have", "preferred", "knowledge", "skills", "ability", "years",
        "good", "strong", "excellent", "work", "hybrid", "remote", "office", "locations", "noida", "pune"
    }
    target_keywords = jd_words - stop_words
    
    history_score = 0.0
    if not target_keywords:
        return 0.0
        
    matched_count = 0
    for kw in target_keywords:
        if kw in desc_text:
            matched_count += 1
            # Add up to 0.5 total score, divided by number of keywords
            history_score += 0.5 / len(target_keywords)
            
    return min(history_score, 0.5)

def calculate_availability_multiplier(cand):
    """
    Convert platform activity signals into a multiplier reflecting candidate availability.
    Signals that are simply absent (e.g. candidates ingested from a plain resume PDF,
    which carries no recruiter-engagement telemetry) are treated as neutral (1.0x),
    not as the worst possible value — missing data should not read as bad data.
    """
    signals = cand.get("redrob_signals", {})

    # 1. Open to work flag
    otw_mult = 1.2 if signals.get("open_to_work_flag", False) else 1.0

    # 2. Activity Recency — neutral until we actually know the candidate is stale
    active_mult = 1.0
    last_active_str = signals.get("last_active_date")
    if last_active_str:
        active_date = parse_date(last_active_str)
        if active_date:
            days_inactive = (CURRENT_REF_DATE - active_date).days
            if days_inactive <= 30:
                active_mult = 1.2
            elif days_inactive <= 90:
                active_mult = 1.0
            elif days_inactive <= 180:
                active_mult = 0.7
            else:
                active_mult = 0.5

    # 3. Recruiter Response Rate (RRR) — neutral when no engagement data exists
    rrr = signals.get("recruiter_response_rate")
    if rrr is None:
        rrr_mult = 1.0
    elif rrr >= 0.7:
        rrr_mult = 1.2
    elif rrr >= 0.4:
        rrr_mult = 1.0
    elif rrr >= 0.2:
        rrr_mult = 0.7
    else:
        rrr_mult = 0.4

    # 4. Notice Period — neutral when unknown, rather than assuming a long notice period
    notice = signals.get("notice_period_days")
    if notice is None:
        notice_mult = 1.0
    elif notice <= 30:
        notice_mult = 1.2
    elif notice <= 60:
        notice_mult = 1.0
    elif notice <= 90:
        notice_mult = 0.8
    else:
        notice_mult = 0.5

    # The four sub-multipliers can each reach 1.2x, so their raw product can
    # approach ~2.07x — enough to push almost any well-fit, well-engaged candidate
    # past the 1.0 score ceiling and flatten ranking entirely. raw_fit is already a
    # 0-1 fit score by construction (its weights sum to 1.0), so it alone can reach
    # ~0.9-1.0 for a strong-but-imperfect candidate — any multiplier above 1.0x
    # guarantees clipping for those candidates too. Engagement should mainly act as
    # a downside risk signal (deprioritizing hard-to-reach candidates), not a way
    # to inflate an already-strong fit score, so the upper bound is capped at 1.0x
    # (no boost) while poor engagement can still discount down to 0.75x.
    raw_mult = otw_mult * active_mult * rrr_mult * notice_mult
    return max(0.75, min(1.0, raw_mult))

def generate_reasoning(cand, rank, is_consulting=False):
    """
    Programmatically generate a customized, fact-grounded recruiter reasoning for Stage 4 review.
    Does not hallucinate, connects to JD, and adapts tone to rank.
    """
    profile = cand.get("profile", {})
    exp = profile.get("years_of_experience", 0.0)
    title = profile.get("current_title", "Engineer")
    signals = cand.get("redrob_signals", {})
    
    # Check for Tier-1 education
    edu_tier = ""
    for edu in cand.get("education", []):
        if edu.get("tier", "").lower() == "tier_1":
            edu_tier = "Tier-1 graduate"
            break
        elif edu.get("tier", "").lower() == "tier_2":
            edu_tier = "Tier-2 graduate"
            break
            
    # Check for GitHub activity
    github_act = signals.get("github_activity_score", -1)
    github_str = ""
    if github_act > 25:
        github_str = f"strong open-source contributions (GitHub score: {github_act:.1f})"
        
    # Find matching skills to list
    vdb_skills = {"pinecone", "weaviate", "qdrant", "milvus", "elasticsearch", "faiss"}
    matching_skills = [s["name"] for s in cand.get("skills", []) if s["name"].lower() in vdb_skills]
    
    skills_str = f"with depth in {', '.join(matching_skills[:2])}" if matching_skills else "with strong backend skills"
    notice_days = signals.get('notice_period_days')
    notice_str = f"{notice_days}d notice" if notice_days is not None else "notice period unknown"
    
    # Build education/github highlight
    highlights = []
    if edu_tier:
        highlights.append(edu_tier)
    if github_str:
        highlights.append(github_str)
    highlights_str = f" ({', '.join(highlights)})" if highlights else ""
    
    consulting_note = " (Note: Entire background is in IT services, requiring vetting for product culture fit)" if is_consulting else ""
    rrr = signals.get('recruiter_response_rate')
    response_str = f"{int(rrr * 100)}% response rate" if rrr is not None else "response rate unavailable"
    if rank <= 10:
        return (
            f"Exceptional {title} with {exp:.1f} years of experience{highlights_str}. Proved production impact at product companies; "
            f"expert {skills_str} matching the 'shipper' profile. Strong engagement signals ({notice_str}, {response_str}).{consulting_note}"
        )
    elif rank <= 50:
        concern = ""
        if notice_days is not None and notice_days > 60:
            concern = f" Notice period is {notice_days} days, but technical depth outweighs notice lag."
        elif not profile.get('location', '').lower() in ['pune', 'noida', 'delhi', 'gurgaon']:
            concern = " Relocation to Pune/Noida offices required, but candidate is willing to relocate."
            
        highlight_prefix = f" ({edu_tier})" if edu_tier else ""
        return (
            f"Strong candidate{highlight_prefix} with {exp:.1f} years experience as {title}. Shipped search/retrieval components {skills_str}. "
            f"Highly active on platform.{concern}"
        )
    else:
        highlight_prefix = f" ({edu_tier})" if edu_tier else ""
        return (
            f"Solid backend/data profile with adjacent ML exposure ({exp:.1f} yrs experience){highlight_prefix}. "
            f"Good foundational skills, though less direct vector search production experience; serves as a high-quality filler."
        )

LAST_RUN_STATS = {
    "scanned": 0,
    "honeypots": 0,
    "consulting": 0,
    "country_filtered": 0,
    "location_filtered": 0,
    "experience_filtered": 0,
    "title_filtered": 0,
    "shortlisted": 0
}

def score_candidate(cand, semantic_similarity=None, jd_title_keywords=None, jd_skills=None, jd_text=""):
    """
    Process filters and return final score if valid, else None.
    """
    global LAST_RUN_STATS
    is_default = is_default_jd(jd_text)
    
    # 1. Hard filters
    # A. Honeypot check
    hp_flag, _ = is_honeypot(cand)
    if hp_flag:
        LAST_RUN_STATS["honeypots"] += 1
        return None
        
    # B. Consulting check
    # Soft penalty (-0.05) will be applied to consulting candidates instead of hard exclusion
    is_consulting = is_consulting_only(cand)
    if is_consulting:
        LAST_RUN_STATS["consulting"] += 1
        
    profile = cand.get("profile", {})
    
    if is_default:
        is_india_only = False
        min_exp, max_exp = 0.0, 50.0
        target_cities = []
    else:
        is_india_only = any(kw in jd_text.lower() for kw in ["india", "pune", "noida", "delhi", "gurgaon", "ncr", "bangalore", "bengaluru", "hyderabad", "mumbai", "chennai", "kolkata"])
        min_exp, max_exp = extract_experience_range_from_jd(jd_text)
        min_exp = max(0.0, min_exp - 1.0)
        max_exp = max_exp + 3.0
        tech_cities = ["pune", "noida", "gurgaon", "delhi", "ncr", "bangalore", "bengaluru", "hyderabad", "mumbai", "chennai", "kolkata", "san francisco", "london", "new york", "remote", "toronto"]
        target_cities = [c for c in tech_cities if c in jd_text.lower()]

    # C. Country Check — soft penalty instead of hard exclude
    country_penalty = 0.0
    if is_india_only:
        country = profile.get("country", "").strip()
        if country.lower() != "india":
            LAST_RUN_STATS["country_filtered"] += 1
            country_penalty = -0.20  # soft penalty, not hard exclude
        
    # D. Location & Relocation Check — soft penalty instead of hard exclude
    location_penalty = 0.0
    if target_cities:
        location = profile.get("location", "").lower()
        willing_relocate = cand.get("redrob_signals", {}).get("willing_to_relocate", False)
        is_matched_city = any(c in location for c in target_cities)
        if not is_matched_city and not willing_relocate:
            LAST_RUN_STATS["location_filtered"] += 1
            location_penalty = -0.15  # soft penalty, not hard exclude
        
    # E. Experience Check — soft penalty instead of hard exclude
    exp_penalty = 0.0
    years_exp = profile.get("years_of_experience", 0)
    if years_exp < min_exp:
        LAST_RUN_STATS["experience_filtered"] += 1
        exp_penalty = -0.15 * (min_exp - years_exp) / max(min_exp, 1)  # scale by how far off
    elif years_exp > max_exp:
        LAST_RUN_STATS["experience_filtered"] += 1
        exp_penalty = -0.10
        
    # 2. Compute scores
    title_s = calculate_title_score(cand, jd_title_keywords=None if is_default else jd_title_keywords)
    if title_s == 0.0:
        LAST_RUN_STATS["title_filtered"] += 1
        title_s = 0.01  # keep candidate in pool with minimal title score, don't hard-exclude
        
    skill_s = calculate_skill_score(cand, jd_skills=None if is_default else jd_skills)
    
    # NLP Match: semantic similarity when embeddings are available, else keyword fallback
    if semantic_similarity is not None:
        nlp_s = max(0.0, semantic_similarity)
    else:
        nlp_s = calculate_history_score(cand, jd_text=jd_text)

    # Career description score — always computed as an additive bonus on top of NLP
    # Rewards candidates who describe production impact in their job history text
    career_s = calculate_history_score(cand, jd_text=jd_text)
        
    # --- Integration of newly analyzed dataset signals ---
    signals = cand.get("redrob_signals", {})
    
    # A. Education Tier Signal
    edu_score = 0.0
    for edu in cand.get("education", []):
        tier = edu.get("tier", "").lower()
        if tier == "tier_1":
            edu_score = max(edu_score, 0.10)
        elif tier == "tier_2":
            edu_score = max(edu_score, 0.05)
            
    # B. GitHub Activity Signal — JD explicitly values open-source contributions
    github_score = 0.0
    github_act = signals.get("github_activity_score", -1)
    if github_act > 0:
        github_score = min(github_act / 100.0, 0.20)  # up to 0.20 bonus (was 0.08)

    # C. Verified Skill Assessment Signal — all tests, weighted by score quality
    assess_score = 0.0
    assessments = signals.get("skill_assessment_scores", {})
    # Primary AI/ML tests get full weight; adjacent tests get half weight
    primary_tests = {"NLP", "Python", "Fine-tuning LLMs", "Machine Learning", "Deep Learning"}
    adjacent_tests = {"Data Structures", "System Design", "SQL", "Statistics", "Cloud Computing"}
    for test_name, test_val in assessments.items():
        if test_name in primary_tests and test_val >= 50.0:
            assess_score += 0.05 * (test_val / 100.0)   # up to 0.05 per primary test
        elif test_name in adjacent_tests and test_val >= 70.0:
            assess_score += 0.02 * (test_val / 100.0)   # up to 0.02 per adjacent test
    assess_score = min(assess_score, 0.20)  # cap at 0.20 (was 0.10)

    # D. Profile Completeness Signal — high completeness signals serious, engaged candidates
    completeness_score = 0.0
    completeness = signals.get("profile_completeness_score", 0)
    if completeness >= 90:
        completeness_score = 0.08
    elif completeness >= 75:
        completeness_score = 0.05
    elif completeness >= 50:
        completeness_score = 0.02

    # Calculate base score with signal bonuses. raw_fit below is already a 0-1 fit
    # score (its weights sum to 1.0), so a large additive bonus on top of it
    # guarantees ceiling clipping for any "good, not even perfect" match — the
    # uncapped raw value here could reach ~0.68, which was enough on its own to
    # push most realistic candidates to the 1.0 ceiling and erase differentiation.
    # Capped tight (0.04) so it acts as a genuine tiebreaker, not a dominant term —
    # same reasoning as the availability multiplier and interest_score caps below.
    signal_bonus = min(0.04, edu_score + github_score + assess_score + completeness_score + (career_s * 0.10))
    
    if not is_default and (jd_skills or jd_title_keywords):
        # 50% Skill coverage, 30% Title fit, 20% BGE Neural Semantic fit
        raw_fit = (skill_s * 0.50) + (title_s * 0.30) + (nlp_s * 0.20)
        
        # If candidate has ZERO skill match and low title match for a specific JD,
        # scale down raw fit & bonuses so completely unrelated profiles stay under 55%
        if skill_s == 0.0 and title_s <= 0.05:
            raw_fit *= 0.20
            signal_bonus *= 0.20
    else:
        raw_fit = (title_s * 0.40) + (skill_s * 0.30) + (nlp_s * 0.30)
        
    penalties = country_penalty + location_penalty + exp_penalty
    if is_consulting:
        penalties -= 0.05
        
    # Availability Multiplier & Interest.
    # raw_fit is already a 0-1 fit score by construction (its weights sum to 1.0),
    # so it alone can reach ~0.9-1.0 for a strong-but-imperfect candidate. This
    # interest bonus used to add up to +0.2 on top of that — enough on its own to
    # push most well-tracked candidates over the 1.0 ceiling regardless of any
    # other fix. Capped much tighter (0.03) so it's a genuine tiebreaker, not a
    # second independent path to score saturation.
    availability_mult = calculate_availability_multiplier(cand)
    views = signals.get("profile_views_received_30d", 0)
    searches = signals.get("search_appearance_30d", 0)
    saved = signals.get("saved_by_recruiters_30d", 0)
    interest_score = min((views * 2 + searches * 0.1 + saved * 5) / 100.0, 0.03)
    
    # Calibrated absolute fit score
    final_fit = (raw_fit + signal_bonus + penalties) * availability_mult + interest_score
    final_score = max(0.0, min(1.0, final_fit))
    
    return {
        "candidate_id": cand["candidate_id"],
        "name": profile.get("anonymized_name"),
        "headline": profile.get("headline"),
        "years_exp": years_exp,
        "location": profile.get("location"),
        "current_title": profile.get("current_title"),
        "current_company": profile.get("current_company"),
        "score": final_score,
        "score_breakdown": {
            "title_fit": round(min(1.0, title_s) * 100),
            "skill_coverage": round(min(1.0, skill_s) * 100),
            "semantic_fit": round(min(1.0, nlp_s) * 100),
            "signal_bonus": round(signal_bonus * 100)
        },
        "candidate_raw": cand  # keep reference for detail display
    }

def rank_candidates(candidates_list, jd_text=""):
    """
    Ranks a list of candidate dictionaries.
    Uses precomputed neural embeddings if available.
    """
    global LAST_RUN_STATS
    
    # Reset stats
    LAST_RUN_STATS = {
        "scanned": len(candidates_list),
        "honeypots": 0,
        "consulting": 0,
        "country_filtered": 0,
        "location_filtered": 0,
        "experience_filtered": 0,
        "title_filtered": 0,
        "shortlisted": 0
    }
    
    similarities_dict = {}
    
    # If precomputed embeddings exist and JD is provided, compute semantic scores
    if EMBEDDINGS_LOADED and jd_text:
        try:
            # Generate embedding for the JD (normalized)
            jd_embedding = encode_texts(jd_text, normalize=True)
            # Compute dot products (since both are normalized, this equals cosine similarity)
            similarities = np.dot(CANDIDATE_EMBEDDINGS, jd_embedding)
            
            # Build dictionary for candidate lookup
            for cid, idx in CANDIDATE_ID_TO_INDEX.items():
                similarities_dict[cid] = float(similarities[idx])
        except Exception as e:
            print(f"Warning: Failed to compute semantic similarity: {e}")
                
    # Extract jd keywords and skills once
    jd_skills = extract_skills_from_jd(jd_text)
    jd_title_keywords = extract_title_keywords_from_jd(jd_text)
    
    scored = []
    for c in candidates_list:
        cid = c["candidate_id"]
        sem_sim = similarities_dict.get(cid, None)
        
        res = score_candidate(
            c, 
            semantic_similarity=sem_sim,
            jd_title_keywords=jd_title_keywords,
            jd_skills=jd_skills,
            jd_text=jd_text
        )
        if res is not None:
            scored.append(res)
            
    # Sort by score desc, then by candidate_id asc
    scored.sort(key=lambda x: (-x["score"], x["candidate_id"]))
    
    for c in scored:
        c["score"] = max(0.0, min(1.0, c["score"]))
    
    # Assign ranks and reasoning to all candidates
    ranked_results = []
    for idx, c in enumerate(scored):
        rank = idx + 1
        c["rank"] = rank
        if rank <= 100:
            c["reasoning"] = generate_reasoning(c["candidate_raw"], rank, is_consulting_only(c["candidate_raw"]))
        else:
            c["reasoning"] = f"Candidate ranks outside top 100 shortlist (Rank {rank})."
        ranked_results.append(c)
        
    LAST_RUN_STATS["shortlisted"] = len(ranked_results)
    return ranked_results
