from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
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
    return all_segments


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
