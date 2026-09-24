"""Thin OpenRouter client: structured JSON output, vision/audio inputs, image generation.

All calls go through `_post`, which retries transient failures (429/5xx/timeouts)
with exponential backoff and records cost + latency on the run trace.
`chat_json` adds a self-repair loop: if the model's output fails schema
validation, the validation error is fed back and the model tries again.
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .config import Settings
from .trace import Trace

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


def image_part(path_or_bytes: Path | bytes, mime: str = "image/png") -> dict[str, Any]:
    data = path_or_bytes if isinstance(path_or_bytes, bytes) else Path(path_or_bytes).read_bytes()
    if isinstance(path_or_bytes, Path) and path_or_bytes.suffix.lower() in {".jpg", ".jpeg"}:
        mime = "image/jpeg"
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}}


def audio_part(path: Path) -> dict[str, Any]:
    fmt = path.suffix.lstrip(".").lower() or "mp3"
    return {"type": "input_audio", "input_audio": {"data": base64.b64encode(path.read_bytes()).decode(), "format": fmt}}


def _extract_json(text: str) -> Any:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


class OpenRouter:
    def __init__(self, settings: Settings, trace: Trace):
        if not settings.api_key:
            raise LLMError("OPENROUTER_API_KEY is not set. Put it in .env or export it.")
        self.s = settings
        self.trace = trace
        # OpenRouter reserves worst-case cost per in-flight request; capping concurrency keeps
        # parallel agents from tripping the account's in-flight budget (HTTP 402).
        self._slots = threading.BoundedSemaphore(settings.max_concurrent_requests)
        # Image outputs are priced per output token, so their worst-case reservation is large:
        # generate one image at a time and cap max_tokens (image + model reasoning tokens).
        self._image_slot = threading.BoundedSemaphore(1)
        self.http = httpx.Client(
            base_url=settings.base_url,
            timeout=httpx.Timeout(180.0, connect=20.0),
            headers={
                "Authorization": f"Bearer {settings.api_key}",
                "HTTP-Referer": "https://github.com/eduvideo-agent",
                "X-Title": "EduVideo Agent",
            },
        )

    def can_spend(self, estimate: float) -> bool:
        return self.trace.cost_usd + estimate <= self.s.max_cost_usd

    def _post(self, payload: dict[str, Any], purpose: str, retries: int = 4) -> dict[str, Any]:
        if self.trace.cost_usd >= self.s.max_cost_usd:
            raise LLMError(f"cost budget ${self.s.max_cost_usd:.2f} reached (spent ${self.trace.cost_usd:.3f})")
        delay = 4.0
        last: Exception | None = None
        for attempt in range(1, retries + 1):
            t0 = time.time()
            try:
                with self._slots:
                    r = self.http.post("/chat/completions", json=payload)
                if r.status_code == 402 and "in_flight" in r.text:
                    raise LLMError(f"HTTP 402 in-flight budget busy: {r.text[:120]}")
                if r.status_code in (408, 429, 500, 502, 503, 504, 529):
                    raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
                if r.status_code >= 400:
                    # Non-retryable (bad request, auth, insufficient credits).
                    raise LLMError(f"HTTP {r.status_code}: {r.text[:500]}")
                data = r.json()
                if "error" in data:
                    raise LLMError(f"provider error: {json.dumps(data['error'])[:300]}")
                if not data.get("choices"):
                    raise LLMError("empty choices in response")
                self.trace.add_cost(payload["model"], (data.get("usage") or {}).get("cost"), time.time() - t0, purpose)
                return data
            except (httpx.TimeoutException, httpx.TransportError, LLMError, json.JSONDecodeError) as e:
                last = e
                fatal = isinstance(e, LLMError) and bool(re.match(r"HTTP (400|401|402|403|404)", str(e))) \
                    and "in-flight" not in str(e)
                self.trace.log("llm", "retry" if not fatal else "fail", f"{purpose}: {e}", attempt=attempt, model=payload["model"])
                if fatal or attempt == retries:
                    break
                time.sleep(delay)
                delay *= 2
        raise LLMError(f"{purpose} failed after {retries} attempts: {last}")

    # ------------------------------------------------------------------ text
    def chat(self, model: str, messages: list[dict[str, Any]], purpose: str, temperature: float = 0.7,
             max_tokens: int = 4000, **extra: Any) -> str:
        data = self._post({"model": model, "messages": messages, "temperature": temperature,
                           "max_tokens": max_tokens, **extra}, purpose)
        return data["choices"][0]["message"].get("content") or ""

    def chat_json(self, model: str, messages: list[dict[str, Any]], schema: type[T], purpose: str,
                  temperature: float = 0.5, repair_rounds: int = 2) -> T:
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": schema.__name__, "strict": False, "schema": schema.model_json_schema()},
        }
        msgs = list(messages)
        for attempt in range(repair_rounds + 1):
            text = self.chat(model, msgs, purpose, temperature=temperature, max_tokens=6000,
                             response_format=response_format)
            try:
                return schema.model_validate(_extract_json(text))
            except (ValidationError, json.JSONDecodeError, ValueError) as e:
                self.trace.log("llm", "repair", f"{purpose}: schema validation failed (attempt {attempt + 1})",
                               error=str(e)[:500])
                msgs = msgs + [
                    {"role": "assistant", "content": text[:6000]},
                    {"role": "user", "content": f"Your JSON did not validate against the schema:\n{str(e)[:1500]}\n"
                                                "Return the corrected JSON object only."},
                ]
        raise LLMError(f"{purpose}: model could not produce valid {schema.__name__} JSON")

    # ----------------------------------------------------------------- image
    def generate_image(self, prompt: str, purpose: str, aspect_ratio: str = "16:9") -> bytes:
        payload = {
            "model": self.s.image_model,
            "modalities": ["image", "text"],
            "image_config": {"aspect_ratio": aspect_ratio},
            "max_tokens": self.s.image_max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        with self._image_slot:
            data = self._post(payload, purpose, retries=3)
        msg = data["choices"][0]["message"]
        for img in msg.get("images") or []:
            url = (img.get("image_url") or {}).get("url", "")
            if url.startswith("data:"):
                return base64.b64decode(url.split(",", 1)[1])
            if url.startswith("http"):
                return self.http.get(url).content
        raise LLMError(f"{purpose}: image model returned no image (text: {(msg.get('content') or '')[:200]})")
