"""Pairing the phone with the PC.

Two ways to get frames off an iPhone:

* **IP camera app** (recommended, and what the README describes). The app
  serves MJPEG or JPEG snapshots over plain HTTP; the PC pulls it server-side.
  No certificates, no browser permissions, works on every iOS version.
* **Browser camera page.** iOS Safari only grants ``getUserMedia`` to a secure
  context, so the page must be served over HTTPS. We generate a self-signed
  certificate for that, which the phone has to accept once.
"""
from __future__ import annotations

import io
import ipaddress
import socket
from pathlib import Path

import segno

from .config import state_dir


def lan_ip() -> str:
    """Best-effort primary LAN address (no packets are actually sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.4)
            sock.connect(("192.0.2.1", 9))  # TEST-NET-1, never routed
            return sock.getsockname()[0]
    except OSError:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


def candidate_urls(
    port: int,
    token: str,
    tls: bool = False,
    path: str = "/camera",
    tailscale_url: str = "",
) -> list[str]:
    """Pairing URLs, most useful first.

    A tailnet HTTPS URL wins when available: it works from any network the
    phone is on, and iOS only allows camera access on HTTPS, so it is the only
    option that needs neither a self-signed warning nor an IP-camera app.
    """
    urls: list[str] = []
    if tailscale_url:
        urls.append(f"{tailscale_url.rstrip('/')}{path}?t={token}")
    scheme = "https" if tls else "http"
    host = lan_ip()
    urls.append(f"{scheme}://{host}:{port}{path}?t={token}")
    if host != "127.0.0.1":
        urls.append(f"{scheme}://127.0.0.1:{port}{path}?t={token}")
    return urls


def best_pair_url(
    port: int, token: str, tls: bool = False, path: str = "/camera", tailscale_url: str = ""
) -> str:
    return candidate_urls(port, token, tls=tls, path=path, tailscale_url=tailscale_url)[0]


def qr_png(data: str, scale: int = 6) -> bytes:
    """PNG bytes of a QR code for `data` (typically the pairing URL)."""
    buf = io.BytesIO()
    segno.make(data, error="m").save(buf, kind="png", scale=scale, border=2)
    return buf.getvalue()


def qr_svg(data: str, scale: int = 4) -> str:
    """Inline SVG QR.

    ``omitsize=True`` is load-bearing, not cosmetic: without it segno emits
    ``width``/``height`` but no ``viewBox``, so any CSS that resizes the image
    crops the code instead of scaling it (the page showed only the top-left
    finder pattern and no scanner could read it). Omitting the fixed size
    forces a ``viewBox``, which scales correctly.
    """
    return segno.make(data, error="m").svg_inline(scale=scale, border=2, omitsize=True)


def _cert_covers(cert_path: Path, host: str) -> bool:
    """True when the existing cert already names this host in its SANs.

    Reusing a cert that lacks the current LAN address is what produces the
    unhelpful "certificate is not valid for this address" prompt after the
    user's IP changes (which happens constantly on Wi-Fi).
    """
    try:
        from cryptography import x509

        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = {str(v.value) for v in san}
    except Exception:
        return False
    return host in names


def ensure_cert(host: str | None = None) -> tuple[Path, Path]:
    """Generate a self-signed cert for the LAN address, reusing it when valid.

    iOS needs the connection to be HTTPS for camera access; a self-signed cert
    accepted once in Safari is enough.
    """
    host = host or lan_ip()
    cert_dir = state_dir() / "tls"
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = cert_dir / "cert.pem", cert_dir / "key.pem"
    if cert_path.is_file() and key_path.is_file() and _cert_covers(cert_path, host):
        return cert_path, key_path

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import datetime as dt

    cert_dir.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    sans: list[x509.GeneralName] = [x509.DNSName("localhost")]
    for value in {host, "127.0.0.1"}:
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(value)))
        except ValueError:
            sans.append(x509.DNSName(value))

    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "ajt local"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ancient-japanese-translater"),
        ]
    )
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.chmod(0o600)
    return cert_path, key_path
