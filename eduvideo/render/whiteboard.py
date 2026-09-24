"""Whiteboard renderer: every frame is computed from the timeline clock (t = frame / fps).

Per scene:
  * a pen writes the title (handwriting reveal),
  * the illustration is sketched stroke by stroke, then coloured in,
  * labels are written and joined to the exact part with a leader line + dot,
  * arrows draw themselves, then keep flowing (animated dots) to show movement,
  * highlights draw a ring that keeps pulsing,
  * board lines (e.g. the word equation) are handwritten,
all starting on the word the narrator is saying. Scenes change with a slide, captions
highlight the word being spoken, and the narration WAV is muxed in (exact frame multiple,
so audio and video end on the same sample).
"""
from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from ..agents.sync import LABEL_WRITE, LEADER_DRAW
from ..audio import ffmpeg_exe
from ..schemas import AnimEvent, Caption, LessonPlan, SceneBoard, SceneSlot, Storyboard, Timeline
from .sketch import Sketch, build
from .text import renderer

W, H = 1280, 720
BOARD = (248, 249, 251)
INK = (30, 41, 59)
MUTED = (100, 116, 139)
ACCENT = (245, 158, 11)
HILITE = (255, 200, 30)
COLORS = {"blue": (37, 99, 235), "orange": (234, 128, 0), "green": (22, 163, 74), "red": (220, 38, 38),
          "gray": (90, 104, 124), "purple": (124, 58, 237)}
SS = 3  # supersampling factor for anti-aliased strokes

DIAGRAM = (316, 132, 964, 618)   # 4:3 drawing box, label gutters either side
BOARD_IMG = (792, 206, 1232, 536)  # small drawing on board scenes
LINES_X, LINES_Y, LINES_W = 84, 176, 660


def _ease(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    return x * x * (3 - 2 * x)


def _prog(t: float, e: AnimEvent) -> float:
    return min(max((t - e.start) / max(e.end - e.start, 1e-3), 0.0), 1.0)


# ------------------------------------------------------------------ drawing
def aa_stroke(img: Image.Image, pts: list[tuple[float, float]], color, width: float, alpha: int = 255,
              dot: tuple[float, float, float] | None = None, ring: tuple[float, float, float, float, float, float] | None = None) -> None:
    """Anti-aliased polyline / dot / arc drawn via a supersampled patch covering only its bounding box."""
    xs = [p[0] for p in pts] + ([dot[0] - dot[2], dot[0] + dot[2]] if dot else []) + ([ring[0], ring[2]] if ring else [])
    ys = [p[1] for p in pts] + ([dot[1] - dot[2], dot[1] + dot[2]] if dot else []) + ([ring[1], ring[3]] if ring else [])
    if not xs:
        return
    pad = width + 4
    x0, y0 = int(max(min(xs) - pad, 0)), int(max(min(ys) - pad, 0))
    x1, y1 = int(min(max(xs) + pad, img.width)), int(min(max(ys) + pad, img.height))
    if x1 <= x0 or y1 <= y0:
        return
    patch = Image.new("RGBA", ((x1 - x0) * SS, (y1 - y0) * SS), (*color, 0))
    d = ImageDraw.Draw(patch)
    fill = (*color, alpha)
    if len(pts) >= 2:
        sp = [((x - x0) * SS, (y - y0) * SS) for x, y in pts]
        d.line(sp, fill=fill, width=int(width * SS), joint="curve")
        r = width * SS / 2
        for x, y in (sp[0], sp[-1]):
            d.ellipse((x - r, y - r, x + r, y + r), fill=fill)
    if dot:
        cx, cy, r = (dot[0] - x0) * SS, (dot[1] - y0) * SS, dot[2] * SS
        d.ellipse((cx - r - 2 * SS, cy - r - 2 * SS, cx + r + 2 * SS, cy + r + 2 * SS), fill=(255, 255, 255, alpha))
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill)
    if ring:
        rx0, ry0, rx1, ry1, a0, a1 = ring
        d.arc(((rx0 - x0) * SS, (ry0 - y0) * SS, (rx1 - x0) * SS, (ry1 - y0) * SS), a0, a1, fill=fill, width=int(width * SS))
    patch = patch.resize((x1 - x0, y1 - y0), Image.Resampling.LANCZOS)
    img.alpha_composite(patch, (x0, y0))


def make_pen() -> tuple[Image.Image, tuple[int, int]]:
    """Blue marker held at ~40°, tip at the returned offset."""
    size = 150
    big = Image.new("RGBA", (size * SS, size * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    a = math.radians(40)
    dx, dy = math.cos(a), -math.sin(a)
    nx, ny = math.sin(a), math.cos(a)
    tip = (14 * SS, (size - 14) * SS)

    def quad(l0, l1, w0, w1):
        return [(tip[0] + dx * l0 * SS + nx * w0 * SS, tip[1] + dy * l0 * SS + ny * w0 * SS),
                (tip[0] + dx * l1 * SS + nx * w1 * SS, tip[1] + dy * l1 * SS + ny * w1 * SS),
                (tip[0] + dx * l1 * SS - nx * w1 * SS, tip[1] + dy * l1 * SS - ny * w1 * SS),
                (tip[0] + dx * l0 * SS - nx * w0 * SS, tip[1] + dy * l0 * SS - ny * w0 * SS)]

    shadow = Image.new("RGBA", big.size, (0, 0, 0, 0))
    ds = ImageDraw.Draw(shadow)
    ds.polygon([(x + 10 * SS, y + 8 * SS) for x, y in quad(4, 128, 3, 10)], fill=(0, 0, 0, 70))
    big.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(5 * SS)))
    d.polygon(quad(0, 16, 1.2, 6.5), fill=(30, 41, 59, 255))        # nib
    d.polygon(quad(15, 30, 7, 9), fill=(203, 213, 225, 255))        # grip ring
    d.polygon(quad(29, 108, 9, 9), fill=(37, 99, 235, 255))         # body
    d.polygon(quad(29, 108, 9, 3), fill=(59, 130, 246, 255))        # highlight
    d.polygon(quad(107, 128, 9.5, 9.5), fill=(30, 58, 138, 255))    # cap
    pen = big.resize((size, size), Image.Resampling.LANCZOS)
    return pen, (14, size - 14)


def reveal(img: Image.Image, p: float) -> Image.Image | None:
    w = int(img.width * p)
    return img.crop((0, 0, w, img.height)) if w > 0 else None


# ------------------------------------------------------------------ scene
@dataclass
class _Label:
    text_img: Image.Image
    pos: tuple[int, int]
    attach: tuple[float, float]
    anchor: tuple[float, float]
    color: tuple[int, int, int]


@dataclass
class _Arrow:
    pts: list[tuple[float, float]]
    color: tuple[int, int, int]
    tag: Image.Image | None
    tag_pos: tuple[int, int] = (0, 0)


@dataclass
class _Scene:
    slot: SceneSlot
    board: SceneBoard
    events: list[AnimEvent]
    title_img: Image.Image
    title_pos: tuple[int, int]
    sketch: Sketch | None
    img_box: tuple[int, int, int, int]
    labels: dict[int, _Label] = field(default_factory=dict)
    arrows: dict[int, _Arrow] = field(default_factory=dict)
    rings: dict[int, tuple[tuple[float, float, float, float], tuple[int, int, int]]] = field(default_factory=dict)
    lines: dict[int, tuple[Image.Image, tuple[int, int]]] = field(default_factory=dict)
    region_cache: np.ndarray | None = None


class Whiteboard:
    def __init__(self, tl: Timeline, plan: LessonPlan, storyboard: Storyboard, groundings: dict[int, dict],
                 run_dir: Path, script: str, header: str):
        self.tl, self.plan, self.script = tl, plan, script
        self.bg = np.empty((H, W, 3), np.float32)
        self.bg[:] = BOARD
        self.pen, self.pen_tip = make_pen()
        self.cap_font = renderer(script, 36)
        self._word_cache: dict = {}
        self._cap_cache: dict = {}
        self.header = renderer(script, 20).render(header, MUTED)
        by_scene: dict[int, list[AnimEvent]] = {}
        for e in tl.events:
            by_scene.setdefault(e.scene_id, []).append(e)
        self.scenes = [self._prepare(sl, bd, by_scene.get(sl.scene_id, []), groundings.get(sl.scene_id, {}), run_dir)
                       for sl, bd in zip(tl.scenes, storyboard.scenes)]
        self.intro = self._prepare_intro(plan.topic_native, header)

    # -------------------------------------------------------- preparation
    def _title(self, text: str, size: int = 44) -> Image.Image:
        r = renderer(self.script, size)
        while r.measure(text) > W - 160 and size > 30:
            size -= 4
            r = renderer(self.script, size)
        return r.render(text, INK)

    def _prepare(self, sl: SceneSlot, bd: SceneBoard, events: list[AnimEvent], ground: dict, run_dir: Path) -> _Scene:
        title = self._title(sl.title)
        box = DIAGRAM if bd.layout == "diagram" else BOARD_IMG
        img = Image.open(run_dir / sl.image)
        blank = np.asarray(img.convert("L")).min() > 245
        sketch = None if blank else build(img, (box[2] - box[0], box[3] - box[1]))
        sc = _Scene(sl, bd, events, title, ((W - title.width) // 2, 50), sketch, box)

        def anchor(eid: str) -> tuple[float, float] | None:
            g = ground.get(eid)
            if not g:
                return None
            py, px = g["point"]
            return box[0] + px / 1000 * (box[2] - box[0]), box[1] + py / 1000 * (box[3] - box[1])

        def bbox(eid: str) -> tuple[float, float, float, float] | None:
            g = ground.get(eid)
            if not g:
                return None
            y0, x0, y1, x1 = g["box_2d"]
            bw, bh = box[2] - box[0], box[3] - box[1]
            return box[0] + x0 / 1000 * bw, box[1] + y0 / 1000 * bh, box[0] + x1 / 1000 * bw, box[1] + y1 / 1000 * bh

        # labels: left/right gutters, stacked without overlap near their part's height
        lab_font = renderer(self.script, 25)
        pending: dict[str, list[tuple[int, Image.Image, tuple[float, float], tuple[int, int, int]]]] = {"l": [], "r": []}
        arrow_specs: list[tuple[int, tuple[float, float], tuple[float, float], tuple[int, int, int], str]] = []
        for i, a in enumerate(bd.annotations):
            col = COLORS[a.color]
            if a.kind == "label":
                pt = anchor(a.target)
                if pt is None:
                    continue
                lines = lab_font.wrap(a.text, 250)[:2]
                imgs = [lab_font.render(ln, INK) for ln in lines]
                tw, th = max(i2.width for i2 in imgs), sum(i2.height for i2 in imgs) - 4 * (len(imgs) - 1)
                timg = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
                y = 0
                side = "l" if pt[0] < (box[0] + box[2]) / 2 else "r"
                for i2 in imgs:
                    timg.alpha_composite(i2, (tw - i2.width if side == "l" else 0, y))
                    y += i2.height - 4
                pending[side].append((i, timg, pt, col))
            elif a.kind == "arrow":
                end = anchor(a.target)
                if end is None:
                    continue
                if a.source in ("left", "right", "top", "bottom"):
                    # enter from the drawing's own border (not the label gutters), at least 90 px long
                    start = {"left": (box[0] + 6, end[1] - 40), "right": (box[2] - 6, end[1] - 40),
                             "top": (end[0] - 60, box[1] + 6), "bottom": (end[0] + 40, box[3] - 6)}[a.source]
                    if math.hypot(end[0] - start[0], end[1] - start[1]) < 90:
                        k = 90 / max(math.hypot(end[0] - start[0], end[1] - start[1]), 1)
                        start = (end[0] - (end[0] - start[0]) * k, end[1] - (end[1] - start[1]) * k)
                else:
                    start = anchor(a.source or "") or (box[0] + 6, end[1])
                arrow_specs.append((i, start, end, col, a.text))
            elif a.kind == "highlight":
                b = bbox(a.target)
                if b is None:
                    continue
                pad = 10
                sc.rings[i] = ((b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad), col)
        for side, items in pending.items():
            items.sort(key=lambda it: it[2][1])
            y_prev = box[1] - 8
            placed = []
            for i, timg, pt, col in items:
                y = max(pt[1] - timg.height / 2, y_prev)
                placed.append([i, timg, pt, col, y])
                y_prev = y + timg.height + 14
            overflow = y_prev - 14 - (box[3] + 10)
            if overflow > 0:
                for pl in placed:
                    pl[4] -= overflow
            for i, timg, pt, col, y in placed:
                if side == "l":
                    x = box[0] - 30 - timg.width
                    attach = (box[0] - 22, y + timg.height / 2)
                else:
                    x = box[2] + 30
                    attach = (box[2] + 22, y + timg.height / 2)
                sc.labels[i] = _Label(timg, (int(x), int(y)), attach, pt, col)

        # arrows last, so their text tags can avoid everything already placed — and the drawing itself
        content = None
        if sketch is not None:
            ink = sketch.color.min(axis=2) < 232
            content = np.asarray(Image.fromarray((ink * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(9))) > 0
        occupied = [(sc.title_pos[0] - 10, 0, sc.title_pos[0] + title.width + 10, sc.title_pos[1] + title.height + 12)]
        occupied += [(lb.pos[0] - 10, lb.pos[1] - 6, lb.pos[0] + lb.text_img.width + 10, lb.pos[1] + lb.text_img.height + 6)
                     for lb in sc.labels.values()]
        for i, start, end, col, text in arrow_specs:
            sc.arrows[i] = self._arrow_geom(start, end, col, text, i, occupied, content, box)

        line_font_sizes = (36, 32, 28)
        for i, ln in enumerate(bd.board_lines):
            for size in line_font_sizes:
                r = renderer(self.script, size)
                if r.measure(ln.text) <= LINES_W:
                    break
            sc.lines[i] = (r.render(ln.text, INK), (LINES_X, LINES_Y + i * 84))
        return sc

    def _arrow_geom(self, start, end, col, text: str, i: int, occupied: list[tuple[float, float, float, float]],
                    content: np.ndarray | None = None, box: tuple[int, int, int, int] = DIAGRAM) -> _Arrow:
        sx, sy = start
        ex, ey = end
        length = math.hypot(ex - sx, ey - sy) or 1.0
        ux, uy = (ex - sx) / length, (ey - sy) / length
        ex, ey = ex - ux * 12, ey - uy * 12  # stop short so the head does not hide the part
        bend = (0.18 if i % 2 == 0 else -0.18) * length
        cx, cy = (sx + ex) / 2 - uy * bend, (sy + ey) / 2 + ux * bend
        pts = [((1 - s) ** 2 * sx + 2 * (1 - s) * s * cx + s * s * ex, (1 - s) ** 2 * sy + 2 * (1 - s) * s * cy + s * s * ey)
               for s in np.linspace(0, 1, 48)]
        tag = None
        tag_pos = (0, 0)
        if text:
            # text with a white halo (no box) beside the arrow; choose the spot that covers the least of
            # the other labels (heavily penalised), the drawing, and the arrow's own line
            tag = renderer(self.script, 23).render(text.split("•")[0].strip(), col, stroke=3, stroke_fill=(255, 255, 255))

            def drawing_cover(rect: tuple[float, float, float, float]) -> int:
                if content is None:
                    return 0
                x0, y0 = int(max(rect[0], box[0])) - box[0], int(max(rect[1], box[1])) - box[1]
                x1, y1 = int(min(rect[2], box[2])) - box[0], int(min(rect[3], box[3])) - box[1]
                return int(content[y0:y1, x0:x1].sum()) if x1 > x0 and y1 > y0 else 0

            def line_cover(rect: tuple[float, float, float, float]) -> int:
                return sum(1 for px, py in pts if rect[0] - 6 <= px <= rect[2] + 6 and rect[1] - 6 <= py <= rect[3] + 6)

            best, best_cost = None, float("inf")
            for frac in (0.1, 0.2, 0.35, 0.5, 0.65):
                mx, my = pts[int(frac * (len(pts) - 1))]
                for side in (1, -1):
                    for dist in (30, 50, 75, 100):
                        x = min(max(mx - tag.width / 2 - uy * dist * side, 8), W - tag.width - 8)
                        y = min(max(my - tag.height / 2 + ux * dist * side, 118), H - 110 - tag.height)
                        rect = (x, y, x + tag.width, y + tag.height)
                        ov = sum(max(0, min(rect[2], o[2]) - max(rect[0], o[0])) * max(0, min(rect[3], o[3]) - max(rect[1], o[1]))
                                 for o in occupied)
                        cost = 20 * ov + drawing_cover(rect) + 400 * line_cover(rect) + 2 * dist
                        if cost < best_cost:
                            best, best_cost = rect, cost
            assert best is not None
            tag_pos = (int(best[0]), int(best[1]))
            occupied.append((best[0] - 6, best[1] - 4, best[2] + 6, best[3] + 4))
        return _Arrow(pts, col, tag, tag_pos)

    def _prepare_intro(self, topic: str, sub: str) -> dict:
        t = self._title(topic, 76)
        s = renderer(self.script, 30).render(sub, MUTED)
        y = (H - t.height - s.height - 40) // 2 - 20
        return {"topic": t, "topic_pos": ((W - t.width) // 2, y), "sub": s,
                "sub_pos": ((W - s.width) // 2, y + t.height + 34), "line_y": y + t.height + 12}

    # --------------------------------------------------------------- frames
    def _pen_at(self, img: Image.Image, x: float, y: float, t: float) -> None:
        wig = math.sin(t * 37) * 1.6
        img.alpha_composite(self.pen, (int(x - self.pen_tip[0]), int(y - self.pen_tip[1] + wig)))

    def _write(self, img: Image.Image, text_img: Image.Image, pos: tuple[int, int], p: float, t: float,
               pens: list) -> None:
        part = reveal(text_img, p)
        if part is not None:
            img.alpha_composite(part, pos)
        if 0 < p < 1:
            pens.append((pos[0] + text_img.width * p, pos[1] + text_img.height * 0.62))

    def render_intro(self, t: float) -> Image.Image:
        img = Image.fromarray(self.bg.astype(np.uint8)).convert("RGBA")
        pens: list = []
        it = self.intro
        tw = min(max(len(self.plan.topic_native) / 14.0, 0.6), 1.3)
        self._write(img, it["topic"], it["topic_pos"], (t - 0.15) / tw, t, pens)
        p_line = (t - 0.2 - tw) / 0.35
        if p_line > 0:
            x0 = W / 2 - 90
            aa_stroke(img, [(x0, it["line_y"]), (x0 + 180 * min(p_line, 1), it["line_y"])], ACCENT, 6)
            if p_line < 1:
                pens.append((x0 + 180 * p_line, it["line_y"]))
        self._write(img, it["sub"], it["sub_pos"], (t - 0.55 - tw) / 0.5, t, pens)
        for x, y in pens[-1:]:
            self._pen_at(img, x, y, t)
        return img

    def render_scene(self, sc: _Scene, t: float) -> Image.Image:
        ev = {(e.kind, e.index): e for e in sc.events}
        pens: list[tuple[float, float]] = []
        # drawing region (numpy): sketch → colour
        frame = self.bg
        if sc.sketch is not None:
            draw, color = ev.get(("draw", 0)), ev.get(("color", 0))
            p_draw = _prog(t, draw) if draw else 1.0
            p_col = _ease(_prog(t, color)) if color else 1.0
            x0, y0, x1, y1 = sc.img_box
            if p_draw >= 1 and p_col >= 1 and sc.region_cache is not None:
                region = sc.region_cache
            else:
                region = sc.sketch.render(self.bg[y0:y1, x0:x1], p_draw, p_col)
                if p_draw >= 1 and p_col >= 1:
                    sc.region_cache = region
            frame = self.bg.copy()
            frame[y0:y1, x0:x1] = region
            pen = sc.sketch.pen_at(p_draw)
            if pen:
                pens.append((x0 + pen[0], y0 + pen[1]))
        img = Image.fromarray(frame.astype(np.uint8)).convert("RGBA")

        te = ev.get(("title", 0))
        if te:
            p = _prog(t, te)
            self._write(img, sc.title_img, sc.title_pos, p / 0.85, t, pens)
            if p > 0.85:
                q = (p - 0.85) / 0.15
                cx, y = W / 2, sc.title_pos[1] + sc.title_img.height + 4
                aa_stroke(img, [(cx - 60, y), (cx - 60 + 120 * min(q, 1), y)], ACCENT, 5)

        for e in sc.events:
            if t < e.start:
                continue
            p = _prog(t, e)
            if e.kind == "label" and e.index in sc.labels:
                lb = sc.labels[e.index]
                total = LABEL_WRITE + LEADER_DRAW
                pw = min(p * total / LABEL_WRITE, 1.0)
                self._write(img, lb.text_img, lb.pos, pw, t, pens)
                pl = (p * total - LABEL_WRITE) / LEADER_DRAW
                if pl > 0:
                    pl = min(pl, 1.0)
                    ax, ay = lb.attach
                    bx, by = lb.anchor
                    ex, ey = ax + (bx - ax) * pl, ay + (by - ay) * pl
                    aa_stroke(img, [(ax, ay), (ex, ey)], lb.color, 3,
                              dot=(bx, by, 6) if pl >= 1 else None)
                    if pl < 1:
                        pens.append((ex, ey))
            elif e.kind == "arrow" and e.index in sc.arrows:
                ar = sc.arrows[e.index]
                n = max(2, int(len(ar.pts) * _ease(p)))
                pts = ar.pts[:n]
                aa_stroke(img, pts, ar.color, 6)
                if p < 1:
                    pens.append(pts[-1])
                else:
                    self._arrow_head(img, ar)
                    self._flow(img, ar, t - e.end)
                    if ar.tag is not None:
                        img.alpha_composite(ar.tag, ar.tag_pos)
            elif e.kind == "highlight" and e.index in sc.rings:
                (rx0, ry0, rx1, ry1), col = sc.rings[e.index]
                a1 = -90 + 360 * _ease(p)
                aa_stroke(img, [], col, 5, ring=(rx0, ry0, rx1, ry1, -90, a1))
                if p < 1:
                    ang = math.radians(a1)
                    pens.append(((rx0 + rx1) / 2 + (rx1 - rx0) / 2 * math.cos(ang), (ry0 + ry1) / 2 + (ry1 - ry0) / 2 * math.sin(ang)))
                else:
                    pulse = (math.sin((t - e.end) * 4.2) + 1) / 2
                    g = 4 + 6 * pulse
                    aa_stroke(img, [], col, 3, alpha=int(90 * (1 - pulse)) + 30,
                              ring=(rx0 - g, ry0 - g, rx1 + g, ry1 + g, 0, 360))
            elif e.kind == "line" and e.index in sc.lines:
                limg, pos = sc.lines[e.index]
                bullet_y = pos[1] + limg.height * 0.55
                aa_stroke(img, [], ACCENT, 1, dot=(pos[0] - 26, bullet_y, 6 * min(p * 4, 1)))
                self._write(img, limg, pos, p, t, pens)
        if pens:
            x, y = pens[-1]
            self._pen_at(img, x, y, t)
        return img

    def _arrow_head(self, img: Image.Image, ar: _Arrow) -> None:
        (x1, y1), (x2, y2) = ar.pts[-4], ar.pts[-1]
        ang = math.atan2(y2 - y1, x2 - x1)
        L, Wd = 20, 11
        tip = (x2 + math.cos(ang) * 6, y2 + math.sin(ang) * 6)
        left = (tip[0] - L * math.cos(ang) + Wd * math.sin(ang), tip[1] - L * math.sin(ang) - Wd * math.cos(ang))
        right = (tip[0] - L * math.cos(ang) - Wd * math.sin(ang), tip[1] - L * math.sin(ang) + Wd * math.cos(ang))
        xs, ys = [tip[0], left[0], right[0]], [tip[1], left[1], right[1]]
        x0, y0 = int(min(xs)) - 2, int(min(ys)) - 2
        patch = Image.new("RGBA", ((int(max(xs)) - x0 + 4) * SS, (int(max(ys)) - y0 + 4) * SS), (*ar.color, 0))
        ImageDraw.Draw(patch).polygon([((x - x0) * SS, (y - y0) * SS) for x, y in (tip, left, right)], fill=(*ar.color, 255))
        patch = patch.resize((patch.width // SS, patch.height // SS), Image.Resampling.LANCZOS)
        img.alpha_composite(patch, (x0, y0))

    def _flow(self, img: Image.Image, ar: _Arrow, dt: float) -> None:
        n = len(ar.pts)
        for k in range(3):
            s = ((dt / 1.4) + k / 3) % 1.0
            x, y = ar.pts[min(int(s * (n - 1)), n - 1)]
            aa_stroke(img, [], (255, 255, 255), 1, dot=(x, y, 3.2))

    # ------------------------------------------------------------ captions
    def _word(self, text: str, color) -> Image.Image:
        key = (text, color)
        if key not in self._word_cache:
            self._word_cache[key] = self.cap_font.render(text, color)
        return self._word_cache[key]

    def _caption(self, ci: int, cap: Caption, active: int) -> Image.Image:
        key = (ci, active)
        if key in self._cap_cache:
            return self._cap_cache[key]
        space = self.cap_font.measure(" ")
        max_w = W - 240
        lines: list[list[int]] = [[]]
        width = 0.0
        for i, w in enumerate(cap.words):
            ww = self._word(w.text, (255, 255, 255)).width
            if lines[-1] and width + space + ww > max_w:
                lines.append([])
                width = 0.0
            width += (space if lines[-1] else 0) + ww
            lines[-1].append(i)
        line_h = int(self.cap_font.line_height) + 6
        lws = [sum(self._word(cap.words[i].text, (255, 255, 255)).width for i in ln) + space * (len(ln) - 1) for ln in lines]
        bw, bh = int(max(lws)) + 44, line_h * len(lines) + 14
        box = Image.new("RGBA", (bw * SS, bh * SS), (0, 0, 0, 0))
        ImageDraw.Draw(box).rounded_rectangle((0, 0, bw * SS - 1, bh * SS - 1), radius=16 * SS, fill=(17, 24, 39, 225))
        box = box.resize((bw, bh), Image.Resampling.LANCZOS)
        for li, ln in enumerate(lines):
            x = (bw - lws[li]) / 2
            for i in ln:
                color = HILITE if i == active else ((255, 255, 255) if i < active else (170, 180, 195))
                wi = self._word(cap.words[i].text, color)
                box.alpha_composite(wi, (int(x), 5 + li * line_h))
                x += wi.width + space
        self._cap_cache[key] = box
        return box

    # ---------------------------------------------------------------- frame
    def frame(self, t: float) -> Image.Image:
        tl = self.tl
        first = tl.scenes[0].vis_start
        XF = 0.5
        # which boards are visible (slide transitions centred on each cut)
        cuts = [first] + [sl.vis_end for sl in tl.scenes[:-1]]
        layers: list[tuple[Image.Image, int]] = []
        idx = sum(1 for c in cuts if t >= c)  # 0 = intro, k = scene k
        prev_cut = cuts[idx - 1] if idx > 0 else None
        next_cut = cuts[idx] if idx < len(cuts) else None

        def board(i: int, tt: float) -> Image.Image:
            return self.render_intro(tt) if i == 0 else self.render_scene(self.scenes[i - 1], tt)

        if next_cut is not None and t > next_cut - XF / 2:
            e = _ease((t - (next_cut - XF / 2)) / XF)
            layers = [(board(idx, t), int(-e * W)), (board(idx + 1, t), int((1 - e) * W))]
        elif prev_cut is not None and t < prev_cut + XF / 2:
            e = _ease((t - (prev_cut - XF / 2)) / XF)
            layers = [(board(idx - 1, t), int(-e * W)), (board(idx, t), int((1 - e) * W))]
        else:
            layers = [(board(idx, t), 0)]
        img = Image.new("RGBA", (W, H), (*BOARD, 255))
        for layer, dx in layers:
            if -W < dx < W:
                img.alpha_composite(layer.crop((max(0, -dx), 0, min(W, W - dx), H)), (max(0, dx), 0))

        if t >= first:
            img.alpha_composite(self.header, (28, 14))
            cur = max(1, min(idx, len(tl.scenes)))
            badge = renderer("Latin", 20).render(f"{cur} / {len(tl.scenes)}", MUTED)
            img.alpha_composite(badge, (W - badge.width - 28, 14))

        for ci, cap in enumerate(tl.captions):
            if cap.start <= t < cap.end:
                active = max(i for i, w in enumerate(cap.words) if w.start <= t) if cap.words[0].start <= t else -1
                if t > cap.words[-1].end + 0.05:
                    active = len(cap.words)
                c = self._caption(ci, cap, active)
                img.alpha_composite(c, ((W - c.width) // 2, H - c.height - 22))
                break

        d = ImageDraw.Draw(img)
        prog = t / tl.duration
        d.rectangle((0, H - 5, W, H), fill=(226, 232, 240, 255))
        d.rectangle((0, H - 5, int(W * prog), H), fill=(*ACCENT, 255))
        return img.convert("RGB")


def render_video(tl: Timeline, plan: LessonPlan, storyboard: Storyboard, groundings: dict[int, dict], run_dir: Path,
                 out_path: Path, script: str, header: str, keyframe_times: dict[int, float]) -> dict[int, Path]:
    wb = Whiteboard(tl, plan, storyboard, groundings, run_dir, script, header)
    n = int(round(tl.duration * tl.fps))
    kf_dir = run_dir / "keyframes"
    kf_dir.mkdir(exist_ok=True)
    kf_frames = {int(round(t * tl.fps)): sid for sid, t in keyframe_times.items()}
    keyframes: dict[int, Path] = {}
    cmd = [ffmpeg_exe(), "-y", "-v", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(tl.fps), "-i", "pipe:0",
           "-i", str(run_dir / tl.audio_path),
           "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
           "-movflags", "+faststart", str(out_path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        for f in range(n):
            frame = wb.frame(f / tl.fps)
            if f in kf_frames:
                p = kf_dir / f"scene_{kf_frames[f]}.jpg"
                frame.save(p, quality=90)
                keyframes[kf_frames[f]] = p
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read().decode() if proc.stderr else ""
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg failed: {err[-800:]}")
    return keyframes


def contact_sheet(keyframes: dict[int, Path], out: Path, cols: int = 3) -> None:
    ims = [Image.open(keyframes[k]) for k in sorted(keyframes)]
    if not ims:
        return
    w, h = ims[0].width // 2, ims[0].height // 2
    rows = math.ceil(len(ims) / cols)
    sheet = Image.new("RGB", (cols * w + (cols + 1) * 8, rows * h + (rows + 1) * 8), (20, 20, 24))
    for i, im in enumerate(ims):
        sheet.paste(im.resize((w, h), Image.Resampling.LANCZOS), (8 + (i % cols) * (w + 8), 8 + (i // cols) * (h + 8)))
    sheet.save(out, quality=90)
