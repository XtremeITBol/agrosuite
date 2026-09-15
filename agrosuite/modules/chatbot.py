"""Módulo 3 — Atención al cliente automatizada.

Arquitectura deliberadamente escalonada, de barato a caro:

1. **Clasificador de intención** — regresión logística sobre TF-IDF de
   caracteres y palabras, entrenado con el histórico de tickets. Los char
   n-grams importan: los clientes escriben por WhatsApp sin tildes y con
   errores de tipeo ("cogoyero", "quando llega").

2. **Extracción de entidades** — número de pedido, SKU, producto, tonelaje.

3. **Acciones sobre datos reales** — si la intención lo permite, el bot
   consulta la base y responde con el dato concreto (estado del pedido,
   stock del insumo, riesgo de plaga del lote), no con un texto genérico.

4. **Recuperación en base de conocimiento** — TF-IDF + similitud coseno
   sobre los artículos, para las preguntas informativas.

5. **Escalamiento** — si la confianza cae por debajo del piso configurado, o
   la intención es un reclamo, deriva a un humano. Un bot que inventa es peor
   que un bot que deriva.

El adaptador LLM opcional (`AGROSUITE_LLM_PROVIDER`) sólo reescribe la
respuesta ya fundamentada en datos; nunca es la fuente de los hechos. Esto
mantiene el costo en cero por defecto y evita alucinaciones sobre precios o
estados de pedido.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report
from sklearn.pipeline import make_pipeline, make_union
from sklearn.model_selection import train_test_split

from .. import db
from ..config import settings

ESCALATION_INTENTS = {"reclamo", "contacto"}

_STATUS_TEXT = {
    "pendiente": "pendiente de despacho",
    "en_transito": "en tránsito",
    "entregado": "entregado",
    "retrasado": "retrasado",
}


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKD", text.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", t)).strip()


# ---------------------------------------------------------------------------
# Motor
# ---------------------------------------------------------------------------

@dataclass
class BotReply:
    answer: str
    intent: str
    confidence: float
    escalated: bool
    source: str                          # accion_datos | base_conocimiento | fallback
    entities: dict = dc_field(default_factory=dict)
    kb_matches: list = dc_field(default_factory=list)
    suggested_actions: list = dc_field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class CustomerBot:
    """Bot conversacional. Instanciar una vez y reutilizar (carga perezosa)."""

    def __init__(self, db_path=None):
        self.db_path = db_path
        self._intent_clf = None
        self._intent_report: dict = {}
        self._kb: list[dict] = []
        self._kb_vec = None
        self._kb_matrix = None

    # -- entrenamiento -----------------------------------------------------
    def fit(self) -> dict:
        with db.session(self.db_path) as conn:
            tickets = db.query(conn, "SELECT message, intent FROM tickets WHERE intent IS NOT NULL")
            self._kb = db.query(conn, "SELECT * FROM kb_articles")

        if len(tickets) < 30:
            raise ValueError("Se necesitan al menos 30 tickets etiquetados para entrenar.")

        X = [normalize(t["message"]) for t in tickets]
        y = [t["intent"] for t in tickets]

        # Las preguntas de la base de conocimiento también son ejemplos válidos
        # de cada intención: enriquecen las clases con pocos tickets.
        X += [normalize(a["question"]) for a in self._kb]
        y += [a["intent"] for a in self._kb]

        features = make_union(
            TfidfVectorizer(analyzer="word", ngram_range=(1, 2), min_df=1, sublinear_tf=True),
            TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=2, sublinear_tf=True),
        )
        clf = make_pipeline(features, LogisticRegression(max_iter=2000, C=6.0,
                                                         class_weight="balanced"))

        # Estratificar sólo si cada clase tiene al menos dos ejemplos; una
        # intención que existe únicamente en la base de conocimiento no debe
        # hacer fallar el entrenamiento completo.
        from collections import Counter
        strat = y if min(Counter(y).values()) >= 2 else None
        Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.25, random_state=0,
                                              stratify=strat)
        clf.fit(Xtr, ytr)
        rep = classification_report(yte, clf.predict(Xte), output_dict=True, zero_division=0)
        clf.fit(X, y)                      # reentrenar con todo para producción
        self._intent_clf = clf
        self._intent_report = {
            "accuracy": round(rep["accuracy"], 4),
            "macro_f1": round(rep["macro avg"]["f1-score"], 4),
            "weighted_f1": round(rep["weighted avg"]["f1-score"], 4),
            "n_examples": len(X),
            "n_intents": len(set(y)),
            "per_intent": {k: round(v["f1-score"], 3) for k, v in rep.items()
                           if isinstance(v, dict) and k not in
                           ("accuracy", "macro avg", "weighted avg")},
        }

        # índice de la base de conocimiento
        self._kb_vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True)
        corpus = [normalize(a["question"] + " " + a["answer"]) for a in self._kb]
        self._kb_matrix = self._kb_vec.fit_transform(corpus)
        return self._intent_report

    def _ensure(self):
        if self._intent_clf is None:
            self.fit()

    @property
    def report(self) -> dict:
        self._ensure()
        return self._intent_report

    # -- entidades ---------------------------------------------------------
    @staticmethod
    def extract_entities(text: str) -> dict:
        n = normalize(text)
        ents: dict = {}
        m = re.search(r"(?:pedido|orden|nota|nro|numero|n)\D{0,8}(\d{1,6})", n)
        if m:
            ents["order_id"] = int(m.group(1))
        elif (m := re.search(r"\b(\d{1,5})\b", n)) and re.search(r"pedido|orden|carga", n):
            ents["order_id"] = int(m.group(1))
        m = re.search(r"\b([a-z]{3}-[a-z]{3,4}-\d{1,3})\b", text.lower())
        if m:
            ents["sku"] = m.group(1).upper()
        for prod in ("soya", "maiz", "trigo", "girasol", "sorgo", "urea", "glifosato",
                     "fungicida", "insecticida", "semilla", "diesel"):
            if prod in n:
                ents.setdefault("products", []).append(prod)
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:t|ton|toneladas)\b", n)
        if m:
            ents["tonnage"] = float(m.group(1).replace(",", "."))
        # Variantes ortográficas incluidas a propósito: por WhatsApp llegan
        # "cogoyero", "eliotis", "diabrotica" sin tilde.
        for species, kws in (
            ("Spodoptera frugiperda", ("cogoll", "cogoy", "spodopt", "gusano cogo")),
            ("Phakopsora pachyrhizi", ("roya", "phakops")),
            ("Euschistus heros", ("chinche", "euschist")),
            ("Anticarsia gemmatalis", ("oruga", "anticarsia")),
            ("Helicoverpa armigera", ("heliotis", "eliotis", "helicoverpa")),
            ("Diabrotica speciosa", ("vaquita", "diabrotica")),
        ):
            if any(k in n for k in kws):
                ents["pest"] = species
                break
        return ents

    # -- acciones con datos reales ----------------------------------------
    def _act(self, intent: str, ents: dict) -> tuple[str, list] | tuple[None, list]:
        with db.session(self.db_path) as conn:
            if intent == "estado_pedido" and "order_id" in ents:
                o = db.one(conn, """
                    SELECT o.*, c.name AS customer FROM orders o
                    JOIN customers c ON c.id = o.customer_id WHERE o.id = ?
                """, (ents["order_id"],))
                if not o:
                    return (f"No encuentro el pedido {ents['order_id']}. "
                            "¿Puedes verificar el número? Aparece en la nota de entrega."), []
                due = date.fromisoformat(o["due_date"])
                delta = (due - date.today()).days
                when = ("hoy" if delta == 0 else
                        f"en {delta} días" if delta > 0 else f"hace {-delta} días")
                txt = (f"Pedido {o['id']} — {o['customer']}: {o['tonnage']:,.1f} t de "
                       f"{o['product']} a USD {o['price_usd_t']:,.2f}/t. "
                       f"Estado: **{_STATUS_TEXT.get(o['status'], o['status'])}**. "
                       f"Fecha comprometida: {o['due_date']} ({when}).")
                acts = []
                if o["status"] == "retrasado":
                    txt += " Lamentamos el retraso; puedo derivarte con logística para una fecha firme."
                    acts.append("derivar_logistica")
                return txt, acts

            if intent == "insumos" and ("sku" in ents or ents.get("products")):
                where, params = ("sku = ?", (ents["sku"],)) if "sku" in ents else (
                    " OR ".join(["LOWER(name) LIKE ?"] * len(ents["products"])),
                    tuple(f"%{p}%" for p in ents["products"]))
                items = db.query(conn, f"SELECT * FROM inventory WHERE {where} LIMIT 3", params)
                if items:
                    parts = []
                    for it in items:
                        cover = it["on_hand"] / it["daily_usage_mean"] if it["daily_usage_mean"] else 0
                        parts.append(f"{it['name']} ({it['sku']}): {it['on_hand']:,.0f} "
                                     f"{it['unit']} en stock, cobertura ~{cover:.0f} días, "
                                     f"entrega en {it['lead_time_days']} días.")
                    return " ".join(parts), ["cotizar"]

            if intent == "precios" and ents.get("products"):
                prod = ents["products"][0]
                r = db.one(conn, """
                    SELECT AVG(price_usd_t) AS p, COUNT(*) AS n FROM orders
                    WHERE LOWER(product) LIKE ? AND due_date >= date('now','-60 day')
                """, (f"%{prod}%",))
                if r and r["p"]:
                    return (f"El precio promedio contratado de {prod} en los últimos 60 días "
                            f"es USD {r['p']:,.2f}/t ({r['n']} operaciones). El precio final "
                            f"depende del grado de calidad: A suma 6%, C descuenta 8%."), ["cotizar"]

            if intent == "plagas" and "pest" in ents:
                from .pest import predict_risk
                res = predict_risk(self.db_path, auto_train=False)
                hits = [a for a in res["alerts"] if a["species"] == ents["pest"]]
                if hits:
                    top = hits[0]
                    high = sum(1 for h in hits if h["band"] == "ALTO")
                    return (f"{top['common_name']} ({top['species']}): el modelo estima "
                            f"riesgo {top['band'].lower()} a {res['horizon_days']} días en "
                            f"{high} de {len(hits)} lotes hospedantes. Umbral de daño económico: "
                            f"{top['economic_threshold_pct']}% de incidencia. "
                            f"{top['recommendation']}"), ["ver_mapa_riesgo"]
        return None, []

    # -- recuperación en base de conocimiento ------------------------------
    def _retrieve(self, text: str, intent: str, k: int = 3) -> list[dict]:
        if self._kb_matrix is None or not self._kb:
            return []
        q = self._kb_vec.transform([normalize(text)])
        sims = (self._kb_matrix @ q.T).toarray().ravel()
        # sesgo a favor de los artículos de la intención predicha
        for i, a in enumerate(self._kb):
            if a["intent"] == intent:
                sims[i] += 0.12
        idx = np.argsort(-sims)[:k]
        return [{"question": self._kb[i]["question"], "answer": self._kb[i]["answer"],
                 "score": round(float(sims[i]), 3), "intent": self._kb[i]["intent"]}
                for i in idx if sims[i] > 0.05]

    # -- punto de entrada --------------------------------------------------
    def reply(self, message: str, customer_id: int | None = None,
              channel: str = "web", persist: bool = True) -> BotReply:
        self._ensure()
        proba = self._intent_clf.predict_proba([normalize(message)])[0]
        classes = self._intent_clf.classes_
        top = int(np.argmax(proba))
        intent, conf = str(classes[top]), float(proba[top])
        ents = self.extract_entities(message)

        answer, source, actions = None, "fallback", []

        if intent not in ESCALATION_INTENTS:
            answer, actions = self._act(intent, ents)
            if answer:
                source = "accion_datos"

        kb = self._retrieve(message, intent)
        if answer is None and kb and conf >= settings.chatbot_confidence_floor:
            answer, source = kb[0]["answer"], "base_conocimiento"

        escalate = (intent in ESCALATION_INTENTS
                    or conf < settings.chatbot_confidence_floor
                    or answer is None)

        if answer is None:
            answer = ("No estoy seguro de haber entendido. Te derivo con un ejecutivo "
                      "para que te ayude directamente. ¿Puedes contarme un poco más "
                      "mientras tanto?")
        if intent == "reclamo":
            answer = (kb[0]["answer"] if kb else
                      "Registramos tu reclamo y lo derivamos a un ejecutivo.")
            source = "base_conocimiento" if kb else source
        if escalate and "derivar_humano" not in actions:
            actions.append("derivar_humano")

        answer = self._maybe_polish(message, answer, intent)

        if persist:
            with db.session(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO tickets (customer_id, channel, message, intent, confidence,
                                         answer, escalated, created_at, resolved_at)
                    VALUES (?,?,?,?,?,?,?,?,?)
                """, (customer_id, channel, message, intent, round(conf, 4), answer,
                      int(escalate), datetime.now().isoformat(timespec="seconds"),
                      None if escalate else datetime.now().isoformat(timespec="seconds")))

        return BotReply(answer=answer, intent=intent, confidence=round(conf, 4),
                        escalated=escalate, source=source, entities=ents,
                        kb_matches=kb, suggested_actions=actions)

    # -- adaptador LLM opcional -------------------------------------------
    def _maybe_polish(self, question: str, grounded: str, intent: str) -> str:
        """Reescribe la respuesta ya fundamentada. Nunca aporta hechos nuevos.

        Sin API key configurada esto es un no-op, así que el costo es cero.
        """
        if settings.llm_provider == "none" or not settings.llm_api_key:
            return grounded
        try:
            import httpx
            prompt = (
                "Reescribe la RESPUESTA en español rioplatense-boliviano neutro, tono "
                "cordial y profesional, máximo 3 frases. No agregues ningún dato que no "
                "esté en la RESPUESTA: no inventes precios, fechas ni estados.\n\n"
                f"PREGUNTA: {question}\nRESPUESTA: {grounded}"
            )
            if settings.llm_provider == "anthropic":
                r = httpx.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": settings.llm_api_key,
                             "anthropic-version": "2023-06-01"},
                    json={"model": settings.llm_model or "claude-sonnet-4-5",
                          "max_tokens": 400,
                          "messages": [{"role": "user", "content": prompt}]},
                    timeout=15)
                r.raise_for_status()
                return r.json()["content"][0]["text"].strip()
            if settings.llm_provider == "openai":
                r = httpx.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                    json={"model": settings.llm_model or "gpt-4o-mini",
                          "messages": [{"role": "user", "content": prompt}]},
                    timeout=15)
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"].strip()
        except Exception:
            pass       # ante cualquier fallo, la respuesta fundamentada sirve igual
        return grounded


_bot: CustomerBot | None = None


def get_bot(db_path=None) -> CustomerBot:
    global _bot
    if _bot is None or _bot.db_path != db_path:
        _bot = CustomerBot(db_path)
    return _bot


def stats(db_path=None) -> dict:
    """Métricas operativas del canal de atención."""
    with db.session(db_path) as conn:
        total = db.one(conn, "SELECT COUNT(*) n FROM tickets")["n"]
        esc = db.one(conn, "SELECT COUNT(*) n FROM tickets WHERE escalated = 1")["n"]
        by_intent = db.query(conn, """
            SELECT intent, COUNT(*) n FROM tickets WHERE intent IS NOT NULL
            GROUP BY intent ORDER BY n DESC
        """)
        by_channel = db.query(conn, "SELECT channel, COUNT(*) n FROM tickets GROUP BY channel ORDER BY n DESC")
        # Contención = resuelto sin intervención humana. Un ticket clasificado
        # y no derivado cuenta como contenido, haya o no respuesta guardada
        # (el histórico importado puede no traer el texto de la respuesta).
        auto = db.one(conn, "SELECT COUNT(*) n FROM tickets WHERE escalated = 0 AND intent IS NOT NULL")["n"]
    return {
        "tickets_total": total,
        "escalated": esc,
        "auto_resolved": auto,
        "containment_rate_pct": round(100 * auto / total, 1) if total else 0.0,
        "by_intent": by_intent,
        "by_channel": by_channel,
    }
