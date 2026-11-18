"""Server-side frame sources.

The README's model is "iPhone runs an IP camera app, the PC pulls the stream".
iOS camera apps expose one of two shapes, so we auto-detect:

* ``multipart/x-mixed-replace`` MJPEG - the classic ``/video`` endpoint.
* a plain JPEG snapshot endpoint - fetched on an interval.

Both are pushed into an asyncio queue consumed by the OCR loop, so the GUI sees
a steady stream of frames without the browser being involved.
"""
from __future__ import annotations

import asyncio
import io
import time
from dataclasses import dataclass

import httpx
from PIL import Image


@dataclass(slots=True)
class Frame:
    image: Image.Image
    taken_at: float
    source: str = "stream"


_JPEG_SOI = b"\xff\xd8"
_JPEG_EOI = b"\xff\xd9"


def _split_jpegs(buffer: bytes) -> tuple[list[bytes], bytes]:
    """Extract complete JPEGs from an MJPEG byte stream."""
    frames: list[bytes] = []
    while True:
        start = buffer.find(_JPEG_SOI)
        if start < 0:
            return frames, b""
        end = buffer.find(_JPEG_EOI, start + 2)
        if end < 0:
            return frames, buffer[start:]
        frames.append(buffer[start : end + 2])
        buffer = buffer[end + 2 :]


async def mjpeg_frames(url: str, queue: asyncio.Queue[Frame], stop: asyncio.Event) -> None:
    """Read an MJPEG stream and enqueue decoded frames (drops old ones)."""
    buffer = b""
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=None)) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if "image/jpeg" in content_type and "multipart" not in content_type:
                # Snapshot endpoint masquerading as a stream; poll instead.
                data = await resp.aread()
                _push_png(queue, data, "snapshot")
                return
            async for chunk in resp.aiter_bytes(8192):
                if stop.is_set():
                    return
                buffer += chunk
                jpegs, buffer = _split_jpegs(buffer)
                for jpeg in jpegs:
                    _push_png(queue, jpeg, "mjpeg")
                if len(buffer) > 8_000_000:  # stream desync guard
                    buffer = b""


async def snapshot_loop(url: str, interval: float, queue: asyncio.Queue[Frame], stop: asyncio.Event) -> None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        while not stop.is_set():
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                _push_png(queue, resp.content, "snapshot")
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue


def _push_png(queue: asyncio.Queue[Frame], data: bytes, source: str) -> None:
    try:
        image = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return
    frame = Frame(image=image, taken_at=time.time(), source=source)
    try:
        queue.put_nowait(frame)
    except asyncio.QueueFull:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait(frame)
        except asyncio.QueueFull:
            pass


async def start_source(url: str, queue: asyncio.Queue[Frame], stop: asyncio.Event) -> None:
    """Run the reader that matches `url`, falling back to snapshot polling.

    Camera apps frequently advertise an MJPEG endpoint that does not actually
    stream. Falling back to plain snapshot polling keeps the app usable instead
    of leaving a blank screen.
    """
    try:
        await mjpeg_frames(url, queue, stop)
    except Exception:
        if stop.is_set():
            return
        await snapshot_loop(url, 0.4, queue, stop)
