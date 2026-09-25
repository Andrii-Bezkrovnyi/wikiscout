---
name: wikipedia_market_trends
description: Measures and compares topic interest across Wikipedia language editions using the Wikimedia Pageviews API, and turns it into a one-page PDF for product decisions. Use when someone asks whether interest in a topic is growing, which language market to launch or localize into, whether to add a course or content area, or asks to compare topics or languages by Wikipedia demand. Resolves article titles across languages via Wikidata, normalises by total wiki traffic, and grades every result with an explicit confidence level.
license: MIT
---

# Wikipedia Market Trends

Answers one question for B2C founders: **is attention to this topic growing, in
this language, and how much should we trust that?**

`wiki_tool.py` does all the data work and prints a JSON verdict with quality
flags. You pick the command, read the flags, and explain what they mean for the
decision. Do not write your own analysis code and do not call the API directly -
normalisation, significance testing and the confidence rules live in the tool.

## Setup (once)

```bash
pip install -r requirements.txt
export WT_USER_AGENT="wikiscout/2.0 (you@example.com)"   # Wikimedia asks for a contact
python wiki_tool.py --selftest        # 8 offline checks, must print "ok": true
```

No API key is needed. `GEMINI_API_KEY` is only used by the optional
`--ai-summary` flag, which you should leave off - you are the agent, you write
the summary yourself from the JSON.

## Pick the command

| User asks | Run |
|---|---|
| "is interest in X growing in <lang>?" | `--topic X --langs <lang>` |
| "compare X across languages" / "which market first?" | `--topic X --langs a,b,c` |
| "which article does this map to?" | `--topic X --langs a,b --resolve-only` |
| user names exact pages | `--queries pl.wikipedia:Post_przerywany cs.wikipedia:...` |
| "make a report / PDF to share" | add `--output report.pdf` |
| any query at all | add `--brief` - smaller output, same flags and caveats |
| "was that jump real?" | re-run with `--granularity daily --start <month> --end <month>` |
| same topic, new language or period | just run again - the cache makes it fast |

## Always pass --brief

`--brief` prints a much smaller JSON - verdict, ranking, every flag and every
caveat, without the full metric block. It costs roughly a quarter of the tokens
and drops nothing that limits the conclusion. Use the full output only when you
need a specific number such as `baseline` or `seasonality_index`.

## Recipe 1 - one topic, one language

```bash
python wiki_tool.py --topic "astronomy" --langs uk --start 36m --brief \
  --question "Should we add an astronomy course?"
```

Read `verdict`, `confidence`, `segments[].flags`, `segments[].caveats`, `notes`.
Answer in the user's language with the number, the direction **and the confidence
with its reason**. Offer the PDF; build it only if they want one.

## Recipe 2 - compare language markets

```bash
python wiki_tool.py --topic "intermittent fasting" --langs pl,cs,uk --start 24m \
  --output fasting.pdf --question "<the user's original question>"
```

`comparison.ranking` is sorted by confidence first, then growth. Always mention
`audience_size_ppm` next to growth: fast growth on a tiny base is not a market.

## Recipe 3 - the user gives exact article titles

The original interface still works, and skips Wikidata entirely:

```bash
python wiki_tool.py --queries uk.wikipedia:Астрономія --start 20240101 --end 20241231
```

Use it only when the user names the pages. Otherwise prefer `--topic`: a
translated guess is not a title, and a wrong title returns `no_data`, which is
easy to misread as "no demand".

## Interpretation rules - apply these, do not re-derive them

1. **`status: "no_article"` / `"no_data"` is not "no demand".** It means the
   concept has no verified page in that edition, the title is wrong, or the page
   is a redirect. Re-check with `--resolve-only` before saying anything about
   that market.
2. **`unverified_title` invalidates every number in that row.** Confidence is
   forced to `none`; report the article as unresolved, never quote its trend.
3. **`trend_not_significant` beats a big percentage.** +40% with p=0.4 is noise;
   report it as "no measurable trend", never as growth.
4. **`confidence` is not decoration.** `low` or `none` means "do not make a build
   decision on this alone" - say that out loud.
5. **Never quote raw pageviews across languages.** The tool reports `share_ppm`
   (views per million wiki pageviews). Editions differ in size by orders of
   magnitude and overall Wikipedia traffic drifts year over year.
6. **Compare like months.** Use `metrics.yoy.change_pct`, never "last 3 months vs
   the 3 before" - `metrics.seasonality` shows why (diet topics peak in January,
   school topics collapse in July).
7. **`spike_dominated` means a news event, not demand.** Confirm with a daily
   drill-down before reporting it as interest.
8. **`level_shift` means the page was probably renamed or merged.** Before/after
   comparison is unsafe; say so.
9. **`tracks_project_traffic`** means the whole edition moved, not this topic.
10. **Growth is not size.** Rank on growth, sanity-check on `audience_size_ppm`.
11. **Attention is not willingness to pay.** Frame every answer as a shortlist
    for cheap validation, not as a decision.

Every answer states the period, the metric and the main limitation. The PDF
already carries a limitations block generated from the flags that actually fired
- do not remove or soften it.

## Title resolution and why it is strict

`--topic` picks the Wikidata item that actually has Wikipedia articles (raw
search returns clinical trials and journal papers for medical topics), takes the
title from its sitelinks, and for languages without a sitelink searches that
edition using the topic's label **in that language** - then verifies the hit maps
back to the same Wikidata item. A hit that does not is rejected and listed in
`rejected`, never analysed. `--allow-unverified` overrides this and forces
confidence to `none`.

This matters: searching pl.wikipedia for "intermittent fasting" returns
"Stres oksydacyjny" (oxidative stress), which would otherwise produce a clean,
significant, completely meaningless trend.

## When the topic is ambiguous

`--resolve-only` returns `alternatives` with Wikidata descriptions. If the top
entity does not match what the user meant (Mercury the planet vs the element),
ask them, or re-run with `--qid Q308`. Silently analysing the wrong concept is
the main way to produce a confident wrong answer.

## Output contract

stdout is always one JSON document; progress goes to stderr. Failures print
`{"ok": false, "error": ..., "hint": ...}` with exit code 1, never a traceback.

Key fields: `verdict`, `recommendation`, `confidence`, `comparison.ranking`,
`notes`, and per segment: `metrics` (total_change_pct, p_value, significant, yoy,
seasonality, audience_size_ppm, baseline, anomalies), `flags`, `caveats`,
`summary`. Raw monthly points are omitted from stdout; use `--json-out` if you
need them on disk.

## Methodology (thresholds)

* Trend: OLS on log(views), two-sided t-test on the slope, `significant` = p<0.05.
* Normalisation: `share_ppm = article views / edition views x 1e6`.
* YoY: last 12 periods vs the previous 12, same calendar months.
* Spikes: modified z-score (MAD) >= 3.5; `spike_dominated` when one period holds
  more than 25% of all attention in the window.
* Seasonality is computed with outlier months removed, and reports
  `years_per_month`; with fewer than 3 years it is indicative only, not a
  baseline you can subtract.
* Ambiguity: items that are publications (journals, papers, books, films -
  Wikidata P31) are never chosen as the topic and never reported as rival
  concepts, however many editions they have. Among real concepts, an
  alternative is reported only if it has at least 30% as many Wikipedia
  editions as the chosen one.
* Level shift: rolling 6-period medians differing by 4x or more.
* Volume floor: median < 50 views/day -> `low_volume`, which caps confidence at
  `low` regardless of p-value (below that, month-to-month moves are mostly
  Poisson noise, and a significant slope on 8 views/day still decides nothing).
* Title verification: a non-sitelink title must map back to the same Wikidata
  item, otherwise the segment is skipped (`unverified_title` -> confidence
  `none` when forced with `--allow-unverified`).
* History floor: fewer than 18 monthly points -> `short_history`.
* Confidence starts at `high` and drops one level per problem found.
* `agent=user` always (bots excluded); the current incomplete month is dropped;
  data exists only from 2015-07.

Tune with `WT_MIN_DAILY_VIEWS`, `WT_MIN_POINTS`, `WT_SPIKE_MAD_Z`,
`WT_LEVEL_SHIFT_RATIO`. After changing a threshold re-run `--selftest` and
`pytest test_wiki_tool.py`.

## Do not

* Do not report a number the tool did not print.
* Do not use `--ai-summary` when you are the agent - you already have the facts.
* Do not treat a missing article as evidence of low demand.
* Do not drop the limitations block from the PDF.

## Roadmap - how to grow this further

1. **Article baskets instead of single pages.** A theme is a cluster: traverse
   Wikidata subclasses or category trees and analyse the aggregate - far more
   robust than one page that may be renamed or split.
2. **Bulk data.** For dozens of topics, switch from the per-article API to the
   monthly `pageviews-ez` dumps into DuckDB/Parquet; the CLI contract stays the
   same, only the fetch layer changes.
3. **Saved studies.** `--json-out` already produces reusable studies; add `list`
   and `diff` so a founder can re-run last quarter's shortlist and see what moved.
4. **Cross-source validation.** Same interface, more providers: Google Trends,
   app-store rank history, keyword volume. Pageviews become one vote of several.
5. **Segmentation.** `--access mobile-web` per language reveals mobile-first
   markets; add device and referrer splits to the study schema.
6. **Forecasting.** Seasonal-naive or ETS baseline with prediction intervals and
   honest backtesting on held-out months.
7. **Monitoring.** Scheduled runs that alert when a watched topic's normalised
   share breaks out of its seasonal band.
