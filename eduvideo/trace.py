"""Structured run trace: every agent decision, retry, verdict and API cost is recorded.

The trace is written to `run_log.jsonl` as it happens and summarised into
`report.md` at the end, so a reviewer can see exactly why each component was
accepted or regenerated.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_COLORS = {
    "orchestrator": "\033[1;37m",
    "planner": "\033[36m",
    "writer": "\033[34m",
    "script_reviewer": "\033[35m",
    "visual": "\033[32m",
    "visual_critic": "\033[92m",
    "narrator": "\033[33m",
    "audio_qa": "\033[93m",
    "sync": "\033[96m",
    "renderer": "\033[94m",
    "final_qa": "\033[95m",
    "llm": "\033[90m",
}
_RESET = "\033[0m"


@dataclass
class Trace:
    run_dir: Path
    events: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float = 0.0
    llm_calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _t0: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.run_dir / "run_log.jsonl", "a", encoding="utf-8")

    def log(self, agent: str, event: str, message: str = "", **data: Any) -> None:
        rec = {"t": round(time.time() - self._t0, 2), "agent": agent, "event": event, "message": message, **data}
        with self._lock:
            self.events.append(rec)
            self._fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            self._fh.flush()
            if agent != "llm":
                color = _COLORS.get(agent, "")
                tty = sys.stdout.isatty()
                prefix = f"{color}[{rec['t']:6.1f}s] {agent:<15}{_RESET}" if tty else f"[{rec['t']:6.1f}s] {agent:<15}"
                print(f"{prefix} {event:<10} {message}", flush=True)

    def add_cost(self, model: str, cost: float | None, latency: float, purpose: str) -> None:
        with self._lock:
            self.llm_calls += 1
            self.cost_usd += cost or 0.0
        self.log("llm", "call", purpose, model=model, cost=cost, latency=round(latency, 2))

    def elapsed(self) -> float:
        return time.time() - self._t0

    def close(self) -> None:
        self._fh.close()
