"""
ENTSOG cross-border capacity booking module.

Primary responsibility: fetch yearly / quarterly / monthly / daily capacity
publications (technical, offered, booked, available) from the ENTSOG
Transparency Platform for the four Serbia cross-border points:

  1. Kiskundorozsma-2 (HU) / Horgos (RS)          HU<->RS
  2. Kiskundorozsma   (HU)  > RS                  HU<->RS
  3. Kalotina (BG) / Dimitrovgrad (RS)            BG<->RS
  4. Kireevo / Kirevo (BG) / Zajecar (RS)         BG<->RS

ENTSOG endpoints used (all under https://transparency.entsog.eu/api/v1):

  /operatorpointdirections   metadata: discover the live pointDirection IDs
                             that match our four border points.
  /operationaldata           the actual capacity timeseries; capacity is
                             returned as one of the indicators below:
                                - Firm Technical
                                - Firm Booked
                                - Firm Available

The yearly / quarterly / monthly / daily split is requested through the
`periodType` parameter (year / quarter / month / day). The endpoint returns
one snapshot per period; we map those into our `auction_product_type`
column directly.

Units: ENTSOG returns kWh/day, kWh/h, MWh/day or GWh/day depending on the
operator. We convert everything to mcm/day using the GCV configured in the
app (default 10.55 kWh/m^3, matching the workbook).

This module also keeps legacy helpers used by uploaded-CSV flow and by
charts.py so the rest of the dashboard keeps working.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timezone
from typing import Any, IO, Optional, Tuple, Union

import numpy as np
import pandas as pd
import requests


# =============================================================================
# Constants and ENTSOG metadata
# =============================================================================

ENTSOG_BASE_URL = "https://transparency.entsog.eu/api/v1"

# Indicators we ask ENTSOG for via /operationaldata.
ENTSOG_CAPACITY_INDICATORS = [
    "Firm Technical",
    "Firm Booked",
    "Firm Available",
]

# Period types we request (one call per type per pointDirection).
ENTSOG_PERIOD_TYPES = ["year", "quarter", "month", "day"]
PERIOD_TYPE_TO_PRODUCT = {
    "year": "yearly",
    "quarter": "quarterly",
    "month": "monthly",
    "day": "daily",
}

# Energy conversion default (kWh per m^3, HHV).
DEFAULT_GCV_KWH_PER_M3 = 10.55

# Pattern set used to recognise our four target points in the ENTSOG metadata.
TARGET_BORDER_POINTS = [
    {
        "canonical_key": "kiskundorozsma_2_horgos",
        "label": "Kiskundorozsma-2 (HU) / Horgos (RS)",
        "country_from": "HU",
        "country_to": "RS",
        "patterns_all": [],
        "patterns_any": ["kiskundorozsma 2", "kiskundorozsma ii", "horgos", "horgo"],
        "patterns_not": [],
    },
    {
        "canonical_key": "kiskundorozsma_hu_rs",
        "label": "Kiskundorozsma (HU > RS)",
        "country_from": "HU",
        "country_to": "RS",
        "patterns_all": ["kiskundorozsma"],
        "patterns_any": [],
        "patterns_not": ["kiskundorozsma 2", "kiskundorozsma ii", "horgos"],
    },
    {
        "canonical_key": "kalotina_dimitrovgrad",
        "label": "Kalotina (BG) / Dimitrovgrad (RS)",
        "country_from": "BG",
        "country_to": "RS",
        "patterns_all": [],
        "patterns_any": ["kalotina", "dimitrovgrad"],
        "patterns_not": [],
    },
    {
        "canonical_key": "kireevo_zajecar",
        "label": "Kireevo/Kirevo (BG) / Zajecar (RS)",
        "country_from": "BG",
        "country_to": "RS",
        "patterns_all": [],
        "patterns_any": ["kireevo", "kirevo", "zajecar", "zaychar"],
        "patterns_not": [],
    },
]

BORDER_POINT_SHORT_NAMES = {
    "Kireevo (BG)/Zaychar (RS)": "BG->RS Kireevo",
    "Kireevo / Zaychar": "BG->RS Kireevo",
    "Kiskundorozsma 2": "RS->HU Kisk. 2",
    "Kiskundorozsma (HU)/Kiskundorozsma (RS)": "HU->RS Kisk.",
    "Kiskundorozsma": "HU->RS Kisk.",
    "Kalotina": "BG->RS Kalotina",
    "Horgos": "HU->RS Horgos",
    "Zvornik": "BA/RS Zvornik",
    "Mokrin": "RS storage / Mokrin",
}

PRODUCT_ORDER = ["yearly", "quarterly", "monthly", "daily", "Unknown"]


# =============================================================================
# Small utilities
# =============================================================================

def _strip_accents(text: Any) -> str:
    """ASCII-fold and lowercase, for fuzzy matching of ENTSOG point labels."""
    if text is None:
        return ""
    s = str(text)
    # NFKD decomposition then drop combining characters.
    nfkd = unicodedata.normalize("NFKD", s)
    ascii_only = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    return ascii_only.lower()


def _to_number(value: Any) -> float:
    if value is None:
        return np.nan
    if isinstance(value, float) and np.isnan(value):
        return np.nan
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("\u00a0", " ")
    if not text or text.lower() in ("nan", "none", "-", "n/a", "na"):
        return np.nan
    text = text.replace("%", "")
    if "," in text and "." in text:
        text = text.replace(",", "")
    elif "," in text and text.count(",") == 1 and len(text.split(",")[-1]) <= 3:
        text = text.replace(",", ".")
    match = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", text)
    if not match:
        return np.nan
    try:
        return float(match.group(0))
    except ValueError:
        return np.nan


# =============================================================================
# ENTSOG HTTP layer
# =============================================================================

def _entsog_get_json(
    path: str,
    params: dict[str, Any],
    timeout: int = 60,
    session: Optional[requests.Session] = None,
) -> Tuple[list[dict[str, Any]], str, Optional[str]]:
    """GET an ENTSOG endpoint. Never raises; returns ([], url, error) on failure."""
    url = f"{ENTSOG_BASE_URL}/{path.lstrip('/')}"
    sess = session or requests
    try:
        response = sess.get(
            url,
            params=params,
            timeout=timeout,
            headers={
                "User-Agent": "serbia-gas-dashboard/1.0 (capacity-module)",
                "Accept": "application/json",
            },
        )
    except requests.RequestException as exc:
        return [], url, f"network error: {exc}"

    if response.status_code >= 400:
        return [], response.url, f"HTTP {response.status_code} {response.reason}"

    try:
        payload = response.json()
    except ValueError as exc:
        return [], response.url, f"invalid JSON: {exc}"

    records: Any = None
    for key in (
        "operationaldatas",
        "operationaldata",
        "operatorpointdirections",
        "data",
        path.strip("/").lower(),
    ):
        if isinstance(payload, dict) and key in payload:
            records = payload[key]
            break
    if records is None:
        records = payload if isinstance(payload, list) else []
    if not isinstance(records, list):
        records = []
    return records, response.url, None


# =============================================================================
# Border-point matching against ENTSOG metadata
# =============================================================================

def _match_target_border_point(
    point_label: Any, country_from: str, country_to: str
) -> Optional[dict]:
    """Return the canonical spec for an ENTSOG point label or None."""
    label_norm = _strip_accents(point_label)
    if not label_norm:
        return None
    cf = (country_from or "").upper()
    ct = (country_to or "").upper()
    for spec in TARGET_BORDER_POINTS:
        pair_ok = (
            (cf == spec["country_from"] and ct == spec["country_to"])
            or (cf == spec["country_to"] and ct == spec["country_from"])
        )
        if not pair_ok:
            continue
        if spec["patterns_all"] and not all(p in label_norm for p in spec["patterns_all"]):
            continue
        if spec["patterns_any"] and not any(p in label_norm for p in spec["patterns_any"]):
            continue
        if spec["patterns_not"] and any(p in label_norm for p in spec["patterns_not"]):
            continue
        return spec
    return None


def _discover_target_point_directions(
    session: Optional[requests.Session] = None,
) -> Tuple[pd.DataFrame, list[str], Optional[str]]:
    """Query /operatorpointdirections and keep rows matching our 4 target IPs."""
    records, url, err = _entsog_get_json(
        "operatorpointdirections", {"limit": -1}, session=session,
    )
    urls = [url]
    if err or not records:
        return pd.DataFrame(), urls, err or "operatorpointdirections returned no rows"

    df = pd.DataFrame(records)
    expected = [
        "pointKey", "pointLabel", "operatorKey", "tsoEicCode", "operatorLabel",
        "directionKey", "validFrom", "validTo", "hasData",
        "tSOCountry", "adjacentCountry", "adjacentTsoEic",
    ]
    for col in expected:
        if col not in df.columns:
            df[col] = ""

    df["pointLabel"] = df["pointLabel"].astype(str)
    df["country_from"] = df["tSOCountry"].astype(str).str.upper().str.strip()
    df["country_to"] = df["adjacentCountry"].astype(str).str.upper().str.strip()
    df["directionKey"] = df["directionKey"].astype(str).str.lower().str.strip()

    matched: list[dict] = []
    for _, row in df.iterrows():
        spec = _match_target_border_point(
            row["pointLabel"], row["country_from"], row["country_to"]
        )
        if not spec:
            continue
        op_key = str(row.get("operatorKey", "")).strip()
        pt_key = str(row.get("pointKey", "")).strip()
        d_key = str(row.get("directionKey", "")).strip()
        composite = f"{op_key}{pt_key}{d_key}" if op_key and pt_key and d_key else ""
        matched.append({
            "canonical_key": spec["canonical_key"],
            "canonical_label": spec["label"],
            "spec_country_from": spec["country_from"],
            "spec_country_to": spec["country_to"],
            "pointKey": pt_key,
            "pointLabel": row.get("pointLabel", ""),
            "operatorKey": op_key,
            "operatorLabel": row.get("operatorLabel", ""),
            "tsoEicCode": row.get("tsoEicCode", ""),
            "directionKey": d_key,
            "pointDirection": composite,
            "validFrom": row.get("validFrom", ""),
            "validTo": row.get("validTo", ""),
            "hasData": row.get("hasData", ""),
            "country_from": row["country_from"],
            "country_to": row["country_to"],
        })
    if not matched:
        return pd.DataFrame(), urls, (
            "No /operatorpointdirections row matched any of the 4 target border points. "
            "ENTSOG may have renamed a point; check the live operatorpointdirections "
            "endpoint for HU<->RS and BG<->RS rows."
        )
    return pd.DataFrame(matched), urls, None


# =============================================================================
# Unit conversion
# =============================================================================

def _convert_to_mcm_per_day(
    value: float, unit: Any, gcv_kwh_per_m3: float
) -> Tuple[float, str]:
    if pd.isna(value):
        return np.nan, "missing_value"
    if not gcv_kwh_per_m3 or gcv_kwh_per_m3 <= 0:
        return np.nan, "invalid_gcv"
    u = str(unit or "").lower().replace(" ", "").replace("_", "/")
    kwh_per_mcm = gcv_kwh_per_m3 * 1_000_000.0

    if u in ("kwh/d", "kwh/day", "kwh"):
        kwh_day = value
    elif u in ("mwh/d", "mwh/day", "mwh"):
        kwh_day = value * 1_000.0
    elif u in ("gwh/d", "gwh/day", "gwh"):
        kwh_day = value * 1_000_000.0
    elif u in ("kwh/h", "kwh/hour"):
        kwh_day = value * 24.0
    elif u in ("mwh/h", "mwh/hour"):
        kwh_day = value * 24_000.0
    elif u in ("m3/d", "m3/day", "scm/d", "scm/day"):
        return value / 1_000_000.0, "ok"
    elif u in ("mcm/d", "mcm/day"):
        return float(value), "ok"
    else:
        return np.nan, f"unsupported_unit:{unit or 'unknown'}"
    return kwh_day / kwh_per_mcm, "ok"


# =============================================================================
# Period classification fallback
# =============================================================================

def _classify_product_from_period(date_from: Any, date_to: Any) -> str:
    if pd.isna(date_from) or pd.isna(date_to):
        return "daily"
    try:
        df_ts = pd.to_datetime(date_from)
        dt_ts = pd.to_datetime(date_to)
    except (TypeError, ValueError):
        return "daily"
    days = max(1, int((dt_ts - df_ts).total_seconds() // 86400) + 1)
    if days >= 330:
        return "yearly"
    if days >= 80:
        return "quarterly"
    if days >= 27:
        return "monthly"
    return "daily"


def _indicator_to_column(indicator: Any) -> str:
    i = str(indicator or "").lower()
    if "technical" in i:
        return "technical_capacity"
    if "offered" in i:
        return "offered_capacity"
    if "booked" in i or "allocated" in i or "allocation" in i:
        return "booked_capacity"
    if "available" in i:
        return "available_capacity"
    return ""


# =============================================================================
# MAIN PUBLIC FUNCTION
# =============================================================================

def fetch_entsog_cross_border_capacity_year(
    year: int,
    include_points: Optional[list[str]] = None,
    direction_filter: Optional[list[str]] = None,
    preferred_unit: str = "mcm/day",
    gcv_kwh_per_m3: float = DEFAULT_GCV_KWH_PER_M3,
    session: Optional[requests.Session] = None,
) -> Tuple[pd.DataFrame, dict[str, Any]]:
    """Fetch yearly/quarterly/monthly/daily capacity bookings for the 4 IPs."""
    start_date = date(int(year), 1, 1)
    end_date = date(int(year), 12, 31)
    quality: dict[str, Any] = {
        "last_successful_fetch": None,
        "records_fetched": 0,
        "api_errors": [],
        "query_urls": [],
        "missing_points": [],
        "missing_products": [],
        "data_warnings": [],
        "matched_point_directions": [],
        "date_range": f"{start_date.isoformat()} to {end_date.isoformat()}",
        "unit": preferred_unit,
        "gcv_kwh_per_m3": gcv_kwh_per_m3,
    }

    # --- 1. Discover live pointDirection IDs --------------------------------
    opd_df, opd_urls, opd_err = _discover_target_point_directions(session=session)
    quality["query_urls"].extend(opd_urls)
    if opd_err:
        quality["api_errors"].append(opd_err)
    if opd_df.empty:
        quality["missing_points"] = [s["label"] for s in TARGET_BORDER_POINTS]
        return _empty_year_frame(), quality

    if include_points:
        opd_df = opd_df[opd_df["canonical_key"].isin(include_points)].copy()
    if direction_filter:
        df_lc = {d.lower() for d in direction_filter}
        opd_df = opd_df[opd_df["directionKey"].isin(df_lc)].copy()
    if opd_df.empty:
        quality["api_errors"].append("Filters left no operator-point-direction rows to query.")
        return _empty_year_frame(), quality

    expected_canonicals = set(opd_df["canonical_key"].unique())
    expected_products = set(PERIOD_TYPE_TO_PRODUCT.values())

    quality["matched_point_directions"] = sorted(
        opd_df.apply(
            lambda r: f"{r['canonical_label']} | {r['operatorLabel']} | {r['directionKey']}",
            axis=1,
        ).tolist()
    )

    # --- 2. Fetch operationaldata -------------------------------------------
    sess = session or requests.Session()
    sess.headers.update({
        "User-Agent": "serbia-gas-dashboard/1.0 (capacity-module)",
        "Accept": "application/json",
    })

    chunks_by_period: dict[str, list[Tuple[date, date]]] = {
        # Yearly and quarterly products only have a handful of records per
        # year, so fetch the full year in a single call to avoid the same
        # record being returned twice from overlapping half-year chunks.
        "year": [(start_date, end_date)],
        "quarter": [(start_date, end_date)],
        # Monthly = 12 rows, also small; one call is enough.
        "month": [(start_date, end_date)],
        # Daily = up to 366 rows but the ENTSOG server has handled this in
        # one call historically; if it ever times out, switch to halves.
        "day": _split_year_into_chunks(start_date, end_date),
    }
    all_records: list[dict] = []

    for _, opd_row in opd_df.iterrows():
        pd_composite = opd_row.get("pointDirection", "")
        op_key = opd_row.get("operatorKey", "")
        pt_key = opd_row.get("pointKey", "")
        d_key = opd_row.get("directionKey", "")
        canonical_key = opd_row["canonical_key"]
        canonical_label = opd_row["canonical_label"]

        for period_type in ENTSOG_PERIOD_TYPES:
            product_label = PERIOD_TYPE_TO_PRODUCT[period_type]
            for chunk_start, chunk_end in chunks_by_period[period_type]:
                params: dict[str, Any] = {
                    "from": chunk_start.isoformat(),
                    "to": chunk_end.isoformat(),
                    "indicator": ",".join(ENTSOG_CAPACITY_INDICATORS),
                    "periodType": period_type,
                    "timezone": "CET",
                    "limit": -1,
                }
                if pd_composite:
                    params["pointDirection"] = pd_composite
                else:
                    params["operatorKey"] = op_key
                    params["pointKey"] = pt_key
                    params["directionKey"] = d_key

                records, url, err = _entsog_get_json(
                    "operationaldata", params, timeout=60, session=sess,
                )
                quality["query_urls"].append(url)
                if err:
                    quality["api_errors"].append(
                        f"{canonical_label} | {product_label} | "
                        f"{chunk_start}..{chunk_end}: {err}"
                    )
                    continue
                if not records:
                    continue

                for rec in records:
                    rec["_canonical_key"] = canonical_key
                    rec["_canonical_label"] = canonical_label
                    rec["_country_from"] = opd_row.get("spec_country_from", "")
                    rec["_country_to"] = opd_row.get("spec_country_to", "")
                    rec["_query_period_type"] = period_type
                    rec["_query_product_label"] = product_label
                    rec["_query_url"] = url
                all_records.extend(records)

    if not all_records:
        quality["missing_points"] = sorted(expected_canonicals)
        quality["missing_products"] = sorted(expected_products)
        quality["api_errors"].append(
            "All /operationaldata queries returned zero records. Check API errors "
            "above, or that ENTSOG has published capacity data for the requested "
            "year. Try a different year."
        )
        return _empty_year_frame(), quality

    quality["records_fetched"] = len(all_records)
    quality["last_successful_fetch"] = (
        datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    # --- 3. Normalize & pivot ------------------------------------------------
    raw = pd.DataFrame(all_records)
    raw = _normalize_raw_capacity(
        raw, gcv_kwh_per_m3=gcv_kwh_per_m3, preferred_unit=preferred_unit
    )
    df = _pivot_indicators_to_columns(raw, preferred_unit=preferred_unit)

    if df.empty:
        quality["api_errors"].append(
            "Records fetched but none mapped to Firm Technical / Firm Booked / "
            "Firm Available indicators."
        )
        return _empty_year_frame(), quality

    # --- 4. Derived columns + warnings --------------------------------------
    tech = pd.to_numeric(df.get("technical_capacity"), errors="coerce")
    bkd = pd.to_numeric(df.get("booked_capacity"), errors="coerce")
    df["booked_pct_of_technical"] = np.where(
        (tech > 0) & bkd.notna(), bkd / tech * 100.0, np.nan,
    )

    if "available_capacity" not in df.columns:
        df["available_capacity"] = np.nan
    avail_missing = df["available_capacity"].isna() & tech.notna() & bkd.notna()
    df.loc[avail_missing, "available_capacity"] = tech - bkd

    if "offered_capacity" not in df.columns:
        df["offered_capacity"] = np.nan

    df["country_pair"] = (
        df["country_from"].astype(str) + ">" + df["country_to"].astype(str)
    )
    df["warning"] = ""
    no_off = df["offered_capacity"].isna()
    df.loc[no_off, "warning"] = (
        df.loc[no_off, "warning"].astype(str)
        + "offered_capacity not available from ENTSOG; "
    )
    tech_zero = (tech == 0) & bkd.notna() & (bkd > 0)
    df.loc[tech_zero, "warning"] = (
        df.loc[tech_zero, "warning"].astype(str)
        + "technical_capacity=0 suspicious; "
    )
    over = (bkd > tech) & tech.notna() & bkd.notna()
    df.loc[over, "warning"] = (
        df.loc[over, "warning"].astype(str)
        + "booked > technical (re-auctioned?); "
    )

    # DQ aggregation
    present_canonicals = set(df["_canonical_key"].dropna().unique())
    quality["missing_points"] = sorted([
        spec["label"] for spec in TARGET_BORDER_POINTS
        if spec["canonical_key"] in expected_canonicals
        and spec["canonical_key"] not in present_canonicals
    ])
    present_products_by_point = (
        df.groupby("_canonical_key")["auction_product_type"]
        .agg(lambda s: set(s.dropna().unique()))
        .to_dict()
    )
    missing_prod_lines: list[str] = []
    for ck in present_canonicals:
        present = present_products_by_point.get(ck, set())
        missing = sorted(expected_products - present)
        if missing:
            label = next(
                (s["label"] for s in TARGET_BORDER_POINTS if s["canonical_key"] == ck),
                ck,
            )
            missing_prod_lines.append(f"{label}: {', '.join(missing)}")
    quality["missing_products"] = missing_prod_lines

    dup_mask = df.duplicated(
        subset=["_canonical_key", "direction", "auction_product_type", "date_from", "date_to"],
        keep=False,
    )
    if dup_mask.any():
        df.loc[dup_mask, "warning"] = df.loc[dup_mask, "warning"] + "duplicate record; "
        quality["data_warnings"].append(
            f"{int(dup_mask.sum())} duplicate (point, product, period) rows found."
        )

    flagged = int(df["warning"].astype(str).str.len().gt(0).sum())
    if flagged:
        quality["data_warnings"].append(
            f"{flagged} rows carry a warning flag (see 'warning' column)."
        )

    sort_cols = [c for c in ["_canonical_label", "direction", "auction_product_type", "date_from"] if c in df.columns]
    df = df.sort_values(sort_cols).reset_index(drop=True)
    return df, quality


def _split_year_into_chunks(start: date, end: date) -> list[Tuple[date, date]]:
    mid = date(start.year, 7, 1)
    if start >= mid or end <= mid:
        return [(start, end)]
    return [(start, date(start.year, 6, 30)), (mid, end)]


def _empty_year_frame() -> pd.DataFrame:
    cols = [
        "date_from", "date_to", "gas_day",
        "country_from", "country_to", "country_pair",
        "TSO", "TSO_code",
        "interconnection_point_name", "interconnection_point_code",
        "direction", "auction_product_type",
        "technical_capacity", "offered_capacity", "booked_capacity", "available_capacity",
        "booked_pct_of_technical",
        "unit", "source_url", "query_metadata", "warning",
        "_canonical_key", "_canonical_label",
    ]
    return pd.DataFrame(columns=cols)


# =============================================================================
# Normalization / pivoting
# =============================================================================

def _normalize_raw_capacity(
    raw: pd.DataFrame, gcv_kwh_per_m3: float, preferred_unit: str,
) -> pd.DataFrame:
    """Heterogeneous ENTSOG records -> tidy long frame."""

    def first_col(*candidates):
        for c in candidates:
            if c in raw.columns:
                return c
        return None

    period_from_col = first_col("periodFrom", "from", "PeriodFrom")
    period_to_col = first_col("periodTo", "to", "PeriodTo")
    indicator_col = first_col("indicator", "Indicator")
    unit_col = first_col("unit", "Unit")
    value_col = first_col("value", "Value")
    op_label_col = first_col("operatorLabel", "OperatorLabel")
    eic_col = first_col("tsoEicCode", "TsoEicCode")
    point_label_col = first_col("pointLabel", "PointLabel")
    point_key_col = first_col("pointKey", "PointKey")
    direction_col = first_col("directionKey", "DirectionKey", "direction")
    period_type_col = first_col("periodType", "PeriodType")

    df = pd.DataFrame(index=raw.index)
    if period_from_col:
        df["date_from"] = pd.to_datetime(raw[period_from_col], errors="coerce", utc=True)
        if df["date_from"].notna().any():
            df["date_from"] = df["date_from"].dt.tz_convert("Europe/Belgrade").dt.tz_localize(None)
    else:
        df["date_from"] = pd.NaT
    if period_to_col:
        df["date_to"] = pd.to_datetime(raw[period_to_col], errors="coerce", utc=True)
        if df["date_to"].notna().any():
            df["date_to"] = df["date_to"].dt.tz_convert("Europe/Belgrade").dt.tz_localize(None)
    else:
        df["date_to"] = pd.NaT
    df["gas_day"] = df["date_from"].dt.normalize()

    df["indicator"] = raw[indicator_col].astype(str) if indicator_col else ""
    df["unit_native"] = raw[unit_col].astype(str) if unit_col else ""
    df["value_native"] = raw[value_col].map(_to_number) if value_col else np.nan

    converted, status = [], []
    for v, u in zip(df["value_native"], df["unit_native"]):
        c, s = _convert_to_mcm_per_day(v, u, gcv_kwh_per_m3=gcv_kwh_per_m3)
        converted.append(c)
        status.append(s)
    df["value_mcm_day"] = converted
    df["conversion_status"] = status

    if preferred_unit == "mcm/day":
        df["value_final"] = df["value_mcm_day"]
        df["unit_final"] = "mcm/day"
    else:
        df["value_final"] = df["value_native"]
        df["unit_final"] = df["unit_native"].where(df["unit_native"].astype(bool), "n/a")

    df["TSO"] = raw[op_label_col].astype(str) if op_label_col else ""
    df["TSO_code"] = raw[eic_col].astype(str) if eic_col else ""
    df["interconnection_point_name"] = raw[point_label_col].astype(str) if point_label_col else ""
    df["interconnection_point_code"] = raw[point_key_col].astype(str) if point_key_col else ""
    df["direction"] = raw[direction_col].astype(str).str.lower() if direction_col else ""

    query_pt = (
        raw["_query_period_type"].astype(str).str.lower()
        if "_query_period_type" in raw.columns
        else pd.Series([""] * len(raw), index=raw.index)
    )
    actual_pt = (
        raw[period_type_col].astype(str).str.lower()
        if period_type_col
        else pd.Series([""] * len(raw), index=raw.index)
    )
    chosen_pt = query_pt.where(query_pt.isin(ENTSOG_PERIOD_TYPES), actual_pt)
    mapped = chosen_pt.map(PERIOD_TYPE_TO_PRODUCT).fillna("")
    fallback = [
        _classify_product_from_period(a, b)
        for a, b in zip(df["date_from"], df["date_to"])
    ]
    df["auction_product_type"] = [m if m else f for m, f in zip(mapped, fallback)]

    df["_canonical_key"] = (
        raw["_canonical_key"] if "_canonical_key" in raw.columns else ""
    )
    df["_canonical_label"] = (
        raw["_canonical_label"] if "_canonical_label" in raw.columns else ""
    )
    df["country_from"] = (
        raw["_country_from"] if "_country_from" in raw.columns else ""
    )
    df["country_to"] = raw["_country_to"] if "_country_to" in raw.columns else ""
    df["source_url"] = raw["_query_url"] if "_query_url" in raw.columns else ""
    df["query_metadata"] = (
        "periodType=" + chosen_pt.fillna("").astype(str)
        + "; indicator=" + df["indicator"].astype(str)
    )

    df["metric_col"] = df["indicator"].map(_indicator_to_column)
    df = df[df["metric_col"] != ""].copy()
    return df


def _pivot_indicators_to_columns(df: pd.DataFrame, preferred_unit: str) -> pd.DataFrame:
    if df.empty:
        return _empty_year_frame()

    # Keys that uniquely identify a (point, product, period) row. Note that
    # source_url and query_metadata are deliberately NOT in the key because
    # they vary per HTTP call (one per indicator) — keeping them would prevent
    # the indicators from collapsing into a single row.
    key_cols = [
        "date_from", "date_to", "gas_day",
        "country_from", "country_to",
        "TSO", "TSO_code",
        "interconnection_point_name", "interconnection_point_code",
        "direction", "auction_product_type",
        "unit_final",
        "_canonical_key", "_canonical_label",
    ]
    # Drop rows with NaT date_from/date_to to keep pivot keys hashable.
    work = df.dropna(subset=["date_from", "date_to"]).copy()
    if work.empty:
        return _empty_year_frame()

    # Pivot the numeric value across indicators.
    pivot = (
        work.pivot_table(
            index=key_cols,
            columns="metric_col",
            values="value_final",
            aggfunc="mean",
        )
        .reset_index()
    )
    for c in ("technical_capacity", "offered_capacity", "booked_capacity", "available_capacity"):
        if c not in pivot.columns:
            pivot[c] = np.nan

    # Aggregate source_url and query_metadata separately (first non-empty).
    meta = (
        work.groupby(key_cols, dropna=False)
        .agg(
            source_url=("source_url", lambda s: next((str(x) for x in s if str(x).strip()), "")),
            query_metadata=("query_metadata", lambda s: " | ".join(sorted({str(x) for x in s if str(x).strip()}))),
        )
        .reset_index()
    )
    pivot = pivot.merge(meta, on=key_cols, how="left")

    pivot = pivot.rename(columns={"unit_final": "unit"})

    ordered = [
        "date_from", "date_to", "gas_day",
        "country_from", "country_to",
        "TSO", "TSO_code",
        "interconnection_point_name", "interconnection_point_code",
        "direction", "auction_product_type",
        "technical_capacity", "offered_capacity", "booked_capacity", "available_capacity",
        "unit", "source_url", "query_metadata",
        "_canonical_key", "_canonical_label",
    ]
    for c in ordered:
        if c not in pivot.columns:
            pivot[c] = "" if c in ("source_url", "query_metadata", "unit") else np.nan
    return pivot[ordered].copy()


# =============================================================================
# FX rates (used by legacy capacity-bookings upload flow)
# =============================================================================

def fetch_latest_fx_rates(base: str = "EUR") -> dict[str, Any]:
    sources = [
        ("frankfurter.dev", f"https://api.frankfurter.dev/v1/latest?base={base}"),
        ("frankfurter.app", f"https://api.frankfurter.app/latest?base={base}"),
    ]
    for src, url in sources:
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                payload = r.json()
                rates = payload.get("rates") or payload.get("Rates") or {}
                if rates:
                    rates[base] = 1.0
                    return {
                        "base": base,
                        "rates": {k: float(v) for k, v in rates.items()},
                        "date": payload.get("date", ""),
                        "source": src,
                    }
        except (requests.RequestException, ValueError):
            continue
    return {
        "base": base,
        "rates": {base: 1.0, "BGN": 1.95583},  # fixed BGN parity
        "date": "",
        "source": "fallback_static",
    }


# =============================================================================
# Legacy helpers (uploaded-CSV flow + charts.py compatibility)
# =============================================================================

def empty_capacity_frame() -> pd.DataFrame:
    cols = [
        "tso", "border_point", "border_point_full", "border_point_short",
        "direction", "product", "auction_product_type",
        "period", "delivery_period", "delivery_sort",
        "period_start", "period_end", "period_days",
        "offered_mwh", "booked_mwh", "utilisation_pct",
        "offered_capacity_mwh_day", "booked_capacity_mwh_day",
        "booked_percentage", "price_original", "price_currency",
        "price_unit_detected", "price_eur_per_mwh",
        "data_quality_warning", "has_quality_warning",
        "source_retrieval_date",
    ]
    return pd.DataFrame(columns=cols)


def border_point_short_name(border_point: Any, direction: Any = None) -> str:
    if border_point is None or (isinstance(border_point, float) and np.isnan(border_point)):
        full = ""
    else:
        full = str(border_point).strip()
    if full in BORDER_POINT_SHORT_NAMES:
        return BORDER_POINT_SHORT_NAMES[full]
    compact = re.sub(r"\s*\([^)]*\)", "", full).strip()
    if compact in BORDER_POINT_SHORT_NAMES:
        return BORDER_POINT_SHORT_NAMES[compact]
    return compact[:24] + "..." if len(compact) > 27 else compact


def read_uploaded(upload: Union[IO, bytes]) -> pd.DataFrame:
    """Parse an uploaded CSV/XLSX of capacity bookings."""
    name = getattr(upload, "name", "")
    if name.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(upload)
    else:
        df = pd.read_csv(upload)
    df.columns = [str(c).strip() for c in df.columns]
    return df


COLUMN_ALIASES = {
    "tso": ["tso", "operator", "transmission system operator"],
    "border_point": [
        "border point", "border_point", "point", "interconnection point", "pointlabel",
    ],
    "direction": ["type", "direction", "entry/exit", "directionkey"],
    "product": [
        "product", "product type", "auction product type", "auction_product_type", "runtime period",
    ],
    "period": ["period", "delivery period", "gas day", "date"],
    "offered_mwh": [
        "offered (mwh/day)", "offered (mwh/d)", "offered (kwh/h)", "offered (kwh/day)",
        "offered", "offered_mwh", "offered capacity", "offered_capacity_mwh_day",
        "offered_capacity",
    ],
    "booked_mwh": [
        "booked (mwh/day)", "booked (mwh/d)", "booked (kwh/h)", "booked (kwh/day)",
        "booked", "booked_mwh", "booked capacity", "booked_capacity_mwh_day",
        "booked_capacity",
    ],
    "utilisation_pct": [
        "booked %", "booked%", "utilisation_pct", "utilization_pct", "utilisation",
    ],
    "price": ["price", "reserve price", "tariff"],
    "currency": ["currency", "ccy"],
    "price_unit": ["price unit", "unit", "price_unit"],
    "source_timestamp": [
        "source timestamp", "retrieval date", "retrieved at",
        "created at", "updated at", "timestamp",
    ],
}


def _clean_col_key(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())


def _first_present(df: pd.DataFrame, aliases: list[str]) -> Optional[str]:
    lookup = {_clean_col_key(c): c for c in df.columns}
    for a in aliases:
        if a in lookup:
            return lookup[a]
    return None


def prepare_chart_data(
    df: pd.DataFrame, fx_rates: Optional[dict[str, Any]] = None,
) -> pd.DataFrame:
    """Normalize a free-form capacity bookings frame (e.g. uploaded CSV)."""
    if df is None or df.empty:
        return empty_capacity_frame()

    out = pd.DataFrame(index=df.index)
    src_cols: dict[str, Optional[str]] = {}
    for target, aliases in COLUMN_ALIASES.items():
        src = _first_present(df, aliases)
        src_cols[target] = src
        out[target] = df[src] if src else np.nan

    out["tso"] = out["tso"].fillna("").astype(str).str.strip()
    out["border_point_full"] = out["border_point"].fillna("").astype(str).str.strip()
    out["direction"] = out["direction"].fillna("").astype(str).str.strip().str.lower()
    out["border_point_short"] = [
        border_point_short_name(bp, d)
        for bp, d in zip(out["border_point_full"], out["direction"])
    ]
    out["product"] = out["product"].fillna("").astype(str).str.strip().str.lower()
    out["period"] = out["period"].fillna("").astype(str).str.strip()

    out["offered_mwh"] = out["offered_mwh"].map(_to_number)
    out["booked_mwh"] = out["booked_mwh"].map(_to_number)
    out["utilisation_pct"] = out["utilisation_pct"].map(_to_number)
    derive = (
        out["utilisation_pct"].isna()
        & (out["offered_mwh"].fillna(0) > 0)
        & out["booked_mwh"].notna()
    )
    out.loc[derive, "utilisation_pct"] = (
        out.loc[derive, "booked_mwh"] / out.loc[derive, "offered_mwh"] * 100.0
    )
    out["offered_capacity_mwh_day"] = out["offered_mwh"]
    out["booked_capacity_mwh_day"] = out["booked_mwh"]
    out["booked_percentage"] = out["utilisation_pct"]

    def _norm_prod(p: Any) -> str:
        p = str(p or "").lower()
        if "year" in p:
            return "yearly"
        if "quarter" in p:
            return "quarterly"
        if "month" in p:
            return "monthly"
        if "day" in p:
            return "daily"
        return p or "daily"
    out["auction_product_type"] = out["product"].map(_norm_prod)

    if "price" in out.columns:
        out["price_original"] = out["price"].map(_to_number)
    else:
        out["price_original"] = np.nan
    out["price_currency"] = (
        out["currency"].fillna("").astype(str).str.upper().str.strip()
        if "currency" in out.columns else ""
    )
    out["price_unit_detected"] = (
        out["price_unit"].fillna("").astype(str).str.strip()
        if "price_unit" in out.columns else ""
    )

    rates = (fx_rates or {}).get("rates") if fx_rates else None
    if rates:
        def _to_eur(price, ccy):
            if pd.isna(price) or not ccy:
                return np.nan
            ccy = str(ccy).upper().strip()
            if ccy == "EUR":
                return float(price)
            rate = rates.get(ccy)
            if rate and rate > 0:
                return float(price) / rate
            return np.nan
        out["price_eur_per_mwh"] = [
            _to_eur(p, c) for p, c in zip(out["price_original"], out["price_currency"])
        ]
    else:
        out["price_eur_per_mwh"] = np.nan

    out["delivery_period"] = out["period"]
    out["delivery_sort"] = pd.to_datetime(out["period"], errors="coerce")
    out["period_start"] = out["delivery_sort"]
    out["period_end"] = out["delivery_sort"]
    out["period_days"] = 1

    if src_cols.get("source_timestamp"):
        out["source_retrieval_date"] = df[src_cols["source_timestamp"]].astype(str)
    else:
        out["source_retrieval_date"] = ""

    out["data_quality_warning"] = ""
    out["has_quality_warning"] = False
    return out


def run_data_quality_checks(df: pd.DataFrame) -> dict[str, Any]:
    quality: dict[str, Any] = {
        "warnings": [],
        "row_count": int(len(df)) if df is not None else 0,
    }
    if df is None or df.empty:
        return quality
    if "offered_mwh" in df.columns and "booked_mwh" in df.columns:
        bad = (df["offered_mwh"].fillna(0) > 0) & df["booked_mwh"].isna()
        if bad.any():
            quality["warnings"].append(
                f"{int(bad.sum())} rows have offered > 0 but no booked value."
            )
    if "utilisation_pct" in df.columns:
        over = df["utilisation_pct"].fillna(0) > 100.0
        if over.any():
            quality["warnings"].append(
                f"{int(over.sum())} rows show utilisation > 100% (re-auctioned?)."
            )
    return quality


def attach_quality_warnings(
    df: pd.DataFrame, quality: dict[str, Any],
) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    if "data_quality_warning" not in df.columns:
        df["data_quality_warning"] = ""
    if "utilisation_pct" in df.columns:
        over = df["utilisation_pct"].fillna(0) > 100.0
        df.loc[over, "data_quality_warning"] = (
            df.loc[over, "data_quality_warning"].astype(str) + "utilisation>100%; "
        )
    df["has_quality_warning"] = df["data_quality_warning"].astype(str).str.len() > 0
    return df


def plotted_series_validation_table(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    keep = [
        c for c in [
            "tso", "border_point_full", "border_point_short", "direction",
            "auction_product_type", "delivery_period",
            "offered_capacity_mwh_day", "booked_capacity_mwh_day",
            "booked_percentage", "price_original", "price_currency",
            "price_unit_detected", "price_eur_per_mwh", "data_quality_warning",
        ] if c in df.columns
    ]
    return df[keep].copy()


# =============================================================================
# Module self-check
# =============================================================================

if __name__ == "__main__":  # pragma: no cover
    import sys
    yr = int(sys.argv[1]) if len(sys.argv) > 1 else (date.today().year - 1)
    print(f"[capacity] Fetching ENTSOG cross-border capacity for year={yr} ...")
    df, q = fetch_entsog_cross_border_capacity_year(yr)
    print(f"  records: {len(df)}")
    print(f"  matched pointDirections: {q['matched_point_directions']}")
    print(f"  api_errors: {q['api_errors'][:3]}")
    print(f"  missing_points: {q['missing_points']}")
    print(f"  missing_products: {q['missing_products']}")
    if not df.empty:
        sample_cols = [
            "_canonical_label", "direction", "auction_product_type",
            "date_from", "date_to", "technical_capacity", "booked_capacity",
            "available_capacity", "unit",
        ]
        sample_cols = [c for c in sample_cols if c in df.columns]
        print(df[sample_cols].head(10).to_string(index=False))
