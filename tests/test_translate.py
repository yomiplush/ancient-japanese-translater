from __future__ import annotations

import asyncio

import pytest

from ajt import translate
from ajt.translate import Gloss, OfflineTranslator, Translation, _parse_json_block, _to_translation


def test_parse_json_plain():
    assert _parse_json_block('{"現代語訳": "はい"}')["現代語訳"] == "はい"


def test_parse_json_with_fence():
    raw = '```json\n{"現代語訳": "はい"}\n```'
    assert _parse_json_block(raw)["現代語訳"] == "はい"


def test_parse_json_with_prose_around():
    raw = 'はい、どうぞ:\n{"現代語訳": "はい"}\n以上です。'
    assert _parse_json_block(raw)["現代語訳"] == "はい"


def test_parse_json_garbage_returns_empty():
    assert _parse_json_block("全くJSONではない") == {}
    assert _parse_json_block("") == {}


def test_to_translation_maps_keys():
    data = {
        "原文補正": "いとあはれなり",
        "現代語訳": "たいそうしみじみとしている",
        "語注": [{"語": "あはれ", "意味": "しみじみとした感慨"}],
        "時代": "平安",
    }
    tr = _to_translation(data, "src", "gemini", raw="")
    assert tr.modern == "たいそうしみじみとしている"
    assert tr.corrected == "いとあはれなり"
    assert tr.era == "平安"
    assert tr.glosses[0].term == "あはれ"
    assert tr.confidence > 0.5


def test_to_translation_accepts_english_keys():
    tr = _to_translation({"modern": "はい", "era": "江戸"}, "s", "openai", "")
    assert tr.modern == "はい" and tr.era == "江戸"


def test_to_translation_handles_prose_answer():
    tr = _to_translation({}, "s", "openai", raw="これは訳です。")
    assert tr.modern == "これは訳です。"
    assert tr.notes


def test_offline_translate_classical():
    tr = asyncio.run(OfflineTranslator().translate("いとあはれなりけり"))
    assert tr.backend == "offline"
    assert "たいそう" in tr.modern
    assert tr.confidence > 0


def test_offline_translate_empty_input():
    tr = asyncio.run(OfflineTranslator().translate("   "))
    assert tr.modern == ""


def test_offline_glosses_are_reported():
    tr = asyncio.run(OfflineTranslator().translate("いとをかしきことなり"))
    terms = [g.term for g in tr.glosses]
    assert "いと" in terms


def test_offline_modern_input_is_passed_through():
    text = "これは現代の文章です。"
    tr = asyncio.run(OfflineTranslator().translate(text))
    assert tr.modern == text
    assert "現代語" in tr.notes


def test_offline_is_honest_about_confidence():
    tr = asyncio.run(OfflineTranslator().translate("いとあはれなりけり"))
    assert tr.confidence <= 0.75
    assert "精度は保証されません" in tr.notes


@pytest.mark.parametrize(
    "source,expected",
    [
        ("いとあはれなりけり。", "たいそうしみじみとしていた。"),
        ("世の中にある人、いみじきものなり。", "世の中にある人、この上ないものだ。"),
        ("をかしきことなり。", "趣があることだ。"),
        ("あはれなりける人かな。", "しみじみとしていた人かな。"),
    ],
)
def test_offline_produces_grammatical_output(source, expected):
    tr = asyncio.run(OfflineTranslator().translate(source))
    assert tr.modern == expected


def test_offline_does_not_mangle_words_containing_auxiliary_kana():
    """回帰テスト: 「じ」を打消推量として語中で置換すると「しみじみ」が壊れる。"""
    tr = asyncio.run(OfflineTranslator().translate("いとあはれなりけり。"))
    assert "しみじみ" in tr.modern
    assert "まいみ" not in tr.modern


def test_offline_auxiliary_applies_at_clause_end_only():
    tr = asyncio.run(OfflineTranslator().translate("花咲きぬれども、人は来ず。"))
    assert tr.modern.endswith("来ない。")


def test_offline_unknown_classical_terms_pass_through_unchanged():
    """未知語は創作せずそのまま残す（誤訳より原文の方がまし）。"""
    text = "ものぐるほしきまで覚ゆ。"
    tr = asyncio.run(OfflineTranslator().translate(text))
    assert "ものぐるほしき" in tr.modern


def test_fallback_uses_offline_on_error():
    class Boom:
        name = "boom"

        async def translate(self, text):
            raise RuntimeError("backend down")

    wrapped = translate.FallbackTranslator(Boom(), OfflineTranslator())
    tr = asyncio.run(wrapped.translate("いとあはれなり"))
    assert tr.backend == "offline"
    assert "backend down" in tr.notes


def test_fallback_keeps_primary_result():
    class Good:
        name = "good"

        async def translate(self, text):
            return Translation(modern="良い訳", backend="good")

    wrapped = translate.FallbackTranslator(Good(), OfflineTranslator())
    tr = asyncio.run(wrapped.translate("なんでも"))
    assert tr.modern == "良い訳"


def test_fallback_on_empty_primary_result():
    class Silly:
        name = "silly"

        async def translate(self, text):
            return Translation(modern="", backend="silly")

    wrapped = translate.FallbackTranslator(Silly(), OfflineTranslator())
    tr = asyncio.run(wrapped.translate("いとあはれなり"))
    assert tr.modern
    assert tr.backend == "offline"


def test_build_translator_defaults_to_offline():
    class Cfg:
        def resolved_provider(self):
            return "offline"

    assert build_translator_offline(Cfg())


def build_translator_offline(cfg):
    tr = translate.build_translator(cfg)
    return isinstance(tr, OfflineTranslator)


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_build_translator_wraps_llm_in_fallback(provider):
    class Cfg:
        openai_base_url = "http://x/v1"
        openai_api_key = "k"
        openai_model = "m"
        gemini_api_key = "k"
        gemini_model = "m"

        def resolved_provider(self):
            return provider

    tr = translate.build_translator(Cfg())
    assert isinstance(tr, translate.FallbackTranslator)
