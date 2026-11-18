from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from ajt.config import Config
from ajt.server import create_app


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("AJT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AJT_TOKEN", "testtoken")
    cfg = Config()
    cfg.token = "testtoken"
    cfg.provider = "offline"
    app = create_app(cfg)

    # Keep the OCR model out of unit tests; the assembly path is covered by
    # test_ocr.py against a stub.
    st = app.state.ajt
    from ajt.ocr import OcrResult, Line

    def fake_recognize(image, refine=True):
        return OcrResult(
            text="いとあはれなり",
            lines=[Line("いとあはれなり", 0.9, (0, 0, 40, 300))],
            layout="vertical",
            mean_score=0.9,
        )

    st.ocr.recognize = fake_recognize
    st.gate.changed = lambda image: (True, 255.0)
    # Act as the PC's own browser by default; remote-caller tests build their
    # own TestClient with a different peer address.
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        yield c


def _jpeg_bytes(size=(64, 96), color=(240, 240, 230)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


def test_health_is_open(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_root_serves_gui(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "現代口語" in r.text


def test_static_assets_exist(client):
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_camera_page_injects_token(client):
    r = client.get("/camera", params={"t": "testtoken"})
    assert r.status_code == 200
    assert "testtoken" in r.text
    assert "__TOKEN__" not in r.text


def test_frame_requires_token(client):
    r = client.post("/api/frame", files={"file": ("f.jpg", _jpeg_bytes(), "image/jpeg")})
    assert r.status_code == 401


def test_frame_with_query_token(client):
    r = client.post(
        "/api/frame",
        params={"t": "testtoken"},
        files={"file": ("f.jpg", _jpeg_bytes(), "image/jpeg")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ocr"]["text"] == "いとあはれなり"
    assert body["translation"]["modern"]
    assert body["preview"]


def test_frame_with_bearer_token(client):
    r = client.post(
        "/api/frame",
        headers={"Authorization": "Bearer testtoken"},
        files={"file": ("f.jpg", _jpeg_bytes(), "image/jpeg")},
    )
    assert r.status_code == 200


def test_bad_token_rejected(client):
    r = client.post(
        "/api/frame",
        params={"t": "wrong"},
        files={"file": ("f.jpg", _jpeg_bytes(), "image/jpeg")},
    )
    assert r.status_code == 401


def test_frame_rejects_non_image(client):
    r = client.post(
        "/api/frame",
        params={"t": "testtoken"},
        files={"file": ("f.txt", b"not an image", "text/plain")},
    )
    assert r.status_code == 400


def test_raw_frame_endpoint(client):
    r = client.post(
        "/api/frame/raw",
        params={"t": "testtoken"},
        content=_jpeg_bytes(),
        headers={"Content-Type": "image/jpeg"},
    )
    assert r.status_code == 200
    assert r.json()["ocr"]["text"]


def test_text_endpoint(client):
    r = client.post("/api/text", params={"t": "testtoken"}, json={"text": "いとあはれなりけり"})
    assert r.status_code == 200
    assert "たいそう" in r.json()["translation"]["modern"]


def test_text_endpoint_rejects_empty(client):
    r = client.post("/api/text", params={"t": "testtoken"}, json={"text": "  "})
    assert r.status_code == 400


def test_skipped_frame_when_scene_unchanged(client):
    client.app.state.ajt.gate.changed = lambda image: (False, 0.4)
    r = client.post(
        "/api/frame",
        params={"t": "testtoken"},
        files={"file": ("f.jpg", _jpeg_bytes(), "image/jpeg")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["skipped"] is True
    assert "ocr" not in body


def test_force_bypasses_gate(client):
    client.app.state.ajt.gate.changed = lambda image: (False, 0.4)
    r = client.post(
        "/api/frame",
        params={"t": "testtoken", "force": "1"},
        files={"file": ("f.jpg", _jpeg_bytes(), "image/jpeg")},
    )
    assert r.json()["skipped"] is False


def test_empty_ocr_yields_no_translation(client):
    from ajt.ocr import OcrResult

    client.app.state.ajt.ocr.recognize = lambda image, refine=True: OcrResult(
        text="", lines=[], empty=True
    )
    r = client.post(
        "/api/frame",
        params={"t": "testtoken"},
        files={"file": ("f.jpg", _jpeg_bytes(), "image/jpeg")},
    )
    assert r.json()["translation"] is None


def test_pair_endpoint_returns_qr(client):
    r = client.get("/api/pair")
    body = r.json()
    assert body["token"] == "testtoken"
    assert body["url"].startswith("http")
    assert "<svg" in body["svg"]


def test_pair_endpoint_refuses_remote_callers_without_token(client):
    """トークン自体が秘密なので、LAN越しに配ってはいけない。"""
    with TestClient(client.app, client=("192.168.1.50", 54321)) as remote:
        r = remote.get("/api/pair")
    assert r.status_code == 403


def test_pair_endpoint_allows_remote_with_token(client):
    with TestClient(client.app, client=("192.168.1.50", 54321)) as remote:
        r = remote.get("/api/pair", params={"t": "testtoken"})
    assert r.status_code == 200


def test_pair_qr_png_refuses_remote_callers(client):
    with TestClient(client.app, client=("10.0.0.9", 5555)) as remote:
        r = remote.get("/api/pair/qr.png")
    assert r.status_code == 403


def test_pair_qr_png(client):
    r = client.get("/api/pair/qr.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content[:2] == b"\xff\xd8" or r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_reset_clears_stats(client):
    client.post(
        "/api/frame",
        params={"t": "testtoken"},
        files={"file": ("f.jpg", _jpeg_bytes(), "image/jpeg")},
    )
    client.post("/api/reset", params={"t": "testtoken"})
    health = client.get("/api/health").json()
    assert health["stats"]["frames"] == 0


def test_last_result_is_stored(client):
    client.post(
        "/api/frame",
        params={"t": "testtoken"},
        files={"file": ("f.jpg", _jpeg_bytes(), "image/jpeg")},
    )
    r = client.get("/api/last", params={"t": "testtoken"})
    assert r.json()["ocr"]["text"]


def test_websocket_rejects_bad_token(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/ws?t=nope") as ws:
            ws.receive_text()


def test_websocket_ping_pong(client):
    with client.websocket_connect("/ws?t=testtoken") as ws:
        ws.send_text("ping")
        import json

        msg = json.loads(ws.receive_text())
        assert "pong" in msg
