"""Autoridad certificadora local para servir HTTPS en la red de la finca.

Por qué esto existe: una PWA sólo se instala, registra service worker, usa GPS
y abre la cámara en un **contexto seguro**. `localhost` cuenta como seguro,
pero `http://192.168.1.50:8000` no. Sin HTTPS, la app en el celular queda
reducida a una página web común — sin offline y sin geolocalización.

La solución sin costo ni dominio propio es una CA local: se genera una vez,
se instala en los celulares una sola vez, y a partir de ahí el servidor de la
finca es de confianza para esos dispositivos.

Todo se hace con `cryptography` (Python puro), no con el binario de OpenSSL,
para que el .exe de Windows funcione en máquinas que no lo tienen instalado.
"""
from __future__ import annotations

import datetime as dt
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

CA_DAYS = 3650          # la CA se instala una vez; que dure
LEAF_DAYS = 397         # máximo que aceptan los navegadores modernos


def _name(cn: str) -> x509.Name:
    return x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "BO"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "AgroSuite"),
        x509.NameAttribute(NameOID.COMMON_NAME, cn),
    ])


def _write(path: Path, data: bytes, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if private:
        try:
            path.chmod(0o600)
        except OSError:
            pass     # Windows no soporta chmod POSIX; el archivo queda en el perfil del usuario


def ensure_ca(cert_dir: Path) -> tuple[Path, Path]:
    """Crea (o reutiliza) la CA local. Devuelve (cert, key)."""
    ca_cert, ca_key = cert_dir / "agrosuite-ca.crt", cert_dir / "agrosuite-ca.key"
    if ca_cert.exists() and ca_key.exists():
        return ca_cert, ca_key

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(dt.timezone.utc)
    subject = _name("AgroSuite Local CA")
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_cert_sign=True, crl_sign=True,
            content_commitment=False, key_encipherment=False, data_encipherment=False,
            key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write(ca_cert, cert.public_bytes(serialization.Encoding.PEM))
    _write(ca_key, key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()), private=True)
    return ca_cert, ca_key


def ensure_server_cert(cert_dir: Path, hosts: list[str]) -> tuple[Path, Path, Path]:
    """Certificado de servidor firmado por la CA local, con SAN para cada IP.

    Se regenera si cambió el conjunto de direcciones (típico al cambiar de red
    WiFi o si el router reasigna la IP por DHCP) o si está por vencer.
    Devuelve (cert, key, ca_cert).
    """
    cert_dir = Path(cert_dir)
    ca_cert_path, ca_key_path = ensure_ca(cert_dir)
    crt, key_path = cert_dir / "server.crt", cert_dir / "server.key"
    sans_file = cert_dir / "server.sans"

    wanted = sorted({"localhost", "127.0.0.1", *hosts})
    current = sans_file.read_text().splitlines() if sans_file.exists() else []

    fresh = False
    if crt.exists() and key_path.exists() and sorted(current) == wanted:
        try:
            existing = x509.load_pem_x509_certificate(crt.read_bytes())
            fresh = existing.not_valid_after_utc > dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=14)
        except Exception:
            fresh = False
    if fresh:
        return crt, key_path, ca_cert_path

    ca_cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
    ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)

    alt: list[x509.GeneralName] = []
    for h in wanted:
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            alt.append(x509.DNSName(h))

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name("AgroSuite Server"))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=LEAF_DAYS))
        .add_extension(x509.SubjectAlternativeName(alt), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.ObjectIdentifier("1.3.6.1.5.5.7.3.1")]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    _write(crt, cert.public_bytes(serialization.Encoding.PEM))
    _write(key_path, key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()), private=True)
    _write(sans_file, "\n".join(wanted).encode())
    return crt, key_path, ca_cert_path


def describe(cert_path: Path) -> dict:
    c = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
    try:
        sans = [str(g.value) for g in c.extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value]
    except x509.ExtensionNotFound:
        sans = []
    return {
        "subject": c.subject.rfc4514_string(),
        "issuer": c.issuer.rfc4514_string(),
        "valid_until": c.not_valid_after_utc.date().isoformat(),
        "hosts": sans,
    }
