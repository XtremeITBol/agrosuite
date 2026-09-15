"""Cliente de clima con degradación elegante.

Open-Meteo es gratuito, sin API key y con límite generoso — encaja con el
requisito de bajo costo. Si no hay red (o `AGROSUITE_WEATHER_ONLINE=0`), el
sistema usa el histórico almacenado en la base: la app nunca se cae por una
dependencia externa.
"""
from __future__ import annotations

from datetime import date, timedelta

from .. import db
from ..config import settings

try:  # httpx es opcional: sin él, el modo offline sigue funcionando
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore


ARCHIVE_FIELDS = "temperature_2m_min,temperature_2m_max,precipitation_sum,relative_humidity_2m_mean,wind_speed_10m_max"


def fetch_open_meteo(lat: float, lon: float, start: date, end: date,
                     forecast: bool = False) -> list[dict]:
    """Descarga clima diario. Devuelve [] si no hay red o el módulo httpx."""
    if httpx is None:
        return []
    url = settings.weather_forecast_url if forecast else settings.weather_api_url
    params = {
        "latitude": lat, "longitude": lon, "daily": ARCHIVE_FIELDS,
        "timezone": "America/La_Paz",
    }
    if forecast:
        params["forecast_days"] = min(16, (end - date.today()).days + 1)
    else:
        params["start_date"] = start.isoformat()
        params["end_date"] = end.isoformat()
    try:
        r = httpx.get(url, params=params, timeout=settings.weather_timeout_s)
        r.raise_for_status()
        d = r.json()["daily"]
    except Exception:
        return []

    out = []
    for i, day in enumerate(d.get("time", [])):
        def g(key, default=0.0):
            v = d.get(key, [])
            return v[i] if i < len(v) and v[i] is not None else default
        out.append({
            "date": day,
            "tmin_c": g("temperature_2m_min", 15.0),
            "tmax_c": g("temperature_2m_max", 28.0),
            "rain_mm": g("precipitation_sum", 0.0),
            "rh_pct": g("relative_humidity_2m_mean", 70.0),
            "wind_kmh": g("wind_speed_10m_max", 10.0),
        })
    return out


def sync_region(region: str, lat: float, lon: float, days_back: int = 365,
                db_path=None) -> dict:
    """Actualiza el histórico y el pronóstico de una región desde Open-Meteo.

    Escribe con UPSERT, así que es seguro ejecutarlo a diario desde un cron.
    """
    if not settings.weather_online:
        return {"region": region, "synced": 0, "source": "offline",
                "detail": "AGROSUITE_WEATHER_ONLINE=0 — se usa el histórico local."}

    today = date.today()
    rows = fetch_open_meteo(lat, lon, today - timedelta(days=days_back),
                            today - timedelta(days=1))
    rows += fetch_open_meteo(lat, lon, today, today + timedelta(days=14), forecast=True)
    if not rows:
        return {"region": region, "synced": 0, "source": "offline",
                "detail": "Open-Meteo no respondió; se mantiene el histórico local."}

    with db.session(db_path) as conn:
        for r in rows:
            conn.execute("""
                INSERT INTO weather_daily (region, date, tmin_c, tmax_c, rain_mm,
                                           rh_pct, wind_kmh, source)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(region, date) DO UPDATE SET
                    tmin_c=excluded.tmin_c, tmax_c=excluded.tmax_c,
                    rain_mm=excluded.rain_mm, rh_pct=excluded.rh_pct,
                    wind_kmh=excluded.wind_kmh, source=excluded.source
            """, (region, r["date"], r["tmin_c"], r["tmax_c"], r["rain_mm"],
                  r["rh_pct"], r["wind_kmh"],
                  "open-meteo-forecast" if r["date"] >= today.isoformat() else "open-meteo"))
    return {"region": region, "synced": len(rows), "source": "open-meteo"}


def sync_all(db_path=None) -> list[dict]:
    from ..seed import REGIONS
    return [sync_region(name, lat, lon, db_path=db_path)
            for name, (lat, lon) in REGIONS.items()]
