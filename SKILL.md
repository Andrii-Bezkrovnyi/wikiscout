---
name: wikipedia_market_trends
description: Analyzes Wikipedia pageviews across languages and topics to validate B2C product demand, trends, and market localization feasibility. Generates a 1-page PDF report.
---

# Wikipedia Market Trends Skill

## Overview
This skill queries the Wikimedia Analytics API, calculates traffic momentum (daily average, peak spikes, growth trend), and generates a one-page PDF report with visualizations and strategic insights.

## When to Use
- Validating market demand for a new course, topic, or feature.
- Comparing interest across different language segments (e.g., `uk.wikipedia` vs `pl.wikipedia`).
- Generating shareable one-page PDF reports for product stakeholders.

## Iterative Development (Future Roadmap)
To handle more complex research and larger datasets, this skill can be iteratively upgraded:
1. **Automated Translation & Entity Resolution:** Integrate the Wikidata API so the agent only needs to provide the English term (e.g., "Intermittent fasting"), and the skill automatically finds the correct localized article titles in Polish, Czech, etc.
2. **Local Caching:** Implement SQLite or a local `.json` cache for Wikimedia API responses. This prevents redundant network calls and rate-limiting when the agent runs multiple iterative queries on the same dates.
3. **Multi-Metric Dashboards:** Expand the PDF generator to include YoY (Year-over-Year) growth tables and pie charts representing market share among the selected languages, handling months of data via Pandas dataframes for faster aggregation.

## Tool Interface
Run the Python script directly or invoke via CLI:
```bash
python wiki_tool.py --queries uk.wikipedia:Python pl.wikipedia:Python --start 20250101 --end 20250601 --output report.pdf
```
