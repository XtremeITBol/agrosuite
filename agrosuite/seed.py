"""Generador de datos sintéticos con señal real.

No son números aleatorios: el clima sigue una estacionalidad del hemisferio
sur, y la incidencia de plagas se genera a partir de un modelo causal
(grados-día acumulados, humedad relativa, lluvia, susceptibilidad del
cultivo). Esto importa porque permite que los modelos de ML aprendan una
relación verdadera y que las métricas de validación signifiquen algo.

Cuando conectes datos reales, reemplaza este módulo por tus conectores de
ingesta: el resto del sistema no cambia.
"""
from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta
from datetime import time as dtime

import numpy as np

from . import db
from .config import settings

# ---------------------------------------------------------------------------
# Catálogos de dominio (agricultura extensiva, zona este de Bolivia)
# ---------------------------------------------------------------------------

CROPS = [
    # nombre,        ciclo, temp base, rinde objetivo t/ha
    ("Soya",           120, 10.0, 2.6),
    ("Maíz",           135,  8.0, 5.4),
    ("Trigo",          115,  4.5, 2.2),
    ("Girasol",        110,  6.0, 1.8),
    ("Sorgo",          120, 10.0, 3.5),
]

# Plagas con umbrales agronómicos reales y su modelo de grados-día.
# ddays_threshold: acumulación térmica desde siembra a partir de la cual la
# población alcanza densidad de daño económico.
PESTS = {
    "Spodoptera frugiperda": {
        "common": "Cogollero",
        "hosts": ("Maíz", "Sorgo", "Soya"),
        "base_temp": 10.9,
        "ddays_threshold": 380,
        "rh_optimum": 75,
        "rain_effect": -0.25,     # lluvias fuertes lavan larvas pequeñas
        "economic_threshold": 20, # % de plantas con daño
        "max_incidence": 62,
    },
    "Anticarsia gemmatalis": {
        "common": "Oruga de las leguminosas",
        "hosts": ("Soya",),
        "base_temp": 12.0,
        "ddays_threshold": 460,
        "rh_optimum": 80,
        "rain_effect": 0.10,
        "economic_threshold": 25,
        "max_incidence": 55,
    },
    "Helicoverpa armigera": {
        "common": "Heliotis",
        "hosts": ("Soya", "Maíz", "Girasol", "Trigo"),
        "base_temp": 11.0,
        "ddays_threshold": 520,
        "rh_optimum": 65,
        "rain_effect": -0.15,
        "economic_threshold": 15,
        "max_incidence": 44,
    },
    "Euschistus heros": {
        "common": "Chinche marrón",
        "hosts": ("Soya",),
        "base_temp": 13.0,
        "ddays_threshold": 620,
        "rh_optimum": 70,
        "rain_effect": -0.05,
        "economic_threshold": 12,
        "max_incidence": 38,
    },
    "Phakopsora pachyrhizi": {
        "common": "Roya asiática",
        "hosts": ("Soya",),
        "base_temp": 15.0,
        "ddays_threshold": 400,
        "rh_optimum": 92,          # requiere hoja mojada prolongada
        "rain_effect": 0.55,
        "economic_threshold": 5,
        "max_incidence": 72,
    },
    "Diabrotica speciosa": {
        "common": "Vaquita de San Antonio",
        "hosts": ("Soya", "Maíz"),
        "base_temp": 12.5,
        "ddays_threshold": 500,
        "rh_optimum": 70,
        "rain_effect": 0.05,
        "economic_threshold": 18,
        "max_incidence": 40,
    },
}

REGIONS = {
    "Norte Integrado": (-17.05, -63.25),
    "Chiquitania":     (-17.80, -61.60),
    "Cuatro Cañadas":  (-17.42, -62.60),
    "Pailón":          (-17.65, -62.75),
}

SOILS = ["Franco arcilloso", "Franco arenoso", "Arcilloso", "Franco limoso"]

KB_ARTICLES = [
    ("estado_pedido", "¿Cómo consulto el estado de mi pedido?",
     "Puedes consultar el estado indicando tu número de pedido. El sistema te devuelve estado (pendiente, en tránsito, entregado o retrasado), tonelaje y fecha comprometida."),
    ("estado_pedido", "¿Cuándo llega mi carga?",
     "La fecha comprometida figura en el pedido. Si el estado es 'en tránsito' la entrega ocurre normalmente dentro de las 48 horas siguientes al despacho."),
    ("precios", "¿Cuál es el precio de la tonelada?",
     "El precio se fija por contrato según producto y grado de calidad. Grado A tiene una prima del 6% sobre el precio base; el grado C descuenta 8%."),
    ("precios", "¿Hacen descuentos por volumen?",
     "Sí. A partir de 500 t por campaña aplica un descuento escalonado del 2% al 5% sobre el precio base."),
    ("calidad", "¿Qué humedad reciben?",
     "Recibimos grano con humedad de hasta 14%. Por encima de ese valor se aplica merma por secado de 1.2% por cada punto de humedad excedente."),
    ("calidad", "¿Cómo se determina el grado de calidad?",
     "Se muestrea en balanza según norma: se evalúan humedad, impurezas, grano dañado y grano quebrado. El resultado determina grado A, B o C."),
    ("logistica", "¿Puedo cambiar la dirección de entrega?",
     "Sí, siempre que el pedido no esté despachado. Una vez en tránsito el cambio genera un cargo por redireccionamiento."),
    ("logistica", "¿Hacen recojo en campo?",
     "Sí, contamos con flota propia para recojo en campo dentro del radio de las zonas de acopio. Se coordina con 48 horas de anticipación."),
    ("plagas", "¿Qué hago si detecto cogollero en mi maíz?",
     "Realiza monitoreo en 10 puntos por lote. Si superas el 20% de plantas con daño en cogollo, corresponde control. Prioriza productos selectivos para conservar enemigos naturales y rota modos de acción."),
    ("plagas", "¿Cuándo aplicar contra roya asiática?",
     "La roya exige control preventivo. Con humedad relativa sostenida sobre 90% y temperaturas de 18 a 26 °C el riesgo es alto: aplicar fungicida en R1-R2 y repetir a los 18-21 días."),
    ("plagas", "¿Cuál es el umbral de daño económico de la chinche marrón?",
     "En soya para grano el umbral es de 2 chinches por metro lineal, equivalente aproximadamente a 12% de incidencia en el monitoreo."),
    ("insumos", "¿Tienen stock de fertilizante?",
     "El stock se consulta en línea por SKU. El sistema también informa el punto de reorden y los días de cobertura restantes."),
    ("insumos", "¿Cuál es el tiempo de entrega de un insumo?",
     "Depende del SKU: los insumos locales entregan en 3 a 7 días y los importados entre 21 y 45 días."),
    ("pagos", "¿Qué formas de pago aceptan?",
     "Transferencia bancaria, cheque diferido y canje por grano. El canje se valoriza al precio de pizarra del día de la entrega."),
    ("pagos", "¿Emiten factura?",
     "Sí, se emite factura electrónica al confirmar la entrega y se envía al correo registrado del cliente."),
    ("reclamo", "Quiero presentar un reclamo",
     "Lamentamos el inconveniente. Registramos el reclamo y lo derivamos a un ejecutivo, que responde dentro de las 24 horas hábiles con un número de caso."),
    ("contacto", "¿Cómo hablo con una persona?",
     "Te derivo con un ejecutivo comercial. El horario de atención es de lunes a viernes de 08:00 a 18:00."),
    ("horarios", "¿Cuál es el horario de recepción en planta?",
     "La recepción en planta opera de lunes a sábado de 06:00 a 22:00. En pico de cosecha se extiende a 24 horas."),
]

TICKET_TEMPLATES = {
    "estado_pedido": [
        "hola quiero saber donde esta mi pedido {oid}",
        "buenas, el pedido {oid} sigue sin llegar",
        "necesito el estado del pedido numero {oid}",
        "cuando me entregan la carga del pedido {oid}?",
    ],
    "precios": [
        "cuanto esta la tonelada de soya esta semana",
        "me pasan lista de precios por favor",
        "hay descuento si compro 800 toneladas",
        "que precio manejan para maiz grado A",
    ],
    "calidad": [
        "recibieron mi grano con 15 de humedad, cuanto me descuentan",
        "como calculan el grado de calidad",
        "me bajaron a grado C y no entiendo por que",
    ],
    "logistica": [
        "puedo cambiar la direccion de entrega",
        "hacen recojo en campo en cuatro cañadas",
        "necesito coordinar un camion para el viernes",
    ],
    "plagas": [
        "tengo cogollero en el maiz que hago",
        "cuando debo aplicar contra roya",
        "cual es el umbral de la chinche marron",
        "veo orugas en la soya, es grave?",
    ],
    "insumos": [
        "tienen stock de urea",
        "cuanto tarda en llegar el fungicida",
        "necesito cotizacion de semilla de soya",
    ],
    "pagos": [
        "aceptan canje por grano",
        "todavia no me llega la factura",
        "que formas de pago tienen",
    ],
    "reclamo": [
        "quiero poner un reclamo, la entrega llego incompleta",
        "muy mal servicio, nadie me responde",
        "reclamo formal por la merma aplicada",
    ],
    "contacto": [
        "quiero hablar con una persona",
        "me pasan con un ejecutivo",
        "necesito hablar con alguien de ventas",
    ],
    "horarios": [
        "hasta que hora reciben en planta",
        "atienden los sabados?",
        "cual es el horario de recepcion",
        "a que hora abre la balanza",
    ],
}

FIRST = ["Agropecuaria", "Agrícola", "Estancia", "Hacienda", "Granos", "Semillas"]
SECOND = ["del Este", "San Julián", "Los Tajibos", "Río Grande", "El Carmen",
          "Santa Fe", "Yapacaní", "La Enconada", "Tres Cruces", "Okinawa"]


# ---------------------------------------------------------------------------
# Modelos generadores
# ---------------------------------------------------------------------------

def _seasonal_weather(d: date, lat: float, rng: random.Random) -> dict:
    """Clima diario del hemisferio sur con estacionalidad y persistencia."""
    doy = d.timetuple().tm_yday
    # pico térmico ~ 15 de enero (doy 15), mínimo ~ 15 de julio
    phase = math.cos(2 * math.pi * (doy - 15) / 365.25)
    tmean = 24.0 + 5.5 * phase - 0.35 * (abs(lat) - 17.0)
    amplitude = 11.0 - 2.0 * phase          # más amplitud térmica en invierno seco
    tmin = tmean - amplitude / 2 + rng.gauss(0, 1.6)
    tmax = tmean + amplitude / 2 + rng.gauss(0, 1.9)
    if tmax < tmin + 2:
        tmax = tmin + 2

    # estación húmeda noviembre-marzo
    wet = 0.5 + 0.5 * math.cos(2 * math.pi * (doy - 10) / 365.25)
    p_rain = 0.08 + 0.40 * wet
    rain = round(rng.expovariate(1 / (14.0 * wet + 2.0)), 1) if rng.random() < p_rain else 0.0

    rh = 58 + 26 * wet + (8 if rain > 0 else 0) + rng.gauss(0, 5)
    rh = float(min(99.0, max(28.0, rh)))
    wind = max(0.0, rng.gauss(11 + 5 * (1 - wet), 4))
    return {
        "tmin_c": round(tmin, 1),
        "tmax_c": round(tmax, 1),
        "rain_mm": rain,
        "rh_pct": round(rh, 1),
        "wind_kmh": round(wind, 1),
    }


def _pest_pressure(pest: dict, ddays: float, rh_mean: float,
                   rain_7d: float, susceptible: bool) -> float:
    """Presión de plaga esperada (0-100 %) — el 'ground truth' del generador.

    Curva logística sobre grados-día acumulados, modulada por humedad
    relativa (campana alrededor del óptimo de la especie) y por lluvia.
    """
    if not susceptible:
        return 0.0
    k = 0.012
    phenology = 1.0 / (1.0 + math.exp(-k * (ddays - pest["ddays_threshold"])))
    # decaimiento después del pico poblacional
    if ddays > pest["ddays_threshold"] + 500:
        phenology *= math.exp(-(ddays - pest["ddays_threshold"] - 500) / 600)
    humidity = math.exp(-((rh_mean - pest["rh_optimum"]) ** 2) / (2 * 14.0 ** 2))
    rain = 1.0 + pest["rain_effect"] * math.tanh(rain_7d / 40.0)
    # Techo por especie: una infestación real rara vez llega al 100% de plantas
    # con daño, porque antes se aplica control o la plaga se autolimita.
    ceiling = pest.get("max_incidence", 60.0)
    return float(max(0.0, min(100.0, ceiling * phenology * humidity * max(0.05, rain))))


def generate(db_path=None, days_history: int = 730, n_fields: int = 28) -> dict:
    """Puebla la base con un histórico completo. Idempotente: recrea el esquema."""
    rng = random.Random(settings.seed)
    np_rng = np.random.default_rng(settings.seed)

    path = db_path or settings.db_path
    from pathlib import Path as _P
    p = _P(path)
    if p.exists():
        p.unlink()
    for suffix in ("-wal", "-shm"):
        aux = _P(str(p) + suffix)
        if aux.exists():
            aux.unlink()
    db.init_db(path)

    today = date.today()
    start = today - timedelta(days=days_history)
    counts: dict[str, int] = {}

    with db.session(path) as conn:
        # ---- cultivos ----
        crops = [
            {"id": i + 1, "name": n, "cycle_days": c, "base_temp_c": b, "target_yield": y}
            for i, (n, c, b, y) in enumerate(CROPS)
        ]
        counts["crops"] = db.insert_many(conn, "crops", crops)
        crop_by_id = {c["id"]: c for c in crops}

        # ---- clima ----
        weather_rows = []
        for region, (lat, lon) in REGIONS.items():
            d = start
            while d <= today + timedelta(days=14):   # 14 días de pronóstico
                w = _seasonal_weather(d, lat, rng)
                w.update({"region": region, "date": d.isoformat(),
                          "source": "synthetic" if d <= today else "forecast"})
                weather_rows.append(w)
                d += timedelta(days=1)
        counts["weather_daily"] = db.insert_many(conn, "weather_daily", weather_rows)

        wx: dict[str, dict[str, dict]] = {}
        for row in weather_rows:
            wx.setdefault(row["region"], {})[row["date"]] = row

        # ---- lotes ----
        region_names = list(REGIONS)
        fields = []
        for i in range(n_fields):
            region = region_names[i % len(region_names)]
            lat0, lon0 = REGIONS[region]
            crop = rng.choice(crops)
            # campaña de verano o invierno según cultivo
            if crop["name"] in ("Soya", "Maíz"):
                anchor = date(today.year if today.month >= 11 else today.year - 1, 11, 20)
            else:
                anchor = date(today.year if today.month >= 5 else today.year - 1, 5, 10)
            planting = anchor + timedelta(days=rng.randint(-25, 25))
            if planting > today:
                planting -= timedelta(days=365)
            fields.append({
                "id": i + 1,
                "name": f"Lote {i + 1:02d} — {rng.choice(SECOND)}",
                "region": region,
                "lat": round(lat0 + rng.uniform(-0.45, 0.45), 4),
                "lon": round(lon0 + rng.uniform(-0.45, 0.45), 4),
                "area_ha": round(rng.uniform(80, 950), 1),
                "soil_type": rng.choice(SOILS),
                "crop_id": crop["id"],
                "planting_date": planting.isoformat(),
            })
        counts["fields"] = db.insert_many(conn, "fields", fields)

        # ---- observaciones de plagas (monitoreo semanal) ----
        obs = []
        scouts = ["J. Mamani", "R. Vargas", "L. Suárez", "M. Cuéllar", "A. Rojas"]
        for f in fields:
            crop_name = crop_by_id[f["crop_id"]]["name"]
            plant_date = date.fromisoformat(f["planting_date"])
            for pest_name, pest in PESTS.items():
                susceptible = crop_name in pest["hosts"]
                if not susceptible and rng.random() > 0.15:
                    continue  # se registran algunos ceros como control negativo
                ddays = 0.0
                d = plant_date
                rain_win: list[float] = []
                rh_win: list[float] = []
                while d <= min(today, plant_date + timedelta(days=200)):
                    w = wx[f["region"]].get(d.isoformat())
                    if w:
                        tmean = (w["tmin_c"] + w["tmax_c"]) / 2
                        ddays += max(0.0, tmean - pest["base_temp"])
                        rain_win.append(w["rain_mm"])
                        rh_win.append(w["rh_pct"])
                        rain_win, rh_win = rain_win[-7:], rh_win[-7:]
                    if (d - plant_date).days % 7 == 3 and rh_win:
                        truth = _pest_pressure(pest, ddays, float(np.mean(rh_win)),
                                               float(np.sum(rain_win)), susceptible)
                        # el monitoreo humano observa con ruido y sesgo a la baja
                        seen = max(0.0, truth * rng.uniform(0.75, 1.15) + rng.gauss(0, 2.5))
                        action = None
                        if seen >= pest["economic_threshold"]:
                            action = rng.choice(["Aplicación selectiva", "Aplicación total",
                                                 "Monitoreo intensificado", "Liberación de Trichogramma"])
                        obs.append({
                            "field_id": f["id"], "date": d.isoformat(), "species": pest_name,
                            "incidence_pct": round(min(100.0, seen), 2),
                            "scouted_by": rng.choice(scouts), "action_taken": action,
                        })
                    d += timedelta(days=1)
        counts["pest_observations"] = db.insert_many(conn, "pest_observations", obs)

        # ---- cosechas ----
        harvests = []
        for f in fields:
            crop = crop_by_id[f["crop_id"]]
            plant_date = date.fromisoformat(f["planting_date"])
            for cycle in range(3):
                hdate = plant_date + timedelta(days=crop["cycle_days"] + rng.randint(-8, 12) - 365 * cycle)
                if hdate > today or hdate < start:
                    continue
                # rinde afectado por plagas del ciclo, lluvia y azar
                pest_hit = np.mean([o["incidence_pct"] for o in obs
                                    if o["field_id"] == f["id"]
                                    and abs((date.fromisoformat(o["date"]) - hdate).days) < 45] or [0.0])
                season_rain = sum(
                    wx[f["region"]][d.isoformat()]["rain_mm"]
                    for d in (hdate - timedelta(days=n) for n in range(90))
                    if d.isoformat() in wx[f["region"]]
                )
                water_factor = min(1.15, 0.55 + season_rain / 420.0)
                pest_factor = max(0.55, 1.0 - pest_hit / 145.0)
                yield_t_ha = crop["target_yield"] * water_factor * pest_factor * rng.uniform(0.9, 1.1)
                tonnage = round(yield_t_ha * f["area_ha"], 1)
                moisture = round(min(19.0, max(10.5, rng.gauss(13.2, 1.5))), 1)
                grade = "A" if moisture <= 13.0 and pest_hit < 12 else ("B" if moisture <= 15 else "C")
                harvests.append({
                    "field_id": f["id"], "date": hdate.isoformat(), "tonnage": tonnage,
                    "moisture_pct": moisture, "quality_grade": grade,
                    "cost_usd": round(f["area_ha"] * rng.uniform(390, 560), 2),
                    "operator": rng.choice(scouts),
                })
        counts["harvests"] = db.insert_many(conn, "harvests", harvests)

        # ---- red logística ----
        silos = [
            {"id": 1, "name": "Silo Pailón", "lat": -17.65, "lon": -62.75,
             "capacity_t": 45000, "stock_t": 12400, "handling_usd_t": 3.1},
            {"id": 2, "name": "Silo Cuatro Cañadas", "lat": -17.42, "lon": -62.60,
             "capacity_t": 32000, "stock_t": 8800, "handling_usd_t": 2.7},
            {"id": 3, "name": "Silo Okinawa", "lat": -17.20, "lon": -62.90,
             "capacity_t": 28000, "stock_t": 5200, "handling_usd_t": 3.4},
            {"id": 4, "name": "Silo San Julián", "lat": -16.98, "lon": -62.68,
             "capacity_t": 21000, "stock_t": 3100, "handling_usd_t": 2.9},
        ]
        counts["silos"] = db.insert_many(conn, "silos", silos)

        plants = [
            # demanda contractual de la ventana de planificación (no anual)
            {"id": 1, "name": "Planta Aceitera Warnes", "kind": "planta", "lat": -17.51,
             "lon": -63.17, "demand_t": 9500, "price_usd_t": 372.0},
            {"id": 2, "name": "Puerto Jennefer (Hidrovía)", "kind": "puerto", "lat": -17.83,
             "lon": -63.05, "demand_t": 13000, "price_usd_t": 361.0},
            {"id": 3, "name": "Planta Balanceados Montero", "kind": "planta", "lat": -17.34,
             "lon": -63.25, "demand_t": 6000, "price_usd_t": 348.0},
        ]
        counts["plants"] = db.insert_many(conn, "plants", plants)

        vehicles = []
        for i in range(14):
            cap = rng.choice([28.0, 28.0, 32.0, 25.0, 34.0])
            vehicles.append({
                "id": i + 1, "plate": f"{rng.randint(1000, 4999)}-{rng.choice('ABCDEFG')}TX",
                "capacity_t": cap, "cost_usd_km": round(0.85 + cap * 0.012, 3),
                "available": 1 if rng.random() > 0.12 else 0,
            })
        counts["vehicles"] = db.insert_many(conn, "vehicles", vehicles)

        # ---- inventario de insumos ----
        skus = [
            ("FRT-UREA-50", "Urea granulada 46-0-0", "Fertilizante", "bolsa 50kg", 21, 28.5),
            ("FRT-FDA-50", "Fosfato diamónico", "Fertilizante", "bolsa 50kg", 35, 41.0),
            ("HRB-GLI-20", "Glifosato 62% SL", "Herbicida", "bidón 20L", 28, 96.0),
            ("INS-LAM-5", "Lambda-cialotrina 5% EC", "Insecticida", "bidón 5L", 24, 58.0),
            ("FNG-AZX-5", "Azoxistrobina + Ciproconazol", "Fungicida", "bidón 5L", 32, 132.0),
            ("SEM-SOY-40", "Semilla de soya certificada", "Semilla", "bolsa 40kg", 12, 74.0),
            ("SEM-MAI-60", "Semilla de maíz híbrido", "Semilla", "bolsa 60k sem", 18, 168.0),
            ("CMB-DSL-200", "Diésel", "Combustible", "barril 200L", 4, 212.0),
            ("REP-CUCH-01", "Cuchillas de cosechadora", "Repuesto", "juego", 45, 340.0),
            ("BIO-TRIC-01", "Trichogramma pretiosum", "Control biológico", "pulgada cuadrada", 7, 6.4),
        ]
        inv = []
        for i, (sku, name, cat, unit, lead, cost) in enumerate(skus):
            mean = rng.uniform(4, 95)
            inv.append({
                "id": i + 1, "sku": sku, "name": name, "category": cat, "unit": unit,
                "on_hand": round(mean * rng.uniform(3, 40), 1),
                "unit_cost_usd": cost, "lead_time_days": lead,
                "daily_usage_mean": round(mean, 2),
                "daily_usage_std": round(mean * rng.uniform(0.18, 0.55), 2),
                "order_cost_usd": round(rng.uniform(80, 260), 2),
                "holding_rate": 0.22,
            })
        counts["inventory"] = db.insert_many(conn, "inventory", inv)

        # ---- clientes, pedidos, tickets ----
        customers = []
        for i in range(22):
            nm = f"{rng.choice(FIRST)} {rng.choice(SECOND)}"
            customers.append({
                "id": i + 1, "name": nm,
                "email": nm.lower().replace(" ", ".").replace("á", "a").replace("í", "i") + "@example.com",
                "segment": rng.choice(["Pequeño", "Mediano", "Grande", "Cooperativa"]),
            })
        counts["customers"] = db.insert_many(conn, "customers", customers)

        orders = []
        for i in range(180):
            due = today + timedelta(days=rng.randint(-120, 45))
            if due < today - timedelta(days=7):
                status = rng.choices(["entregado", "retrasado"], weights=[0.86, 0.14])[0]
            elif due < today + timedelta(days=7):
                status = rng.choices(["en_transito", "pendiente", "entregado"], weights=[.5, .2, .3])[0]
            else:
                status = "pendiente"
            orders.append({
                "id": i + 1, "customer_id": rng.randint(1, len(customers)),
                "product": rng.choice(["Soya", "Maíz", "Trigo", "Girasol", "Sorgo", "Harina de soya"]),
                "tonnage": round(rng.uniform(25, 900), 1),
                "price_usd_t": round(rng.uniform(320, 420), 2),
                "due_date": due.isoformat(), "status": status,
            })
        counts["orders"] = db.insert_many(conn, "orders", orders)

        counts["kb_articles"] = db.insert_many(conn, "kb_articles", [
            {"id": i + 1, "intent": it, "question": q, "answer": a}
            for i, (it, q, a) in enumerate(KB_ARTICLES)
        ])

        tickets = []
        intents = list(TICKET_TEMPLATES)
        for i in range(320):
            intent = rng.choice(intents)
            tpl = rng.choice(TICKET_TEMPLATES[intent])
            msg = tpl.format(oid=rng.randint(1, len(orders)))
            created = datetime.combine(today, dtime(8, 0)) - timedelta(
                days=rng.randint(0, 180), hours=rng.randint(0, 23),
                minutes=rng.randint(0, 59))
            tickets.append({
                "id": i + 1, "customer_id": rng.randint(1, len(customers)),
                "channel": rng.choices(["whatsapp", "web", "email", "telefono"],
                                       weights=[.55, .2, .15, .1])[0],
                "message": msg, "intent": intent, "confidence": None, "answer": None,
                "escalated": 1 if intent in ("reclamo", "contacto") else 0,
                "created_at": created.isoformat(timespec="seconds"),
                "resolved_at": None,
            })
        counts["tickets"] = db.insert_many(conn, "tickets", tickets)

    return counts


if __name__ == "__main__":  # pragma: no cover
    import json
    print(json.dumps(generate(), indent=2, ensure_ascii=False))
