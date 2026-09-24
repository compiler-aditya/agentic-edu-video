"""Storyboard (visual director) agent: decides what gets drawn and written, and when.

For every scene it produces a whiteboard storyboard: the parts (elements) the drawing must
contain, the image prompt, and the annotations — labels, arrows, highlights and board
lines — each tied to a *cue*: words copied from the narration. The sync agent later turns
cues into timestamps, so every label/arrow appears exactly when it is spoken.

A deterministic validator checks the storyboard (cues present verbatim, targets exist,
text fits, counts per layout); failures go back to the agent with the issue list.
"""
from __future__ import annotations

import json
import re

from ..schemas import LessonPlan, Script, Storyboard
from ..textutil import norm_text, script_ratio
from .base import Ctx

SYSTEM = """You are the visual director of whiteboard-style explainer videos for Indian school students
(think of a teacher drawing a textbook diagram while explaining it). You plan what is drawn and
exactly when each label, arrow and written line appears, synchronised to the narration."""

EDGES = {"left", "right", "top", "bottom"}
DRAW_WORDS = 5       # the first words of a diagram scene are spoken while the title is written and the drawing sketched
BOARD_WORDS = 2      # board scenes: only the title is written first
MIN_CUE_GAP = 3      # words between consecutive cues, so one pen can finish each action
MAX_LABEL_CHARS = 30
MAX_LINE_CHARS = 42


def _rules(ctx: Ctx) -> str:
    lang = ctx.brief.lang
    return f"""Rules:
- One entry per scene, same ids. `layout`:
  * "diagram" (most scenes): one large textbook diagram; 2-5 annotations point out its parts.
  * "board" (use for the recap/summary scene, optional elsewhere): 1-4 short lines written on the
    board (e.g. a word equation "कार्बन डाइऑक्साइड + पानी → ग्लूकोज़ + ऑक्सीजन") plus a small drawing.
- `elements`: the concrete parts the drawing must show, each with a short snake_case id and an
  English visual description. Every annotation target must be one of these ids.
- `image_prompt` (English): one clear, scientifically accurate textbook diagram that contains all
  elements, drawn large and well separated so each part can be pointed at. No text in the image.
- annotations:
  * label: `text` is 1-3 {lang.name} words naming the part (max {MAX_LABEL_CHARS} characters), may add the
    English term after " • " if it is a science term (e.g. "रंध्र • Stomata").
  * arrow: shows movement or flow (light reaching the leaf, water rising, gas entering/leaving).
    `source` is an element id or an edge (left/right/top/bottom) the flow comes from; `target` is
    where it goes; optional short `text` tag.
  * highlight: circles a part the narrator emphasises.
  * `color`: blue=water, orange=sunlight/energy, gray=carbon dioxide, green=oxygen/plant, red=warning,
    purple=other.
- `cue`: 1-3 consecutive words copied EXACTLY (same spelling and matras) from THIS scene's narration,
  at the moment the annotation should appear. Order annotations by cue position.
  TIMING (one pen does everything): the first {DRAW_WORDS} words of a diagram scene (first {BOARD_WORDS} of a board
  scene) are spoken while the title is written and the drawing is sketched, so no cue may start there;
  and consecutive cues must be at least {MIN_CUE_GAP} words apart.
- board_lines: max {MAX_LINE_CHARS} characters each, in {lang.name} (symbols + and → allowed), each with a cue."""


def draft_storyboard(ctx: Ctx, plan: LessonPlan, script: Script) -> Storyboard:
    scenes = [{"id": sc.id, "title": sc.title, "narration": sc.narration,
               "visual_idea": plan.scenes[sc.id - 1].visual_idea} for sc in script.scenes]
    prompt = f"""Storyboard this whiteboard video: {ctx.brief.describe()}
Art direction for all drawings: {plan.visual_style}

Scenes:
{json.dumps(scenes, ensure_ascii=False, indent=1)}

{_rules(ctx)}"""
    ctx.trace.log("storyboard", "draft", f"planning drawings and synced annotations for {len(scenes)} scenes")
    return ctx.llm.chat_json(ctx.settings.writer_model,
                             [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                             Storyboard, purpose="storyboard.draft", temperature=0.5)


def repair_storyboard(ctx: Ctx, script: Script, sb: Storyboard, issues: list[str], keep_visuals: bool) -> Storyboard:
    keep = ("Keep every scene's layout, elements and image_prompt exactly as they are (the drawings are "
            "already made); only fix annotations, cues and board lines.") if keep_visuals else ""
    prompt = f"""Fix this storyboard. Current narration per scene:
{json.dumps([{"id": sc.id, "narration": sc.narration} for sc in script.scenes], ensure_ascii=False, indent=1)}

Current storyboard:
{sb.model_dump_json(indent=1)}

Problems to fix:
{json.dumps(issues, ensure_ascii=False, indent=1)}

{keep}
{_rules(ctx)}

Return the full corrected storyboard."""
    ctx.trace.log("storyboard", "repair", f"fixing {len(issues)} issue(s)" + (" (visuals locked)" if keep_visuals else ""))
    fixed = ctx.llm.chat_json(ctx.settings.writer_model,
                              [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                              Storyboard, purpose="storyboard.repair", temperature=0.3)
    if keep_visuals:
        for new, old in zip(fixed.scenes, sb.scenes):
            new.layout, new.elements, new.image_prompt = old.layout, old.elements, old.image_prompt
    return fixed


def validate_storyboard(ctx: Ctx, script: Script, sb: Storyboard) -> list[str]:
    script_name = ctx.brief.lang.script
    issues: list[str] = []
    if len(sb.scenes) != len(script.scenes):
        return [f"storyboard has {len(sb.scenes)} scenes, script has {len(script.scenes)}"]
    for i, (board, sc) in enumerate(zip(sb.scenes, script.scenes), 1):
        board.id = i
        where = f"scene {i}"
        narr = norm_text(sc.narration)
        ids = [e.id for e in board.elements]
        if len(set(ids)) != len(ids):
            issues.append(f"{where}: duplicate element ids {ids}")
        if not all(re.fullmatch(r"[a-z][a-z0-9_]*", x) for x in ids):
            issues.append(f"{where}: element ids must be snake_case: {ids}")
        if not board.image_prompt or script_ratio(board.image_prompt, "Latin") < 0.9:
            issues.append(f"{where}: image_prompt must be English")
        if board.layout == "diagram":
            if not 2 <= len(board.elements) <= 6:
                issues.append(f"{where}: diagram needs 2-6 elements, has {len(board.elements)}")
            if not 2 <= len(board.annotations) <= 5:
                issues.append(f"{where}: diagram needs 2-5 annotations, has {len(board.annotations)}")
            if board.board_lines:
                issues.append(f"{where}: diagram scenes must not have board_lines")
        else:
            if not 1 <= len(board.board_lines) <= 4:
                issues.append(f"{where}: board needs 1-4 board_lines, has {len(board.board_lines)}")
            if not 1 <= len(board.elements) <= 3:
                issues.append(f"{where}: board needs 1-3 elements for its small drawing")
            if board.annotations:
                issues.append(f"{where}: board scenes must not have annotations")
        for a in board.annotations:
            tag = f"{where} {a.kind} '{a.text or a.target}'"
            if a.target not in ids:
                issues.append(f"{tag}: target '{a.target}' is not an element id {ids}")
            if a.kind == "arrow" and a.source not in ids and a.source not in EDGES:
                issues.append(f"{tag}: arrow source '{a.source}' must be an element id or one of {sorted(EDGES)}")
            if a.kind == "label" and not a.text.strip():
                issues.append(f"{tag}: label needs text")
            if a.text and len(a.text) > MAX_LABEL_CHARS + 16:
                issues.append(f"{tag}: text too long ({len(a.text)} chars)")
            if a.text and script_name != "Latin" and script_ratio(a.text.split("•")[0], script_name) < 0.8:
                issues.append(f"{tag}: text must start with the {ctx.brief.lang.name} term")
            if not norm_text(a.cue) or norm_text(a.cue) not in narr:
                issues.append(f"{tag}: cue '{a.cue}' is not copied exactly from the narration")
        for ln in board.board_lines:
            if len(ln.text) > MAX_LINE_CHARS + 8:
                issues.append(f"{where} line '{ln.text}': too long ({len(ln.text)} chars)")
            if not norm_text(ln.cue) or norm_text(ln.cue) not in narr:
                issues.append(f"{where} line '{ln.text}': cue '{ln.cue}' is not copied exactly from the narration")
        issues += _timing_issues(where, board, sc.narration)
    return issues


def cue_positions(cue: str, narration: str) -> list[int]:
    """Word indices where `cue` starts in the narration (punctuation-insensitive)."""
    toks = [norm_text(t) for t in narration.split()]
    parts = [norm_text(t) for t in cue.split() if norm_text(t)]
    if not parts:
        return []
    return [i for i in range(len(toks) - len(parts) + 1) if toks[i:i + len(parts)] == parts]


def _timing_issues(where: str, board, narration: str) -> list[str]:
    """Can one pen actually do these actions on time? (cues after the drawing phase, spaced apart)"""
    first_ok = DRAW_WORDS if board.layout == "diagram" else BOARD_WORDS
    items = [(a.cue, a.text or a.target) for a in board.annotations] + [(ln.cue, ln.text) for ln in board.board_lines]
    items.sort(key=lambda it: min(cue_positions(it[0], narration) or [10**6]))  # the pen works in spoken order
    issues, placed = [], []
    last = -MIN_CUE_GAP
    for cue, name in items:
        pos = [i for i in cue_positions(cue, narration) if i >= first_ok]
        if not cue_positions(cue, narration):
            continue  # reported above as "not copied exactly"
        if not pos:
            issues.append(f"{where} '{name}': cue '{cue}' is within the first {first_ok} words (the pen is still "
                          "writing the title / sketching) — pick words spoken later")
            continue
        nxt = next((i for i in pos if i >= last + MIN_CUE_GAP), None)
        if nxt is None:
            issues.append(f"{where} '{name}': cue '{cue}' is less than {MIN_CUE_GAP} words after the previous cue — "
                          "spread the cues out or drop one")
            continue
        placed.append(nxt)
        last = nxt
    return issues


def storyboard_loop(ctx: Ctx, plan: LessonPlan, script: Script, sb: Storyboard | None = None,
                    keep_visuals: bool = False) -> Storyboard:
    sb = sb or draft_storyboard(ctx, plan, script)
    for rnd in range(1, ctx.settings.max_script_rounds + 1):
        issues = validate_storyboard(ctx, script, sb)
        n_ann = sum(len(b.annotations) + len(b.board_lines) for b in sb.scenes)
        if not issues:
            ctx.trace.log("storyboard", "verdict", f"round {rnd}: VALID — "
                          + ", ".join(f"s{b.id}:{b.layout}/{len(b.annotations) + len(b.board_lines)}" for b in sb.scenes)
                          + f" ({n_ann} synced annotations)")
            return sb
        for i in issues[:8]:
            ctx.trace.log("storyboard", "rule", i)
        ctx.trace.log("storyboard", "verdict", f"round {rnd}: INVALID — {len(issues)} issue(s)")
        if rnd == ctx.settings.max_script_rounds:
            break
        sb = repair_storyboard(ctx, script, sb, issues, keep_visuals)
    # Drop whatever is still invalid rather than failing the run.
    _prune(ctx, script, sb)
    return sb


def _prune(ctx: Ctx, script: Script, sb: Storyboard) -> None:
    for board, sc in zip(sb.scenes, script.scenes):
        narr, ids = norm_text(sc.narration), {e.id for e in board.elements}
        keep = [a for a in board.annotations if norm_text(a.cue) and norm_text(a.cue) in narr and a.target in ids
                and (a.kind != "arrow" or a.source in ids or a.source in EDGES) and (a.kind != "label" or a.text)]
        lines = [ln for ln in board.board_lines if norm_text(ln.cue) and norm_text(ln.cue) in narr]
        dropped = len(board.annotations) - len(keep) + len(board.board_lines) - len(lines)
        if dropped:
            ctx.trace.log("storyboard", "prune", f"scene {board.id}: dropped {dropped} invalid annotation(s)")
        board.annotations, board.board_lines = keep[:5], lines[:4]
