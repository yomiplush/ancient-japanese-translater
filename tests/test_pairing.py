from __future__ import annotations

import os

import pytest

from ajt import config as config_mod
from ajt.config import Config
from ajt.pairing import candidate_urls, ensure_cert, lan_ip, qr_png, qr_svg


def test_lan_ip_is_a_valid_address():
    ip = lan_ip()
    assert ip.count(".") == 3
    assert all(0 <= int(p) <= 255 for p in ip.split("."))


def test_candidate_urls_include_token():
    urls = candidate_urls(8765, "abc", tls=False)
    assert urls[0].startswith("http://")
    assert "abc" in urls[0]
    assert "/camera" in urls[0]


def test_candidate_urls_use_https_when_tls():
    urls = candidate_urls(8765, "abc", tls=True)
    assert urls[0].startswith("https://")


def test_qr_png_is_a_png():
    data = qr_png("http://example.test/camera?t=x")
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_qr_svg_is_svg():
    svg = qr_svg("http://example.test/x")
    assert "<svg" in svg and "path" in svg


def test_qr_svg_has_viewbox_so_css_can_scale_it():
    """回帰テスト: viewBox が無いと CSS で縮小したとき切り取られて読めない。

    実際にこの状態で GUI の QR がスキャン不能になっていた。
    """
    svg = qr_svg("http://example.test/camera?t=x")
    assert "viewBox=" in svg
    # 固定サイズがあると viewBox 無しで描画され、リサイズ時に crop される。
    assert "width=" not in svg.split(">", 1)[0]
    assert "height=" not in svg.split(">", 1)[0]


def test_qr_png_decodes_to_the_payload():
    """PNG も実際にデコードできること（生成だけでなく内容の検証）。"""
    import cv2
    import numpy as np

    payload = "http://172.20.10.3:8765/camera?t=abc123"
    png = qr_png(payload, scale=8)
    img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    decoded, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
    assert decoded == payload


def test_ensure_cert_generates_and_reuses(tmp_path, monkeypatch):
    monkeypatch.setenv("AJT_STATE_DIR", str(tmp_path))
    cert, key = ensure_cert(host="192.168.0.10")
    assert cert.is_file() and key.is_file()
    before = cert.read_bytes()
    cert2, key2 = ensure_cert(host="192.168.0.10")
    assert cert2 == cert and key2 == key
    assert cert2.read_bytes() == before  # not regenerated


def test_ensure_cert_includes_lan_san(tmp_path, monkeypatch):
    monkeypatch.setenv("AJT_STATE_DIR", str(tmp_path))
    from cryptography import x509

    cert_path, _ = ensure_cert(host="10.1.2.3")
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    values = [str(v.value) for v in san]
    assert "10.1.2.3" in values
    assert "127.0.0.1" in values
    assert "localhost" in values


def test_ensure_cert_key_is_private(tmp_path, monkeypatch):
    monkeypatch.setenv("AJT_STATE_DIR", str(tmp_path))
    _, key = ensure_cert()
    mode = key.stat().st_mode & 0o777
    assert mode == 0o600


def test_ensure_cert_regenerates_when_host_missing(tmp_path, monkeypatch):
    """Wi-FiのIPが変わったら証明書を作り直す（古いIPのままだと警告になる）。"""
    monkeypatch.setenv("AJT_STATE_DIR", str(tmp_path))
    cert1, _ = ensure_cert(host="192.168.0.10")
    first = cert1.read_bytes()
    cert2, _ = ensure_cert(host="192.168.0.99")
    assert cert2.read_bytes() != first
    from cryptography import x509

    cert = x509.load_pem_x509_certificate(cert2.read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "192.168.0.99" in {str(v.value) for v in san}


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------
def test_provider_auto_prefers_gemini_then_openai(monkeypatch):
    monkeypatch.setenv("AJT_PROVIDER", "auto")
    monkeypatch.setenv("AJT_GEMINI_API_KEY", "g")
    monkeypatch.setenv("AJT_OPENAI_API_KEY", "o")
    assert Config().resolved_provider() == "gemini"


def test_provider_auto_uses_openai_when_only_openai(monkeypatch):
    monkeypatch.setenv("AJT_PROVIDER", "auto")
    monkeypatch.setenv("AJT_GEMINI_API_KEY", "")
    monkeypatch.setenv("AJT_OPENAI_API_KEY", "o")
    assert Config().resolved_provider() == "openai"


def test_provider_auto_falls_back_to_offline(monkeypatch):
    monkeypatch.setenv("AJT_PROVIDER", "auto")
    monkeypatch.setenv("AJT_GEMINI_API_KEY", "")
    monkeypatch.setenv("AJT_OPENAI_API_KEY", "")
    assert Config().resolved_provider() == "offline"


def test_explicit_gemini_without_key_falls_back(monkeypatch):
    monkeypatch.setenv("AJT_PROVIDER", "gemini")
    monkeypatch.setenv("AJT_GEMINI_API_KEY", "")
    assert Config().resolved_provider() == "offline"


def test_explicit_offline_is_respected(monkeypatch):
    monkeypatch.setenv("AJT_PROVIDER", "offline")
    monkeypatch.setenv("AJT_OPENAI_API_KEY", "o")
    assert Config().resolved_provider() == "offline"


def test_token_is_persisted_and_stable(tmp_path, monkeypatch):
    monkeypatch.setenv("AJT_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("AJT_TOKEN", raising=False)
    first = config_mod.get_token()
    second = config_mod.get_token()
    assert first == second
    assert len(first) >= 16
    assert (tmp_path / "token").read_text().strip() == first
    assert (tmp_path / "token").stat().st_mode & 0o777 == 0o600


def test_env_token_overrides_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AJT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AJT_TOKEN", "chosen")
    assert config_mod.get_token() == "chosen"


def test_load_dotenv_does_not_override_existing(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("AJT_TEST_A=from_file\nAJT_TEST_B=from_file\n", encoding="utf-8")
    monkeypatch.setenv("AJT_TEST_A", "from_env")
    monkeypatch.delenv("AJT_TEST_B", raising=False)
    config_mod.load_dotenv(env)
    assert os.environ["AJT_TEST_A"] == "from_env"
    assert os.environ["AJT_TEST_B"] == "from_file"


def test_load_dotenv_missing_file_is_silent(tmp_path):
    config_mod.load_dotenv(tmp_path / "nope.env")
