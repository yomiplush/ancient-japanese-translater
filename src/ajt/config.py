"""Runtime configuration. Everything is env-driven so the same tree runs on a
laptop and on a headless box."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def state_dir() -> Path:
    """Resolve the state directory lazily so ``AJT_STATE_DIR`` set after import
    (tests, ``--env-file`` wrappers, docker entrypoints) still takes effect."""
    directory = Path(os.environ.get("AJT_STATE_DIR") or Path.cwd() / ".ajt")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader (no dependency). Existing env always wins."""
    path = path or Path.cwd() / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def get_token() -> str:
    """Pairing token shared with the phone. Persisted so restarts keep working."""
    token = _env("AJT_TOKEN")
    if token:
        return token
    path = state_dir() / "token"
    if path.is_file():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(16)
    path.write_text(token, encoding="utf-8")
    path.chmod(0o600)
    return token


@dataclass(slots=True)
class Config:
    host: str = field(default_factory=lambda: _env("AJT_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("AJT_PORT", 8765))
    token: str = field(default_factory=get_token)

    # OCR
    ocr_threads: int = field(default_factory=lambda: _env_int("AJT_OCR_THREADS", 8))
    ocr_max_side: int = field(default_factory=lambda: _env_int("AJT_OCR_MAX_SIDE", 1280))
    min_score: float = field(default_factory=lambda: _env_float("AJT_MIN_SCORE", 0.45))

    # Translation
    provider: str = field(default_factory=lambda: _env("AJT_PROVIDER", "auto").lower())
    openai_base_url: str = field(
        default_factory=lambda: _env("AJT_OPENAI_BASE_URL", "https://api.openai.com/v1")
    )
    openai_api_key: str = field(default_factory=lambda: _env("AJT_OPENAI_API_KEY"))
    openai_model: str = field(default_factory=lambda: _env("AJT_OPENAI_MODEL", "gpt-4o-mini"))
    gemini_api_key: str = field(default_factory=lambda: _env("AJT_GEMINI_API_KEY"))
    gemini_model: str = field(default_factory=lambda: _env("AJT_GEMINI_MODEL", "gemini-2.0-flash"))

    # Optional server-side capture (MJPEG IP camera app, or a file for demos)
    source_url: str = field(default_factory=lambda: _env("AJT_SOURCE_URL"))

    # Frame gating: skip OCR when the scene barely changes.
    gate_distance: int = field(default_factory=lambda: _env_int("AJT_GATE_DISTANCE", 5))
    cache_size: int = 256

    @property
    def has_openai(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def has_gemini(self) -> bool:
        return bool(self.gemini_api_key)

    def resolved_provider(self) -> str:
        """Which translation backend to actually use."""
        if self.provider in ("offline", "rule"):
            return "offline"
        if self.provider == "gemini":
            return "gemini" if self.has_gemini else "offline"
        if self.provider in ("openai", "llm"):
            return "openai" if self.has_openai else "offline"
        # auto
        if self.has_gemini:
            return "gemini"
        if self.has_openai:
            return "openai"
        return "offline"
