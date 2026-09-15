"""Descubrimiento de la red local.

El .exe corre en la PC de la oficina y los celulares lo consumen por WiFi, así
que al arrancar hay que decirle al usuario a qué dirección conectarse.
"""
from __future__ import annotations

import ipaddress
import socket


def primary_lan_ip() -> str | None:
    """IP de la interfaz que sale a la red, sin enviar un solo paquete.

    El truco del socket UDP 'conectado' hace que el sistema operativo elija la
    interfaz de salida y la exponga en getsockname(); UDP no establece
    conexión, así que no requiere que el destino exista ni haya internet.
    """
    for probe in ("10.255.255.255", "192.168.1.1", "8.8.8.8"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(0.2)
            s.connect((probe, 1))
            ip = s.getsockname()[0]
            if not ip.startswith("127."):
                return ip
        except OSError:
            continue
        finally:
            s.close()
    return None


def all_lan_ips() -> list[str]:
    """Todas las IPv4 privadas de la máquina, la principal primero."""
    found: list[str] = []
    primary = primary_lan_ip()
    if primary:
        found.append(primary)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip in found or ip.startswith("127."):
                continue
            try:
                if ipaddress.ip_address(ip).is_private:
                    found.append(ip)
            except ValueError:
                pass
    except socket.gaierror:
        pass
    return found


def port_is_free(host: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def pick_port(host: str, preferred: int, tries: int = 20) -> int:
    """Devuelve el primer puerto libre a partir del preferido."""
    for p in range(preferred, preferred + tries):
        if port_is_free(host, p):
            return p
    return preferred
