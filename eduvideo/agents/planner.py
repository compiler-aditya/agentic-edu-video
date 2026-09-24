"""Curriculum Planner agent: turns a topic into a scene-by-scene lesson plan."""
from __future__ import annotations

from ..schemas import LessonPlan
from .base import Ctx

SYSTEM = """You are an experienced Indian school teacher and instructional designer who plans
short explainer videos aligned to the NCERT/CBSE syllabus. You design for the stated class
level: concrete, visual, one idea per scene, and scientifically correct."""


def plan_lesson(ctx: Ctx) -> LessonPlan:
    b, s = ctx.brief, ctx.settings
    speech_budget = s.target_seconds - s.lead_in - s.tail
    prompt = f"""Plan a {s.target_seconds:.0f}-second educational video.

Topic: {b.describe()}

Requirements:
- 4 or 5 scenes. Scene 1 hooks the student with an everyday observation; the last scene is a
  one-line recap. The middle scenes explain the core concept step by step.
- Narration will be spoken in {b.lang.name}. Total narration across all scenes must be about
  {speech_budget:.0f} seconds, so target_seconds must sum to roughly that (min 6s per scene).
- Scene titles in {b.lang.name} ({b.lang.native} script), 2-5 words each.
- The video is a whiteboard explainer: each scene's drawing is sketched on screen and its parts
  are labelled as the narrator mentions them. `visual_idea` describes ONE clear textbook-style
  diagram per scene with distinct, labelable parts (no text in the drawing itself). The last
  scene can be a board summary (e.g. the word equation) with a small drawing.
- `visual_style`: one sentence of art direction shared by all drawings (clean flat vector,
  bold dark outlines, pure white background).
- Stay within the Class {b.grade} syllabus; do not introduce concepts beyond that level."""
    ctx.trace.log("planner", "start", f"planning scenes for {b.describe()}")
    plan = ctx.llm.chat_json(
        s.planner_model,
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        LessonPlan,
        purpose="planner.plan",
        temperature=0.6,
    )
    plan = _normalise(plan, speech_budget)
    ctx.trace.log("planner", "done", f"{len(plan.scenes)} scenes: " + " | ".join(sc.title_en for sc in plan.scenes),
                  objectives=plan.learning_objectives)
    return plan


def _normalise(plan: LessonPlan, speech_budget: float) -> LessonPlan:
    """Deterministic guardrails on the plan: scene count, ids and time budget."""
    scenes = plan.scenes[:6]
    if len(scenes) < 3:
        raise ValueError(f"planner returned only {len(scenes)} scenes; need at least 3")
    total = sum(max(sc.target_seconds, 1.0) for sc in scenes)
    for i, sc in enumerate(scenes, 1):
        sc.id = i
        sc.target_seconds = round(max(sc.target_seconds, 1.0) / total * speech_budget, 1)
    plan.scenes = scenes
    return plan
