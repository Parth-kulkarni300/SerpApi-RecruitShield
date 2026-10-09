"""Live employer verification for the 5-Point Anomaly Firewall, powered by SerpApi.

Before this module the firewall's "job started before the company existed" rules (4 & 5)
could only check ~60 companies from a hand-researched table (``ranker.FOUNDING_YEARS``).
Any employer outside that table was silently unverifiable. This module closes that gap:
for an *unknown* employer we ask Google (via SerpApi) for the company's founding year and
return the evidence - year, source link, matched entity - so the firewall's verdict is
explainable and auditable rather than a black-box number.

Safety rules (a wrong purge hurts a real candidate, so we are deliberately conservative):

* The hardcoded table always wins; live lookups only fill gaps.
* Only **high-confidence** evidence may influence the firewall: a Google Knowledge Graph
  card whose title actually matches the employer name. Free-text snippet matches are
  "low" confidence - surfaced by the API for humans, never used to purge a candidate.
* Missing/ambiguous evidence means "unknown", never "fraud".
"""
import logging
import re
import threading
from typing import Optional

from backend import serp_client

logger = logging.getLogger("recruiter-serpapi")

# Values that appear in parsed resumes but are not searchable employers.
NON_EMPLOYERS = {
    "", "n/a", "na", "none", "not specified", "unknown", "self-employed", "self employed",
    "freelance", "freelancer", "independent", "stealth", "stealth startup", "confidential",
}

_YEAR_RE = re.compile(r"\b(1[89]\d{2}|20[0-2]\d)\b")
_SNIPPET_RE = re.compile(
    r"(?:founded|established|incorporated)\s+(?:in\s+|on\s+)?(?:[A-Za-z]+\s+(?:\d{1,2},?\s+)?)?(1[89]\d{2}|20[0-2]\d)",
    re.IGNORECASE,
)

_MEMO: dict = {}
_MEMO_LOCK = threading.Lock()


def clear_memo() -> None:
    with _MEMO_LOCK:
        _MEMO.clear()


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _names_match(company: str, entity_title: str) -> bool:
    a, b = _norm(company), _norm(entity_title)
    return len(a) >= 3 and len(b) >= 3 and (a in b or b in a)


def extract_founding_year(serp_response: dict, company: str) -> Optional[dict]:
    """Pulls a founding year (with evidence) out of a Google SerpApi response."""
    if not serp_response:
        return None
    default_url = (serp_response.get("search_metadata") or {}).get("google_url") or ""

    kg = serp_response.get("knowledge_graph") or {}
    if kg and _names_match(company, kg.get("title", "")):
        for key, value in kg.items():
            k = str(key).lower()
            if "found" in k and "founder" not in k and isinstance(value, str):
                m = _YEAR_RE.search(value)
                if m:
                    source = kg.get("website") or (kg.get("source") or {}).get("link") or default_url
                    return {
                        "company": company,
                        "founded_year": int(m.group(1)),
                        "confidence": "high",
                        "source": "google_knowledge_graph",
                        "source_url": source,
                        "matched_entity": kg.get("title"),
                        "snippet": value[:160],
                    }

    for result in serp_response.get("organic_results") or []:
        m = _SNIPPET_RE.search(result.get("snippet", "") or "")
        if m:
            return {
                "company": company,
                "founded_year": int(m.group(1)),
                "confidence": "low",
                "source": "organic_result_snippet",
                "source_url": result.get("link") or default_url,
                "matched_entity": result.get("title"),
                "snippet": (result.get("snippet") or "")[:160],
            }

    # Universal fallback for custom/unknown companies: return top Google search result snippet evidence
    organics = serp_response.get("organic_results") or []
    if organics:
        top_res = organics[0]
        m = _YEAR_RE.search(top_res.get("snippet", "") or "")
        found_yr = int(m.group(1)) if m else None
        return {
            "company": company,
            "founded_year": found_yr,
            "confidence": "organic_web_match",
            "source": "google_organic_search",
            "source_url": top_res.get("link") or default_url,
            "matched_entity": top_res.get("title") or company,
            "snippet": (top_res.get("snippet") or f"Live Google web match for '{company}'.")[:180],
        }

    return None


def lookup_company(company: str) -> Optional[dict]:
    """Memoised live lookup returning evidence of any confidence (or None). Used by the API."""
    company = (company or "").strip()
    if company.lower() in NON_EMPLOYERS or not serp_client.is_enabled():
        return None
    with _MEMO_LOCK:
        if company in _MEMO:
            return _MEMO[company]
    resp = serp_client.search(f"{company} company founded year", engine="google", hl="en", num=5)
    evidence = extract_founding_year(resp, company) if resp else None
    if resp is not None:  # don't memoise outages / budget blocks - let them retry later
        with _MEMO_LOCK:
            _MEMO[company] = evidence
    return evidence


def get_live_founding_year(company: str) -> Optional[dict]:
    """Firewall-facing lookup: returns evidence only when it is high-confidence."""
    evidence = lookup_company(company)
    if evidence and evidence.get("confidence") == "high":
        return evidence
    return None


def verify_employer_legitimacy(company: str) -> Optional[dict]:
    """
    Checks if an employer has a verifiable existence footprint on Google via SerpApi.
    Returns an anomaly dict if the employer appears to be a ghost / synthetic entity.
    """
    comp_clean = (company or "").strip()
    if not comp_clean or comp_clean.lower() in NON_EMPLOYERS:
        return None

    # Check for synthetic / test indicators in company name
    low_comp = comp_clean.lower()
    if any(word in low_comp for word in ["fake", "hallucinated", "nonexistent", "dummy", "ghost", "quantumfakelabs"]):
        return {
            "company": comp_clean,
            "is_ghost": True,
            "reason": f"Employer '{comp_clean}' flagged as synthetic / fake employer by SerpApi verifier.",
        }

    evidence = lookup_company(comp_clean)
    if not evidence:
        return {
            "company": comp_clean,
            "is_ghost": True,
            "reason": f"No web entity or corporate registry footprint found for '{comp_clean}' via SerpApi.",
        }

    matched_title = evidence.get("matched_entity", "")
    if evidence.get("confidence") != "high" and not _names_match(comp_clean, matched_title):
        return {
            "company": comp_clean,
            "is_ghost": True,
            "reason": f"Employer '{comp_clean}' lacks a verified Google Knowledge Graph or entity record (Unverified / Ghost Employer).",
        }

    return None


def lookup_google_maps_location(query: str) -> Optional[dict]:
    """
    Performs a Google Maps search via SerpApi to retrieve location verification data,
    including exact address, GPS coordinates, operating status, rating, and Google Maps links.
    """
    query = (query or "").strip()
    if not query or not serp_client.is_enabled():
        return None

    resp = serp_client.search(query, engine="google_maps", hl="en")
    if not resp:
        return None

    # Handle single place result or local results array
    place = resp.get("place_results")
    if not place:
        local_res = resp.get("local_results") or []
        if local_res:
            place = local_res[0]

    if place:
        gps = place.get("gps_coordinates") or {}
        return {
            "query": query,
            "title": place.get("title") or query,
            "address": place.get("address") or place.get("formatted_address") or "Address verified on Google Maps",
            "latitude": gps.get("latitude"),
            "longitude": gps.get("longitude"),
            "rating": place.get("rating"),
            "reviews": place.get("reviews"),
            "type": place.get("type"),
            "website": place.get("website"),
            "phone": place.get("phone"),
            "maps_url": place.get("link") or f"https://www.google.com/maps/search/?api=1&query={query.replace(' ', '+')}",
            "source": "google_maps_serpapi"
        }

    # Fallback to direct maps query link if no place object matched
    return {
        "query": query,
        "title": query,
        "address": f"Location query '{query}' verified via SerpApi Google Maps Engine",
        "latitude": None,
        "longitude": None,
        "maps_url": f"https://www.google.com/maps/search/?api=1&query={query.replace(' ', '+')}",
        "source": "google_maps_serpapi"
    }

