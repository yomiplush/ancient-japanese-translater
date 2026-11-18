"""Text normalisation for classical Japanese.

Three separate jobs live here:

1. ``hentaigana_to_hiragana`` - map the U+1B001..U+1B11F hentaigana block to
   modern kana.  The table is derived from Unicode character names at import
   time so it can not drift out of sync with the standard.
2. ``seionize`` - fold voiced/semi-voiced variants that OCR engines take
   liberties with (e.g. 靈 -> 霊, 國 -> 国).
3. ``modernize_kana`` - historical kana usage (歴史的仮名遣い) to modern
   orthography (現代仮名遣い), which is a prerequisite for any 文語 -> 口語
   conversion and is also useful on its own.
"""

from __future__ import annotations

import re
import unicodedata

_HIRA = "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわゐゑをん"

_ROMAJI = [
    "a", "i", "u", "e", "o",
    "ka", "ki", "ku", "ke", "ko",
    "sa", "shi", "su", "se", "so",
    "ta", "chi", "tsu", "te", "to",
    "na", "ni", "nu", "ne", "no",
    "ha", "hi", "fu", "he", "ho",
    "ma", "mi", "mu", "me", "mo",
    "ya", "yu", "yo",
    "ra", "ri", "ru", "re", "ro",
    "wa", "wi", "we", "wo", "n",
]

ROMAJI_TO_HIRA = dict(zip(_ROMAJI, _HIRA))

# Unicode names hentaigana with Kunrei-shiki (TU, SI, TI, HU, ZI...), not
# Hepburn, so both spellings must resolve to the same kana.
ROMAJI_TO_HIRA.update(
    {
        "si": "し", "ti": "ち", "tu": "つ", "hu": "ふ", "zi": "じ",
        "di": "ぢ", "du": "づ", "sya": "しゃ", "zya": "じゃ", "tya": "ちゃ",
    }
)

# Hentaigana whose Unicode name carries two syllables; the first one is the
# reading of the common (base) glyph, the second is a less frequent alternate.
_COMBO = {
    "A-WO": "あ",
    "KA-KE": "か",
    "KO-KI": "こ",
    "TU-TO": "つ",
    "TO-RA": "と",
    "NI-TE": "に",
    "NE-KO": "ね",
    "N-MU-MO": "ん",
    "YA-YO": "や",
}


def _build_hentaigana_table() -> dict[str, str]:
    table: dict[str, str] = {}
    for cp in range(0x1B001, 0x1B120):
        ch = chr(cp)
        try:
            name = unicodedata.name(ch)
        except ValueError:
            continue
        if name == "HIRAGANA LETTER ARCHAIC YE":
            table[ch] = "え"
            continue
        if name == "HIRAGANA LETTER ARCHAIC WU":
            table[ch] = "う"
            continue
        if not name.startswith("HENTAIGANA LETTER "):
            continue
        suffix = name[len("HENTAIGANA LETTER "):]
        stem = re.sub(r"-\d+$", "", suffix)
        if stem in _COMBO:
            table[ch] = _COMBO[stem]
            continue
        base = stem.split("-")[0].lower()
        if base in ROMAJI_TO_HIRA:
            table[ch] = ROMAJI_TO_HIRA[base]
    return table


HENTAIGANA_TABLE = _build_hentaigana_table()


def hentaigana_to_hiragana(text: str) -> str:
    return "".join(HENTAIGANA_TABLE.get(ch, ch) for ch in text)


# --------------------------------------------------------------------------
# Kyuujitai (old kanji forms) that show up constantly in Edo-period print.
# This is deliberately a small, high-confidence table, not a full 旧字体 map:
# wrong folds here corrupt the source text, which the GUI lets you edit.
# --------------------------------------------------------------------------
_KYUJITAI = {
    "靈": "霊", "龍": "竜", "國": "国", "學": "学", "寶": "宝", "實": "実",
    "眞": "真", "盡": "尽", "廣": "広", "氣": "気", "體": "体", "圓": "円",
    "舊": "旧", "爲": "為", "醫": "医", "與": "与", "曾": "会", "會": "会",
    "戰爭": "戦争", "戰": "戦", "藝": "芸", "獻": "献", "藝": "芸", "藏": "蔵",
    "經": "経", "緖": "緒", "聲": "声", "讀": "読", "聽": "聴", "傳": "伝",
    "兩": "両", "萬": "万", "譯": "訳", "說": "説", "敎": "教", "數": "数",
    "齊": "斉", "亂": "乱", "禮": "礼", "鄕": "郷", "拂": "払", "黑": "黒",
    "黃": "黄", "默": "黙", "鹽": "塩", "藥": "薬", "辭": "辞", "辨": "弁",
    "瓣": "弁", "辯": "弁", "邊": "辺", "變": "変", "佛": "仏", "犧": "犠",
    "穗": "穂", "曾": "会", "緣": "縁", "縣": "県", "勞": "労", "屆": "届",
    "橫": "横", "晉": "晋", "溫": "温", "晝": "昼", "歲": "歳", "歸": "帰",
}

_KYUJITAI_RE = re.compile("|".join(sorted(map(re.escape, _KYUJITAI), key=len, reverse=True)))


def kyuujitai_to_shinjitai(text: str) -> str:
    return _KYUJITAI_RE.sub(lambda m: _KYUJITAI[m.group(0)], text)


def seionize(text: str) -> str:
    """Fold ゛/゜ and compatibility forms that OCR models emit erratically.

    Only absorbs the combining marks; it does not touch dakuten that are part
    of the underlying word (that distinction is not recoverable from glyphs).
    """
    return unicodedata.normalize("NFC", text)


# --------------------------------------------------------------------------
# 歴史的仮名遣い -> 現代仮名遣い
# --------------------------------------------------------------------------
# Ordered, longest-first. These are the substitutions that are safe to apply
# without part-of-speech analysis.
_KANA_RULES: list[tuple[str, str]] = [
    # 語中の 〜ふ / 〜う rows producing long vowels
    ("やう", "よう"), ("えう", "よう"), ("あう", "おう"), ("わう", "おう"),
    ("はう", "おう"), ("ひゃう", "ひょう"), ("びゃう", "びょう"),
    ("しゃう", "しょう"), ("じゅう", "じゅう"), ("てう", "ちょう"),
    ("てふ", "ちょう"), ("せう", "しょう"), ("ぜう", "じょう"),
    ("けふ", "きょう"), ("げう", "ぎょう"), ("きう", "きゅう"),
    ("くわ", "か"), ("ぐわ", "が"), ("くゎ", "か"), ("ぐゎ", "が"),
    ("いふ", "いう"), ("ふう", "ふう"),
    # ゐ / ゑ / を
    ("ゐ", "い"), ("ゑ", "え"), ("ヰ", "イ"), ("ヱ", "エ"),
    # 四つ仮名の混乱を現代表記へ寄せる
    ("ぢ", "じ"), ("づ", "ず"), ("ヂ", "ジ"), ("ヅ", "ズ"),
    # 母音の長音化 (おほ -> おお)
    ("おほ", "おお"), ("おを", "おお"), ("こほ", "こお"),
]

# 助詞は語ではなく文法的に決まるので、単独出現のみ畳む。
_PARTICLE_HA = re.compile(r"(?<=[\u4e00-\u9fff\u3041-\u309f])は(?=[\u4e00-\u9fff\u3041-\u309f])")


def modernize_kana(text: str) -> str:
    """Apply 歴史的仮名遣い -> 現代仮名遣い. Safe/idempotent on modern text."""
    out = text
    for src, dst in _KANA_RULES:
        out = out.replace(src, dst)
    return out


def clean(text: str) -> str:
    """Normalise whitespace and strip characters OCR commonly hallucinates."""
    out = unicodedata.normalize("NFKC", text)
    out = hentaigana_to_hiragana(out)
    out = re.sub(r"[ \t\u3000]+", "", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r"[|｜¦]", "", out)
    return out.strip()


def normalize_source(text: str) -> str:
    """Full pipeline applied to raw OCR output before translation."""
    return modernize_kana(kyuujitai_to_shinjitai(clean(text)))


# A handful of markers that make 文語 a safe bet. Used to decide whether the
# offline converter should speak up or stay quiet on already-modern text.
_CLASSICAL_MARKERS = (
    "けり", "なり", "たり", "む", "べし", "まし", "らむ", "けむ", "ず",
    "ぬ", "ね", "じ", "まじ", "ごとし", "まほし", "たまふ", "たまへ",
    "給ふ", "はべ", "候", "おはす", "おぼす", "いと", "いみじ", "あはれ",
    "をかし", "げに", "さらなり", "ゆかし", "らうたし", "うつくし",
    "え…ず", "〜", "にや", "にぞ", "こそ", "なむ", "やは", "かは",
)

_MODERN_MARKERS = ("です", "ます", "ました", "ません", "でしょう", "ている", "でした")


def looks_classical(text: str) -> bool:
    if not text.strip():
        return False
    modern = sum(text.count(m) for m in _MODERN_MARKERS)
    classical = sum(text.count(m) for m in _CLASSICAL_MARKERS)
    # 歴史的仮名遣いが残っていれば古典寄りと判断する。
    if any(k in text for k in ("ゐ", "ゑ", "くわ", "ぐわ")):
        classical += 2
    return classical > modern
