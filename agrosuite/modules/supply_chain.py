"""Módulo 1 — Optimización de la cadena de suministro.

Tres problemas resueltos con métodos exactos o heurísticas reconocidas:

1. `optimize_network`  — Transbordo lote → silo → planta/puerto formulado como
   MILP (variables continuas de flujo + binarias de activación de silo) y
   resuelto con HiGHS vía `scipy.optimize.milp`. Maximiza margen
   (ingreso − flete − acopio − costo fijo de operar cada silo).

2. `plan_routes`       — Ruteo de flota capacitado (CVRP) por construcción de
   vecino más cercano con restricción de capacidad y refinamiento 2-opt.

3. `inventory_policy`  — Política (s, Q) por SKU: punto de reorden con nivel de
   servicio objetivo y lote económico de compra (EOQ de Wilson).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Sequence

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from .. import db

# Tarifa de flete de referencia para grano a granel en camión (USD por t·km).
FREIGHT_USD_T_KM = 0.062
# Costo fijo diario de operar un silo (personal, energía, secado en marcha).
SILO_FIXED_USD_DAY = 850.0
# Factor de sinuosidad: la distancia por camino es mayor que la línea recta.
ROAD_FACTOR = 1.32
# Peso del margen perdido por tonelada de demanda no atendida. Con 1.0 el
# modelo prioriza servir al destino de mayor precio; bajarlo hace que el
# ahorro de flete pese más que la prima de precio.
LOST_SALE_WEIGHT = 1.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def road_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    return haversine_km(lat1, lon1, lat2, lon2) * ROAD_FACTOR


# ---------------------------------------------------------------------------
# 1. Red de transbordo (MILP)
# ---------------------------------------------------------------------------

@dataclass
class NetworkPlan:
    status: str
    logistics_cost_usd: float       # flete + acopio + costo fijo de silos
    freight_usd: float
    handling_usd: float
    fixed_usd: float
    cost_per_ton_usd: float
    delivered_value_usd: float      # valor de venta del grano entregado
    unmet_value_usd: float          # margen perdido por demanda no atendida
    harvest_tons: float             # cosecha evacuada desde los lotes
    delivered_tons: float           # tonelaje total entregado a destinos
    field_to_silo: list[dict]
    silo_to_plant: list[dict]
    silos_active: list[str]
    unmet_demand: list[dict]

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _pending_supply(conn, horizon_days: int) -> list[dict]:
    """Tonelaje disponible por lote: cosecha reciente aún no evacuada.

    En producción esto sale del sistema de balanza. Aquí se aproxima con las
    cosechas de la ventana reciente.
    """
    since = (date.today() - timedelta(days=horizon_days)).isoformat()
    return db.query(conn, """
        SELECT f.id AS field_id, f.name, f.lat, f.lon, f.region,
               c.name AS crop, SUM(h.tonnage) AS tons
        FROM harvests h
        JOIN fields f ON f.id = h.field_id
        JOIN crops  c ON c.id = f.crop_id
        WHERE h.date >= ?
        GROUP BY f.id
        HAVING tons > 0
        ORDER BY tons DESC
    """, (since,))


def optimize_network(db_path=None, horizon_days: int = 120,
                     freight_rate: float = FREIGHT_USD_T_KM) -> NetworkPlan:
    with db.session(db_path) as conn:
        supply = _pending_supply(conn, horizon_days)
        silos = db.query(conn, "SELECT * FROM silos ORDER BY id")
        plants = db.query(conn, "SELECT * FROM plants ORDER BY id")

    if not supply or not silos or not plants:
        return NetworkPlan("sin_datos", 0, 0, 0, 0, 0, 0, 0, 0, 0, [], [], [], [])

    nF, nS, nP = len(supply), len(silos), len(plants)
    nX, nY = nF * nS, nS * nP
    nVar = nX + nY + nS               # x (lote→silo), y (silo→planta), z (silo activo)

    ix = lambda f, s: f * nS + s                      # noqa: E731
    iy = lambda s, p: nX + s * nP + p                 # noqa: E731
    iz = lambda s: nX + nY + s                        # noqa: E731

    # ---- costos ----
    c = np.zeros(nVar)
    dist_fs = np.zeros((nF, nS))
    dist_sp = np.zeros((nS, nP))
    for f, fld in enumerate(supply):
        for s, sl in enumerate(silos):
            d = road_km(fld["lat"], fld["lon"], sl["lat"], sl["lon"])
            dist_fs[f, s] = d
            c[ix(f, s)] = d * freight_rate + sl["handling_usd_t"]
    for s, sl in enumerate(silos):
        for p, pl in enumerate(plants):
            d = road_km(sl["lat"], sl["lon"], pl["lat"], pl["lon"])
            dist_sp[s, p] = d
            # Objetivo = costo logístico + costo de oportunidad de la demanda
            # insatisfecha. Entregar una tonelada en el destino p evita perder
            # su margen, de ahí el término negativo. Es equivalente, salvo una
            # constante, a maximizar ingreso menos costo, pero no inventa
            # utilidad sobre el grano ya almacenado.
            c[iy(s, p)] = d * freight_rate - pl["price_usd_t"] * LOST_SALE_WEIGHT
        c[iz(s)] = SILO_FIXED_USD_DAY

    constraints: list[LinearConstraint] = []

    # (a) toda la cosecha debe evacuarse: Σ_s x[f,s] == supply_f
    A = np.zeros((nF, nVar))
    b = np.zeros(nF)
    for f, fld in enumerate(supply):
        for s in range(nS):
            A[f, ix(f, s)] = 1.0
        b[f] = fld["tons"]
    constraints.append(LinearConstraint(A, b, b))

    # (b) capacidad libre de cada silo: Σ_f x[f,s] <= cap_s − stock_s
    A = np.zeros((nS, nVar))
    ub = np.zeros(nS)
    for s, sl in enumerate(silos):
        for f in range(nF):
            A[s, ix(f, s)] = 1.0
        ub[s] = max(0.0, sl["capacity_t"] - sl["stock_t"])
    constraints.append(LinearConstraint(A, -np.inf, ub))

    # (c) conservación de flujo: Σ_p y[s,p] − Σ_f x[f,s] <= stock_s
    A = np.zeros((nS, nVar))
    ub = np.zeros(nS)
    for s, sl in enumerate(silos):
        for p in range(nP):
            A[s, iy(s, p)] = 1.0
        for f in range(nF):
            A[s, ix(f, s)] = -1.0
        ub[s] = sl["stock_t"]
    constraints.append(LinearConstraint(A, -np.inf, ub))

    # (d) demanda máxima por planta: Σ_s y[s,p] <= demand_p
    A = np.zeros((nP, nVar))
    ub = np.zeros(nP)
    for p, pl in enumerate(plants):
        for s in range(nS):
            A[p, iy(s, p)] = 1.0
        ub[p] = pl["demand_t"]
    constraints.append(LinearConstraint(A, -np.inf, ub))

    # (e) activación de silo (big-M): un silo que recibe o despacha grano paga
    #     su costo fijo. Debe cubrir tanto la entrada (x) como la salida (y),
    #     porque un silo puede despachar sólo contra su stock previo.
    total_supply = float(sum(f["tons"] for f in supply))
    bigM = total_supply + sum(s["capacity_t"] for s in silos)
    A = np.zeros((nS, nVar))
    for s in range(nS):
        for f in range(nF):
            A[s, ix(f, s)] = 1.0
        for p in range(nP):
            A[s, iy(s, p)] = 1.0
        A[s, iz(s)] = -bigM
    constraints.append(LinearConstraint(A, -np.inf, np.zeros(nS)))

    lb = np.zeros(nVar)
    up = np.full(nVar, np.inf)
    up[nX + nY:] = 1.0
    integrality = np.zeros(nVar)
    integrality[nX + nY:] = 1

    res = milp(c=c, constraints=constraints, integrality=integrality,
               bounds=Bounds(lb, up))

    if not res.success or res.x is None:
        return NetworkPlan(f"infactible: {res.message}", 0, 0, 0, 0, 0, 0, 0, 0, 0, [], [], [], [])

    x = res.x
    tol = 1e-4
    f2s, s2p = [], []
    freight = handling = delivered_value = fixed = 0.0
    delivered_tons = 0.0
    for f, fld in enumerate(supply):
        for s, sl in enumerate(silos):
            t = x[ix(f, s)]
            if t > tol:
                fr = dist_fs[f, s] * freight_rate * t
                hd = sl["handling_usd_t"] * t
                freight += fr
                handling += hd
                f2s.append({
                    "field": fld["name"], "field_id": fld["field_id"], "crop": fld["crop"],
                    "silo": sl["name"], "tons": round(t, 1), "km": round(dist_fs[f, s], 1),
                    "freight_usd": round(fr, 2), "handling_usd": round(hd, 2),
                })
    delivered = {p["id"]: 0.0 for p in plants}
    for s, sl in enumerate(silos):
        for p, pl in enumerate(plants):
            t = x[iy(s, p)]
            if t > tol:
                fr = dist_sp[s, p] * freight_rate * t
                val = pl["price_usd_t"] * t
                freight += fr
                delivered_value += val
                delivered_tons += t
                delivered[pl["id"]] += t
                s2p.append({
                    "silo": sl["name"], "destination": pl["name"], "kind": pl["kind"],
                    "tons": round(t, 1), "km": round(dist_sp[s, p], 1),
                    "freight_usd": round(fr, 2), "value_usd": round(val, 2),
                })
    active = [silos[s]["name"] for s in range(nS) if x[iz(s)] > 0.5]
    fixed = SILO_FIXED_USD_DAY * len(active)

    unmet, unmet_value = [], 0.0
    for p in plants:
        gap = p["demand_t"] - delivered[p["id"]]
        if gap > 1.0:
            unmet_value += gap * p["price_usd_t"]
            unmet.append({
                "destination": p["name"], "demand_t": p["demand_t"],
                "delivered_t": round(float(delivered[p["id"]]), 1),
                "unmet_t": round(float(gap), 1),
                "lost_value_usd": round(float(gap * p["price_usd_t"]), 2),
            })

    logistics = freight + handling + fixed
    return NetworkPlan(
        status="óptimo",
        logistics_cost_usd=round(logistics, 2),
        freight_usd=round(freight, 2),
        handling_usd=round(handling, 2),
        fixed_usd=round(fixed, 2),
        cost_per_ton_usd=round(logistics / delivered_tons, 2) if delivered_tons else 0.0,
        delivered_value_usd=round(delivered_value, 2),
        unmet_value_usd=round(unmet_value, 2),
        harvest_tons=round(total_supply, 1),
        delivered_tons=round(delivered_tons, 1),
        field_to_silo=sorted(f2s, key=lambda r: -r["tons"]),
        silo_to_plant=sorted(s2p, key=lambda r: -r["tons"]),
        silos_active=active,
        unmet_demand=unmet,
    )


# ---------------------------------------------------------------------------
# 2. Ruteo de flota (CVRP: Clarke-Wright + 2-opt)
# ---------------------------------------------------------------------------

def _route_cost(order: Sequence[int], D: np.ndarray) -> float:
    """Costo de una ruta cerrada que arranca y termina en el depósito (índice 0)."""
    if not order:
        return 0.0
    total = D[0, order[0]]
    for a, b in zip(order, order[1:]):
        total += D[a, b]
    return total + D[order[-1], 0]


def _two_opt(order: list[int], D: np.ndarray, max_pass: int = 60) -> list[int]:
    best = order[:]
    best_cost = _route_cost(best, D)
    improved, passes = True, 0
    while improved and passes < max_pass:
        improved, passes = False, passes + 1
        for i in range(len(best) - 1):
            for j in range(i + 1, len(best)):
                cand = best[:i] + best[i:j + 1][::-1] + best[j + 1:]
                cost = _route_cost(cand, D)
                if cost < best_cost - 1e-9:
                    best, best_cost, improved = cand, cost, True
    return best


def plan_routes(db_path=None, horizon_days: int = 30, trips_per_day: int = 2,
                depot: tuple[float, float] | None = None) -> dict:
    """Plan de despacho de **un día** para la flota disponible.

    El backlog de una campaña excede por varios órdenes de magnitud lo que la
    flota mueve en una jornada, así que el planificador no intenta rutear todo:
    reparte la capacidad diaria entre los lotes de forma proporcional al
    tonelaje pendiente (evita que un lote grande monopolice la flota), arma los
    viajes y reporta cuántos días de backlog quedan.

    Los viajes se construyen contra la capacidad real de cada camión (nunca se
    arma un viaje que ningún vehículo de la flota pueda tomar): cada camión se
    llena por vecino más cercano entre los lotes con tonelaje pendiente, y las
    rutas de más de dos paradas se refinan con 2-opt.
    """
    with db.session(db_path) as conn:
        stops = _pending_supply(conn, horizon_days)
        vehicles = db.query(conn, "SELECT * FROM vehicles WHERE available = 1 ORDER BY capacity_t DESC")
        silos = db.query(conn, "SELECT * FROM silos ORDER BY id")

    if not stops or not vehicles:
        return {"status": "sin_datos", "routes": [], "total_km": 0.0,
                "total_cost_usd": 0.0, "vehicles_used": 0,
                "vehicles_available": len(vehicles), "avg_utilization_pct": 0.0,
                "planned_tons": 0.0, "pending_tons": 0.0, "backlog_days": 0.0,
                "note": "Sin cosecha pendiente o sin flota disponible."}

    if silos:
        dep = depot if depot is not None else (silos[0]["lat"], silos[0]["lon"])
        depot_name = silos[0]["name"]
    else:
        dep = depot if depot is not None else (-17.65, -62.75)
        depot_name = "DEPÓSITO"

    nodes = [{"name": depot_name, "lat": dep[0], "lon": dep[1]}] + [
        {"name": s["name"], "field_id": s["field_id"], "lat": s["lat"],
         "lon": s["lon"], "tons": float(s["tons"])} for s in stops
    ]
    n = len(nodes)
    D = np.zeros((n, n))
    for i in range(n):
        for j in range(i + 1, n):
            d = road_km(nodes[i]["lat"], nodes[i]["lon"], nodes[j]["lat"], nodes[j]["lon"])
            D[i, j] = D[j, i] = d

    # --- capacidad de la jornada y reparto proporcional ---
    slots: list[dict] = []          # una entrada por viaje disponible
    for v in vehicles:
        for _ in range(max(1, trips_per_day)):
            slots.append(dict(v))
    slots.sort(key=lambda v: -v["capacity_t"])
    daily_capacity = sum(v["capacity_t"] for v in slots)

    total_pending = sum(nodes[i]["tons"] for i in range(1, n))
    share = min(1.0, daily_capacity / total_pending) if total_pending > 0 else 0.0
    planned = {i: nodes[i]["tons"] * share for i in range(1, n)}

    # --- construir viajes contra la capacidad REAL de cada camión ---
    # Cada slot se llena por vecino más cercano con remanente pendiente: así un
    # camión de 25 t nunca recibe un viaje de 34 t, y las cargas parciales se
    # agrupan en milk runs geográficamente coherentes. El 2-opt posterior
    # reordena las paradas de cada ruta multi-parada.
    remaining = {i: t for i, t in planned.items() if t > 0.05}
    out: list[dict] = []
    total_km = total_cost = planned_tons = 0.0
    trip_no: dict[str, int] = {}

    for veh in slots:
        if not remaining:
            break
        cap = veh["capacity_t"]
        load, pos, stops = 0.0, 0, []
        while load < cap - 0.05 and remaining:
            # candidato: el lote pendiente más cercano a la posición actual
            j = min(remaining, key=lambda k: D[pos, k])
            take = min(cap - load, remaining[j])
            if take <= 0.05:
                break
            stops.append(j)
            load += take
            remaining[j] -= take
            if remaining[j] <= 0.05:
                del remaining[j]
            pos = j
        if not stops:
            continue
        seq = _two_opt(stops, D) if len(stops) > 2 else stops
        km = _route_cost(seq, D)
        cost = km * veh["cost_usd_km"]
        total_km += km
        total_cost += cost
        planned_tons += load
        trip_no[veh["plate"]] = trip_no.get(veh["plate"], 0) + 1
        out.append({
            "vehicle": veh["plate"],
            "trip": trip_no[veh["plate"]],
            "capacity_t": cap,
            "load_t": round(load, 1),
            "utilization_pct": round(100 * load / cap, 1),
            "km": round(km, 1),
            "cost_usd": round(cost, 2),
            "stops": [nodes[i]["name"] for i in seq],
        })

    unrouted = sum(remaining.values())

    pending_after = total_pending - planned_tons
    return {
        "status": "ok",
        "depot": depot_name,
        "routes": out,
        "vehicles_used": len({r["vehicle"] for r in out}),
        "vehicles_available": len(vehicles),
        "trips_planned": len(out),
        "trips_per_day": trips_per_day,
        "daily_capacity_t": round(daily_capacity, 1),
        "planned_tons": round(planned_tons, 1),
        "pending_tons": round(max(0.0, pending_after), 1),
        "backlog_days": round(total_pending / daily_capacity, 1) if daily_capacity else 0.0,
        "total_km": round(total_km, 1),
        "total_cost_usd": round(total_cost, 2),
        "cost_per_ton_usd": round(total_cost / planned_tons, 2) if planned_tons else 0.0,
        "avg_utilization_pct": round(float(np.mean([r["utilization_pct"] for r in out])), 1) if out else 0.0,
        "note": (f"{round(unrouted,1)} t del cupo diario quedaron sin camión asignado."
                 if unrouted > 0.05 else "Todo el cupo diario quedó asignado."),
    }


# ---------------------------------------------------------------------------
# 3. Política de inventario (s, Q)
# ---------------------------------------------------------------------------

_Z = {0.90: 1.2816, 0.95: 1.6449, 0.975: 1.9600, 0.98: 2.0537, 0.99: 2.3263}


def inventory_policy(db_path=None, service_level: float = 0.95) -> dict:
    z = _Z.get(round(service_level, 3), 1.6449)
    with db.session(db_path) as conn:
        items = db.query(conn, "SELECT * FROM inventory ORDER BY sku")

    rows, total_value, at_risk = [], 0.0, 0
    for it in items:
        mu, sigma, L = it["daily_usage_mean"], it["daily_usage_std"], it["lead_time_days"]
        demand_lt = mu * L
        sigma_lt = sigma * math.sqrt(L)
        safety = z * sigma_lt
        rop = demand_lt + safety
        annual_demand = mu * 365
        holding = it["unit_cost_usd"] * it["holding_rate"]
        eoq = math.sqrt(2 * annual_demand * it["order_cost_usd"] / holding) if holding > 0 else 0.0
        cover = it["on_hand"] / mu if mu > 0 else float("inf")
        status = ("CRÍTICO" if it["on_hand"] < demand_lt else
                  "REPONER" if it["on_hand"] <= rop else "OK")
        if status != "OK":
            at_risk += 1
        value = it["on_hand"] * it["unit_cost_usd"]
        total_value += value
        rows.append({
            "sku": it["sku"], "name": it["name"], "category": it["category"],
            "unit": it["unit"], "on_hand": round(it["on_hand"], 1),
            "lead_time_days": L,
            "reorder_point": round(rop, 1),
            "safety_stock": round(safety, 1),
            "eoq": round(eoq, 1),
            "days_of_cover": round(cover, 1),
            "stock_value_usd": round(value, 2),
            "suggested_order_qty": round(max(0.0, eoq if it["on_hand"] <= rop else 0.0), 1),
            "suggested_order_usd": round(max(0.0, eoq if it["on_hand"] <= rop else 0.0) * it["unit_cost_usd"], 2),
            "status": status,
        })

    order = {"CRÍTICO": 0, "REPONER": 1, "OK": 2}
    rows.sort(key=lambda r: (order[r["status"]], r["days_of_cover"]))
    return {
        "service_level": service_level,
        "z": z,
        "items": rows,
        "skus_at_risk": at_risk,
        "total_stock_value_usd": round(total_value, 2),
        "total_suggested_purchase_usd": round(sum(r["suggested_order_usd"] for r in rows), 2),
    }
