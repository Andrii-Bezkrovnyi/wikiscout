#!/usr/bin/env python3
"""Wikipedia Market Trends - agent skill tool.

What changed vs the first version, and why:

  * the tool no longer calls an LLM to draw conclusions. It prints a compact
    JSON verdict to stdout so the calling agent can answer, refine and re-run.
    (--ai-summary keeps the old Gemini paragraph for standalone use.)
  * article titles are resolved through Wikidata sitelinks, so "intermittent
    fasting" maps to the right page in every language instead of being guessed;
  * views are normalised by the whole edition's traffic, because editions
    differ in size and overall Wikipedia traffic drifts year over year;
  * a log-linear trend with a p-value replaces "total / average / max", so a
    big percentage can be labelled as noise when it is noise;
  * quality flags (low volume, spike, level shift, gaps, short history) lower
    an explicit confidence level, and land in the PDF's limitations block;
  * every HTTP answer is cached in sqlite, so follow-up questions are free;
  * the PDF uses a Unicode TTF font; transliteration is only a fallback.

Usage
-----
    # by topic (recommended: titles resolved via Wikidata)
    python wiki_tool.py --topic "intermittent fasting" --langs pl,cs --start 24m

    # by explicit project:article (the original interface, still supported)
    python wiki_tool.py --queries pl.wikipedia:Post_przerywany \
        cs.wikipedia:"Přerušovaný půst" --start 20240101 --end 20241231 \
        --output fasting.pdf

Progress messages go to stderr; stdout is always a single JSON document.
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import math
import os
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import httpx

from dotenv import load_dotenv

# fpdf2 subsets TTF fonts through fontTools, which logs a warning for every
# table it cannot subset ("MERG NOT subset"). Harmless, but it pollutes stderr.
import logging
for _noisy in ("fontTools", "fontTools.subset", "fontTools.ttLib", "fpdf"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)

load_dotenv()

# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------
__version__ = "2.4.0"

USER_AGENT = os.getenv("WT_USER_AGENT", f"wikiscout/{__version__} (admin@example.com)")
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}

REST = "https://wikimedia.org/api/rest_v1/metrics/pageviews"
WD_API = "https://www.wikidata.org/w/api.php"

CACHE_PATH = Path(os.getenv("WT_CACHE_PATH", Path.home() / ".cache" / "wikiscout.db"))
OFFLINE = os.getenv("WT_OFFLINE", "0") == "1"       # tests / reproducible demos
HTTP_TIMEOUT = float(os.getenv("WT_HTTP_TIMEOUT", "20"))
MAX_RETRIES = int(os.getenv("WT_MAX_RETRIES", "4"))

# analysis thresholds - see SKILL.md "Methodology"
MIN_DAILY_VIEWS = int(os.getenv("WT_MIN_DAILY_VIEWS", "50"))
MIN_POINTS = int(os.getenv("WT_MIN_POINTS", "18"))
SPIKE_MAD_Z = float(os.getenv("WT_SPIKE_MAD_Z", "3.5"))
SPIKE_SHARE = float(os.getenv("WT_SPIKE_SHARE", "0.25"))
LEVEL_SHIFT_RATIO = float(os.getenv("WT_LEVEL_SHIFT_RATIO", "4.0"))
# how big a rival Wikidata item must be (relative to the chosen one) to count
# as a genuine ambiguity rather than a same-named journal or paper
RIVAL_RATIO = float(os.getenv("WT_RIVAL_RATIO", "0.3"))

# Wikidata items of these types are publications named after a topic, not rival
# readings of it. "Astronomy and Astrophysics" is a journal; it exists in many
# language editions, so a size ratio alone does not filter it out.
PUBLICATION_TYPES = {
    "Q5633421",    # scientific journal
    "Q737498",     # academic journal
    "Q13442814",   # scholarly article
    "Q191067",     # article
    "Q11032",      # newspaper
    "Q41298",      # magazine
    "Q1002697",    # periodical
    "Q30612",      # clinical trial
    "Q571",        # book
    "Q47461344",   # written work
    "Q7725634",    # literary work
    "Q3331189",    # version, edition or translation
    "Q11424",      # film
    "Q482994",     # album
    "Q7366",       # song
    "Q5398426",    # television series
}

DATA_START = dt.date(2015, 7, 1)     # Pageviews API has nothing earlier
CONFIDENCE_ORDER = ["none", "low", "medium", "high"]

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    r"C:\Windows\Fonts\DejaVuSans.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ms",
]
BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    r"C:\Windows\Fonts\DejaVuSans-Bold.ttf",
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
]


def log(message: str) -> None:
    """Progress goes to stderr so that stdout stays valid JSON."""
    print(message, file=sys.stderr)


class NotFound(Exception):
    """The API has no data for this resource (HTTP 404)."""


class OfflineMiss(Exception):
    """WT_OFFLINE=1 and the URL is not in the cache."""


# ==========================================================================
# 1. HTTP with sqlite cache
# ==========================================================================
_conn: Optional[sqlite3.Connection] = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(CACHE_PATH))
        _conn.execute("""CREATE TABLE IF NOT EXISTS http_cache (
            url TEXT PRIMARY KEY, body TEXT NOT NULL,
            status INTEGER NOT NULL DEFAULT 200, ts REAL NOT NULL)""")
        _conn.commit()
    return _conn


def cache_put(url: str, status: int, body: Any) -> None:
    _db().execute("INSERT OR REPLACE INTO http_cache (url, body, status, ts) VALUES (?,?,?,?)",
                  (url, json.dumps(body, ensure_ascii=False), status, time.time()))
    _db().commit()


def cache_get(url: str, ttl: Optional[float]) -> Optional[Tuple[int, Any]]:
    row = _db().execute("SELECT body, status, ts FROM http_cache WHERE url=?", (url,)).fetchone()
    if row is None:
        return None
    body, status, ts = row
    if ttl is not None and (time.time() - ts) > ttl:
        return None
    return status, json.loads(body)


def fetch_json(url: str, ttl: Optional[float] = None) -> Dict[str, Any]:
    """GET with cache, retry and honest 404 handling.

    ttl=None means "cache forever" - finished months never change, which is
    what makes repeated and follow-up queries cheap.
    """
    cached = cache_get(url, ttl)
    if cached is not None:
        status, body = cached
        if status == 404:
            raise NotFound(url)
        return body

    if OFFLINE:
        raise OfflineMiss(f"offline mode: {url} is not cached")

    delay, last = 1.0, None
    for _ in range(MAX_RETRIES):
        try:
            resp = httpx.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT,
                             follow_redirects=True)
        except httpx.HTTPError as exc:
            last = exc
            time.sleep(delay)
            delay *= 2
            continue
        if resp.status_code == 404:
            cache_put(url, 404, {"error": "not_found"})
            raise NotFound(url)
        if resp.status_code == 200:
            body = resp.json()
            cache_put(url, 200, body)
            return body
        if resp.status_code in (429, 500, 502, 503, 504):
            last = RuntimeError(f"HTTP {resp.status_code}")
            time.sleep(delay)
            delay *= 2
            continue
        raise RuntimeError(f"HTTP {resp.status_code} for {url}: {resp.text[:200]}")
    raise RuntimeError(f"request failed after {MAX_RETRIES} attempts: {last}")


# ==========================================================================
# 2. dates
# ==========================================================================
def last_complete_month() -> dt.date:
    first = dt.date.today().replace(day=1)
    return (first - dt.timedelta(days=1)).replace(day=1)


def parse_date(token: str, is_end: bool = False) -> dt.date:
    """Accepts YYYYMMDD (original format), YYYY-MM-DD, YYYY-MM, YYYY,
    relative '24m' / '3y', and 'latest'."""
    token = str(token).strip().lower()
    if token in ("latest", "now", "today"):
        return dt.date.today() - dt.timedelta(days=2)      # API lags 1-2 days
    if re.fullmatch(r"\d{8}", token):                      # 20240101
        return dt.date(int(token[:4]), int(token[4:6]), int(token[6:8]))
    m = re.fullmatch(r"(\d+)\s*([my])", token)             # 24m / 3y
    if m:
        months = int(m.group(1)) * (12 if m.group(2) == "y" else 1)
        anchor = last_complete_month()
        year = anchor.year + (anchor.month - months - 1) // 12
        month = (anchor.month - months - 1) % 12 + 1
        return dt.date(year, month, 1)
    if re.fullmatch(r"\d{4}", token):
        return dt.date(int(token), 12, 31) if is_end else dt.date(int(token), 1, 1)
    if re.fullmatch(r"\d{4}-\d{2}", token):
        y, mo = int(token[:4]), int(token[5:7])
        if is_end:
            nxt = dt.date(y + (mo == 12), (mo % 12) + 1, 1)
            return nxt - dt.timedelta(days=1)
        return dt.date(y, mo, 1)
    return dt.date.fromisoformat(token)


def resolve_period(start: str, end: str, granularity: str) -> Tuple[dt.date, dt.date]:
    s = max(parse_date(start), DATA_START)
    e = parse_date(end, is_end=True)
    if granularity == "monthly":
        s = s.replace(day=1)
        lcm = last_complete_month()
        if e > lcm:                      # a partial month always looks like a crash
            nxt = dt.date(lcm.year + (lcm.month == 12), (lcm.month % 12) + 1, 1)
            e = nxt - dt.timedelta(days=1)
    if e < s:
        raise ValueError(f"empty period: {s} .. {e}")
    return s, e


def _stamp(d: dt.date) -> str:
    return d.strftime("%Y%m%d")


def _iso(timestamp: str) -> str:
    return f"{timestamp[0:4]}-{timestamp[4:6]}-{timestamp[6:8]}"


def _ttl_for(end: dt.date) -> Optional[float]:
    return None if end < last_complete_month() else 12 * 3600.0


# ==========================================================================
# 3. Wikidata: topic -> article title per language
# ==========================================================================
KNOWN_WIKIS = {
    "en", "de", "fr", "es", "it", "pt", "nl", "pl", "ru", "uk", "cs", "sk", "sv",
    "no", "da", "fi", "hu", "ro", "bg", "sr", "hr", "el", "tr", "ar", "he", "fa",
    "hi", "id", "ms", "vi", "th", "ja", "ko", "zh", "ca", "eu", "lt", "lv", "et",
    "sl", "ka", "az", "kk", "be",
}
MONTH = 30 * 86400.0


def _wd_url(params: Dict[str, str]) -> str:
    return WD_API + "?" + urllib.parse.urlencode(params)


def wd_search(topic: str, language: str = "en", limit: int = 5) -> List[Dict[str, Any]]:
    data = fetch_json(_wd_url({
        "action": "wbsearchentities", "search": topic, "language": language,
        "uselang": language, "type": "item", "limit": str(limit), "format": "json"}),
        ttl=MONTH)
    return [{"qid": i.get("id"), "label": i.get("label"),
             "description": i.get("description", "")} for i in data.get("search", [])]


def wd_entities(qids: Sequence[str], props: str = "sitelinks",
                languages: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    params = {"action": "wbgetentities", "ids": "|".join(qids),
              "props": props, "format": "json"}
    if languages:
        params["languages"] = "|".join(languages)
    return fetch_json(_wd_url(params), ttl=MONTH).get("entities", {})


def instance_of(entity: Dict[str, Any]) -> set:
    """The P31 ('instance of') values of a Wikidata item."""
    out = set()
    for claim in (entity.get("claims") or {}).get("P31", []):
        try:
            out.add(claim["mainsnak"]["datavalue"]["value"]["id"])
        except (KeyError, TypeError):
            continue
    return out


def sitelinks_of(entity: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for site, info in (entity.get("sitelinks") or {}).items():
        if site.endswith("wiki") and site[:-4] in KNOWN_WIKIS:
            out[site[:-4]] = info.get("title")
    return out


def wiki_search(lang: str, query: str, limit: int = 3) -> List[str]:
    url = f"https://{lang}.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "list": "search", "srsearch": query,
        "srlimit": str(limit), "format": "json"})
    try:
        data = fetch_json(url, ttl=7 * 86400.0)
    except NotFound:
        return []
    return [h["title"] for h in data.get("query", {}).get("search", [])]


def wiki_page_qid(lang: str, title: str) -> Optional[str]:
    """Which Wikidata item does this article belong to?

    This is the safety net. A full-text search in a foreign wiki will always
    return *something*; without this check a search for 'intermittent fasting'
    in pl.wikipedia happily returns 'Stres oksydacyjny' (oxidative stress) and
    the tool then computes a confident trend for the wrong concept.
    """
    url = f"https://{lang}.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "prop": "pageprops", "ppprop": "wikibase_item",
        "titles": title, "redirects": "1", "format": "json"})
    try:
        data = fetch_json(url, ttl=7 * 86400.0)
    except NotFound:
        return None
    for _, page in (data.get("query", {}).get("pages", {}) or {}).items():
        qid = (page.get("pageprops") or {}).get("wikibase_item")
        if qid:
            return qid
    return None


def resolve_topic(topic: str, langs: Sequence[str], search_lang: str = "en",
                  qid: Optional[str] = None,
                  allow_unverified: bool = False) -> Dict[str, Any]:
    """Map a free-form topic to real article titles, and verify every one of them.

    Two rules make this trustworthy:
      * among the Wikidata search hits, prefer the item that actually exists in
        Wikipedia (most sitelinks) - raw search returns clinical trials and
        journal articles for medical topics;
      * a title that did not come from a sitelink is accepted only if the page
        maps back to the same Wikidata item.
    """
    candidates: List[Dict[str, Any]] = []
    entities: Dict[str, Any] = {}
    warnings: List[str] = []

    if qid is None:
        candidates = wd_search(topic, search_lang)
        if candidates:
            ids = [c["qid"] for c in candidates[:5] if c.get("qid")]
            entities = wd_entities(ids, props="sitelinks|claims") if ids else {}
            counts = {i: len(sitelinks_of(entities.get(i, {}))) for i in ids}
            publications = {i for i in ids
                            if instance_of(entities.get(i, {})) & PUBLICATION_TYPES}
            concepts = [i for i in ids if i not in publications] or ids
            qid = max(concepts, key=lambda i: counts.get(i, 0)) if concepts else None
            if qid and counts.get(qid, 0) == 0:
                warnings.append("The matched Wikidata item has no Wikipedia article in "
                                "any language - the topic may be phrased unusually.")
            # An alternative is only worth mentioning if it is a comparable
            # concept. "Astronomy and Astrophysics" (a journal, 6 editions) is
            # not a rival reading of "astronomy" (44 editions); "Mercury the
            # element" vs "Mercury the planet" is.
            best = counts.get(qid, 0)
            candidates = [c for c in candidates
                          if c["qid"] != qid
                          and c["qid"] not in publications
                          and counts.get(c["qid"], 0) >= max(3, best * RIVAL_RATIO)]

    if qid and qid not in entities:
        entities = wd_entities([qid], props="sitelinks")
    sitelinks = sitelinks_of(entities.get(qid, {})) if qid else {}

    labels: Dict[str, str] = {}
    descriptions: Dict[str, str] = {}
    if qid:
        ents = wd_entities([qid], props="labels|descriptions",
                           languages=list(dict.fromkeys(list(langs) + [search_lang])))
        entity_data = ents.get(qid, {}) or {}
        labels = {l: v.get("value") for l, v in (entity_data.get("labels") or {}).items()}
        descriptions = {l: v.get("value") for l, v in
                        (entity_data.get("descriptions") or {}).items()}

    resolved: Dict[str, Dict[str, str]] = {}
    missing: List[str] = []
    rejected: List[Dict[str, str]] = []

    for lang in langs:
        if lang in sitelinks:
            resolved[lang] = {"article": sitelinks[lang], "source": "wikidata",
                              "title_confidence": "high"}
            continue
        query = labels.get(lang) or labels.get(search_lang) or topic
        accepted = None
        for hit in wiki_search(lang, query):
            hit_qid = wiki_page_qid(lang, hit)
            if hit_qid == qid:
                accepted = {"article": hit, "source": "wiki-search-verified",
                            "title_confidence": "medium"}
                break
            rejected.append({"lang": lang, "article": hit, "qid": hit_qid or "unknown"})
        if accepted:
            resolved[lang] = accepted
        elif allow_unverified and rejected and rejected[-1]["lang"] == lang:
            resolved[lang] = {"article": rejected[-1]["article"],
                              "source": "wiki-search-unverified",
                              "title_confidence": "low"}
        else:
            missing.append(lang)

    entity_label = next((c["label"] for c in candidates if c["qid"] == qid), None)
    if entity_label is None and qid:
        entity_label = labels.get(search_lang) or labels.get("en") or topic

    if candidates:
        warnings.append("Topic may be ambiguous - other Wikipedia concepts match this "
                        "phrase. Check the entity description and re-run with --qid if "
                        "this is the wrong one.")
    if rejected:
        detail = "; ".join(f"{r['lang']}: '{r['article']}' belongs to {r['qid']}"
                           for r in rejected[:4])
        warnings.append(f"Rejected search hits that describe a different concept ({detail}). "
                        "They were NOT analysed.")
    if missing:
        warnings.append(f"No verified article in: {', '.join(missing)}. That usually means "
                        "the concept has no page in that edition - a finding in itself, but "
                        "never read it as 'no demand' without checking manually.")
    unverified = [l for l, v in resolved.items() if v["title_confidence"] == "low"]
    if unverified:
        warnings.append(f"UNVERIFIED titles used for {', '.join(unverified)} "
                        "(--allow-unverified). These numbers may describe a different "
                        "topic entirely; confidence is forced to 'low'.")

    return {"topic": topic, "qid": qid,
            "entity": {"label": entity_label,
                       "description": descriptions.get(search_lang, ""),
                       "wikipedia_editions": len(sitelinks)},
            "alternatives": [{"qid": c["qid"], "label": c["label"],
                              "description": c["description"]} for c in candidates[:3]],
            "resolved": resolved, "missing": missing, "rejected": rejected,
            "warnings": warnings}

# ==========================================================================
# 4. Pageviews: article series + the denominator
# ==========================================================================
def fetch_pageviews(project: str, article: str, start: str, end: str,
                    granularity: str = "monthly", access: str = "all-access",
                    agent: str = "user") -> List[Dict[str, Any]]:
    """Original signature kept: project like 'uk.wikipedia', dates YYYYMMDD."""
    safe = urllib.parse.quote(article.replace(" ", "_"), safe="")
    url = (f"{REST}/per-article/{project}/{access}/{agent}/{safe}/"
           f"{granularity}/{start}/{end}")
    try:
        data = fetch_json(url, ttl=_ttl_for(parse_date(end, is_end=True)))
    except NotFound:
        return []
    return [{"date": _iso(i["timestamp"]), "views": int(i["views"])}
            for i in data.get("items", [])]


def fetch_project_totals(project: str, start: str, end: str,
                         granularity: str = "monthly", access: str = "all-access",
                         agent: str = "user") -> Dict[str, int]:
    """The denominator: total pageviews of the whole language edition."""
    url = (f"{REST}/aggregate/{project}/{access}/{agent}/{granularity}/{start}/{end}")
    try:
        data = fetch_json(url, ttl=_ttl_for(parse_date(end, is_end=True)))
    except NotFound:
        return {}
    return {_iso(i["timestamp"]): int(i["views"]) for i in data.get("items", [])}


def build_series(project: str, article: str, start: dt.date, end: dt.date,
                 granularity: str = "monthly", access: str = "all-access",
                 agent: str = "user", normalize: bool = True,
                 title_confidence: str = "high") -> Dict[str, Any]:
    raw = fetch_pageviews(project, article, _stamp(start), _stamp(end),
                          granularity, access, agent)
    totals = (fetch_project_totals(project, _stamp(start), _stamp(end),
                                   granularity, access, agent) if normalize and raw else {})
    points = []
    for item in raw:
        total = totals.get(item["date"])
        points.append({"date": item["date"], "views": item["views"],
                       "project_views": total,
                       "share_ppm": (item["views"] / total * 1e6) if total else None})
    lang = project.split(".")[0]
    return {"lang": lang, "project": project, "article": article,
            "granularity": granularity, "access": access, "agent": agent,
            "normalized": bool(totals), "title_confidence": title_confidence,
            "points": points}


# ==========================================================================
# 5. statistics (pure numpy-free where possible; numpy only for convenience)
# ==========================================================================
def _betacf(a: float, b: float, x: float) -> float:
    MAXIT, EPS, FPMIN = 300, 3e-16, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (FPMIN if abs(d) < FPMIN else d)
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (FPMIN if abs(d) < FPMIN else d)
        c = 1.0 + aa / c
        c = FPMIN if abs(c) < FPMIN else c
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (FPMIN if abs(d) < FPMIN else d)
        c = 1.0 + aa / c
        c = FPMIN if abs(c) < FPMIN else c
        de = d * c
        h *= de
        if abs(de - 1.0) < EPS:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                     + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_pvalue(t: float, df: int) -> float:
    """Two-sided p-value for a t statistic (no scipy dependency)."""
    if df <= 0:
        return 1.0
    if not math.isfinite(t):
        return 0.0
    return betainc(df / 2.0, 0.5, df / (df + t * t))


def loglinear_trend(values: Sequence[float]) -> Dict[str, Any]:
    """Fit log(y) = a + b*t. The slope reads as growth per period."""
    n = len(values)
    if n < 4:
        return {"n": n, "growth_per_period": 0.0, "total_change_pct": 0.0,
                "r2": 0.0, "p_value": 1.0, "significant": False}
    # Guard zeros without destroying scale: a fixed floor of 1.0 would flatten
    # any series measured in small units (share_ppm is often single digits).
    positive = [float(v) for v in values if v > 0]
    floor = min(positive) / 10.0 if positive else 1.0
    ly = [math.log(max(float(v), floor)) for v in values]
    t = list(range(n))
    mt, my = sum(t) / n, sum(ly) / n
    sxx = sum((ti - mt) ** 2 for ti in t)
    sxy = sum((ti - mt) * (yi - my) for ti, yi in zip(t, ly))
    slope = sxy / sxx if sxx else 0.0
    intercept = my - slope * mt
    resid = [yi - (slope * ti + intercept) for ti, yi in zip(t, ly)]
    ss_res = sum(r * r for r in resid)
    ss_tot = sum((yi - my) ** 2 for yi in ly)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    df = n - 2
    if df > 0 and ss_res > 0 and sxx > 0:
        se = math.sqrt(ss_res / df / sxx)
        t_stat = slope / se if se else float("inf")
        p = t_pvalue(t_stat, df)
    else:
        t_stat, p = (float("inf"), 0.0) if slope else (0.0, 1.0)
    return {"n": n, "growth_per_period": math.exp(slope) - 1.0,
            "total_change_pct": (math.exp(slope * (n - 1)) - 1.0) * 100.0,
            "r2": r2, "t_stat": t_stat, "p_value": p, "significant": p < 0.05}


def median(values: Sequence[float]) -> float:
    s = sorted(values)
    n = len(s)
    if not n:
        return 0.0
    return float(s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2)


def mad_zscores(values: Sequence[float]) -> List[float]:
    med = median(values)
    mad = median([abs(v - med) for v in values])
    if mad == 0:
        mean_abs = sum(abs(v - med) for v in values) / max(1, len(values))
        if mean_abs == 0:
            return [0.0] * len(values)
        return [0.7979 * (v - med) / mean_abs for v in values]
    return [0.6745 * (v - med) / mad for v in values]


def pct_change(old: float, new: float) -> float:
    return 0.0 if old == 0 else (new - old) / old * 100.0


# ==========================================================================
# 6. analysis: metrics, quality flags, confidence
# ==========================================================================
def _downgrade(level: str, steps: int = 1) -> str:
    return CONFIDENCE_ORDER[max(0, CONFIDENCE_ORDER.index(level) - steps)]


def _days_in(date_iso: str) -> int:
    d = dt.date.fromisoformat(date_iso)
    return calendar.monthrange(d.year, d.month)[1]


def _expected_months(first: str, last: str) -> int:
    a, b = dt.date.fromisoformat(first), dt.date.fromisoformat(last)
    return (b.year - a.year) * 12 + (b.month - a.month) + 1


def _seasonality(points, values, exclude: Sequence[str] = ()) -> Dict[str, Any]:
    """Seasonal index, computed with outlier months removed.

    Without the exclusion a single news spike in, say, September turns into a
    claimed 288% seasonal swing, which is then used to explain the whole series.
    """
    if len(values) < 24:
        return {"available": False, "note": "needs 24 months"}
    skip = set(exclude)
    kept = [(p, v) for p, v in zip(points, values) if p["date"] not in skip]
    if len(kept) < 20:
        return {"available": False, "note": "too many outlier months to estimate seasonality"}
    by_month: Dict[int, List[float]] = {}
    for p, v in kept:
        by_month.setdefault(int(p["date"][5:7]), []).append(v)
    overall = sum(v for _, v in kept) / len(kept)
    if overall == 0:
        return {"available": False, "note": "no traffic"}
    index = {f"{m:02d}": round(sum(vs) / len(vs) / overall, 3)
             for m, vs in sorted(by_month.items()) if len(vs) >= 2}
    if not index:
        return {"available": False, "note": ""}
    amp = (max(index.values()) - min(index.values())) * 100
    peak, trough = max(index, key=index.get), min(index, key=index.get)
    years = min(len(v) for v in by_month.values())
    note = (f"Seasonal swing {amp:.0f}% (peak {peak}, trough {trough}). "
            "Compare like months year over year, never consecutive months.")
    if years < 3:
        note += (f" Estimated from only {years} observation(s) per month"
                 + (" after removing outliers" if skip else "") + " - indicative only.")
    return {"available": True, "index": index, "amplitude_pct": round(amp, 1),
            "peak_month": peak, "trough_month": trough,
            "years_per_month": years, "outliers_excluded": len(skip),
            "note": note}


def _yoy(points, values) -> Dict[str, Any]:
    if len(values) < 24:
        return {"available": False, "reason": "needs 24 months of history"}
    last, prev = sum(values[-12:]), sum(values[-24:-12])
    ups = sum(1 for i in range(-12, 0) if values[i] > values[i - 12])
    return {"available": True, "change_pct": round(pct_change(prev, last), 1),
            "window_last": f"{points[-12]['date'][:7]}..{points[-1]['date'][:7]}",
            "window_prev": f"{points[-24]['date'][:7]}..{points[-13]['date'][:7]}",
            "months_up": ups}


def _level_shift(values: Sequence[float]) -> Optional[Dict[str, Any]]:
    n = len(values)
    if n < 12:
        return None
    best = None
    for cut in range(4, n - 4):
        before = median(values[max(0, cut - 6):cut])
        after = median(values[cut:cut + 6])
        if before <= 0 and after <= 0:
            continue
        ratio = (after + 1e-9) / (before + 1e-9)
        score = max(ratio, 1 / ratio if ratio > 0 else 0)
        if score >= LEVEL_SHIFT_RATIO and (best is None or score > best["score"]):
            best = {"index": cut, "score": round(score, 1),
                    "direction": "up" if ratio > 1 else "down"}
    return best


def analyze_series(series: Dict[str, Any]) -> Dict[str, Any]:
    """One article in one language -> metrics + flags + confidence + caveats."""
    points = series.get("points") or []
    lang, article = series.get("lang"), series.get("article")
    base = {"lang": lang, "project": series.get("project"), "article": article}

    if not points:
        return {**base, "status": "no_data", "flags": ["no_data"], "confidence": "none",
                "metrics": {}, "caveats": [],
                "summary": (f"No pageview data for '{article}' in {series.get('project')}. "
                            "Either the title is wrong, the page is a redirect, or it does "
                            "not exist - check with --resolve-only."),
                "points": []}

    monthly = series.get("granularity", "monthly") == "monthly"
    normalized = bool(series.get("normalized"))
    raw = [float(p["views"]) for p in points]
    values = ([float(p["share_ppm"]) if p.get("share_ppm") is not None else float(p["views"])
               for p in points] if normalized else raw)

    flags: List[str] = []
    caveats: List[str] = []

    # --- volume floor -----------------------------------------------------
    daily = [v / _days_in(p["date"]) for p, v in zip(points, raw)] if monthly else raw
    med_daily = median(daily)
    if med_daily < MIN_DAILY_VIEWS:
        flags.append("low_volume")
        caveats.append(f"Median {med_daily:.0f} views/day is below the {MIN_DAILY_VIEWS}/day "
                       "noise floor; period-to-period moves are mostly random, and a "
                       "statistically significant slope on this little traffic is still a "
                       "weak basis for a decision.")

    # --- coverage ---------------------------------------------------------
    if monthly:
        if len(points) < MIN_POINTS:
            flags.append("short_history")
            caveats.append(f"Only {len(points)} months of data - no seasonal baseline.")
        expected = _expected_months(points[0]["date"], points[-1]["date"])
        if expected > len(points):
            flags.append("series_gap")
            caveats.append(f"{expected - len(points)} month(s) missing inside the period.")

    # --- trend ------------------------------------------------------------
    trend = loglinear_trend(values)
    trend_raw = loglinear_trend(raw)
    if not trend["significant"]:
        flags.append("trend_not_significant")
        caveats.append(f"Trend is not statistically distinguishable from noise "
                       f"(p={trend['p_value']:.2f}); do not report the percentage as growth.")

    # --- baseline: the topic, or the whole edition? -----------------------
    baseline: Dict[str, Any] = {"available": False}
    totals = [p.get("project_views") for p in points]
    if normalized and all(t for t in totals):
        proj = loglinear_trend([float(t) for t in totals])
        baseline = {"available": True,
                    "project_total_change_pct": round(proj["total_change_pct"], 1),
                    "topic_raw_change_pct": round(trend_raw["total_change_pct"], 1),
                    "normalised_change_pct": round(trend["total_change_pct"], 1)}
        if abs(trend["total_change_pct"]) < 5 and abs(trend_raw["total_change_pct"]) > 15:
            flags.append("tracks_project_traffic")
            caveats.append("Raw views moved but the share of total wiki traffic did not: "
                           "this is the whole edition moving, not this topic.")

    # --- spikes -----------------------------------------------------------
    z = mad_zscores(values) if len(values) >= 8 else [0.0] * len(values)
    anomalies = [{"date": p["date"], "z": round(zi, 1), "views": int(r)}
                 for p, r, zi in zip(points, raw, z) if abs(zi) >= SPIKE_MAD_Z]
    total = sum(values) or 1.0
    top_share = max(values) / total
    if len(values) >= 12 and top_share > SPIKE_SHARE:
        flags.append("spike_dominated")
        caveats.append(f"A single period holds {top_share*100:.0f}% of all attention - "
                       "likely a news event, not sustained interest.")
    elif anomalies:
        caveats.append("Outlier periods: " + ", ".join(a["date"] for a in anomalies[:4])
                       + ". Check for a news event before reading them as a trend.")

    if anomalies:
        mask = {a["date"] for a in anomalies}
        clean = [v for p, v in zip(points, values) if p["date"] not in mask]
        if len(clean) >= 6:
            ct = loglinear_trend(clean)
            if (ct["growth_per_period"] > 0) != (trend["growth_per_period"] > 0):
                flags.append("trend_flips_without_outliers")
                caveats.append("Trend direction reverses once outlier periods are removed - "
                               "the 'trend' is one event.")

    # --- level shift ------------------------------------------------------
    shift = _level_shift(values)
    if shift:
        flags.append("level_shift")
        caveats.append(f"Level shift ({shift['direction']}, x{shift['score']}) around "
                       f"{points[shift['index']]['date']} - possible page rename, redirect "
                       "or merge; before/after comparison is unsafe.")

    seasonality = (_seasonality(points, values, [a["date"] for a in anomalies])
                   if monthly else {"available": False, "note": "daily granularity"})
    yoy = _yoy(points, values) if monthly else {"available": False, "reason": "daily granularity"}
    if seasonality.get("available") and seasonality["amplitude_pct"] > 40:
        caveats.append(seasonality["note"])

    # --- confidence -------------------------------------------------------
    hard = {"short_history", "spike_dominated", "series_gap",
            "trend_flips_without_outliers", "level_shift"}
    confidence = _downgrade("high", sum(1 for f in flags if f in hard))
    if "trend_not_significant" in flags:
        confidence = _downgrade(confidence)
    if not normalized:
        confidence = _downgrade(confidence)
        caveats.append("Not normalised by total wiki traffic - cross-language comparison "
                       "is unsafe (editions differ in size and drift over time).")
    if "low_volume" in flags:
        # a hard cap, not a step: below the noise floor nothing deserves "medium"
        confidence = min(confidence, "low", key=CONFIDENCE_ORDER.index)

    # an unverified article title makes every number meaningless, however clean
    if series.get("title_confidence") == "low":
        flags.append("unverified_title")
        caveats.append(f"The title '{article}' was NOT verified against the Wikidata entity "
                       "for this topic - it may describe something else entirely. Verify it "
                       "before quoting any number from this row.")
        confidence = "none"

    unit = "per month" if monthly else "per day"
    metrics = {
        "metric": "share_ppm" if normalized else "views",
        "granularity": series.get("granularity"),
        "points": len(points),
        "period": f"{points[0]['date']}..{points[-1]['date']}",
        "total_views": int(sum(raw)),
        "median_daily_views": round(med_daily, 1),
        "last_period_views": int(raw[-1]),
        f"growth_{unit.replace(' ', '_')}_pct": round(trend["growth_per_period"] * 100, 2),
        "total_change_pct": round(trend["total_change_pct"], 1),
        "r2": round(trend["r2"], 2),
        "p_value": (round(trend["p_value"], 4) if trend["p_value"] >= 1e-4
                    else float(f"{trend['p_value']:.1e}")),
        "significant": trend["significant"],
        "audience_size_ppm": (round(sum(v for v in values[-12:]) / len(values[-12:]), 2)
                              if normalized else None),
        "yoy": yoy, "seasonality": {k: v for k, v in seasonality.items() if k != "index"},
        "seasonality_index": seasonality.get("index", {}),
        "anomalies": anomalies[:6], "baseline": baseline,
    }

    slope = trend["growth_per_period"]
    direction = ("growing" if slope > 0.002 else "declining" if slope < -0.002 else "flat")
    if not trend["significant"]:
        direction = "flat (no significant trend)"
    p_text = "p<0.001" if metrics["p_value"] < 0.001 else f"p={metrics['p_value']:.3f}"
    summary = (f"{lang}: '{article}' is {direction} - {metrics['total_change_pct']:+.0f}% over "
               f"{metrics['period']} ({p_text}, "
               f"median {metrics['median_daily_views']:.0f} views/day). "
               f"Confidence: {confidence}.")

    return {**base, "status": "ok", "normalized": normalized, "metrics": metrics,
            "flags": flags, "caveats": caveats, "confidence": confidence,
            "summary": summary, "points": points}


def compare_segments(analyses: List[Dict[str, Any]]) -> Dict[str, Any]:
    usable = [a for a in analyses if a["status"] == "ok"]
    ranked = sorted(usable, key=lambda a: (CONFIDENCE_ORDER.index(a["confidence"]),
                                           a["metrics"]["total_change_pct"]), reverse=True)
    rows = [{"rank": i + 1, "lang": a["lang"], "article": a.get("article") or "-",
             "total_change_pct": a["metrics"]["total_change_pct"],
             "significant": a["metrics"]["significant"],
             "yoy_pct": a["metrics"]["yoy"].get("change_pct"),
             "median_daily_views": a["metrics"]["median_daily_views"],
             "audience_size_ppm": a["metrics"]["audience_size_ppm"],
             "confidence": a["confidence"], "flags": a["flags"]}
            for i, a in enumerate(ranked)]

    notes: List[str] = []
    skipped = [a for a in analyses if a["status"] != "ok"]
    if skipped:
        notes.append("No data for: " + ", ".join(
            f"{a['lang']}" + (f" ('{a['article']}')" if a.get("article") else "")
            for a in skipped)
            + ". Missing data is not evidence of low demand - verify the title first.")
    if any(not a["normalized"] for a in usable):
        notes.append("Some series are not normalised; the ranking is indicative only.")
    weak = [a["lang"] for a in usable if a["confidence"] in ("low", "none")]
    if weak:
        notes.append("Low-confidence segments (do not decide on these alone): " + ", ".join(weak))
    if usable:
        notes.append("Growth and market size are different questions: a small edition can grow "
                     "fast and still be a tiny audience. Read audience_size_ppm next to growth.")
    return {"ranking": rows, "leader": rows[0]["lang"] if rows else None, "notes": notes}


def build_verdict(study: Dict[str, Any]) -> Tuple[str, str]:
    ok = [a for a in study["segments"] if a["status"] == "ok"]
    ranking = study["comparison"]["ranking"]
    if not ok:
        return ("No usable pageview data for these segments.",
                "Verify article titles with --resolve-only, or widen the period.")
    def describe(row: Dict[str, Any]) -> str:
        if not row["significant"]:
            return f"{row['lang']}: no measurable trend"
        return f"{row['lang']}: {row['total_change_pct']:+.0f}%"

    if len(ok) == 1:
        verdict = ok[0]["summary"]
    elif all(not r["significant"] for r in ranking):
        verdict = ("No segment shows a statistically measurable trend over this period - "
                   "the differences between them are within noise.")
    else:
        top = ranking[0]
        rest = ", ".join(describe(r) for r in ranking[1:])
        verdict = (f"{top['lang']} leads on normalised growth "
                   f"({top['total_change_pct']:+.0f}%, confidence {top['confidence']}); "
                   f"{rest}.")

    strong = [r for r in ranking if r["confidence"] in ("high", "medium")]
    if not strong:
        rec = ("No segment here is backed by high-confidence data. Extend the period or "
               "validate demand outside Wikipedia (search ads, a landing page test) before "
               "committing build time.")
    else:
        lead = strong[0]
        parts = [f"Investigate {lead['lang']} first: {lead['total_change_pct']:+.0f}% with "
                 f"{lead['confidence']} confidence."]
        sized = [r for r in ranking if r["audience_size_ppm"]]
        if sized:
            big = max(sized, key=lambda r: r["audience_size_ppm"])
            if big["lang"] != lead["lang"]:
                parts.append(f"{big['lang']} has the largest existing audience "
                             f"({big['audience_size_ppm']:.1f} views per million) - "
                             "the slower, safer bet.")
        weak = [r["lang"] for r in ranking if r["confidence"] in ("low", "none")]
        if weak:
            parts.append(f"Treat {', '.join(weak)} as unproven.")
        parts.append("Validate with a cheap demand test before building.")
        rec = " ".join(parts)
    return verdict, rec


# ==========================================================================
# 7. chart
# ==========================================================================
def build_chart(study: Dict[str, Any], path: str) -> Optional[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    ok = [a for a in study["segments"] if a["status"] == "ok" and a.get("points")]
    if not ok:
        return None
    normalized = ok[0].get("normalized")
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9,
                         "axes.spines.top": False, "axes.spines.right": False})
    fig, ax = plt.subplots(figsize=(9, 3.6))
    palette = ["#2563eb", "#dc2626", "#059669", "#d97706", "#7c3aed", "#0891b2"]

    for i, a in enumerate(ok):
        color = palette[i % len(palette)]
        x = [dt.date.fromisoformat(p["date"]) for p in a["points"]]
        y = [(p["share_ppm"] if normalized and p.get("share_ppm") is not None else p["views"])
             for p in a["points"]]
        label_article = (a.get("article") or "?")[:30]
        ax.plot(x, y, color=color, lw=1.0, alpha=0.4)
        if len(y) >= 6:                      # centred rolling mean, full span
            win = 3 if len(y) < 18 else 5
            pad = win // 2
            padded = [y[0]] * pad + list(y) + [y[-1]] * pad
            smooth = [sum(padded[j:j + win]) / win for j in range(len(y))]
            ax.plot(x, smooth, color=color, lw=2.0, label=f"{a['lang']}: {label_article}")
        else:
            ax.plot(x, y, color=color, lw=2.0, label=f"{a['lang']}: {label_article}")
        for an in a["metrics"].get("anomalies", [])[:3]:
            idx = next((k for k, p in enumerate(a["points"]) if p["date"] == an["date"]), None)
            if idx is not None:
                ax.scatter([x[idx]], [y[idx]], s=28, color=color, zorder=5,
                           edgecolor="white", linewidth=0.8)

    ax.set_ylabel("Views per million wiki pageviews" if normalized else "Pageviews")
    ax.set_ylim(bottom=0)
    ax.grid(True, linestyle="--", alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


# ==========================================================================
# 8. PDF (Unicode font, transliteration only as a fallback)
# ==========================================================================
def safe_pdf_text(text: str) -> str:
    """Fallback for systems without any Unicode TTF: transliterate Cyrillic and
    strip diacritics so FPDF's latin-1 core fonts do not crash."""
    cyr = "АБВГДЕЄЖЗИІЇЙКЛМНОПРСТУФХЦЧШЩЬЮЯабвгдеєжзиіїйклмнопрстуфхцчшщьюя"
    lat = ["A", "B", "V", "G", "D", "E", "Ye", "Zh", "Z", "Y", "I", "Yi", "Y", "K", "L", "M",
           "N", "O", "P", "R", "S", "T", "U", "F", "Kh", "Ts", "Ch", "Sh", "Shch", "'", "Yu",
           "Ya", "a", "b", "v", "g", "d", "e", "ye", "zh", "z", "y", "i", "yi", "y", "k", "l",
           "m", "n", "o", "p", "r", "s", "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "'",
           "yu", "ya"]
    table = dict(zip(cyr, lat))
    res = "".join(table.get(c, c) for c in text)
    res = "".join(c for c in unicodedata.normalize("NFD", res)
                  if unicodedata.category(c) != "Mn")
    return res.encode("latin-1", "replace").decode("latin-1")


class Report:
    """Thin wrapper over FPDF that picks a Unicode font when one exists."""

    def __init__(self) -> None:
        from fpdf import FPDF
        self.pdf = FPDF()
        self.unicode_ok = False
        self.family = "Helvetica"
        regular = next((p for p in FONT_CANDIDATES if Path(p).exists()), None)
        if regular:
            try:
                self.pdf.add_font("Body", "", regular)
                bold = next((p for p in BOLD_CANDIDATES if Path(p).exists()), regular)
                self.pdf.add_font("Body", "B", bold)
                self.family, self.unicode_ok = "Body", True
            except Exception:                       # broken font file, keep Helvetica
                self.family, self.unicode_ok = "Helvetica", False
        self.pdf.set_auto_page_break(auto=True, margin=10)
        self.pdf.add_page()

    def text(self, value: str) -> str:
        return value if self.unicode_ok else safe_pdf_text(value)

    def font(self, size: float, bold: bool = False) -> None:
        self.pdf.set_font(self.family, "B" if bold else "", size)

    def line(self, value: str, size: float = 9, bold: bool = False,
             height: float = 5) -> None:
        from fpdf import XPos, YPos
        self.font(size, bold)
        self.pdf.cell(0, height, self.text(value), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def block(self, value: str, size: float = 8.5, height: float = 4.4,
              border: int = 0, fill: bool = False) -> None:
        """multi_cell with an explicit cursor reset: fpdf2 leaves x at the right
        edge by default, which silently pushes the next line off the page."""
        from fpdf import XPos, YPos
        self.font(size)
        self.pdf.multi_cell(0, height, self.text(value), border=border, fill=fill,
                            new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def build_pdf_report(study: Dict[str, Any], filename: str,
                     chart_path: Optional[str] = None,
                     ai_summary: Optional[str] = None) -> str:
    from fpdf import XPos, YPos

    rep = Report()
    pdf = rep.pdf
    period = study["period"]

    rep.line("Wikipedia Market Trends", 15, bold=True, height=8)
    rep.line(f"{study.get('topic') or 'Segments'}   |   {period['from']} - {period['to']}   |   "
             f"{period['granularity']}, agent=user   |   "
             f"generated {dt.date.today().isoformat()}", 8)
    if study.get("question"):
        rep.block(f"Question: {study['question']}", 8.5)
    pdf.ln(1)

    # verdict box
    pdf.set_fill_color(241, 245, 249)
    rep.font(10.5, bold=True)
    rep.block(study["verdict"], 10.5, height=5.5, border=1, fill=True)
    all_flags = sorted({f for a in study["segments"] for f in a.get("flags", [])})
    rep.line(f"Overall confidence: {study['confidence'].upper()}"
             f"   (issues found: {', '.join(all_flags) if all_flags else 'none'})",
             8, bold=True)
    pdf.ln(1)

    if chart_path and Path(chart_path).exists():
        pdf.image(chart_path, x=10, w=190)
        pdf.ln(2)

    # key numbers
    rep.line("Key numbers", 10, bold=True, height=6)
    widths = [14, 52, 24, 20, 22, 24, 24]
    head = ["Lang", "Article", "Change", "YoY", "Views/day", "Share ppm", "Confidence"]
    rep.font(8, bold=True)
    pdf.set_fill_color(226, 232, 240)
    for w, h in zip(widths, head):
        pdf.cell(w, 5.5, rep.text(h), border=1, fill=True, align="C")
    pdf.ln()
    rep.font(8)
    for a in study["segments"]:
        title = a.get("article") or "(no article in this edition)"
        if a["status"] != "ok":
            cells = [a["lang"], title[:30], "-", "-", "-", "-", "no data"]
        else:
            m = a["metrics"]
            yoy = m["yoy"].get("change_pct")
            cells = [a["lang"], title[:30],
                     f"{m['total_change_pct']:+.1f}%" if m["significant"] else "no trend",
                     f"{yoy:+.1f}%" if yoy is not None else "-",
                     f"{m['median_daily_views']:.0f}",
                     f"{m['audience_size_ppm']:.1f}" if m["audience_size_ppm"] else "-",
                     a["confidence"]]
        for col, (w, c) in enumerate(zip(widths, cells)):
            pdf.cell(w, 5, rep.text(str(c)), border=1, align="L" if col < 2 else "R")
        pdf.ln()
    pdf.ln(2)

    rep.line("What to do next", 10, bold=True, height=6)
    rep.block(study["recommendation"])

    if ai_summary:
        pdf.ln(1)
        rep.line("Executive summary (LLM)", 10, bold=True, height=6)
        rep.block(ai_summary)

    # limitations - generated from the flags that actually fired
    pdf.ln(1)
    rep.line("Assumptions and limitations", 10, bold=True, height=6)
    lines = list(study.get("notes", []))
    for a in study["segments"]:
        for c in a.get("caveats", []):
            lines.append(f"[{a['lang']}] {c}")
    lines.append("Pageviews measure attention, not willingness to pay. A language edition is "
                 "not a country; Wikipedia readers skew to desktop, study and research use.")
    lines.append("Source: Wikimedia Pageviews API (per-article + aggregate), titles resolved "
                 f"via Wikidata sitelinks. Bot traffic excluded (agent=user). "
                 f"wikiscout {__version__}.")
    seen, uniq = set(), []
    for line_text in lines:
        if line_text not in seen:
            seen.add(line_text)
            uniq.append(line_text)
    pdf.set_fill_color(254, 243, 199)
    rep.block("\n".join(f"- {t}" for t in uniq[:10]), 7.4, height=3.8,
              border=1, fill=True)

    pdf.output(filename)
    return filename


# ==========================================================================
# 9. optional LLM paragraph (off by default - the agent should do this)
# ==========================================================================
def generate_insights_with_gemini(study: Dict[str, Any]) -> str:
    """Kept from the original tool for standalone runs. When this script is
    used as an agent skill, leave it off: the agent already has the JSON and
    can answer in the user's language."""
    from google import genai
    from google.genai.errors import ServerError

    facts = {
        "verdict": study["verdict"],
        "confidence": study["confidence"],
        "ranking": study["comparison"]["ranking"],
        "caveats": [c for a in study["segments"] for c in a.get("caveats", [])],
    }
    prompt = f"""You are a B2C product growth analyst. Here is a finished analysis of
Wikipedia pageview data, already normalised by total wiki traffic and graded for
reliability:

{json.dumps(facts, ensure_ascii=False, indent=1)}

Write an executive summary for a startup founder, max 120 words.
Rules: use ONLY the numbers given above - never invent or recompute any figure.
State the confidence level and the main limitation explicitly. If confidence is
low, say plainly that the data does not support a decision.
Plain English text, no markdown, no bullet symbols."""

    client = genai.Client()
    delay = 2
    for attempt in range(3):
        try:
            chat = client.chats.create(model=os.getenv("WT_GEMINI_MODEL", "gemini-2.5-flash"))
            response = chat.send_message(prompt)
            return (response.text or "").strip().replace("**", "").replace("—", "-")
        except ServerError as exc:
            if getattr(exc, "code", None) == 503 and attempt < 2:
                log(f"   Gemini 503, retry in {delay}s")
                time.sleep(delay)
                delay *= 2
                continue
            raise
    return ""


# ==========================================================================
# 10. orchestration
# ==========================================================================
def parse_queries(queries: Iterable[str]) -> List[Tuple[str, str]]:
    """'pl.wikipedia:Post_przerywany' -> ('pl.wikipedia', 'Post przerywany')."""
    out = []
    for q in queries:
        if ":" not in q:
            raise ValueError(f"invalid query '{q}', expected 'project:article'")
        project, article = q.split(":", 1)
        if "." not in project:
            project = f"{project}.wikipedia"
        out.append((project, article.replace("_", " ")))
    return out


def run_study(args: argparse.Namespace) -> Dict[str, Any]:
    start, end = resolve_period(args.start, args.end, args.granularity)
    resolution: Dict[str, Any] = {}
    targets: List[Tuple[str, str]] = []

    if args.topic:
        langs = [l.strip() for l in (args.langs or "en").split(",") if l.strip()]
        if not args.langs:
            log("   (no --langs given, defaulting to en)")
        log(f"1. Resolving '{args.topic}' via Wikidata...")
        resolution = resolve_topic(args.topic, langs, args.search_lang, args.qid,
                                   allow_unverified=args.allow_unverified)
        for lang in langs:
            info = resolution["resolved"].get(lang)
            if info:
                targets.append((f"{lang}.wikipedia", info["article"]))
                log(f"   {lang}: {info['article']} ({info['source']})")
            else:
                targets.append((f"{lang}.wikipedia", None))
                log(f"   {lang}: no verified article found - skipped")
        for r in resolution.get("rejected", []):
            log(f"   rejected {r['lang']}: '{r['article']}' is {r['qid']}, "
                f"not {resolution['qid']}")
    else:
        targets = parse_queries(args.queries)

    log("2. Fetching pageviews (cached where possible)...")
    segments = []
    for project, article in targets:
        if article is None:
            segments.append({"lang": project.split(".")[0], "project": project,
                             "article": None, "status": "no_article", "flags": ["no_article"],
                             "confidence": "none", "metrics": {}, "caveats": [], "points": [],
                             "summary": f"No verified article about this topic in {project}. "
                                        "Not analysed - a guessed title would produce a "
                                        "confident number about the wrong concept."})
            continue
        title_conf = "high"
        if resolution:
            info = resolution["resolved"].get(project.split(".")[0], {})
            title_conf = info.get("title_confidence", "high")
        series = build_series(project, article, start, end, args.granularity,
                              args.access, args.agent, normalize=not args.no_normalize,
                              title_confidence=title_conf)
        segments.append(analyze_series(series))

    log("3. Analysing...")
    comparison = compare_segments(segments)
    study = {
        "schema": 2,
        "topic": args.topic,
        "question": args.question,
        "qid": resolution.get("qid"),
        "entity": resolution.get("entity"),
        "alternatives": resolution.get("alternatives", []),
        "period": {"from": str(start), "to": str(end), "granularity": args.granularity},
        "normalized": not args.no_normalize,
        "segments": segments,
        "comparison": comparison,
        "notes": list(resolution.get("warnings", [])) + comparison["notes"],
        "confidence": (comparison["ranking"][0]["confidence"]
                       if comparison["ranking"] else "none"),
    }
    study["verdict"], study["recommendation"] = build_verdict(study)
    return study


def brief(study: Dict[str, Any]) -> Dict[str, Any]:
    """The smallest payload that still supports a correct answer.

    A cheap model pays for every token of the full study; this keeps the verdict,
    the ranking and - crucially - every flag and caveat, so nothing that limits
    the conclusion is dropped to save space.
    """
    segments = []
    for a in study["segments"]:
        m = a.get("metrics") or {}
        row = {"lang": a["lang"], "article": a.get("article"), "status": a["status"],
               "confidence": a["confidence"], "flags": a.get("flags", []),
               "summary": a.get("summary"), "caveats": a.get("caveats", [])}
        if m:
            row["numbers"] = {
                "total_change_pct": m.get("total_change_pct"),
                "significant": m.get("significant"),
                "p_value": m.get("p_value"),
                "yoy_pct": (m.get("yoy") or {}).get("change_pct"),
                "median_daily_views": m.get("median_daily_views"),
                "audience_size_ppm": m.get("audience_size_ppm"),
            }
        segments.append(row)
    return {"ok": True, "topic": study.get("topic"), "period": study["period"],
            "verdict": study["verdict"], "recommendation": study["recommendation"],
            "confidence": study["confidence"], "segments": segments,
            "ranking": study["comparison"]["ranking"], "notes": study["notes"],
            "tool_version": __version__}


def compact(study: Dict[str, Any]) -> Dict[str, Any]:
    """What goes to stdout: everything except the raw point arrays."""
    out = json.loads(json.dumps(study, ensure_ascii=False, default=str))
    for seg in out["segments"]:
        seg.pop("points", None)
        seg.get("metrics", {}).pop("seasonality_index", None)
    return out


def self_test() -> int:
    """Offline checks of the analytical core - no network, no API key."""
    def make(values, normalized=True, start=(2023, 1)):
        pts, (y, m) = [], start
        for v in values:
            pts.append({"date": f"{y:04d}-{m:02d}-01", "views": int(v),
                        "project_views": 1_000_000_000 if normalized else None,
                        "share_ppm": float(v) / 1000 if normalized else None})
            m += 1
            if m == 13:
                y, m = y + 1, 1
        return {"lang": "xx", "project": "xx.wikipedia", "article": "Test",
                "granularity": "monthly", "normalized": normalized, "points": pts}

    checks = [
        ("flat series -> trend not significant",
         "trend_not_significant" in analyze_series(make([10000 + (i % 3) * 40
                                                         for i in range(36)]))["flags"]),
        ("exp growth -> significant, rate recovered",
         abs(analyze_series(make([10000 * math.exp(0.03 * i) for i in range(36)]))
             ["metrics"]["growth_per_month_pct"] - (math.exp(0.03) - 1) * 100) < 0.01),
        ("one huge month -> spike_dominated",
         "spike_dominated" in analyze_series(make([500] * 17 + [90000] + [500] * 18))["flags"]),
        ("tiny numbers -> low_volume",
         "low_volume" in analyze_series(make([300] * 36))["flags"]),
        ("12x jump -> level_shift",
         "level_shift" in analyze_series(make([1000] * 18 + [12000] * 18))["flags"]),
        ("pure seasonality -> YoY about zero",
         abs(analyze_series(make([10000 * (1 + 0.5 * math.sin(2 * math.pi * i / 12))
                                  for i in range(24)]))["metrics"]["yoy"]["change_pct"]) < 2),
        ("p-value matches the t table (t=2.086, df=20)",
         abs(t_pvalue(2.086, 20) - 0.05) < 0.005),
        ("no data -> status no_data, confidence none",
         analyze_series({"lang": "xx", "project": "xx.wikipedia", "article": "X",
                         "granularity": "monthly", "normalized": False,
                         "points": []})["confidence"] == "none"),
    ]
    payload = {"ok": all(p for _, p in checks),
               "checks": [{"name": n, "passed": bool(p)} for n, p in checks],
               "passed": sum(1 for _, p in checks if p), "total": len(checks)}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Wikipedia Market Trends - pageview analysis for B2C product decisions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Prints JSON to stdout; progress goes to stderr.")
    p.add_argument("--topic", help="free-form topic, resolved to titles via Wikidata")
    p.add_argument("--langs", help="comma separated language codes for --topic, e.g. pl,cs,uk")
    p.add_argument("--queries", nargs="+",
                   help="explicit project:article pairs (original interface), "
                        "e.g. pl.wikipedia:Post_przerywany")
    p.add_argument("--qid", help="Wikidata QID, to disambiguate the topic")
    p.add_argument("--search-lang", default="en", help="language used to search Wikidata")
    p.add_argument("--start", default="24m",
                   help="YYYYMMDD, YYYY-MM, YYYY, or relative like 24m / 3y (default 24m)")
    p.add_argument("--end", default="latest", help="YYYYMMDD, YYYY-MM or 'latest'")
    p.add_argument("--granularity", default="monthly", choices=["monthly", "daily"])
    p.add_argument("--access", default="all-access",
                   choices=["all-access", "desktop", "mobile-web", "mobile-app"])
    p.add_argument("--agent", default="user", choices=["user", "all-agents", "spider"])
    p.add_argument("--no-normalize", action="store_true",
                   help="skip division by total wiki traffic (not recommended)")
    p.add_argument("--question", help="the user's original question, stored in the study")
    p.add_argument("--output", help="write a one-page PDF to this path")
    p.add_argument("--chart", help="also keep the PNG chart at this path")
    p.add_argument("--json-out", help="write the full study (with raw points) to this file")
    p.add_argument("--brief", action="store_true",
                   help="print a much smaller JSON (verdict, ranking, flags, caveats) - "
                        "use this when a small/cheap model is driving the tool")
    p.add_argument("--resolve-only", action="store_true",
                   help="only show which article each language maps to")
    p.add_argument("--allow-unverified", action="store_true",
                   help="analyse search hits that could NOT be verified against the "
                        "Wikidata entity; their confidence is forced to 'none'")
    p.add_argument("--ai-summary", action="store_true",
                   help="add a Gemini paragraph to the PDF (needs GEMINI_API_KEY); "
                        "leave off when an agent is driving this tool")
    p.add_argument("--cache-clear", action="store_true", help="delete the HTTP cache and exit")
    p.add_argument("--selftest", action="store_true", help="offline checks, no network")
    p.add_argument("--version", action="store_true",
                   help="print the version and a fingerprint of this file, then exit")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.version:
        import hashlib
        path = Path(__file__).resolve()
        print(json.dumps({
            "ok": True, "version": __version__,
            "file": str(path),
            "md5": hashlib.md5(path.read_bytes()).hexdigest(),
            "lines": len(path.read_text(encoding="utf-8").split("\n")),
            "python": sys.version.split()[0],
        }, ensure_ascii=False, indent=2))
        return 0

    if args.selftest:
        return self_test()
    if args.cache_clear:
        if CACHE_PATH.exists():
            CACHE_PATH.unlink()
        print(json.dumps({"ok": True, "cleared": str(CACHE_PATH)}))
        return 0

    try:
        if args.resolve_only:
            if not args.topic:
                raise ValueError("--resolve-only needs --topic")
            langs = [l.strip() for l in (args.langs or "en").split(",") if l.strip()]
            print(json.dumps({"ok": True, **resolve_topic(args.topic, langs,
                                                          args.search_lang, args.qid,
                                                          args.allow_unverified)},
                             ensure_ascii=False, indent=2))
            return 0

        if not args.topic and not args.queries:
            raise ValueError("give either --topic (with --langs) or --queries project:article")

        study = run_study(args)

        chart_path = args.chart
        if args.output and not chart_path:
            chart_path = str(Path(args.output).with_suffix(".png"))
        if chart_path:
            build_chart(study, chart_path)

        ai_summary = None
        if args.ai_summary:
            log("4. Generating LLM summary...")
            try:
                ai_summary = generate_insights_with_gemini(study)
            except Exception as exc:
                log(f"   LLM summary skipped: {exc}")

        result = brief(study) if args.brief else compact(study)
        if args.output:
            log("5. Building PDF...")
            try:
                build_pdf_report(study, args.output, chart_path, ai_summary)
                result["report_path"] = str(Path(args.output).resolve())
            except Exception as exc:
                # rendering must never destroy a finished analysis
                result["report_error"] = f"{type(exc).__name__}: {exc}"
                log(f"   PDF failed: {result['report_error']} "
                    "(the analysis below is still valid)")
            if not args.chart and chart_path and Path(chart_path).exists():
                Path(chart_path).unlink()      # keep the folder clean unless asked
                chart_path = None
        if chart_path:
            result["chart_path"] = str(Path(chart_path).resolve())
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(study, ensure_ascii=False,
                                                      indent=2, default=str), encoding="utf-8")
            result["study_path"] = str(Path(args.json_out).resolve())
        if ai_summary:
            result["ai_summary"] = ai_summary

        result["tool_version"] = __version__
        result["ok"] = True
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0

    except (OfflineMiss, ValueError, RuntimeError, NotFound) as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                          "hint": "check --start/--end, the article title "
                                  "(--resolve-only) or your network"},
                         ensure_ascii=False, indent=2))
        return 1
    except Exception as exc:
        # Never dump a raw traceback at an agent, but never hide *where* it broke
        # either: the last frames are what makes a bug report actionable.
        import traceback
        frames = traceback.extract_tb(exc.__traceback__)[-3:]
        where = [f"{Path(f.filename).name}:{f.lineno} in {f.name}: {f.line}" for f in frames]
        if os.getenv("WT_DEBUG") == "1":
            traceback.print_exc(file=sys.stderr)
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                          "where": where,
                          "hint": "unexpected error - please report the 'where' field; "
                                  "set WT_DEBUG=1 for the full traceback"},
                         ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # `wiki_tool.py ... | head` closes stdout early; that is not an error.
        try:
            sys.stdout.close()
        except Exception:
            pass
        os._exit(0)
    except KeyboardInterrupt:
        log("interrupted")
        os._exit(130)
