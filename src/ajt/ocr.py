"""OCR for photographed classical Japanese.

RapidOCR (PP-OCRv4, ONNX, CPU) gives one box per *column* for vertical text,
which is exactly the unit we need: sort boxes right-to-left, then top-to-bottom
inside a column. Everything else here is about not wasting CPU on frames that
did not change and about reading each column at a sane resolution.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import numpy as np
from PIL import Image, ImageOps


Box = tuple[float, float, float, float]  # x0, y0, x1, y1


@dataclass(slots=True)
class Line:
    text: str
    score: float
    box: Box
    column: int = 0

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "score": round(self.score, 3),
            "box": [round(v, 1) for v in self.box],
            "column": self.column,
        }


@dataclass(slots=True)
class OcrResult:
    text: str
    lines: list[Line] = field(default_factory=list)
    layout: str = "vertical"
    mean_score: float = 0.0
    elapsed_ms: int = 0
    cached: bool = False
    empty: bool = False

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "lines": [line.as_dict() for line in self.lines],
            "layout": self.layout,
            "mean_score": round(self.mean_score, 3),
            "elapsed_ms": self.elapsed_ms,
            "cached": self.cached,
            "empty": self.empty,
        }


def _decode(data: bytes | str | Path | np.ndarray | Image.Image) -> Image.Image:
    if isinstance(data, Image.Image):
        return data.convert("RGB")
    if isinstance(data, np.ndarray):
        return Image.fromarray(data).convert("RGB")
    if isinstance(data, (str, Path)):
        return Image.open(data).convert("RGB")
    import io

    return Image.open(io.BytesIO(data)).convert("RGB")


def _quad_to_xyxy(quad: Iterable) -> Box:
    xs = [float(p[0]) for p in quad]
    ys = [float(p[1]) for p in quad]
    return min(xs), min(ys), max(xs), max(ys)


def _boxes_share_column(a: Box, b: Box) -> bool:
    """True when two boxes overlap horizontally enough to be the same column."""
    overlap = min(a[2], b[2]) - max(a[0], b[0])
    narrower = min(a[2] - a[0], b[2] - b[0])
    return narrower > 0 and overlap > 0.5 * narrower


def reading_order(lines: list[Line], layout: str) -> list[Line]:
    """Order recognised boxes the way a human reads the page.

    Vertical Japanese: columns right-to-left, and within a column the split
    boxes top-to-bottom. Horizontal text: rows top-to-bottom, left-to-right.
    """
    if not lines:
        return []

    if layout == "horizontal":
        rows: list[list[Line]] = []
        for line in sorted(lines, key=lambda l: l.box[1]):
            cy = (line.box[1] + line.box[3]) / 2
            for row in rows:
                rcy = sum((r.box[1] + r.box[3]) / 2 for r in row) / len(row)
                height = max(r.box[3] - r.box[1] for r in row)
                if abs(cy - rcy) < max(height * 0.6, 1):
                    row.append(line)
                    break
            else:
                rows.append([line])
        ordered = []
        for row in sorted(rows, key=lambda r: min(x.box[1] for x in r)):
            ordered.extend(sorted(row, key=lambda l: l.box[0]))
        return ordered

    # vertical: cluster into columns by horizontal overlap
    columns: list[list[Line]] = []
    for line in sorted(lines, key=lambda l: -l.box[0]):
        for col in columns:
            if any(_boxes_share_column(line.box, c.box) for c in col):
                col.append(line)
                break
        else:
            columns.append([line])
    columns.sort(key=lambda c: -max(x.box[2] for x in c))
    ordered: list[Line] = []
    for idx, col in enumerate(columns):
        for line in sorted(col, key=lambda l: l.box[1]):
            line.column = idx
            ordered.append(line)
    return ordered


def detect_layout(lines: list[Line]) -> str:
    if not lines:
        return "vertical"
    tall = sum(1 for l in lines if (l.box[3] - l.box[1]) > (l.box[2] - l.box[0]) * 1.5)
    return "vertical" if tall > len(lines) * 0.5 else "horizontal"


def join_columns(ordered: list[Line]) -> str:
    """Flatten ordered vertical lines into text.

    The detector routinely splits one physical column into several boxes (a
    page break, a smudge, a change in stroke weight). Those pieces are one
    continuous run of text, so they are concatenated *without* a separator;
    only a change of column starts a new line. Emitting a newline per box
    would shred sentences into meaningless fragments.
    """
    if not ordered:
        return ""
    out: list[str] = []
    prev_column = None
    for line in ordered:
        if prev_column is not None and line.column != prev_column:
            out.append("\n")
        out.append(line.text)
        prev_column = line.column
    return "".join(out)


# --------------------------------------------------------------------------
# UI chrome rejection
# --------------------------------------------------------------------------
# Photographing a screen (or an NDL viewer) drags in URL bars, battery
# percentages, dates, and toolbar labels. They OCR cleanly, so confidence does
# not catch them, and they poison the translation.
_CHROME_PATTERNS = [
    re.compile(r"^https?://", re.I),
    re.compile(r"\.(go\.jp|jp|com|org|net|io)(/|$)", re.I),
    re.compile(r"^[\w.-]+\.(go\.jp|jp|com|org|net|io)\b", re.I),
    re.compile(r"^\d{1,3}%[+\-]?$"),                              # 21%+
    re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b.*\d", re.I),    # Wed Nov 18
    re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$"),                      # clock
    re.compile(r"^(English|日本語|简体中文|한국어)$", re.I),
    re.compile(r"^(連続表示|連続|表示|検索|番号|右左|ページ|次へ|前へ|拡大|縮小|画像|保存)$"),
    re.compile(r"^\d+$"),                                        # bare page counters
    re.compile(r"^[A-Za-z][A-Za-z0-9]{0,3}$"),                    # stray "D", "V2", "LOn1"
    # デジタルコレクションのビューアが写り込んだ場合のタイトル行
    # (例: 「霊能真柱2巻[1] - 国立国会図書館」)。OCR誤りも許容する。
    re.compile(r"\[\s*\d+\s*\]"),
    re.compile(r"国立国[会．・.]?[会立区]?"),
    re.compile(r"^[^。、]{0,24}[卷巻][\s\d]*[\[\(]"),
]

# A line of a Japanese book contains kana or kanji. Anything without a single
# CJK character is a toolbar label, a filename, or a stray glyph.
_CJK = re.compile(r"[\u3041-\u309f\u30a1-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U0001b001-\U0001b11f]")


def _is_chrome_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    if any(p.match(stripped) for p in _CHROME_PATTERNS):
        return True
    # 本文には必ず仮名か漢字が含まれる。含まない行はUI部品とみなす。
    if not _CJK.search(stripped):
        return True
    return False


def filter_chrome(lines: list[Line]) -> list[Line]:
    """Drop UI chrome lines, keeping the page body.

    Deliberately conservative: a wrongly dropped column is unrecoverable,
    whereas a stray toolbar label merely looks untidy. If filtering would
    remove everything, the original text is returned.
    """
    kept = [l for l in lines if not _is_chrome_text(l.text)]
    return kept or lines


class OcrEngine:
    """Thread-safe wrapper around RapidOCR.

    RapidOCR/ONNX sessions are not safe to share across threads, so the model
    is guarded by a lock and the engine is meant to be used from a small worker
    pool rather than from the event loop.
    """

    def __init__(self, threads: int = 8, max_side: int = 1280, min_score: float = 0.45):
        self.threads = max(1, threads)
        self.max_side = max_side
        self.min_score = min_score
        self._engine = None
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, OcrResult] = OrderedDict()
        self._cache_size = 128

    def _ensure(self):
        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR

            self._engine = RapidOCR(intra_op_num_threads=self.threads)
        return self._engine

    def warmup(self) -> None:
        with self._lock:
            self._ensure()

    def _prepare(self, image: Image.Image) -> Image.Image:
        if image.mode != "RGB":
            image = image.convert("RGB")
        w, h = image.size
        longest = max(w, h)
        if longest > self.max_side:
            scale = self.max_side / longest
            image = image.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        return image

    def _cache_key(self, image: Image.Image) -> str:
        small = image.convert("L").resize((32, 32), Image.BILINEAR)
        return hashlib.sha1(small.tobytes()).hexdigest()

    def recognize(self, data: bytes | np.ndarray | Image.Image, *, refine: bool = True) -> OcrResult:
        started = time.perf_counter()
        image = self._prepare(_decode(data))
        key = self._cache_key(image)

        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                cached = OcrResult(**{**{f: getattr(hit, f) for f in hit.__slots__}})
                cached.cached = True
                return cached

            raw = self._run(image, refine=refine)
            result = self._assemble(raw)
            result.elapsed_ms = int((time.perf_counter() - started) * 1000)
            self._cache[key] = result
            if len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
            return result

    def _run(self, image: Image.Image, *, refine: bool) -> list[Line]:
        engine = self._ensure()
        res, _ = engine(np.array(image))
        lines: list[Line] = []
        for item in res or []:
            quad, text, score = item[0], item[1], float(item[2])
            text = text.strip()
            if not text:
                continue
            box = _quad_to_xyxy(quad)
            lines.append(Line(text=text, score=score, box=box))

        if refine:
            lines = [self._refine_line(image, line) for line in lines]

        kept = [l for l in lines if l.score >= self.min_score]
        # If the threshold wipes the page, fall back to everything: a low score
        # on a badly lit photo is still better than an empty screen.
        return kept or lines

    def _refine_line(self, image: Image.Image, line: Line) -> Line:
        """Re-read a tall column rotated to horizontal.

        PP-OCR already rotates tall crops, but on narrow columns the extra
        attempt recovers stroke details (`强` vs `强`) often enough to be worth
        the ~15 ms. We only keep the result when it is clearly better.
        """
        x0, y0, x1, y1 = line.box
        w, h = x1 - x0, y1 - y0
        if h <= w * 1.8 or w < 8 or h < 40:
            return line
        pad = 4
        crop = image.crop(
            (
                max(0, int(x0) - pad),
                max(0, int(y0) - pad),
                min(image.width, int(x1) + pad),
                min(image.height, int(y1) + pad),
            )
        )
        rotated = crop.transpose(Image.ROTATE_90)
        target_h = 48
        scale = target_h / max(1, rotated.height)
        if scale > 1:
            rotated = rotated.resize(
                (max(1, int(rotated.width * scale)), target_h), Image.LANCZOS
            )
        try:
            out = self._engine(np.array(rotated.convert("RGB")), use_det=False, use_cls=False, use_rec=True)
        except Exception:
            return line
        if not out or not out[0]:
            return line
        best_text, best_score = out[0][0][0].strip(), float(out[0][0][1])
        if best_text and best_score > line.score + 0.03:
            return Line(text=best_text, score=best_score, box=line.box)
        return line

    def _assemble(self, lines: list[Line]) -> OcrResult:
        if not lines:
            return OcrResult(text="", lines=[], empty=True)
        layout = detect_layout(lines)
        ordered = reading_order(filter_chrome(lines), layout)
        text = join_columns(ordered) if layout == "vertical" else "\n".join(
            l.text for l in ordered
        )
        mean = sum(l.score for l in ordered) / len(ordered)
        return OcrResult(
            text=text,
            lines=ordered,
            layout=layout,
            mean_score=mean,
            empty=not text.strip(),
        )


class FrameGate:
    """Cheap scene-change detector.

    Compares a 16x16 grayscale thumbnail of each frame with the last accepted
    one. ``threshold`` is the mean absolute difference in 0..255 units; small
    values mean "the phone did not move", so we skip OCR entirely.
    """

    def __init__(self, threshold: float = 5.0):
        self.threshold = threshold
        self._last: np.ndarray | None = None

    def reset(self) -> None:
        self._last = None

    def changed(self, image: Image.Image) -> tuple[bool, float]:
        thumb = np.asarray(
            ImageOps.autocontrast(image.convert("L").resize((16, 16), Image.BILINEAR)),
            dtype=np.float32,
        )
        if self._last is None:
            self._last = thumb
            return True, 255.0
        distance = float(np.abs(thumb - self._last).mean())
        if distance >= self.threshold:
            self._last = thumb
            return True, distance
        return False, distance


def preprocess_for_display(image: Image.Image, max_side: int = 1600) -> Image.Image:
    w, h = image.size
    if max(w, h) > max_side:
        scale = max_side / max(w, h)
        image = image.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    return image
