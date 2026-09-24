"""Language-agnostic text helpers used by validators and the sync agent."""
from __future__ import annotations

import difflib
import hashlib
import re
import unicodedata

from .config import SCRIPT_RANGES

_PUNCT = re.compile(r"[\s।॥.,!?;:\"'“”‘’()\[\]{}\-–—…/\\|*_~`]+")
_NUKTA = "़"


def norm_token(tok: str) -> str:
    tok = unicodedata.normalize("NFC", tok)
    tok = _PUNCT.sub("", tok).replace(_NUKTA, "").replace("‌", "").replace("‍", "")
    return tok.lower()


def norm_text(text: str) -> str:
    return "".join(norm_token(t) for t in text.split())


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm_text(a), norm_text(b), autojunk=False).ratio()


def script_ratio(text: str, script: str) -> float:
    """Share of letters in `text` that belong to the expected writing system."""
    ranges = SCRIPT_RANGES[script]
    letters = [c for c in text if unicodedata.category(c)[0] in "LM"]
    if not letters:
        return 0.0
    ok = sum(1 for c in letters if any(lo <= ord(c) <= hi for lo, hi in ranges))
    return ok / len(letters)


def word_count(text: str) -> int:
    return sum(1 for t in text.split() if norm_token(t))


def text_hash(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode()).hexdigest()[:12]
