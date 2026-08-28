"""FREE news-catalyst feed — our own build of the Unusual-Whales / Twitter
"breaking headline" workflow, with NO paid feed and NO X API.

The pain this solves: a market-moving headline breaks (e.g. a US-Iran peace
deal) and we want to fire longs the SECOND it hits — instead of finding out
late by scrolling Twitter.

Two free sources, combined:
  1. GDELT 2.0 DOC API  (https://api.gdeltproject.org/api/v2/doc/doc) — indexes
     global news every ~15 min, full-text searchable, free, no key. Gives us:
       - latest matching articles (headline + domain + timestamp), and
       - a coverage-VOLUME timeline, so a SURGE in coverage = a developing
         catalyst (the "breaking" detector).
  2. RSS wires (Yahoo Finance / CNBC / CoinDesk / CoinTelegraph) — lowest-latency
     major headlines, keyword-filtered.

PURE parsers (testable) + thin cached fetch. Nothing here trades; it's the signal
product. Wiring into perception/override is a separate, gated step.
"""

from __future__ import annotations

import json
import logging
import ssl
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except Exception:                     # pragma: no cover
    # P1-15: never fall back to an unverified context (MITM risk). Use the
    # system trust store with full verification; certifi is a pinned dep.
    _SSL = ssl.create_default_context()

_GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"

# Free, no-auth RSS wires. Mix of macro + crypto so a catalyst on either side
# surfaces. Add/remove freely.
_RSS_FEEDS = [
    "https://finance.yahoo.com/news/rssindex",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
]


@dataclass(frozen=True)
class Article:
    title: str
    url: str
    domain: str
    seen: Optional[datetime]   # UTC
    source: str = ""           # "gdelt" | rss feed host


@dataclass(frozen=True)
class CatalystReport:
    query: str
    n_recent: int              # articles in the window
    breaking: bool             # coverage surging vs its own baseline
    surge_x: float             # latest coverage bin / baseline median
    headlines: List[Article]   # newest first
    note: str = ""


# ── GDELT parsing (pure) ─────────────────────────────────────────────────────

def _parse_gdelt_date(s: str) -> Optional[datetime]:
    # GDELT seendate format: "20260615T143000Z"
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def parse_gdelt_artlist(payload: dict) -> List[Article]:
    out: List[Article] = []
    for a in (payload or {}).get("articles", []) or []:
        out.append(Article(
            title=(a.get("title") or "").strip(),
            url=a.get("url") or "",
            domain=a.get("domain") or "",
            seen=_parse_gdelt_date(a.get("seendate") or ""),
            source="gdelt",
        ))
    out.sort(key=lambda x: x.seen or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out


def detect_surge(volume_points: List[float], min_baseline: float = 1e-9) -> tuple:
    """Given a coverage-volume timeline (oldest->newest), is the latest bin a
    SURGE vs the baseline (median of the earlier bins)? Returns (breaking, x)."""
    if len(volume_points) < 3:
        return (False, 1.0)
    latest = volume_points[-1]
    base = median(volume_points[:-1]) or min_baseline
    x = latest / base if base > 0 else 0.0
    # "breaking" = latest coverage at least 2.5x its recent baseline AND nonzero
    return (x >= 2.5 and latest > 0, round(x, 2))


def parse_gdelt_timeline(payload: dict) -> List[float]:
    """Extract the coverage-volume series from a GDELT TimelineVol payload."""
    tl = (payload or {}).get("timeline") or []
    if not tl:
        return []
    pts = tl[0].get("data") or []     # first (only) series
    return [float(p.get("value") or 0) for p in pts]


# ── RSS parsing (pure) ───────────────────────────────────────────────────────

def _parse_rss_date(s: str) -> Optional[datetime]:
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(s.strip(), fmt)
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def parse_rss(xml_text: str, source: str = "") -> List[Article]:
    """Parse an RSS/Atom feed into Articles. Tolerant of malformed feeds."""
    out: List[Article] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    # RSS <item> and Atom <entry>
    items = root.iter("item")
    for it in items:
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date") or ""
        dom = urllib.parse.urlparse(link).netloc
        if title:
            out.append(Article(title=title, url=link, domain=dom,
                               seen=_parse_rss_date(pub), source=source or dom))
    return out


def filter_keywords(articles: List[Article], keywords: List[str]) -> List[Article]:
    """Keep articles whose title contains ANY keyword (case-insensitive)."""
    if not keywords:
        return articles
    kw = [k.lower() for k in keywords if k]
    return [a for a in articles if any(k in a.title.lower() for k in kw)]


# ── thin cached fetch ────────────────────────────────────────────────────────
_CACHE_TTL_S = 300.0           # news moves fast; 5-min cache
_cache: Dict[str, tuple] = {}
_lock = threading.Lock()

# Single-flight coalescing: when N threads request the same cache key on a
# cold miss, only ONE network fetch runs; the rest wait for its result.
_inflight: Dict[str, threading.Event] = {}
_inflight_results: Dict[str, object] = {}
_inflight_lock = threading.Lock()

# Bounded per-request timeout. GDELT's free tier frequently stalls near the
# client timeout; two parallel calls × a long timeout inflated research().
# 3s caps a stalled GDELT while still giving the API a fair shot on a good link.
_HTTP_TIMEOUT_S = float(__import__("os").environ.get("HERMES_NEWS_HTTP_TIMEOUT_S", "3"))


def _get_json(url: str, timeout: float = _HTTP_TIMEOUT_S) -> Optional[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    _t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
            _elapsed = time.monotonic() - _t0
            if _elapsed > 2.0:
                logger.info(f"[news] GET json {url[:80]}... in {_elapsed:.2f}s")
            return data
    except Exception as _e:
        _elapsed = time.monotonic() - _t0
        logger.warning(f"[news] GET json failed after {_elapsed:.2f}s: {type(_e).__name__}: {_e}")
        return None


def _get_text(url: str, timeout: float = _HTTP_TIMEOUT_S) -> Optional[str]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    _t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            data = r.read().decode("utf-8", "replace")
            _elapsed = time.monotonic() - _t0
            if _elapsed > 2.0:
                logger.info(f"[news] GET text {url[:80]}... in {_elapsed:.2f}s")
            return data
    except Exception as _e:
        _elapsed = time.monotonic() - _t0
        logger.warning(f"[news] GET text failed after {_elapsed:.2f}s: {type(_e).__name__}: {_e}")
        return None


def catalyst_scan(query: str, timespan: str = "1h", max_records: int = 30,
                  ttl: float = _CACHE_TTL_S,
                  allow_fetch: bool = True) -> Optional[CatalystReport]:
    """Free catalyst scan for a topic/ticker via GDELT: latest headlines + a
    coverage-surge ('breaking') read. Cached per (query, timespan).

    allow_fetch=False = CACHE-ONLY (return last cached value or None, no network)."""
    key = f"gdelt::{query}::{timespan}"
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and (now - hit[0]) < ttl:
            return hit[1]
    if not allow_fetch:
        return hit[1] if hit else None

    # Single-flight: if another thread is already fetching this key, wait for
    # its result instead of issuing a duplicate GDELT request.
    waiter: Optional[threading.Event] = None
    with _inflight_lock:
        existing = _inflight.get(key)
        if existing is not None:
            waiter = existing
        else:
            evt = threading.Event()
            _inflight[key] = evt

    if waiter is not None:
        _wt0 = time.monotonic()
        waiter.wait(timeout=_HTTP_TIMEOUT_S + 1.0)
        with _inflight_lock:
            result = _inflight_results.get(key)
        logger.debug(f"[news] catalyst_scan({query}) coalesced in "
                     f"{time.monotonic() - _wt0:.2f}s")
        return result if isinstance(result, CatalystReport) else None

    _fetch_t0 = time.monotonic()
    try:
        q = urllib.parse.quote(query)
        art_url = (f"{_GDELT}?query={q}&mode=ArtList&maxrecords={max_records}"
                   f"&format=json&sortby=datedesc&timespan={timespan}")
        vol_url = f"{_GDELT}?query={q}&mode=TimelineVol&format=json&timespan={timespan}"
        # The two GDELT calls are independent — parallelize them so a slow
        # ArtList doesn't serialise behind a slow TimelineVol (was 2 × 12s = 24s).
        with ThreadPoolExecutor(max_workers=2) as _pool:
            f_art = _pool.submit(_get_json, art_url)
            f_vol = _pool.submit(_get_json, vol_url)
            art = f_art.result(timeout=_HTTP_TIMEOUT_S + 1.0)
            vol = f_vol.result(timeout=_HTTP_TIMEOUT_S + 1.0)

        if art is None and vol is None:
            with _lock:
                _cache[key] = (now, None)
            with _inflight_lock:
                _inflight_results[key] = None
            return None

        headlines = parse_gdelt_artlist(art or {})
        breaking, surge_x = detect_surge(parse_gdelt_timeline(vol or {}))
        rep = CatalystReport(
            query=query, n_recent=len(headlines), breaking=breaking, surge_x=surge_x,
            headlines=headlines[:max_records],
            note=("⚡ BREAKING — coverage surging" if breaking
                  else "elevated coverage" if surge_x >= 1.5 else ""),
        )
        with _lock:
            _cache[key] = (now, rep)
        with _inflight_lock:
            _inflight_results[key] = rep
        _elapsed = time.monotonic() - _fetch_t0
        if _elapsed > 1.5:
            logger.info(f"[news] catalyst_scan({query}) in {_elapsed:.2f}s "
                        f"(art={'ok' if art else 'miss'}, vol={'ok' if vol else 'miss'})")
        return rep
    finally:
        with _inflight_lock:
            _evt = _inflight.pop(key, None)
            # Don't leave stale result dict entries for keys that may be
            # re-requested much later; a fresh fetch will repopulate.
            _inflight_results.pop(key, None)
        if _evt is not None:
            _evt.set()


def rss_headlines(keywords: Optional[List[str]] = None, feeds: Optional[List[str]] = None,
                  limit: int = 25, ttl: float = _CACHE_TTL_S) -> List[Article]:
    """Lowest-latency major-wire headlines, optionally keyword-filtered. Cached."""
    feeds = feeds or _RSS_FEEDS
    key = "rss::" + ",".join(sorted(feeds)) + "::" + ",".join(sorted(keywords or []))
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and (now - hit[0]) < ttl:
            return hit[1]
    arts: List[Article] = []
    for f in feeds:
        txt = _get_text(f)
        if txt:
            arts.extend(parse_rss(txt, source=urllib.parse.urlparse(f).netloc))
    if keywords:
        arts = filter_keywords(arts, keywords)
    arts.sort(key=lambda x: x.seen or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    arts = arts[:limit]
    with _lock:
        _cache[key] = (now, arts)
    return arts
