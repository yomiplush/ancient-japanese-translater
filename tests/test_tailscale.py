from __future__ import annotations

import json

import pytest

from ajt import tailscale
from ajt.tailscale import TailscaleInfo

_STATUS = {
    "BackendState": "Running",
    "MagicDNSSuffix": "example.ts.net",
    "Self": {
        "DNSName": "pc.example.ts.net.",
        "TailscaleIPs": ["100.64.0.5", "fd7a:115c:a1e0::5"],
        "Online": True,
    },
}

_SERVE = {
    "TCP": {"443": {"HTTPS": True}},
    "Web": {
        "pc.example.ts.net:443": {
            "Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}}
        }
    },
}


def _fake_run(serve_json: str, status_json: str = ""):
    """Fake the CLI, distinguishing `status` from `serve status`.

    Order matters: "serve status --json" also contains "status --json", so the
    serve case must be checked first.
    """
    status_json = status_json or json.dumps(_STATUS)

    def run(args, **kwargs):
        joined = " ".join(args)
        if joined.startswith("tailscale serve status"):
            return serve_json
        if joined.startswith("tailscale status"):
            return status_json
        return ""

    return run


@pytest.fixture(autouse=True)
def clear_cache():
    tailscale.invalidate_cache()
    yield
    tailscale.invalidate_cache()


def test_probe_without_binary(monkeypatch):
    monkeypatch.setattr(tailscale.shutil, "which", lambda _: None)
    assert tailscale.probe(8765).available is False


def test_probe_detects_tailnet_and_serving(monkeypatch):
    monkeypatch.setattr(tailscale.shutil, "which", lambda _: "/usr/bin/tailscale")
    monkeypatch.setattr(tailscale, "_run", _fake_run(json.dumps(_SERVE)))
    info = tailscale.probe(8765)
    assert info.available
    assert info.ip == "100.64.0.5"          # IPv4 preferred over IPv6
    assert info.dns_name == "pc.example.ts.net"  # trailing dot stripped
    assert info.https_serving
    assert info.https_url == "https://pc.example.ts.net"


def test_probe_ignores_serve_pointing_elsewhere(monkeypatch):
    monkeypatch.setattr(tailscale.shutil, "which", lambda _: "/usr/bin/tailscale")
    other = json.loads(json.dumps(_SERVE))
    other["Web"]["pc.example.ts.net:443"]["Handlers"]["/"]["Proxy"] = "http://127.0.0.1:9999"
    monkeypatch.setattr(tailscale, "_run", _fake_run(json.dumps(other)))
    info = tailscale.probe(8765)
    assert info.available
    assert not info.https_serving
    assert info.https_url == ""


def test_probe_non_standard_serve_port(monkeypatch):
    monkeypatch.setattr(tailscale.shutil, "which", lambda _: "/usr/bin/tailscale")
    serve = {
        "Web": {
            "pc.example.ts.net:8443": {
                "Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}}
            }
        }
    }
    monkeypatch.setattr(tailscale, "_run", _fake_run(json.dumps(serve)))
    info = tailscale.probe(8765)
    assert info.https_url == "https://pc.example.ts.net:8443"


def test_probe_requires_running_backend(monkeypatch):
    monkeypatch.setattr(tailscale.shutil, "which", lambda _: "/usr/bin/tailscale")
    stopped = dict(_STATUS, BackendState="Stopped")
    monkeypatch.setattr(tailscale, "_run", _fake_run("", json.dumps(stopped)))
    assert not tailscale.probe(8765).available


def test_probe_survives_broken_json(monkeypatch):
    monkeypatch.setattr(tailscale.shutil, "which", lambda _: "/usr/bin/tailscale")
    monkeypatch.setattr(tailscale, "_run", _fake_run("", "not json"))
    assert not tailscale.probe(8765).available


def test_probe_is_cached(monkeypatch):
    calls = {"n": 0}

    def run(args, **kwargs):
        calls["n"] += 1
        return json.dumps(_STATUS) if "status --json" in " ".join(args) else ""

    monkeypatch.setattr(tailscale.shutil, "which", lambda _: "/usr/bin/tailscale")
    monkeypatch.setattr(tailscale, "_run", run)
    tailscale.probe(8765)
    first = calls["n"]
    tailscale.probe(8765)
    assert calls["n"] == first  # second call served from cache


def test_enable_serve_failure_is_reported(monkeypatch):
    class Result:
        returncode = 1
        stdout = ""
        stderr = "Access denied"

    monkeypatch.setattr(tailscale.shutil, "which", lambda _: "/usr/bin/tailscale")
    monkeypatch.setattr(tailscale.subprocess, "run", lambda *a, **k: Result())
    ok, output = tailscale.enable_serve(8765)
    assert not ok
    assert "Access denied" in output


def test_https_url_empty_without_serving():
    assert TailscaleInfo(available=True, dns_name="x.ts.net").https_url == ""
