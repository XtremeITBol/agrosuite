"""Configuración central de AgroSuite.

Todos los parámetros salen de variables de entorno con valores por defecto
sensatos, de modo que la app corre sin configuración previa.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)


def _resource_dir() -> Path:
    """Dónde viven los archivos de sólo lectura (el tablero, la PWA).

    PyInstaller descomprime el bundle en un directorio temporal y expone su
    ruta en sys._MEIPASS. Ese directorio se borra al cerrar el programa, así
    que sirve para recursos pero nunca para datos.
    """
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def _writable_data_dir() -> Path:
    """Dónde viven los datos que deben sobrevivir al cierre y a las
    actualizaciones: base, modelos, certificados y fotos de monitoreo.

    Se prefiere una carpeta junto al ejecutable (instalación portable, que es
    lo que una finca espera de un .exe). Si ese lugar es de sólo lectura
    —típico si quedó en Archivos de Programa— se cae al perfil del usuario.
    """
    override = os.getenv("AGROSUITE_DATA_DIR")
    if override:
        return Path(override).expanduser()

    candidates = []
    if FROZEN:
        candidates.append(Path(sys.executable).resolve().parent / "datos-agrosuite")
    else:
        candidates.append(Path(__file__).resolve().parent.parent / "data")
    if os.name == "nt":
        candidates.append(Path(os.getenv("LOCALAPPDATA", Path.home())) / "AgroSuite")
    else:
        candidates.append(Path.home() / ".agrosuite")

    for c in candidates:
        try:
            c.mkdir(parents=True, exist_ok=True)
            probe = c / ".escritura"
            probe.write_text("ok")
            probe.unlink()
            return c
        except OSError:
            continue
    return Path.home()


BASE_DIR = _resource_dir()
WEB_DIR = BASE_DIR / "web"
DATA_DIR = _writable_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


@dataclass
class Settings:
    # --- almacenamiento ---
    db_path: Path = field(default_factory=lambda: Path(os.getenv("AGROSUITE_DB", DATA_DIR / "agrosuite.db")))

    # --- servidor ---
    host: str = os.getenv("AGROSUITE_HOST", "127.0.0.1")
    port: int = int(os.getenv("AGROSUITE_PORT", "8000"))
    debug: bool = _env_bool("AGROSUITE_DEBUG", False)
    # HTTPS con CA local: obligatorio para que la PWA funcione en los celulares
    # (service worker, GPS y cámara exigen contexto seguro fuera de localhost).
    use_https: bool = _env_bool("AGROSUITE_HTTPS", False)
    https_port: int = int(os.getenv("AGROSUITE_HTTPS_PORT", "8443"))

    # --- clima ---
    # Open-Meteo es gratuito y no requiere API key. Si no hay red, el sistema
    # usa el clima sintético almacenado en la base.
    weather_api_url: str = os.getenv(
        "AGROSUITE_WEATHER_URL", "https://archive-api.open-meteo.com/v1/archive"
    )
    weather_forecast_url: str = os.getenv(
        "AGROSUITE_FORECAST_URL", "https://api.open-meteo.com/v1/forecast"
    )
    weather_online: bool = _env_bool("AGROSUITE_WEATHER_ONLINE", False)
    weather_timeout_s: float = float(os.getenv("AGROSUITE_WEATHER_TIMEOUT", "8"))

    # --- región por defecto (Santa Cruz, Bolivia: zona sojera) ---
    default_lat: float = float(os.getenv("AGROSUITE_LAT", "-17.78"))
    default_lon: float = float(os.getenv("AGROSUITE_LON", "-63.18"))

    # --- chatbot ---
    # Adaptador LLM opcional. Sin key, el bot funciona 100% offline con
    # clasificación de intención + recuperación TF-IDF.
    llm_provider: str = os.getenv("AGROSUITE_LLM_PROVIDER", "none")  # none | anthropic | openai
    llm_api_key: str = os.getenv("AGROSUITE_LLM_API_KEY", "")
    llm_model: str = os.getenv("AGROSUITE_LLM_MODEL", "")
    chatbot_confidence_floor: float = float(os.getenv("AGROSUITE_BOT_FLOOR", "0.35"))

    # --- semilla determinista para datos sintéticos ---
    seed: int = int(os.getenv("AGROSUITE_SEED", "42"))


settings = Settings()
