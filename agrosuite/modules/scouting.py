"""Monitoreo a campo: alta de observaciones desde el celular.

Este módulo es el receptor de la cola offline de la PWA. Su requisito central
es la **idempotencia**: un celular sin señal acumula observaciones y las
reenvía cuando vuelve la conexión, a veces varias veces (la pestaña se
recarga, el usuario toca sincronizar de nuevo, el navegador reintenta el
Background Sync). Cada observación lleva un UUID generado en el dispositivo, y
el índice único sobre esa columna hace que el reenvío sea inofensivo.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
import uuid as uuidlib
from datetime import date, datetime
from pathlib import Path

from .. import db
from ..config import DATA_DIR
from ..seed import PESTS

MEDIA_DIR = DATA_DIR / "media"
MAX_PHOTO_BYTES = 6 * 1024 * 1024

# Firmas de archivo. Se valida el contenido real, no la extensión declarada:
# el cliente es un celular en el campo, no una fuente confiable.
_MAGIC = {
    b"\xff\xd8\xff": "jpg",
    b"\x89PNG\r\n\x1a\n": "png",
    b"RIFF": "webp",          # se confirma con el marcador WEBP en el offset 8
}

# Identificador opaco generado por el cliente. Se acota longitud y charset para
# que sea seguro como clave, sin exigir el formato UUID v4: el cliente puede ser
# un WebView viejo sin crypto.randomUUID, o un integrador externo.
_UUID_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,64}$")


class ValidationError(ValueError):
    """Error de datos del cliente. La API lo traduce a HTTP 400."""


def _sniff(data: bytes) -> str | None:
    for magic, ext in _MAGIC.items():
        if data.startswith(magic):
            if ext == "webp":
                return "webp" if data[8:12] == b"WEBP" else None
            return ext
    return None


def save_photo(data: bytes) -> str:
    """Guarda la foto y devuelve su nombre. Deduplica por hash del contenido."""
    if len(data) > MAX_PHOTO_BYTES:
        raise ValidationError(
            f"La foto supera el máximo de {MAX_PHOTO_BYTES // (1024*1024)} MB.")
    ext = _sniff(data)
    if ext is None:
        raise ValidationError("El archivo no es una imagen JPEG, PNG ni WebP.")
    name = f"{hashlib.sha256(data).hexdigest()[:32]}.{ext}"
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    path = MEDIA_DIR / name
    if not path.exists():
        path.write_bytes(data)
    return name


def decode_photo(payload: str | None) -> bytes | None:
    """Acepta un data URL o base64 pelado, como los manda la PWA."""
    if not payload:
        return None
    if payload.startswith("data:"):
        _, _, payload = payload.partition(",")
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise ValidationError("La foto no está codificada en base64 válido.")


def catalog(db_path=None) -> dict:
    """Todo lo que la PWA necesita para funcionar sin conexión.

    Se descarga y se guarda en el dispositivo, de modo que el formulario de
    monitoreo se pueda completar en un lote sin señal.
    """
    with db.session(db_path) as conn:
        fields = db.query(conn, """
            SELECT f.id, f.name, f.region, f.area_ha, f.lat, f.lon,
                   c.name AS crop, f.planting_date
            FROM fields f JOIN crops c ON c.id = f.crop_id ORDER BY f.name
        """)
        scouts = [r["scouted_by"] for r in db.query(conn, """
            SELECT scouted_by, COUNT(*) n FROM pest_observations
            GROUP BY scouted_by ORDER BY n DESC LIMIT 20
        """)]
    species = [
        {"species": name, "common": p["common"], "hosts": list(p["hosts"]),
         "economic_threshold": p["economic_threshold"]}
        for name, p in PESTS.items()
    ]
    actions = ["Aplicación selectiva", "Aplicación total", "Monitoreo intensificado",
               "Liberación de Trichogramma", "Sin acción"]
    return {"fields": fields, "species": species, "scouts": scouts,
            "actions": actions, "version": date.today().isoformat()}


def _validate(obs: dict, valid_fields: set[int]) -> dict:
    try:
        field_id = int(obs.get("field_id"))
    except (TypeError, ValueError):
        raise ValidationError("field_id debe ser un entero.")
    if field_id not in valid_fields:
        raise ValidationError(f"El lote {field_id} no existe.")

    species = str(obs.get("species") or "").strip()
    if species not in PESTS:
        raise ValidationError(f"Especie desconocida: '{species}'.")

    try:
        incidence = float(obs.get("incidence_pct"))
    except (TypeError, ValueError):
        raise ValidationError("incidence_pct debe ser numérico.")
    if not 0.0 <= incidence <= 100.0:
        raise ValidationError("incidence_pct debe estar entre 0 y 100.")

    raw_date = str(obs.get("date") or date.today().isoformat())[:10]
    try:
        d = date.fromisoformat(raw_date)
    except ValueError:
        raise ValidationError(f"Fecha inválida: '{raw_date}'. Se espera AAAA-MM-DD.")
    if d > date.today():
        raise ValidationError("La fecha del monitoreo no puede ser futura.")

    def coord(key, lo, hi):
        v = obs.get(key)
        if v in (None, ""):
            return None
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ValidationError(f"{key} debe ser numérico.")
        if not lo <= v <= hi:
            raise ValidationError(f"{key} fuera de rango.")
        return v

    cu = str(obs.get("client_uuid") or "").strip() or str(uuidlib.uuid4())
    if not _UUID_RE.match(cu):
        raise ValidationError("client_uuid tiene un formato inválido.")

    return {
        "field_id": field_id,
        "date": d.isoformat(),
        "species": species,
        "incidence_pct": round(incidence, 2),
        "scouted_by": (str(obs.get("scouted_by") or "Sin identificar").strip())[:80],
        "action_taken": (str(obs.get("action_taken")).strip()[:120]
                         if obs.get("action_taken") else None),
        "lat": coord("lat", -90, 90),
        "lon": coord("lon", -180, 180),
        "client_uuid": cu,
        "source": "campo",
    }


def record_batch(observations: list[dict], db_path=None) -> dict:
    """Da de alta un lote de observaciones. Idempotente por client_uuid.

    Nunca falla el lote completo por una observación mala: procesa lo válido y
    devuelve el detalle de lo rechazado. Un celular no debe quedarse con la
    cola trabada para siempre por un registro corrupto.
    """
    if not isinstance(observations, list) or not observations:
        raise ValidationError("Se espera una lista de observaciones no vacía.")
    if len(observations) > 500:
        raise ValidationError("Máximo 500 observaciones por envío.")

    with db.session(db_path) as conn:
        valid_fields = {r["id"] for r in db.query(conn, "SELECT id FROM fields")}
        accepted, duplicated, rejected = [], [], []

        for i, raw in enumerate(observations):
            if not isinstance(raw, dict):
                rejected.append({"index": i, "error": "Cada observación debe ser un objeto."})
                continue
            try:
                row = _validate(raw, valid_fields)
                photo = decode_photo(raw.get("photo"))
                row["photo"] = save_photo(photo) if photo else None
            except ValidationError as e:
                rejected.append({"index": i, "client_uuid": raw.get("client_uuid"),
                                 "error": str(e)})
                continue

            row["created_at"] = datetime.now().isoformat(timespec="seconds")
            cols = list(row)
            # El INSERT se apoya en el índice único, no en un SELECT previo.
            # Cuando vuelve la señal, la página y el service worker pueden
            # sincronizar a la vez: entre un SELECT de comprobación y su INSERT
            # cabe perfectamente el INSERT del otro. Dejar que la base decida
            # es la única forma de que la idempotencia sea real bajo
            # concurrencia. ON CONFLICT DO NOTHING deja rowcount en 0.
            cur = conn.execute(
                f"INSERT INTO pest_observations ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' for _ in cols)}) "
                # El índice único es parcial (ignora los NULL de los registros
                # históricos), y SQLite sólo lo reconoce como destino del
                # upsert si se repite su predicado acá.
                f"ON CONFLICT(client_uuid) WHERE client_uuid IS NOT NULL DO NOTHING",
                [row[c] for c in cols])
            if cur.rowcount:
                accepted.append({"client_uuid": row["client_uuid"], "id": cur.lastrowid})
            else:
                prev = db.one(conn, "SELECT id FROM pest_observations WHERE client_uuid = ?",
                              (row["client_uuid"],))
                duplicated.append({"client_uuid": row["client_uuid"],
                                   "id": prev["id"] if prev else None})

    return {
        "accepted": accepted, "duplicated": duplicated, "rejected": rejected,
        "counts": {"accepted": len(accepted), "duplicated": len(duplicated),
                   "rejected": len(rejected)},
        "server_time": datetime.now().isoformat(timespec="seconds"),
    }


def recent(db_path=None, limit: int = 50, source: str | None = None) -> dict:
    sql = """SELECT o.id, o.field_id, f.name AS field, o.date, o.species,
                    o.incidence_pct, o.scouted_by, o.action_taken, o.lat, o.lon,
                    o.photo, o.source, o.created_at
             FROM pest_observations o JOIN fields f ON f.id = o.field_id"""
    params: tuple = ()
    if source:
        sql += " WHERE o.source = ?"
        params = (source,)
    sql += " ORDER BY COALESCE(o.created_at, o.date) DESC, o.id DESC LIMIT ?"
    with db.session(db_path) as conn:
        rows = db.query(conn, sql, params + (limit,))
    for r in rows:
        p = PESTS.get(r["species"])
        r["common_name"] = p["common"] if p else r["species"]
        r["over_threshold"] = bool(p and r["incidence_pct"] >= p["economic_threshold"])
    return {"observations": rows, "count": len(rows)}
