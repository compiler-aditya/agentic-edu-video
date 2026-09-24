"""Typed contracts passed between agents. Every LLM output is validated against these."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- planning
class ScenePlan(BaseModel):
    id: int = Field(description="1-based scene number")
    title: str = Field(description="Short on-screen scene title in the target language (2-5 words)")
    title_en: str = Field(description="English translation of the title")
    learning_goal: str = Field(description="What the student learns in this scene (English)")
    visual_idea: str = Field(description="What the whiteboard drawing should show (English)")
    target_seconds: float = Field(description="Planned narration length in seconds")


class LessonPlan(BaseModel):
    topic_native: str = Field(description="Topic name in the target language")
    subject_native: str = Field(description="Subject name in the target language")
    learning_objectives: list[str] = Field(description="2-4 learning objectives (English)")
    visual_style: str = Field(description="One art-direction sentence applied to every illustration for consistency")
    scenes: list[ScenePlan]


# ------------------------------------------------------------------ script
class SceneScript(BaseModel):
    id: int
    title: str = Field(description="On-screen title in the target language")
    narration: str = Field(description="Spoken narration in the target language")


class Script(BaseModel):
    scenes: list[SceneScript]


# -------------------------------------------------------------- storyboard
Color = Literal["blue", "orange", "green", "red", "gray", "purple"]
Edge = Literal["left", "right", "top", "bottom"]


class Element(BaseModel):
    id: str = Field(description="short snake_case id, e.g. 'leaf', 'roots', 'sun'")
    description: str = Field(description="English: how this part looks in the drawing (used to draw and to locate it)")


class Annotation(BaseModel):
    kind: Literal["label", "arrow", "highlight"]
    target: str = Field(description="element id the label points at / the arrow ends at / the highlight circles")
    source: str | None = Field(default=None, description="arrow only: element id, or an edge (left/right/top/bottom) the arrow flows in from")
    text: str = Field(default="", description="label text (required for label; optional short tag for arrow) in the target language")
    cue: str = Field(description="words copied EXACTLY from this scene's narration; the annotation appears when they are spoken")
    color: Color = "blue"


class BoardLine(BaseModel):
    text: str = Field(description="line to write on the board in the target language (may use + and →)")
    cue: str = Field(description="words copied EXACTLY from the narration; writing starts when they are spoken")


class SceneBoard(BaseModel):
    id: int
    layout: Literal["diagram", "board"] = Field(
        description="diagram: large drawing with labels/arrows; board: written lines on the left + small drawing on the right")
    elements: list[Element] = Field(description="parts that must be visible in the drawing (diagram: 2-6, board: 1-3)")
    image_prompt: str = Field(description="English description of the single illustration to draw for this scene")
    annotations: list[Annotation] = Field(default_factory=list, description="diagram: 2-5 annotations; board: none")
    board_lines: list[BoardLine] = Field(default_factory=list, description="board: 1-4 lines; diagram: none")


class Storyboard(BaseModel):
    scenes: list[SceneBoard]


class Located(BaseModel):
    id: str
    found: bool
    box_2d: list[int] = Field(default_factory=list, description="[ymin, xmin, ymax, xmax] normalised 0-1000")
    point: list[int] = Field(default_factory=list, description="[y, x] normalised 0-1000: best spot on the element for a pointer")


class Grounding(BaseModel):
    elements: list[Located]


# ----------------------------------------------------------------- reviews
class ReviewIssue(BaseModel):
    scene_id: int | None = Field(default=None, description="Scene the issue is in, or null for global issues")
    severity: Literal["minor", "major", "critical"]
    category: Literal["factual", "grade_level", "language", "length", "key_terms", "coherence", "visual_prompt", "other"]
    problem: str
    fix: str = Field(description="Concrete instruction for the writer")


class ScriptReview(BaseModel):
    approved: bool
    score: int = Field(ge=1, le=10)
    summary: str
    issues: list[ReviewIssue] = Field(default_factory=list)


class VisualReview(BaseModel):
    relevant_to_narration: bool
    scientifically_accurate: bool
    garbled_or_wrong_text: bool = Field(description="True if the image contains misspelled, gibberish or incorrect text/labels")
    age_appropriate: bool
    missing_elements: list[str] = Field(default_factory=list, description="ids of required elements that are absent or unrecognisable")
    score: int = Field(ge=1, le=10)
    problems: list[str] = Field(default_factory=list)
    improved_prompt: str = Field(default="", description="A better English prompt if the image should be regenerated")

    @property
    def approved(self) -> bool:
        return (self.relevant_to_narration and self.scientifically_accurate and not self.garbled_or_wrong_text
                and self.age_appropriate and not self.missing_elements and self.score >= 7)


class Transcript(BaseModel):
    transcript: str
    problems: list[str] = Field(default_factory=list, description="Mispronounced, skipped or garbled words, if any")


class FrameCheck(BaseModel):
    scene_id: int
    visual_matches_narration: bool
    labels_point_correctly: bool = Field(default=True, description="every label/arrow points at the part its text names")
    captions_legible: bool
    rendering_glitch: bool = Field(description="True for broken text shaping, overlapping elements, cut-off text, artifacts")
    problems: list[str] = Field(default_factory=list)


class FinalReview(BaseModel):
    frames: list[FrameCheck]
    overall_ok: bool
    notes: str = ""


# ------------------------------------------------------------ timeline data
class Word(BaseModel):
    text: str
    start: float
    end: float


class SceneAudio(BaseModel):
    scene_id: int
    path: str
    duration: float
    words: list[Word]          # timestamps relative to the scene audio file
    voice: str
    rate: str = "+0%"
    narration_hash: str
    asr_similarity: float | None = None
    coverage: float = 1.0      # share of narration tokens matched to a TTS word timestamp
    ts_offset: float | None = None  # measured lag of audible speech behind the TTS timestamps (s)
    boundary_source: Literal["tts", "estimated"] = "tts"
    engine: Literal["edge", "elevenlabs", "openrouter"] = "edge"  # elevenlabs clips are slices of one continuous take


class Caption(BaseModel):
    start: float
    end: float
    words: list[Word]          # absolute timestamps
    scene_id: int


class AnimEvent(BaseModel):
    scene_id: int
    kind: Literal["title", "draw", "color", "label", "arrow", "highlight", "line"]
    start: float               # absolute seconds
    end: float                 # when the writing/drawing motion finishes (element then persists)
    index: int = 0             # annotation / board-line index within the scene
    cue: str = ""
    cue_time: float | None = None  # when the cue word is spoken (None for scheduled events)


class SceneSlot(BaseModel):
    scene_id: int
    title: str
    layout: Literal["diagram", "board"]
    image: str
    start: float               # narration start (absolute)
    end: float                 # narration end (absolute)
    vis_start: float           # visual slot (includes padding / transition region)
    vis_end: float


class Timeline(BaseModel):
    duration: float
    fps: int
    width: int
    height: int
    scenes: list[SceneSlot]
    captions: list[Caption]
    events: list[AnimEvent]
    audio_path: str
