"""Script Writer agent: narration, on-screen titles, key terms and image prompts per scene.

It is called in three modes by the orchestrator:
  * draft   – first version from the lesson plan
  * revise  – fix the issues raised by the Script Reviewer
  * retime  – shorten/lengthen specific scenes to hit the measured audio duration
"""
from __future__ import annotations

import json

from ..schemas import LessonPlan, ReviewIssue, Script
from .base import Ctx

SYSTEM = """You are a warm, lively Indian school teacher explaining a topic to one curious student, and
your words will be spoken by an expressive voice. It must sound like a real conversation, not a
textbook being read out: everyday spoken language, examples from Indian life, no bullet points,
markdown, emojis, brackets or abbreviations the voice would stumble on."""


def _rules(ctx: Ctx) -> str:
    lang = ctx.brief.lang
    return f"""Writing rules:
- `narration` is spoken {lang.name} written ONLY in {lang.script} script. Write scientific terms the
  way Indian teachers say them, transliterated into {lang.script} (e.g. क्लोरोफिल, ग्लूकोज़) — never
  Latin letters, chemical formulas, digits-with-units or symbols in the narration.
- Grade level: Class {ctx.brief.grade}. Accurate, NCERT-consistent facts only.
- CONVERSATIONAL, NOT SENTENCE-BY-SENTENCE: talk to the student (आप / हम / चलिए), ask a question now
  and then and answer it, and join sentences with connecting words (तो, अब, देखिए, यानी, इसलिए, पर,
  क्योंकि) so each thought leads into the next. Vary sentence length; avoid a string of short
  statements that each stand alone, and avoid reading out definitions or lists.
- Each scene picks up from the previous one with a natural bridge (e.g. "अब सवाल यह है कि…" /
  "तो पत्ती यह करती कैसे है?") — no repeated greetings, no "in this video".
- Use plain punctuation only (, ? ! ।) — no ellipsis "…" or "...", it creates long awkward pauses.
- The video is a whiteboard explainer: while the narrator talks, the scene's drawing is sketched
  and its parts are labelled as they are mentioned. So name the concrete parts/things the student
  should look at (e.g. पत्ती, जड़ें, सूरज) in the order you want them pointed out.
- `title`: the on-screen title in {lang.name}, 2-5 words."""


def draft_script(ctx: Ctx, plan: LessonPlan, wps: float) -> Script:
    scenes = [
        {"id": sc.id, "title": sc.title, "learning_goal": sc.learning_goal, "visual_idea": sc.visual_idea,
         "target_words": max(8, round(sc.target_seconds * wps))}
        for sc in plan.scenes
    ]
    prompt = f"""Write the script for this video: {ctx.brief.describe()}

Learning objectives the video as a whole must cover (name every product/input/term they mention):
{json.dumps(plan.learning_objectives, ensure_ascii=False)}

Scenes (hit each scene's target_words within ±15%; word = space-separated token):
{json.dumps(scenes, ensure_ascii=False, indent=1)}

{_rules(ctx)}

Return one entry per scene with the same ids."""
    ctx.trace.log("writer", "draft", f"writing {len(scenes)} scenes (~{sum(s['target_words'] for s in scenes)} words @ {wps:.2f} w/s)")
    script = ctx.llm.chat_json(ctx.settings.writer_model,
                               [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                               Script, purpose="writer.draft", temperature=0.7)
    return _align_ids(script, plan)


def revise_script(ctx: Ctx, plan: LessonPlan, script: Script, issues: list[ReviewIssue], wps: float) -> Script:
    targets = {sc.id: max(8, round(sc.target_seconds * wps)) for sc in plan.scenes}
    prompt = f"""Revise this script for: {ctx.brief.describe()}

Learning objectives the video must cover: {json.dumps(plan.learning_objectives, ensure_ascii=False)}

Current script:
{script.model_dump_json(indent=1)}

Reviewer issues to fix (fix ALL of them without introducing new problems; keep everything else
unchanged, keep each scene's length close to its target):
{json.dumps([i.model_dump() for i in issues], ensure_ascii=False, indent=1)}

Target words per scene: {json.dumps(targets)}

{_rules(ctx)}

Return the full revised script (all scenes, same ids)."""
    ctx.trace.log("writer", "revise", f"fixing {len(issues)} issue(s) in scenes "
                  f"{sorted({i.scene_id for i in issues if i.scene_id})}")
    revised = ctx.llm.chat_json(ctx.settings.writer_model,
                                [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                                Script, purpose="writer.revise", temperature=0.5)
    return _align_ids(revised, plan)


def retime_script(ctx: Ctx, plan: LessonPlan, script: Script, word_targets: dict[int, int],
                  measured: dict[int, float], direction: str) -> Script:
    current = {sc.id: len(sc.narration.split()) for sc in script.scenes}
    changes = {sid: {"current_words": current[sid], "measured_seconds": round(measured[sid], 1),
                     "target_words": word_targets[sid]} for sid in word_targets}
    prompt = f"""The narration audio is too {'long' if direction == 'shorten' else 'short'} for a
{ctx.settings.min_seconds:.0f}-{ctx.settings.max_seconds:.0f} second video. {direction.capitalize()} the narration of these
scenes to the target word counts (±10%), keeping the facts and the flow:
{json.dumps(changes, indent=1)}

Current script:
{script.model_dump_json(indent=1)}

{_rules(ctx)}

Do not change scenes not listed above. Keep the titles, and keep mentioning the same concrete
parts of the drawing. Return the full script."""
    ctx.trace.log("writer", "retime", f"{direction} scenes {sorted(word_targets)} -> "
                  + ", ".join(f"s{k}:{current[k]}→{v}w" for k, v in word_targets.items()))
    revised = ctx.llm.chat_json(ctx.settings.writer_model,
                                [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                                Script, purpose="writer.retime", temperature=0.4)
    return _align_ids(revised, plan)


def _align_ids(script: Script, plan: LessonPlan) -> Script:
    if len(script.scenes) != len(plan.scenes):
        raise ValueError(f"writer returned {len(script.scenes)} scenes, plan has {len(plan.scenes)}")
    for i, sc in enumerate(script.scenes, 1):
        sc.id = i
        sc.narration = " ".join(sc.narration.replace("*", "").split())
    return script
