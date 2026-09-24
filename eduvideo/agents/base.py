from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import Language, Settings
from ..llm import OpenRouter
from ..trace import Trace


@dataclass
class Brief:
    grade: str
    subject: str
    topic: str
    lang: Language

    def describe(self) -> str:
        return f"Class {self.grade} → {self.subject} → {self.topic} (narration language: {self.lang.name})"


@dataclass
class Ctx:
    """Shared handles every agent receives from the orchestrator."""
    settings: Settings
    llm: OpenRouter
    trace: Trace
    run_dir: Path
    brief: Brief
