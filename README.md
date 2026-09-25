# wikiscout - Wikipedia Market Trends

An [Agent Skill](https://agentskills.io/specification) that answers product
questions from Wikipedia pageview data: *is interest in this topic growing, in
which language market, and how much should we trust the answer?*

The analysis lives in the tool, not in prompts. `wiki_tool.py` prints a JSON
verdict with quality flags and an explicit confidence level; the agent picks the
command and explains what the flags mean for the decision.

```
wikiscout/
  SKILL.md                    what the agent reads: recipes, interpretation rules, thresholds
  wiki_tool.py                the whole tool: cache, Wikidata, API, stats, flags, chart, PDF
  test_wiki_tool.py           37 tests, no network
  requirements.txt
  .env.example
  eval/
    agent.py                  a tool-using agent over the CLI (Gemini Flash / Flash-Lite)
    run_eval.py               6 scenarios with mechanical pass/fail checks
    requirements-eval.txt
```

## Install

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
$env:WT_USER_AGENT = "wikiscout/2.4 (you@example.com)"   # Wikimedia asks for a contact
python wiki_tool.py --selftest
```

On Linux or macOS: `source venv/bin/activate` and
`export WT_USER_AGENT="wikiscout/2.4 (you@example.com)"`.

No API key is needed. `GEMINI_API_KEY` is only used by the optional
`--ai-summary` flag and by the evaluation harness.

## Run

```powershell
# by topic - titles resolved and verified via Wikidata in every language
python wiki_tool.py --topic "intermittent fasting" --langs pl,cs --start 24m --output fasting.pdf

# check first which article each language actually maps to
python wiki_tool.py --topic "intermittent fasting" --langs pl,cs --resolve-only

# by explicit pages (the original interface, unchanged)
python wiki_tool.py --queries uk.wikipedia:Астрономія --start 20240101 --end 20241231 --output astronomy_uk.pdf

# a much smaller payload - use this when a small/cheap model drives the tool
python wiki_tool.py --topic "astronomy" --langs uk --start 24m --brief

# which build am I running?
python wiki_tool.py --version
```

Dates accept `YYYYMMDD`, `YYYY-MM`, `YYYY`, or relative forms like `24m` / `3y`.
The current, incomplete month is always dropped - a partial month looks like a
crash on every chart.

### Output contract

stdout is always a single JSON document; progress messages go to stderr. So this
gives a clean file:

```powershell
python wiki_tool.py --topic "astronomy" --langs uk --start 24m > study.json
```

Failures print `{"ok": false, "error": ..., "where": [...], "hint": ...}` with
exit code 1 - never a traceback. `where` names the file, line and code that
broke; `WT_DEBUG=1` adds the full traceback on stderr.

Key fields: `verdict`, `recommendation`, `confidence`, `comparison.ranking`,
`notes`, and per segment `metrics`, `flags`, `caveats`, `summary`. Raw monthly
points are omitted from stdout - use `--json-out` to keep them on disk.

## Verify

```powershell
python wiki_tool.py --selftest          # 8 checks of the analytical core
python -m pytest test_wiki_tool.py -q   # 37 tests, offline, seeded HTTP cache
```

The tests are not "it did not crash": they assert what the analysis *should*
conclude on series with known properties. A flat series must not produce a
trend. An exponential one must recover its exact growth rate. A single huge
month must be flagged as a spike, and the trend must be refitted without it.
Pure seasonality must give ~0% YoY. A search hit belonging to a different
Wikidata item must be rejected instead of analysed. Eight views a day must not
come back as `medium` confidence, however small the p-value.

Every bug found on live data became a test, so it cannot come back.

## Evaluate on a cheap model

The skill is only useful if a small, fast model can drive it correctly. `eval/`
turns the model probe into a tool-using agent: it gets `SKILL.md` as its system
prompt and one tool that runs this CLI - nothing else. Nothing about Wikipedia
or the methodology lives in the agent code, so when a scenario fails, the fix
belongs in `SKILL.md` or in the CLI's error messages, not in the test.

```powershell
pip install -r eval\requirements-eval.txt
# put GEMINI_API_KEY=... in .env
python eval\run_eval.py                 # 6 scenarios
python eval\run_eval.py --only 2        # just one, to save quota
python eval\agent.py "your question"    # a single ad-hoc run
```

Scoring is mechanical - no LLM judges the answer:

* `numbers_grounded` - every percentage in the answer must appear in the CLI
  output the model actually received (2% tolerance). This catches a confident
  hallucination.
* `confidence_stated` - the answer names the confidence level or its reason.
* `missing_pl_explained` - when a language has no article, the model must say so
  and must **not** report it as "no demand".
* `pdf_created`, `tool_budget`, `both/three_languages`, `reports_the_problem`,
  `flags_ambiguity`.

Transcripts and every command the model ran land in `eval/results/`.

## What changed from the first version, and why

| Before | Now |
|---|---|
| Gemini turned `total / average / max` into conclusions | The tool computes the analysis and prints JSON; the agent writes the answer. `--ai-summary` keeps the old paragraph for standalone runs, constrained to the numbers it is given |
| Article titles guessed per language; a Polish search for "intermittent fasting" silently analysed "Stres oksydacyjny" (oxidative stress) | Titles come from Wikidata sitelinks; anything else must map back to the same Wikidata item or it is rejected and listed in `rejected`, never analysed |
| Wikidata search returned journals and clinical trials as the topic | Publications (P31: journal, paper, book, film…) are never chosen as the topic and never listed as rival concepts |
| Raw view counts compared across editions | Normalised to views per million wiki pageviews, using the `aggregate` endpoint as denominator; `baseline` shows raw vs normalised side by side |
| No trend at all, despite SKILL.md claiming one | Log-linear trend with a two-sided p-value; insignificant results print as "no trend", not as a percentage |
| "Peak" was just `max(views)` | MAD-based outlier detection, `spike_dominated`, level-shift detection, a trend refit without outliers, and seasonality computed with outlier months removed |
| Assumptions appeared only if the LLM felt like it | `confidence` (high/medium/low/none) plus a limitations block generated from the flags that actually fired |
| Every run re-downloaded everything | sqlite cache keyed by URL; finished months cached forever, the current month for 12h |
| Cyrillic transliterated to "Astronomiya" | Unicode TTF font in the PDF; transliteration only if no font is found |
| `temp_chart.png` left in the working directory | Chart written next to the PDF and removed unless `--chart` is given |
| Crashes printed one line with no location | `where` field with file, line and code; `--version` with an md5 fingerprint |
| No tests, no requirements.txt | 37 tests + 8 selftest checks + 6 agent scenarios, pinned dependencies |

## What pageviews cannot tell you

Attention is not willingness to pay. A language edition is not a country -
Spanish spans two continents, Ukrainian and Russian editions have large diaspora
readership, English is the global default. Wikipedia readers skew to desktop,
study and research use. Falling views may mean attention moved to YouTube, an
app or an AI assistant rather than that interest died, which is why normalised
share is the safer metric.

Correct framing: this ranks hypotheses for cheap validation - a landing page, a
search-ads test, a pilot lesson. It never decides on its own.

## Methodology and roadmap

See `SKILL.md` for exact thresholds, the confidence rules, and the planned next
steps: article baskets instead of single pages, bulk `pageviews-ez` dumps for
large studies, saved studies with `diff`, cross-source validation, device
segmentation, forecasting with intervals, and monitoring.
