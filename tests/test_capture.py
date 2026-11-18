from __future__ import annotations

import asyncio
import io

import pytest
from PIL import Image

from ajt.capture import (
    Frame,
    _push_png,
    _split_jpegs,
    mjpeg_frames,
    snapshot_loop,
    start_source,
)


def _jpeg(color=(200, 100, 50), size=(32, 32)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


def test_split_jpegs_extracts_multiple_frames():
    a, b = _jpeg((255, 0, 0)), _jpeg((0, 255, 0))
    frames, rest = _split_jpegs(a + b)
    assert len(frames) == 2
    assert rest == b""
    assert frames[0][:2] == b"\xff\xd8" and frames[0][-2:] == b"\xff\xd9"


def test_split_jpegs_keeps_partial_frame_for_next_chunk():
    a = _jpeg()
    frames, rest = _split_jpegs(a + a[:20])
    assert len(frames) == 1
    assert rest == a[:20]


def test_split_jpegs_ignores_leading_garbage():
    junk = b"Content-Type: image/jpeg\r\n\r\n"
    frames, rest = _split_jpegs(junk + _jpeg())
    assert len(frames) == 1 and rest == b""


def test_split_jpegs_empty():
    assert _split_jpegs(b"") == ([], b"")


def test_push_png_enqueues_frame():
    q: asyncio.Queue[Frame] = asyncio.Queue(maxsize=2)
    _push_png(q, _jpeg(), "test")
    assert q.qsize() == 1
    frame = q.get_nowait()
    assert frame.image.size == (32, 32)
    assert frame.source == "test"


def test_push_png_drops_oldest_when_full():
    q: asyncio.Queue[Frame] = asyncio.Queue(maxsize=2)
    _push_png(q, _jpeg((255, 0, 0)), "old")
    _push_png(q, _jpeg((0, 255, 0)), "mid")
    _push_png(q, _jpeg((0, 0, 255)), "new")
    assert q.qsize() == 2
    newest = q.get_nowait()
    assert newest.source in ("mid", "new")


def test_push_png_ignores_undecodable_bytes():
    q: asyncio.Queue[Frame] = asyncio.Queue(maxsize=2)
    _push_png(q, b"not a jpeg", "bad")
    assert q.qsize() == 0


def test_frame_records_source():
    frame = Frame(image=Image.new("RGB", (4, 4)), taken_at=0.0, source="mjpeg")
    assert frame.source == "mjpeg"


class _FakeResponse:
    def __init__(self, chunks, headers=None, content=b""):
        self._chunks = chunks
        self.headers = headers or {}
        self.content = content
        self._content = content

    def raise_for_status(self):
        return None

    async def aiter_bytes(self, size=8192):
        for chunk in self._chunks:
            yield chunk

    async def aread(self):
        return self._content

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    def __init__(self, response):
        self._response = response
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url):
        self.calls += 1
        return self._response

    async def get(self, url):
        self.calls += 1
        return self._response


def test_mjpeg_frames_reads_stream(monkeypatch):
    jpeg = _jpeg()
    resp = _FakeResponse([jpeg], headers={"content-type": "multipart/x-mixed-replace"})
    monkeypatch.setattr("ajt.capture.httpx.AsyncClient", lambda **kw: _FakeClient(resp))

    q: asyncio.Queue[Frame] = asyncio.Queue(maxsize=4)
    stop = asyncio.Event()

    async def run():
        await mjpeg_frames("http://cam/video", q, stop)

    asyncio.run(run())
    assert q.qsize() == 1
    assert q.get_nowait().source == "mjpeg"


def test_mjpeg_frames_handles_snapshot_endpoint(monkeypatch):
    """アプリが MJPEG ではなく単発 JPEG を返す場合は snapshot として扱う。"""
    resp = _FakeResponse([], headers={"content-type": "image/jpeg"}, content=_jpeg())
    monkeypatch.setattr("ajt.capture.httpx.AsyncClient", lambda **kw: _FakeClient(resp))

    q: asyncio.Queue[Frame] = asyncio.Queue(maxsize=4)
    asyncio.run(mjpeg_frames("http://cam/shot.jpg", q, asyncio.Event()))
    assert q.qsize() == 1
    assert q.get_nowait().source == "snapshot"


def test_snapshot_loop_stops_on_event(monkeypatch):
    resp = _FakeResponse([], headers={"content-type": "image/jpeg"}, content=_jpeg())
    monkeypatch.setattr("ajt.capture.httpx.AsyncClient", lambda **kw: _FakeClient(resp))

    q: asyncio.Queue[Frame] = asyncio.Queue(maxsize=4)
    stop = asyncio.Event()

    async def run():
        task = asyncio.create_task(snapshot_loop("http://cam/shot.jpg", 0.01, q, stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run())
    assert q.qsize() >= 1


def test_start_source_falls_back_to_snapshot_on_stream_error(monkeypatch):
    """MJPEG が失敗しても、スナップショット取得で復帰すること。"""

    class BoomClient(_FakeClient):
        def stream(self, method, url):
            raise RuntimeError("stream unsupported")

    resp = _FakeResponse([], headers={"content-type": "image/jpeg"}, content=_jpeg())
    monkeypatch.setattr("ajt.capture.httpx.AsyncClient", lambda **kw: BoomClient(resp))

    q: asyncio.Queue[Frame] = asyncio.Queue(maxsize=4)
    stop = asyncio.Event()

    async def run():
        task = asyncio.create_task(start_source("http://cam/x", q, stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run())
    assert q.qsize() >= 1
