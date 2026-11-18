from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from ajt.ocr import (
    FrameGate,
    Line,
    OcrEngine,
    _is_chrome_text,
    detect_layout,
    filter_chrome,
    join_columns,
    reading_order,
)


def _line(text, x0, y0, x1, y1, score=0.9):
    return Line(text=text, score=score, box=(x0, y0, x1, y1))


def test_reading_order_vertical_columns_right_to_left():
    # Two columns: the right-hand one (larger x) must come first.
    lines = [
        _line("左", 10, 10, 40, 200),
        _line("右", 100, 10, 130, 200),
    ]
    ordered = reading_order(lines, "vertical")
    assert [l.text for l in ordered] == ["右", "左"]


def test_reading_order_vertical_within_column_top_to_bottom():
    lines = [
        _line("下", 100, 200, 130, 380),
        _line("上", 100, 10, 130, 190),
    ]
    ordered = reading_order(lines, "vertical")
    assert [l.text for l in ordered] == ["上", "下"]


def test_reading_order_splits_wide_columns():
    # A column split into two boxes by the detector (very common on photos).
    lines = [
        _line("二", 100, 210, 130, 400),
        _line("一", 102, 10, 132, 200),
        _line("三", 300, 10, 330, 400),
    ]
    ordered = reading_order(lines, "vertical")
    assert [l.text for l in ordered] == ["三", "一", "二"]


def test_reading_order_assigns_column_index():
    lines = [
        _line("右", 200, 10, 230, 200),
        _line("左", 10, 10, 40, 200),
    ]
    ordered = reading_order(lines, "vertical")
    assert ordered[0].column == 0
    assert ordered[1].column == 1


def test_reading_order_horizontal_rows():
    lines = [
        _line("二", 10, 100, 200, 130),
        _line("一", 10, 10, 200, 40),
        _line("右", 210, 10, 400, 40),
    ]
    ordered = reading_order(lines, "horizontal")
    assert [l.text for l in ordered] == ["一", "右", "二"]


def test_reading_order_empty():
    assert reading_order([], "vertical") == []


def test_join_columns_concatenates_split_boxes_within_a_column():
    """検出器は1列を複数ボックスに割る。列内は区切り無しで連結する。"""
    lines = [
        _line("いと", 100, 10, 130, 100),
        _line("あはれなり", 100, 105, 130, 300),
        _line("花咲きけり", 200, 10, 230, 300),
    ]
    ordered = reading_order(lines, "vertical")
    assert join_columns(ordered) == "花咲きけり\nいとあはれなり"


def test_join_columns_newlines_only_between_columns():
    lines = [_line("一", 200, 10, 230, 200), _line("二", 100, 10, 130, 200)]
    assert join_columns(reading_order(lines, "vertical")) == "一\n二"


def test_join_columns_empty():
    assert join_columns([]) == ""


def test_vertical_assembly_keeps_columns_whole(monkeypatch):
    """縦書きページは列単位で1行になり、断片化しないこと。"""
    engine = OcrEngine(threads=1)

    def fake_run(self, image, *, refine):
        return [
            Line("いと", 0.9, (100, 10, 130, 90)),
            Line("あはれなり", 0.9, (100, 95, 130, 300)),
            Line("花咲きけり", 0.9, (200, 10, 230, 300)),
        ]

    monkeypatch.setattr(OcrEngine, "_run", fake_run)
    result = engine.recognize(Image.new("RGB", (300, 340), (255, 255, 255)))
    assert result.layout == "vertical"
    assert result.text == "花咲きけり\nいとあはれなり"


def test_detect_layout_vertical():
    lines = [_line("あ", 0, 0, 40, 400), _line("い", 60, 0, 100, 380)]
    assert detect_layout(lines) == "vertical"


def test_detect_layout_horizontal():
    lines = [_line("あ" * 10, 0, 0, 400, 40), _line("い" * 10, 0, 60, 400, 100)]
    assert detect_layout(lines) == "horizontal"


def test_detect_layout_empty_defaults_vertical():
    assert detect_layout([]) == "vertical"


def test_frame_gate_first_frame_always_changes():
    gate = FrameGate(threshold=5.0)
    img = Image.new("RGB", (64, 64), (10, 10, 10))
    changed, distance = gate.changed(img)
    assert changed and distance == 255.0


def test_frame_gate_ignores_identical_frames():
    gate = FrameGate(threshold=5.0)
    img = Image.new("RGB", (64, 64), (10, 10, 10))
    gate.changed(img)
    changed, distance = gate.changed(img.copy())
    assert not changed
    assert distance == 0.0


def test_frame_gate_detects_real_change():
    gate = FrameGate(threshold=5.0)
    gate.changed(Image.new("RGB", (64, 64), (10, 10, 10)))
    changed, distance = gate.changed(Image.new("RGB", (64, 64), (200, 200, 200)))
    assert changed and distance > 5


def test_frame_gate_reset():
    gate = FrameGate(threshold=5.0)
    img = Image.new("RGB", (64, 64), (10, 10, 10))
    gate.changed(img)
    gate.reset()
    changed, distance = gate.changed(img.copy())
    assert changed and distance == 255.0


def test_ocr_assembles_synthetic_page(monkeypatch):
    """Exercise the real assembly path with a stubbed detector."""
    engine = OcrEngine(threads=1, max_side=640, min_score=0.3)

    def fake_run(self, image, *, refine):
        return [
            Line("左列", 0.9, (20, 10, 50, 300)),
            Line("右列", 0.85, (100, 10, 130, 300)),
        ]

    monkeypatch.setattr(OcrEngine, "_run", fake_run)
    result = engine.recognize(Image.new("RGB", (200, 320), (255, 255, 255)))
    assert result.text == "右列\n左列"
    assert result.layout == "vertical"
    assert len(result.lines) == 2


def test_ocr_caches_identical_frames(monkeypatch):
    engine = OcrEngine(threads=1)
    calls = {"n": 0}

    def fake_run(self, image, *, refine):
        calls["n"] += 1
        return [Line("あ", 0.9, (0, 0, 40, 200))]

    monkeypatch.setattr(OcrEngine, "_run", fake_run)
    img = Image.new("RGB", (100, 240), (255, 255, 255))
    first = engine.recognize(img)
    second = engine.recognize(img.copy())
    assert calls["n"] == 1
    assert second.cached and not first.cached
    assert second.text == first.text


def test_ocr_empty_result_is_flagged(monkeypatch):
    engine = OcrEngine(threads=1)
    monkeypatch.setattr(OcrEngine, "_run", lambda self, image, *, refine: [])
    result = engine.recognize(Image.new("RGB", (100, 100), (0, 0, 0)))
    assert result.empty
    assert result.text == ""


def test_min_score_filters_noise_but_never_empties_page(monkeypatch):
    engine = OcrEngine(threads=1, min_score=0.9)

    def weak_run(self, image, *, refine):
        return [Line("あ", 0.2, (0, 0, 40, 200))]

    monkeypatch.setattr(OcrEngine, "_run", weak_run)
    result = engine.recognize(Image.new("RGB", (100, 240), (255, 255, 255)))
    assert result.text == "あ"  # fallback keeps it rather than showing nothing


def test_result_serialises_to_json_safe_types():
    engine = OcrEngine(threads=1)
    result = engine._assemble([Line("あ", 0.91234, (1.234, 2.345, 3.456, 4.567))])
    data = result.as_dict()
    assert data["mean_score"] == pytest.approx(0.912)
    assert data["lines"][0]["box"] == [1.2, 2.3, 3.5, 4.6]


# --------------------------------------------------------------------------
# UI chrome rejection
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "chrome",
    [
        "https://dl.ndl.go.jp/pid/1234",
        "dl.ndl.go.jp",
        "21%+",
        "Wed Nov 18",
        "12:34",
        "English",
        "連続表示",
        "837",
        "LOn1",
        "D",
        "V2",
        "霊能真柱2巻[1] - 国立国会図書館",
        "国立国会図書館",
    ],
)
def test_chrome_lines_are_rejected(chrome):
    # _is_chrome_text decides per line; filter_chrome alone would fall back to
    # "keep everything" when a page has no body text at all.
    assert _is_chrome_text(chrome) is True


@pytest.mark.parametrize(
    "body",
    [
        "いとあはれなりけり",
        "天地自然の理を説く",
        "変体仮名𛀁を含む行",
        "これハ本文デス",
    ],
)
def test_body_lines_are_kept(body):
    lines = [Line(body, 0.9, (0, 0, 40, 200))]
    assert [l.text for l in filter_chrome(lines)] == [body]


def test_filter_chrome_never_empties_a_page_of_real_text():
    lines = [Line("本文", 0.9, (0, 0, 40, 200))]
    assert filter_chrome(lines) == lines


def test_filter_chrome_strips_toolbar_but_keeps_body():
    lines = [
        Line("21%+", 0.99, (0, 0, 10, 10)),
        Line("いとあはれなり", 0.9, (0, 40, 40, 300)),
        Line("dl.ndl.go.jp", 0.98, (0, 310, 100, 330)),
        Line("花咲きけり", 0.88, (60, 40, 100, 300)),
    ]
    kept = [l.text for l in filter_chrome(lines)]
    assert kept == ["いとあはれなり", "花咲きけり"]
