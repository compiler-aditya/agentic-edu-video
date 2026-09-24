"""Final QA agent: verifies the rendered file, then has a vision model inspect keyframes.

Checks:
  * the MP4 decodes end-to-end without errors,
  * duration is inside the 30-60 s contract,
  * video and audio stream durations agree (A/V drift),
  * every scene has narration, captions and a visual slot (from the timeline),
  * a vision model looks at one keyframe per scene (drawing + labels/arrows + caption) and
    flags drawings that don't match the narration, labels pointing at the wrong part,
    illegible captions or rendering glitches.
Flagged drawings go back to the Visual agent; mis-pointed labels go back to the Grounder.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..audio import ffmpeg_exe
from ..llm import LLMError, image_part
from ..schemas import FinalReview, Script, Storyboard, Timeline
from .base import Ctx


@dataclass
class QAResult:
    ok: bool
    video_s: float
    audio_s: float
    problems: list[str] = field(default_factory=list)
    redo_visuals: list[int] = field(default_factory=list)
    regrounds: list[int] = field(default_factory=list)
    review: FinalReview | None = None


def _stream_seconds(path: Path, stream: str) -> float:
    proc = subprocess.run([ffmpeg_exe(), "-v", "error", "-i", str(path), "-map", f"0:{stream}:0", "-f", "null",
                           "-progress", "pipe:1", "-nostats", "-"], capture_output=True, text=True)
    if proc.returncode != 0 or proc.stderr.strip():
        raise RuntimeError(f"decode error in {stream} stream: {proc.stderr.strip()[:300]}")
    times = re.findall(r"out_time_us=(\d+)", proc.stdout)
    return int(times[-1]) / 1e6 if times else 0.0


def final_qa(ctx: Ctx, video: Path, tl: Timeline, script: Script, sb: Storyboard,
             keyframes: dict[int, Path]) -> QAResult:
    s = ctx.settings
    problems: list[str] = []
    v = _stream_seconds(video, "v")
    a = _stream_seconds(video, "a")
    drift = abs(v - a)
    ctx.trace.log("final_qa", "probe", f"video {v:.3f}s, audio {a:.3f}s, drift {drift * 1000:.0f} ms, decode OK")
    if not s.min_seconds <= v <= s.max_seconds:
        problems.append(f"duration {v:.1f}s outside {s.min_seconds:.0f}-{s.max_seconds:.0f}s")
    if drift > 1.5 / tl.fps + 0.03:  # AAC priming/padding is ~21-46 ms
        problems.append(f"A/V drift {drift * 1000:.0f} ms")
    for sl in tl.scenes:
        if not any(c.scene_id == sl.scene_id for c in tl.captions):
            problems.append(f"scene {sl.scene_id} has no captions")

    result = QAResult(ok=not problems, video_s=v, audio_s=a, problems=problems)
    if not keyframes:
        return result

    by_id = {sc.id: sc for sc in script.scenes}
    content: list[dict] = [{"type": "text", "text": (
        f"These are the final frames of each scene of a whiteboard-style educational video for Class "
        f"{ctx.brief.grade} ({ctx.brief.subject}: {ctx.brief.topic}), narrated in {ctx.brief.lang.name}. "
        "Each frame shows: a small grey header and scene counter at the top, the handwritten scene title, a "
        "drawing (or written board lines on the left for summary scenes), labels joined to parts of the drawing "
        "by thin lines ending in a dot, coloured arrows, circles around emphasised parts, and a dark caption bar "
        "at the bottom. For each frame check: (a) the drawing fits the narration; (b) labels_point_correctly: "
        "every label's line ends on the part its text names and every arrow goes to/from the right parts; "
        f"(c) the caption and all {ctx.brief.lang.script} text are legible and correctly shaped (no broken "
        "conjuncts/matras, not cut off); (d) rendering_glitch: overlapping labels, text over other text, "
        "elements cut off by the frame edge. Report one entry per scene_id.")}]
    for sid in sorted(keyframes):
        board = sb.scenes[sid - 1]
        items = [f"{a.kind} '{a.text or a.target}' → {a.target}" for a in board.annotations] + \
                [f"written line '{ln.text}'" for ln in board.board_lines]
        content.append({"type": "text", "text": f"scene_id={sid}. Narration: {by_id[sid].narration}\n"
                                                f"Expected on screen: {'; '.join(items)}"})
        content.append(image_part(keyframes[sid], "image/jpeg"))
    try:
        review = ctx.llm.chat_json(s.vision_model, [{"role": "user", "content": content}], FinalReview,
                                   purpose="final_qa.frames", temperature=0.1)
    except LLMError as e:
        ctx.trace.log("final_qa", "skip", f"vision review unavailable: {e}")
        return result
    result.review = review
    for fc in review.frames:
        status = "ok" if (fc.visual_matches_narration and fc.labels_point_correctly and fc.captions_legible
                          and not fc.rendering_glitch) else "FLAG"
        ctx.trace.log("final_qa", "frame", f"scene {fc.scene_id}: {status}"
                      + (f" — {'; '.join(fc.problems)[:160]}" if fc.problems else ""))
        if not fc.visual_matches_narration:
            result.redo_visuals.append(fc.scene_id)
        elif not fc.labels_point_correctly:
            result.regrounds.append(fc.scene_id)
        if not fc.captions_legible or fc.rendering_glitch:
            result.problems.append(f"scene {fc.scene_id}: caption/render issue: {'; '.join(fc.problems)[:200]}")
    result.ok = not result.problems and not result.redo_visuals and not result.regrounds
    ctx.trace.log("final_qa", "verdict", ("PASS" if result.ok else "FAIL") + (f" — {'; '.join(result.problems)}" if result.problems else "")
                  + (f" redo visuals {result.redo_visuals}" if result.redo_visuals else "")
                  + (f" re-locate labels {result.regrounds}" if result.regrounds else ""))
    return result
