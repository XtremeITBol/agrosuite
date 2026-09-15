"""Capa de acceso a datos sobre SQLite (stdlib, cero dependencias).

Se usa SQLite porque el requisito es bajo costo y arranque desde cero. El
esquema y las consultas son SQL estándar, de modo que migrar a PostgreSQL
más adelante sólo implica cambiar la cadena de conexión y el driver.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .config import settings

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS crops (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    cycle_days      INTEGER NOT NULL,
    base_temp_c     REAL NOT NULL,      -- temperatura base para grados-día
    target_yield    REAL NOT NULL       -- t/ha objetivo
);

CREATE TABLE IF NOT EXISTS fields (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    region          TEXT NOT NULL,
    lat             REAL NOT NULL,
    lon             REAL NOT NULL,
    area_ha         REAL NOT NULL,
    soil_type       TEXT NOT NULL,
    crop_id         INTEGER NOT NULL REFERENCES crops(id),
    planting_date   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fields_region ON fields(region);

CREATE TABLE IF NOT EXISTS weather_daily (
    id              INTEGER PRIMARY KEY,
    region          TEXT NOT NULL,
    date            TEXT NOT NULL,
    tmin_c          REAL NOT NULL,
    tmax_c          REAL NOT NULL,
    rain_mm         REAL NOT NULL,
    rh_pct          REAL NOT NULL,
    wind_kmh        REAL NOT NULL,
    source          TEXT NOT NULL DEFAULT 'synthetic',
    UNIQUE(region, date)
);
CREATE INDEX IF NOT EXISTS ix_weather_date ON weather_daily(date);

CREATE TABLE IF NOT EXISTS harvests (
    id              INTEGER PRIMARY KEY,
    field_id        INTEGER NOT NULL REFERENCES fields(id),
    date            TEXT NOT NULL,
    tonnage         REAL NOT NULL,
    moisture_pct    REAL NOT NULL,
    quality_grade   TEXT NOT NULL,      -- A | B | C
    cost_usd        REAL NOT NULL,
    operator        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_harvests_field ON harvests(field_id);
CREATE INDEX IF NOT EXISTS ix_harvests_date ON harvests(date);

CREATE TABLE IF NOT EXISTS pest_observations (
    id              INTEGER PRIMARY KEY,
    field_id        INTEGER NOT NULL REFERENCES fields(id),
    date            TEXT NOT NULL,
    species         TEXT NOT NULL,
    incidence_pct   REAL NOT NULL,      -- % de plantas afectadas
    scouted_by      TEXT NOT NULL,
    action_taken    TEXT
);
CREATE INDEX IF NOT EXISTS ix_pest_field_date ON pest_observations(field_id, date);

CREATE TABLE IF NOT EXISTS silos (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    lat             REAL NOT NULL,
    lon             REAL NOT NULL,
    capacity_t      REAL NOT NULL,
    stock_t         REAL NOT NULL DEFAULT 0,
    handling_usd_t  REAL NOT NULL       -- costo de acopio por tonelada
);

CREATE TABLE IF NOT EXISTS plants (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL,      -- planta | puerto | acopiador
    lat             REAL NOT NULL,
    lon             REAL NOT NULL,
    demand_t        REAL NOT NULL,
    price_usd_t     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS vehicles (
    id              INTEGER PRIMARY KEY,
    plate           TEXT NOT NULL UNIQUE,
    capacity_t      REAL NOT NULL,
    cost_usd_km     REAL NOT NULL,
    available       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS inventory (
    id                  INTEGER PRIMARY KEY,
    sku                 TEXT NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    category            TEXT NOT NULL,
    unit                TEXT NOT NULL,
    on_hand             REAL NOT NULL,
    unit_cost_usd       REAL NOT NULL,
    lead_time_days      INTEGER NOT NULL,
    daily_usage_mean    REAL NOT NULL,
    daily_usage_std     REAL NOT NULL,
    order_cost_usd      REAL NOT NULL DEFAULT 120,
    holding_rate        REAL NOT NULL DEFAULT 0.22
);

CREATE TABLE IF NOT EXISTS customers (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    email           TEXT,
    segment         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY,
    customer_id     INTEGER NOT NULL REFERENCES customers(id),
    product         TEXT NOT NULL,
    tonnage         REAL NOT NULL,
    price_usd_t     REAL NOT NULL,
    due_date        TEXT NOT NULL,
    status          TEXT NOT NULL       -- pendiente | en_transito | entregado | retrasado
);
CREATE INDEX IF NOT EXISTS ix_orders_customer ON orders(customer_id);

CREATE TABLE IF NOT EXISTS tickets (
    id              INTEGER PRIMARY KEY,
    customer_id     INTEGER REFERENCES customers(id),
    channel         TEXT NOT NULL,      -- whatsapp | web | email | telefono
    message         TEXT NOT NULL,
    intent          TEXT,
    confidence      REAL,
    answer          TEXT,
    escalated       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    resolved_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_tickets_created ON tickets(created_at);

CREATE TABLE IF NOT EXISTS kb_articles (
    id              INTEGER PRIMARY KEY,
    intent          TEXT NOT NULL,
    question        TEXT NOT NULL,
    answer          TEXT NOT NULL
);
"""

# Columnas agregadas después de la primera versión del esquema. Se aplican con
# ALTER TABLE sobre bases existentes para no obligar a regenerar los datos.
MIGRATIONS: list[tuple[str, str, str]] = [
    # (tabla, columna, definición)
    ("pest_observations", "lat",         "REAL"),
    ("pest_observations", "lon",         "REAL"),
    ("pest_observations", "photo",       "TEXT"),
    ("pest_observations", "client_uuid", "TEXT"),
    ("pest_observations", "source",      "TEXT NOT NULL DEFAULT 'oficina'"),
    ("pest_observations", "created_at",  "TEXT"),
]

POST_MIGRATION_SQL = [
    # Idempotencia de la sincronización: el celular reenvía la cola sin miedo a
    # duplicar. SQLite ignora los NULL en índices únicos, así que los registros
    # históricos sin uuid conviven sin conflicto.
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_pest_client_uuid "
    "ON pest_observations(client_uuid) WHERE client_uuid IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_pest_source ON pest_observations(source)",
]


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path or settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def session(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | str | None = None) -> None:
    with session(db_path) as conn:
        conn.executescript(SCHEMA)
        migrate(conn)


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Aplica las migraciones pendientes. Seguro de ejecutar en cada arranque."""
    applied: list[str] = []
    for table, column, ddl in MIGRATIONS:
        cols = {r["name"] for r in query(conn, f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            applied.append(f"{table}.{column}")
    for sql in POST_MIGRATION_SQL:
        conn.execute(sql)
    return applied


def query(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def one(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> dict | None:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def insert_many(conn: sqlite3.Connection, table: str, rows: Iterable[dict]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    cols = list(rows[0].keys())
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})"
    )
    conn.executemany(sql, [[r[c] for c in cols] for r in rows])
    return len(rows)
