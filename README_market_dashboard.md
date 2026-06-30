# Companies House Market Research Dashboard

## Run locally

1. Put these files in the same folder:
   - `companies_house_market_server.py`
   - `business_market_research_dashboard.html`
   - `requirements_market_dashboard.txt`

2. Install requirements:

```bash
python -m pip install -r requirements_market_dashboard.txt
```

3. Start the server:

```bash
python companies_house_market_server.py
```

4. Open:

```text
http://127.0.0.1:5000
```

The first run downloads the Companies House Basic Company Data ZIP, extracts the CSV, and loads it into DuckDB. Later runs reuse `companies.duckdb`.

## What is new

- Market Radar tab: full-market KPIs, eureka cards, status mix, trend pulse.
- Heat Map tab: aggregated postcode-district density using the full filtered result count.
- Time Trend tab: incorporation trend by year plus positive/negative development tables.
- Opportunity Finder tab: ranks towns and sectors by recent formation, active share, risk share and saturation.
- Companies tab: paginated company browser behind the insights.
- `/api/insights`: full-population analytics endpoint.
- `/api/export.csv`: export filtered rows.

## Important limitation

Companies House Basic Company Data is a monthly snapshot of live companies. It is useful for market sizing, incorporation momentum, status/risk mix and geographic density, but it does not contain revenue, trading activity, website traffic, customer demand or dissolved companies.
