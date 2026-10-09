"""Thin SerpApi client used by RecruitShield's live verification layer.

Design goals (this project runs on a free SerpApi quota, inside a ranking loop that can
touch the same employer hundreds of times):

* **Opt-in** - nothing happens unless ``SERPAPI_API_KEY`` is set. Without a key every
  public function degrades to "no live data" and the firewall falls back to the
  hardcoded reference table, so the app (and the test-suite) works offline.
* **Persistent cache** - every query (including *negative* results) is cached on disk
  for 30 days, so repeated audits / server restarts never re-spend quota.
* **Hard call budget** - ``SERPAPI_MAX_LIVE_CALLS`` (default 50) caps live requests per
  process so a large upload can't silently burn the whole monthly allowance.
* **Never raises into callers** - network/API failures are logged and reported as
  ``None``; a verification outage must never crash the integrity audit.

The API key is read from the process environment only (``backend.main`` calls
``load_dotenv()`` at startup); it is never logged or returned from any endpoint.
"""
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

try:
    from serpapi import GoogleSearch
except ImportError:
    try:
        from serpapi.google_search import GoogleSearch
    except ImportError:
        GoogleSearch = None

logger = logging.getLogger("recruiter-serpapi")

SERPAPI_SEARCH_URL = "https://serpapi.com/search.json"
SERPAPI_ACCOUNT_URL = "https://serpapi.com/account.json"

CACHE_DIR = Path(__file__).parent / ".serp_cache"
CACHE_FILE = CACHE_DIR / "serp_cache.json"
CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
REQUEST_TIMEOUT_SECONDS = 10

_LOCK = threading.Lock()
_CACHE: Optional[dict] = None  # lazily loaded from CACHE_FILE
_STATS = {"live_calls": 0, "cache_hits": 0, "errors": 0, "budget_blocked": 0}


def get_api_key() -> Optional[str]:
    key = (os.environ.get("SERPAPI_API_KEY") or os.environ.get("SERPAPI_KEY") or "").strip()
    return key or None


def is_enabled() -> bool:
    """True when a key is configured and live verification hasn't been switched off."""
    if os.environ.get("SERPAPI_DISABLED", "").strip().lower() in {"1", "true", "yes"}:
        return False
    return get_api_key() is not None


def max_live_calls() -> int:
    try:
        return max(0, int(os.environ.get("SERPAPI_MAX_LIVE_CALLS", "50")))
    except ValueError:
        return 50


def get_stats() -> dict:
    with _LOCK:
        return dict(_STATS)


def reset_state(cache_file: Optional[Path] = None) -> None:
    """Clears in-memory cache + counters (used by tests; optionally redirects the cache file)."""
    global _CACHE, CACHE_FILE
    with _LOCK:
        _CACHE = None
        for k in _STATS:
            _STATS[k] = 0
        if cache_file is not None:
            CACHE_FILE = Path(cache_file)


def _load_cache() -> dict:
    global _CACHE
    if _CACHE is None:
        try:
            _CACHE = json.loads(CACHE_FILE.read_text(encoding="utf-8")) if CACHE_FILE.exists() else {}
        except Exception as exc:  # corrupt cache must never break the app
            logger.warning("SerpApi cache unreadable (%s); starting fresh.", exc)
            _CACHE = {}
    return _CACHE


def _persist_cache() -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_CACHE or {}), encoding="utf-8")
        tmp.replace(CACHE_FILE)
    except Exception as exc:
        logger.warning("Could not persist SerpApi cache: %s", exc)


def _cache_key(engine: str, query: str, params: dict) -> str:
    extras = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return f"{engine}|{query.strip().lower()}|{extras}"


def _log_serp_tool_call(engine: str, query: str, source: str):
    try:
        from backend.agent import log_agent_event
        log_agent_event(
            "SERPAPI_TOOL_CALL",
            f"SerpApi ({engine})",
            f"Executed SerpApi '{engine}' tool call: '{query}' [{source}]",
            details=f"Engine: {engine}, Query: '{query}', Source: {source}"
        )
    except Exception:
        pass

def search(query: str, engine: str = "google", **params) -> Optional[dict]:
    """Runs one SerpApi search and returns the parsed JSON, or ``None`` if unavailable.

    Order of operations: cache -> budget check -> live request. Failed requests are *not*
    cached (so a transient outage can recover) but successful empty answers are.
    """
    if not is_enabled() or not query or not query.strip():
        return None

    key = _cache_key(engine, query, params)
    with _LOCK:
        cache = _load_cache()
        hit = cache.get(key)
        if hit and (time.time() - hit.get("ts", 0)) < CACHE_TTL_SECONDS:
            _STATS["cache_hits"] += 1
            _log_serp_tool_call(engine, query, "Cache Hit (0 Quota Spent)")
            return {**hit["data"], "_from_cache": True}

        if _STATS["live_calls"] >= max_live_calls():
            _STATS["budget_blocked"] += 1
            logger.warning("SerpApi live-call budget (%d) exhausted; skipping '%s'.", max_live_calls(), query)
            return None
        _STATS["live_calls"] += 1

    try:
        resp = requests.get(
            SERPAPI_SEARCH_URL,
            params={"engine": engine, "q": query, "api_key": get_api_key(), **params},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        data = resp.json()
        if resp.status_code != 200 or data.get("error"):
            # Empty-result "errors" are a normal answer; anything else is a real failure.
            if "hasn't returned any results" in str(data.get("error", "")):
                data = {"organic_results": [], "search_metadata": data.get("search_metadata", {})}
            else:
                raise RuntimeError(str(data.get("error") or f"HTTP {resp.status_code}")[:200])
        
        _log_serp_tool_call(engine, query, "Live API Search")
    except Exception as exc:
        with _LOCK:
            _STATS["errors"] += 1
        logger.warning("SerpApi request failed for '%s': %s", query, exc)
        return None

    with _LOCK:
        _load_cache()[key] = {"ts": time.time(), "data": data}
        _persist_cache()
    return {**data, "_from_cache": False}


def account_info() -> Optional[dict]:
    """Remaining-quota info from SerpApi's (free, non-counted) account endpoint."""
    if not is_enabled():
        return None
    try:
        resp = requests.get(SERPAPI_ACCOUNT_URL, params={"api_key": get_api_key()}, timeout=REQUEST_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            return None
        data = resp.json()
        return {
            "plan": data.get("plan_name"),
            "searches_left": data.get("total_searches_left"),
            "monthly_limit": data.get("searches_per_month"),
        }
    except Exception as exc:
        logger.warning("SerpApi account lookup failed: %s", exc)
        return None
