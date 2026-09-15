#!/usr/bin/env python3
"""Punto de entrada del ejecutable de Windows.

A diferencia de `run.py`, que es una CLI para desarrollo, esto está pensado
para alguien que hace doble clic en un .exe y espera que algo pase:

* Prepara la base y entrena los modelos la primera vez, mostrando progreso.
* Levanta el servidor en HTTPS sobre toda la red local, para que los celulares
  de la finca puedan usar la app de campo.
* Abre el tablero en el navegador.
* Muestra en pantalla la dirección y el paso de instalación del certificado.
* No se cierra solo: si algo falla, el mensaje queda visible.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback
import warnings
import webbrowser

warnings.filterwarnings("ignore")

# Con PyInstaller el ejecutable ya trae el paquete embebido; en desarrollo hay
# que poder correr este archivo desde packaging/.
if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


LINE = "─" * 62


def banner(text: str) -> None:
    print("\n  " + LINE)
    print(f"   {text}")
    print("  " + LINE)


def main() -> int:
    banner("AgroSuite — iniciando")

    from agrosuite import certs, db, net
    from agrosuite.config import DATA_DIR, settings
    from agrosuite.modules import pest

    print(f"   Datos en: {DATA_DIR}")

    # --- primera ejecución: preparar base y modelos -----------------------
    db.init_db()
    with db.session() as conn:
        n_fields = db.one(conn, "SELECT COUNT(*) n FROM fields")["n"]

    if n_fields == 0:
        print("\n   Primera ejecución: preparando datos de demostración.")
        print("   Esto tarda algunos segundos y ocurre una sola vez.\n")
        from agrosuite.seed import generate
        counts = generate()
        print(f"   Base creada: {counts['fields']} lotes, "
              f"{counts['harvests']} cosechas, {counts['tickets']} tickets.")

    if not pest.MODEL_PATH.exists():
        print("\n   Entrenando el modelo de predicción de plagas…")
        try:
            _, rep = pest.train()
            print(f"   Listo. ROC-AUC {rep.roc_auc} sobre {rep.n_test} casos de prueba.")
        except Exception as e:
            print(f"   No se pudo entrenar ahora ({e}).")
            print("   El sistema seguirá con la regla agronómica de grados-día.")

    # --- red y certificados ----------------------------------------------
    ips = net.all_lan_ips()
    settings.use_https = os.getenv("AGROSUITE_HTTPS", "1") not in ("0", "false", "no")
    port = net.pick_port("0.0.0.0", settings.https_port if settings.use_https else settings.port)
    settings.port = port

    ssl_ctx = None
    if settings.use_https:
        print("\n   Generando certificado para la red local…")
        crt, key, ca = certs.ensure_server_cert(DATA_DIR / "certs", ips)
        ssl_ctx = (str(crt), str(key))

    scheme = "https" if settings.use_https else "http"
    local_url = f"{scheme}://localhost:{port}/"

    banner("AgroSuite está corriendo")
    print(f"   Tablero (esta PC)   {local_url}")
    if ips:
        print("\n   Desde el celular, en la misma red WiFi:")
        for ip in ips:
            print(f"     App de campo      {scheme}://{ip}:{port}/campo")
    else:
        print("\n   No se detectó red local. Los celulares no podrán conectarse")
        print("   hasta que esta PC esté en una red WiFi o cableada.")

    if settings.use_https and ips:
        print(f"\n   PASO ÚNICO en cada celular — instalar el certificado:")
        print(f"     1. Abrir en el celular:  {scheme}://{ips[0]}:{port}/agrosuite-ca.crt")
        print(f"     2. Aceptar la advertencia del navegador y descargar.")
        print(f"     3. Ajustes › Seguridad › Cifrado y credenciales ›")
        print(f"        Instalar un certificado › Certificado de CA.")
        print(f"     4. Volver a abrir  {scheme}://{ips[0]}:{port}/campo")
        print(f"\n   Sin ese paso la app funciona, pero sin modo offline ni GPS.")

    print("\n   Para detener el servidor: cerrá esta ventana o presioná Ctrl+C.")
    print("  " + LINE + "\n")

    threading.Timer(1.5, lambda: webbrowser.open(local_url)).start()

    from agrosuite.api import create_app
    app = create_app()
    app.run(host="0.0.0.0", port=port, ssl_context=ssl_ctx,
            threaded=True, use_reloader=False, debug=False)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n   Servidor detenido.")
        sys.exit(0)
    except Exception:
        banner("AgroSuite no pudo iniciar")
        traceback.print_exc()
        print("\n   Copiá este mensaje completo para reportar el problema.")
        # Sin esto la consola se cierra al instante y el usuario no ve el error.
        try:
            input("\n   Presioná Enter para cerrar…")
        except EOFError:
            time.sleep(30)
        sys.exit(1)
