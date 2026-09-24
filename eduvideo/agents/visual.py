"""Visual agent + Visual Critic + Grounder.

For each scene:
  1. generate a text-free textbook diagram on a white background (so the renderer can
     sketch it stroke by stroke on the whiteboard),
  2. the Visual Critic checks relevance, scientific accuracy, garbled text and that every
     storyboard element is present — otherwise regenerate with its improved prompt,
  3. the Grounder (vision model) locates every element (box + pointer point) so labels
     and arrows can be drawn onto the right part; missing elements send the image back.
If image generation is unavailable, a plain board with the scene title is used.
"""
from __future__ import annotations

import io
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from ..llm import LLMError, image_part
from ..schemas import Grounding, LessonPlan, Located, SceneBoard, SceneScript, VisualReview
from ..textutil import text_hash
from .base import Ctx

SRC_W, SRC_H = 1200, 900  # 4:3 drawing canvas; labels live in the gutters beside it

STYLE = ("Clean flat vector textbook illustration with bold dark outlines and a limited bright palette, "
         "on a pure white background, like a diagram in an NCERT science textbook. ")

CRITIC_SYSTEM = """You are a meticulous visual reviewer for children's educational videos in India.
You judge whether an illustration correctly and clearly supports the narration it will be shown with."""


@dataclass
class VisualResult:
    scene_id: int
    path: Path
    source: str                      # "generated" | "fallback" | "cached"
    attempts: list[dict] = field(default_factory=list)
    score: int = 0
    approved: bool = False
    key: str = ""                    # hash of the storyboard visuals (prompt + parts) it was approved for
    grounding: dict[str, dict] = field(default_factory=dict)


def _key(scene: SceneScript, board: SceneBoard) -> str:
    # A drawing depends only on what it must show; retimed narration keeps the same content.
    return text_hash(board.image_prompt, ",".join(f"{e.id}:{e.description}" for e in board.elements))


def _prompt(style: str, board: SceneBoard, base: str | None = None, avoid: list[str] | None = None) -> str:
    parts = "; ".join(f"{e.id.replace('_', ' ')}: {e.description}" for e in board.elements)
    extra = f" Avoid: {'; '.join(avoid)}." if avoid else ""
    return (f"{(base or board.image_prompt).strip()} It must clearly show these separate parts: {parts}. "
            f"{STYLE}{style.strip()} Leave generous white space around the drawing. Absolutely no text, "
            f"words, letters, numbers, labels or arrows with writing anywhere in the image.{extra}")


def strip_frame(img: Image.Image) -> Image.Image:
    """Whiten border/frame lines that image models like to draw around a diagram."""
    a = np.asarray(img).copy()
    nonwhite = (a.min(axis=2) < 225)
    h, w = nonwhite.shape
    rows, cols = nonwhite.mean(axis=1), nonwhite.mean(axis=0)
    for i in range(int(h * 0.12)):              # top / bottom edges
        if rows[i] > 0.6:
            a[: i + 3] = 255
        if rows[h - 1 - i] > 0.6:
            a[h - 4 - i:] = 255
    for j in range(int(w * 0.12)):              # left / right edges
        if cols[j] > 0.6:
            a[:, : j + 3] = 255
        if cols[w - 1 - j] > 0.6:
            a[:, w - 4 - j:] = 255
    return Image.fromarray(a)


def fit_canvas(data: bytes | Image.Image) -> Image.Image:
    """Remove frames, crop to the drawn content, then pad with white to a 4:3 canvas (never cuts content)."""
    img = data if isinstance(data, Image.Image) else Image.open(io.BytesIO(data))
    img = strip_frame(img.convert("RGB"))
    a = np.asarray(img).astype(np.int16)
    ink = (a.min(axis=2) < 235) | (a.max(axis=2) - a.min(axis=2) > 25)
    ys, xs = np.where(ink)
    if ys.size:
        pad = int(0.04 * max(img.size))
        x0, x1 = max(xs.min() - pad, 0), min(xs.max() + pad, img.width)
        y0, y1 = max(ys.min() - pad, 0), min(ys.max() + pad, img.height)
        img = img.crop((x0, y0, x1, y1))
    w, h = img.size
    scale = min(SRC_W / w, SRC_H / h)
    img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (SRC_W, SRC_H), (255, 255, 255))
    canvas.paste(img, ((SRC_W - img.width) // 2, (SRC_H - img.height) // 2))
    return canvas


def critique(ctx: Ctx, img_path: Path, scene: SceneScript, board: SceneBoard, goal: str) -> VisualReview:
    elements = "\n".join(f"- {e.id}: {e.description}" for e in board.elements)
    prompt = f"""This drawing will be sketched on a whiteboard while the narrator says (in {ctx.brief.lang.name}):
"{scene.narration}"

Scene learning goal: {goal}
Audience: Class {ctx.brief.grade} students, subject {ctx.brief.subject}.
Required parts (the video will point at each one with a label or arrow):
{elements}

The drawing is deliberately text-free: labels are added by the video, so do NOT penalise missing
labels. List in missing_elements the ids of required parts that are absent or unrecognisable.
Mark scientifically_accurate=false for wrong biology/physics/geography. Any text that appears and is
misspelled or gibberish sets garbled_or_wrong_text=true. If the score is below 7, write
improved_prompt: a complete English prompt that fixes the problems."""
    return ctx.llm.chat_json(
        ctx.settings.vision_model,
        [{"role": "system", "content": CRITIC_SYSTEM},
         {"role": "user", "content": [{"type": "text", "text": prompt}, image_part(img_path)]}],
        VisualReview, purpose=f"visual_critic.scene{scene.id}", temperature=0.2,
    )


def ground(ctx: Ctx, img_path: Path, board: SceneBoard, feedback: list[str] | None = None) -> dict[str, dict]:
    """Locate every storyboard element in the image (normalised 0-1000 coordinates)."""
    elements = "\n".join(f"- {e.id}: {e.description}" for e in board.elements)
    fb = ("\nA previous attempt pointed at the wrong places: " + "; ".join(feedback)) if feedback else ""
    prompt = f"""Locate each of these parts in the image:
{elements}

For each id return found (false if it is not visible), box_2d = [ymin, xmin, ymax, xmax] tightly around
that part, and point = [y, x] on the part itself (not on the background) where a label pointer should
touch it. All coordinates are normalised to 0-1000.{fb}"""
    g = ctx.llm.chat_json(ctx.settings.vision_model,
                          [{"role": "user", "content": [{"type": "text", "text": prompt}, image_part(img_path)]}],
                          Grounding, purpose=f"grounder.scene{board.id}", temperature=0.0)
    out: dict[str, dict] = {}
    wanted = {e.id for e in board.elements}
    for loc in g.elements:
        if loc.id not in wanted or not loc.found or len(loc.box_2d) != 4:
            continue
        y0, x0, y1, x1 = (min(max(v, 0), 1000) for v in loc.box_2d)
        if y1 - y0 < 8 or x1 - x0 < 8:
            continue
        py, px = (loc.point if len(loc.point) == 2 else [(y0 + y1) // 2, (x0 + x1) // 2])
        if not (x0 <= px <= x1 and y0 <= py <= y1):
            py, px = (y0 + y1) // 2, (x0 + x1) // 2
        out[loc.id] = Located(id=loc.id, found=True, box_2d=[y0, x0, y1, x1], point=[py, px]).model_dump()
    return out


def make_visual(ctx: Ctx, plan: LessonPlan, scene: SceneScript, board: SceneBoard,
                feedback: list[str] | None = None, start_prompt: str | None = None) -> VisualResult:
    out_dir = ctx.run_dir / "images"
    out_dir.mkdir(exist_ok=True)
    final = out_dir / f"scene_{scene.id}.png"
    goal = plan.scenes[scene.id - 1].learning_goal
    result = VisualResult(scene.id, final, "fallback", key=_key(scene, board))

    if not ctx.settings.generate_images:
        Image.new("RGB", (SRC_W, SRC_H), (255, 255, 255)).save(final)
        ctx.trace.log("visual", "fallback", f"scene {scene.id}: image generation disabled, board without drawing")
        return result

    prompt = _prompt(plan.visual_style, board, start_prompt, feedback)
    best: tuple[int, Path, bool, dict] | None = None
    tag = "fix" if feedback else "try"
    for attempt in range(1, ctx.settings.max_image_attempts + 1):
        if attempt > 1 and not ctx.llm.can_spend(0.30):
            ctx.trace.log("visual", "budget", f"scene {scene.id}: cost guard reached, keeping best image so far")
            break
        try:
            data = ctx.llm.generate_image(prompt, purpose=f"visual.scene{scene.id}.{tag}{attempt}", aspect_ratio="4:3")
            path = out_dir / f"scene_{scene.id}_{tag}{attempt}.png"
            fit_canvas(data).save(path)
        except (LLMError, OSError) as e:
            ctx.trace.log("visual", "error", f"scene {scene.id} attempt {attempt}: {e}")
            result.attempts.append({"attempt": attempt, "error": str(e)[:200]})
            continue
        try:
            review = critique(ctx, path, scene, board, goal)
        except LLMError as e:
            ctx.trace.log("visual_critic", "error", f"scene {scene.id}: critic failed ({e}); accepting image unreviewed")
            review = VisualReview(relevant_to_narration=True, scientifically_accurate=True, garbled_or_wrong_text=False,
                                  age_appropriate=True, score=7, problems=["unreviewed: critic unavailable"])
        grounding: dict[str, dict] = {}
        if review.approved:
            try:
                grounding = ground(ctx, path, board)
            except LLMError as e:
                ctx.trace.log("grounder", "error", f"scene {scene.id}: {e}")
            needed = {a.target for a in board.annotations} | {a.source for a in board.annotations if a.source}
            missing = sorted(x for x in needed if x in {e.id for e in board.elements} and x not in grounding)
            ctx.trace.log("grounder", "locate", f"scene {scene.id}: located {len(grounding)}/{len(board.elements)} parts"
                          + (f", missing {missing}" if missing else ""))
            if missing:
                review.missing_elements = missing
                review.problems.append(f"could not locate: {', '.join(missing)}")
        result.attempts.append({"attempt": attempt, "image": path.name, "score": review.score,
                                "approved": review.approved, "problems": review.problems})
        ctx.trace.log("visual_critic", "verdict",
                      f"scene {scene.id} attempt {attempt}: {'APPROVED' if review.approved else 'REJECTED'} "
                      f"score={review.score}/10" + (f" — {'; '.join(review.problems)[:160]}" if review.problems else ""),
                      score=review.score)
        rank = review.score + (5 if review.approved else 0)
        if best is None or rank > best[0]:
            best = (rank, path, review.approved, grounding)
        if review.approved:
            break
        avoid = review.problems + [f"missing: {m}" for m in review.missing_elements]
        prompt = _prompt(plan.visual_style, board, review.improved_prompt or None, avoid)

    if best is None:
        Image.new("RGB", (SRC_W, SRC_H), (255, 255, 255)).save(final)
        ctx.trace.log("visual", "fallback", f"scene {scene.id}: all image attempts failed, board without drawing")
        return result
    if not best[3]:  # never grounded (e.g. best image was not approved): locate parts anyway
        try:
            best = (best[0], best[1], best[2], ground(ctx, best[1], board))
        except LLMError:
            pass
    Image.open(best[1]).save(final)
    result.source, result.score, result.approved, result.grounding = "generated", best[0] - (5 if best[2] else 0), best[2], best[3]
    ctx.trace.log("visual", "select", f"scene {scene.id}: using {best[1].name}")
    return result


def _write_manifest(ctx: Ctx, out: dict[int, VisualResult]) -> None:
    (ctx.run_dir / "images" / "visuals.json").write_text(json.dumps(
        {k: {"source": v.source, "score": v.score, "approved": v.approved, "key": v.key,
             "grounding": v.grounding, "attempts": v.attempts} for k, v in sorted(out.items())},
        ensure_ascii=False, indent=1))


def load_manifest(ctx: Ctx) -> dict[int, dict]:
    vj = ctx.run_dir / "images" / "visuals.json"
    return {int(k): v for k, v in json.loads(vj.read_text()).items()} if vj.exists() else {}


def make_all_visuals(ctx: Ctx, plan: LessonPlan, scenes: list[SceneScript], boards: list[SceneBoard]) -> dict[int, VisualResult]:
    """Generate (or reuse, when the scene's prompt and parts are unchanged since approval) every scene's drawing."""
    prior = load_manifest(ctx)
    ctx.trace.log("visual", "start", f"drawing {len(scenes)} illustrations with {ctx.settings.image_model}")

    def one(pair: tuple[SceneScript, SceneBoard]) -> VisualResult:
        sc, bd = pair
        p = prior.get(sc.id)
        path = ctx.run_dir / "images" / f"scene_{sc.id}.png"
        if p and p.get("approved") and p.get("key") == _key(sc, bd) and path.exists():
            ctx.trace.log("visual", "cache", f"scene {sc.id}: approved drawing unchanged — reusing")
            return VisualResult(sc.id, path, "cached", p.get("attempts", []), p.get("score", 0), True, p["key"],
                                p.get("grounding", {}))
        return make_visual(ctx, plan, sc, bd)

    with ThreadPoolExecutor(max_workers=ctx.settings.max_workers) as pool:
        out = {r.scene_id: r for r in pool.map(one, list(zip(scenes, boards)))}
    _write_manifest(ctx, out)
    return out


def update_visual(ctx: Ctx, visuals: dict[int, VisualResult], result: VisualResult) -> None:
    visuals[result.scene_id] = result
    _write_manifest(ctx, visuals)
