#!/usr/bin/env python3
"""Punto de entrada de AgroSuite.

Uso:
    python run.py seed          # genera la base con datos sintéticos
    python run.py train         # entrena los modelos de plagas y del chatbot
    python run.py serve         # levanta la API y el tablero
    python run.py demo          # seed + train + serve (todo en uno)
    python run.py sync-weather  # actualiza el clima desde Open-Meteo
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)


def cmd_seed(args) -> int:
    from agrosuite.seed import generate
    counts = generate(days_history=args.days, n_fields=args.fields)
    print("Base generada:")
    for k, v in counts.items():
        print(f"  {k:22} {v:>6}")
    return 0


def cmd_train(args) -> int:
    from agrosuite.modules import chatbot, pest
    print("Entrenando el modelo de plagas…")
    _, report = pest.train()
    r = report.to_dict()
    print(f"  ROC-AUC {r['roc_auc']}  |  PR-AUC {r['pr_auc']}  |  F1 {r['f1']}")
    print(f"  baseline persistencia {r['baseline_persistence_auc']}  |  "
          f"entrenamiento {r['n_train']} / prueba {r['n_test']} (corte {r['split_date']})")
    print("Entrenando el clasificador de intención…")
    rep = chatbot.get_bot().fit()
    print(f"  exactitud {rep['accuracy']}  |  macro-F1 {rep['macro_f1']}  |  "
          f"{rep['n_intents']} intenciones, {rep['n_examples']} ejemplos")
    return 0


def cmd_serve(args) -> int:
    from agrosuite import net
    from agrosuite.api import create_app
    from agrosuite.config import DATA_DIR, settings

    lan = getattr(args, "lan", False) or getattr(args, "https", False)
    host = args.host or ("0.0.0.0" if lan else settings.host)
    settings.use_https = bool(getattr(args, "https", False))
    port = args.port or (settings.https_port if settings.use_https else settings.port)
    settings.port = port

    ssl_ctx = None
    ips = net.all_lan_ips()
    if settings.use_https:
        from agrosuite import certs
        crt, key, ca = certs.ensure_server_cert(DATA_DIR / "certs", ips)
        ssl_ctx = (str(crt), str(key))

    app = create_app()
    scheme = "https" if settings.use_https else "http"
    shown = host if host != "0.0.0.0" else "127.0.0.1"

    print()
    print("  " + "─" * 58)
    print(f"   AgroSuite — servidor {'seguro' if settings.use_https else 'local'}")
    print("  " + "─" * 58)
    print(f"   En esta PC       {scheme}://localhost:{port}/")
    if lan and ips:
        print("   Desde el celular (misma red WiFi):")
        for ip in ips:
            print(f"                    {scheme}://{ip}:{port}/campo")
    elif lan:
        print("   No se detectó una IP de red local.")
    if settings.use_https:
        print()
        print("   Antes de usar el celular, instalá UNA VEZ el certificado:")
        for ip in ips[:1] or ["<ip-de-esta-pc>"]:
            print(f"     1. Abrí  https://{ip}:{port}/agrosuite-ca.crt")
            print(f"     2. Android: Ajustes › Seguridad › Cifrado y credenciales")
            print(f"        › Instalar certificado › Certificado de CA")
        print("   Sin ese paso el navegador muestra una advertencia y la app")
        print("   queda en modo limitado (sin offline ni GPS).")
    else:
        print()
        print("   Modo sin cifrado: la app de campo funciona limitada en el")
        print("   celular. Para instalación, GPS y modo sin señal, usá --https.")
    print("  " + "─" * 58)
    print()

    if not lan:
        host = shown
    app.run(host=host, port=port, debug=args.debug,
            use_reloader=False, ssl_context=ssl_ctx, threaded=True)
    return 0


def cmd_sync(args) -> int:
    from agrosuite.config import settings
    from agrosuite.integrations import weather
    settings.weather_online = True
    print(json.dumps(weather.sync_all(), indent=2, ensure_ascii=False))
    return 0


def cmd_demo(args) -> int:
    cmd_seed(args)
    cmd_train(args)
    return cmd_serve(args)


def main() -> int:
    p = argparse.ArgumentParser(description="AgroSuite — plataforma agrícola inteligente")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("seed", help="genera la base con datos sintéticos")
    sp.add_argument("--days", type=int, default=730, help="días de histórico")
    sp.add_argument("--fields", type=int, default=28, help="cantidad de lotes")
    sp.set_defaults(func=cmd_seed)

    tp = sub.add_parser("train", help="entrena los modelos")
    tp.set_defaults(func=cmd_train)

    rp = sub.add_parser("serve", help="levanta la API y el tablero")
    rp.add_argument("--host", default=None)
    rp.add_argument("--port", type=int, default=None)
    rp.add_argument("--debug", action="store_true")
    rp.add_argument("--lan", action="store_true",
                    help="escucha en toda la red local (celulares en la misma WiFi)")
    rp.add_argument("--https", action="store_true",
                    help="HTTPS con CA local; necesario para la app de campo (implica --lan)")
    rp.set_defaults(func=cmd_serve)

    wp = sub.add_parser("sync-weather", help="actualiza el clima desde Open-Meteo")
    wp.set_defaults(func=cmd_sync)

    dp = sub.add_parser("demo", help="seed + train + serve")
    dp.add_argument("--days", type=int, default=730)
    dp.add_argument("--fields", type=int, default=28)
    dp.add_argument("--host", default=None)
    dp.add_argument("--port", type=int, default=None)
    dp.add_argument("--debug", action="store_true")
    dp.add_argument("--lan", action="store_true")
    dp.add_argument("--https", action="store_true")
    dp.set_defaults(func=cmd_demo)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
