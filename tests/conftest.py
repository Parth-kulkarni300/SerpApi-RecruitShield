from pathlib import Path

import pytest

SAMPLE_CANDIDATES_PATH = Path(__file__).resolve().parents[1] / "backend" / "sample_candidates.jsonl"


def make_candidate(
    candidate_id="C-TEST-1",
    years_of_experience=5.0,
    skills=None,
    career_history=None,
    education=None,
    platform_signals=None,
    current_title="Software Engineer",
    current_company="Acme Corp",
    location="Bangalore",
    country="India",
):
    """Builds a minimal, self-consistent candidate dict in the shape ranker.py expects,
    so individual honeypot/scoring rules can be exercised without depending on the
    bundled sample dataset's specific contents.
    """
    return {
        "candidate_id": candidate_id,
        "profile": {
            "anonymized_name": "Test Candidate",
            "headline": f"{current_title} with {years_of_experience} yrs experience",
            "years_of_experience": years_of_experience,
            "location": location,
            "current_title": current_title,
            "current_company": current_company,
            "country": country,
        },
        "skills": skills if skills is not None else [
            {"name": "Python", "proficiency": "advanced", "duration_months": 36},
        ],
        "career_history": career_history if career_history is not None else [
            {
                "company": current_company,
                "title": current_title,
                "start_date": "2021-01-01",
                "end_date": "2026-01-01",
                "duration_months": 60,
                "description": "Worked on backend systems.",
            }
        ],
        "education": education if education is not None else [
            {"institution": "Test University", "degree": "B.E.", "tier": "tier_1"}
        ],
        "redrob_signals": platform_signals if platform_signals is not None else {
            "signup_date": "2023-01-01",
            "last_active_date": "2026-01-01",
            "open_to_work_flag": True,
            "recruiter_response_rate": 0.5,
            "notice_period_days": 30,
            "profile_views_received_30d": 10,
            "search_appearance_30d": 20,
            "saved_by_recruiters_30d": 1,
            "github_activity_score": 20.0,
            "skill_assessment_scores": {},
            "profile_completeness_score": 80,
            "willing_to_relocate": False,
        },
    }


@pytest.fixture
def candidate_factory():
    return make_candidate


@pytest.fixture
def loaded_sample_candidates():
    """Loads the real bundled demo dataset into agent.py's module-level state, the way
    the backend does at startup, and resets that state afterward so tests don't leak
    into each other.
    """
    import backend.agent as agent_mod
    from backend.agent import load_candidates_file

    load_candidates_file(str(SAMPLE_CANDIDATES_PATH))
    yield agent_mod

    agent_mod.CANDIDATES.clear()
    agent_mod.RAW_INITIAL_CANDIDATES.clear()
    agent_mod.ACTIVE_SHORTLIST.clear()
    agent_mod.HONEYPOT_CANDIDATES.clear()
