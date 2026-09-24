"""Orchestrator: runs the agent graph, owns every feedback loop and retry budget.

    PLAN ─► WRITE ─► REVIEW ──(rejected: issues)──► WRITE (revise)             [loop ≤ 3]
                       │ approved
                       ▼
                  STORYBOARD ──(invalid cues/targets)──► STORYBOARD (repair)     [loop ≤ 3]
            ┌──────────┴──────────┐
            ▼                     ▼
   VISUALS ⟲ critic ⟲ grounder  NARRATE ⟲ audio QA                               [parallel]
            │                     │
            │               DURATION CHECK ──(outside 30-60s)──► WRITE (retime) ► REVIEW ► NARRATE
            │                     │                              ► STORYBOARD (re-cue, drawings locked)
            └──────────┬──────────┘
                       ▼
                     SYNC  (calibrated word timestamps → captions + cue-timed pen actions)
                       ▼
                     MUSIC ⟲ listen check → duck under every word → clarity (ASR on the mix)
                       ▼
                 WHITEBOARD RENDER
                       ▼
                   FINAL QA ──(drawing wrong)──► VISUALS (redraw) ─┐
                       │    ──(label points wrong)──► GROUNDER ────┴► SYNC ► RENDER   [loop ≤ 1]
                       ▼
                     DONE  (final.mp4, captions.srt, timeline.json, storyboard.json, report.md)
"""
from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .agents.base import Brief, Ctx
from .agents.music import MusicResult, add_music
from .agents.narrator import narrate_all
from .agents.planner import plan_lesson
from .agents.qa import QAResult, final_qa
from .agents.reviewer import deterministic_checks, review_script
from .agents.sync import build_timeline
from .agents.storyboard import storyboard_loop, validate_storyboard
from .agents.visual import VisualResult, ground, make_all_visuals, make_visual, update_visual
from .agents.writer import draft_script, retime_script, revise_script
from .config import Settings
from .llm import LLMError, OpenRouter
from .render.whiteboard import contact_sheet, render_video
from .report import write_report
from .schemas import LessonPlan, SceneAudio, Script, Storyboard, Timeline
from .trace import Trace


class Orchestrator:
    def __init__(self, settings: Settings, brief: Brief, run_dir: Path):
        self.s = settings
        self.run_dir = run_dir
        self.trace = Trace(run_dir)
        self.ctx = Ctx(settings, OpenRouter(settings, self.trace), self.trace, run_dir, brief)
        hist = run_dir / "history.json"
        self.history: dict[str, list] = (json.loads(hist.read_text()) if hist.exists()
                                         else {"script_reviews": [], "duration_rounds": []})

    # --------------------------------------------------------------- helpers
    def _save(self, name: str, obj) -> None:
        (self.run_dir / name).write_text(obj.model_dump_json(indent=1), encoding="utf-8")

    def _load(self, name: str, model):
        p = self.run_dir / name
        return model.model_validate_json(p.read_text(encoding="utf-8")) if p.exists() else None

    def _gap(self, audios: dict[int, SceneAudio]) -> float:
        # continuous ElevenLabs takes already contain the pauses between scenes
        return 0.0 if audios and all(a.engine == "elevenlabs" for a in audios.values()) else self.s.scene_gap

    def _total_seconds(self, audios: dict[int, SceneAudio]) -> float:
        return self.s.lead_in + sum(a.duration for a in audios.values()) + self._gap(audios) * (len(audios) - 1) + self.s.tail

    # ---------------------------------------------------------------- stages
    def _script_loop(self, plan: LessonPlan, wps: float) -> Script:
        script = draft_script(self.ctx, plan, wps)
        best: tuple[int, Script] | None = None
        issues: list = []
        for rnd in range(1, self.s.max_script_rounds + 1):
            self._save(f"script_round{rnd}.json", script)
            approved, review, issues = review_script(self.ctx, plan, script, wps, rnd, previous=issues)
            self.history["script_reviews"].append({"round": rnd, "approved": approved, "score": review.score,
                                                   "summary": review.summary,
                                                   "issues": [i.model_dump() for i in issues]})
            blocking = [i for i in issues if i.severity != "minor"]
            if best is None or (review.score - len(blocking)) > best[0]:
                best = (review.score - len(blocking), script)
            if approved:
                return script
            if rnd == self.s.max_script_rounds:
                break
            script = revise_script(self.ctx, plan, script, issues, wps)
        assert best is not None
        self.trace.log("orchestrator", "fallback", "script review budget exhausted; continuing with the best-scored version")
        return best[1]

    def _duration_loop(self, plan: LessonPlan, script: Script, audios: dict[int, SceneAudio]):
        lo, hi = self.s.min_seconds + 1.5, self.s.max_seconds - 1.5
        rate = "+0%"
        for rnd in range(1, self.s.max_duration_rounds + 1):
            total = self._total_seconds(audios)
            self.history["duration_rounds"].append({"round": rnd, "total": round(total, 2),
                                                    "scenes": {k: round(v.duration, 2) for k, v in audios.items()}})
            if lo <= total <= hi:
                self.trace.log("orchestrator", "duration", f"round {rnd}: {total:.1f}s is inside {lo:.1f}-{hi:.1f}s ✓")
                return script, audios, rate
            fixed = self.s.lead_in + self.s.tail + self._gap(audios) * (len(audios) - 1)
            speech = sum(a.duration for a in audios.values())
            scale = (self.s.target_seconds - fixed) / speech
            direction = "shorten" if total > hi else "lengthen"
            targets = {}
            for sc in script.scenes:
                n = len(sc.narration.split())
                t = max(6, round(n * scale))
                if abs(t - n) >= 2:
                    targets[sc.id] = t
            self.trace.log("orchestrator", "duration", f"round {rnd}: {total:.1f}s outside {lo:.1f}-{hi:.1f}s → "
                           f"asking writer to {direction} {len(targets)} scene(s) by ×{scale:.2f}")
            measured = {k: v.duration for k, v in audios.items()}
            candidate = retime_script(self.ctx, plan, script, targets, measured, direction)
            wps = sum(len(a.words) for a in audios.values()) / speech
            approved, review, issues = review_script(self.ctx, plan, candidate, wps, round_no=f"retime{rnd}",
                                                     check_length=False)
            if not approved:
                candidate = revise_script(self.ctx, plan, candidate, [i for i in issues if i.severity != "minor"], wps)
                det = deterministic_checks(self.ctx, plan, candidate, wps, check_length=False)
                if det:
                    self.trace.log("orchestrator", "warn", f"retimed script still has {len(det)} rule issue(s); keeping it")
            script = candidate
            self._save("script.json", script)
            audios = narrate_all(self.ctx, script.scenes, rate, cache=audios)

        # Last resort: nudge the speaking rate (bounded to keep speech natural).
        total = self._total_seconds(audios)
        if not lo <= total <= hi:
            fixed = self.s.lead_in + self.s.tail + self._gap(audios) * (len(audios) - 1)
            speech = total - fixed
            pct = max(-15, min(15, round((speech / (self.s.target_seconds - fixed) - 1) * 100)))
            rate = f"{pct:+d}%"
            self.trace.log("orchestrator", "duration", f"still {total:.1f}s after rewrites → TTS rate {rate}")
            audios = narrate_all(self.ctx, script.scenes, rate, cache=audios)
        return script, audios, rate

    def _soundtrack(self, script: Script, tl: Timeline) -> None:
        """Mix the background music under the narration (updates tl.audio_path)."""
        self.music = MusicResult(None)
        if not (self.s.music and self.s.elevenlabs_key):
            return
        self.music = add_music(self.ctx, script, tl)
        (self.run_dir / "timeline.json").write_text(tl.model_dump_json(indent=1))

    def _keyframe_times(self, tl: Timeline) -> dict[int, float]:
        """One frame per scene after all of its animations have finished (everything visible)."""
        times = {}
        for sl in tl.scenes:
            last = max((e.end for e in tl.events if e.scene_id == sl.scene_id), default=sl.start)
            times[sl.scene_id] = round(min(max(last + 0.3, (sl.start + sl.end) / 2), sl.vis_end - 0.3), 2)
        return times

    def _render(self, tl: Timeline, plan: LessonPlan, sb: Storyboard,
                visuals: dict[int, VisualResult]) -> tuple[Path, dict[int, Path]]:
        b = self.ctx.brief
        out = self.run_dir / "final.mp4"
        header = f"{b.lang.grade_word} {b.grade}  •  {plan.subject_native}  •  {plan.topic_native}"
        self.trace.log("renderer", "start", f"rendering whiteboard video {tl.duration:.2f}s @ {tl.fps}fps "
                       f"{tl.width}x{tl.height}")
        kf = render_video(tl, plan, sb, {k: v.grounding for k, v in visuals.items()}, self.run_dir, out,
                          b.lang.script, header, self._keyframe_times(tl))
        contact_sheet(kf, self.run_dir / "contact_sheet.jpg")
        self.trace.log("renderer", "done", f"wrote {out.name} ({out.stat().st_size / 1e6:.1f} MB)")
        return out, kf

    # ------------------------------------------------------------------- run
    def run(self) -> Path:
        ctx, b = self.ctx, self.ctx.brief
        self.trace.log("orchestrator", "start", b.describe(), run_dir=str(self.run_dir))
        self.trace.log("orchestrator", "models", f"preset '{self.s.models}': "
                       + ", ".join(f"{k}={v}" for k, v in self.s.model_table().items()), models=self.s.model_table())
        tts = (f"ElevenLabs {self.s.el_model} voice {b.lang.el_voice} (whole-lesson take)" if self.s.use_elevenlabs(b.lang)
               else f"edge-tts {b.lang.voice}")
        self.trace.log("orchestrator", "tts", f"narration engine: {tts}")

        plan = self._load("plan.json", LessonPlan)
        if plan:
            self.trace.log("orchestrator", "resume", "loaded plan.json")
        else:
            plan = plan_lesson(ctx)
            self._save("plan.json", plan)

        wps = self.s.speaking_rate(b.lang)
        script = self._load("script.json", Script)
        if script:
            self.trace.log("orchestrator", "resume", "loaded approved script.json")
        else:
            script = self._script_loop(plan, wps)
            self._save("script.json", script)

        sb = self._load("storyboard.json", Storyboard)
        if sb and not validate_storyboard(ctx, script, sb):
            self.trace.log("orchestrator", "resume", "loaded valid storyboard.json")
        else:
            # a stale storyboard from a previous session is repaired with its drawings locked
            sb = storyboard_loop(ctx, plan, script, sb, keep_visuals=sb is not None)
            self._save("storyboard.json", sb)

        cached_audio: dict[int, SceneAudio] = {}
        if (self.run_dir / "audio" / "audio.json").exists():
            raw = json.loads((self.run_dir / "audio" / "audio.json").read_text())
            cached_audio = {int(k): SceneAudio.model_validate(v) for k, v in raw.items()}

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_vis = pool.submit(make_all_visuals, ctx, plan, script.scenes, sb.scenes)
            self.trace.log("orchestrator", "fanout", "visual agent and narrator agent running in parallel")
            audios = narrate_all(ctx, script.scenes, cache=cached_audio)
            new_script, audios, rate = self._duration_loop(plan, script, audios)
            if new_script is not script:
                # narration changed: re-check cues against the new wording (drawings stay as they are)
                sb = storyboard_loop(ctx, plan, new_script, sb, keep_visuals=True)
                self._save("storyboard.json", sb)
            script = new_script
            self._save("script.json", script)
            visuals: dict[int, VisualResult] = fut_vis.result()
        (self.run_dir / "history.json").write_text(json.dumps(self.history, ensure_ascii=False, indent=1))
        images = {sc.id: f"images/scene_{sc.id}.png" for sc in script.scenes}
        has_image = {k: v.source != "fallback" for k, v in visuals.items()}

        tl = build_timeline(ctx, script, sb, audios, images, has_image)
        self._soundtrack(script, tl)
        video, keyframes = self._render(tl, plan, sb, visuals)

        qa: QAResult = final_qa(ctx, video, tl, script, sb, keyframes)
        for _ in range(self.s.max_final_qa_rounds):
            if qa.ok or not (qa.redo_visuals or qa.regrounds):
                break
            feedback = {fc.scene_id: fc.problems for fc in (qa.review.frames if qa.review else [])}
            for sid in qa.redo_visuals:
                self.trace.log("orchestrator", "repair", f"scene {sid}: drawing does not match narration → redraw")
                sc, bd = script.scenes[sid - 1], sb.scenes[sid - 1]
                update_visual(ctx, visuals, make_visual(ctx, plan, sc, bd, feedback=feedback.get(sid)))
            for sid in qa.regrounds:
                if sid in qa.redo_visuals:
                    continue
                self.trace.log("orchestrator", "repair", f"scene {sid}: labels point at wrong parts → re-locate")
                try:
                    visuals[sid].grounding = ground(ctx, visuals[sid].path, sb.scenes[sid - 1], feedback.get(sid))
                    update_visual(ctx, visuals, visuals[sid])
                except LLMError as e:
                    self.trace.log("grounder", "error", f"scene {sid}: {e}")
            has_image = {k: v.source != "fallback" for k, v in visuals.items()}
            tl = build_timeline(ctx, script, sb, audios, images, has_image)
            self._soundtrack(script, tl)
            video, keyframes = self._render(tl, plan, sb, visuals)
            qa = final_qa(ctx, video, tl, script, sb, keyframes)

        if not qa.ok:
            self.trace.log("orchestrator", "warn", "final QA not fully satisfied: "
                           + "; ".join(qa.problems + [f"visual {i}" for i in qa.redo_visuals]))
        write_report(self.run_dir, b, plan, script, sb, audios, tl, qa, self.history, self.trace, rate,
                     self.s.model_table(), self.music)
        latest = self.run_dir.parent / "latest.mp4"
        shutil.copyfile(video, latest)
        self.trace.log("orchestrator", "done", f"{video} ({qa.video_s:.1f}s) — {self.trace.llm_calls} API calls, "
                       f"${self.trace.cost_usd:.3f}, {self.trace.elapsed():.0f}s wall time")
        self.trace.close()
        return video
