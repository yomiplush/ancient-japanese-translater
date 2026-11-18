"""HTTP API + single-page GUI.

The browser (or a curl loop) posts a JPEG frame; the server runs OCR, then
translation, and streams the result back. A WebSocket closes the loop so the
GUI updates without polling. The API is intentionally boring: it must be
usable from an iPhone camera app, a script, and the bundled UI alike.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image

from . import capture, normalize, pairing, translate
from .config import Config
from .ocr import FrameGate, OcrEngine, preprocess_for_display

WEB_DIR = Path(__file__).parent / "web"

MAX_UPLOAD = 16 * 1024 * 1024


class AppState:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.ocr = OcrEngine(
            threads=cfg.ocr_threads, max_side=cfg.ocr_max_side, min_score=cfg.min_score
        )
        self.translator = translate.build_translator(cfg)
        self.gate = FrameGate(threshold=cfg.gate_distance)
        self.ocr_lock = asyncio.Lock()
        self.stats = {"frames": 0, "ocr": 0, "skipped": 0, "errors": 0}
        self.capture_error = ""
        self.last_result: dict | None = None
        self.subscribers: set[WebSocket] = set()
        self._frame_queue: asyncio.Queue[capture.Frame] = asyncio.Queue(maxsize=2)
        self._stop = asyncio.Event()
        self._source_task: asyncio.Task | None = None

    async def broadcast(self, payload: dict) -> None:
        dead = []
        message = json.dumps(payload, ensure_ascii=False)
        for ws in list(self.subscribers):
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.subscribers.discard(ws)


state: AppState | None = None


def get_state() -> AppState:
    if state is None:
        raise HTTPException(503, "サーバー初期化中です")
    return state


def _is_loopback(request: Request) -> bool:
    client = request.client.host if request.client else ""
    return client in ("127.0.0.1", "::1", "localhost", "")


def require_pairing_access(
    request: Request,
    authorization: str | None = Header(default=None),
    t: str | None = Query(default=None),
) -> None:
    """The pairing payload *is* the secret, so never hand it to the network.

    Anyone who can read the token can drive the camera and the API. Loopback is
    trusted (that is the PC's own GUI); a remote caller either already holds the
    token or must fall back to the URL printed on the PC.
    """
    if _is_loopback(request):
        return
    supplied = t or (authorization or "").removeprefix("Bearer ").strip()
    if supplied != get_state().cfg.token:
        raise HTTPException(403, "ペアリング情報はPC上でのみ表示できます")


def require_token(
    request: Request,
    authorization: str | None = Header(default=None),
    t: str | None = Query(default=None),
) -> None:
    """Accept ``?t=`` (browser convenience) or a bearer header (scripts)."""
    st = get_state()
    supplied = t
    if not supplied and authorization:
        supplied = authorization.removeprefix("Bearer ").strip()
    if not supplied:
        supplied = request.cookies.get("ajt_token")
    if supplied != st.cfg.token:
        raise HTTPException(401, "認証トークンが正しくありません")


def _decode_upload(data: bytes) -> Image.Image:
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "画像が大きすぎます")
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:
        raise HTTPException(400, f"画像を読み込めません: {exc}") from exc
    return image.convert("RGB")


def _image_to_b64(image: Image.Image, quality: int = 70) -> str:
    thumb = preprocess_for_display(image, max_side=900)
    buf = io.BytesIO()
    thumb.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


async def process_frame(
    image: Image.Image,
    *,
    translate_text: bool = True,
    force: bool = False,
) -> dict:
    """OCR -> normalise -> translate. The single path used by every endpoint."""
    st = get_state()
    started = time.perf_counter()
    st.stats["frames"] += 1

    if not force:
        changed, distance = await asyncio.to_thread(st.gate.changed, image)
        if not changed:
            st.stats["skipped"] += 1
            return {
                "skipped": True,
                "distance": round(distance, 2),
                "stats": dict(st.stats),
            }

    async with st.ocr_lock:
        st.stats["ocr"] += 1
        try:
            ocr_result = await asyncio.to_thread(st.ocr.recognize, image)
        except Exception as exc:
            st.stats["errors"] += 1
            raise HTTPException(500, f"OCRに失敗しました: {exc}") from exc

    payload: dict = {
        "ocr": ocr_result.as_dict(),
        "source": normalize.normalize_source(ocr_result.text),
        "stats": dict(st.stats),
        "preview": _image_to_b64(image),
        "total_ms": int((time.perf_counter() - started) * 1000),
    }

    if ocr_result.empty:
        payload["translation"] = None
        payload["skipped"] = False
        st.last_result = payload
        await st.broadcast(payload)
        return payload

    if translate_text:
        try:
            result = await st.translator.translate(ocr_result.text)
            payload["translation"] = result.as_dict()
        except Exception as exc:  # translator already falls back, but be safe
            st.stats["errors"] += 1
            payload["translation"] = {
                "modern": "",
                "backend": "error",
                "confidence": 0.0,
                "notes": f"翻訳に失敗しました: {exc}",
                "glosses": [],
            }

    payload["skipped"] = False
    payload["total_ms"] = int((time.perf_counter() - started) * 1000)
    st.last_result = payload
    await st.broadcast(payload)
    return payload


def create_app(cfg: Config | None = None, *, tls: bool = False) -> FastAPI:
    global state
    cfg = cfg or Config()
    state = AppState(cfg)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await asyncio.to_thread(state.ocr.warmup)
        if cfg.source_url:
            state._source_task = asyncio.create_task(_pump_source(state))
        try:
            yield
        finally:
            state._stop.set()
            if state._source_task:
                state._source_task.cancel()

    app = FastAPI(title="ancient-japanese-translater", version="0.1.0", lifespan=lifespan)
    app.state.ajt = state
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    async def _pump_source(st: AppState) -> None:
        queue: asyncio.Queue[capture.Frame] = st._frame_queue

        async def reader() -> None:
            while not st._stop.is_set():
                try:
                    await capture.start_source(cfg.source_url, queue, st._stop)
                except Exception as exc:  # noqa: BLE001 - keep retrying, but report
                    st.capture_error = f"{type(exc).__name__}: {exc}"
                if st._stop.is_set():
                    return
                await asyncio.sleep(1.0)

        reader_task = asyncio.create_task(reader())
        try:
            while not st._stop.is_set():
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                try:
                    await process_frame(frame.image)
                    st.capture_error = ""
                except Exception as exc:  # noqa: BLE001
                    st.stats["errors"] += 1
                    st.capture_error = f"{type(exc).__name__}: {exc}"
        finally:
            reader_task.cancel()

    # ------------------------------------------------------------------ UI
    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8"))

    @app.get("/camera", response_class=HTMLResponse)
    async def camera_page(t: str = Query(default="")) -> HTMLResponse:
        """Phone-side capture page. Served over HTTPS when available."""
        html = (WEB_DIR / "camera.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__TOKEN__", t))

    @app.get("/api/health")
    async def health() -> dict:
        return {
            "ok": True,
            "provider": cfg.resolved_provider(),
            "ocr_threads": cfg.ocr_threads,
            "source": bool(cfg.source_url),
            "capture_error": state.capture_error,
            "stats": dict(state.stats),
        }

    @app.get("/api/config")
    async def config_info(_: None = Depends(require_token)) -> dict:
        return {
            "provider": cfg.resolved_provider(),
            "provider_setting": cfg.provider,
            "has_openai": cfg.has_openai,
            "has_gemini": cfg.has_gemini,
            "min_score": cfg.min_score,
            "gate_distance": cfg.gate_distance,
        }

    # --------------------------------------------------------------- frame
    @app.post("/api/frame")
    async def post_frame(
        file: UploadFile = File(...),
        translate: bool = Query(default=True),
        force: bool = Query(default=False),
        _: None = Depends(require_token),
    ) -> JSONResponse:
        data = await file.read()
        image = _decode_upload(data)
        result = await process_frame(image, translate_text=translate, force=force)
        return JSONResponse(result)

    @app.post("/api/frame/raw")
    async def post_frame_raw(
        request: Request,
        translate: bool = Query(default=True),
        force: bool = Query(default=True),
        _: None = Depends(require_token),
    ) -> JSONResponse:
        """Body is the raw JPEG - what an IP camera app can POST directly."""
        data = await request.body()
        image = _decode_upload(data)
        result = await process_frame(image, translate_text=translate, force=force)
        return JSONResponse(result)

    @app.post("/api/text")
    async def post_text(payload: dict, _: None = Depends(require_token)) -> dict:
        text = str(payload.get("text", ""))
        if not text.strip():
            raise HTTPException(400, "text が空です")
        result = await state.translator.translate(text)
        return {"source": normalize.normalize_source(text), "translation": result.as_dict()}

    @app.get("/api/last")
    async def last(_: None = Depends(require_token)) -> dict:
        return state.last_result or {}

    @app.post("/api/reset")
    async def reset(_: None = Depends(require_token)) -> dict:
        state.gate.reset()
        state.stats = {"frames": 0, "ocr": 0, "skipped": 0, "errors": 0}
        state.last_result = None
        return {"ok": True}

    # ---------------------------------------------------------- pairing/QR
    @app.get("/api/pair")
    async def pair(
        tls_flag: bool = Query(default=False, alias="tls"),
        _: None = Depends(require_pairing_access),
    ) -> dict:
        url = pairing.candidate_urls(cfg.port, cfg.token, tls=tls_flag or tls, path="/camera")[0]
        return {"url": url, "token": cfg.token, "svg": pairing.qr_svg(url)}

    @app.get("/api/pair/qr.png")
    async def pair_qr_png(
        tls_flag: bool = Query(default=False, alias="tls"),
        _: None = Depends(require_pairing_access),
    ) -> Response:
        url = pairing.candidate_urls(cfg.port, cfg.token, tls=tls_flag or tls, path="/camera")[0]
        return Response(pairing.qr_png(url), media_type="image/png")

    # --------------------------------------------------------------- ws
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket, t: str = Query(default="")) -> None:
        if t != cfg.token:
            await ws.close(code=4401)
            return
        await ws.accept()
        state.subscribers.add(ws)
        try:
            if state.last_result:
                await ws.send_text(json.dumps(state.last_result, ensure_ascii=False))
            while True:
                # Keepalive: the GUI sends pings, we echo.
                msg = await ws.receive_text()
                if msg == "ping":
                    await ws.send_text(json.dumps({"pong": time.time()}))
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            state.subscribers.discard(ws)

    return app
