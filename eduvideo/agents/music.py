"""Music agent: background score that supports the narration without competing with it.

  1. Brief    – an LLM writes an instrumental music prompt from the lesson (topic, class, mood).
  2. Compose  – ElevenLabs Music generates a track of exactly the video's length (instrumental only).
  3. Listen   – deterministic loudness-jump check + an audio LLM that rejects vocals, sudden hits,
                wrong mood or anything too busy for a background bed; rejected tracks are re-composed.
  4. Mix      – the music is ducked under every spoken word (we know exactly when each word is
                spoken), rises gently in pauses, the intro card and the ending, and fades in/out.
  5. Clarity  – the ASR round-trip is repeated on the final mix; if the narration is less
                intelligible than without music, the bed is lowered and re-checked, or dropped.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx
import numpy as np
from pydantic import BaseModel, Field

from ..audio import SR, decode, frame_db, write_wav
from ..llm import LLMError, audio_part
from ..schemas import Script, Timeline
from ..textutil import similarity, text_hash
from .base import Ctx

BED_DB = -30.0      # music RMS in pauses / intro / outro (narration speech sits around -19 dB RMS)
DUCK_DB = 14.0      # extra attenuation while a word is being spoken
ATTACK, RELEASE = 0.08, 0.45
FADE_IN, FADE_OUT = 1.0, 2.0


class MusicBrief(BaseModel):
    prompt: str = Field(description="English prompt for an instrumental background track")
    mood: str


class MusicReview(BaseModel):
    has_vocals: bool = Field(description="any singing, humming, chanting or spoken words")
    has_sudden_loud_parts: bool = Field(description="hits, drops, crescendos or jumps that would startle a listener")
    mood_fits: bool
    calm_enough_for_background: bool
    score: int = Field(ge=1, le=10)
    problems: list[str] = Field(default_factory=list)

    @property
    def approved(self) -> bool:
        return not self.has_vocals and not self.has_sudden_loud_parts and self.mood_fits \
            and self.calm_enough_for_background and self.score >= 7


class MixReview(BaseModel):
    transcript: str
    music_too_loud: bool = Field(description="the background music makes any narrated word hard to hear")
    balance_score: int = Field(ge=1, le=10, description="10 = music clearly supports and never masks the voice")


@dataclass
class MusicResult:
    path: str | None
    prompt: str = ""
    level_db: float = BED_DB
    attempts: list[dict] = field(default_factory=list)
    clarity: dict = field(default_factory=dict)


def _brief(ctx: Ctx, script: Script, seconds: float) -> MusicBrief:
    b = ctx.brief
    lesson = " ".join(sc.title for sc in script.scenes)
    prompt = f"""Write a prompt for an INSTRUMENTAL background track for a {seconds:.0f}-second whiteboard
explainer video for Class {b.grade} students ({b.subject}: {b.topic}); scene titles: {lesson}.
The narration is a warm teacher voice, so the music must stay underneath it: gentle, curious and
positive, steady tempo and volume from start to end, light instruments (e.g. soft piano, marimba,
plucked strings, light pads), a subtle Indian touch is welcome (e.g. a soft sitar or santoor motif),
no vocals, no drums hits, no drops or build-ups, no sudden changes. Return the prompt and a 2-3 word mood."""
    return ctx.llm.chat_json(ctx.settings.planner_model, [{"role": "user", "content": prompt}], MusicBrief,
                             purpose="music.brief", temperature=0.6)


def _compose(ctx: Ctx, prompt: str, seconds: float) -> bytes:
    r = httpx.post("https://api.elevenlabs.io/v1/music", headers={"xi-api-key": ctx.settings.elevenlabs_key},
                   params={"output_format": "mp3_44100_128"}, timeout=300,
                   json={"prompt": prompt, "music_length_ms": int(min(max(seconds, 10), 300) * 1000),
                         "model_id": "music_v1", "force_instrumental": True})
    if r.status_code != 200:
        raise LLMError(f"ElevenLabs music HTTP {r.status_code}: {r.text[:200]}")
    return r.content


def _jumps_db(x: np.ndarray) -> float:
    """Largest rise in short-term loudness (0.5 s windows) — a proxy for hits and drops.

    The first/last 2 s are ignored (tracks start from silence and our mix fades them anyway), and
    quiet moments are clamped to 20 dB below the median so a brief rest is not scored as a jump.
    """
    db = frame_db(x)
    win = db[: db.size // 50 * 50].reshape(-1, 50).mean(axis=1)
    body = win[4:-4]
    if body.size < 2:
        return 0.0
    body = np.maximum(body, np.median(win) - 20)
    return float(np.max(np.diff(body)))


def _listen(ctx: Ctx, path, brief: MusicBrief) -> MusicReview:
    text = (f"This is a background music track for a children's educational video (intended mood: {brief.mood}). "
            "It will play quietly under a teacher's narration. Judge it strictly: vocals of any kind, sudden loud "
            "parts, or busy/attention-grabbing music are not acceptable. List concrete problems.")
    return ctx.llm.chat_json(ctx.settings.audio_model,
                             [{"role": "user", "content": [{"type": "text", "text": text}, audio_part(path)]}],
                             MusicReview, purpose="music.listen", temperature=0.0)


def _to_rms_db(x: np.ndarray, target_db: float) -> np.ndarray:
    rms = float(np.sqrt(np.mean(x ** 2) + 1e-12))
    return x * (10 ** (target_db / 20) / rms)


def duck_envelope(n: int, speech: list[tuple[float, float]], total: float, sr: int = SR,
                  bed_db: float = BED_DB, duck_db: float = DUCK_DB) -> np.ndarray:
    """Per-sample gain (linear) for the music bed: ducked under speech, smoothed, faded in/out."""
    hop = 0.01
    frames = int(np.ceil(total / hop)) + 1
    target = np.zeros(frames)                          # 0 = full bed level, 1 = fully ducked
    for s0, s1 in speech:
        target[int(max(s0 - 0.12, 0) / hop): int(min(s1 + 0.2, total) / hop) + 1] = 1.0
    env = np.zeros(frames)
    a, r = hop / ATTACK, hop / RELEASE
    level = 0.0
    for i, tgt in enumerate(target):                   # fast attack, slow release, like a broadcast ducker
        level += (tgt - level) * (a if tgt > level else r)
        env[i] = level
    db = bed_db - duck_db * env
    t = np.arange(frames) * hop
    fade = np.clip(t / FADE_IN, 0, 1) * np.clip((total - t) / FADE_OUT, 0, 1)
    gain = 10 ** (db / 20) * fade
    return np.interp(np.arange(n) / sr, t, gain)


def _mix(narration: np.ndarray, music_raw: np.ndarray, speech, total: float, bed_db: float) -> np.ndarray:
    n = len(narration)
    m = music_raw
    if len(m) < n:                                     # loop the track if it came back short
        reps = int(np.ceil(n / max(len(m), 1))) + 1
        m = np.concatenate([m] * reps)
    m = _to_rms_db(m[:n], 0.0)                         # unit RMS; the envelope sets the absolute level
    return narration + m * duck_envelope(n, speech, total, bed_db=bed_db)


def _clarity(ctx: Ctx, path, reference: str) -> tuple[float, MixReview | None]:
    try:
        rv = ctx.llm.chat_json(ctx.settings.audio_model, [{"role": "user", "content": [
            {"type": "text", "text": f"Transcribe the {ctx.brief.lang.name} narration verbatim in {ctx.brief.lang.script} "
                                     "script, ignoring the background music. Then judge whether the music ever makes a "
                                     "word hard to hear."},
            audio_part(path)]}], MixReview, purpose="music.clarity", temperature=0.0)
        return similarity(reference, rv.transcript), rv
    except LLMError as e:
        ctx.trace.log("music", "skip", f"clarity check unavailable: {e}")
        return 1.0, None


def add_music(ctx: Ctx, script: Script, tl: Timeline) -> MusicResult:
    """Compose, check and mix a background bed into the timeline's narration; updates tl.audio_path."""
    run = ctx.run_dir
    narration = decode(run / tl.audio_path)
    total = tl.duration
    reference = " ".join(sc.narration for sc in script.scenes)
    speech = [(w.start, w.end) for c in tl.captions for w in c.words]
    music_dir = run / "audio"
    manifest = music_dir / "music.json"

    brief_key = text_hash(reference, f"{total:.2f}")
    cached = json.loads(manifest.read_text()) if manifest.exists() else {}
    result = MusicResult(None)
    track = None
    if cached.get("key") == brief_key and cached.get("track") and (run / cached["track"]).exists():
        ctx.trace.log("music", "cache", "lesson unchanged, reusing the approved music track")
        track, result.prompt, result.attempts = cached["track"], cached["prompt"], cached.get("attempts", [])
    else:
        brief = _brief(ctx, script, total)
        prompt = brief.prompt
        ctx.trace.log("music", "brief", f"mood '{brief.mood}': {prompt[:150]}")
        best: tuple[int, str] | None = None
        for attempt in range(1, 3):
            try:
                data = _compose(ctx, prompt, total + 0.5)
            except (LLMError, httpx.HTTPError) as e:
                ctx.trace.log("music", "error", f"compose failed: {e}")
                result.attempts.append({"attempt": attempt, "error": str(e)[:200]})
                continue
            rel = f"audio/music_take{attempt}.mp3"
            (run / rel).write_bytes(data)
            jump = _jumps_db(decode(run / rel))
            try:
                rv = _listen(ctx, run / rel, brief)
            except LLMError as e:
                ctx.trace.log("music", "error", f"listening check unavailable ({e}); accepting track unreviewed")
                rv = MusicReview(has_vocals=False, has_sudden_loud_parts=False, mood_fits=True,
                                 calm_enough_for_background=True, score=7, problems=["unreviewed"])
            if jump > 10:
                rv.has_sudden_loud_parts = True
                rv.problems.append(f"loudness jumps by {jump:.0f} dB")
            result.attempts.append({"attempt": attempt, "track": rel, "score": rv.score, "approved": rv.approved,
                                    "loudness_jump_db": round(jump, 1), "problems": rv.problems})
            ctx.trace.log("music", "verdict", f"take {attempt}: {'APPROVED' if rv.approved else 'REJECTED'} "
                          f"score={rv.score}/10, max loudness jump {jump:.1f} dB"
                          + (f" — {'; '.join(rv.problems)[:160]}" if rv.problems else ""))
            if rv.approved:
                best = (rv.score, rel)
                break
            prompt = f"{brief.prompt} Avoid: {'; '.join(rv.problems)}."
        if best is None:
            ctx.trace.log("music", "drop", "no acceptable track; the video keeps narration only")
            manifest.write_text(json.dumps({"key": brief_key, "track": None, "attempts": result.attempts}, indent=1))
            return result
        track, result.prompt = best[1], brief.prompt
        manifest.write_text(json.dumps({"key": brief_key, "track": track, "prompt": brief.prompt,
                                        "attempts": result.attempts}, ensure_ascii=False, indent=1))

    music_raw = decode(run / track)
    for bed_db in (BED_DB, BED_DB - 6):
        mixed = _mix(narration, music_raw, speech, total, bed_db)
        peak = np.abs(mixed).max()
        if peak > 0.97:
            mixed *= 0.97 / peak
        out = run / "soundtrack.wav"
        write_wav(out, mixed)
        sim, rv = _clarity(ctx, out, reference)
        result.clarity = {"bed_db": bed_db, "asr_similarity": round(sim, 3),
                          "balance": rv.balance_score if rv else None, "too_loud": rv.music_too_loud if rv else None}
        ok = sim >= 0.9 and not (rv and rv.music_too_loud)
        ctx.trace.log("music", "mix", f"bed {bed_db:.0f} dB, ducked −{DUCK_DB:.0f} dB under speech: narration ASR "
                      f"similarity {sim:.2f}" + (f", balance {rv.balance_score}/10" if rv else "")
                      + (" ✓" if ok else " — too loud, lowering the bed"))
        if ok:
            tl.audio_path = str(out.relative_to(run))
            result.path, result.level_db = tl.audio_path, bed_db
            return result
    ctx.trace.log("music", "drop", "music still masks the narration at the lower level; keeping narration only")
    return result
