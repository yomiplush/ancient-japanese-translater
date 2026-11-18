from __future__ import annotations

from ajt import normalize


def test_hentaigana_table_covers_the_block():
    assert len(normalize.HENTAIGANA_TABLE) > 200
    # Every mapping must land on a modern kana, never on hentaigana.
    for src, dst in normalize.HENTAIGANA_TABLE.items():
        assert len(dst) == 1, (src, dst)
        assert not (0x1B001 <= ord(dst) <= 0x1B11F), (src, dst)


def test_hentaigana_conversion():
    # 変体仮名「𛀁」(archaic ye) and a hentaigana あ
    assert normalize.hentaigana_to_hiragana("𛀁") == "え"
    assert normalize.hentaigana_to_hiragana("𛀂") == "あ"
    assert normalize.hentaigana_to_hiragana("あ𛀂か") == "ああか"


def test_hentaigana_combos():
    # ん variants (N-MU-MO) must map to ん, not n/mu
    assert normalize.hentaigana_to_hiragana("𛄝") == "ん"
    assert normalize.hentaigana_to_hiragana("𛁬") == "つ"


def test_kyuujitai_folding():
    assert normalize.kyuujitai_to_shinjitai("靈能真柱") == "霊能真柱"
    assert normalize.kyuujitai_to_shinjitai("國學") == "国学"
    assert normalize.kyuujitai_to_shinjitai("萬歳") == "万歳"
    # Unknown kanji untouched
    assert normalize.kyuujitai_to_shinjitai("天地自然") == "天地自然"


def test_modernize_kana_historical():
    assert normalize.modernize_kana("やうやう") == "ようよう"
    assert normalize.modernize_kana("けふ") == "きょう"
    assert normalize.modernize_kana("てふ") == "ちょう"
    assert normalize.modernize_kana("ゐる") == "いる"
    assert normalize.modernize_kana("くわし") == "かし"


def test_modernize_kana_is_safe_on_modern_text():
    text = "これは現代の文章です。"
    assert normalize.modernize_kana(text) == text


def test_clean_removes_ocr_noise():
    assert normalize.clean("天  地\u3000人") == "天地人"
    assert "|" not in normalize.clean("天|地")


def test_normalize_source_pipeline():
    out = normalize.normalize_source("靈能真柱　やうやう")
    assert out == "霊能真柱ようよう"


def test_looks_classical_detects_modern_text():
    assert not normalize.looks_classical("これは現代語の文章です。ゆっくり読んでください。")


def test_looks_classical_detects_bungo():
    assert normalize.looks_classical("いとあはれなりけり")


def test_looks_classical_handles_empty():
    assert not normalize.looks_classical("   ")
