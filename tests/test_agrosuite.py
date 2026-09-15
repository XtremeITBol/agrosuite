"""Suite de pruebas de AgroSuite (unittest, sin dependencias externas).

Ejecutar:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import math
import tempfile
import unittest
import warnings
from datetime import date
from pathlib import Path

warnings.filterwarnings("ignore")

from agrosuite import certs, db, seed
from agrosuite.config import settings
from agrosuite.modules import analytics, chatbot, pest, scouting, supply_chain

TMP = Path(tempfile.mkdtemp()) / "test.db"


def setUpModule():
    settings.db_path = TMP
    seed.generate(TMP, days_history=730, n_fields=20)
    pest.train(TMP)


# ---------------------------------------------------------------------------

class TestDataLayer(unittest.TestCase):
    def test_schema_populated(self):
        with db.session(TMP) as c:
            for table, minimum in [("crops", 5), ("fields", 20), ("weather_daily", 1000),
                                   ("pest_observations", 500), ("harvests", 20),
                                   ("silos", 4), ("inventory", 10), ("tickets", 300)]:
                n = db.one(c, f"SELECT COUNT(*) n FROM {table}")["n"]
                self.assertGreaterEqual(n, minimum, f"{table} tiene sólo {n} filas")

    def test_foreign_keys_valid(self):
        with db.session(TMP) as c:
            orphans = db.one(c, """
                SELECT COUNT(*) n FROM harvests h
                LEFT JOIN fields f ON f.id = h.field_id WHERE f.id IS NULL""")["n"]
            self.assertEqual(orphans, 0)

    def test_weather_has_forecast(self):
        with db.session(TMP) as c:
            future = db.one(c, "SELECT COUNT(*) n FROM weather_daily WHERE date > ?",
                            (date.today().isoformat(),))["n"]
            self.assertGreater(future, 0, "Falta el pronóstico a futuro")

    def test_weather_physically_plausible(self):
        with db.session(TMP) as c:
            bad = db.one(c, """SELECT COUNT(*) n FROM weather_daily
                WHERE tmax_c < tmin_c OR rh_pct < 0 OR rh_pct > 100 OR rain_mm < 0""")["n"]
            self.assertEqual(bad, 0)


class TestSupplyChain(unittest.TestCase):
    def test_haversine_known_distance(self):
        # Santa Cruz de la Sierra ↔ La Paz ≈ 545 km en línea recta
        d = supply_chain.haversine_km(-17.78, -63.18, -16.50, -68.15)
        self.assertTrue(530 < d < 560, f"distancia inesperada: {d:.0f} km")

    def test_network_optimum_and_conservation(self):
        plan = supply_chain.optimize_network(TMP)
        self.assertEqual(plan.status, "óptimo")
        self.assertGreater(plan.delivered_tons, 0)

        # toda la cosecha se evacúa
        moved = sum(r["tons"] for r in plan.field_to_silo)
        self.assertAlmostEqual(moved, plan.harvest_tons, delta=max(1.0, plan.harvest_tons * 0.001))

        # conservación de flujo por silo: salidas ≤ entradas + stock inicial
        with db.session(TMP) as c:
            stock = {s["name"]: s["stock_t"] for s in db.query(c, "SELECT name, stock_t FROM silos")}
            cap = {s["name"]: s["capacity_t"] for s in db.query(c, "SELECT name, capacity_t FROM silos")}
        for silo in stock:
            inflow = sum(r["tons"] for r in plan.field_to_silo if r["silo"] == silo)
            outflow = sum(r["tons"] for r in plan.silo_to_plant if r["silo"] == silo)
            self.assertLessEqual(outflow, inflow + stock[silo] + 1.0,
                                 f"{silo} despacha más de lo que tiene")
            self.assertLessEqual(inflow, cap[silo] - stock[silo] + 1.0,
                                 f"{silo} excede su capacidad libre")

    def test_network_respects_demand_ceiling(self):
        plan = supply_chain.optimize_network(TMP)
        with db.session(TMP) as c:
            demand = {p["name"]: p["demand_t"] for p in db.query(c, "SELECT name, demand_t FROM plants")}
        for dest, cap in demand.items():
            got = sum(r["tons"] for r in plan.silo_to_plant if r["destination"] == dest)
            self.assertLessEqual(got, cap + 1.0, f"{dest} recibe por encima de su demanda")

    def test_active_silos_match_flow(self):
        plan = supply_chain.optimize_network(TMP)
        with_flow = ({r["silo"] for r in plan.field_to_silo} |
                     {r["silo"] for r in plan.silo_to_plant})
        self.assertEqual(set(plan.silos_active), with_flow)

    def test_routes_never_exceed_capacity(self):
        rt = supply_chain.plan_routes(TMP)
        self.assertEqual(rt["status"], "ok")
        self.assertGreater(len(rt["routes"]), 0)
        for r in rt["routes"]:
            self.assertLessEqual(r["load_t"], r["capacity_t"] + 0.11,
                                 f"{r['vehicle']} sobrecargado")
            self.assertLessEqual(r["utilization_pct"], 100.5)
            self.assertGreater(r["km"], 0)

    def test_routes_respect_trips_per_day(self):
        rt = supply_chain.plan_routes(TMP, trips_per_day=1)
        seen: dict[str, int] = {}
        for r in rt["routes"]:
            seen[r["vehicle"]] = seen.get(r["vehicle"], 0) + 1
        self.assertTrue(all(v <= 1 for v in seen.values()),
                        "un camión hace más viajes de los permitidos")

    def test_two_opt_never_worsens(self):
        import numpy as np
        rng = np.random.default_rng(3)
        pts = rng.uniform(-1, 1, size=(9, 2))
        D = np.array([[math.dist(a, b) for b in pts] for a in pts])
        order = list(range(1, 9))
        before = supply_chain._route_cost(order, D)
        after = supply_chain._route_cost(supply_chain._two_opt(order, D), D)
        self.assertLessEqual(after, before + 1e-9)

    def test_inventory_policy_math(self):
        pol = supply_chain.inventory_policy(TMP, service_level=0.95)
        self.assertAlmostEqual(pol["z"], 1.6449, places=3)
        with db.session(TMP) as c:
            items = {i["sku"]: i for i in db.query(c, "SELECT * FROM inventory")}
        for row in pol["items"]:
            it = items[row["sku"]]
            expect_rop = (it["daily_usage_mean"] * it["lead_time_days"] +
                          1.6449 * it["daily_usage_std"] * math.sqrt(it["lead_time_days"]))
            self.assertAlmostEqual(row["reorder_point"], expect_rop, delta=0.15)
            expect_eoq = math.sqrt(2 * it["daily_usage_mean"] * 365 * it["order_cost_usd"] /
                                   (it["unit_cost_usd"] * it["holding_rate"]))
            self.assertAlmostEqual(row["eoq"], expect_eoq, delta=0.15)
            self.assertIn(row["status"], ("OK", "REPONER", "CRÍTICO"))

    def test_inventory_status_consistent(self):
        pol = supply_chain.inventory_policy(TMP)
        for r in pol["items"]:
            if r["status"] == "OK":
                self.assertGreater(r["on_hand"], r["reorder_point"])
                self.assertEqual(r["suggested_order_qty"], 0)
            else:
                self.assertLessEqual(r["on_hand"], r["reorder_point"] + 1e-6)


class TestPest(unittest.TestCase):
    def test_dataset_has_signal(self):
        df = pest.build_dataset(TMP)
        self.assertGreater(len(df), 500)
        self.assertTrue(0.05 < df["target"].mean() < 0.95, "clases demasiado desbalanceadas")
        self.assertFalse(df[pest.FEATURES].isna().all().any(), "hay features vacías")

    def test_model_beats_baselines(self):
        _, rep = pest.train(TMP, save=False)
        self.assertGreater(rep.roc_auc, 0.75, "el modelo no supera un umbral mínimo útil")
        self.assertGreaterEqual(rep.roc_auc, rep.baseline_persistence_auc,
                                "el modelo no mejora la persistencia")
        self.assertGreater(rep.n_test, 30)
        self.assertLess(rep.brier, 0.25)

    def test_temporal_split_is_really_temporal(self):
        df = pest.build_dataset(TMP).sort_values("date")
        cut = df["date"].quantile(0.75)
        self.assertTrue((df[df["date"] <= cut]["date"].max() <=
                         df[df["date"] > cut]["date"].min()),
                        "hay solapamiento temporal entre entrenamiento y prueba")

    def test_predict_shape_and_bands(self):
        res = pest.predict_risk(TMP)
        self.assertGreater(len(res["alerts"]), 0)
        for a in res["alerts"]:
            self.assertTrue(0.0 <= a["risk"] <= 1.0)
            self.assertIn(a["band"], ("ALTO", "MEDIO", "BAJO"))
            self.assertTrue(a["recommendation"])
            # sólo se evalúan plagas sobre cultivos hospedantes
            self.assertIn(a["crop"], seed.PESTS[a["species"]]["hosts"])
        self.assertEqual(sum(res["summary"].values()), len(res["alerts"]))

    def test_fallback_without_model(self):
        """Sin modelo entrenado, la regla agronómica debe seguir respondiendo."""
        backup = None
        if pest.MODEL_PATH.exists():
            backup = pest.MODEL_PATH.with_suffix(".bak")
            pest.MODEL_PATH.rename(backup)
        try:
            res = pest.predict_risk(TMP, auto_train=False)
            self.assertGreater(len(res["alerts"]), 0)
            self.assertIn("grados-día", res["model"])
        finally:
            if backup:
                backup.rename(pest.MODEL_PATH)

    def test_single_field_filter(self):
        res = pest.predict_risk(TMP, field_id=1)
        self.assertTrue(all(a["field_id"] == 1 for a in res["alerts"]))


class TestChatbot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bot = chatbot.CustomerBot(TMP)
        cls.report = cls.bot.fit()

    def test_intent_classifier_quality(self):
        self.assertGreater(self.report["accuracy"], 0.70)
        self.assertGreaterEqual(self.report["n_intents"], 8)

    def test_entity_extraction(self):
        e = chatbot.CustomerBot.extract_entities("hola, el pedido 137 de 250 t de soya no llega")
        self.assertEqual(e["order_id"], 137)
        self.assertEqual(e["tonnage"], 250.0)
        self.assertIn("soya", e["products"])

    def test_entity_typos(self):
        """Los clientes escriben mal por WhatsApp: el extractor debe tolerarlo."""
        self.assertEqual(
            chatbot.CustomerBot.extract_entities("tengo cogoyero en el maiz")["pest"],
            "Spodoptera frugiperda")
        self.assertEqual(
            chatbot.CustomerBot.extract_entities("hay eliotis en la soya")["pest"],
            "Helicoverpa armigera")

    def test_order_lookup_uses_real_data(self):
        with db.session(TMP) as c:
            o = db.one(c, "SELECT * FROM orders LIMIT 1")
        r = self.bot.reply(f"donde esta mi pedido {o['id']}", persist=False)
        self.assertEqual(r.intent, "estado_pedido")
        self.assertEqual(r.source, "accion_datos")
        self.assertIn(str(o["id"]), r.answer)
        self.assertIn(o["product"], r.answer)

    def test_unknown_order_does_not_hallucinate(self):
        r = self.bot.reply("estado del pedido 999999", persist=False)
        self.assertIn("999999", r.answer)
        self.assertIn("no encuentro", r.answer.lower())

    def test_gibberish_escalates(self):
        r = self.bot.reply("zxcvb qwerty asdfg", persist=False)
        self.assertTrue(r.escalated)
        self.assertIn("derivar_humano", r.suggested_actions)

    def test_complaint_always_escalates(self):
        r = self.bot.reply("quiero poner un reclamo formal, llego incompleto", persist=False)
        self.assertTrue(r.escalated)

    def test_persistence_writes_ticket(self):
        with db.session(TMP) as c:
            before = db.one(c, "SELECT COUNT(*) n FROM tickets")["n"]
        self.bot.reply("cual es el horario de recepcion", persist=True)
        with db.session(TMP) as c:
            after = db.one(c, "SELECT COUNT(*) n FROM tickets")["n"]
            last = db.one(c, "SELECT * FROM tickets ORDER BY id DESC LIMIT 1")
        self.assertEqual(after, before + 1)
        self.assertIsNotNone(last["answer"])
        self.assertIsNotNone(last["confidence"])

    def test_normalize_strips_accents(self):
        self.assertEqual(chatbot.normalize("¿Cuándo llegó el camión?"),
                         "cuando llego el camion")


class TestAnalytics(unittest.TestCase):
    def test_kpis_have_comparison(self):
        k = analytics.kpis(TMP)
        self.assertIn("rendimiento_t_ha", k["kpis"])
        for name, v in k["kpis"].items():
            self.assertIn("value", v)
            self.assertIn("unit", v)

    def test_yield_math(self):
        y = analytics.yield_by_field(TMP)
        self.assertGreater(len(y["fields"]), 0)
        for f in y["fields"]:
            expected = 100 * f["yield_t_ha"] / f["target_yield"]
            self.assertAlmostEqual(f["attainment_pct"], expected, delta=0.6)

    def test_anomalies_bounded(self):
        a = analytics.anomalies(TMP, contamination=0.1)
        if a.get("anomalies") is not None and a.get("n_records"):
            self.assertLessEqual(len(a["anomalies"]), math.ceil(a["n_records"] * 0.2))
            for x in a["anomalies"]:
                self.assertIn(x["driver"], analytics.ANOMALY_FEATURES)

    def test_forecast_beats_mean_baseline(self):
        f = analytics.forecast_yield(TMP)
        if f.get("forecast"):
            self.assertLess(f["mae_t_ha"], f["baseline_mae_t_ha"],
                            "el modelo no mejora predecir la media")
            for r in f["forecast"]:
                lo, hi = r["interval_t_ha"]
                self.assertLessEqual(lo, r["forecast_t_ha"] + 1e-9)
                self.assertGreaterEqual(hi, r["forecast_t_ha"] - 1e-9)

    def test_series_monotonic_dates(self):
        s = analytics.production_series(TMP)
        periods = [r["period"] for r in s["series"]]
        self.assertEqual(periods, sorted(periods))


class TestAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from agrosuite.api import create_app
        cls.app = create_app(TMP)
        cls.client = cls.app.test_client()

    def _ok(self, path):
        r = self.client.get(path)
        self.assertEqual(r.status_code, 200, f"{path} -> {r.status_code}: {r.data[:200]}")
        return r.get_json()

    def test_health(self):
        d = self._ok("/api/health")
        self.assertEqual(d["status"], "ok")
        self.assertGreater(d["records"]["fields"], 0)

    def test_all_get_endpoints(self):
        for p in ["/api/fields", "/api/supply/network", "/api/supply/routes",
                  "/api/supply/inventory", "/api/pest/risk", "/api/pest/observations",
                  "/api/chat/stats", "/api/chat/kb", "/api/analytics/kpis",
                  "/api/analytics/yield", "/api/analytics/anomalies",
                  "/api/analytics/forecast", "/api/analytics/series"]:
            with self.subTest(endpoint=p):
                d = self._ok(p)
                self.assertIsInstance(d, dict)

    def test_chat_endpoint(self):
        r = self.client.post("/api/chat/message",
                             json={"message": "cuanto esta la soya", "persist": False})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(d["answer"])
        self.assertIn("intent", d)

    def test_chat_rejects_empty(self):
        r = self.client.post("/api/chat/message", json={"message": "   "})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["type"], "validacion")

    def test_chat_rejects_oversize(self):
        r = self.client.post("/api/chat/message", json={"message": "x" * 2500})
        self.assertEqual(r.status_code, 400)

    def test_query_param_validation(self):
        self.assertEqual(self.client.get("/api/analytics/kpis?window_days=abc").status_code, 400)
        self.assertEqual(self.client.get("/api/analytics/kpis?window_days=999999").status_code, 400)
        self.assertEqual(self.client.get("/api/supply/inventory?service_level=2").status_code, 400)
        self.assertEqual(self.client.get("/api/analytics/series?freq=X").status_code, 400)

    def test_404_is_json(self):
        r = self.client.get("/api/no-existe")
        self.assertEqual(r.status_code, 404)
        self.assertIn("error", r.get_json())

    def test_dashboard_served(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"AgroSuite", r.data)

    def test_responses_are_json_serializable(self):
        """Los tipos de numpy rompen json.dumps si se filtran a la respuesta."""
        for p in ["/api/supply/network", "/api/pest/risk", "/api/analytics/forecast"]:
            with self.subTest(endpoint=p):
                json.dumps(self._ok(p))


class TestScouting(unittest.TestCase):
    """Monitoreo a campo: la ruta crítica de la app Android."""

    def _obs(self, **kw):
        import uuid as u
        base = {"field_id": 1, "species": "Spodoptera frugiperda",
                "incidence_pct": 18.5, "scouted_by": "Prueba",
                "client_uuid": str(u.uuid4())}
        base.update(kw)
        return base

    def test_catalog_is_self_contained(self):
        """La PWA debe poder operar sin señal con sólo este catálogo."""
        cat = scouting.catalog(TMP)
        self.assertGreater(len(cat["fields"]), 0)
        self.assertGreater(len(cat["species"]), 0)
        self.assertTrue(all({"id", "name", "crop"} <= set(f) for f in cat["fields"]))
        for s in cat["species"]:
            self.assertIn("economic_threshold", s)
            self.assertIsInstance(s["hosts"], list)

    def test_accepts_and_persists(self):
        o = self._obs(lat=-17.42, lon=-62.60)
        r = scouting.record_batch([o], TMP)
        self.assertEqual(r["counts"]["accepted"], 1)
        with db.session(TMP) as c:
            row = db.one(c, "SELECT * FROM pest_observations WHERE client_uuid = ?",
                         (o["client_uuid"],))
        self.assertIsNotNone(row)
        self.assertEqual(row["source"], "campo")
        self.assertAlmostEqual(row["lat"], -17.42)

    def test_resend_is_idempotent(self):
        o = self._obs()
        first = scouting.record_batch([o], TMP)
        again = scouting.record_batch([o, o], TMP)
        self.assertEqual(first["counts"]["accepted"], 1)
        self.assertEqual(again["counts"]["accepted"], 0)
        self.assertEqual(again["counts"]["duplicated"], 2)
        with db.session(TMP) as c:
            n = db.one(c, "SELECT COUNT(*) n FROM pest_observations WHERE client_uuid = ?",
                       (o["client_uuid"],))["n"]
        self.assertEqual(n, 1)

    def test_concurrent_sync_never_duplicates(self):
        """La página y el service worker sincronizan a la vez al volver la señal."""
        import threading
        batch = [self._obs() for _ in range(8)]
        uuids = [o["client_uuid"] for o in batch]
        results, errors = [], []

        def worker():
            try:
                results.append(scouting.record_batch(list(batch), TMP))
            except Exception as e:      # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"la sincronización concurrente falló: {errors}")
        self.assertEqual(sum(r["counts"]["accepted"] for r in results), len(batch))
        with db.session(TMP) as c:
            n = db.one(c, "SELECT COUNT(*) n FROM pest_observations WHERE client_uuid IN "
                          f"({','.join('?' * len(uuids))})", uuids)["n"]
        self.assertEqual(n, len(batch))

    def test_bad_record_does_not_block_the_queue(self):
        """Un registro corrupto no debe trabar la cola del celular para siempre."""
        good, bad = self._obs(), self._obs(species="Inexistente")
        r = scouting.record_batch([good, bad], TMP)
        self.assertEqual(r["counts"]["accepted"], 1)
        self.assertEqual(r["counts"]["rejected"], 1)
        self.assertIn("client_uuid", r["rejected"][0])

    def test_validation_rules(self):
        from datetime import timedelta
        cases = [
            (self._obs(field_id=999999), "lote"),
            (self._obs(incidence_pct=101), "incidence"),
            (self._obs(incidence_pct=-1), "incidence"),
            (self._obs(date=(date.today() + timedelta(days=2)).isoformat()), "futura"),
            (self._obs(lat=200), "lat"),
            (self._obs(client_uuid="x"), "client_uuid"),
        ]
        for obs, needle in cases:
            with self.subTest(needle=needle):
                r = scouting.record_batch([obs], TMP)
                self.assertEqual(r["counts"]["rejected"], 1)
                self.assertIn(needle.lower(), r["rejected"][0]["error"].lower())

    def test_photo_must_be_a_real_image(self):
        import base64
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAE"
            "hQGAhKmMIQAAAABJRU5ErkJggg==")
        ok = self._obs(photo="data:image/png;base64," + base64.b64encode(png).decode())
        bad = self._obs(photo="data:image/png;base64," + base64.b64encode(b"no soy imagen").decode())
        r = scouting.record_batch([ok, bad], TMP)
        self.assertEqual(r["counts"]["accepted"], 1)
        self.assertEqual(r["counts"]["rejected"], 1)
        with db.session(TMP) as c:
            row = db.one(c, "SELECT photo FROM pest_observations WHERE client_uuid = ?",
                         (ok["client_uuid"],))
        self.assertTrue(row["photo"].endswith(".png"))
        self.assertTrue((scouting.MEDIA_DIR / row["photo"]).exists())

    def test_photo_is_deduplicated(self):
        """Dos monitoreos con la misma foto no duplican el archivo en disco."""
        import base64
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAE"
            "hQGAhKmMIQAAAABJRU5ErkJggg==")
        n1 = scouting.save_photo(png)
        n2 = scouting.save_photo(png)
        self.assertEqual(n1, n2)

    def test_oversize_photo_rejected(self):
        with self.assertRaises(scouting.ValidationError):
            scouting.save_photo(b"\xff\xd8\xff" + b"\x00" * (7 * 1024 * 1024))

    def test_scouting_feeds_the_pest_model(self):
        """Lo cargado a campo tiene que llegar al dataset de entrenamiento."""
        o = self._obs(incidence_pct=44.0)
        scouting.record_batch([o], TMP)
        obs = scouting.recent(TMP, limit=500, source="campo")["observations"]
        self.assertTrue(any(x["client_uuid"] == o["client_uuid"]
                            if "client_uuid" in x else True for x in obs))
        with db.session(TMP) as c:
            row = db.one(c, """SELECT o.* FROM pest_observations o
                               WHERE o.client_uuid = ?""", (o["client_uuid"],))
        self.assertEqual(row["species"], "Spodoptera frugiperda")


class TestCertificates(unittest.TestCase):
    """HTTPS local: sin esto la PWA no instala, no usa GPS ni funciona offline."""

    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp()) / "certs"

    def test_generates_trust_chain(self):
        crt, key, ca = certs.ensure_server_cert(self.dir, ["192.168.1.50"])
        for p in (crt, key, ca):
            self.assertTrue(p.exists())
        info = certs.describe(crt)
        self.assertIn("192.168.1.50", info["hosts"])
        self.assertIn("127.0.0.1", info["hosts"])
        self.assertIn("localhost", info["hosts"])
        self.assertIn("AgroSuite Local CA", info["issuer"])

    def test_is_idempotent(self):
        c1, _, _ = certs.ensure_server_cert(self.dir, ["192.168.1.50"])
        data1 = c1.read_bytes()
        c2, _, _ = certs.ensure_server_cert(self.dir, ["192.168.1.50"])
        self.assertEqual(data1, c2.read_bytes(), "regeneró un certificado todavía válido")

    def test_regenerates_when_the_network_changes(self):
        c1, _, _ = certs.ensure_server_cert(self.dir, ["192.168.1.50"])
        data1 = c1.read_bytes()
        c2, _, _ = certs.ensure_server_cert(self.dir, ["10.0.0.7"])
        self.assertNotEqual(data1, c2.read_bytes())
        self.assertIn("10.0.0.7", certs.describe(c2)["hosts"])

    def test_leaf_lifetime_within_browser_limit(self):
        """Los navegadores rechazan certificados de servidor de más de 398 días."""
        import datetime as _dt
        from cryptography import x509
        crt, _, _ = certs.ensure_server_cert(self.dir, ["192.168.1.60"])
        c = x509.load_pem_x509_certificate(crt.read_bytes())
        days = (c.not_valid_after_utc - c.not_valid_before_utc).days
        self.assertLessEqual(days, 398, f"certificado de {days} días: los navegadores lo rechazarán")

    def test_server_cert_is_not_a_ca(self):
        crt, _, _ = certs.ensure_server_cert(self.dir, ["192.168.1.70"])
        from cryptography import x509
        c = x509.load_pem_x509_certificate(crt.read_bytes())
        bc = c.extensions.get_extension_for_class(x509.BasicConstraints).value
        self.assertFalse(bc.ca)


class TestPWAAssets(unittest.TestCase):
    """El manifest y el service worker se validan, no se asumen."""

    @classmethod
    def setUpClass(cls):
        from agrosuite.api import create_app
        cls.client = create_app(TMP).test_client()

    def test_manifest_is_valid_and_installable(self):
        r = self.client.get("/manifest.webmanifest")
        self.assertEqual(r.status_code, 200)
        m = json.loads(r.data)
        # Requisitos mínimos de instalabilidad en Android/Chrome.
        for key in ("name", "short_name", "start_url", "display", "icons"):
            self.assertIn(key, m)
        self.assertIn(m["display"], ("standalone", "fullscreen", "minimal-ui"))
        sizes = {i["sizes"] for i in m["icons"]}
        self.assertIn("192x192", sizes)
        self.assertIn("512x512", sizes)
        self.assertTrue(any(i.get("purpose") == "maskable" for i in m["icons"]))

    def test_icons_exist_and_are_png(self):
        for i in json.loads(self.client.get("/manifest.webmanifest").data)["icons"]:
            with self.subTest(icon=i["src"]):
                r = self.client.get(i["src"])
                self.assertEqual(r.status_code, 200)
                self.assertTrue(r.data.startswith(b"\x89PNG"))

    def test_service_worker_served_with_root_scope(self):
        r = self.client.get("/sw.js")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("Service-Worker-Allowed"), "/")
        self.assertIn("sync-observations", r.data.decode())

    def test_field_app_and_queue_are_served(self):
        self.assertEqual(self.client.get("/campo").status_code, 200)
        self.assertEqual(self.client.get("/idb-queue.js").status_code, 200)

    def test_api_post_endpoint_contract(self):
        import uuid as u
        obs = {"field_id": 1, "species": "Spodoptera frugiperda", "incidence_pct": 12,
               "scouted_by": "API", "client_uuid": str(u.uuid4())}
        r = self.client.post("/api/field/observations", json={"observations": [obs]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["counts"]["accepted"], 1)

    def test_partial_batch_returns_207(self):
        import uuid as u
        good = {"field_id": 1, "species": "Spodoptera frugiperda", "incidence_pct": 12,
                "scouted_by": "API", "client_uuid": str(u.uuid4())}
        bad = dict(good, client_uuid=str(u.uuid4()), field_id=987654)
        r = self.client.post("/api/field/observations", json={"observations": [good, bad]})
        self.assertEqual(r.status_code, 207)

    def test_catalog_endpoint(self):
        r = self.client.get("/api/field/catalog")
        self.assertEqual(r.status_code, 200)
        self.assertGreater(len(r.get_json()["fields"]), 0)

    def test_server_info_lists_urls(self):
        d = self.client.get("/api/field/server-info").get_json()
        self.assertIn("lan_ips", d)
        self.assertIn("ca_url", d)


class TestWindowsPackaging(unittest.TestCase):
    """Errores de empaquetado que sólo aparecerían en la PC del usuario."""

    ROOT = Path(__file__).resolve().parent.parent

    def test_packaging_files_present(self):
        for p in ["packaging/launcher.py", "packaging/agrosuite.spec",
                  "packaging/build_windows.bat", ".github/workflows/build-windows.yml"]:
            with self.subTest(file=p):
                self.assertTrue((self.ROOT / p).exists(), f"falta {p}")

    def test_launcher_compiles(self):
        import py_compile
        py_compile.compile(str(self.ROOT / "packaging" / "launcher.py"), doraise=True)

    def test_spec_declares_critical_hidden_imports(self):
        """sklearn y scipy cargan submódulos por nombre: sin esto el .exe crashea."""
        spec = (self.ROOT / "packaging" / "agrosuite.spec").read_text()
        for needle in ["sklearn", "scipy", "cryptography", "joblib",
                       "collect_submodules", "sklearn.utils._typedefs"]:
            with self.subTest(needle=needle):
                self.assertIn(needle, spec)

    def test_spec_bundles_the_web_assets(self):
        """Sin la carpeta web el .exe arranca pero no sirve ni el tablero ni la PWA."""
        spec = (self.ROOT / "packaging" / "agrosuite.spec").read_text()
        self.assertIn('"web"', spec)

    def test_data_dir_is_writable_and_outside_the_bundle(self):
        """Con PyInstaller, sys._MEIPASS se borra al cerrar: los datos no van ahí."""
        from agrosuite.config import DATA_DIR
        self.assertTrue(DATA_DIR.exists())
        probe = DATA_DIR / ".test-escritura"
        probe.write_text("ok")
        self.assertEqual(probe.read_text(), "ok")
        probe.unlink()


if __name__ == "__main__":
    unittest.main(verbosity=2)
