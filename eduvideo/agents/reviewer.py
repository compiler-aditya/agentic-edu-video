"""Script Reviewer agent: deterministic guardrails + an independent LLM judge.

Deterministic checks catch what code can verify exactly (language/script purity,
word budgets, symbols that TTS would mispronounce).
The LLM judge (a different model family from the writer) checks what code cannot:
factual accuracy, grade-appropriateness and coherence.
"""
from __future__ import annotations

import re

from ..schemas import LessonPlan, ReviewIssue, Script, ScriptReview
from ..textutil import script_ratio, word_count
from .base import Ctx

SYSTEM = """You are a strict but fair reviewer of educational video scripts for Indian schools (NCERT/CBSE).
You check scientific accuracy first, then age-appropriateness, clarity, language quality and flow.
Be specific and actionable. Only report issues you are confident about.

Severity guide:
- critical: a factual error or a statement that would mislead students.
- major: a learning objective is not covered at all; ungrammatical sentences; vocabulary or
  concepts clearly above the class level; an image prompt that contradicts its narration.
- minor: everything else — which words are tagged as key terms, stylistic preferences, and
  simplifications that NCERT itself uses at this class level.
Approve when a good teacher would happily show this video in class, even if minor issues remain."""

_TTS_UNSAFE = re.compile(r"[A-Za-z₀-₉%+=→<>&@#]")


def deterministic_checks(ctx: Ctx, plan: LessonPlan, script: Script, wps: float,
                         check_length: bool = True) -> list[ReviewIssue]:
    lang = ctx.brief.lang
    issues: list[ReviewIssue] = []
    for sc, ps in zip(script.scenes, plan.scenes):
        ratio = script_ratio(sc.narration, lang.script)
        if ratio < 0.97:
            issues.append(ReviewIssue(scene_id=sc.id, severity="major", category="language",
                                      problem=f"narration is only {ratio:.0%} {lang.script} script",
                                      fix=f"write the narration entirely in {lang.script} script"))
        if "…" in sc.narration or "..." in sc.narration:
            issues.append(ReviewIssue(scene_id=sc.id, severity="major", category="language",
                                      problem="narration uses an ellipsis, which the voice turns into a long pause",
                                      fix="use a comma or full stop instead"))
        if lang.script != "Latin" and _TTS_UNSAFE.search(sc.narration):
            bad = sorted(set(_TTS_UNSAFE.findall(sc.narration)))
            issues.append(ReviewIssue(scene_id=sc.id, severity="major", category="language",
                                      problem=f"narration contains Latin letters or symbols {bad} that the voice will misread",
                                      fix=f"spell these out in {lang.name} words"))
        target = max(8, round(ps.target_seconds * wps))
        n = word_count(sc.narration)
        if check_length and not 0.6 * target <= n <= 1.45 * target:
            issues.append(ReviewIssue(scene_id=sc.id, severity="major", category="length",
                                      problem=f"{n} words vs target {target}",
                                      fix=f"rewrite to about {target} words"))
    return issues


def review_script(ctx: Ctx, plan: LessonPlan, script: Script, wps: float, round_no: int | str,
                  check_length: bool = True, previous: list[ReviewIssue] | None = None
                  ) -> tuple[bool, ScriptReview, list[ReviewIssue]]:
    det = deterministic_checks(ctx, plan, script, wps, check_length)
    for i in det:
        ctx.trace.log("script_reviewer", "rule", f"scene {i.scene_id}: {i.problem}", severity=i.severity)

    prev_block = ""
    if previous:
        prev = [f"- scene {i.scene_id} [{i.severity}] {i.problem}" for i in previous if i.severity != "minor"]
        if prev:
            prev_block = ("\nIssues raised in the previous review round (verify each is now fixed; judge the script "
                          "on the same standard rather than raising the bar):\n" + "\n".join(prev) + "\n")
    prompt = f"""Review this script for a 30-60 second video: {ctx.brief.describe()}

Learning objectives: {plan.learning_objectives}

Script (JSON):
{script.model_dump_json(indent=1)}

Check each scene for:
1. factual/scientific errors or misleading simplifications (critical if wrong),
2. suitability for Class {ctx.brief.grade} (vocabulary, concept level),
3. natural, grammatical, CONVERSATIONAL {ctx.brief.lang.name}: it should sound like a teacher talking with
   a student (questions, connecting words, scenes that lead into each other), not a list of
   disconnected textbook sentences — flag that as major,
4. flow between scenes and whether the objectives are covered.
{prev_block}
Set approved=true only if there are no major or critical issues. Score 1-10."""
    review = ctx.llm.chat_json(ctx.settings.reviewer_model,
                               [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                               ScriptReview, purpose=f"script_reviewer.{round_no}", temperature=0.2)
    for i in review.issues:
        ctx.trace.log("script_reviewer", "issue", f"[{i.severity}/{i.category}] scene {i.scene_id}: {i.problem}")

    blocking = [i for i in det + review.issues if i.severity in ("major", "critical")]
    approved = review.approved and not blocking
    ctx.trace.log("script_reviewer", "verdict",
                  f"{round_no}: {'APPROVED' if approved else 'REJECTED'} score={review.score}/10, "
                  f"{len(blocking)} blocking issue(s) — {review.summary[:140]}",
                  score=review.score, approved=approved)
    return approved, review, det + review.issues
