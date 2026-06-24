from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from faster_whisper import WhisperModel


@dataclass
class TranscriptSegment:
    source: str
    start: float
    end: float
    text: str


_MODEL_CACHE: Dict[Tuple[str, str, str], WhisperModel] = {}


def _get_model(model_name: str) -> WhisperModel:
    device = os.getenv("LOCAL_MINUTES_WHISPER_DEVICE", "cpu")
    compute_type = os.getenv("LOCAL_MINUTES_WHISPER_COMPUTE", "int8")
    key = (model_name, device, compute_type)
    if key not in _MODEL_CACHE:
        _MODEL_CACHE[key] = WhisperModel(model_name, device=device, compute_type=compute_type)
    return _MODEL_CACHE[key]


def transcribe_audio_files(audio_files: Dict[str, Path], whisper_model: str = "base") -> List[TranscriptSegment]:
    """Transcribe each audio source separately and merge by timestamp.

    Source labels are kept so summaries can distinguish the user's mic from system audio.
    Near-duplicate MIC/SYSTEM segments are removed by default because macOS speaker echo
    and meeting app monitoring can cause the same sentence to be captured twice.
    """
    model = _get_model(whisper_model)
    all_segments: List[TranscriptSegment] = []

    for source, path in audio_files.items():
        if not path.exists() or path.stat().st_size == 0:
            continue
        segments, _info = model.transcribe(
            str(path),
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        for seg in segments:
            text = (seg.text or "").strip()
            if text:
                all_segments.append(
                    TranscriptSegment(
                        source=source,
                        start=round(float(seg.start), 2),
                        end=round(float(seg.end), 2),
                        text=text,
                    )
                )

    all_segments.sort(key=lambda s: (s.start, s.source))
    if os.getenv("LOCAL_MINUTES_DEDUP_TRANSCRIPT", "1").strip().lower() not in {"0", "false", "no", "off"}:
        all_segments = _dedupe_near_duplicate_segments(all_segments)
    return all_segments


def _dedupe_near_duplicate_segments(segments: List[TranscriptSegment]) -> List[TranscriptSegment]:
    """Remove near-identical mic/system transcript lines that happen at the same time.

    Default behavior prefers the SYSTEM copy for cross-source duplicates because the duplicate
    usually comes from meeting audio leaking into the laptop microphone. Override with:
        export LOCAL_MINUTES_DEDUP_PREFER=mic
    """
    if len(segments) < 2:
        return segments

    window_seconds = _env_float("LOCAL_MINUTES_DEDUP_WINDOW_SECONDS", 2.5, minimum=0.1, maximum=15.0)
    threshold = _env_float("LOCAL_MINUTES_DEDUP_SIMILARITY", 0.84, minimum=0.5, maximum=1.0)
    preferred = os.getenv("LOCAL_MINUTES_DEDUP_PREFER", "system").strip().lower()
    keep = [True] * len(segments)

    for i, current in enumerate(segments):
        if not keep[i]:
            continue
        current_norm = _normalize_text(current.text)
        if not current_norm:
            continue
        for j in range(i + 1, len(segments)):
            other = segments[j]
            if other.start - current.start > window_seconds:
                break
            if not keep[j]:
                continue
            if current.source == other.source:
                continue
            if not _segments_close_in_time(current, other, window_seconds):
                continue
            other_norm = _normalize_text(other.text)
            if not other_norm:
                continue
            score = SequenceMatcher(None, current_norm, other_norm).ratio()
            if score < threshold:
                continue

            # Decide which duplicate to keep.
            if preferred == "mic":
                keep_i = current.source == "mic" or other.source != "mic"
            else:
                keep_i = current.source == "system" or other.source != "system"

            if keep_i:
                keep[j] = False
            else:
                keep[i] = False
                break

    return [seg for seg, should_keep in zip(segments, keep) if should_keep]


def _segments_close_in_time(a: TranscriptSegment, b: TranscriptSegment, window_seconds: float) -> bool:
    starts_close = abs(a.start - b.start) <= window_seconds
    overlap = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    shortest = max(0.01, min(a.end - a.start, b.end - b.start))
    return starts_close or (overlap / shortest) >= 0.25


def _normalize_text(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def transcript_to_markdown(segments: Iterable[TranscriptSegment]) -> str:
    lines = ["# Transcript", ""]
    for seg in segments:
        lines.append(f"[{_fmt_time(seg.start)} - {_fmt_time(seg.end)}] **{seg.source}:** {seg.text}")
    return "\n\n".join(lines).strip() + "\n"


def transcript_to_text(segments: Iterable[TranscriptSegment]) -> str:
    return "\n".join(
        f"[{_fmt_time(seg.start)} - {_fmt_time(seg.end)}] {seg.source.upper()}: {seg.text}"
        for seg in segments
    )


def save_transcript(session_dir: Path, segments: List[TranscriptSegment]) -> Tuple[Path, Path]:
    md_path = session_dir / "transcript.md"
    json_path = session_dir / "segments.json"
    md_path.write_text(transcript_to_markdown(segments), encoding="utf-8")
    json_path.write_text(json.dumps([asdict(s) for s in segments], indent=2), encoding="utf-8")
    return md_path, json_path


def _fmt_time(seconds: float) -> str:
    total = int(seconds)
    hh = total // 3600
    mm = (total % 3600) // 60
    ss = total % 60
    if hh:
        return f"{hh:02d}:{mm:02d}:{ss:02d}"
    return f"{mm:02d}:{ss:02d}"
