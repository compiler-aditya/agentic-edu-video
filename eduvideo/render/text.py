"""Complex-script text rendering (Devanagari, Bengali, Tamil, ...) with HarfBuzz + FreeType.

Pillow without libraqm and ffmpeg without libass cannot shape Indic scripts
(matras land on the wrong side, conjuncts break). Shaping here is done with
uharfbuzz and glyphs are rasterised with freetype-py, both pip wheels, so the
renderer is correct on any machine with no system libraries.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from functools import lru_cache

import freetype
import numpy as np
import uharfbuzz as hb
from PIL import Image, ImageFilter

from ..config import find_fallback_font, find_font

_JOINERS = {"‌", "‍", "◌"}


class _Font:
    def __init__(self, path: str, index: int, size: int):
        blob = hb.Blob.from_file_path(path)
        self.hb = hb.Font(hb.Face(blob, index))
        self.hb.scale = (size * 64, size * 64)  # positions come back in 26.6 fixed point
        self.ft = freetype.Face(path, index=index)
        self.ft.set_char_size(size * 64)
        self.ascender = self.ft.size.ascender / 64
        self.descender = self.ft.size.descender / 64
        self._bitmaps: dict[int, tuple[np.ndarray, int, int]] = {}

    def has(self, ch: str) -> bool:
        return self.ft.get_char_index(ord(ch)) != 0

    def glyph(self, gid: int) -> tuple[np.ndarray, int, int]:
        cached = self._bitmaps.get(gid)
        if cached is None:
            self.ft.load_glyph(gid, freetype.FT_LOAD_DEFAULT)
            self.ft.glyph.render(freetype.FT_RENDER_MODE_NORMAL)
            bm = self.ft.glyph.bitmap
            arr = np.array(bm.buffer, dtype=np.uint8).reshape(bm.rows, bm.pitch)[:, : bm.width] if bm.rows else np.zeros((0, 0), np.uint8)
            cached = (arr, self.ft.glyph.bitmap_left, self.ft.glyph.bitmap_top)
            self._bitmaps[gid] = cached
        return cached


@dataclass
class _Glyph:
    font: _Font
    gid: int
    x: float
    y_off: float


class TextRenderer:
    """Shapes and rasterises a single line of text into an alpha mask."""

    def __init__(self, script: str, size: int):
        path, idx = find_font(script)
        self.primary = _Font(path, idx, size)
        fb = find_fallback_font()
        self.fallback = _Font(fb[0], fb[1], size) if fb else None
        self.size = size
        self.ascender = self.primary.ascender
        self.descender = self.primary.descender
        self.line_height = self.ascender - self.descender

    def _runs(self, text: str) -> list[tuple[_Font, str]]:
        runs: list[tuple[_Font, str]] = []
        for ch in text:
            font = self.primary
            if not (ch.isspace() or ch in _JOINERS or unicodedata.category(ch).startswith("M") or self.primary.has(ch)):
                if self.fallback and self.fallback.has(ch):
                    font = self.fallback
            if runs and (runs[-1][0] is font or unicodedata.category(ch).startswith("M") or ch in _JOINERS):
                runs[-1] = (runs[-1][0], runs[-1][1] + ch)
            else:
                runs.append((font, ch))
        return runs

    def shape(self, text: str) -> tuple[list[_Glyph], float]:
        glyphs: list[_Glyph] = []
        pen = 0.0
        for font, run in self._runs(text):
            buf = hb.Buffer()
            buf.add_str(run)
            buf.guess_segment_properties()
            hb.shape(font.hb, buf, {"kern": True, "liga": True})
            for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
                glyphs.append(_Glyph(font, info.codepoint, pen + pos.x_offset / 64, pos.y_offset / 64))
                pen += pos.x_advance / 64
        return glyphs, pen

    def measure(self, text: str) -> float:
        return self.shape(text)[1]

    def mask(self, text: str, pad: int = 2) -> Image.Image:
        """Return an 'L' mask; the baseline sits at y = pad + ascender."""
        glyphs, width = self.shape(text)
        w = int(np.ceil(width)) + 2 * pad
        h = int(np.ceil(self.line_height)) + 2 * pad
        canvas = np.zeros((h, w), dtype=np.uint8)
        baseline = pad + self.ascender
        for g in glyphs:
            arr, left, top = g.font.glyph(g.gid)
            if arr.size == 0:
                continue
            x0 = int(round(pad + g.x + left))
            y0 = int(round(baseline - g.y_off - top))
            x1, y1 = x0 + arr.shape[1], y0 + arr.shape[0]
            cx0, cy0, cx1, cy1 = max(x0, 0), max(y0, 0), min(x1, w), min(y1, h)
            if cx1 <= cx0 or cy1 <= cy0:
                continue
            region = canvas[cy0:cy1, cx0:cx1]
            np.maximum(region, arr[cy0 - y0 : cy1 - y0, cx0 - x0 : cx1 - x0], out=region)
        return Image.fromarray(canvas, "L")

    def render(self, text: str, fill: tuple[int, int, int], stroke: int = 0,
               stroke_fill: tuple[int, int, int] = (0, 0, 0), shadow: bool = False) -> Image.Image:
        pad = stroke + (6 if shadow else 2)
        m = self.mask(text, pad=pad)
        out = Image.new("RGBA", m.size, (0, 0, 0, 0))
        if shadow:
            sh = m.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(4))
            layer = Image.new("RGBA", m.size, (0, 0, 0, 255))
            layer.putalpha(sh.point(lambda a: int(a * 0.7)))
            out = Image.alpha_composite(out, _offset(layer, 2, 3))
        if stroke:
            st = m.filter(ImageFilter.MaxFilter(2 * stroke + 1))
            layer = Image.new("RGBA", m.size, (*stroke_fill, 255))
            layer.putalpha(st)
            out = Image.alpha_composite(out, layer)
        layer = Image.new("RGBA", m.size, (*fill, 255))
        layer.putalpha(m)
        return Image.alpha_composite(out, layer)

    def wrap(self, text: str, max_width: float) -> list[str]:
        lines: list[str] = []
        cur = ""
        for word in text.split():
            trial = f"{cur} {word}".strip()
            if cur and self.measure(trial) > max_width:
                lines.append(cur)
                cur = word
            else:
                cur = trial
        if cur:
            lines.append(cur)
        return lines


def _offset(img: Image.Image, dx: int, dy: int) -> Image.Image:
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.paste(img, (dx, dy))
    return out


@lru_cache(maxsize=None)
def renderer(script: str, size: int) -> TextRenderer:
    return TextRenderer(script, size)
