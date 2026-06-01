"""
Demand forecast and gas-balance calculation.
"""

from __future__ import annotations

from typing import Sequence

import numpy as np
import pandas as pd


SUPPLY_COMPONENT_COLUMNS = [
    "imports_from_bulgaria_available_mcm",
    "kalotina_entry_mcm",
    "kiskundorozsma_entry_mcm",
    "domestic_production_mcm",
]


def forecast_demand(
    temperature_c: pd.Series,
    poly_coeffs: Sequence[float],
    linear_coeffs: Sequence[float],
    use_polynomial: bool = True,
    curve_shift: float = 0.0,
    curve_distortion: float = 1.0,
) -> pd.Series:
    x = temperature_c.astype(float)
    coeffs = poly_coeffs if use_polynomial else linear_coeffs
    base = pd.Series(np.polyval(list(coeffs), x), index=x.index)
    if curve_distortion in (None, 0):
        curve_distortion = 1.0
    return base * curve_distortion + curve_shift


def rolling_avg_temperature(temp_series: pd.Series, window: int = 2) -> pd.Series:
    return temp_series.rolling(window=window, min_periods=1).mean()


def _normalize_daily_index(date_index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    return (
        pd.DatetimeIndex(pd.to_datetime(date_index))
        .normalize()
        .drop_duplicates()
        .sort_values()
    )


def _one_row_per_date(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"]).dt.normalize()
    out = out.sort_values("date")
    return out.groupby("date", as_index=False).last()


def _latest_valid_history_value(
    frame: pd.DataFrame,
    target_day: pd.Timestamp,
    column: str,
    max_days_back: int = 2,
) -> float | None:
    for days_back in range(1, max_days_back + 1):
        candidate_day = target_day - pd.Timedelta(days=days_back)
        if candidate_day not in frame.index:
            continue
        value = frame.at[candidate_day, column]
        if pd.notna(value):
            return float(value)
    return None


def _append_estimated_component(existing: str, component: str) -> str:
    parts = [p.strip() for p in str(existing).split(",") if p.strip()]
    if component not in parts:
        parts.append(component)
    return ", ".join(parts)


def _apply_current_day_flow_estimate(
    flow_aligned: pd.DataFrame,
    today_ts: pd.Timestamp,
    flow_columns: Sequence[str],
) -> pd.DataFrame:
    out = flow_aligned.copy()
    out["is_current_day_estimate"] = False
    out["current_day_estimated_components"] = ""

    if today_ts not in out.index:
        return out

    estimated_components: list[str] = []
    for col in flow_columns:
        if col not in out.columns:
            continue
        today_value = out.at[today_ts, col]
        if pd.notna(today_value):
            continue
        fallback = _latest_valid_history_value(out, today_ts, col, max_days_back=2)
        if fallback is None:
            continue
        out.at[today_ts, col] = fallback
        estimated_components.append(col)

    if estimated_components:
        out.at[today_ts, "is_current_day_estimate"] = True
        out.at[today_ts, "current_day_estimated_components"] = ", ".join(
            estimated_components
        )

    return out


def _apply_current_day_derived_estimate(df: pd.DataFrame, today_ts: pd.Timestamp) -> pd.DataFrame:
    out = df.copy()
    today_mask = out["date"] == today_ts
    if not today_mask.any():
        return out

    today_idx = out.index[today_mask][0]

    def fallback_if_invalid(column: str, spike_ratio: float = 1.5, spike_abs: float = 3.0) -> None:
        today_value = out.at[today_idx, column]
        fallback = None
        previous = None
        for days_back in (1, 2):
            candidate = today_ts - pd.Timedelta(days=days_back)
            mask = out["date"] == candidate
            if not mask.any():
                continue
            idx = out.index[mask][0]
            value = out.at[idx, column]
            if pd.notna(value):
                if fallback is None:
                    fallback = float(value)
                if days_back == 1:
                    previous = float(value)
        if fallback is None:
            return

        invalid = pd.isna(today_value)
        if previous is not None and pd.notna(today_value):
            invalid = float(today_value) > max(previous * spike_ratio, previous + spike_abs)

        if invalid:
            out.at[today_idx, column] = fallback
            out.at[today_idx, "is_current_day_estimate"] = True
            out.at[today_idx, "current_day_estimated_components"] = _append_estimated_component(
                out.at[today_idx, "current_day_estimated_components"],
                column,
            )

    for col in [
        "imports_from_bulgaria_mcm",
        "bosnia_consumption_mcm",
        "imports_from_bulgaria_available_mcm",
        "kalotina_entry_mcm",
        "kiskundorozsma_entry_mcm",
    ]:
        if col in out.columns:
            fallback_if_invalid(col)

    return out


def build_balance(
    date_index: pd.DatetimeIndex,
    today_ts: pd.Timestamp,
    flow_df: pd.DataFrame,
    temp_df: pd.DataFrame,
    poly_coeffs: Sequence[float],
    linear_coeffs: Sequence[float],
    use_polynomial: bool = True,
    curve_shift: float = 0.0,
    curve_distortion: float = 1.0,
    bih_share: float = 0.08,
    domestic_production: float = 0.5,
    max_storage_injection: float = 2.7,
    max_storage_withdrawal: float = 5.0,
) -> pd.DataFrame:
    date_index = _normalize_daily_index(date_index)
    today_ts = pd.Timestamp(today_ts).normalize()
    temp_df = _one_row_per_date(temp_df)
    flow_df = _one_row_per_date(flow_df)

    df = pd.DataFrame({"date": date_index})

    temp = temp_df.set_index("date").reindex(date_index)["temperature_c"]
    df["temperature_c"] = temp.values
    df["avg_temperature_c"] = rolling_avg_temperature(temp).values

    is_forecast = df["date"] > today_ts
    df["is_forecast"] = is_forecast.values
    df["temperature_actual_c"] = np.where(is_forecast, np.nan, df["temperature_c"])
    df["temperature_forecast_c"] = np.where(is_forecast, df["temperature_c"], np.nan)

    demand = forecast_demand(
        df["avg_temperature_c"],
        poly_coeffs=poly_coeffs,
        linear_coeffs=linear_coeffs,
        use_polynomial=use_polynomial,
        curve_shift=curve_shift,
        curve_distortion=curve_distortion,
    ).values
    df["demand_mcm"] = demand
    df["required_actual_mcm"] = np.where(is_forecast, np.nan, demand)
    df["required_forecast_mcm"] = np.where(is_forecast, demand, np.nan)

    flow_columns = ["kireevo", "kiskundorozsma_2", "kalotina", "kiskundorozsma_hu"]
    flow_aligned = flow_df.set_index("date").reindex(date_index)
    flow_aligned = _apply_current_day_flow_estimate(flow_aligned, today_ts, flow_columns)
    estimate_flags = flow_aligned[
        ["is_current_day_estimate", "current_day_estimated_components"]
    ].copy()
    flow_aligned = flow_aligned.drop(
        columns=["is_current_day_estimate", "current_day_estimated_components"]
    )

    kkd_hu = pd.to_numeric(flow_aligned.get("kiskundorozsma_hu"), errors="coerce")
    kireevo = pd.to_numeric(flow_aligned.get("kireevo"), errors="coerce")
    kkd_2 = pd.to_numeric(flow_aligned.get("kiskundorozsma_2"), errors="coerce")
    kalotina = pd.to_numeric(flow_aligned.get("kalotina"), errors="coerce")

    imports_from_bulgaria = (kireevo - kkd_2.clip(lower=0.0)).clip(lower=0.0)
    imports_from_bulgaria = imports_from_bulgaria.where(kireevo.notna() & kkd_2.notna())
    bosnia_consumption = (imports_from_bulgaria * float(bih_share)).clip(lower=0.0)

    df["kalotina_entry_mcm"] = kalotina.values
    df["kiskundorozsma_entry_mcm"] = kkd_hu.values
    df["imports_from_bulgaria_mcm"] = imports_from_bulgaria.values
    df["bosnia_consumption_pct"] = float(bih_share) * 100.0
    df["bosnia_consumption_mcm"] = bosnia_consumption.values
    df["imports_from_bulgaria_available_mcm"] = (
        df["imports_from_bulgaria_mcm"] - df["bosnia_consumption_mcm"]
    ).clip(lower=0.0)
    df["domestic_production_mcm"] = float(domestic_production)
    df["is_current_day_estimate"] = estimate_flags["is_current_day_estimate"].values
    df["current_day_estimated_components"] = estimate_flags[
        "current_day_estimated_components"
    ].values

    df = _apply_current_day_derived_estimate(df, today_ts)

    df["serbian_supply_before_bosnia_mcm"] = df[
        [
            "imports_from_bulgaria_mcm",
            "kalotina_entry_mcm",
            "kiskundorozsma_entry_mcm",
            "domestic_production_mcm",
        ]
    ].sum(axis=1, min_count=1)
    df["serbian_available_supply_mcm"] = df[
        [
            "imports_from_bulgaria_available_mcm",
            "kalotina_entry_mcm",
            "kiskundorozsma_entry_mcm",
            "domestic_production_mcm",
        ]
    ].sum(axis=1, min_count=1)

    df["storage_imbalance_raw_mcm"] = df["serbian_available_supply_mcm"] - df["demand_mcm"]
    capped_storage = df["storage_imbalance_raw_mcm"].clip(
        lower=-float(max_storage_withdrawal),
        upper=float(max_storage_injection),
    )
    df["storage_imbalance_mcm"] = capped_storage
    df["storage_injection_mcm"] = capped_storage.clip(lower=0.0)
    df["storage_withdrawal_mcm"] = -capped_storage.clip(upper=0.0)
    df["unserved_deficit_after_storage_limit_mcm"] = (
        -df["storage_imbalance_raw_mcm"] - float(max_storage_withdrawal)
    ).clip(lower=0.0)
    df["uncaptured_surplus_after_storage_limit_mcm"] = (
        df["storage_imbalance_raw_mcm"] - float(max_storage_injection)
    ).clip(lower=0.0)

    return df


def validate_balance_for_plot(
    df: pd.DataFrame,
    today_ts: pd.Timestamp,
    high_total_multiplier: float = 1.6,
    lookaround_days: int = 3,
) -> dict:
    check = df.copy()
    check["date"] = pd.to_datetime(check["date"]).dt.normalize()
    today_ts = pd.Timestamp(today_ts).normalize()

    components = [c for c in SUPPLY_COMPONENT_COLUMNS if c in check.columns]
    check["stacked_supply_total_mcm"] = check[components].sum(axis=1, min_count=1)

    duplicate_dates = check[check["date"].duplicated(keep=False)].sort_values("date").copy()
    hist_fcst_overlap = check[
        check["required_actual_mcm"].notna() & check["required_forecast_mcm"].notna()
    ].copy()

    median_total = check["stacked_supply_total_mcm"].median()
    if pd.isna(median_total) or median_total <= 0:
        high_total_threshold = np.nan
        high_totals = check.iloc[0:0].copy()
    else:
        high_total_threshold = float(median_total * high_total_multiplier)
        high_totals = check[check["stacked_supply_total_mcm"] > high_total_threshold].copy()

    around_today = check[
        check["date"].between(
            today_ts - pd.Timedelta(days=max(lookaround_days, 1)),
            today_ts + pd.Timedelta(days=lookaround_days),
        )
    ].copy()
    current_day_estimates = check[
        check.get("is_current_day_estimate", False) == True  # noqa: E712
    ].copy()

    return {
        "components": components,
        "around_today": around_today,
        "current_day_estimates": current_day_estimates,
        "duplicate_dates": duplicate_dates,
        "hist_fcst_overlap": hist_fcst_overlap,
        "high_totals": high_totals,
        "high_total_threshold": high_total_threshold,
    }
