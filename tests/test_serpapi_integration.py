"""Tests for the SerpApi live-verification layer (backend.serp_client / backend.serp_verifier)
and its integration into the 5-Point Anomaly Firewall.

Everything here is mocked: no test ever performs a real HTTP request or spends SerpApi quota.
"""
import pytest

from backend import serp_client, serp_verifier
from backend.ranker import check_honeypot_reasons


def kg_response(title="NewCo Technologies", founded="March 12, 2020, Pune, India", url="https://newco.example"):
    return {
        "search_metadata": {"google_url": "https://www.google.com/search?q=newco"},
        "knowledge_graph": {"title": title, "founded": founded, "website": url},
        "organic_results": [],
    }


@pytest.fixture(autouse=True)
def isolated_serp_state(tmp_path, monkeypatch):
    """Fresh cache file, counters and memo for every test; no real key leaks in from the environment."""
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.delenv("SERPAPI_KEY", raising=False)
    monkeypatch.delenv("SERPAPI_DISABLED", raising=False)
    monkeypatch.delenv("SERPAPI_MAX_LIVE_CALLS", raising=False)
    original_cache_file = serp_client.CACHE_FILE
    serp_client.reset_state(cache_file=tmp_path / "serp_cache.json")
    serp_verifier.clear_memo()
    yield
    serp_client.reset_state(cache_file=original_cache_file)
    serp_verifier.clear_memo()


def enable_serpapi(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "test-key-not-real")


# ---------------------------------------------------------------- evidence extraction

def test_extracts_high_confidence_year_from_knowledge_graph():
    ev = serp_verifier.extract_founding_year(kg_response(), "NewCo")
    assert ev["founded_year"] == 2020
    assert ev["confidence"] == "high"
    assert ev["source"] == "google_knowledge_graph"
    assert ev["source_url"] == "https://newco.example"


def test_rejects_knowledge_graph_for_a_different_entity():
    # Ambiguous names must not be "verified" against an unrelated company.
    resp = kg_response(title="Completely Different Corp")
    assert serp_verifier.extract_founding_year(resp, "NewCo") is None


def test_ignores_founder_fields_when_reading_founding_year():
    resp = {"knowledge_graph": {"title": "NewCo", "founders": "Someone, 1975"}}
    assert serp_verifier.extract_founding_year(resp, "NewCo") is None


def test_snippet_match_is_low_confidence_only():
    resp = {"organic_results": [{"title": "About NewCo", "link": "https://x.example",
                                 "snippet": "NewCo was founded in 2021 by two friends."}]}
    ev = serp_verifier.extract_founding_year(resp, "NewCo")
    assert ev["confidence"] == "low" and ev["founded_year"] == 2021


# ---------------------------------------------------------------- firewall integration

def make_job_candidate(candidate_factory, company, start="2015-01-01", months=24):
    return candidate_factory(
        current_company=company,
        career_history=[{"company": company, "title": "Engineer", "start_date": start,
                         "end_date": "2017-01-01", "duration_months": months}],
    )


def test_firewall_flags_impossible_tenure_at_unknown_company_using_live_evidence(candidate_factory, monkeypatch):
    enable_serpapi(monkeypatch)
    monkeypatch.setattr(serp_client, "search", lambda q, **kw: kg_response())

    cand = make_job_candidate(candidate_factory, "NewCo Technologies", start="2015-01-01")
    is_hp, reasons = check_honeypot_reasons(cand)

    assert is_hp is True
    assert any("founded in 2020" in r and "live-verified via SerpApi" in r and "https://newco.example" in r
               for r in reasons)


def test_firewall_does_not_flag_plausible_tenure_at_unknown_company(candidate_factory, monkeypatch):
    enable_serpapi(monkeypatch)
    monkeypatch.setattr(serp_client, "search", lambda q, **kw: kg_response())

    cand = make_job_candidate(candidate_factory, "NewCo Technologies", start="2021-01-01", months=24)
    is_hp, reasons = check_honeypot_reasons(cand)
    assert is_hp is False and reasons == []


def test_firewall_ignores_low_confidence_snippet_evidence(candidate_factory, monkeypatch):
    enable_serpapi(monkeypatch)
    snippet_only = {"organic_results": [{"title": "NewCo", "link": "https://x.example",
                                         "snippet": "NewCo was founded in 2021."}]}
    monkeypatch.setattr(serp_client, "search", lambda q, **kw: snippet_only)

    cand = make_job_candidate(candidate_factory, "NewCo Technologies", start="2015-01-01")
    is_hp, _ = check_honeypot_reasons(cand)
    assert is_hp is False  # a loose text match must never purge a candidate


def test_hardcoded_reference_table_takes_precedence_and_skips_live_lookup(candidate_factory, monkeypatch):
    enable_serpapi(monkeypatch)

    def must_not_be_called(*a, **kw):
        raise AssertionError("live lookup should not run for companies in the reference table")

    monkeypatch.setattr(serp_client, "search", must_not_be_called)
    cand = make_job_candidate(candidate_factory, "Google", start="1990-01-01")
    is_hp, reasons = check_honeypot_reasons(cand)
    assert is_hp is True
    assert any("founded in 1998" in r and "SerpApi" not in r for r in reasons)


def test_firewall_makes_no_network_calls_without_an_api_key(candidate_factory, monkeypatch):
    def must_not_be_called(*a, **kw):
        raise AssertionError("no SerpApi request may be made when SERPAPI_API_KEY is unset")

    monkeypatch.setattr(serp_client.requests, "get", must_not_be_called)
    cand = make_job_candidate(candidate_factory, "NewCo Technologies", start="2015-01-01")
    is_hp, reasons = check_honeypot_reasons(cand)
    assert is_hp is False and reasons == []


def test_non_employer_strings_are_never_looked_up(candidate_factory, monkeypatch):
    enable_serpapi(monkeypatch)

    def must_not_be_called(*a, **kw):
        raise AssertionError("placeholders like 'Not specified' are not searchable employers")

    monkeypatch.setattr(serp_client, "search", must_not_be_called)
    for placeholder in ["Not specified", "Freelance", "Self-employed", ""]:
        assert serp_verifier.get_live_founding_year(placeholder) is None


# ---------------------------------------------------------------- client: cache, budget, errors

class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload


def test_client_caches_results_so_repeat_queries_cost_no_quota(monkeypatch):
    enable_serpapi(monkeypatch)
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append(params["q"])
        return FakeResponse(kg_response())

    monkeypatch.setattr(serp_client.requests, "get", fake_get)

    first = serp_client.search("NewCo company founded year")
    second = serp_client.search("newco company founded year")  # same query, different case
    assert len(calls) == 1
    assert first["_from_cache"] is False and second["_from_cache"] is True
    assert serp_client.get_stats()["cache_hits"] == 1


def test_client_enforces_hard_live_call_budget(monkeypatch):
    enable_serpapi(monkeypatch)
    monkeypatch.setenv("SERPAPI_MAX_LIVE_CALLS", "2")
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append(params["q"])
        return FakeResponse(kg_response())

    monkeypatch.setattr(serp_client.requests, "get", fake_get)

    results = [serp_client.search(f"company {i}") for i in range(4)]
    assert len(calls) == 2
    assert results[0] is not None and results[1] is not None
    assert results[2] is None and results[3] is None
    assert serp_client.get_stats()["budget_blocked"] == 2


def test_client_returns_none_on_api_errors_and_does_not_cache_failures(monkeypatch):
    enable_serpapi(monkeypatch)
    state = {"fail": True}

    def flaky_get(url, params=None, timeout=None):
        if state["fail"]:
            return FakeResponse({"error": "Invalid API key."}, status=401)
        return FakeResponse(kg_response())

    monkeypatch.setattr(serp_client.requests, "get", flaky_get)

    assert serp_client.search("NewCo company founded year") is None
    assert serp_client.get_stats()["errors"] == 1
    state["fail"] = False
    assert serp_client.search("NewCo company founded year") is not None  # recovered, wasn't cached as a failure


def test_client_is_disabled_by_kill_switch_even_with_a_key(monkeypatch):
    enable_serpapi(monkeypatch)
    monkeypatch.setenv("SERPAPI_DISABLED", "1")
    assert serp_client.is_enabled() is False
    assert serp_client.search("anything") is None
