"""
Companies House Market Research Server
--------------------------------------

This is a stronger version of your original search server. It keeps the simple
Flask + DuckDB architecture, but adds market-research endpoints that calculate
insights across the FULL matching result set rather than only the first 200 rows.

New capability:
- /api/search: paginated company list for browsing.
- /api/insights: full-market analytics for heatmap, trend, town/sector winners,
  risk pockets and "aha/eureka" cards.
- /api/export.csv: export the currently filtered results.

Important data reality:
Companies House Basic Company Data is a monthly snapshot of live companies on the
register. It is excellent for market sizing, sector density, incorporation trend,
status/risk mix and geographic concentration. It is not a complete trading-status
or revenue dataset, and dissolved companies are not present in the basic snapshot.

Fact-check fixes applied:
1. "None Supplied" (the literal text Companies House uses when no SIC was filed)
   and missing RegAddress.PostTown ("UNKNOWN") were leaking into sector/town
   rankings and eureka cards as if they were real markets. Both are now excluded
   from town/sector groupings entirely; their counts are still reported,
   transparently, under "data_quality" in /api/insights.
2. Risk/opportunity rankings and eureka cards had no minimum sample size, so a
   town with 1-2 companies could show a 100% risk rate with the same visual
   weight as a real pattern from 20+ companies. A separate ranking_min_count
   (default 5, query-param-adjustable) now gates what's eligible to be ranked
   or surfaced as a eureka card, while the full unfiltered breakdown (gated
   only by the lower min_group) stays available for browsing.
"""

from __future__ import annotations

import csv
import io
import math
import os
import time
import zipfile
from datetime import date
from typing import Any

import duckdb
import requests
from flask import Flask, Response, jsonify, request, send_from_directory

ZIP_FILE = "BasicCompanyData.zip"
CSV_FILE = "BasicCompanyData.csv"
DB_FILE = "companies.duckdb"
HTML_FILE = "business_market_research_dashboard.html"
URL_TEMPLATE = "https://download.companieshouse.gov.uk/BasicCompanyDataAsOneFile-{}-01.zip"

# ---------------------------------------------------------------------------
# Status taxonomy. These are grouped for market interpretation, not legal advice.
# ---------------------------------------------------------------------------
STATUS_CATEGORIES: dict[str, list[str]] = {
    "Active": ["Active"],
    "Active - At Risk": [
        "Active - Proposal to Strike off",
        "Live but Receiver Manager on at least one charge",
    ],
    "Insolvency - Voluntary Arrangement": [
        "Voluntary Arrangement",
        "VOLUNTARY ARRANGEMENT / ADMINISTRATIVE RECEIVER",
        "VOLUNTARY ARRANGEMENT / RECEIVER MANAGER",
    ],
    "Insolvency - Administration/Receivership": [
        "In Administration",
        "In Administration/Administrative Receiver",
        "In Administration/Receiver Manager",
        "ADMINISTRATION ORDER",
        "ADMINISTRATIVE RECEIVER",
        "RECEIVER MANAGER / ADMINISTRATIVE RECEIVER",
        "RECEIVERSHIP",
    ],
    "Liquidation": ["Liquidation"],
}

_STATUS_TO_CATEGORY = {raw: cat for cat, raws in STATUS_CATEGORIES.items() for raw in raws}
NEGATIVE_CATEGORIES = {
    "Active - At Risk",
    "Insolvency - Voluntary Arrangement",
    "Insolvency - Administration/Receivership",
    "Liquidation",
}
NEGATIVE_RAW_LABELS = [raw for cat in NEGATIVE_CATEGORIES for raw in STATUS_CATEGORIES.get(cat, [])]
ACTIVE_RAW_LABELS = STATUS_CATEGORIES["Active"]

# DuckDB expressions used in several queries. They avoid hard date parsing
# assumptions because Companies House CSV snapshots have historically appeared
# in slightly different date formats across exports/tools.
INC_YEAR_EXPR = """
CASE
  WHEN regexp_matches(coalesce("IncorporationDate", ''), '^[0-9]{4}-[0-9]{2}-[0-9]{2}')
    THEN try_cast(substr("IncorporationDate", 1, 4) AS INTEGER)
  WHEN regexp_matches(coalesce("IncorporationDate", ''), '^[0-9]{2}/[0-9]{2}/[0-9]{4}')
    THEN try_cast(substr("IncorporationDate", 7, 4) AS INTEGER)
  ELSE try_cast(substr(coalesce("IncorporationDate", ''), 1, 4) AS INTEGER)
END
"""

PRIMARY_SIC_EXPR = """
CASE
  WHEN upper(trim(coalesce("SICCode.SicText_1", ''))) NOT IN ('', 'NONE SUPPLIED') THEN trim("SICCode.SicText_1")
  WHEN upper(trim(coalesce("SICCode.SicText_2", ''))) NOT IN ('', 'NONE SUPPLIED') THEN trim("SICCode.SicText_2")
  WHEN upper(trim(coalesce("SICCode.SicText_3", ''))) NOT IN ('', 'NONE SUPPLIED') THEN trim("SICCode.SicText_3")
  WHEN upper(trim(coalesce("SICCode.SicText_4", ''))) NOT IN ('', 'NONE SUPPLIED') THEN trim("SICCode.SicText_4")
  ELSE 'Unclassified'
END
"""

OUTCODE_EXPR = """
CASE
  WHEN nullif(trim(coalesce("RegAddress.PostCode", '')), '') IS NULL THEN 'UNKNOWN'
  WHEN strpos(trim("RegAddress.PostCode"), ' ') > 0
    THEN upper(split_part(trim("RegAddress.PostCode"), ' ', 1))
  WHEN length(trim("RegAddress.PostCode")) > 3
    THEN upper(left(trim("RegAddress.PostCode"), length(trim("RegAddress.PostCode")) - 3))
  ELSE upper(trim("RegAddress.PostCode"))
END
"""


def categorize_status(raw_status: str | None) -> str:
    return _STATUS_TO_CATEGORY.get(raw_status or "", "Other")


def _months_ago(d: date, months_back: int) -> date:
    month_index = d.month - 1 - months_back
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def find_latest_zip_url(max_months_back: int = 6) -> str:
    """Walk backwards from the current month until a Companies House snapshot resolves."""
    today = date.today()
    for i in range(max_months_back):
        candidate_month = _months_ago(today, i)
        url = URL_TEMPLATE.format(candidate_month.strftime("%Y-%m"))
        resp = requests.head(url, timeout=15, allow_redirects=True)
        if resp.status_code == 200:
            return url
    raise RuntimeError(
        f"Could not find a valid snapshot in the last {max_months_back} months. "
        "Check https://download.companieshouse.gov.uk/ for the current filename."
    )


def download_zip() -> None:
    url = find_latest_zip_url()
    print(f"Downloading {url} ...")
    with requests.get(url, stream=True, timeout=90) as r:
        r.raise_for_status()
        with open(ZIP_FILE, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)
    print("Download complete.")


def extract_zip() -> None:
    print("Extracting ZIP...")
    with zipfile.ZipFile(ZIP_FILE, "r") as z:
        for name in z.namelist():
            if name.lower().endswith(".csv"):
                with z.open(name) as src, open(CSV_FILE, "wb") as dst:
                    dst.write(src.read())
                print(f"Extracted: {CSV_FILE}")
                return
    raise RuntimeError("No CSV found inside the downloaded ZIP.")


def load_duckdb() -> None:
    print("Loading CSV into DuckDB. The full file can take a minute or two...")
    con = duckdb.connect(DB_FILE)
    con.execute(f"""
        CREATE OR REPLACE TABLE companies AS
        SELECT * FROM read_csv_auto('{CSV_FILE}', ALL_VARCHAR=TRUE, SAMPLE_SIZE=-1);
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS postcode_geocache (
            postcode VARCHAR PRIMARY KEY,
            lat DOUBLE,
            lon DOUBLE
        );
    """)
    # These indexes are lightweight helpers for repeated exploration.
    try:
        con.execute('CREATE INDEX IF NOT EXISTS idx_company_name ON companies("CompanyName");')
        con.execute('CREATE INDEX IF NOT EXISTS idx_posttown ON companies("RegAddress.PostTown");')
        con.execute('CREATE INDEX IF NOT EXISTS idx_postcode ON companies("RegAddress.PostCode");')
        con.execute('CREATE INDEX IF NOT EXISTS idx_status ON companies("CompanyStatus");')
    except Exception as exc:  # DuckDB index support can vary by version.
        print(f"Index creation skipped: {exc}")
    con.close()
    print("DuckDB load complete.")


def geocode_postcodes(con: duckdb.DuckDBPyConnection, postcodes: list[str], pause: float = 0.15) -> dict[str, tuple[float, float]]:
    """Return {postcode: (lat, lon)}, using local cache first and postcodes.io for misses."""
    clean = list({p.strip().upper() for p in postcodes if p and p.strip() and p.strip().upper() != "UNKNOWN"})
    if not clean:
        return {}

    cached = con.execute(
        "SELECT postcode, lat, lon FROM postcode_geocache WHERE postcode IN ?",
        [clean],
    ).fetchall()
    result = {row[0]: (row[1], row[2]) for row in cached}
    missing = [p for p in clean if p not in result]

    for i in range(0, len(missing), 100):
        batch = missing[i:i + 100]
        try:
            resp = requests.post(
                "https://api.postcodes.io/postcodes",
                json={"postcodes": batch},
                timeout=20,
            )
            resp.raise_for_status()
            for item in resp.json().get("result", []):
                pc = (item.get("query") or "").upper()
                found = item.get("result")
                if found and found.get("latitude") is not None and found.get("longitude") is not None:
                    lat, lon = float(found["latitude"]), float(found["longitude"])
                    result[pc] = (lat, lon)
                    con.execute(
                        "INSERT OR REPLACE INTO postcode_geocache VALUES (?, ?, ?)",
                        [pc, lat, lon],
                    )
        except requests.RequestException as exc:
            print(f"Geocoding batch failed; continuing without those coordinates: {exc}")
        time.sleep(pause)
    return result


def _safe_int_arg(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def build_filters() -> tuple[str, list[Any], dict[str, str]]:
    """Create a parameterized WHERE clause from request args."""
    keyword = request.args.get("q", "").strip()
    place = request.args.get("place", "").strip()
    category = request.args.get("category", "").strip()

    clauses: list[str] = []
    params: list[Any] = []

    if keyword:
        keyword_clause = ["lower(\"CompanyName\") LIKE '%' || lower(?) || '%'"]
        params.append(keyword)
        for i in range(1, 5):
            keyword_clause.append(f"lower(coalesce(\"SICCode.SicText_{i}\", '')) LIKE '%' || lower(?) || '%'")
            params.append(keyword)
        clauses.append("(" + " OR ".join(keyword_clause) + ")")

    if place:
        clauses.append("""(
            upper(coalesce("RegAddress.PostTown", '')) LIKE '%' || upper(?) || '%'
            OR upper(coalesce("RegAddress.PostCode", '')) LIKE upper(?) || '%'
            OR upper(coalesce("RegAddress.PostCode", '')) LIKE '%' || upper(?) || '%'
        )""")
        params.extend([place, place, place])

    if category:
        raw_labels = STATUS_CATEGORIES.get(category)
        if raw_labels:
            placeholders = ", ".join(["?"] * len(raw_labels))
            clauses.append(f'"CompanyStatus" IN ({placeholders})')
            params.extend(raw_labels)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params, {"q": keyword, "place": place, "category": category}


def _category_case_sql(alias: str = '"CompanyStatus"') -> str:
    parts = []
    for category, raw_labels in STATUS_CATEGORIES.items():
        quoted = ", ".join("'" + label.replace("'", "''") + "'" for label in raw_labels)
        parts.append(f"WHEN {alias} IN ({quoted}) THEN '{category}'")
    return "CASE " + " ".join(parts) + " ELSE 'Other' END"


def _as_pct(numerator: float, denominator: float) -> float:
    return round((numerator / denominator) * 100, 1) if denominator else 0.0


def _growth_pct(recent: int, previous: int) -> float | None:
    if previous == 0:
        return None
    return round(((recent - previous) / previous) * 100, 1)


def _comparison_max_year(max_year: int) -> int:
    """Use the latest complete year for annual opportunity comparisons.

    A Companies House monthly snapshot can contain the current year before that
    year is complete. Comparing a partial year-to-date count with a full prior
    year makes the market look artificially weak, so annual comparisons use the
    latest complete calendar year while the trend still exposes the current YTD
    year separately.
    """
    current_year = date.today().year
    if max_year >= current_year:
        return current_year - 1
    return max_year


def _score_opportunity(count: int, active_rate: float, risk_rate: float, recent: int, previous: int) -> float:
    # The point is not a perfect econometric model. It is a prioritisation lens:
    # strong recent formation + healthy status mix + less saturation gets surfaced.
    growth_bonus = 20 if previous == 0 and recent > 0 else max(-20, min(30, (_growth_pct(recent, previous) or 0) / 4))
    density_penalty = math.log10(max(count, 1)) * 3.5
    return round((recent * 1.8) + (active_rate * 0.35) - (risk_rate * 0.9) + growth_bonus - density_penalty, 1)


def _eureka_cards(total: int, trend: list[dict[str, Any]], towns: list[dict[str, Any]], sectors: list[dict[str, Any]], category_counts: dict[str, int], ranking_min_count: int = 5) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    if total == 0:
        return [{
            "sentiment": "neutral",
            "title": "No market yet",
            "value": "0 matches",
            "note": "Broaden the keyword or remove the place filter to discover adjacent markets.",
        }]

    active = category_counts.get("Active", 0)
    negative = sum(category_counts.get(cat, 0) for cat in NEGATIVE_CATEGORIES)
    cards.append({
        "sentiment": "positive" if active / max(total, 1) >= 0.9 else "warning",
        "title": "Market health",
        "value": f"{_as_pct(active, total)}% active",
        "note": f"{negative:,} companies show at-risk, insolvency or liquidation status inside this search universe.",
    })

    if len(trend) >= 2:
        last, previous = trend[-1], trend[-2]
        delta = last["count"] - previous["count"]
        cards.append({
            "sentiment": "positive" if delta >= 0 else "negative",
            "title": "Latest formation pulse",
            "value": f"{last['year']}: {last['count']:,}",
            "note": f"This is {'+' if delta >= 0 else ''}{delta:,} versus {previous['year']}. Use it as a registration-activity proxy, not revenue or demand proof.",
        })

    # Anything below ranking_min_count is excluded from these two cards: a
    # town with 1-2 companies can show a 100% risk/opportunity rate that
    # looks identical in magnitude to a real pattern from a 20+ company
    # town. towns/sectors already exclude the UNKNOWN/Unclassified buckets
    # (filtered upstream in api_insights), so what's left here is real
    # places and real sectors, just gated on having enough of a sample to
    # say something meaningful.
    growing_towns = [t for t in towns if t.get("recent_count", 0) >= 3 and t.get("count", 0) >= ranking_min_count]
    if growing_towns:
        best = max(growing_towns, key=lambda t: t.get("opportunity_score", -10**9))
        cards.append({
            "sentiment": "positive",
            "title": "Where to look first",
            "value": best["town"],
            "note": f"High opportunity score across {best['count']:,} companies: {best['recent_count']:,} recent incorporations, {best['risk_rate']}% risk rate, {best['active_rate']}% active.",
        })

    risky_towns = [t for t in towns if t.get("count", 0) >= max(5, ranking_min_count)]
    if risky_towns:
        risky = max(risky_towns, key=lambda t: (t.get("risk_rate", 0), t.get("risk_count", 0)))
        if risky.get("risk_count", 0):
            cards.append({
                "sentiment": "negative",
                "title": "Stress pocket",
                "value": risky["town"],
                "note": f"{risky['risk_rate']}% of the {risky['count']:,} matching companies here are in at-risk/insolvency/liquidation categories.",
            })

    if sectors:
        leader = max(sectors, key=lambda s: s.get("count", 0))
        cards.append({
            "sentiment": "neutral",
            "title": "Dominant sector language",
            "value": leader["sic_text"][:54] + ("…" if len(leader["sic_text"]) > 54 else ""),
            "note": f"{leader['count']:,} matching companies use this primary SIC description. This often reveals the buyer vocabulary you should test.",
        })
    return cards[:6]


app = Flask(__name__)


@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.route("/")
def index():
    here = os.path.dirname(os.path.abspath(__file__))
    target = HTML_FILE if os.path.exists(os.path.join(here, HTML_FILE)) else "business_register_search.html"
    if not os.path.exists(os.path.join(here, target)):
        return (
            f"{HTML_FILE} not found next to companies_house_market_server.py. "
            "Save the HTML file into the same folder and refresh.",
            404,
        )
    return send_from_directory(here, target)


@app.route("/api/search")
def api_search():
    limit = _safe_int_arg("limit", 200, 1, 500)
    offset = _safe_int_arg("offset", 0, 0, 10_000_000)
    do_geocode = request.args.get("geocode", "0") == "1"
    where, params, filters = build_filters()

    con = duckdb.connect(DB_FILE)
    total_count = con.execute(f"SELECT count(*) FROM companies {where}", params).fetchone()[0]

    rows = con.execute(f"""
        SELECT
            "CompanyName" AS company_name,
            "CompanyNumber" AS company_number,
            "CompanyStatus" AS status,
            "IncorporationDate" AS incorporation_date,
            "RegAddress.PostTown" AS town,
            "RegAddress.PostCode" AS postcode,
            {PRIMARY_SIC_EXPR} AS sic_text,
            {INC_YEAR_EXPR} AS incorporation_year
        FROM companies
        {where}
        ORDER BY "CompanyName"
        LIMIT {limit} OFFSET {offset}
    """, params).fetchall()

    cols = [
        "company_name", "company_number", "status", "incorporation_date",
        "town", "postcode", "sic_text", "incorporation_year",
    ]
    results = [dict(zip(cols, r)) for r in rows]
    for r in results:
        r["status_category"] = categorize_status(r.get("status"))

    if do_geocode:
        coords = geocode_postcodes(con, [r.get("postcode") or "" for r in results])
        for r in results:
            latlon = coords.get((r.get("postcode") or "").strip().upper())
            r["lat"], r["lon"] = latlon if latlon else (None, None)

    con.close()
    return jsonify({
        "results": results,
        "total_count": total_count,
        "offset": offset,
        "limit": limit,
        "filters": filters,
    })


@app.route("/api/insights")
def api_insights():
    """Full-market analytics. Every count is calculated over the full filtered result set."""
    heatmap_limit = _safe_int_arg("heatmap_limit", 900, 50, 2500)
    min_group = _safe_int_arg("min_group", 3, 1, 1000)
    ranking_min_count = max(_safe_int_arg("ranking_min_count", 5, 1, 1000), min_group)
    where, params, filters = build_filters()
    category_sql = _category_case_sql()

    con = duckdb.connect(DB_FILE)
    total = con.execute(f"SELECT count(*) FROM companies {where}", params).fetchone()[0]

    raw_status_rows = con.execute(f"""
        SELECT coalesce("CompanyStatus", 'Unknown') AS raw_status, count(*) AS count
        FROM companies {where}
        GROUP BY 1
        ORDER BY 2 DESC
    """, params).fetchall()
    raw_status_counts = [{"status": row[0], "count": int(row[1]), "category": categorize_status(row[0])} for row in raw_status_rows]
    category_counts: dict[str, int] = {}
    for item in raw_status_counts:
        category_counts[item["category"]] = category_counts.get(item["category"], 0) + item["count"]

    max_year = con.execute(f"""
        SELECT max(inc_year) FROM (
          SELECT {INC_YEAR_EXPR} AS inc_year FROM companies {where}
        )
        WHERE inc_year BETWEEN 1800 AND {date.today().year + 1}
    """, params).fetchone()[0]
    max_year = int(max_year or date.today().year)
    comparison_max_year = _comparison_max_year(max_year)
    latest_year_is_ytd = max_year >= date.today().year
    recent_start = comparison_max_year - 1
    previous_start = comparison_max_year - 3
    previous_end = comparison_max_year - 2

    trend_rows = con.execute(f"""
        SELECT inc_year, count(*) AS count
        FROM (
          SELECT {INC_YEAR_EXPR} AS inc_year FROM companies {where}
        )
        WHERE inc_year BETWEEN 1980 AND {date.today().year + 1}
        GROUP BY 1
        ORDER BY 1
    """, params).fetchall()
    trend = [{"year": int(y), "count": int(c)} for y, c in trend_rows if y is not None]

    negative_placeholders = ", ".join("'" + x.replace("'", "''") + "'" for x in NEGATIVE_RAW_LABELS)
    active_placeholders = ", ".join("'" + x.replace("'", "''") + "'" for x in ACTIVE_RAW_LABELS)

    # How much of this filtered set has no usable town or sector at all.
    # Companies House marks "no SIC on file" with the literal text
    # "None Supplied" (not a blank field) and overseas-registered companies
    # often have no RegAddress.PostTown — neither is a real place or sector,
    # so both are excluded below rather than ranked as if they were one.
    # This count keeps that exclusion visible instead of silently dropped.
    quality_row = con.execute(f"""
        SELECT
          sum(CASE WHEN nullif(trim(coalesce("RegAddress.PostTown", '')), '') IS NULL THEN 1 ELSE 0 END) AS unknown_town,
          sum(CASE WHEN {PRIMARY_SIC_EXPR} = 'Unclassified' THEN 1 ELSE 0 END) AS unclassified_sector
        FROM companies {where}
    """, params).fetchone()
    data_quality = {
        "unknown_town_count": int(quality_row[0] or 0),
        "unclassified_sector_count": int(quality_row[1] or 0),
    }

    town_rows = con.execute(f"""
        SELECT
          coalesce(nullif(upper(trim(coalesce("RegAddress.PostTown", ''))), ''), 'UNKNOWN') AS town,
          count(*) AS count,
          sum(CASE WHEN "CompanyStatus" IN ({active_placeholders}) THEN 1 ELSE 0 END) AS active_count,
          sum(CASE WHEN "CompanyStatus" IN ({negative_placeholders}) THEN 1 ELSE 0 END) AS risk_count,
          sum(CASE WHEN inc_year BETWEEN {recent_start} AND {comparison_max_year} THEN 1 ELSE 0 END) AS recent_count,
          sum(CASE WHEN inc_year BETWEEN {previous_start} AND {previous_end} THEN 1 ELSE 0 END) AS previous_count
        FROM (
          SELECT *, {INC_YEAR_EXPR} AS inc_year FROM companies {where}
        )
        GROUP BY 1
        HAVING count(*) >= {min_group} AND town != 'UNKNOWN'
        ORDER BY count DESC, town ASC
    """, params).fetchall()
    towns = []
    for town, count, active, risk, recent, previous in town_rows:
        active_rate = _as_pct(active or 0, count or 0)
        risk_rate = _as_pct(risk or 0, count or 0)
        towns.append({
            "town": town,
            "count": int(count or 0),
            "active_count": int(active or 0),
            "risk_count": int(risk or 0),
            "recent_count": int(recent or 0),
            "previous_count": int(previous or 0),
            "growth_pct": _growth_pct(int(recent or 0), int(previous or 0)),
            "active_rate": active_rate,
            "risk_rate": risk_rate,
            "opportunity_score": _score_opportunity(int(count or 0), active_rate, risk_rate, int(recent or 0), int(previous or 0)),
        })

    sector_rows = con.execute(f"""
        SELECT
          {PRIMARY_SIC_EXPR} AS sic_text,
          count(*) AS count,
          sum(CASE WHEN "CompanyStatus" IN ({active_placeholders}) THEN 1 ELSE 0 END) AS active_count,
          sum(CASE WHEN "CompanyStatus" IN ({negative_placeholders}) THEN 1 ELSE 0 END) AS risk_count,
          sum(CASE WHEN inc_year BETWEEN {recent_start} AND {comparison_max_year} THEN 1 ELSE 0 END) AS recent_count,
          sum(CASE WHEN inc_year BETWEEN {previous_start} AND {previous_end} THEN 1 ELSE 0 END) AS previous_count
        FROM (
          SELECT *, {INC_YEAR_EXPR} AS inc_year FROM companies {where}
        )
        GROUP BY 1
        HAVING count(*) >= {min_group} AND sic_text != 'Unclassified'
        ORDER BY count DESC, sic_text ASC
    """, params).fetchall()
    sectors = []
    for sic_text, count, active, risk, recent, previous in sector_rows:
        active_rate = _as_pct(active or 0, count or 0)
        risk_rate = _as_pct(risk or 0, count or 0)
        sectors.append({
            "sic_text": sic_text,
            "count": int(count or 0),
            "active_count": int(active or 0),
            "risk_count": int(risk or 0),
            "recent_count": int(recent or 0),
            "previous_count": int(previous or 0),
            "growth_pct": _growth_pct(int(recent or 0), int(previous or 0)),
            "active_rate": active_rate,
            "risk_rate": risk_rate,
            "opportunity_score": _score_opportunity(int(count or 0), active_rate, risk_rate, int(recent or 0), int(previous or 0)),
        })

    # Heatmap counts use the full result set, then plot the most informative
    # postcode districts. This avoids drawing 100k+ markers while preserving
    # population-level counts.
    heat_rows = con.execute(f"""
        SELECT
          {OUTCODE_EXPR} AS outcode,
          min(nullif(trim("RegAddress.PostCode"), '')) AS representative_postcode,
          count(*) AS count,
          sum(CASE WHEN "CompanyStatus" IN ({negative_placeholders}) THEN 1 ELSE 0 END) AS risk_count,
          sum(CASE WHEN inc_year BETWEEN {recent_start} AND {comparison_max_year} THEN 1 ELSE 0 END) AS recent_count
        FROM (
          SELECT *, {INC_YEAR_EXPR} AS inc_year FROM companies {where}
        )
        WHERE nullif(trim(coalesce("RegAddress.PostCode", '')), '') IS NOT NULL
        GROUP BY 1
    """, params).fetchall()
    source_heat_cell_count = len(heat_rows)

    # Select cells that matter for each heatmap mode. Previously this used
    # only top density cells, which meant switching to risk/recent mode could
    # silently omit smaller but important stress or formation pockets.
    by_outcode = {row[0]: row for row in heat_rows}
    selected: dict[str, Any] = {}
    per_mode = max(1, heatmap_limit // 3)
    for metric_index in (2, 3, 4):  # count, risk_count, recent_count
        ranked = sorted(
            by_outcode.values(),
            key=lambda row: (int(row[metric_index] or 0), int(row[2] or 0), str(row[0])),
            reverse=True,
        )
        for row in ranked[:per_mode]:
            selected.setdefault(row[0], row)
    if len(selected) < heatmap_limit:
        for row in sorted(by_outcode.values(), key=lambda row: (int(row[2] or 0), str(row[0])), reverse=True):
            selected.setdefault(row[0], row)
            if len(selected) >= heatmap_limit:
                break
    heat_rows = sorted(selected.values(), key=lambda row: (int(row[2] or 0), str(row[0])), reverse=True)

    representative_postcodes = [row[1] for row in heat_rows if row[1]]
    coords = geocode_postcodes(con, representative_postcodes) if representative_postcodes else {}
    max_heat_count = max([int(row[2] or 0) for row in heat_rows], default=1)
    heat_points = []
    for outcode, representative, count, risk, recent in heat_rows:
        latlon = coords.get((representative or "").strip().upper())
        if not latlon:
            continue
        heat_points.append({
            "outcode": outcode,
            "postcode": representative,
            "lat": latlon[0],
            "lon": latlon[1],
            "count": int(count or 0),
            "risk_count": int(risk or 0),
            "recent_count": int(recent or 0),
            "intensity": round((int(count or 0) / max_heat_count), 4),
        })

    con.close()

    # Rankings use a higher floor than the general breakdown: a town/sector
    # clearing min_group (default 3) is enough to be listed at all, but
    # being crowned an "opportunity" or "stress pocket" needs a sample big
    # enough that the percentage isn't just 1-2 companies looking extreme.
    ranked_towns = [t for t in towns if t["count"] >= ranking_min_count]
    ranked_sectors = [s for s in sectors if s["count"] >= ranking_min_count]
    towns_by_opportunity = sorted(ranked_towns, key=lambda x: (x["opportunity_score"], x["count"], x["town"]), reverse=True)
    towns_by_risk = sorted(ranked_towns, key=lambda x: (x["risk_rate"], x["risk_count"], x["count"], x["town"]), reverse=True)
    sectors_by_opportunity = sorted(ranked_sectors, key=lambda x: (x["opportunity_score"], x["count"], x["sic_text"]), reverse=True)
    sectors_by_risk = sorted(ranked_sectors, key=lambda x: (x["risk_rate"], x["risk_count"], x["count"], x["sic_text"]), reverse=True)

    comparison_trend = [point for point in trend if point["year"] <= comparison_max_year]
    eureka = _eureka_cards(int(total or 0), comparison_trend, ranked_towns, ranked_sectors, category_counts, ranking_min_count)

    display_towns = sorted(towns, key=lambda x: (x["count"], x["town"]), reverse=True)[:80]
    display_sectors = sorted(sectors, key=lambda x: (x["count"], x["sic_text"]), reverse=True)[:80]

    return jsonify({
        "filters": filters,
        "total_count": int(total or 0),
        "max_incorporation_year": max_year,
        "comparison_window": {
            "recent": f"{recent_start}-{comparison_max_year}",
            "previous": f"{previous_start}-{previous_end}",
            "latest_year_in_trend_is_ytd": latest_year_is_ytd,
        },
        "status": {
            "categories": category_counts,
            "raw": raw_status_counts,
        },
        "data_quality": data_quality,
        "ranking_min_count": ranking_min_count,
        "trend": trend,
        "towns": display_towns,
        "sectors": display_sectors,
        "rankings": {
            "towns_by_opportunity": towns_by_opportunity[:20],
            "towns_by_risk": towns_by_risk[:20],
            "sectors_by_opportunity": sectors_by_opportunity[:20],
            "sectors_by_risk": sectors_by_risk[:20],
        },
        "eureka": eureka,
        "heatmap": {
            "points": heat_points,
            "requested_cells": heatmap_limit,
            "plotted_cells": len(heat_points),
            "source_cells": source_heat_cell_count,
            "full_match_count": int(total or 0),
            "note": "Every count is calculated over the full filtered result set. The map plots aggregated postcode districts, not individual companies.",
        },
    })


@app.route("/api/export.csv")
def api_export_csv():
    """Export filtered results. Capped to protect your laptop from accidental huge exports."""
    limit = _safe_int_arg("limit", 50_000, 1, 250_000)
    where, params, _filters = build_filters()
    con = duckdb.connect(DB_FILE)
    total_count = con.execute(f"SELECT count(*) FROM companies {where}", params).fetchone()[0]
    rows = con.execute(f"""
        SELECT
          "CompanyName" AS company_name,
          "CompanyNumber" AS company_number,
          "CompanyStatus" AS status,
          "IncorporationDate" AS incorporation_date,
          "RegAddress.PostTown" AS town,
          "RegAddress.PostCode" AS postcode,
          {PRIMARY_SIC_EXPR} AS sic_text
        FROM companies
        {where}
        ORDER BY "CompanyName"
        LIMIT {limit}
    """, params).fetchall()
    con.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["company_name", "company_number", "status", "status_category", "incorporation_date", "town", "postcode", "sic_text"])
    for row in rows:
        writer.writerow([row[0], row[1], row[2], categorize_status(row[2]), row[3], row[4], row[5], row[6]])

    truncated = int(total_count or 0) > limit
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=companies_house_market_export.csv",
            "X-Total-Matching-Rows": str(int(total_count or 0)),
            "X-Export-Limit": str(limit),
            "X-Export-Truncated": "true" if truncated else "false",
        },
    )


if __name__ == "__main__":
    if not os.path.exists(DB_FILE):
        download_zip()
        extract_zip()
        load_duckdb()
    else:
        print(f"Found existing {DB_FILE} — reusing it.")
        print(f"Delete {DB_FILE} to pull a fresh monthly snapshot.")

    print("Starting market research dashboard at http://127.0.0.1:5000")
    print("Try: http://127.0.0.1:5000/api/insights?q=pilates&place=london")
    app.run(debug=False)
