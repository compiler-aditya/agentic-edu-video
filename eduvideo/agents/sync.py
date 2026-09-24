"""Sync agent: builds a sample-accurate timeline from the measured narration audio.

Everything on screen is driven by the TTS word timestamps:
  * scene visuals start/end on the scene's narration boundaries (crossfades sit in the gaps),
  * captions are chunked from the aligned words, and the active word is highlighted as spoken,
  * labels, arrows, highlights and board lines start when their cue words are spoken.
The master audio track is assembled from the per-scene clips at the same offsets,
so audio and visuals share one clock.
"""
from __future__ import annotations

import difflib
import json
import math

import numpy as np

from ..audio import SR, decode, frame_db, normalise, normalise_group, voiced_mask, write_wav
from ..schemas import AnimEvent, Caption, SceneAudio, SceneBoard, SceneSlot, Script, Storyboard, Timeline, Word
from ..textutil import norm_token
from .base import Ctx

_BREAK_AFTER = ("।", "॥", ".", "?", "!", ",", ";", ":")
MAX_CAPTION_WORDS = 7
MAX_CAPTION_SECONDS = 3.5
# Hindi/Marathi postpositions and auxiliaries that should not begin a caption line.
_NO_LINE_START = {norm_token(w) for w in "का की के को में से ने पर तक लिए है हैं था थी थे हुआ हुई रहा रही रहे गया गई जाता जाती जाते वाला वाली वाले भी ही".split()}


# ----------------------------------------------------------------- alignment
def narration_tokens(narration: str) -> list[str]:
    """Split on whitespace and glue stray punctuation onto the previous token."""
    out: list[str] = []
    for tok in narration.split():
        if not norm_token(tok) and out:
            out[-1] += tok
        else:
            out.append(tok)
    return out


def align_words(narration: str, boundaries: list[Word]) -> tuple[list[Word], float]:
    """Map narration tokens (with punctuation, for display) onto TTS word timestamps.

    Returns the aligned words and the fraction of tokens that matched a boundary exactly.
    Unmatched tokens get timings interpolated from their neighbours.
    """
    tokens = narration_tokens(narration)
    a = [norm_token(t) for t in tokens]
    b = [norm_token(w.text) for w in boundaries]
    times: list[tuple[float, float] | None] = [None] * len(tokens)
    matched = 0
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            for k in range(i2 - i1):
                times[i1 + k] = (boundaries[j1 + k].start, boundaries[j1 + k].end)
            matched += i2 - i1
        elif op == "replace":
            # Spread the TTS span proportionally over the differing tokens (e.g. numbers read out).
            s, e = boundaries[j1].start, boundaries[j2 - 1].end
            weights = np.array([max(len(x), 1) for x in a[i1:i2]], dtype=float)
            edges = s + (e - s) * np.concatenate([[0], np.cumsum(weights) / weights.sum()])
            for k in range(i2 - i1):
                times[i1 + k] = (float(edges[k]), float(edges[k + 1]))
    _interpolate(times, boundaries)
    words = [Word(text=t, start=round(s, 3), end=round(e, 3)) for t, (s, e) in zip(tokens, times)]  # type: ignore[misc]
    return words, matched / max(len(tokens), 1)


def _interpolate(times: list[tuple[float, float] | None], boundaries: list[Word]) -> None:
    n = len(times)
    end_all = boundaries[-1].end if boundaries else 0.0
    i = 0
    while i < n:
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < n and times[j] is None:
            j += 1
        left = times[i - 1][1] if i > 0 and times[i - 1] else (boundaries[0].start if boundaries else 0.0)
        right = times[j][0] if j < n and times[j] else end_all
        step = max(right - left, 0.05 * (j - i)) / (j - i)
        for k in range(i, j):
            times[k] = (left + step * (k - i), left + step * (k - i + 1))
        i = j


def estimate_words(narration: str, onset: float, offset: float) -> list[Word]:
    """Fallback when the TTS engine gives no timestamps: distribute by syllable weight + pauses."""
    tokens = narration_tokens(narration)
    weights = []
    for t in tokens:
        w = len(norm_token(t)) + 1
        if t.endswith(("।", ".", "?", "!")):
            w += 4
        elif t.endswith((",", ";", ":")):
            w += 2
        weights.append(w)
    total = sum(weights)
    words, cur = [], onset
    for t, w in zip(tokens, weights):
        dur = (offset - onset) * w / total
        words.append(Word(text=t, start=round(cur, 3), end=round(cur + dur * 0.85, 3)))
        cur += dur
    return words


def estimate_ts_offset(samples: np.ndarray, words: list[Word], sr: int = SR) -> tuple[float, list[float]]:
    """Measure how far the audible speech lags the TTS word timestamps.

    Anchors are the points where speech resumes after >=150 ms of silence (clip start and
    sentence/clause pauses). Each anchor is paired with the first word timestamped after
    that silence began; the median difference is the offset. edge-tts timestamps typically lead the decoded
    audio by ~0.1-0.25 s, which would make highlights appear before the word is heard.
    """
    if not words:
        return 0.0, []
    voiced = voiced_mask(frame_db(samples, sr))
    anchors, silent = [], 15  # (speech resumes at, silence began at); the clip start counts as a pause
    for i, v in enumerate(voiced):
        if v and silent >= 15:  # >=150 ms: real pauses, not stop-consonant closures
            anchors.append((i * 0.01, max(i - silent, 0) * 0.01))
        silent = 0 if v else silent + 1
    deltas = []
    for resume, pause_start in anchors:
        # the word spoken after the pause is the first one timestamped after the silence began
        cands = [w for w in words if w.start >= pause_start - 0.1 and w.start <= resume + 0.15]
        if cands:
            d = resume - min(cands, key=lambda w: w.start).start
            if -0.15 <= d <= 0.45:
                deltas.append(round(d, 3))
    return (float(np.median(deltas)) if deltas else 0.0), deltas


def shift_words(words: list[Word], offset: float, duration: float) -> list[Word]:
    return [Word(text=w.text, start=round(min(max(w.start + offset, 0.0), duration), 3),
                 end=round(min(max(w.end + offset, 0.0), duration), 3)) for w in words]


# ------------------------------------------------------------------ timeline
def _caption_chunks(words: list[Word]) -> list[list[Word]]:
    """Clause-aware caption chunking.

    1. split at sentence/clause punctuation, 2. merge tiny clauses into a neighbour,
    3. split long clauses into balanced parts, never starting a line on a postposition
       or auxiliary (e.g. "के | लिए"), which reads badly in Hindi.
    """
    clauses: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        cur.append(w)
        if w.text.endswith(_BREAK_AFTER):
            clauses.append(cur)
            cur = []
    if cur:
        clauses.append(cur)

    merged: list[list[Word]] = []
    for cl in clauses:
        if merged and (len(merged[-1]) <= 2 or len(cl) <= 2) and len(merged[-1]) + len(cl) <= MAX_CAPTION_WORDS:
            merged[-1] = merged[-1] + cl
        else:
            merged.append(cl)

    chunks: list[list[Word]] = []
    for cl in merged:
        parts = max(math.ceil(len(cl) / MAX_CAPTION_WORDS), math.ceil((cl[-1].end - cl[0].start) / MAX_CAPTION_SECONDS))
        parts = max(1, min(parts, len(cl) // 2 or 1))
        start = 0
        for k in range(1, parts):
            cut = round(k * len(cl) / parts)
            for delta in (0, 1, -1, 2, -2):
                c = cut + delta
                if start < c < len(cl) and norm_token(cl[c].text) not in _NO_LINE_START:
                    cut = c
                    break
            chunks.append(cl[start:cut])
            start = cut
        chunks.append(cl[start:])
    return [c for c in chunks if c]


def _find_term(term: str, words: list[Word], after: float | None = None) -> int | None:
    """Index of the word where `term` is spoken; with `after`, prefer the first occurrence at/after that time."""
    parts = [norm_token(p) for p in term.split() if norm_token(p)]
    toks = [norm_token(w.text) for w in words]
    if not parts:
        return None
    hits = [i for i in range(len(toks) - len(parts) + 1)
            if all(toks[i + k] == parts[k] or (len(parts[k]) >= 3 and toks[i + k].startswith(parts[k]))
                   for k in range(len(parts)))]
    if hits:
        if after is not None:
            later = [i for i in hits if words[i].start >= after]
            if later:
                return later[0]
        return hits[0]
    # Fuzzy fallback: best window by similarity.
    joined = "".join(parts)
    best, best_i = 0.0, None
    for i in range(len(toks) - len(parts) + 1):
        r = difflib.SequenceMatcher(None, joined, "".join(toks[i : i + len(parts)])).ratio()
        if r > best:
            best, best_i = r, i
    return best_i if best >= 0.75 else None


# Writing/drawing speeds for the whiteboard animation (seconds)
TITLE_CPS = 22.0          # title handwriting speed, characters per second
LABEL_WRITE = 0.45        # label text
LEADER_DRAW = 0.30        # leader line from label to the part
ARROW_DRAW = 0.6
HIGHLIGHT_DRAW = 0.45
MIN_GAP = 0.12            # pen needs this long between two actions
MIN_SKETCH = 1.3          # shortest stroke-by-stroke drawing
COLOR_FADE = 0.7          # colour fades in under the pen; it does not block annotations


def _write_time(text: str, cps: float) -> float:
    return min(max(len(text) / cps, 0.5), 1.6)


def schedule_scene(ctx: Ctx, slot: SceneSlot, board: SceneBoard, words: list[Word], has_image: bool) -> list[AnimEvent]:
    """Turn storyboard cues into absolute times and lay out the pen's work for one scene."""
    sid = slot.scene_id
    ev: list[AnimEvent] = []
    t0 = max(slot.start - 0.15, slot.vis_start + 0.25)  # after the slide-in has settled
    title_end = t0 + _write_time(slot.title, TITLE_CPS)
    ev.append(AnimEvent(scene_id=sid, kind="title", start=round(t0, 3), end=round(title_end, 3)))

    draw_start = title_end + 0.1
    # earliest moment the pen could start annotating (minimum sketch time + colour pass)
    ready = draw_start + (MIN_SKETCH if has_image and board.layout == "diagram" else 0.0)
    cues: list[tuple[float, str, int, str]] = []  # (time, kind, index, cue)
    items = [(a.kind, i, a.cue) for i, a in enumerate(board.annotations)] + \
            [("line", i, ln.cue) for i, ln in enumerate(board.board_lines)]
    for kind, i, cue in items:
        idx = _find_term(cue, words, after=ready)
        if idx is None:
            ctx.trace.log("sync", "warn", f"scene {sid}: cue '{cue}' not found in spoken words; scheduling after previous")
            cues.append((-1.0, kind, i, cue))
        else:
            cues.append((words[idx].start, kind, i, cue))
    found = sorted(c[0] for c in cues if c[0] >= 0)

    if has_image:
        # sketch until just before the first cue (bounded), then fade in colour
        first_diagram_cue = next((t for t, k, _, _ in sorted(cues) if t >= 0 and k != "line"), None)
        budget_end = slot.start + 0.55 * (slot.end - slot.start)
        draw_end = min(max((first_diagram_cue or budget_end) - MIN_GAP, draw_start + MIN_SKETCH), draw_start + 4.2)
        if board.layout == "board":
            draw_end = min(draw_end, draw_start + 2.4)
        ev.append(AnimEvent(scene_id=sid, kind="draw", start=round(draw_start, 3), end=round(draw_end, 3)))
        ev.append(AnimEvent(scene_id=sid, kind="color", start=round(draw_end, 3), end=round(draw_end + COLOR_FADE, 3)))
        # on board scenes the small drawing sketches itself while the pen writes the lines
        pen_free = draw_end if board.layout == "diagram" else draw_start
    else:
        pen_free = draw_start

    durations = {"label": LABEL_WRITE + LEADER_DRAW, "arrow": ARROW_DRAW, "highlight": HIGHLIGHT_DRAW}
    for t, kind, i, cue in sorted(cues, key=lambda c: (c[0] < 0, c[0])):
        cue_time = t if t >= 0 else None
        want = t if t >= 0 else (found[-1] if found else pen_free)
        start = max(want, pen_free + MIN_GAP if ev[-1].kind not in ("title", "color") else pen_free)
        if kind == "line":
            dur = _write_time(board.board_lines[i].text, 22.0) + 0.2
        else:
            dur = durations[kind]
        start = min(start, slot.end - 0.3)
        if cue_time is not None and start - cue_time > 0.35:
            ctx.trace.log("sync", "shift", f"scene {sid}: {kind} '{cue}' starts {start - cue_time:+.2f}s after its cue "
                          "(pen busy / drawing)")
        ev.append(AnimEvent(scene_id=sid, kind=kind, index=i, cue=cue, cue_time=cue_time,
                            start=round(start, 3), end=round(start + dur, 3)))
        pen_free = start + dur
    return ev


def build_timeline(ctx: Ctx, script: Script, storyboard: Storyboard, audios: dict[int, SceneAudio],
                   images: dict[int, str], has_image: dict[int, bool]) -> Timeline:
    s = ctx.settings
    tl_scenes: list[SceneSlot] = []
    captions: list[Caption] = []
    scene_words: dict[int, list[Word]] = {}
    clips: list[tuple[int, np.ndarray]] = []

    # ElevenLabs clips are slices of one continuous take that already contain the natural pauses:
    # lay them back to back with one shared gain; per-scene TTS clips get a fixed gap and own gain.
    continuous = all(audios[sc.id].engine == "elevenlabs" for sc in script.scenes)
    gap = 0.0 if continuous else s.scene_gap
    raws = [decode(ctx.run_dir / audios[sc.id].path) for sc in script.scenes]
    normed = normalise_group(raws) if continuous else [normalise(r) for r in raws]
    cursor = s.lead_in
    for sc, board, raw, samples in zip(script.scenes, storyboard.scenes, raws, normed):
        au = audios[sc.id]
        start = round(round(cursor * SR) / SR, 4)
        end = start + len(samples) / SR
        clips.append((int(round(start * SR)), samples))
        offset = au.ts_offset if au.ts_offset is not None else estimate_ts_offset(raw, au.words)[0]
        words = shift_words(au.words, offset, len(raw) / SR)
        ctx.trace.log("sync", "calibrate", f"scene {sc.id}: TTS timestamps shifted {offset * 1000:+.0f} ms to match audible speech")
        abs_words = [Word(text=w.text, start=round(start + w.start, 3), end=round(start + w.end, 3)) for w in words]
        scene_words[sc.id] = abs_words
        for chunk in _caption_chunks(abs_words):
            captions.append(Caption(start=chunk[0].start, end=chunk[-1].end, words=chunk, scene_id=sc.id))
        tl_scenes.append(SceneSlot(scene_id=sc.id, title=sc.title, layout=board.layout, image=images[sc.id],
                                   start=start, end=end, vis_start=0.0, vis_end=0.0))
        cursor = end + gap

    total = tl_scenes[-1].end + s.tail
    n_frames = int(np.ceil(total * s.fps))
    total = n_frames / s.fps  # audio is padded to an exact frame multiple → zero A/V drift

    # Visual slots: cuts sit in the middle of each inter-scene gap; scene 1 follows the intro card.
    for i, slot in enumerate(tl_scenes):
        slot.vis_start = round(max(s.lead_in - 0.45, 0.0), 3) if i == 0 else round((tl_scenes[i - 1].end + slot.start) / 2, 3)
        slot.vis_end = total if i == len(tl_scenes) - 1 else round((slot.end + tl_scenes[i + 1].start) / 2, 3)

    events: list[AnimEvent] = []
    for slot, board in zip(tl_scenes, storyboard.scenes):
        events += schedule_scene(ctx, slot, board, scene_words[slot.scene_id], has_image[slot.scene_id])

    for i, cap in enumerate(captions):
        nxt = captions[i + 1] if i + 1 < len(captions) else None
        scene_end = next(sl.end for sl in tl_scenes if sl.scene_id == cap.scene_id)
        hold = cap.words[-1].end + 0.35
        if nxt and nxt.scene_id == cap.scene_id:
            cap.end = round(min(nxt.start, max(hold, cap.end)), 3)
        else:
            cap.end = round(min(hold, scene_end + 0.3), 3)

    master = np.zeros(int(round(total * SR)), dtype=np.float32)
    for offset, samples in clips:
        master[offset : offset + len(samples)] += samples[: max(0, len(master) - offset)]
    audio_path = ctx.run_dir / "narration.wav"
    write_wav(audio_path, master)

    tl = Timeline(duration=round(total, 3), fps=s.fps, width=s.width, height=s.height, scenes=tl_scenes,
                  captions=captions, events=events, audio_path=str(audio_path.relative_to(ctx.run_dir)))
    validate_timeline(ctx, tl)
    (ctx.run_dir / "timeline.json").write_text(tl.model_dump_json(indent=1))
    write_srt(ctx, tl)
    synced = [e for e in events if e.cue_time is not None]
    lag = [e.start - e.cue_time for e in synced]  # type: ignore[operator]
    ctx.trace.log("sync", "done", f"timeline {tl.duration:.2f}s ({n_frames} frames), {len(captions)} captions, "
                  f"{len(events)} animation events; {len(synced)} cued to spoken words"
                  + (f", median start lag {float(np.median(lag)) * 1000:.0f} ms" if lag else ""))
    return tl


def validate_timeline(ctx: Ctx, tl: Timeline) -> None:
    problems = []
    prev_end = 0.0
    for c in tl.captions:
        if c.start < prev_end - 1e-3:
            problems.append(f"caption overlaps previous at {c.start:.2f}s")
        if not (0 <= c.start < c.end <= tl.duration):
            problems.append(f"caption outside video bounds at {c.start:.2f}s")
        if any(w2.start < w1.start for w1, w2 in zip(c.words, c.words[1:])):
            problems.append(f"non-monotonic word times at {c.start:.2f}s")
        prev_end = c.end
    slots = {sl.scene_id: sl for sl in tl.scenes}
    for sl in tl.scenes:
        if not (sl.vis_start <= sl.start < sl.end <= sl.vis_end):
            problems.append(f"scene {sl.scene_id} narration not inside its visual slot")
    for e in tl.events:
        sl = slots[e.scene_id]
        if not (sl.vis_start - 0.2 <= e.start <= e.end <= sl.vis_end + 0.05):
            problems.append(f"scene {e.scene_id} {e.kind} event {e.start:.2f}-{e.end:.2f}s outside its scene")
    if problems:
        raise ValueError("timeline validation failed: " + "; ".join(problems[:5]))
    ctx.trace.log("sync", "validate", "timeline OK: captions monotonic, events inside their scenes, narration inside visual slots")


def _ts(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def write_srt(ctx: Ctx, tl: Timeline) -> None:
    lines = []
    for i, c in enumerate(tl.captions, 1):
        lines += [str(i), f"{_ts(c.start)} --> {_ts(c.end)}", " ".join(w.text for w in c.words), ""]
    (ctx.run_dir / "captions.srt").write_text("\n".join(lines), encoding="utf-8")
    words = [w.model_dump() for c in tl.captions for w in c.words]
    (ctx.run_dir / "word_timestamps.json").write_text(json.dumps(words, ensure_ascii=False, indent=0))
