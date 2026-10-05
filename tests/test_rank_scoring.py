"""Unit tests for the ranking math: backend.ranker.score_candidate and rank_candidates.

These deliberately assert relational invariants (honeypots excluded, scores bounded,
descending sort order, a known penalty makes one candidate score lower than an
otherwise-identical one) rather than exact score values, so they don't become brittle
against future weight/formula tuning in ranker.py.
"""
from backend.ranker import score_candidate, rank_candidates


def test_score_candidate_returns_none_for_a_honeypot(candidate_factory):
    honeypot = candidate_factory(
        platform_signals={
            "signup_date": "2026-06-01",
            "last_active_date": "2023-01-01",
            "willing_to_relocate": False,
        }
    )
    assert score_candidate(honeypot, jd_text="") is None


def test_score_candidate_returns_bounded_score_for_a_clean_candidate(candidate_factory):
    cand = candidate_factory()
    result = score_candidate(cand, jd_text="")
    assert result is not None
    assert 0.0 <= result["score"] <= 1.0
    assert result["candidate_id"] == cand["candidate_id"]


def test_score_candidate_applies_a_strict_consulting_penalty(candidate_factory):
    # Identical in every way except career history: one candidate worked exclusively at
    # consulting firms (TCS), the other at a non-consulting company. Everything else
    # (skills, experience, title) is held constant so the penalty is isolated.
    clean = candidate_factory(candidate_id="C-CLEAN")
    consulting = candidate_factory(
        candidate_id="C-CONSULTING",
        career_history=[
            {
                "company": "TCS",
                "title": "Software Engineer",
                "start_date": "2021-01-01",
                "end_date": "2026-01-01",
                "duration_months": 60,
            }
        ],
    )
    clean_score = score_candidate(clean, jd_text="")["score"]
    consulting_score = score_candidate(consulting, jd_text="")["score"]
    assert consulting_score < clean_score


def test_rank_candidates_excludes_honeypots(candidate_factory):
    clean = candidate_factory(candidate_id="C-CLEAN")
    honeypot = candidate_factory(
        candidate_id="C-HONEYPOT",
        platform_signals={
            "signup_date": "2026-06-01",
            "last_active_date": "2023-01-01",
            "willing_to_relocate": False,
        },
    )
    ranked = rank_candidates([clean, honeypot], jd_text="")
    ranked_ids = {c["candidate_id"] for c in ranked}
    assert ranked_ids == {"C-CLEAN"}


def test_rank_candidates_sorts_by_score_descending(candidate_factory):
    strong = candidate_factory(
        candidate_id="C-STRONG",
        platform_signals={
            "signup_date": "2023-01-01",
            "last_active_date": "2026-01-01",
            "willing_to_relocate": False,
            "github_activity_score": 95.0,
            "profile_completeness_score": 98,
            "skill_assessment_scores": {"Python": 95.0},
        },
    )
    weak = candidate_factory(
        candidate_id="C-WEAK",
        platform_signals={
            "signup_date": "2023-01-01",
            "last_active_date": "2026-01-01",
            "willing_to_relocate": False,
            "github_activity_score": 0.0,
            "profile_completeness_score": 20,
            "skill_assessment_scores": {},
        },
    )
    ranked = rank_candidates([weak, strong], jd_text="")
    assert [c["candidate_id"] for c in ranked] == ["C-STRONG", "C-WEAK"]
    assert ranked[0]["score"] >= ranked[1]["score"]
