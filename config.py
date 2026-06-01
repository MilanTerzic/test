"""
Central configuration and constants for the Serbia Gas Balance dashboard.

All values here originate from the workbook
`Serbian_natural_gas_balance_v8__Kalotina.xlsx`
(sheet: "Serbian Gas Cons. forecast").
"""

from __future__ import annotations

CONVERSION_MCM_TO_GWH = 10.55
CONVERSION_MCM_TO_MWH = 10550.0
CONVERSION_MCM_TO_KWH = 10_550_000.0


def mwh_per_day_to_mcm_per_day(mwh_per_day: float) -> float:
    return mwh_per_day / CONVERSION_MCM_TO_MWH


def kwh_per_day_to_mcm_per_day(kwh_per_day: float) -> float:
    return kwh_per_day / CONVERSION_MCM_TO_KWH


POLY_COEFFS = (0.0007, -0.0188, -0.3194, 11.987)
LINEAR_COEFFS = (-0.354, 11.396)

DOMESTIC_PRODUCTION_MCM = 0.5
BIH_SHARE = 0.08
CURVE_SHIFT_DEFAULT = 0.0
CURVE_DISTORTION_DEFAULT = 1.0

POINTS = {
    "kiskundorozsma_hu": "Kiskundorozsma HU (HU->RS)",
    "kireevo": "Kireevo / Zaychar (BG->RS)",
    "kiskundorozsma_2": "Kiskundorozsma 2 / Horgos transit",
    "kalotina": "Kalotina (BG->RS)",
}

# Important: these two BG pointDirection keys were reversed in the broken build.
ENTSOG_POINT_DIRECTIONS = {
    "kiskundorozsma_hu": "hu-tso-0001itp-00055exit",
    "kireevo": "bg-tso-0001itp-00134exit",
    "kiskundorozsma_2": "hu-tso-0001itp-10013entry",
    "kalotina": "bg-tso-0001itp-00529exit",
}

COLOR_KALOTINA = "#1B7F3A"
COLOR_BG_IMPORT = "#7FB6E2"
COLOR_PRODUCTION = "#34526F"
COLOR_HU_OTHERS = "#2E75B6"
COLOR_HU_MET = "#ED7D31"
COLOR_DEMAND = "#C00000"
COLOR_TEMP = "#1F77B4"
COLOR_STORAGE_POS = "#2E7D32"
COLOR_STORAGE_NEG = "#C00000"
COLOR_TODAY = "#C00000"
GRID_COLOR = "rgba(220,220,220,0.7)"

CAPACITY_DEFS = [
    {
        "tso": "FGSZ",
        "border_point": "Kiskundorozsma (HU)/Kiskundorozsma (RS)",
        "direction": "exit",
        "price_unit": "HUF/kWh/h/day",
        "currency": "HUF",
    },
    {
        "tso": "Bulgartransgaz",
        "border_point": "Kireevo (BG)/Zaychar (RS)",
        "direction": "exit",
        "price_unit": "EUR/kWh/h/day",
        "currency": "EUR",
    },
    {
        "tso": "Gastrans",
        "border_point": "Kireevo (BG)/Zaychar (RS)",
        "direction": "entry",
        "price_unit": "EUR/kWh/h/day",
        "currency": "EUR",
    },
    {
        "tso": "Gastrans",
        "border_point": "Kiskundorozsma 2",
        "direction": "exit",
        "price_unit": "EUR/kWh/h/day",
        "currency": "EUR",
    },
    {
        "tso": "FGSZ",
        "border_point": "Kiskundorozsma 2",
        "direction": "entry",
        "price_unit": "HUF/kWh/h/day",
        "currency": "HUF",
    },
]
