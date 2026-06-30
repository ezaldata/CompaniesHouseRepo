# Market Research Dashboard Audit Report

## Files reviewed

- `companies_house_market_server.py`
- `business_market_research_dashboard.html`
- `requirements_market_dashboard.txt`
- sample CSV supplied by user: 99 rows, 55 columns

## What was tested

1. Python syntax compilation with `py_compile`.
2. CSV loading into DuckDB using the supplied sample CSV.
3. Flask test-client requests against:
   - `/api/search?limit=3`
   - `/api/insights?heatmap_limit=50&min_group=1&ranking_min_count=5`
   - `/api/export.csv?limit=3`
4. Independent recalculation from the raw CSV using pandas for:
   - total filtered rows
   - status category counts
   - unclassified sector count
   - missing town count
   - incorporation trend by year
   - town/sector active counts, risk counts, recent counts, growth and opportunity score
   - CSV export row shape

## Sample CSV validation result

The core arithmetic matched the sample dataset:

- Total rows: 99
- Status categories:
  - Active: 87
  - Active - At Risk: 7
  - Liquidation: 5
- Data quality:
  - Missing/unknown town: 13
  - Unclassified sector: 24
- The app loaded the supplied CSV successfully. The raw CSV header contains some leading spaces, but DuckDB normalized those headers during loading in the tested environment.

## Issues found in the original version

### 1. Partial current-year comparison could mislead the trend signal

The original `/api/insights` used the maximum incorporation year in the filtered dataset as the end of the comparison window. With the supplied sample, that is 2026. Since the audit date is 2026-06-28, 2026 is only year-to-date, not a full calendar year. Comparing 2026 against 2025 can make formation momentum look falsely weak.

Fix applied: annual opportunity comparisons now use the latest complete calendar year. The trend still shows the current-year bar, but the response flags it as YTD with `comparison_window.latest_year_in_trend_is_ytd`.

### 2. Opportunity/risk rankings were not truly full-population rankings

The original town and sector SQL grouped the full filtered dataset but then applied `ORDER BY count DESC LIMIT 80` before opportunity/risk rankings were computed. That means a smaller but fast-growing or risky town outside the top 80 by count could never appear in the Opportunity Finder.

Fix applied: rankings are now computed from all eligible town/sector groups. The API still returns a display list capped to 80 for browsing.

### 3. Heatmap risk/recent modes could omit important low-density pockets

The original heatmap selected top postcode districts only by total count. When the user switched to “Stress / risk” or “Recent formation,” smaller postcode districts with high risk or recent formation could be absent from the plotted data.

Fix applied: heatmap cell selection now includes a mix of top density, top risk and top recent-formation cells before plotting.

### 4. CSV export cap was not transparent enough

The export endpoint was capped, but a downloaded CSV did not tell the user whether it was complete or truncated.

Fix applied: export responses now include:

- `X-Total-Matching-Rows`
- `X-Export-Limit`
- `X-Export-Truncated`

The HTML export link also labels the export as “first 50k.”

### 5. Demo-mode opportunity score did not match live-mode score

The JavaScript demo data used a different growth-bonus formula from the Python API. This only affected demo mode, not live API mode, but it could create inconsistent dashboard behavior.

Fix applied: demo-mode score formula now matches the server formula.

### 6. Minor robustness issues

- Tie ordering for town/sector group rows was not deterministic.
- Whitespace-only post towns were counted as unknown in data quality but could still appear in grouping.

Fix applied: stable secondary ordering and trimmed uppercase town grouping.

### 7. UI wording made unsupported demand inferences

Two pieces of copy could overstate what Companies House registry data can prove: formation counts were framed as demand momentum, and stress ratios referenced declining demand as a possible cause.

Fix applied: UI and eureka-card wording now describes measured registry signals only: incorporation/registration activity and insolvency-related status. Demand, revenue and trading performance are explicitly not claimed.

## Important interpretation caveat

Companies House Basic Company Data is a registry snapshot. It is suitable for basic market sizing, status mix, SIC clustering, registered-office geography and incorporation trend proxies. It is not revenue data, customer demand data, profitability data, trading-location data, website/review data or proof that a company is actively trading.

## Patched package

The patched package contains the corrected server and HTML under the original filenames, so it can replace the existing version directly.
