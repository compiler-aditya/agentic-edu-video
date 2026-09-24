"""Runtime settings, model routing and language/voice/font registry."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path.cwd() / ".env")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Language:
    code: str
    name: str            # English name, used in prompts
    native: str          # native name, shown on screen
    voice: str           # edge-tts neural voice
    alt_voice: str       # used when the audio QA agent rejects a take twice
    script: str          # writing system, used to pick a font + validate output
    words_per_sec: float # initial speaking-rate estimate, refined after first TTS pass
    grade_word: str      # "Class" in the target language
    key_term: str        # "Key term" in the target language (card header)
    el_voice: str = ""   # ElevenLabs voice (whole-lesson narration); empty = use edge-tts
    el_alt_voice: str = ""
    el_words_per_sec: float = 1.45


LANGUAGES: dict[str, Language] = {
    # ElevenLabs voices: Aarohi (calm, conversational Hindi teacher) with Ankit as the alternate take
    "hi": Language("hi", "Hindi", "हिन्दी", "hi-IN-SwaraNeural", "hi-IN-MadhurNeural", "Devanagari", 2.2, "कक्षा", "मुख्य शब्द",
                   el_voice="rqIg3iVrlZOAkxCMdelQ", el_alt_voice="Dy1awEdnuMQtEftpr6Pa", el_words_per_sec=1.45),
    "mr": Language("mr", "Marathi", "मराठी", "mr-IN-AarohiNeural", "mr-IN-ManoharNeural", "Devanagari", 2.0, "इयत्ता", "महत्त्वाचा शब्द"),
    "bn": Language("bn", "Bengali", "বাংলা", "bn-IN-TanishaaNeural", "bn-IN-BashkarNeural", "Bengali", 2.0, "শ্রেণী", "মূল শব্দ"),
    "gu": Language("gu", "Gujarati", "ગુજરાતી", "gu-IN-DhwaniNeural", "gu-IN-NiranjanNeural", "Gujarati", 2.0, "ધોરણ", "મુખ્ય શબ્દ"),
    "ta": Language("ta", "Tamil", "தமிழ்", "ta-IN-PallaviNeural", "ta-IN-ValluvarNeural", "Tamil", 1.6, "வகுப்பு", "முக்கிய சொல்"),
    "te": Language("te", "Telugu", "తెలుగు", "te-IN-ShrutiNeural", "te-IN-MohanNeural", "Telugu", 1.7, "తరగతి", "ముఖ్య పదం"),
    "kn": Language("kn", "Kannada", "ಕನ್ನಡ", "kn-IN-SapnaNeural", "kn-IN-GaganNeural", "Kannada", 1.7, "ತರಗತಿ", "ಮುಖ್ಯ ಪದ"),
    "ml": Language("ml", "Malayalam", "മലയാളം", "ml-IN-SobhanaNeural", "ml-IN-MidhunNeural", "Malayalam", 1.5, "ക്ലാസ്", "പ്രധാന വാക്ക്"),
    "en": Language("en", "English (Indian)", "English", "en-IN-NeerjaNeural", "en-IN-PrabhatNeural", "Latin", 2.5, "Class", "Key term"),
}

# Unicode block ranges used by the deterministic language validator.
SCRIPT_RANGES: dict[str, list[tuple[int, int]]] = {
    "Devanagari": [(0x0900, 0x097F), (0xA8E0, 0xA8FF)],
    "Bengali": [(0x0980, 0x09FF)],
    "Gujarati": [(0x0A80, 0x0AFF)],
    "Tamil": [(0x0B80, 0x0BFF)],
    "Telugu": [(0x0C00, 0x0C7F)],
    "Kannada": [(0x0C80, 0x0CFF)],
    "Malayalam": [(0x0D00, 0x0D7F)],
    "Latin": [(0x0041, 0x005A), (0x0061, 0x007A)],
}

# (path, face index) candidates per script: macOS, Linux (fonts-noto), Windows.
_NIRMALA = [("C:/Windows/Fonts/NirmalaB.ttf", 0), ("C:/Windows/Fonts/Nirmala.ttf", 0)]
FONT_CANDIDATES: dict[str, list[tuple[str, int]]] = {
    "Devanagari": [
        ("/System/Library/Fonts/Kohinoor.ttc", 2),  # Kohinoor Devanagari Semibold
        ("/System/Library/Fonts/Supplemental/Devanagari Sangam MN.ttc", 1),
        ("/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf", 0),
        ("/usr/share/fonts/opentype/noto/NotoSansDevanagari-Bold.ttf", 0),
        ("/usr/share/fonts/noto/NotoSansDevanagari-Bold.ttf", 0),
        *_NIRMALA,
    ],
    "Bengali": [("/System/Library/Fonts/KohinoorBangla.ttc", 1), ("/usr/share/fonts/truetype/noto/NotoSansBengali-Bold.ttf", 0), *_NIRMALA],
    "Gujarati": [("/System/Library/Fonts/KohinoorGujarati.ttc", 1), ("/usr/share/fonts/truetype/noto/NotoSansGujarati-Bold.ttf", 0), *_NIRMALA],
    "Telugu": [("/System/Library/Fonts/KohinoorTelugu.ttc", 1), ("/usr/share/fonts/truetype/noto/NotoSansTelugu-Bold.ttf", 0), *_NIRMALA],
    "Tamil": [("/System/Library/Fonts/Supplemental/Tamil Sangam MN.ttc", 1), ("/usr/share/fonts/truetype/noto/NotoSansTamil-Bold.ttf", 0), *_NIRMALA],
    "Kannada": [("/System/Library/Fonts/Supplemental/Kannada Sangam MN.ttc", 1), ("/usr/share/fonts/truetype/noto/NotoSansKannada-Bold.ttf", 0), *_NIRMALA],
    "Malayalam": [("/System/Library/Fonts/Supplemental/Malayalam Sangam MN.ttc", 1), ("/usr/share/fonts/truetype/noto/NotoSansMalayalam-Bold.ttf", 0), *_NIRMALA],
    "Latin": [
        ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 0),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0),
        ("C:/Windows/Fonts/arialbd.ttf", 0),
    ],
    # Wide-coverage fallback for characters the primary font lacks (subscripts, symbols).
    "Fallback": [
        ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 0),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
        ("C:/Windows/Fonts/seguisym.ttf", 0),
    ],
}


def find_font(script: str) -> tuple[str, int]:
    override = os.environ.get("EDUVIDEO_FONT")
    if override:
        path, _, idx = override.partition(":")
        return path, int(idx or 0)
    for path, idx in FONT_CANDIDATES.get(script, []) + FONT_CANDIDATES["Latin"]:
        if Path(path).exists():
            return path, idx
    raise FileNotFoundError(
        f"No font found for script {script!r}. Install Noto Sans (e.g. `apt install fonts-noto-core`) "
        "or set EDUVIDEO_FONT=/path/to/font.ttf"
    )


def find_fallback_font() -> tuple[str, int] | None:
    for path, idx in FONT_CANDIDATES["Fallback"]:
        if Path(path).exists():
            return path, idx
    return None


# Model routing presets. The reviewer is always a different model family from the writer,
# so the critic does not share the generator's blind spots. Any role can be overridden
# with EDUVIDEO_<ROLE>_MODEL.
MODEL_PRESETS: dict[str, dict[str, str]] = {
    # highest quality: frontier writer, independent frontier reviewer, pro-tier vision/audio/image
    "best": {
        "planner": "anthropic/claude-opus-5.5",
        "writer": "anthropic/claude-opus-5.5",
        "reviewer": "openai/gpt-5.5",
        "vision": "google/gemini-3.1-pro-preview",
        "audio": "google/gemini-3.1-pro-preview",
        "image": "google/gemini-3-pro-image",
    },
    # ~3x cheaper and faster, for iteration
    "fast": {
        "planner": "google/gemini-3.8-flash",
        "writer": "google/gemini-3.8-flash",
        "reviewer": "anthropic/claude-sonnet-5",
        "vision": "google/gemini-3.8-flash",
        "audio": "google/gemini-3.8-flash",
        "image": "google/gemini-3.1-flash-image",
    },
}
ROLES = ("planner", "writer", "reviewer", "vision", "audio", "image")


@dataclass
class Settings:
    api_key: str = field(default_factory=lambda: os.environ.get("OPENROUTER_API_KEY", ""))
    base_url: str = field(default_factory=lambda: _env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"))

    models: str = field(default_factory=lambda: _env("EDUVIDEO_MODELS", "best"))
    planner_model: str = ""
    writer_model: str = ""
    reviewer_model: str = ""
    vision_model: str = ""
    audio_model: str = ""
    image_model: str = ""
    image_max_tokens: int = field(default_factory=lambda: int(_env("EDUVIDEO_IMAGE_MAX_TOKENS", "8000")))

    # Narration engine: "elevenlabs" (whole-lesson expressive narration), "edge" (free neural TTS),
    # or "auto" = ElevenLabs when a key is configured and the language has a voice, else edge-tts.
    tts: str = field(default_factory=lambda: _env("EDUVIDEO_TTS", "auto"))
    elevenlabs_key: str = field(default_factory=lambda: os.environ.get("ELEVENLABS_API_KEY", ""))
    el_model: str = field(default_factory=lambda: _env("EDUVIDEO_EL_MODEL", "eleven_v3"))
    el_alt_model: str = "eleven_multilingual_v2"
    el_speed: float = field(default_factory=lambda: float(_env("EDUVIDEO_EL_SPEED", "1.15")))

    # Video contract
    min_seconds: float = 30.0
    max_seconds: float = 60.0
    target_seconds: float = 50.0
    width: int = 1280
    height: int = 720
    fps: int = 25

    # Timeline padding (seconds)
    lead_in: float = 2.0    # handwritten topic title card before the first narration
    scene_gap: float = 0.45 # silence between scenes; crossfade happens here
    tail: float = 1.0

    # Agent loop budgets
    max_script_rounds: int = 3
    max_image_attempts: int = 3
    max_tts_attempts: int = 3
    max_duration_rounds: int = 3
    max_final_qa_rounds: int = 1

    asr_check: bool = True
    generate_images: bool = True
    max_workers: int = 5
    max_concurrent_requests: int = field(default_factory=lambda: int(_env("EDUVIDEO_MAX_CONCURRENCY", "2")))
    # Hard cost ceiling for one run; regeneration loops also check it before spending.
    max_cost_usd: float = field(default_factory=lambda: float(_env("EDUVIDEO_MAX_COST_USD", "4.0")))

    def __post_init__(self) -> None:
        self.use_models(self.models)

    def use_models(self, preset: str) -> None:
        if preset not in MODEL_PRESETS:
            raise ValueError(f"unknown model preset {preset!r}; choose from {sorted(MODEL_PRESETS)}")
        self.models = preset
        for role in ROLES:
            setattr(self, f"{role}_model", os.environ.get(f"EDUVIDEO_{role.upper()}_MODEL", MODEL_PRESETS[preset][role]))

    def model_table(self) -> dict[str, str]:
        return {role: getattr(self, f"{role}_model") for role in ROLES}

    def use_elevenlabs(self, lang: Language) -> bool:
        if self.tts == "edge":
            return False
        return bool(self.elevenlabs_key and lang.el_voice)

    def speaking_rate(self, lang: Language) -> float:
        """Words per second of the narration engine, used to budget script length."""
        return lang.el_words_per_sec if self.use_elevenlabs(lang) else lang.words_per_sec
