"""API REST de AgroSuite (Flask) + servidor del tablero.

Cada módulo se expone bajo su propio prefijo. Las respuestas son JSON plano,
así que cualquier frontend (web, móvil, Power BI, un bot de WhatsApp) puede
consumirlas sin acoplarse a la implementación.
"""
from __future__ import annotations

import logging
import traceback
from datetime import date
from functools import wraps

from flask import Blueprint, Flask, jsonify, request, send_from_directory

from . import certs, db, net
from .config import DATA_DIR, WEB_DIR, settings
from .modules import analytics, chatbot, pest, scouting, supply_chain
from .integrations import weather

log = logging.getLogger("agrosuite")


def _json_error(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except (ValueError, scouting.ValidationError) as e:
            return jsonify({"error": str(e), "type": "validacion"}), 400
        except Exception as e:                       # pragma: no cover
            log.error("Fallo en %s: %s", fn.__name__, traceback.format_exc())
            return jsonify({"error": str(e), "type": "interno"}), 500
    return wrapper


def _int_arg(name: str, default: int, lo: int, hi: int) -> int:
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"El parámetro '{name}' debe ser un entero.")
    if not lo <= v <= hi:
        raise ValueError(f"El parámetro '{name}' debe estar entre {lo} y {hi}.")
    return v


def _float_arg(name: str, default: float, lo: float, hi: float) -> float:
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"El parámetro '{name}' debe ser numérico.")
    if not lo <= v <= hi:
        raise ValueError(f"El parámetro '{name}' debe estar entre {lo} y {hi}.")
    return v


# ---------------------------------------------------------------------------
# Blueprints
# ---------------------------------------------------------------------------

core = Blueprint("core", __name__)
sc_bp = Blueprint("supply", __name__, url_prefix="/api/supply")
pest_bp = Blueprint("pest", __name__, url_prefix="/api/pest")
bot_bp = Blueprint("bot", __name__, url_prefix="/api/chat")
an_bp = Blueprint("analytics", __name__, url_prefix="/api/analytics")
field_bp = Blueprint("field", __name__, url_prefix="/api/field")


@core.get("/api/health")
def health():
    with db.session() as conn:
        counts = {t: db.one(conn, f"SELECT COUNT(*) n FROM {t}")["n"] for t in
                  ("fields", "harvests", "pest_observations", "weather_daily",
                   "orders", "tickets", "inventory")}
    return jsonify({"status": "ok", "date": date.today().isoformat(),
                    "db": str(settings.db_path), "records": counts,
                    "pest_model_trained": pest.MODEL_PATH.exists()})


@core.get("/api/fields")
@_json_error
def fields():
    with db.session() as conn:
        rows = db.query(conn, """
            SELECT f.*, c.name AS crop, c.target_yield
            FROM fields f JOIN crops c ON c.id = f.crop_id ORDER BY f.id
        """)
    return jsonify({"fields": rows, "count": len(rows)})


@core.post("/api/weather/sync")
@_json_error
def weather_sync():
    return jsonify({"results": weather.sync_all()})


# ---- cadena de suministro -------------------------------------------------

@sc_bp.get("/network")
@_json_error
def network():
    horizon = _int_arg("horizon_days", 120, 1, 3650)
    rate = _float_arg("freight_rate", supply_chain.FREIGHT_USD_T_KM, 0.001, 5.0)
    return jsonify(supply_chain.optimize_network(horizon_days=horizon, freight_rate=rate).to_dict())


@sc_bp.get("/routes")
@_json_error
def routes():
    return jsonify(supply_chain.plan_routes(
        horizon_days=_int_arg("horizon_days", 30, 1, 3650),
        trips_per_day=_int_arg("trips_per_day", 2, 1, 6)))


@sc_bp.get("/inventory")
@_json_error
def inventory():
    return jsonify(supply_chain.inventory_policy(
        service_level=_float_arg("service_level", 0.95, 0.5, 0.999)))


# ---- plagas ---------------------------------------------------------------

@pest_bp.get("/risk")
@_json_error
def pest_risk():
    fid = request.args.get("field_id", type=int)
    return jsonify(pest.predict_risk(field_id=fid))


@pest_bp.post("/train")
@_json_error
def pest_train():
    _, report = pest.train()
    return jsonify(report.to_dict())


@pest_bp.get("/observations")
@_json_error
def pest_obs():
    limit = _int_arg("limit", 200, 1, 5000)
    fid = request.args.get("field_id", type=int)
    with db.session() as conn:
        sql = """SELECT o.*, f.name AS field FROM pest_observations o
                 JOIN fields f ON f.id = o.field_id"""
        params: tuple = ()
        if fid:
            sql += " WHERE o.field_id = ?"
            params = (fid,)
        sql += " ORDER BY o.date DESC LIMIT ?"
        rows = db.query(conn, sql, params + (limit,))
    return jsonify({"observations": rows, "count": len(rows)})


# ---- chatbot --------------------------------------------------------------

@bot_bp.post("/message")
@_json_error
def chat_message():
    payload = request.get_json(silent=True) or {}
    msg = (payload.get("message") or "").strip()
    if not msg:
        raise ValueError("El campo 'message' es obligatorio.")
    if len(msg) > 2000:
        raise ValueError("El mensaje excede los 2000 caracteres.")
    reply = chatbot.get_bot().reply(
        msg,
        customer_id=payload.get("customer_id"),
        channel=payload.get("channel", "web"),
        persist=bool(payload.get("persist", True)),
    )
    return jsonify(reply.to_dict())


@bot_bp.get("/stats")
@_json_error
def chat_stats():
    return jsonify(chatbot.stats())


@bot_bp.post("/train")
@_json_error
def chat_train():
    return jsonify(chatbot.get_bot().fit())


@bot_bp.get("/kb")
@_json_error
def chat_kb():
    with db.session() as conn:
        return jsonify({"articles": db.query(conn, "SELECT * FROM kb_articles ORDER BY intent")})


# ---- analítica ------------------------------------------------------------

@an_bp.get("/kpis")
@_json_error
def an_kpis():
    return jsonify(analytics.kpis(window_days=_int_arg("window_days", 180, 7, 3650)))


@an_bp.get("/yield")
@_json_error
def an_yield():
    return jsonify(analytics.yield_by_field())


@an_bp.get("/anomalies")
@_json_error
def an_anomalies():
    return jsonify(analytics.anomalies(
        contamination=_float_arg("contamination", 0.08, 0.01, 0.4)))


@an_bp.get("/forecast")
@_json_error
def an_forecast():
    return jsonify(analytics.forecast_yield())


@an_bp.get("/series")
@_json_error
def an_series():
    freq = request.args.get("freq", "M")
    if freq not in ("M", "W"):
        raise ValueError("El parámetro 'freq' debe ser 'M' o 'W'.")
    return jsonify(analytics.production_series(freq=freq))


# ---- monitoreo a campo (PWA) ---------------------------------------------

@field_bp.get("/catalog")
@_json_error
def field_catalog():
    """Datos maestros que la PWA guarda para operar sin conexión."""
    return jsonify(scouting.catalog())


@field_bp.post("/observations")
@_json_error
def field_observations():
    """Alta de monitoreo. Acepta una observación o un lote (cola offline).

    Idempotente por `client_uuid`: reenviar la misma cola no duplica nada.
    """
    payload = request.get_json(silent=True)
    if payload is None:
        raise ValueError("Se espera un cuerpo JSON.")
    batch = payload.get("observations") if isinstance(payload, dict) else payload
    if isinstance(batch, dict):
        batch = [batch]
    if not isinstance(batch, list):
        batch = [payload]
    result = scouting.record_batch(batch)
    # 207 cuando el lote fue parcial: el cliente debe mirar el detalle.
    status = 207 if result["counts"]["rejected"] else 200
    return jsonify(result), status


@field_bp.get("/observations")
@_json_error
def field_recent():
    return jsonify(scouting.recent(
        limit=_int_arg("limit", 50, 1, 500),
        source=request.args.get("source")))


@field_bp.get("/server-info")
@_json_error
def server_info():
    """Direcciones y estado del certificado, para configurar los celulares."""
    cert = DATA_DIR / "certs" / "server.crt"
    info = {"lan_ips": net.all_lan_ips(), "port": settings.port,
            "https": settings.use_https, "ca_url": "/agrosuite-ca.crt"}
    if cert.exists():
        try:
            info["certificate"] = certs.describe(cert)
        except Exception:
            pass
    scheme = "https" if settings.use_https else "http"
    info["urls"] = [f"{scheme}://{ip}:{settings.port}/campo" for ip in info["lan_ips"]]
    return jsonify(info)


# ---------------------------------------------------------------------------
# Fábrica de la app
# ---------------------------------------------------------------------------

def create_app(db_path=None) -> Flask:
    if db_path:
        settings.db_path = db_path
    db.init_db(settings.db_path)

    app = Flask(__name__, static_folder=None)
    app.json.ensure_ascii = False
    app.json.sort_keys = False
    # Las fotos de monitoreo llegan en base64 dentro del JSON de sincronización.
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024
    for bp in (core, sc_bp, pest_bp, bot_bp, an_bp, field_bp):
        app.register_blueprint(bp)

    @app.get("/")
    def index():
        return send_from_directory(WEB_DIR, "index.html")

    @app.get("/campo")
    def campo():
        """Pantalla de monitoreo a campo (la que se instala en el celular)."""
        return send_from_directory(WEB_DIR, "campo.html")

    @app.get("/agrosuite-ca.crt")
    def download_ca():
        """Certificado de la CA local, para instalar en los celulares."""
        ca = DATA_DIR / "certs" / "agrosuite-ca.crt"
        if not ca.exists():
            return jsonify({"error": "No hay CA generada. Arrancá con --https."}), 404
        # El tipo MIME importa: Android sólo ofrece instalar el certificado
        # cuando lo reconoce como tal.
        return send_from_directory(ca.parent, ca.name,
                                   mimetype="application/x-x509-ca-cert",
                                   as_attachment=True,
                                   download_name="agrosuite-ca.crt")

    @app.get("/media/<path:filename>")
    def media(filename):
        return send_from_directory(scouting.MEDIA_DIR, filename)

    @app.get("/<path:filename>")
    def static_files(filename):
        return send_from_directory(WEB_DIR, filename)

    @app.after_request
    def headers(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
        # El service worker debe poder controlar todo el sitio, no sólo /static.
        if request.path.endswith("sw.js"):
            resp.headers["Service-Worker-Allowed"] = "/"
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.errorhandler(413)
    def too_large(e):
        return jsonify({"error": "El envío excede el tamaño máximo permitido.",
                        "type": "validacion"}), 413

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"error": "Recurso no encontrado", "path": request.path}), 404

    return app
