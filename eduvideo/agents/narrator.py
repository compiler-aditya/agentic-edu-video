"""Narrator agent (TTS) + Audio QA agent.

TTS: Microsoft neural voices via edge-tts (native Indian-language voices with
word-boundary timestamps). If that service fails, OpenRouter's gpt-audio-mini is
used as a fallback and word timings are estimated from the waveform.

Audio QA per scene:
  1. deterministic – non-empty audio, TTS word coverage, speech onset agrees with
     the first word timestamp (catches timestamp drift), sane speaking rate;
  2. ASR round-trip – an audio-capable LLM transcribes the clip and the transcript
     is compared with the script; low similarity = mispronounced/skipped words.
Failed takes are re-synthesised (second attempt same voice, then the alternate voice).
"""
from __future__ import annotations

import asyncio
import base64
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import edge_tts
import numpy as np

from ..audio import SR, decode, speech_bounds, write_wav
from ..llm import LLMError, audio_part
from ..schemas import SceneAudio, SceneScript, Transcript, Word
from ..textutil import similarity, text_hash
from .base import Ctx
from .sync import align_words, estimate_ts_offset, estimate_words

ASR_THRESHOLD = 0.80


@dataclass
class Take:
    audio: SceneAudio
    ok: bool
    problems: list[str]


async def _edge_tts(text: str, voice: str, rate: str, out_path) -> list[Word]:
    comm = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
    words: list[Word] = []
    with open(out_path, "wb") as fh:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                fh.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                s = chunk["offset"] / 1e7
                words.append(Word(text=chunk["text"], start=s, end=s + chunk["duration"] / 1e7))
    return words


def _openrouter_tts(ctx: Ctx, text: str, out_path) -> None:
    """Fallback TTS through OpenRouter (streamed PCM16 @ 24 kHz)."""
    payload = {
        "model": "openai/gpt-audio-mini", "modalities": ["text", "audio"], "stream": True,
        "audio": {"voice": "shimmer", "format": "pcm16"},
        "messages": [{"role": "system", "content": f"You are a text-to-speech engine. Read the user's {ctx.brief.lang.name} "
                                                   "text aloud exactly as written, warmly and clearly, like a school teacher. "
                                                   "Do not add or skip any words."},
                     {"role": "user", "content": text}],
    }
    pcm = bytearray()
    with ctx.llm.http.stream("POST", "/chat/completions", json=payload) as r:
        if r.status_code >= 400:
            raise LLMError(f"fallback TTS HTTP {r.status_code}")
        for line in r.iter_lines():
            if not line.startswith("data: ") or line.endswith("[DONE]"):
                continue
            delta = (json.loads(line[6:]).get("choices") or [{}])[0].get("delta", {})
            data = (delta.get("audio") or {}).get("data")
            if data:
                pcm.extend(base64.b64decode(data))
    if not pcm:
        raise LLMError("fallback TTS returned no audio")
    samples = np.frombuffer(bytes(pcm), dtype=np.int16).astype(np.float32) / 32768.0
    write_wav(out_path, samples, sr=24000)


def _synthesize(ctx: Ctx, scene: SceneScript, voice: str, rate: str, attempt: int) -> SceneAudio:
    audio_dir = ctx.run_dir / "audio"
    audio_dir.mkdir(exist_ok=True)
    path = audio_dir / f"scene_{scene.id}_take{attempt}.mp3"
    boundaries: list[Word] = []
    source = "tts"
    try:
        boundaries = asyncio.run(_edge_tts(scene.narration, voice, rate, path))
        if path.stat().st_size < 1000:
            raise RuntimeError("edge-tts returned empty audio")
    except Exception as e:  # network/service errors from edge-tts
        ctx.trace.log("narrator", "error", f"scene {scene.id}: edge-tts failed ({e}); using OpenRouter TTS fallback")
        path = path.with_suffix(".wav")
        _openrouter_tts(ctx, scene.narration, path)
        voice, source = "openai/gpt-audio-mini:shimmer", "estimated"

    samples = decode(path)
    duration = len(samples) / SR
    if boundaries:
        words, coverage = align_words(scene.narration, boundaries)
    else:
        onset, offset = speech_bounds(samples)
        words, coverage, source = estimate_words(scene.narration, onset, offset), 0.0, "estimated"
    audio = SceneAudio(scene_id=scene.id, path=str(path.relative_to(ctx.run_dir)), duration=round(duration, 3),
                       words=words, voice=voice, rate=rate, narration_hash=text_hash(scene.narration, voice, rate),
                       boundary_source=source, coverage=round(coverage, 3))
    return audio


def _qa(ctx: Ctx, scene: SceneScript, audio: SceneAudio) -> Take:
    problems: list[str] = []
    samples = decode(ctx.run_dir / audio.path)
    if audio.duration < 1.0 or np.abs(samples).max() < 0.01:
        problems.append("audio is empty or silent")
    onset, offset = speech_bounds(samples)
    coverage = audio.coverage
    ts_off, anchors = estimate_ts_offset(samples, audio.words)
    audio.ts_offset = round(ts_off, 3)
    # robust spread (median absolute deviation): a stray anchor must not fail a good take
    jitter = float(np.median([abs(d - ts_off) for d in anchors])) if anchors else 0.0
    if audio.boundary_source == "tts":
        if coverage < 0.8:
            problems.append(f"only {coverage:.0%} of narration words have TTS timestamps")
        if not -0.1 <= ts_off <= 0.35:
            problems.append(f"TTS timestamps are {ts_off:+.2f}s away from the audible speech")
        if jitter > 0.12:
            problems.append(f"TTS timestamps are inconsistent with pauses in the audio (±{jitter:.2f}s)")
        if audio.words and audio.words[-1].end > audio.duration + 0.1:
            problems.append("word timestamps run past the end of the audio")
    wps = len(audio.words) / max(offset - onset, 0.1)
    if not 1.0 <= wps <= 4.5:
        problems.append(f"implausible speaking rate {wps:.2f} words/s")

    if ctx.settings.asr_check and not problems:
        try:
            tr = ctx.llm.chat_json(
                ctx.settings.audio_model,
                [{"role": "user", "content": [
                    {"type": "text", "text": f"Transcribe this {ctx.brief.lang.name} speech verbatim in {ctx.brief.lang.script} "
                                             "script (numbers as words). Also list any words that sound mispronounced, "
                                             "garbled or cut off."},
                    audio_part(ctx.run_dir / audio.path)]}],
                Transcript, purpose=f"audio_qa.scene{scene.id}", temperature=0.0)
            audio.asr_similarity = round(similarity(scene.narration, tr.transcript), 3)
            if audio.asr_similarity < ASR_THRESHOLD:
                problems.append(f"ASR transcript similarity {audio.asr_similarity:.2f} < {ASR_THRESHOLD}")
        except LLMError as e:
            ctx.trace.log("audio_qa", "skip", f"scene {scene.id}: ASR check unavailable ({e})")

    ok = not problems
    ctx.trace.log("audio_qa", "verdict",
                  f"scene {scene.id}: {'PASS' if ok else 'FAIL'} {audio.duration:.2f}s, {wps:.2f} w/s, "
                  f"timestamps={audio.boundary_source} coverage={coverage:.0%}, "
                  f"offset {ts_off * 1000:+.0f} ms (MAD {jitter * 1000:.0f} ms over {len(anchors)} pause anchors)"
                  + (f", ASR sim={audio.asr_similarity:.2f}" if audio.asr_similarity is not None else "")
                  + (f" — {'; '.join(problems)}" if problems else ""),
                  scene=scene.id, ok=ok, asr=audio.asr_similarity)
    return Take(audio, ok, problems)


def narrate_scene(ctx: Ctx, scene: SceneScript, rate: str = "+0%", cached: SceneAudio | None = None) -> SceneAudio:
    lang = ctx.brief.lang
    if cached and cached.narration_hash == text_hash(scene.narration, cached.voice, rate) \
            and (ctx.run_dir / cached.path).exists():
        ctx.trace.log("narrator", "cache", f"scene {scene.id}: narration unchanged, reusing audio")
        return cached
    best: Take | None = None
    for attempt in range(1, ctx.settings.max_tts_attempts + 1):
        voice = lang.voice if attempt < 3 else lang.alt_voice
        ctx.trace.log("narrator", "tts", f"scene {scene.id} take {attempt}: {voice} rate {rate}")
        audio = _synthesize(ctx, scene, voice, rate, attempt)
        take = _qa(ctx, scene, audio)
        if best is None or (take.ok and not best.ok) or \
                ((take.audio.asr_similarity or 0) > (best.audio.asr_similarity or 0) and take.ok == best.ok):
            best = take
        if take.ok:
            break
    assert best is not None
    if not best.ok:
        ctx.trace.log("audio_qa", "accept", f"scene {scene.id}: no take passed; keeping best take "
                      f"({'; '.join(best.problems)})")
    return best.audio


def narrate_all(ctx: Ctx, scenes: list[SceneScript], rate: str = "+0%",
                cache: dict[int, SceneAudio] | None = None) -> dict[int, SceneAudio]:
    cache = cache or {}
    with ThreadPoolExecutor(max_workers=ctx.settings.max_workers) as pool:
        results = list(pool.map(lambda sc: narrate_scene(ctx, sc, rate, cache.get(sc.id)), scenes))
    out = {a.scene_id: a for a in results}
    (ctx.run_dir / "audio" / "audio.json").write_text(
        json.dumps({k: v.model_dump() for k, v in out.items()}, ensure_ascii=False, indent=1))
    return out
