"""Tailscale integration.

Why this exists: an iPhone on the same Wi-Fi as the PC is often not reachable
from it at all (client isolation, or the "hotspot" subnet where the phone owns
the AP and the PC is a guest). Tailscale solves reachability *and* the other
half of the problem: iOS Safari only grants camera access to a secure context,
and ``tailscale serve`` provides a real Let's Encrypt certificate for the
machine's ``*.ts.net`` name. So the phone opens HTTPS on a trusted cert with no
self-signed warning, over the tailnet, from any network.

Everything here degrades to "no Tailscale" silently: the app must keep working
on plain LAN HTTP when Tailscale is absent.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass

_TIMEOUT = 4.0
_CACHE_TTL = 15.0
_cache: dict[int | None, tuple[float, "TailscaleInfo"]] = {}


@dataclass(slots=True)
class TailscaleInfo:
    available: bool = False
    ip: str = ""
    dns_name: str = ""
    https_serving: bool = False
    serving_port: int = 0
    serving_target: str = ""

    @property
    def https_url(self) -> str:
        """The tailnet HTTPS base URL, if `tailscale serve` is proxying us."""
        if not (self.https_serving and self.dns_name):
            return ""
        suffix = "" if self.serving_port == 443 else f":{self.serving_port}"
        return f"https://{self.dns_name}{suffix}"

    def as_dict(self) -> dict:
        return {
            "available": self.available,
            "ip": self.ip,
            "dns_name": self.dns_name,
            "https_serving": self.https_serving,
            "serving_port": self.serving_port,
            "https_url": self.https_url,
        }


def _run(args: list[str]) -> str:
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def probe(app_port: int | None = None, *, use_cache: bool = True) -> TailscaleInfo:
    """Detect the tailnet name, IP and whether `serve` fronts our port.

    ``probe`` shells out to the CLI, which costs a few milliseconds; results are
    cached briefly because the pairing endpoint is polled by the GUI and the
    answer changes only when the user reconfigures Tailscale.
    """
    if use_cache:
        hit = _cache.get(app_port)
        if hit and (time.monotonic() - hit[0]) < _CACHE_TTL:
            return hit[1]

    info = _probe_uncached(app_port)
    _cache[app_port] = (time.monotonic(), info)
    return info


def invalidate_cache() -> None:
    _cache.clear()


def _probe_uncached(app_port: int | None = None) -> TailscaleInfo:
    info = TailscaleInfo()
    if not shutil.which("tailscale"):
        return info

    raw = _run(["tailscale", "status", "--json"])
    if not raw.strip():
        return info
    try:
        status = json.loads(raw)
    except json.JSONDecodeError:
        return info
    if status.get("BackendState") != "Running":
        return info

    info.available = True
    me = status.get("Self") or {}
    ips = me.get("TailscaleIPs") or []
    info.ip = next((ip for ip in ips if ":" not in ip), "")
    info.dns_name = str(me.get("DNSName") or "").rstrip(".")

    serve_raw = _run(["tailscale", "serve", "status", "--json"])
    if not serve_raw.strip():
        return info
    try:
        serve = json.loads(serve_raw)
    except json.JSONDecodeError:
        return info

    for host_port, cfg in (serve.get("Web") or {}).items():
        handlers = (cfg or {}).get("Handlers") or {}
        root = handlers.get("/") or {}
        proxy = str(root.get("Proxy") or "")
        if not proxy:
            continue
        if app_port is not None and f":{app_port}" not in proxy:
            continue
        port = 443
        if ":" in host_port:
            try:
                port = int(host_port.rsplit(":", 1)[1])
            except ValueError:
                port = 443
        info.https_serving = True
        info.serving_port = port
        info.serving_target = proxy
        break

    return info


def enable_serve(app_port: int) -> tuple[bool, str]:
    """Point `tailscale serve --https=443` at the local app.

    Requires the user to be a Tailscale operator (usually true for the machine
    owner); on failure the caller falls back to plain HTTP pairing.
    """
    if not shutil.which("tailscale"):
        return False, "tailscale コマンドが見つかりません"
    try:
        result = subprocess.run(
            ["tailscale", "serve", "--bg", "--https=443", f"http://127.0.0.1:{app_port}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        return False, output or "tailscale serve に失敗しました"
    invalidate_cache()
    return True, output


def disable_serve() -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["tailscale", "serve", "--https=443", "off"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    invalidate_cache()
    return result.returncode == 0, (result.stdout + result.stderr).strip()
