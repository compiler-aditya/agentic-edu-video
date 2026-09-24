"""Audio helpers: ffmpeg discovery, decoding to numpy, WAV writing, speech-onset detection."""
from __future__ import annotations

import shutil
import subprocess
import wave
from functools import lru_cache
from pathlib import Path

import numpy as np

SR = 48000


@lru_cache(maxsize=None)
def ffmpeg_exe() -> str:
    """Prefer a system ffmpeg; fall back to the static binary shipped with imageio-ffmpeg."""
    sys_ff = shutil.which("ffmpeg")
    if sys_ff:
        return sys_ff
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def decode(path: Path, sr: int = SR) -> np.ndarray:
    proc = subprocess.run(
        [ffmpeg_exe(), "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", str(sr), "-"],
        capture_output=True, check=True,
    )
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def write_wav(path: Path, samples: np.ndarray, sr: int = SR) -> None:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def frame_db(samples: np.ndarray, sr: int = SR, win: float = 0.01) -> np.ndarray:
    n = int(sr * win)
    frames = samples[: len(samples) // n * n].reshape(-1, n)
    rms = np.sqrt(np.mean(frames**2, axis=1) + 1e-12)
    return 20 * np.log10(rms)


def speech_bounds(samples: np.ndarray, sr: int = SR, rel_db: float = -35.0) -> tuple[float, float]:
    """First and last time the signal rises within `rel_db` of its loudest 10 ms frame."""
    db = frame_db(samples, sr)
    if db.size == 0:
        return 0.0, 0.0
    voiced = np.where(db > db.max() + rel_db)[0]
    if voiced.size == 0:
        return 0.0, len(samples) / sr
    return voiced[0] * 0.01, (voiced[-1] + 1) * 0.01


def normalise(samples: np.ndarray, sr: int = SR, target_db: float = -19.0, peak_db: float = -1.0) -> np.ndarray:
    """Match speech loudness across scenes (RMS over voiced frames) with a peak ceiling."""
    db = frame_db(samples, sr)
    if db.size == 0:
        return samples
    voiced = db[db > db.max() - 35]
    speech_rms_db = 10 * np.log10(np.mean(10 ** (voiced / 10))) if voiced.size else db.max()
    gain = 10 ** ((target_db - speech_rms_db) / 20)
    out = samples * gain
    peak = np.abs(out).max() if out.size else 0
    ceiling = 10 ** (peak_db / 20)
    if peak > ceiling:
        out *= ceiling / peak
    return out
