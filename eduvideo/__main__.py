"""CLI entry point.

    uv run eduvideo "Class 7 → Science → Photosynthesis"
    uv run eduvideo --grade 7 --subject Science --topic Photosynthesis --lang hi
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from .agents.base import Brief
from .config import LANGUAGES, Settings
from .llm import LLMError


def parse_topic(text: str) -> tuple[str, str, str]:
    parts = [p.strip() for p in re.split(r"\s*(?:→|->|>|\||/)\s*", text) if p.strip()]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError('expected "Class 7 → Science → Photosynthesis"')
    grade = re.sub(r"(?i)^(class|grade|std\.?)\s*", "", parts[0]).strip()
    return grade, parts[1], parts[2]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="eduvideo", description="Agentic educational video generator")
    ap.add_argument("input", nargs="?", help='e.g. "Class 7 → Science → Photosynthesis"')
    ap.add_argument("--grade")
    ap.add_argument("--subject")
    ap.add_argument("--topic")
    ap.add_argument("--lang", default="hi", choices=sorted(LANGUAGES), help="narration language (default: hi)")
    ap.add_argument("--seconds", type=float, default=50, help="target length, 30-60 (default 50)")
    ap.add_argument("--tts", choices=["auto", "elevenlabs", "edge"], default=None,
                    help="narration engine (default auto: ElevenLabs if ELEVENLABS_API_KEY is set, else edge-tts)")
    ap.add_argument("--out", default="output", help="output root directory")
    ap.add_argument("--resume", help="resume an existing run directory (reuses plan/script/images/audio)")
    ap.add_argument("--models", choices=["best", "fast"], default=None,
                    help="model preset: best (default, highest quality) or fast (~3x cheaper)")
    ap.add_argument("--no-images", action="store_true", help="skip image generation; render typographic slides")
    ap.add_argument("--no-asr-check", action="store_true", help="skip the ASR round-trip audio check")
    args = ap.parse_args(argv)

    if args.input:
        grade, subject, topic = parse_topic(args.input)
    elif args.grade and args.subject and args.topic:
        grade, subject, topic = args.grade, args.subject, args.topic
    else:
        ap.error('give "Class 7 → Science → Photosynthesis" or --grade/--subject/--topic')
    if not 30 <= args.seconds <= 60:
        ap.error("--seconds must be between 30 and 60")

    settings = Settings()
    if args.models:
        settings.use_models(args.models)
    settings.target_seconds = args.seconds
    if args.tts:
        settings.tts = args.tts
    settings.generate_images = not args.no_images
    settings.asr_check = not args.no_asr_check
    brief = Brief(grade=grade, subject=subject, topic=topic, lang=LANGUAGES[args.lang])

    if args.resume:
        run_dir = Path(args.resume)
    else:
        slug = re.sub(r"[^a-z0-9]+", "-", f"class{grade}-{subject}-{topic}".lower()).strip("-")
        run_dir = Path(args.out) / f"{slug}-{args.lang}-{time.strftime('%Y%m%d-%H%M%S')}"

    from .orchestrator import Orchestrator
    try:
        video = Orchestrator(settings, brief, run_dir).run()
    except LLMError as e:
        print(f"\n✖ API error: {e}\n  Partial results are in {run_dir}; rerun with --resume {run_dir}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(f"\n✖ interrupted; resume with --resume {run_dir}", file=sys.stderr)
        return 130
    print(f"\n✔ video: {video}\n  report: {run_dir / 'report.md'}\n  captions: {run_dir / 'captions.srt'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
