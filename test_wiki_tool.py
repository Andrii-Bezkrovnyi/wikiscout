"""Tests for wiki_tool.py.

Run with:  python -m pytest test_wiki_tool.py -q

No network: every test seeds the sqlite HTTP cache with synthetic API answers
under the exact URLs the tool builds, then runs the tool in offline mode. That
also means these tests fail loudly if the URL format ever changes by accident.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import tempfile
import urllib.parse
from pathlib import Path

import pytest

# must be set before importing the module (it reads them at import time)
_TMP = Path(tempfile.mkdtemp(prefix="wikiscout-test-"))
os.environ["WT_CACHE_PATH"] = str(_TMP / "cache.db")
os.environ["WT_OFFLINE"] = "1"

import wiki_tool as wt  # noqa: E402


# --------------------------------------------------------------------------
# helpers: put synthetic API answers into the cache
# --------------------------------------------------------------------------
def months(start: dt.date, count: int):
    y, m = start.year, start.month
    for _ in range(count):
        yield dt.date(y, m, 1)
        m += 1
        if m == 13:
            y, m = y + 1, 1


def seed_article(project: str, article: str, start: dt.date, count: int, values):
    items = [{"project": project, "article": article.replace(" ", "_"),
              "granularity": "monthly", "timestamp": d.strftime("%Y%m%d") + "00",
              "access": "all-access", "agent": "user", "views": int(v)}
             for d, v in zip(months(start, count), values)]
    end = list(months(start, count))[-1]
    end = dt.date(end.year + (end.month == 12), (end.month % 12) + 1, 1) - dt.timedelta(days=1)
    safe = urllib.parse.quote(article.replace(" ", "_"), safe="")
    url = (f"{wt.REST}/per-article/{project}/all-access/user/{safe}/monthly/"
           f"{start.strftime('%Y%m%d')}/{end.strftime('%Y%m%d')}")
    wt.cache_put(url, 200, {"items": items})
    return start, end


def seed_missing_article(project: str, article: str, start: dt.date, end: dt.date):
    safe = urllib.parse.quote(article.replace(" ", "_"), safe="")
    url = (f"{wt.REST}/per-article/{project}/all-access/user/{safe}/monthly/"
           f"{start.strftime('%Y%m%d')}/{end.strftime('%Y%m%d')}")
    wt.cache_put(url, 404, {"error": "not_found"})


def seed_totals(project: str, start: dt.date, end: dt.date, count: int, total=500_000_000):
    items = [{"project": project, "access": "all-access", "agent": "user",
              "granularity": "monthly", "timestamp": d.strftime("%Y%m%d") + "00",
              "views": int(total)} for d in months(start, count)]
    url = (f"{wt.REST}/aggregate/{project}/all-access/user/monthly/"
           f"{start.strftime('%Y%m%d')}/{end.strftime('%Y%m%d')}")
    wt.cache_put(url, 200, {"items": items})


def seed_wikidata(topic: str, qid: str, sitelinks: dict, langs=("pl", "cs"),
                  extra_candidates=(), instance_of=None):
    """Seed the three Wikidata calls the resolver makes: search, sitelinks, labels."""
    search = {"search": [{"id": qid, "label": topic, "description": "synthetic test entity"},
                         *extra_candidates]}
    wt.cache_put(wt._wd_url({
        "action": "wbsearchentities", "search": topic, "language": "en", "uselang": "en",
        "type": "item", "limit": "5", "format": "json"}), 200, search)

    def _claims(type_qid):
        if not type_qid:
            return {}
        return {"P31": [{"mainsnak": {"datavalue": {"value": {"id": type_qid}}}}]}

    ids = [qid] + [c["id"] for c in extra_candidates]
    entities = {qid: {"sitelinks": {f"{l}wiki": {"site": f"{l}wiki", "title": t}
                                    for l, t in sitelinks.items()},
                      "claims": _claims(instance_of)}}
    for c in extra_candidates:
        entities[c["id"]] = {"sitelinks": c.get("sitelinks", {}),
                             "claims": _claims(c.get("instance_of"))}
    # batch lookup over all candidates, and the single-id lookup
    wt.cache_put(wt._wd_url({"action": "wbgetentities", "ids": "|".join(ids),
                             "props": "sitelinks|claims", "format": "json"}), 200,
                 {"entities": entities})
    wt.cache_put(wt._wd_url({"action": "wbgetentities", "ids": qid,
                             "props": "sitelinks", "format": "json"}), 200,
                 {"entities": {qid: entities[qid]}})

    languages = list(dict.fromkeys(list(langs) + ["en"]))
    wt.cache_put(wt._wd_url({"action": "wbgetentities", "ids": qid,
                             "props": "labels|descriptions", "format": "json",
                             "languages": "|".join(languages)}), 200,
                 {"entities": {qid: {
                     "labels": {l: {"language": l, "value": topic} for l in languages},
                     "descriptions": {"en": {"language": "en", "value": "synthetic entity"}}}}})


def seed_wiki_search(lang: str, query: str, hits, limit: int = 3):
    url = f"https://{lang}.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "list": "search", "srsearch": query,
        "srlimit": str(limit), "format": "json"})
    wt.cache_put(url, 200, {"query": {"search": [{"title": h} for h in hits]}})


def seed_page_qid(lang: str, title: str, qid):
    url = f"https://{lang}.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "prop": "pageprops", "ppprop": "wikibase_item",
        "titles": title, "redirects": "1", "format": "json"})
    props = {"pageprops": {"wikibase_item": qid}} if qid else {}
    wt.cache_put(url, 200, {"query": {"pages": {"1": {"title": title, **props}}}})


def series(values, normalized=True, start=(2023, 1)):
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


# --------------------------------------------------------------------------
# dates and the original CLI interface
# --------------------------------------------------------------------------
def test_original_yyyymmdd_format_still_parses():
    assert wt.parse_date("20240101") == dt.date(2024, 1, 1)
    assert wt.parse_date("20241231", is_end=True) == dt.date(2024, 12, 31)


def test_relative_and_iso_periods():
    assert wt.parse_date("2023-05") == dt.date(2023, 5, 1)
    assert wt.parse_date("2023-05", is_end=True) == dt.date(2023, 5, 31)
    start, _ = wt.resolve_period("24m", "latest", "monthly")
    assert start.day == 1


def test_partial_current_month_is_never_included():
    _, end = wt.resolve_period("24m", "latest", "monthly")
    assert end < dt.date.today().replace(day=1)


def test_period_clamped_to_pageviews_data_start():
    start, _ = wt.resolve_period("2010-01", "2016-01", "monthly")
    assert start >= wt.DATA_START


def test_queries_interface_is_backwards_compatible():
    assert wt.parse_queries(["pl.wikipedia:Post_przerywany"]) == [
        ("pl.wikipedia", "Post przerywany")]
    assert wt.parse_queries(["uk:Астрономія"]) == [("uk.wikipedia", "Астрономія")]
    with pytest.raises(ValueError):
        wt.parse_queries(["broken"])


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------
def test_pvalue_matches_the_t_table():
    assert wt.t_pvalue(2.086, 20) == pytest.approx(0.05, abs=0.005)
    assert wt.t_pvalue(0.0, 20) == pytest.approx(1.0)
    assert wt.t_pvalue(10.0, 30) < 1e-8


def test_trend_recovers_a_known_growth_rate():
    t = wt.loglinear_trend([1000 * math.exp(0.02 * i) for i in range(36)])
    assert t["growth_per_period"] == pytest.approx(math.exp(0.02) - 1, rel=1e-6)
    assert t["r2"] > 0.999 and t["significant"]


def test_noise_is_not_reported_as_a_trend():
    assert not wt.loglinear_trend([1000 + (i % 5) * 7 for i in range(36)])["significant"]


def test_mad_finds_the_outlier():
    z = wt.mad_zscores([10] * 20 + [500])
    assert z[-1] > 5 and max(z[:-1]) < 3


# --------------------------------------------------------------------------
# quality flags - the point of the rewrite
# --------------------------------------------------------------------------
def test_low_volume_flag_for_tiny_numbers():
    a = wt.analyze_series(series([300] * 30))
    assert "low_volume" in a["flags"] and a["confidence"] != "high"


def test_spike_is_not_growth():
    a = wt.analyze_series(series([20_000] * 12 + [900_000] + [20_000] * 11))
    assert "spike_dominated" in a["flags"] and a["confidence"] != "high"


def test_level_shift_detected():
    assert "level_shift" in wt.analyze_series(series([20_000] * 12 + [300_000] * 12))["flags"]


def test_short_history_flag():
    assert "short_history" in wt.analyze_series(series([50_000] * 10))["flags"]


def test_clean_growth_is_high_confidence():
    a = wt.analyze_series(series([40_000 * math.exp(0.03 * i) for i in range(36)]))
    assert a["metrics"]["significant"] and a["flags"] == []
    assert a["confidence"] in ("high", "medium")


def test_unnormalised_series_is_downgraded():
    a = wt.analyze_series(series([40_000 * math.exp(0.03 * i) for i in range(36)],
                                 normalized=False))
    assert a["confidence"] != "high"
    assert any("normalis" in c for c in a["caveats"])


def test_whole_wiki_growth_is_not_topic_growth():
    pts, (y, m) = [], (2023, 1)
    for i in range(24):
        total, views = int(5e8 * math.exp(0.03 * i)), int(5e4 * math.exp(0.03 * i))
        pts.append({"date": f"{y:04d}-{m:02d}-01", "views": views,
                    "project_views": total, "share_ppm": views / total * 1e6})
        m += 1
        if m == 13:
            y, m = y + 1, 1
    a = wt.analyze_series({"lang": "xx", "project": "xx.wikipedia", "article": "T",
                           "granularity": "monthly", "normalized": True, "points": pts})
    assert "tracks_project_traffic" in a["flags"]
    assert abs(a["metrics"]["total_change_pct"]) < 5


def test_seasonality_does_not_become_a_fake_trend():
    values = [10_000 * (1 + 0.5 * math.sin(2 * math.pi * i / 12)) for i in range(24)]
    a = wt.analyze_series(series(values))
    assert abs(a["metrics"]["yoy"]["change_pct"]) < 2.0


def test_missing_data_is_explained_not_silently_dropped():
    a = wt.analyze_series({"lang": "pl", "project": "pl.wikipedia", "article": "Wrong title",
                           "granularity": "monthly", "normalized": False, "points": []})
    assert a["status"] == "no_data" and a["confidence"] == "none"
    assert "redirect" in a["summary"] or "title" in a["summary"]


# --------------------------------------------------------------------------
# end to end
# --------------------------------------------------------------------------
def test_end_to_end_topic_pdf_and_json(capsys, tmp_path):
    start = dt.date(2023, 1, 1)
    seed_wikidata("intermittent fasting", "Q_TEST_1",
                  {"pl": "Post przerywany", "cs": "Přerušovaný půst"}, langs=("pl", "cs"))
    s, e = seed_article("pl.wikipedia", "Post przerywany", start, 24,
                        [20_000 * math.exp(0.03 * i) for i in range(24)])
    seed_totals("pl.wikipedia", s, e, 24)
    seed_article("cs.wikipedia", "Přerušovaný půst", start, 24, [6_000] * 24)
    seed_totals("cs.wikipedia", s, e, 24, total=120_000_000)

    pdf = tmp_path / "out.pdf"
    code = wt.main(["--topic", "intermittent fasting", "--langs", "pl,cs",
                    "--start", "20230101", "--end", "20241231",
                    "--output", str(pdf), "--json-out", str(tmp_path / "study.json")])
    assert code == 0
    result = json.loads(capsys.readouterr().out)

    assert result["ok"] is True
    assert result["comparison"]["leader"] == "pl"        # designed answer
    pl = next(s for s in result["segments"] if s["lang"] == "pl")
    cs = next(s for s in result["segments"] if s["lang"] == "cs")
    assert pl["metrics"]["significant"] and pl["confidence"] in ("high", "medium")
    assert not cs["metrics"]["significant"]              # flat series
    # normalisation actually happened: cs has a smaller wiki, so its share is larger
    assert cs["metrics"]["audience_size_ppm"] > pl["metrics"]["audience_size_ppm"] / 2
    assert pdf.exists() and pdf.stat().st_size > 10_000
    assert (tmp_path / "study.json").exists()
    assert "points" not in pl                            # stdout stays compact


def test_wrong_title_is_reported_as_missing_data_not_as_low_demand(capsys, tmp_path):
    """The exact failure of the first version: a guessed Polish title returned
    nothing and the tool implied Poland had no demand."""
    start, end = dt.date(2024, 1, 1), dt.date(2024, 12, 31)
    seed_missing_article("pl.wikipedia", "Post przerywany bad", start, end)
    s, e = seed_article("cs.wikipedia", "Přerušovaný půst", start, 12, [520] * 12)
    seed_totals("cs.wikipedia", s, e, 12, total=120_000_000)

    code = wt.main(["--queries", "pl.wikipedia:Post_przerywany_bad",
                    "cs.wikipedia:Přerušovaný_půst",
                    "--start", "20240101", "--end", "20241231"])
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    pl = next(s for s in result["segments"] if s["lang"] == "pl")
    assert pl["status"] == "no_data"
    assert any("not evidence of low demand" in n for n in result["notes"])
    cs = next(s for s in result["segments"] if s["lang"] == "cs")
    assert "low_volume" in cs["flags"]                   # 17 views/day is noise
    assert cs["confidence"] in ("low", "none")


def test_cache_prevents_a_second_request(tmp_path):
    """Offline mode proves it: the second call can only work from the cache."""
    start = dt.date(2023, 1, 1)
    s, e = seed_article("uk.wikipedia", "Астрономія", start, 24, [9_000] * 24)
    seed_totals("uk.wikipedia", s, e, 24, total=200_000_000)
    first = wt.build_series("uk.wikipedia", "Астрономія", s, e)
    second = wt.build_series("uk.wikipedia", "Астрономія", s, e)
    assert first["points"] == second["points"] and len(first["points"]) == 24


def test_pdf_keeps_cyrillic_when_a_unicode_font_exists(tmp_path):
    from pypdf import PdfReader
    start = dt.date(2023, 1, 1)
    s, e = seed_article("uk.wikipedia", "Астрономія", start, 24,
                        [9_000 * math.exp(0.01 * i) for i in range(24)])
    seed_totals("uk.wikipedia", s, e, 24, total=200_000_000)
    pdf = tmp_path / "uk.pdf"
    code = wt.main(["--queries", "uk.wikipedia:Астрономія", "--start", "20230101",
                    "--end", "20241231", "--output", str(pdf)])
    assert code == 0 and pdf.exists()
    reader = PdfReader(str(pdf))
    assert len(reader.pages) == 1
    text = reader.pages[0].extract_text()
    rep = wt.Report()
    if rep.unicode_ok:
        assert "Астрономія" in text                      # no more "Astronomiya"
    assert "Assumptions and limitations" in text          # mandatory block


def test_errors_are_json_not_tracebacks(capsys):
    code = wt.main(["--start", "20240101", "--end", "20241231"])   # no topic, no queries
    assert code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and "error" in out


def test_wrong_language_hit_is_rejected_by_qid_verification(capsys, tmp_path):
    """The live-run bug: Wikidata has no Polish sitelink for the topic, the
    fallback search returns 'Stres oksydacyjny' (oxidative stress), and the
    tool used to analyse it as if it were the topic."""
    seed_wikidata("intermittent fasting", "Q1666254",
                  {"cs": "Přerušovaný půst"}, langs=("pl", "cs"))     # no plwiki link
    seed_wiki_search("pl", "intermittent fasting", ["Stres oksydacyjny"])
    seed_page_qid("pl", "Stres oksydacyjny", "Q210044")               # a different concept

    start = dt.date(2024, 1, 1)
    s, e = seed_article("cs.wikipedia", "Přerušovaný půst", start, 24, [6_000] * 24)
    seed_totals("cs.wikipedia", s, e, 24, total=120_000_000)

    code = wt.main(["--topic", "intermittent fasting", "--langs", "pl,cs",
                    "--start", "20240101", "--end", "20251231"])
    assert code == 0
    result = json.loads(capsys.readouterr().out)

    pl = next(x for x in result["segments"] if x["lang"] == "pl")
    assert pl["status"] == "no_article"
    assert pl["article"] is None                    # never analysed
    assert any("Stres oksydacyjny" in n for n in result["notes"])
    assert any("Q210044" in n for n in result["notes"])


def test_unverified_title_forces_confidence_to_none():
    series_dict = series([40_000 * math.exp(0.03 * i) for i in range(36)])
    series_dict["title_confidence"] = "low"
    a = wt.analyze_series(series_dict)
    assert a["metrics"]["significant"]               # the maths is still clean
    assert "unverified_title" in a["flags"]          # but the subject is unknown
    assert a["confidence"] == "none"


def test_low_volume_caps_confidence_even_when_the_trend_is_significant():
    """8 views/day with p<0.001 must not come back as 'medium'."""
    a = wt.analyze_series(series([240 * math.exp(0.04 * i) for i in range(30)]))
    assert "low_volume" in a["flags"] and a["metrics"]["significant"]
    assert a["confidence"] in ("low", "none")


def test_entity_with_articles_wins_over_a_clinical_trial(capsys):
    """Wikidata search returns journal papers for medical topics; the resolver
    must pick the concept that actually has Wikipedia articles."""
    seed_wikidata("some diet", "Q_REAL", {"pl": "Dieta", "cs": "Dieta"}, langs=("pl",),
                  extra_candidates=[{"id": "Q_PAPER", "label": "A trial of some diet",
                                     "description": "clinical trial", "sitelinks": {}}])
    res = wt.resolve_topic("some diet", ["pl"])
    assert res["qid"] == "Q_REAL"
    assert res["alternatives"] == []                 # the paper is filtered out
    assert res["resolved"]["pl"]["title_confidence"] == "high"


def test_pdf_is_built_when_one_language_has_no_article(tmp_path, capsys):
    """Regression: a skipped segment carries article=None, which used to crash
    the PDF table with 'NoneType' object is not subscriptable."""
    seed_wikidata("intermittent fasting", "Q1666254",
                  {"cs": "Přerušovaný půst"}, langs=("pl", "cs"))
    seed_wiki_search("pl", "intermittent fasting", ["Stres oksydacyjny"])
    seed_page_qid("pl", "Stres oksydacyjny", "Q898814")
    start = dt.date(2024, 1, 1)
    s, e = seed_article("cs.wikipedia", "Přerušovaný půst", start, 24, [9_000] * 24)
    seed_totals("cs.wikipedia", s, e, 24, total=120_000_000)

    pdf = tmp_path / "mixed.pdf"
    code = wt.main(["--topic", "intermittent fasting", "--langs", "pl,cs",
                    "--start", "20240101", "--end", "20251231", "--output", str(pdf)])
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True and pdf.exists() and pdf.stat().st_size > 10_000

    from pypdf import PdfReader
    text = PdfReader(str(pdf)).pages[0].extract_text()
    assert "no article" in text.lower()          # the gap is visible in the report
    assert len(PdfReader(str(pdf)).pages) == 1


def test_a_failing_pdf_does_not_destroy_the_analysis(tmp_path, capsys, monkeypatch):
    start = dt.date(2023, 1, 1)
    s, e = seed_article("uk.wikipedia", "Астрономія", start, 24, [9_000] * 24)
    seed_totals("uk.wikipedia", s, e, 24, total=200_000_000)
    monkeypatch.setattr(wt, "build_pdf_report",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("font exploded")))
    code = wt.main(["--queries", "uk.wikipedia:Астрономія", "--start", "20230101",
                    "--end", "20241231", "--output", str(tmp_path / "x.pdf")])
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert "font exploded" in result["report_error"]
    assert result["segments"][0]["status"] == "ok"      # the numbers survived


def test_pdf_and_chart_survive_a_skipped_segment(capsys, tmp_path):
    """One language resolves, the other is skipped because no title could be
    verified. The report must still build - a skipped row is 'no data', not a
    crash."""
    seed_wikidata("intermittent fasting", "Q1666254",
                  {"cs": "Přerušovaný půst"}, langs=("pl", "cs"))
    seed_wiki_search("pl", "intermittent fasting", ["Stres oksydacyjny"])
    seed_page_qid("pl", "Stres oksydacyjny", "Q898814")
    start = dt.date(2024, 1, 1)
    s, e = seed_article("cs.wikipedia", "Přerušovaný půst", start, 24, [6_000] * 24)
    seed_totals("cs.wikipedia", s, e, 24, total=120_000_000)

    pdf = tmp_path / "fasting.pdf"
    code = wt.main(["--topic", "intermittent fasting", "--langs", "pl,cs",
                    "--start", "20240101", "--end", "20251231", "--output", str(pdf)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["ok"] is True, out
    assert pdf.exists() and pdf.stat().st_size > 10_000
    pl = next(x for x in out["segments"] if x["lang"] == "pl")
    assert pl["status"] == "no_article"

    from pypdf import PdfReader
    text = PdfReader(str(pdf)).pages[0].extract_text()
    assert "no data" in text.lower()          # the skipped row is shown, not hidden


def test_unexpected_errors_report_where_they_broke(capsys, monkeypatch, tmp_path):
    """The catch-all must not hide the location of a bug."""
    def boom(*a, **kw):
        raise TypeError("'NoneType' object is not subscriptable")
    monkeypatch.setattr(wt, "run_study", boom)
    code = wt.main(["--topic", "x", "--langs", "en", "--start", "20240101"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["ok"] is False
    assert out["where"] and "wiki_tool.py:" in out["where"][-1]


def test_version_flag_reports_a_fingerprint(capsys):
    """So 'which build am I actually running?' is never a guess again."""
    assert wt.main(["--version"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["version"] == wt.__version__
    assert len(out["md5"]) == 32 and out["lines"] > 1000


def test_every_successful_result_is_stamped_with_the_version(capsys, tmp_path):
    start = dt.date(2024, 1, 1)
    s, e = seed_article("uk.wikipedia", "Астрономія", start, 24, [9_000] * 24)
    seed_totals("uk.wikipedia", s, e, 24, total=200_000_000)
    assert wt.main(["--queries", "uk.wikipedia:Астрономія",
                    "--start", "20240101", "--end", "20251231"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tool_version"] == wt.__version__


def test_a_journal_named_after_the_topic_is_not_an_alternative():
    """Live run on 'astronomy' listed 'Astronomy Letters' (a journal) as a rival
    concept and warned about ambiguity for no reason."""
    seed_wikidata("astronomy", "Q333", {"uk": "Астрономія", **{l: f"Astronomy-{l}"
                                                               for l in wt.KNOWN_WIKIS}},
                  langs=("uk",),
                  extra_candidates=[{"id": "Q263028", "label": "Astronomy Letters",
                                     "description": "journal",
                                     "instance_of": "Q5633421",     # scientific journal
                                     "sitelinks": {f"{l}wiki": {"site": f"{l}wiki",
                                                                "title": "Astronomy Letters"}
                                                   for l in wt.KNOWN_WIKIS}}])
    res = wt.resolve_topic("astronomy", ["uk"])
    assert res["qid"] == "Q333"
    assert res["alternatives"] == []
    assert not any("ambiguous" in w for w in res["warnings"])


def test_a_real_rival_concept_is_still_reported():
    """Two comparable concepts must still raise the ambiguity warning."""
    many = {f"{l}wiki": {"site": f"{l}wiki", "title": "Mercury"} for l in wt.KNOWN_WIKIS}
    seed_wikidata("mercury", "Q308", {l: "Mercury" for l in wt.KNOWN_WIKIS}, langs=("en",),
                  extra_candidates=[{"id": "Q925", "label": "mercury",
                                     "description": "chemical element", "sitelinks": many}])
    res = wt.resolve_topic("mercury", ["en"])
    assert [a["qid"] for a in res["alternatives"]] == ["Q925"]
    assert any("ambiguous" in w for w in res["warnings"])


def test_seasonality_ignores_outlier_months():
    """A single spike must not become a 'seasonal swing'."""
    values = [1_000.0] * 24
    values[8] = 9_000.0                       # one September news event
    a = wt.analyze_series(series(values))
    seas = a["metrics"]["seasonality"]
    assert seas["available"] and seas["outliers_excluded"] >= 1
    assert seas["amplitude_pct"] < 20         # flat series stays flat
    assert "indicative only" in seas["note"]  # 2 years of data is not a baseline


def test_a_journal_is_never_chosen_as_the_topic_itself():
    """Even if Wikidata ranks the journal first, the concept must win."""
    all_wikis = {f"{l}wiki": {"site": f"{l}wiki", "title": "Nature"} for l in wt.KNOWN_WIKIS}
    # the search puts the journal first; the concept is the second candidate
    wt.cache_put(wt._wd_url({
        "action": "wbsearchentities", "search": "nature", "language": "en", "uselang": "en",
        "type": "item", "limit": "5", "format": "json"}), 200,
        {"search": [{"id": "Q180445", "label": "Nature", "description": "journal"},
                    {"id": "Q7860", "label": "nature", "description": "the phenomena of the "
                                                                     "physical world"}]})
    wt.cache_put(wt._wd_url({"action": "wbgetentities", "ids": "Q180445|Q7860",
                             "props": "sitelinks|claims", "format": "json"}), 200,
                 {"entities": {
                     "Q180445": {"sitelinks": all_wikis,
                                 "claims": {"P31": [{"mainsnak": {"datavalue": {
                                     "value": {"id": "Q5633421"}}}}]}},
                     "Q7860": {"sitelinks": {f"{l}wiki": {"site": f"{l}wiki", "title": "Nature"}
                                             for l in ("en", "de", "fr", "uk")},
                               "claims": {}}}})
    wt.cache_put(wt._wd_url({"action": "wbgetentities", "ids": "Q7860",
                             "props": "labels|descriptions", "format": "json",
                             "languages": "uk|en"}), 200,
                 {"entities": {"Q7860": {"labels": {"uk": {"value": "Природа"}},
                                         "descriptions": {}}}})

    res = wt.resolve_topic("nature", ["uk"])
    assert res["qid"] == "Q7860"                     # the concept, not the journal
    assert res["resolved"]["uk"]["article"] == "Nature"
