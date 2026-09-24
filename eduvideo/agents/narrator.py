"""Narrator agent (TTS) + Audio QA agent.

Two engines:
  * ElevenLabs (default when ELEVENLABS_API_KEY is set): the WHOLE lesson is narrated in one
    expressive take (eleven_v3), so intonation flows from sentence to sentence and scene to
    scene like a teacher talking, instead of every sentence starting fresh. The take is cut
    into scenes in the middle of the natural pauses between them, and word timings come from
    ElevenLabs' character-level alignment.
  * edge-tts: per-scene Microsoft neural voices with word-boundary events (free fallback);
    OpenRouter gpt-audio-mini if that service is down.

Audio QA per scene:
  1. deterministic – non-empty audio, timestamp coverage, sane speaking rate, and the timestamp
     offset against the audible onsets (calibration; both engines' timestamps run early);
  2. ASR round-trip – an audio-capable LLM transcribes the clip and the transcript is compared
     with the script; low similarity = mispronounced/skipped/hallucinated words.
Failed takes are re-synthesised (ElevenLabs: the whole lesson; third attempt: alternate voice).
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import edge_tts
import httpx
import numpy as np

from ..audio import SR, decode, speech_bounds, write_wav
from ..llm import LLMError, audio_part
from ..schemas import SceneAudio, SceneScript, Transcript, Word
from ..textutil import similarity, text_hash
from .base import Ctx
from .sync import align_words, estimate_ts_offset, estimate_words

ASR_THRESHOLD = 0.85


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
    # both engines' timestamps lead the audible speech; measure it against the waveform
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
                  + f"offset {ts_off * 1000:+.0f} ms (MAD {jitter * 1000:.0f} ms over {len(anchors)} pause anchors)"
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


# ------------------------------------------------------------------ ElevenLabs
SCENE_BREAK = "\n\n"   # paragraph break between scenes → a natural, slightly longer pause


def _elevenlabs(ctx: Ctx, text: str, voice: str, model: str, speed: float) -> tuple[bytes, dict]:
    body = {"text": text, "model_id": model, "language_code": ctx.brief.lang.code,
            "voice_settings": {"speed": round(min(max(speed, 0.7), 1.2), 2)}}
    delay = 3.0
    for attempt in range(1, 5):
        try:
            r = httpx.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/with-timestamps",
                           params={"output_format": "mp3_44100_128"}, json=body, timeout=240,
                           headers={"xi-api-key": ctx.settings.elevenlabs_key})
            if r.status_code in (429, 500, 502, 503, 504):
                raise LLMError(f"ElevenLabs HTTP {r.status_code}")
            if r.status_code >= 400:
                raise LLMError(f"ElevenLabs HTTP {r.status_code}: {r.text[:200]}")
            j = r.json()
            return base64.b64decode(j["audio_base64"]), j["alignment"]
        except (httpx.TransportError, httpx.TimeoutException, LLMError) as e:
            if attempt == 4 or (isinstance(e, LLMError) and "HTTP 4" in str(e) and "429" not in str(e)):
                raise LLMError(str(e)) from e
            ctx.trace.log("narrator", "retry", f"ElevenLabs: {e}; retrying in {delay:.0f}s")
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")


def slice_lesson(alignment: dict, scene_texts: list[str], total: float) -> tuple[list[tuple[float, float]], list[list[Word]]]:
    """Split a whole-lesson take into scenes.

    Returns, per scene, the (start, end) of its audio slice (cut in the middle of the pause
    between scenes) and its word timings relative to that slice. Words are rebuilt from the
    character alignment by splitting on whitespace, so they line up 1:1 with `text.split()`.
    """
    chars = alignment["characters"]
    starts = alignment["character_start_times_seconds"]
    ends = alignment["character_end_times_seconds"]
    words: list[tuple[str, float, float]] = []
    cur, cs, ce = "", 0.0, 0.0
    for ch, st, en in zip(chars, starts, ends):
        if ch.isspace():
            if cur:
                words.append((cur, cs, ce))
            cur = ""
            continue
        if not cur:
            cs = st
        cur += ch
        ce = en
    if cur:
        words.append((cur, cs, ce))
    counts = [len(t.split()) for t in scene_texts]
    if sum(counts) != len(words):
        raise ValueError(f"alignment has {len(words)} words, script has {sum(counts)}")
    groups, i = [], 0
    for n in counts:
        groups.append(words[i:i + n])
        i += n
    cuts = [0.0] + [round((g[-1][2] + h[0][1]) / 2, 3) for g, h in zip(groups, groups[1:])] + [total]
    spans = list(zip(cuts, cuts[1:]))
    rel = [[Word(text=t, start=round(s0 - a, 3), end=round(e0 - a, 3)) for t, s0, e0 in g] for g, (a, _) in zip(groups, spans)]
    return spans, rel


def _speed(ctx: Ctx, rate: str) -> float:
    m = re.fullmatch(r"([+-]\d+)%", rate)
    return ctx.settings.el_speed * (1 + int(m.group(1)) / 100) if m else ctx.settings.el_speed


def narrate_lesson(ctx: Ctx, scenes: list[SceneScript], rate: str, cache: dict[int, SceneAudio]) -> dict[int, SceneAudio]:
    lang, s = ctx.brief.lang, ctx.settings
    full = SCENE_BREAK.join(sc.narration for sc in scenes)
    speed = _speed(ctx, rate)
    lesson_hash = text_hash(full, lang.el_voice, s.el_model, f"{speed:.3f}")
    if cache and all(sc.id in cache and cache[sc.id].engine == "elevenlabs" and cache[sc.id].narration_hash == lesson_hash
                     and (ctx.run_dir / cache[sc.id].path).exists() for sc in scenes):
        ctx.trace.log("narrator", "cache", "lesson narration unchanged, reusing the ElevenLabs take")
        return {sc.id: cache[sc.id] for sc in scenes}

    audio_dir = ctx.run_dir / "audio"
    audio_dir.mkdir(exist_ok=True)
    best: tuple[float, dict[int, SceneAudio], bool] | None = None
    for attempt in range(1, s.max_tts_attempts + 1):
        voice, model = (lang.el_voice, s.el_model) if attempt < 3 else (lang.el_alt_voice or lang.el_voice, s.el_alt_model)
        ctx.trace.log("narrator", "tts", f"whole lesson take {attempt}: ElevenLabs {model} voice {voice} speed {speed:.2f} "
                      f"({len(full)} chars, {len(scenes)} scenes in one take)")
        data, alignment = _elevenlabs(ctx, full, voice, model, speed)
        take_path = audio_dir / f"lesson_take{attempt}.mp3"
        take_path.write_bytes(data)
        samples = decode(take_path)
        total = len(samples) / SR
        spans, rel_words = slice_lesson(alignment, [sc.narration for sc in scenes], total)
        audios: dict[int, SceneAudio] = {}
        for sc, (a, b), boundaries in zip(scenes, spans, rel_words):
            path = audio_dir / f"scene_{sc.id}_el{attempt}.wav"
            write_wav(path, samples[int(a * SR):int(b * SR)])
            words, coverage = align_words(sc.narration, boundaries)
            audios[sc.id] = SceneAudio(scene_id=sc.id, path=str(path.relative_to(ctx.run_dir)), duration=round(b - a, 3),
                                       words=words, voice=f"elevenlabs:{voice}:{model}", rate=rate,
                                       narration_hash=lesson_hash, coverage=round(coverage, 3), engine="elevenlabs")
        ctx.trace.log("narrator", "slice", f"take {attempt}: {total:.2f}s cut into scenes at the natural pauses → "
                      + ", ".join(f"s{k}:{v.duration:.1f}s" for k, v in audios.items()))
        with ThreadPoolExecutor(max_workers=s.max_workers) as pool:
            takes = list(pool.map(lambda sc, au=audios: _qa(ctx, sc, au[sc.id]), scenes))
        ok = all(t.ok for t in takes)
        worst = min((t.audio.asr_similarity or 1.0) for t in takes)
        if best is None or (ok and not best[2]) or (ok == best[2] and worst > best[0]):
            best = (worst, audios, ok)
        if ok:
            break
        ctx.trace.log("audio_qa", "retake", f"take {attempt} failed in scene(s) "
                      f"{[t.audio.scene_id for t in takes if not t.ok]}; re-recording the whole lesson for continuity")
    assert best is not None
    if not best[2]:
        ctx.trace.log("audio_qa", "accept", f"no take passed every scene; keeping the best (worst-scene ASR {best[0]:.2f})")
    return best[1]


def narrate_all(ctx: Ctx, scenes: list[SceneScript], rate: str = "+0%",
                cache: dict[int, SceneAudio] | None = None) -> dict[int, SceneAudio]:
    cache = cache or {}
    out: dict[int, SceneAudio] | None = None
    if ctx.settings.use_elevenlabs(ctx.brief.lang):
        try:
            out = narrate_lesson(ctx, scenes, rate, cache)
        except (LLMError, ValueError, KeyError) as e:
            ctx.trace.log("narrator", "fallback", f"ElevenLabs unavailable ({e}); falling back to edge-tts per scene")
    if out is None:
        with ThreadPoolExecutor(max_workers=ctx.settings.max_workers) as pool:
            results = list(pool.map(lambda sc: narrate_scene(ctx, sc, rate, cache.get(sc.id)), scenes))
        out = {a.scene_id: a for a in results}
    (ctx.run_dir / "audio" / "audio.json").write_text(
        json.dumps({k: v.model_dump() for k, v in out.items()}, ensure_ascii=False, indent=1))
    return out
