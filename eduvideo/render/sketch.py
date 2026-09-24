"""Whiteboard "draw-on" effect: turn an illustration into pen strokes revealed over time.

1. Extract line art from the illustration (dark outlines + colour-boundary edges).
2. Split the canvas into small cells; cells containing ink are ordered by a greedy
   nearest-neighbour walk, which makes the pen move continuously across nearby strokes
   the way a person draws, rather than scanning line by line.
3. Every pixel gets the rank of its cell, so the sketch visible at progress p is simply
   `ink & (rank <= p * n_cells)`; the pen sits on the most recently drawn cell.
After the sketch completes, the full-colour drawing fades in over it.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageFilter

CELL = 10
INK = np.array([38, 46, 61], dtype=np.float32)   # sketch stroke colour


@dataclass
class Sketch:
    color: np.ndarray        # (h, w, 3) float32 illustration, 0-255
    ink: np.ndarray          # (h, w) bool line-art mask
    rank: np.ndarray         # (h, w) float32 draw order per pixel (inf = never drawn)
    path: np.ndarray         # (n, 2) cell centres (x, y) in draw order
    size: tuple[int, int]

    @property
    def n(self) -> int:
        return len(self.path)

    def pen_at(self, p: float) -> tuple[float, float] | None:
        if self.n == 0 or p <= 0 or p >= 1:
            return None
        x, y = self.path[min(int(p * self.n), self.n - 1)]
        return float(x), float(y)

    def render(self, board: np.ndarray, p_sketch: float, p_color: float) -> np.ndarray:
        """Composite onto `board` (h, w, 3 float) with multiply blending so white vanishes into the board."""
        out = board.copy()
        if p_sketch > 0:
            visible = self.ink & (self.rank <= p_sketch * self.n)
            out[visible] = INK
        if p_color > 0:
            colored = board * (self.color / 255.0)
            out = out * (1 - p_color) + colored * p_color
        return out


def line_art(rgb: np.ndarray) -> np.ndarray:
    """Outlines only: colour/brightness boundaries plus thin dark strokes.

    Large dark fills (soil, a dark pot) are reduced to their boundary so the sketch phase
    looks like pen lines, not ink blobs; the fills arrive with the colour pass.
    """
    gray = Image.fromarray(rgb.astype(np.uint8)).convert("L")
    mag = np.asarray(gray.filter(ImageFilter.GaussianBlur(1.0)).filter(ImageFilter.FIND_EDGES), dtype=np.float32)
    edges = mag > 24
    # dense textures (e.g. hundreds of chloroplasts) would sketch as a solid blob: keep only strong edges there
    density = np.asarray(Image.fromarray((edges * 255).astype(np.uint8)).filter(ImageFilter.BoxBlur(10)), np.float32) / 255
    edges &= (density < 0.28) | (mag > 70)
    dark = np.asarray(gray, dtype=np.float32) < 90
    dark_img = Image.fromarray((dark * 255).astype(np.uint8))
    thick = np.asarray(dark_img.filter(ImageFilter.MinFilter(7))) > 0   # dark areas wider than ~7px
    grown = np.asarray(Image.fromarray((thick * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(7))) > 0
    thin_dark = dark & ~grown
    ink = edges | thin_dark
    ink[:4], ink[-4:], ink[:, :4], ink[:, -4:] = False, False, False, False  # edge-filter border artefacts
    thick_lines = np.asarray(Image.fromarray((ink * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(3))) > 0
    busy = np.asarray(Image.fromarray((ink * 255).astype(np.uint8)).filter(ImageFilter.BoxBlur(8)), np.float32) / 255 > 0.22
    return np.where(busy, ink, thick_lines)  # marker-thick strokes, except inside busy detail


def build(img: Image.Image, size: tuple[int, int]) -> Sketch:
    img = img.convert("RGB").resize(size, Image.Resampling.LANCZOS)
    rgb = np.asarray(img, dtype=np.float32)
    ink = line_art(rgb)
    h, w = ink.shape
    gh, gw = (h + CELL - 1) // CELL, (w + CELL - 1) // CELL
    padded = np.zeros((gh * CELL, gw * CELL), bool)
    padded[:h, :w] = ink
    cell_ink = padded.reshape(gh, CELL, gw, CELL).sum(axis=(1, 3)) >= 3
    cy, cx = np.nonzero(cell_ink)
    pts = np.stack([cx * CELL + CELL / 2, cy * CELL + CELL / 2], axis=1).astype(np.float32)
    order = _greedy_order(pts)
    cell_rank = np.full((gh, gw), np.inf, np.float32)
    cell_rank[cy[order], cx[order]] = np.arange(len(order), dtype=np.float32)
    rank = np.repeat(np.repeat(cell_rank, CELL, axis=0), CELL, axis=1)[:h, :w]
    return Sketch(rgb, ink, rank, pts[order], size)


def _greedy_order(pts: np.ndarray) -> np.ndarray:
    n = len(pts)
    if n == 0:
        return np.zeros(0, int)
    left = np.ones(n, bool)
    # start at the top-left-most ink so drawing begins where a hand naturally would
    cur = int(np.argmin(pts[:, 0] * 0.6 + pts[:, 1]))
    order = [cur]
    left[cur] = False
    for _ in range(n - 1):
        d = np.abs(pts[:, 0] - pts[cur, 0]) + np.abs(pts[:, 1] - pts[cur, 1])
        d[~left] = np.inf
        cur = int(np.argmin(d))
        order.append(cur)
        left[cur] = False
    return np.array(order)
