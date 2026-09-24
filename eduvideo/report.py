"""Human-readable run report (report.md) summarising every agent decision."""
from __future__ import annotations

import json
from pathlib import Path

from .agents.base import Brief
from .agents.qa import QAResult
from .schemas import LessonPlan, SceneAudio, Script, Storyboard, Timeline
from .trace import Trace


def _event_str(e, board) -> str:
    what = e.kind
    if e.kind in ("label", "arrow", "highlight") and e.index < len(board.annotations):
        a = board.annotations[e.index]
        what = f"{e.kind} “{a.text or a.target}”"
    elif e.kind == "line" and e.index < len(board.board_lines):
        what = f"writes “{board.board_lines[e.index].text}”"
    cue = f" on “{e.cue}” (spoken {e.cue_time:.2f}s)" if e.cue_time is not None else ""
    return f"{what} @ {e.start:.2f}s{cue}"


def write_report(run_dir: Path, brief: Brief, plan: LessonPlan, script: Script, sb: Storyboard,
                 audios: dict[int, SceneAudio],
                 tl: Timeline, qa: QAResult, history: dict, trace: Trace, rate: str,
                 models: dict[str, str] | None = None, music=None) -> None:
    visuals = {}
    vj = run_dir / "images" / "visuals.json"
    if vj.exists():
        visuals = {int(k): v for k, v in json.loads(vj.read_text()).items()}

    events = [json.loads(line) for line in (run_dir / "run_log.jsonl").read_text(encoding="utf-8").splitlines() if line]
    calls = [e for e in events if e["agent"] == "llm" and e["event"] == "call"]
    total_cost = sum(e.get("cost") or 0 for e in calls)
    sessions = sum(1 for e in events if e["agent"] == "orchestrator" and e["event"] == "start")

    L: list[str] = []
    L += [f"# Run report — {brief.describe()}", ""]
    L += ["| | |", "|---|---|",
          f"| Final video | `final.mp4` — **{qa.video_s:.2f}s** (audio {qa.audio_s:.2f}s, drift {abs(qa.video_s - qa.audio_s) * 1000:.0f} ms) |",
          f"| Final QA | {'PASS' if qa.ok else 'ISSUES: ' + '; '.join(qa.problems + [f'visual {i}' for i in qa.redo_visuals])} |",
          f"| Scenes | {len(script.scenes)} |",
          f"| Captions | {len(tl.captions)} chunks, {sum(len(c.words) for c in tl.captions)} word timestamps |",
          f"| Pen actions (write / draw / label / arrow) | {len(tl.events)} total, "
          f"{sum(e.cue_time is not None for e in tl.events)} started on their spoken cue word |",
          f"| TTS rate | {rate} |",
          f"| API calls / cost | {len(calls)} calls, ${total_cost:.3f}" + (f" across {sessions} sessions (resumed)" if sessions > 1 else "") + " |",
          f"| Wall time (this session) | {trace.elapsed():.0f}s |", ""]

    if models:
        L += ["## Models", "", "| Role | Model |", "|---|---|",
              *[f"| {role} | `{m}` |" for role, m in models.items()],
              f"| narration (TTS) | `{next(iter(audios.values())).voice}` |", ""]

    L += ["## Lesson plan (Planner agent)", "", "**Objectives:**", *[f"- {o}" for o in plan.learning_objectives], "",
          f"**Visual style:** {plan.visual_style}", ""]

    L += ["## Script review loop (Writer ⇄ Reviewer)", "", "| Round | Score | Approved | Blocking issues |", "|---|---|---|---|"]
    for r in history.get("script_reviews", []):
        blocking = [i for i in r["issues"] if i["severity"] != "minor"]
        L.append(f"| {r['round']} | {r['score']}/10 | {'✅' if r['approved'] else '❌'} | "
                 + "<br>".join(f"s{i['scene_id']} [{i['category']}] {i['problem'][:110]}" for i in blocking[:6]) + " |")
    L.append("")

    L += ["## Duration control loop (Narrator → Orchestrator → Writer)", "", "| Round | Total (s) | Per-scene audio (s) |", "|---|---|---|"]
    for r in history.get("duration_rounds", []):
        L.append(f"| {r['round']} | {r['total']} | " + ", ".join(f"s{k}: {v}" for k, v in r["scenes"].items()) + " |")
    L.append("")

    L += ["## Scenes", ""]
    for sc in script.scenes:
        au = audios[sc.id]
        sl = next(s for s in tl.scenes if s.scene_id == sc.id)
        v = visuals.get(sc.id, {})
        L += [f"### {sc.id}. {sc.title} — _{plan.scenes[sc.id - 1].title_en}_", "",
              f"> {sc.narration}", "",
              f"- **Timing:** narration {sl.start:.2f}s → {sl.end:.2f}s ({au.duration:.2f}s), visual slot {sl.vis_start:.2f}s → {sl.vis_end:.2f}s",
              f"- **Audio QA:** voice `{au.voice}`, timestamps `{au.boundary_source}` (coverage {au.coverage:.0%})"
              + (f", ASR similarity **{au.asr_similarity:.2f}**" if au.asr_similarity is not None else ""),
              f"- **Layout:** {sb.scenes[sc.id - 1].layout}; drawing: _{sb.scenes[sc.id - 1].image_prompt[:160]}_",
              "- **Synced pen actions:** " + (", ".join(_event_str(e, sb.scenes[sc.id - 1])
                                                         for e in tl.events if e.scene_id == sc.id) or "—"),
              f"- **Visual:** {v.get('source', 'generated')}; attempts: "
              + (", ".join(f"#{a.get('attempt')} score {a.get('score', 'err')}{' ✅' if a.get('approved') else ''}"
                           for a in v.get("attempts", [])) or "cached"),
              ""]

    if music is not None and (music.attempts or music.path):
        L += ["## Background music (Music agent)", "",
              f"- **Prompt:** {music.prompt or '—'}",
              "- **Takes:** " + (", ".join(f"#{a.get('attempt')} " + (f"score {a['score']}/10{' ✅' if a.get('approved') else ' ❌'}"
                                                                      if 'score' in a else "error")
                                          for a in music.attempts) or "cached"),
              "- **Mix:** " + (f"bed {music.level_db:.0f} dB, ducked under every spoken word; narration ASR on the final "
                                f"mix {music.clarity.get('asr_similarity')}, balance {music.clarity.get('balance')}/10"
                                if music.path else "dropped — narration only"), ""]

    if qa.review:
        L += ["## Final QA (vision check of rendered keyframes)", "", "| Scene | Visual matches | Captions legible | Glitch | Notes |", "|---|---|---|---|---|"]
        for fc in qa.review.frames:
            L.append(f"| {fc.scene_id} | {'✅' if fc.visual_matches_narration else '❌'} | {'✅' if fc.captions_legible else '❌'} | "
                     f"{'⚠️' if fc.rendering_glitch else '—'} | {'; '.join(fc.problems)[:160]} |")
        L.append("")
    if (run_dir / "contact_sheet.jpg").exists():
        L += ["## Keyframes", "", "![keyframes](contact_sheet.jpg)", ""]

    L += ["## Agent event log", "", "```"]
    for e in events:
        if e["agent"] == "orchestrator" and e["event"] == "start" and L[-1] != "```":
            L.append("---- resumed session ----")
        if e["agent"] != "llm":
            L.append(f"[{e['t']:6.1f}s] {e['agent']:<15} {e['event']:<9} {e['message'][:400]}")
    L += ["```", ""]
    (run_dir / "report.md").write_text("\n".join(L), encoding="utf-8")
