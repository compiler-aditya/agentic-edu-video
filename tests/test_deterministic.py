"""Offline tests for the deterministic parts of the pipeline (no API calls)."""
import numpy as np
from PIL import Image

from eduvideo.__main__ import parse_topic
from eduvideo.agents.sync import _caption_chunks, _find_term, align_words, estimate_words, narration_tokens
from eduvideo.agents.visual import fit_canvas, strip_frame
from eduvideo.render.text import renderer
from eduvideo.schemas import Word
from eduvideo.textutil import norm_token, script_ratio, similarity

NARRATION = "पौधे सूर्य के प्रकाश से अपना भोजन बनाते हैं। इस प्रक्रिया को प्रकाश संश्लेषण कहते हैं।"


def _boundaries(tokens, step=0.4):
    return [Word(text=norm_token(t), start=i * step, end=i * step + 0.3) for i, t in enumerate(tokens)]


def test_parse_topic_variants():
    assert parse_topic("Class 7 → Science → Photosynthesis") == ("7", "Science", "Photosynthesis")
    assert parse_topic("class 10 -> Maths -> Linear Equations") == ("10", "Maths", "Linear Equations")


def test_align_words_keeps_punctuation_and_timings():
    tokens = narration_tokens(NARRATION)
    words, coverage = align_words(NARRATION, _boundaries(tokens))
    assert coverage == 1.0
    assert words[8].text == "हैं।"  # display text keeps the danda
    assert [w.start for w in words] == sorted(w.start for w in words)


def test_align_words_interpolates_missing_boundaries():
    tokens = narration_tokens(NARRATION)
    b = _boundaries(tokens)
    del b[3:5]  # TTS skipped two boundary events
    words, coverage = align_words(NARRATION, b)
    assert len(words) == len(tokens)
    assert coverage < 1.0
    assert all(w2.start >= w1.start for w1, w2 in zip(words, words[1:]))


def test_estimated_timings_span_speech():
    words = estimate_words(NARRATION, onset=0.3, offset=7.0)
    assert words[0].start == 0.3 and words[-1].end <= 7.0


def test_caption_chunks_respect_clauses_and_postpositions():
    text = ("पौधों की जड़ें मिट्टी से पानी सोखकर पत्तियों तक भेजती हैं। पत्तियां हवा से कार्बन डाइऑक्साइड "
            "लेने के लिए अपने नन्हे छिद्रों यानी स्टोमेटा का उपयोग करती हैं।")
    tokens = narration_tokens(text)
    chunks = _caption_chunks([Word(text=t, start=i * 0.4, end=i * 0.4 + 0.3) for i, t in enumerate(tokens)])
    assert sum(len(c) for c in chunks) == len(tokens)
    assert all(len(c) <= 7 for c in chunks)
    assert chunks[1][-1].text == "हैं।"  # sentence boundary is a chunk boundary
    assert not any(norm_token(c[0].text) in {"के", "लिए", "की", "से"} for c in chunks)


def test_find_term_multiword_and_inflected():
    words = [Word(text=t, start=i, end=i + 0.5) for i, t in enumerate(narration_tokens(NARRATION))]
    assert _find_term("प्रकाश संश्लेषण", words) == 12  # not the earlier standalone "प्रकाश"
    assert _find_term("प्रक्रिया", words) == 10


def test_script_validators():
    assert script_ratio(NARRATION, "Devanagari") == 1.0
    assert script_ratio("पौधे CO2 लेते हैं", "Devanagari") < 0.97
    assert similarity(NARRATION, NARRATION.replace("।", "")) == 1.0


def test_devanagari_shaping_reorders_i_matra():
    # In "कि" the i-matra glyph must be drawn to the LEFT of the consonant.
    r = renderer("Devanagari", 48)
    glyphs, _ = r.shape("कि")
    assert len(glyphs) == 2
    assert glyphs[0].gid != glyphs[1].gid
    ka_only, _ = r.shape("क")
    assert glyphs[1].gid == ka_only[0].gid  # consonant comes second after reordering


def test_fit_canvas_strips_frame_and_keeps_content():
    img = np.full((768, 1024, 3), 255, np.uint8)
    img[4:8, :] = 60
    img[-8:-4, :] = 60
    img[:, 4:8] = 60
    img[:, -8:-4] = 60                       # a border frame drawn by the image model
    img[300:460, 400:620] = (40, 160, 60)    # the actual drawing
    a = np.asarray(strip_frame(Image.fromarray(img)))
    assert a[6].min() == 255 and a[:, 6].min() == 255
    out = np.asarray(fit_canvas(Image.fromarray(img)))
    assert out.shape == (900, 1200, 3)
    assert (np.abs(out.astype(int) - (40, 160, 60)).sum(axis=2) < 30).sum() > 10000  # drawing kept, enlarged


def test_ts_offset_calibration_recovers_known_lag():
    from eduvideo.agents.sync import estimate_ts_offset, shift_words
    sr = 48000
    t = np.arange(int(sr * 4.0)) / sr
    x = np.zeros_like(t, dtype=np.float32)
    bursts = [(0.30, 1.2), (1.6, 2.4), (2.9, 3.6)]  # audible speech after pauses
    for a, b in bursts:
        m = (t >= a) & (t < b)
        x[m] = 0.3 * np.sin(2 * np.pi * 220 * t[m])
    lag = 0.16  # TTS timestamps lead the audio by 160 ms
    words = [Word(text=f"w{i}", start=a - lag, end=b - lag) for i, (a, b) in enumerate(bursts)]
    offset, anchors = estimate_ts_offset(x, words, sr)
    assert abs(offset - lag) < 0.02 and len(anchors) == 3
    shifted = shift_words(words, offset, 4.0)
    assert abs(shifted[1].start - 1.6) < 0.02


class _Trace:
    def __init__(self):
        self.events = []

    def log(self, agent, event, message="", **kw):
        self.events.append((agent, event, message))


def _ctx():
    from types import SimpleNamespace
    from eduvideo.agents.base import Brief
    from eduvideo.config import LANGUAGES
    return SimpleNamespace(brief=Brief("7", "Science", "Photosynthesis", LANGUAGES["hi"]), trace=_Trace())


def _board(**kw):
    from eduvideo.schemas import Annotation, Element, SceneBoard
    base = dict(id=1, layout="diagram",
                elements=[Element(id="leaf", description="a leaf"), Element(id="sun", description="the sun")],
                image_prompt="A leaf under the sun.",
                annotations=[Annotation(kind="label", target="leaf", text="पत्ती • Leaf", cue="पत्ती"),
                             Annotation(kind="arrow", source="sun", target="leaf", cue="सूर्य", color="orange")])
    base.update(kw)
    return SceneBoard(**base)


def test_storyboard_validator_catches_bad_cues_and_targets():
    from eduvideo.agents.storyboard import validate_storyboard
    from eduvideo.schemas import Annotation, SceneScript, Script, Storyboard
    script = Script(scenes=[SceneScript(id=1, title="t", narration="आज हम एक पौधे को देखते हैं। सूर्य की रोशनी पत्ती पर पड़ती है।")])
    assert validate_storyboard(_ctx(), script, Storyboard(scenes=[_board()])) == []
    bad = _board(annotations=[Annotation(kind="label", target="root", text="जड़", cue="जड़ें"),
                              Annotation(kind="arrow", source="moon", target="leaf", cue="पत्ती")])
    issues = validate_storyboard(_ctx(), script, Storyboard(scenes=[bad]))
    assert any("not an element id" in i for i in issues)
    assert any("not copied exactly" in i for i in issues)
    assert any("arrow source" in i for i in issues)


def test_storyboard_validator_enforces_pen_timing():
    from eduvideo.agents.storyboard import validate_storyboard
    from eduvideo.schemas import Annotation, SceneScript, Script, Storyboard
    script = Script(scenes=[SceneScript(id=1, title="t", narration="पत्ती हरी है। आज हम एक पौधे को देखते हैं। सूर्य की रोशनी पत्ती पर पड़ती है।")])
    early = _board(annotations=[Annotation(kind="label", target="leaf", text="पत्ती", cue="हरी"),
                                Annotation(kind="arrow", source="sun", target="leaf", cue="सूर्य")])
    issues = validate_storyboard(_ctx(), script, Storyboard(scenes=[early]))
    assert any("within the first" in i for i in issues)       # spoken while the title/sketch is being drawn
    crowded = _board(annotations=[Annotation(kind="arrow", source="sun", target="leaf", cue="सूर्य"),
                                  Annotation(kind="label", target="leaf", text="पत्ती", cue="रोशनी")])
    issues = validate_storyboard(_ctx(), script, Storyboard(scenes=[crowded]))
    assert any("less than 3 words" in i for i in issues)      # one pen cannot finish the arrow in time
    ok = _board(annotations=[Annotation(kind="arrow", source="sun", target="leaf", cue="सूर्य"),
                             Annotation(kind="label", target="leaf", text="पत्ती", cue="पत्ती पर")])
    assert validate_storyboard(_ctx(), script, Storyboard(scenes=[ok])) == []   # 2nd "पत्ती" is late enough


def test_schedule_starts_annotations_on_their_cue_words():
    from eduvideo.agents.sync import schedule_scene
    from eduvideo.schemas import SceneSlot
    narration = "पौधे की पत्ती बहुत ज़रूरी है और सूर्य की रोशनी पत्ती पर पड़ती है ताकि भोजन बने।"
    toks = narration_tokens(narration)
    words = [Word(text=t, start=10 + i * 0.45, end=10 + i * 0.45 + 0.3) for i, t in enumerate(toks)]
    slot = SceneSlot(scene_id=1, title="पत्ती", layout="diagram", image="x.png", start=10.0,
                     end=words[-1].end, vis_start=9.7, vis_end=words[-1].end + 0.3)
    ev = schedule_scene(_ctx(), slot, _board(), words, has_image=True)
    kinds = [e.kind for e in ev]
    assert kinds[:3] == ["title", "draw", "color"]
    arrow = next(e for e in ev if e.kind == "arrow")
    sun_t = words[toks.index("सूर्य")].start
    assert abs(arrow.start - sun_t) < 1e-6 and arrow.cue_time == sun_t   # starts exactly on the spoken word
    label = next(e for e in ev if e.kind == "label")
    assert label.cue_time == words[toks.index("रोशनी") + 1].start        # 2nd "पत्ती", after the drawing is ready
    draw = next(e for e in ev if e.kind == "draw")
    assert draw.end <= sun_t                                             # drawing finished before it is needed
    assert all(b.start >= a.end for a, b in zip(ev[3:], ev[4:]))         # one pen: actions never overlap


def test_sketch_reveals_outlines_progressively():
    from eduvideo.render.sketch import build
    img = np.full((300, 400, 3), 255, np.uint8)
    img[80:220, 100:300] = (40, 160, 60)
    sk = build(Image.fromarray(img), (400, 300))
    assert sk.n > 20
    board = np.full((300, 400, 3), 248, np.float32)
    half = sk.render(board, 0.5, 0.0)
    full = sk.render(board, 1.0, 0.0)
    drawn = lambda a: int((a.sum(axis=2) < 400).sum())
    assert 0 < drawn(half) < drawn(full)
    assert not sk.ink[150, 200]                                          # interior fill is not "ink"
    assert sk.pen_at(0.5) is not None and sk.pen_at(1.0) is None
