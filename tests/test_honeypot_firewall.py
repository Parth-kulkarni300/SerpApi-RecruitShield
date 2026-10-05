"""Unit tests for the 5-point anomaly firewall: backend.ranker.check_honeypot_reasons
and its stateful wrapper backend.agent.audit_candidate_integrity.
"""
from backend.ranker import check_honeypot_reasons


def test_clean_candidate_is_not_flagged(candidate_factory):
    cand = candidate_factory()
    is_hp, reasons = check_honeypot_reasons(cand)
    assert is_hp is False
    assert reasons == []


def test_flags_signup_after_last_active(candidate_factory):
    cand = candidate_factory(
        platform_signals={
            "signup_date": "2026-06-01",
            "last_active_date": "2023-01-01",
            "willing_to_relocate": False,
        }
    )
    is_hp, reasons = check_honeypot_reasons(cand)
    assert is_hp is True
    assert any("Signup date" in r for r in reasons)


def test_flags_skill_duration_exceeding_total_experience(candidate_factory):
    cand = candidate_factory(
        years_of_experience=2.0,
        skills=[{"name": "Python", "proficiency": "advanced", "duration_months": 120}],
    )
    is_hp, reasons = check_honeypot_reasons(cand)
    assert is_hp is True
    assert any("duration" in r and "exceeds total experience" in r for r in reasons)


def test_flags_expert_skill_with_zero_duration(candidate_factory):
    cand = candidate_factory(
        skills=[{"name": "Rust", "proficiency": "expert", "duration_months": 0}],
    )
    is_hp, reasons = check_honeypot_reasons(cand)
    assert is_hp is True
    assert any("0 months of usage" in r for r in reasons)


def test_flags_job_start_date_before_company_founding(candidate_factory):
    # Google was founded in 1998 (see ranker.FOUNDING_YEARS); starting there in 1990 is impossible.
    cand = candidate_factory(
        current_company="Google",
        career_history=[
            {
                "company": "Google",
                "title": "Software Engineer",
                "start_date": "1990-01-01",
                "end_date": "1995-01-01",
                "duration_months": 60,
            }
        ],
    )
    is_hp, reasons = check_honeypot_reasons(cand)
    assert is_hp is True
    assert any("founded in 1998" in r for r in reasons)


def test_flags_job_duration_exceeding_company_age(candidate_factory):
    # Krutrim was founded in 2023 (see ranker.FOUNDING_YEARS); a 30-year tenure there is impossible.
    cand = candidate_factory(
        current_company="Krutrim",
        career_history=[
            {
                "company": "Krutrim",
                "title": "Software Engineer",
                "start_date": "2023-06-01",
                "end_date": "2026-06-01",
                "duration_months": 360,
            }
        ],
    )
    is_hp, reasons = check_honeypot_reasons(cand)
    assert is_hp is True
    assert any("company was founded" in r and "years ago" in r for r in reasons)


def test_audit_candidate_integrity_purges_known_honeypot_from_sample_dataset(loaded_sample_candidates):
    agent_mod = loaded_sample_candidates
    total_loaded = agent_mod.TOTAL_INITIAL_CANDIDATES

    # load_candidates_file() already runs one audit pass; assert its result matches
    # what re-running it produces, and that exactly the known planted honeypot is purged.
    assert total_loaded == len(agent_mod.RAW_INITIAL_CANDIDATES)
    assert agent_mod.HONEYPOT_COUNT == 1
    assert len(agent_mod.HONEYPOT_CANDIDATES) == 1
    assert len(agent_mod.CANDIDATES) == total_loaded - 1

    purged_ids = {c["candidate_id"] for c in agent_mod.HONEYPOT_CANDIDATES}
    remaining_ids = {c["candidate_id"] for c in agent_mod.CANDIDATES}
    assert purged_ids.isdisjoint(remaining_ids)
