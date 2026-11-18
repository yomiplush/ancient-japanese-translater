"""文語 -> 口語 translation.

Two kinds of backend:

* ``LLMTranslator`` - any OpenAI-compatible endpoint or Gemini. This is the
  mode that actually produces good modern Japanese, and it is the default
  whenever a key is present.
* ``OfflineTranslator`` - a deterministic rule engine. It can not rival an LLM,
  but it must never leave the user with nothing, and it is genuinely useful for
  the common 文語 endings of Edo-period prose. It reports its own confidence so
  the GUI can be honest about what the user is looking at.

Both return the same :class:`Translation` shape.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from . import normalize

SYSTEM_PROMPT = """あなたは日本の古典籍（くずし字・変体仮名・候文を含む）の専門家です。
入力はOCRされた古文（文語体）のテキストです。OCR誤りが含まれることがあります。

次の手順で処理してください。
1. 文脈から明らかなOCR誤りを静かに補正する（推測で語を創作しない）。
2. 原文を文語として解釈する。
3. 現代の口語（話し言葉に近い自然な現代日本語）に訳す。

出力は必ず次のJSONのみ。前後に説明やコードフェンスを付けないこと。
{"原文補正": "...", "現代語訳": "...", "語注": [{"語": "...", "意味": "..."}], "時代": "..."}

「原文補正」は補正後の原文、「現代語訳」は口語訳、「語注」は重要語の短い語釈（最大6件）、
「時代」は推定される成立時代（例: 江戸後期）。原文が既に現代語なら現代語訳はそのまま返す。"""

USER_TEMPLATE = "次の古文を現代口語に訳してください。\n\n---\n{text}\n---"


@dataclass(slots=True)
class Gloss:
    term: str
    meaning: str

    def as_dict(self) -> dict:
        return {"term": self.term, "meaning": self.meaning}


@dataclass(slots=True)
class Translation:
    modern: str
    source: str = ""
    corrected: str = ""
    glosses: list[Gloss] = field(default_factory=list)
    era: str = ""
    backend: str = "offline"
    confidence: float = 0.0
    notes: str = ""

    def as_dict(self) -> dict:
        return {
            "modern": self.modern,
            "source": self.source,
            "corrected": self.corrected,
            "glosses": [g.as_dict() for g in self.glosses],
            "era": self.era,
            "backend": self.backend,
            "confidence": round(self.confidence, 2),
            "notes": self.notes,
        }


class Translator(Protocol):
    name: str

    async def translate(self, text: str) -> Translation: ...


# --------------------------------------------------------------------------
# Offline rule engine
# --------------------------------------------------------------------------
# 文語の語彙は語中にも現れるので、長さ2以上の語だけを文中で置換する。
# (pattern, replacement, note)
_VOCAB_RULES: list[tuple[str, str, str]] = [
    ("あはれなり", "しみじみとしている", "形容動詞"),
    ("いみじかり", "この上なく", "形容詞"),
    ("をかしかり", "趣があった", "形容詞"),
    ("侍り", "あります", "丁寧の補助動詞"),
    ("はべり", "ございます", "丁寧の補助動詞"),
    ("候", "ございます", "候文の丁寧語"),
    ("たまはり", "いただき", "謙譲"),
    ("たまふ", "なさる", "尊敬"),
    ("たまへ", "ください", "尊敬"),
    ("給ふ", "なさる", "尊敬"),
    ("おはします", "いらっしゃる", "尊敬"),
    ("おはす", "いらっしゃる", "尊敬"),
    ("申し上げ", "申し上げ", "謙譲"),
    ("聞こゆ", "申し上げる", "謙譲"),
    ("おぼす", "お思いになる", "尊敬"),
    ("思ほゆ", "自然と思われる", "自発"),
    ("げに", "なるほど", "副詞"),
    ("されども", "しかし", "接続"),
    ("しかれども", "しかし", "接続"),
    ("しかるに", "ところが", "接続"),
    ("つらつら", "よくよく", "副詞"),
    ("やうやう", "しだいに", "副詞"),
    ("やがて", "そのまま", "副詞"),
    ("ただちに", "すぐに", "副詞"),
    ("さらに", "まったく", "副詞"),
    ("いはんや", "ましてや", "反語"),
    ("などか", "どうして", "反語"),
    ("いかで", "どうして", "反語"),
    ("なんぞ", "どうして", "反語"),
    ("いと", "たいそう", "副詞"),
    ("ごとし", "ようだ", "比況"),
    ("ごとく", "ように", "比況"),
    ("ゆゑ", "理由", "名詞"),
    ("ためし", "例", "名詞"),
]

# 助動詞は原則として文節末に付く。文末からのみ剥がすことで、
# 「しみじみ」の「じ」を打消推量と誤認するような事故を防ぐ。
_AUX_RULES: list[tuple[str, str, str]] = [
    ("でありけり", "であった", "過去"),
    ("にけり", "になった", "過去・詠嘆"),
    ("なりけり", "であった", "断定+過去"),
    ("なりき", "であった", "断定+過去"),
    ("けり", "た", "過去"),
    ("べからず", "してはいけない", "禁止"),
    ("べし", "はずだ", "推量・当然"),
    ("まじ", "はずがない", "打消推量"),
    ("らむ", "ているだろう", "現在推量"),
    ("けむ", "ただろう", "過去推量"),
    ("まし", "ただろうに", "反実仮想"),
    ("ざり", "ない", "打消"),
    ("たり", "ている", "完了・存続"),
    ("なり", "だ", "断定"),
    ("む", "だろう", "推量"),
    ("るる", "れる", "受身"),
    ("らる", "れる", "受身・尊敬"),
    ("さす", "させる", "使役"),
    ("しむ", "させる", "使役"),
    ("き", "た", "過去"),
    ("ず", "ない", "打消"),
    ("ぬ", "ない", "打消（連体）"),
    ("じ", "まい", "打消意志"),
]

_AUX_RULES.sort(key=lambda r: -len(r[0]))
_VOCAB_RULES.sort(key=lambda r: -len(r[0]))

# 句読点で文節に割る。助動詞の処理はこの単位で行う。
_CLAUSE_SPLIT = re.compile(r"([。、！？!?\n])")

# 反復記号などを現代表記へ寄せる。
_PUNCT = (("ゝ", "々"), ("／", "/"))

# 形容詞は語幹だけ置換すると「をかしき」→「趣があい」のように壊れる。
# 文語形容詞の活用形は現代語の形と1対1で対応しないため、頻出する活用形を
# そのまま対訳として持つ。形態素解析器を入れるまでの割り切り。
_ADJ_FORMS: list[tuple[str, str]] = [
    ("あはれなりける", "しみじみとしていた"),
    ("あはれなりけり", "しみじみとしていた"),
    ("あはれなり", "しみじみとしている"),
    ("あはれなる", "しみじみとしている"),
    ("あはれに", "しみじみと"),
    ("いみじき", "この上ない"),
    ("いみじく", "この上なく"),
    ("いみじう", "この上なく"),
    ("いみじ", "この上ない"),
    ("をかしき", "趣がある"),
    ("をかしけれ", "趣があるが"),
    ("をかしく", "趣があって"),
    ("をかし", "趣がある"),
    ("ゆかしき", "見たい"),
    ("ゆかしく", "見たく"),
    ("ゆかし", "見たいものだ"),
    ("らうたき", "かわいらしい"),
    ("らうたく", "かわいらしく"),
    ("うつくしき", "かわいい"),
    ("うつくし", "かわいい"),
    ("かしこき", "すばらしい"),
    ("かしこく", "すばらしく"),
    ("かしこし", "すばらしい"),
    ("はかなき", "頼りない"),
    ("はかなく", "頼りなく"),
    ("はかなし", "頼りない"),
    ("さらなり", "言うまでもない"),
    ("心もとなき", "気がかりだ"),
    ("心もとなし", "気がかりだ"),
    ("こころもとなし", "気がかりだ"),
    ("こころもとなき", "気がかりだ"),
]

# 形容詞の文語活用語尾 -> 口語語尾（長い順）。上の表で拾えなかった
# 残りの活用語尾を落とすための最終処理。
_ADJ_ENDINGS: list[tuple[str, str]] = [
    ("からず", "くない"),
    ("かりけり", "かった"),
    ("かりき", "かった"),
    ("かるべし", "いはずだ"),
    ("ければ", "ければ"),
    ("けれども", "けれども"),
    ("かり", "かった"),
    ("かる", "い"),
    ("けれ", "ければ"),
]

_ADJ_FORMS.sort(key=lambda r: -len(r[0]))
_ADJ_ENDINGS.sort(key=lambda r: -len(r[0]))


class OfflineTranslator:
    """Deterministic 文語 -> 口語 conversion with explicit confidence."""

    name = "offline"

    def __init__(self) -> None:
        merged = [(p, r, n) for p, r, n in _VOCAB_RULES]
        merged += [(p, r, n) for p, r, n in _AUX_RULES if len(p) >= 2]
        self._gloss_map = {p: (r, n) for p, r, n in merged}

    async def translate(self, text: str) -> Translation:
        source = normalize.clean(text)
        prepared = normalize.kyuujitai_to_shinjitai(source)
        if not prepared.strip():
            return Translation(modern="", source=source, backend=self.name, notes="テキストがありません")

        if not normalize.looks_classical(prepared):
            modern = normalize.modernize_kana(prepared)
            return Translation(
                modern=modern,
                source=source,
                corrected=prepared,
                backend=self.name,
                confidence=0.3,
                notes="現代語に近いため表記の正規化のみ行いました",
            )

        converted, hits = self._convert(prepared)
        confidence = min(0.75, 0.18 + 0.07 * hits)
        return Translation(
            modern=converted,
            source=source,
            corrected=prepared,
            glosses=self._glosses(prepared),
            backend=self.name,
            confidence=confidence,
            notes=(
                "ルールベースの近似訳です。精度は保証されません。"
                "LLMバックエンド（APIキー設定）で大幅に改善します。"
            ),
        )

    def _convert(self, text: str) -> tuple[str, int]:
        hits = 0
        # 形容詞を先に処理する。語彙規則を先に当てると「いみじ」が
        # 「この上なく」に置換され、あとに残る活用語尾「き」が浮いてしまう。
        text, adj_hits = self._convert_adjectives(text)
        hits += adj_hits

        # 語彙を次に置換する。現代仮名遣い化を先にやると「やうやう」が
        # 「ようよう」になり、語彙規則が当たらなくなる。
        for pattern, replacement, _note in _VOCAB_RULES:
            if pattern in text:
                text = text.replace(pattern, replacement)
                hits += 1

        clauses = _CLAUSE_SPLIT.split(text)
        out: list[str] = []
        for part in clauses:
            if _CLAUSE_SPLIT.fullmatch(part or ""):
                out.append(part)
                continue
            converted, n = self._convert_clause(part)
            out.append(converted)
            hits += n
        text = "".join(out)

        text = normalize.modernize_kana(text)
        for src, dst in _PUNCT:
            text = text.replace(src, dst)
        return _light_cleanup(text), hits

    def _convert_adjectives(self, text: str) -> tuple[str, int]:
        """Replace whole 文語 adjective forms with their 口語 equivalents."""
        hits = 0
        for form, modern in _ADJ_FORMS:
            if form in text:
                text = text.replace(form, modern)
                hits += 1
        return text, hits

    def _convert_clause(self, clause: str) -> tuple[str, int]:
        """Strip 文語 auxiliaries from the end of a clause, longest match first.

        Suffix anchoring is what makes the single-kana rules (じ, き, む) safe:
        they only ever apply where an auxiliary can actually sit.
        """
        if not clause.strip():
            return clause, 0
        text = clause
        hits = 0
        applied: set[str] = set()
        for _ in range(6):  # bounded: each pass removes at least one suffix
            matched = False
            for pattern, replacement, _note in _AUX_RULES:
                if len(text) <= len(pattern):
                    continue  # never consume the whole clause
                if pattern in applied:
                    continue
                if text.endswith(pattern):
                    text = text[: -len(pattern)] + replacement
                    applied.add(pattern)
                    hits += 1
                    matched = True
                    break
            if not matched:
                break
        return text, hits

    def _glosses(self, text: str) -> list[Gloss]:
        found: list[Gloss] = []
        seen: set[str] = set()
        for pattern, (replacement, note) in sorted(self._gloss_map.items(), key=lambda kv: -len(kv[0])):
            if pattern in text and pattern not in seen:
                seen.add(pattern)
                found.append(Gloss(term=pattern, meaning=f"{replacement}（{note}）"))
                if len(found) >= 6:
                    break
        return found


def _light_cleanup(text: str) -> str:
    out = text
    # 助動詞の置換で生まれる不自然な重複を畳む。
    for bad, good in (
        ("だた", "だった"),
        ("ないた", "なかった"),
        ("ているた", "ていた"),
        ("たた", "た"),
        ("るるる", "る"),
        ("，，", "、"),
        ("。。", "。"),
    ):
        out = out.replace(bad, good)
    out = re.sub(r"([、。])\1+", r"\1", out)
    out = out.strip()
    return out


# --------------------------------------------------------------------------
# LLM backends
# --------------------------------------------------------------------------
def _parse_json_block(raw: str) -> dict:
    """LLMs fence JSON, prefix it, or add trailing prose. Dig the object out."""
    if not raw:
        return {}
    candidate = raw.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    start, end = candidate.find("{"), candidate.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            return {}
    return {}


def _to_translation(data: dict, source: str, backend: str, raw: str) -> Translation:
    if not data:
        # The model answered in prose instead of JSON - still useful.
        return Translation(
            modern=raw.strip(),
            source=source,
            backend=backend,
            confidence=0.5,
            notes="LLMの応答をJSONとして解析できませんでした",
        )
    modern = str(data.get("現代語訳") or data.get("modern") or "").strip()
    corrected = str(data.get("原文補正") or data.get("corrected") or "").strip()
    era = str(data.get("時代") or data.get("era") or "").strip()
    glosses = []
    for item in data.get("語注") or data.get("glosses") or []:
        if isinstance(item, dict):
            term = str(item.get("語") or item.get("term") or "").strip()
            meaning = str(item.get("意味") or item.get("meaning") or "").strip()
            if term and meaning:
                glosses.append(Gloss(term=term, meaning=meaning))
        elif isinstance(item, str) and ":" in item:
            term, _, meaning = item.partition(":")
            glosses.append(Gloss(term=term.strip(), meaning=meaning.strip()))
    return Translation(
        modern=modern,
        source=source,
        corrected=corrected,
        era=era,
        glosses=glosses[:8],
        backend=backend,
        confidence=0.9 if modern else 0.2,
    )


class LLMTranslator:
    """OpenAI-compatible chat completion backend."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.name = "openai"

    async def translate(self, text: str) -> Translation:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": USER_TEMPLATE.format(text=normalize.clean(text))},
            ],
            "temperature": 0.2,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions", json=payload, headers=headers
            )
            resp.raise_for_status()
            body = resp.json()
        raw = body["choices"][0]["message"]["content"]
        return _to_translation(_parse_json_block(raw), text, self.name, raw)


class GeminiTranslator:
    name = "gemini"

    def __init__(self, api_key: str, model: str = "gemini-2.0-flash", timeout: float = 60.0):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    async def translate(self, text: str) -> Translation:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent"
        )
        payload = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [
                {"parts": [{"text": USER_TEMPLATE.format(text=normalize.clean(text))}]}
            ],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            body = resp.json()
        try:
            raw = body["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            raise RuntimeError(f"Gemini 応答を解釈できません: {str(body)[:200]}")
        return _to_translation(_parse_json_block(raw), text, self.name, raw)


class FallbackTranslator:
    """Wraps a primary backend; on any error, drop to the offline engine.

    A live camera app must never show a stack trace instead of a translation.
    """

    def __init__(self, primary: Translator, fallback: Translator):
        self.primary = primary
        self.fallback = fallback
        self.name = getattr(primary, "name", "llm")

    async def translate(self, text: str) -> Translation:
        try:
            result = await self.primary.translate(text)
            if result.modern.strip():
                return result
            raise RuntimeError("empty translation")
        except Exception as exc:  # noqa: BLE001 - any backend failure falls back
            offline = await self.fallback.translate(text)
            offline.notes = f"{type(exc).__name__}: {exc} → オフライン訳に切り替えました"
            return offline


def build_translator(cfg) -> Translator:
    offline = OfflineTranslator()
    provider = cfg.resolved_provider()
    if provider == "openai":
        primary = LLMTranslator(
            base_url=cfg.openai_base_url, api_key=cfg.openai_api_key, model=cfg.openai_model
        )
    elif provider == "gemini":
        primary = GeminiTranslator(api_key=cfg.gemini_api_key, model=cfg.gemini_model)
    else:
        return offline
    return FallbackTranslator(primary, offline)
