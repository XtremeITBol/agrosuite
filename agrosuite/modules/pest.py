"""Módulo 2 — Predicción de plagas.

Enfoque híbrido, que es lo que funciona en agronomía:

* **Capa agronómica** — acumulación de grados-día (GDD) por especie desde la
  siembra. Es un modelo fenológico validado en literatura: la plaga necesita
  cierta suma térmica para completar estadios y alcanzar densidad de daño.
  Es interpretable y funciona sin datos históricos, así que sirve desde el
  día uno.

* **Capa de aprendizaje** — un gradient boosting sobre variables climáticas
  agregadas (ventanas móviles de 7/14/30 días), fenología del cultivo y la
  última lectura de monitoreo. Aprende las interacciones que la regla de GDD
  sola no captura.

La predicción es operativa, no académica: **probabilidad de superar el umbral
de daño económico en los próximos 14 días**, que es la decisión real que toma
el productor ("¿aplico o no aplico?").

La validación usa una partición **temporal** (entrena con el pasado, evalúa
con el futuro). Una partición aleatoria en series de tiempo infla las métricas
y no diría nada útil.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import (average_precision_score, brier_score_loss,
                             precision_recall_fscore_support, roc_auc_score)

from .. import db
from ..config import DATA_DIR
from ..seed import PESTS

HORIZON_DAYS = 14           # ventana de anticipación de la alerta
MODEL_PATH = DATA_DIR / "pest_model.joblib"

FEATURES = [
    "ddays", "days_since_planting", "phenology_ratio",
    "tmax_7", "tmin_7", "trange_7", "rh_7", "rain_7", "wet_days_7",
    "tmax_14", "rh_14", "rain_14", "wet_days_14",
    "tmax_30", "rh_30", "rain_30", "wet_days_30",
    "rh_gap_optimum", "lag_incidence", "is_host", "month_sin", "month_cos",
]


# ---------------------------------------------------------------------------
# Construcción del dataset
# ---------------------------------------------------------------------------

def _weather_frame(conn) -> dict[str, pd.DataFrame]:
    w = pd.DataFrame(db.query(conn, "SELECT * FROM weather_daily ORDER BY region, date"))
    if w.empty:
        return {}
    w["date"] = pd.to_datetime(w["date"])
    w["tmean_c"] = (w["tmin_c"] + w["tmax_c"]) / 2
    w["trange_c"] = w["tmax_c"] - w["tmin_c"]
    # Proxy de mojado foliar: días con humedad relativa alta, motor de la roya.
    w["wet_day"] = ((w["rh_pct"] >= 85) | (w["rain_mm"] >= 2.0)).astype(float)

    out = {}
    for region, g in w.groupby("region"):
        g = g.sort_values("date").set_index("date")
        for win in (7, 14, 30):
            g[f"tmax_{win}"] = g["tmax_c"].rolling(win, min_periods=2).mean()
            g[f"tmin_{win}"] = g["tmin_c"].rolling(win, min_periods=2).mean()
            g[f"trange_{win}"] = g["trange_c"].rolling(win, min_periods=2).mean()
            g[f"rh_{win}"] = g["rh_pct"].rolling(win, min_periods=2).mean()
            g[f"rain_{win}"] = g["rain_mm"].rolling(win, min_periods=2).sum()
            g[f"wet_days_{win}"] = g["wet_day"].rolling(win, min_periods=2).sum()
        out[region] = g
    return out


def _gdd_series(g: pd.DataFrame, base_temp: float, planting: pd.Timestamp) -> pd.Series:
    """Grados-día acumulados desde la siembra (método promedio simple)."""
    mask = g.index >= planting
    daily = (g.loc[mask, "tmean_c"] - base_temp).clip(lower=0)
    return daily.cumsum()


def build_dataset(db_path=None) -> pd.DataFrame:
    """Una fila por (lote, especie, fecha de monitoreo) con features y target."""
    with db.session(db_path) as conn:
        obs = pd.DataFrame(db.query(conn, """
            SELECT o.field_id, o.date, o.species, o.incidence_pct,
                   f.region, f.planting_date, c.name AS crop
            FROM pest_observations o
            JOIN fields f ON f.id = o.field_id
            JOIN crops  c ON c.id = f.crop_id
            ORDER BY o.field_id, o.species, o.date
        """))
        wx = _weather_frame(conn)

    if obs.empty or not wx:
        return pd.DataFrame(columns=FEATURES + ["target", "date", "field_id", "species"])

    obs["date"] = pd.to_datetime(obs["date"])
    obs["planting_date"] = pd.to_datetime(obs["planting_date"])

    rows = []
    for (fid, species), grp in obs.groupby(["field_id", "species"]):
        pest = PESTS.get(species)
        if pest is None:
            continue
        grp = grp.sort_values("date").reset_index(drop=True)
        region = grp.loc[0, "region"]
        g = wx.get(region)
        if g is None:
            continue
        planting = grp.loc[0, "planting_date"]
        gdd = _gdd_series(g, pest["base_temp"], planting)
        crop = grp.loc[0, "crop"]
        is_host = float(crop in pest["hosts"])

        inc = grp["incidence_pct"].to_numpy()
        dates = grp["date"].to_numpy()

        for i, d in enumerate(grp["date"]):
            if d not in g.index:
                continue
            w = g.loc[d]
            # target: ¿se supera el umbral económico en los próximos 14 días?
            future = inc[(dates > np.datetime64(d)) &
                         (dates <= np.datetime64(d + timedelta(days=HORIZON_DAYS)))]
            if future.size == 0:
                continue
            target = int(future.max() >= pest["economic_threshold"])

            days_sp = (d - planting).days
            rows.append({
                "field_id": fid, "species": species, "date": d, "crop": crop,
                "region": region,
                "ddays": float(gdd.get(d, 0.0)),
                "days_since_planting": float(days_sp),
                "phenology_ratio": float(gdd.get(d, 0.0)) / pest["ddays_threshold"],
                "tmax_7": w["tmax_7"], "tmin_7": w["tmin_7"], "trange_7": w["trange_7"],
                "rh_7": w["rh_7"], "rain_7": w["rain_7"], "wet_days_7": w["wet_days_7"],
                "tmax_14": w["tmax_14"], "rh_14": w["rh_14"], "rain_14": w["rain_14"],
                "wet_days_14": w["wet_days_14"],
                "tmax_30": w["tmax_30"], "rh_30": w["rh_30"], "rain_30": w["rain_30"],
                "wet_days_30": w["wet_days_30"],
                "rh_gap_optimum": abs(float(w["rh_7"]) - pest["rh_optimum"]),
                # última lectura de monitoreo disponible (no la del futuro)
                "lag_incidence": float(inc[i]),
                "is_host": is_host,
                "month_sin": math.sin(2 * math.pi * d.month / 12),
                "month_cos": math.cos(2 * math.pi * d.month / 12),
                "target": target,
            })

    df = pd.DataFrame(rows)
    return df.dropna(subset=["rh_7", "tmax_7"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Entrenamiento y evaluación
# ---------------------------------------------------------------------------

@dataclass
class TrainReport:
    n_train: int
    n_test: int
    positive_rate: float
    split_date: str
    roc_auc: float
    pr_auc: float
    brier: float
    precision: float
    recall: float
    f1: float
    baseline_persistence_auc: float
    baseline_gdd_auc: float
    top_features: list[dict]

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _permutation_importance(model, X: pd.DataFrame, y: np.ndarray,
                            n_repeats: int = 4, seed: int = 0) -> list[dict]:
    rng = np.random.default_rng(seed)
    base = roc_auc_score(y, model.predict_proba(X)[:, 1])
    out = []
    for col in X.columns:
        drops = []
        for _ in range(n_repeats):
            Xp = X.copy()
            Xp[col] = rng.permutation(Xp[col].to_numpy())
            drops.append(base - roc_auc_score(y, model.predict_proba(Xp)[:, 1]))
        out.append({"feature": col, "auc_drop": round(float(np.mean(drops)), 4)})
    return sorted(out, key=lambda r: -r["auc_drop"])[:8]


def train(db_path=None, test_fraction: float = 0.25,
          save: bool = True) -> tuple[HistGradientBoostingClassifier, TrainReport]:
    df = build_dataset(db_path)
    if len(df) < 200:
        raise ValueError(f"Datos insuficientes para entrenar: {len(df)} filas.")

    # --- partición TEMPORAL, no aleatoria ---
    df = df.sort_values("date").reset_index(drop=True)
    cut = df["date"].quantile(1 - test_fraction)
    train_df, test_df = df[df["date"] <= cut], df[df["date"] > cut]
    if test_df["target"].nunique() < 2 or len(test_df) < 30:
        cut = df["date"].quantile(0.7)
        train_df, test_df = df[df["date"] <= cut], df[df["date"] > cut]

    Xtr, ytr = train_df[FEATURES], train_df["target"].to_numpy()
    Xte, yte = test_df[FEATURES], test_df["target"].to_numpy()

    model = HistGradientBoostingClassifier(
        max_iter=350, learning_rate=0.06, max_leaf_nodes=24,
        min_samples_leaf=25, l2_regularization=1.0,
        early_stopping=True, validation_fraction=0.15, random_state=0,
    )
    model.fit(Xtr, ytr)

    proba = model.predict_proba(Xte)[:, 1]
    pred = (proba >= 0.5).astype(int)
    prec, rec, f1, _ = precision_recall_fscore_support(
        yte, pred, average="binary", zero_division=0)

    # --- baselines honestos ---
    def safe_auc(y, s):
        return float(roc_auc_score(y, s)) if len(np.unique(y)) > 1 else float("nan")

    base_persistence = safe_auc(yte, test_df["lag_incidence"].to_numpy())
    base_gdd = safe_auc(yte, test_df["phenology_ratio"].to_numpy())

    report = TrainReport(
        n_train=len(train_df), n_test=len(test_df),
        positive_rate=round(float(df["target"].mean()), 4),
        split_date=str(pd.Timestamp(cut).date()),
        roc_auc=round(safe_auc(yte, proba), 4),
        pr_auc=round(float(average_precision_score(yte, proba)), 4),
        brier=round(float(brier_score_loss(yte, proba)), 4),
        precision=round(float(prec), 4), recall=round(float(rec), 4),
        f1=round(float(f1), 4),
        baseline_persistence_auc=round(base_persistence, 4),
        baseline_gdd_auc=round(base_gdd, 4),
        top_features=_permutation_importance(model, Xte, yte),
    )

    if save:
        import joblib
        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"model": model, "features": FEATURES,
                     "trained_at": date.today().isoformat(),
                     "report": report.to_dict()}, MODEL_PATH)
    return model, report


def _load_model():
    if not MODEL_PATH.exists():
        return None
    import joblib
    try:
        return joblib.load(MODEL_PATH)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Inferencia: riesgo actual por lote y especie
# ---------------------------------------------------------------------------

def _risk_band(p: float) -> str:
    return "ALTO" if p >= 0.65 else "MEDIO" if p >= 0.35 else "BAJO"


def _recommendation(species: str, p: float, ddays: float, pest: dict,
                    last_inc: float) -> str:
    if p >= 0.65:
        if species == "Phakopsora pachyrhizi":
            return ("Aplicar fungicida preventivo (mezcla triazol + estrobilurina) "
                    "en las próximas 72 h y programar repetición a 18-21 días.")
        return (f"Monitoreo dirigido inmediato en 10 puntos por lote. "
                f"Si se confirma ≥{pest['economic_threshold']}% de incidencia, aplicar "
                f"insecticida selectivo y rotar modo de acción respecto a la última aplicación.")
    if p >= 0.35:
        return ("Intensificar el monitoreo a dos veces por semana. Preparar el "
                "producto y la logística de aplicación, pero no aplicar todavía.")
    return "Monitoreo semanal de rutina. Sin acción de control justificada."


def predict_risk(db_path=None, field_id: int | None = None,
                 auto_train: bool = True) -> dict:
    """Riesgo a 14 días por lote y especie, con explicación agronómica."""
    bundle = _load_model()
    if bundle is None and auto_train:
        try:
            train(db_path)
            bundle = _load_model()
        except Exception:
            bundle = None
    model = bundle["model"] if bundle else None

    with db.session(db_path) as conn:
        where = "WHERE f.id = ?" if field_id else ""
        params = (field_id,) if field_id else ()
        fields = db.query(conn, f"""
            SELECT f.*, c.name AS crop, c.base_temp_c, c.cycle_days
            FROM fields f JOIN crops c ON c.id = f.crop_id {where}
            ORDER BY f.id
        """, params)
        wx = _weather_frame(conn)
        last_obs = db.query(conn, """
            SELECT field_id, species, MAX(date) AS date, incidence_pct
            FROM pest_observations GROUP BY field_id, species
        """)
    last_map = {(r["field_id"], r["species"]): r for r in last_obs}

    today = pd.Timestamp(date.today())
    alerts, feats_batch, meta = [], [], []

    for f in fields:
        g = wx.get(f["region"])
        if g is None or g.empty:
            continue
        # se evalúa contra el último día con dato (histórico o pronóstico)
        ref = g.index[g.index <= today]
        if len(ref) == 0:
            continue
        d = ref[-1]
        w = g.loc[d]
        planting = pd.Timestamp(f["planting_date"])

        for species, pest in PESTS.items():
            is_host = float(f["crop"] in pest["hosts"])
            if not is_host:
                continue
            gdd = _gdd_series(g, pest["base_temp"], planting)
            ddays = float(gdd.get(d, 0.0))
            lo = last_map.get((f["id"], species))
            lag = float(lo["incidence_pct"]) if lo else 0.0
            feats_batch.append({
                "ddays": ddays,
                "days_since_planting": float((d - planting).days),
                "phenology_ratio": ddays / pest["ddays_threshold"],
                "tmax_7": w["tmax_7"], "tmin_7": w["tmin_7"], "trange_7": w["trange_7"],
                "rh_7": w["rh_7"], "rain_7": w["rain_7"], "wet_days_7": w["wet_days_7"],
                "tmax_14": w["tmax_14"], "rh_14": w["rh_14"], "rain_14": w["rain_14"],
                "wet_days_14": w["wet_days_14"],
                "tmax_30": w["tmax_30"], "rh_30": w["rh_30"], "rain_30": w["rain_30"],
                "wet_days_30": w["wet_days_30"],
                "rh_gap_optimum": abs(float(w["rh_7"]) - pest["rh_optimum"]),
                "lag_incidence": lag, "is_host": is_host,
                "month_sin": math.sin(2 * math.pi * d.month / 12),
                "month_cos": math.cos(2 * math.pi * d.month / 12),
            })
            meta.append({
                "field_id": f["id"], "field": f["name"], "region": f["region"],
                "crop": f["crop"], "area_ha": f["area_ha"],
                "species": species, "common_name": pest["common"],
                "ddays": round(ddays, 0), "ddays_threshold": pest["ddays_threshold"],
                "economic_threshold_pct": pest["economic_threshold"],
                "last_scouting": lo["date"] if lo else None,
                "last_incidence_pct": round(lag, 1),
                "rh_7d": round(float(w["rh_7"]), 1),
                "rain_7d": round(float(w["rain_7"]), 1),
                "wet_days_14d": int(w["wet_days_14"]),
                "pest": pest,
            })

    if not meta:
        return {"generated_at": date.today().isoformat(), "model": "ninguno",
                "alerts": [], "summary": {"ALTO": 0, "MEDIO": 0, "BAJO": 0}}

    X = pd.DataFrame(feats_batch)[FEATURES]
    if model is not None:
        probs = model.predict_proba(X)[:, 1]
        engine = "gradient boosting + grados-día"
    else:
        # Respaldo puramente agronómico: logística sobre grados-día modulada
        # por humedad. Permite operar sin histórico de entrenamiento.
        probs = []
        for m, row in zip(meta, feats_batch):
            pest = m["pest"]
            phen = 1 / (1 + math.exp(-0.012 * (m["ddays"] - pest["ddays_threshold"])))
            hum = math.exp(-((row["rh_7"] - pest["rh_optimum"]) ** 2) / (2 * 14.0 ** 2))
            probs.append(min(0.99, phen * hum * 1.15))
        probs = np.array(probs)
        engine = "regla de grados-día (modelo no entrenado)"

    for m, p in zip(meta, probs):
        pest = m.pop("pest")
        p = float(p)
        m["risk"] = round(p, 3)
        m["band"] = _risk_band(p)
        m["phenology_pct"] = round(100 * m["ddays"] / m["ddays_threshold"], 0)
        m["recommendation"] = _recommendation(m["species"], p, m["ddays"], pest,
                                              m["last_incidence_pct"])
        m["area_at_risk_ha"] = round(m["area_ha"], 1) if p >= 0.35 else 0.0
        alerts.append(m)

    alerts.sort(key=lambda a: (-a["risk"], -a["area_ha"]))
    summary = {b: sum(1 for a in alerts if a["band"] == b) for b in ("ALTO", "MEDIO", "BAJO")}
    return {
        "generated_at": date.today().isoformat(),
        "horizon_days": HORIZON_DAYS,
        "model": engine,
        "model_trained_at": bundle.get("trained_at") if bundle else None,
        "alerts": alerts,
        "summary": summary,
        "hectares_at_risk": round(sum(a["area_at_risk_ha"] for a in alerts), 1),
    }
