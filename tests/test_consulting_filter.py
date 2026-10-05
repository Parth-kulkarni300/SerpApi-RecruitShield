"""Unit tests for the consulting-background assessment: backend.ranker.is_consulting_only
and its stateful wrapper backend.agent.apply_consulting_filter.

apply_consulting_filter applies a soft score penalty rather than excluding candidates
(see agent.py's docstring), so the behavior under test is "flags correctly, removes no one".
"""
from backend.ranker import is_consulting_only
from backend.agent import apply_consulting_filter


def test_true_when_every_employer_is_a_consulting_firm(candidate_factory):
    cand = candidate_factory(
        career_history=[
            {"company": "TCS", "title": "Analyst", "start_date": "2018-01-01", "end_date": "2021-01-01", "duration_months": 36},
            {"company": "Infosys", "title": "Consultant", "start_date": "2021-01-01", "end_date": "2026-01-01", "duration_months": 60},
        ]
    )
    assert is_consulting_only(cand) is True


def test_false_when_any_employer_is_not_a_consulting_firm(candidate_factory):
    cand = candidate_factory(
        career_history=[
            {"company": "TCS", "title": "Analyst", "start_date": "2018-01-01", "end_date": "2021-01-01", "duration_months": 36},
            {"company": "Razorpay", "title": "Backend Engineer", "start_date": "2021-01-01", "end_date": "2026-01-01", "duration_months": 60},
        ]
    )
    assert is_consulting_only(cand) is False


def test_false_when_no_career_history(candidate_factory):
    cand = candidate_factory(career_history=[])
    assert is_consulting_only(cand) is False


def test_apply_consulting_filter_removes_no_candidates(loaded_sample_candidates):
    agent_mod = loaded_sample_candidates
    before_ids = {c["candidate_id"] for c in agent_mod.CANDIDATES}
    before_count = len(agent_mod.CANDIDATES)

    apply_consulting_filter()

    after_ids = {c["candidate_id"] for c in agent_mod.CANDIDATES}
    assert len(agent_mod.CANDIDATES) == before_count
    assert after_ids == before_ids


def test_apply_consulting_filter_reports_the_correct_count(loaded_sample_candidates):
    agent_mod = loaded_sample_candidates
    expected_consulting_count = sum(1 for c in agent_mod.CANDIDATES if is_consulting_only(c))

    summary = apply_consulting_filter()

    assert f"Identified {expected_consulting_count} candidates" in summary
