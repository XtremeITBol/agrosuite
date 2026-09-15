"""Módulo 4 — Analítica de producción.

Cuatro capas, de descriptiva a predictiva:

* `kpis`          — indicadores de la operación con comparación contra el
                    período anterior (una cifra sin referencia no es un KPI).
* `yield_by_field`— rendimiento t/ha por lote contra el objetivo del cultivo,
                    con el efecto estimado de la presión de plagas.
* `anomalies`     — Isolation Forest sobre el perfil multivariado de cada
                    cosecha, para detectar registros que merecen auditoría
                    (mermas anómalas, costos fuera de rango, humedad rara).
* `forecast_yield`— pronóstico de rendimiento del próximo ciclo por lote con
                    regresión sobre clima acumulado e historia del lote,
                    validado con leave-one-out temporal.
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest, RandomForestRegressor
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import KFold, cross_val_predict

from .. import db


# ---------------------------------------------------------------------------
# Carga base
# ---------------------------------------------------------------------------

_FRAME_CACHE: dict[tuple, tuple[float, pd.DataFrame]] = {}


def _harvest_frame(db_path=None) -> pd.DataFrame:
    """Dataframe enriquecido de cosechas, memoizado por mtime de la base.

    Las cuatro vistas analíticas lo necesitan; sin caché el tablero lo
    reconstruye cuatro veces por carga. La clave incluye el mtime, así que
    cualquier escritura en la base invalida el caché automáticamente.
    """
    from pathlib import Path as _P
    from .. config import settings as _s
    key = str(db_path or _s.db_path)
    try:
        mtime = _P(key).stat().st_mtime
    except OSError:
        mtime = 0.0
    hit = _FRAME_CACHE.get((key,))
    if hit and hit[0] == mtime:
        return hit[1].copy()
    frame = _build_harvest_frame(db_path)
    _FRAME_CACHE[(key,)] = (mtime, frame)
    return frame.copy()


def _build_harvest_frame(db_path=None) -> pd.DataFrame:
    with db.session(db_path) as conn:
        h = pd.DataFrame(db.query(conn, """
            SELECT h.*, f.name AS field, f.area_ha, f.region, f.soil_type,
                   f.planting_date, c.name AS crop, c.target_yield, c.cycle_days
            FROM harvests h
            JOIN fields f ON f.id = h.field_id
            JOIN crops  c ON c.id = f.crop_id
            ORDER BY h.date
        """))
        pests = pd.DataFrame(db.query(conn, """
            SELECT field_id, date, species, incidence_pct FROM pest_observations
        """))
        wx = pd.DataFrame(db.query(conn, "SELECT * FROM weather_daily"))

    if h.empty:
        return h
    h["date"] = pd.to_datetime(h["date"])
    h["yield_t_ha"] = h["tonnage"] / h["area_ha"]
    h["cost_usd_t"] = h["cost_usd"] / h["tonnage"].replace(0, np.nan)
    h["cost_usd_ha"] = h["cost_usd"] / h["area_ha"]
    h["yield_vs_target"] = h["yield_t_ha"] / h["target_yield"]

    # presión de plagas del ciclo (media de incidencia en los 45 días previos)
    if not pests.empty:
        pests["date"] = pd.to_datetime(pests["date"])
        press = []
        for _, r in h.iterrows():
            m = ((pests["field_id"] == r["field_id"]) &
                 (pests["date"] <= r["date"]) &
                 (pests["date"] >= r["date"] - timedelta(days=45)))
            sel = pests.loc[m, "incidence_pct"]
            press.append(float(sel.mean()) if len(sel) else 0.0)
        h["pest_pressure"] = press
    else:
        h["pest_pressure"] = 0.0

    # clima acumulado de los 90 días previos a la cosecha
    if not wx.empty:
        wx["date"] = pd.to_datetime(wx["date"])
        wx["tmean"] = (wx["tmin_c"] + wx["tmax_c"]) / 2
        rain, tmean, rh = [], [], []
        for _, r in h.iterrows():
            m = ((wx["region"] == r["region"]) & (wx["date"] <= r["date"]) &
                 (wx["date"] >= r["date"] - timedelta(days=90)))
            sel = wx.loc[m]
            rain.append(float(sel["rain_mm"].sum()) if len(sel) else np.nan)
            tmean.append(float(sel["tmean"].mean()) if len(sel) else np.nan)
            rh.append(float(sel["rh_pct"].mean()) if len(sel) else np.nan)
        h["rain_90d"], h["tmean_90d"], h["rh_90d"] = rain, tmean, rh
    return h


# ---------------------------------------------------------------------------
# 1. KPIs
# ---------------------------------------------------------------------------

def _delta(cur: float, prev: float) -> dict:
    if prev in (0, None) or (isinstance(prev, float) and np.isnan(prev)):
        return {"value": round(cur, 2), "prev": None, "change_pct": None}
    return {"value": round(cur, 2), "prev": round(prev, 2),
            "change_pct": round(100 * (cur - prev) / abs(prev), 1)}


def kpis(db_path=None, window_days: int = 180) -> dict:
    h = _harvest_frame(db_path)
    if h.empty:
        return {"window_days": window_days, "kpis": {}, "note": "Sin cosechas registradas."}

    today = pd.Timestamp(date.today())
    cur = h[h["date"] > today - timedelta(days=window_days)]
    prev = h[(h["date"] <= today - timedelta(days=window_days)) &
             (h["date"] > today - timedelta(days=2 * window_days))]

    def agg(df: pd.DataFrame) -> dict:
        if df.empty:
            return {k: float("nan") for k in
                    ("tonnage", "area", "yield", "cost_t", "cost_ha", "moisture",
                     "grade_a", "pest", "attainment")}
        return {
            "tonnage": df["tonnage"].sum(),
            "area": df["area_ha"].sum(),
            "yield": df["tonnage"].sum() / df["area_ha"].sum(),
            "cost_t": df["cost_usd"].sum() / df["tonnage"].sum(),
            "cost_ha": df["cost_usd"].sum() / df["area_ha"].sum(),
            "moisture": df["moisture_pct"].mean(),
            "grade_a": 100 * (df["quality_grade"] == "A").mean(),
            "pest": df["pest_pressure"].mean(),
            "attainment": 100 * df["yield_vs_target"].mean(),
        }

    c, p = agg(cur), agg(prev)
    return {
        "window_days": window_days,
        "period": {"from": str((today - timedelta(days=window_days)).date()),
                   "to": str(today.date())},
        "harvests": int(len(cur)),
        "kpis": {
            "produccion_t":            {**_delta(c["tonnage"], p["tonnage"]), "unit": "t"},
            "superficie_cosechada_ha": {**_delta(c["area"], p["area"]), "unit": "ha"},
            "rendimiento_t_ha":        {**_delta(c["yield"], p["yield"]), "unit": "t/ha"},
            "costo_usd_t":             {**_delta(c["cost_t"], p["cost_t"]), "unit": "USD/t",
                                        "lower_is_better": True},
            "costo_usd_ha":            {**_delta(c["cost_ha"], p["cost_ha"]), "unit": "USD/ha",
                                        "lower_is_better": True},
            "humedad_media_pct":       {**_delta(c["moisture"], p["moisture"]), "unit": "%",
                                        "lower_is_better": True},
            "grado_A_pct":             {**_delta(c["grade_a"], p["grade_a"]), "unit": "%"},
            "presion_plagas_pct":      {**_delta(c["pest"], p["pest"]), "unit": "%",
                                        "lower_is_better": True},
            "cumplimiento_objetivo_pct": {**_delta(c["attainment"], p["attainment"]), "unit": "%"},
        },
    }


# ---------------------------------------------------------------------------
# 2. Rendimiento por lote
# ---------------------------------------------------------------------------

def yield_by_field(db_path=None, limit: int | None = None) -> dict:
    h = _harvest_frame(db_path)
    if h.empty:
        return {"fields": [], "note": "Sin cosechas registradas."}

    g = h.groupby(["field_id", "field", "crop", "region", "soil_type"], as_index=False).agg(
        harvests=("id", "count"),
        area_ha=("area_ha", "last"),
        tonnage=("tonnage", "sum"),
        yield_t_ha=("yield_t_ha", "mean"),
        target_yield=("target_yield", "last"),
        cost_usd_t=("cost_usd_t", "mean"),
        moisture=("moisture_pct", "mean"),
        pest_pressure=("pest_pressure", "mean"),
        last_harvest=("date", "max"),
    )
    g["attainment_pct"] = 100 * g["yield_t_ha"] / g["target_yield"]
    g["gap_t"] = (g["target_yield"] - g["yield_t_ha"]) * g["area_ha"]
    g = g.sort_values("attainment_pct")

    # Efecto de la presión de plagas sobre el rendimiento relativo: correlación
    # simple, informativa, no causal. Se etiqueta como tal en la respuesta.
    corr = None
    if len(g) >= 6 and g["pest_pressure"].std() > 0:
        corr = round(float(np.corrcoef(g["pest_pressure"], g["attainment_pct"])[0, 1]), 3)

    rows = g.to_dict("records")
    for r in rows:
        r["last_harvest"] = str(pd.Timestamp(r["last_harvest"]).date())
        for k in ("area_ha", "tonnage", "yield_t_ha", "target_yield", "cost_usd_t",
                  "moisture", "pest_pressure", "attainment_pct", "gap_t"):
            r[k] = round(float(r[k]), 2)
    if limit:
        rows = rows[:limit]

    return {
        "fields": rows,
        "underperforming": [r["field"] for r in rows if r["attainment_pct"] < 85],
        "total_gap_t": round(float(g.loc[g["gap_t"] > 0, "gap_t"].sum()), 1),
        "pest_vs_attainment_corr": corr,
        "corr_note": ("Es asociación, no causalidad: el clima afecta a ambas variables."),
    }


# ---------------------------------------------------------------------------
# 3. Detección de anomalías
# ---------------------------------------------------------------------------

ANOMALY_FEATURES = ["yield_t_ha", "cost_usd_t", "moisture_pct", "pest_pressure",
                    "cost_usd_ha", "yield_vs_target"]


def anomalies(db_path=None, contamination: float = 0.08) -> dict:
    h = _harvest_frame(db_path)
    if len(h) < 20:
        return {"anomalies": [], "note": f"Se necesitan al menos 20 cosechas ({len(h)} disponibles)."}

    X = h[ANOMALY_FEATURES].replace([np.inf, -np.inf], np.nan)
    X = X.fillna(X.median())
    # Estandarizar para que USD/ha no domine sobre t/ha por escala.
    Z = (X - X.mean()) / X.std().replace(0, 1)

    iso = IsolationForest(n_estimators=300, contamination=contamination,
                          random_state=0).fit(Z)
    h = h.copy()
    h["anomaly_score"] = -iso.score_samples(Z)     # mayor = más anómalo
    h["is_anomaly"] = iso.predict(Z) == -1

    med = X.median()
    out = []
    for _, r in h[h["is_anomaly"]].sort_values("anomaly_score", ascending=False).iterrows():
        # motivo: la variable con mayor desvío estandarizado
        devs = {f: float((r[f] - med[f]) / (X[f].std() or 1)) for f in ANOMALY_FEATURES}
        driver = max(devs, key=lambda k: abs(devs[k]))
        direction = "por encima" if devs[driver] > 0 else "por debajo"
        out.append({
            "harvest_id": int(r["id"]), "field": r["field"], "crop": r["crop"],
            "date": str(r["date"].date()),
            "score": round(float(r["anomaly_score"]), 3),
            "yield_t_ha": round(float(r["yield_t_ha"]), 2),
            "cost_usd_t": round(float(r["cost_usd_t"]), 2),
            "moisture_pct": round(float(r["moisture_pct"]), 1),
            "pest_pressure": round(float(r["pest_pressure"]), 1),
            "driver": driver,
            "driver_deviation_sd": round(devs[driver], 2),
            "reason": (f"{driver} está {abs(devs[driver]):.1f} desvíos estándar "
                       f"{direction} de la mediana de la operación."),
        })
    return {
        "n_records": int(len(h)),
        "contamination": contamination,
        "anomalies": out,
        "note": "Registros que merecen auditoría, no necesariamente errores.",
    }


# ---------------------------------------------------------------------------
# 4. Pronóstico de rendimiento
# ---------------------------------------------------------------------------

FORECAST_FEATURES = ["area_ha", "rain_90d", "tmean_90d", "rh_90d",
                     "pest_pressure", "target_yield", "cycle_days", "month"]


def forecast_yield(db_path=None) -> dict:
    h = _harvest_frame(db_path)
    if len(h) < 25:
        return {"forecast": [], "note": f"Se necesitan al menos 25 cosechas ({len(h)} disponibles)."}

    h = h.copy()
    h["month"] = h["date"].dt.month
    X = h[FORECAST_FEATURES].astype(float)
    X = X.fillna(X.median())
    y = h["yield_t_ha"].astype(float)

    model = RandomForestRegressor(n_estimators=200, min_samples_leaf=2,
                                  random_state=0, n_jobs=-1)
    # Validación cruzada honesta: con pocas cosechas, K-Fold es más estable
    # que una sola partición temporal, y se reporta el MAE fuera de muestra.
    k = min(5, max(2, len(h) // 8))
    pred_cv = cross_val_predict(model, X, y, cv=KFold(k, shuffle=True, random_state=0))
    mae = float(mean_absolute_error(y, pred_cv))
    r2 = float(r2_score(y, pred_cv))
    baseline_mae = float(mean_absolute_error(y, np.full_like(y, y.mean())))

    model.fit(X, y)
    imp = sorted(zip(FORECAST_FEATURES, model.feature_importances_),
                 key=lambda t: -t[1])

    # proyección del próximo ciclo por lote: última observación de cada lote,
    # con la presión de plagas actual estimada por el módulo de plagas.
    last = h.sort_values("date").groupby("field_id").tail(1).copy()
    Xn = last[FORECAST_FEATURES].astype(float)
    Xn = Xn.fillna(X.median())
    last["forecast_t_ha"] = model.predict(Xn)
    last["forecast_tonnage"] = last["forecast_t_ha"] * last["area_ha"]

    rows = []
    for _, r in last.sort_values("forecast_tonnage", ascending=False).iterrows():
        rows.append({
            "field_id": int(r["field_id"]), "field": r["field"], "crop": r["crop"],
            "area_ha": round(float(r["area_ha"]), 1),
            "last_yield_t_ha": round(float(r["yield_t_ha"]), 2),
            "forecast_t_ha": round(float(r["forecast_t_ha"]), 2),
            "forecast_tonnage": round(float(r["forecast_tonnage"]), 1),
            "interval_t_ha": [round(float(r["forecast_t_ha"] - 1.96 * mae), 2),
                              round(float(r["forecast_t_ha"] + 1.96 * mae), 2)],
            "target_yield": round(float(r["target_yield"]), 2),
        })

    return {
        "model": "RandomForest (200 árboles)",
        "cv_folds": k,
        "mae_t_ha": round(mae, 3),
        "r2": round(r2, 3),
        "baseline_mae_t_ha": round(baseline_mae, 3),
        "improvement_vs_mean_pct": round(100 * (baseline_mae - mae) / baseline_mae, 1),
        "feature_importance": [{"feature": f, "importance": round(float(v), 4)} for f, v in imp],
        "forecast": rows,
        "total_forecast_tonnage": round(sum(r["forecast_tonnage"] for r in rows), 1),
        "note": ("El intervalo es ±1.96·MAE fuera de muestra, una banda empírica, "
                 "no un intervalo de predicción formal."),
        "caveat": ("Con el dataset sintético el R² es optimista: 'target_yield' y "
                   "'cycle_days' son constantes por cultivo, así que el modelo "
                   "aprende sobre todo a identificar el cultivo. Con datos reales, "
                   "quita esas dos variables o valida por campaña completa "
                   "(GroupKFold por año) para obtener una cifra creíble."),
    }


def production_series(db_path=None, freq: str = "M") -> dict:
    """Serie temporal de producción y costo, para los gráficos del tablero."""
    h = _harvest_frame(db_path)
    if h.empty:
        return {"series": []}
    g = h.set_index("date").resample("ME" if freq == "M" else "W").agg(
        tonnage=("tonnage", "sum"), area=("area_ha", "sum"),
        cost=("cost_usd", "sum"), moisture=("moisture_pct", "mean"),
    ).fillna(0.0)
    g["yield_t_ha"] = (g["tonnage"] / g["area"].replace(0, np.nan)).fillna(0.0)
    g["cost_usd_t"] = (g["cost"] / g["tonnage"].replace(0, np.nan)).fillna(0.0)
    return {"series": [
        {"period": str(idx.date()), "tonnage": round(float(r["tonnage"]), 1),
         "yield_t_ha": round(float(r["yield_t_ha"]), 2),
         "cost_usd_t": round(float(r["cost_usd_t"]), 2),
         "moisture_pct": round(float(r["moisture"]), 1)}
        for idx, r in g.iterrows() if r["tonnage"] > 0
    ]}
