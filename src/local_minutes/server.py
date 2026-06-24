from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .lmstudio import DEFAULT_BASE_URL, LMStudioError, generate_meeting_notes, list_models
from .recorder import RecordingPaths, SessionRecorder
from .transcribe import save_transcript, transcript_to_text, transcribe_audio_files

ROOT_DIR = Path(__file__).resolve().parents[2]
STATIC_DIR = ROOT_DIR / "static"
DATA_DIR = Path(os.getenv("LOCAL_MINUTES_DATA_DIR", ROOT_DIR / "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Local Minutes", version="0.2.0-macos")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
recorder = SessionRecorder(DATA_DIR)


class StartRecordingRequest(BaseModel):
    mic_device: Optional[str] = None
    system_device: Optional[str] = None
    meeting_title: str = "Untitled meeting"
    manual_notes: str = ""
    lmstudio_base_url: str = os.getenv("LOCAL_MINUTES_LMSTUDIO_BASE_URL", DEFAULT_BASE_URL)
    lmstudio_model: str = Field(
        default=os.getenv("LOCAL_MINUTES_LMSTUDIO_MODEL", ""),
        description="Model ID currently loaded in LM Studio.",
    )
    whisper_model: str = Field(
        default=os.getenv("LOCAL_MINUTES_WHISPER_MODEL", "small"),
        description="faster-whisper model size or path, such as tiny, base, small, medium, large-v3",
    )
    mic_gain: float = Field(default=1.0, ge=0.1, le=4.0)
    system_gain: float = Field(default=1.0, ge=0.1, le=4.0)


@dataclass
class JobState:
    job_id: str
    session_id: str
    meeting_title: str
    manual_notes: str
    lmstudio_base_url: str
    lmstudio_model: str
    whisper_model: str
    status: str = "recording"
    message: str = "Recording in progress."
    created_at_epoch: float = field(default_factory=time.time)
    updated_at_epoch: float = field(default_factory=time.time)
    session_dir: Optional[Path] = None
    transcript_md: Optional[str] = None
    notes_md: Optional[str] = None
    files: Dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "job_id": self.job_id,
            "session_id": self.session_id,
            "meeting_title": self.meeting_title,
            "status": self.status,
            "message": self.message,
            "created_at_epoch": self.created_at_epoch,
            "updated_at_epoch": self.updated_at_epoch,
            "transcript_md": self.transcript_md,
            "notes_md": self.notes_md,
            "files": self.files,
            "error": self.error,
        }


_jobs: Dict[str, JobState] = {}
_jobs_lock = threading.Lock()
_active_job_id: Optional[str] = None


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/devices")
def api_devices() -> Dict[str, object]:
    try:
        return recorder.list_devices()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/recorder-status")
def api_recorder_status() -> Dict[str, object]:
    return recorder.status()


@app.get("/api/lmstudio-models")
def api_lmstudio_models(base_url: str = DEFAULT_BASE_URL) -> Dict[str, object]:
    base_url = (base_url or "").strip() or os.getenv("LOCAL_MINUTES_LMSTUDIO_BASE_URL", DEFAULT_BASE_URL)
    try:
        return {"models": list_models(base_url)}
    except LMStudioError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/start")
def api_start(req: StartRecordingRequest) -> Dict[str, object]:
    global _active_job_id
    with _jobs_lock:
        if _active_job_id:
            raise HTTPException(status_code=409, detail="A recording job is already active.")
        req_model = req.lmstudio_model.strip() or os.getenv("LOCAL_MINUTES_LMSTUDIO_MODEL", "").strip()
        if not req_model:
            raise HTTPException(status_code=400, detail="Select or type an LM Studio model ID before starting.")

    try:
        session_id = recorder.start(req.mic_device, req.system_device, mic_gain=req.mic_gain, system_gain=req.system_gain)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job = JobState(
        job_id=session_id,
        session_id=session_id,
        meeting_title=req.meeting_title.strip() or "Untitled meeting",
        manual_notes=req.manual_notes,
        lmstudio_base_url=req.lmstudio_base_url.strip() or os.getenv("LOCAL_MINUTES_LMSTUDIO_BASE_URL", DEFAULT_BASE_URL),
        lmstudio_model=req_model,
        whisper_model=req.whisper_model.strip() or os.getenv("LOCAL_MINUTES_WHISPER_MODEL", "small"),
    )
    with _jobs_lock:
        _jobs[job.job_id] = job
        _active_job_id = job.job_id
    return {"job": job.to_dict()}


@app.post("/api/stop")
def api_stop() -> Dict[str, object]:
    global _active_job_id
    with _jobs_lock:
        job_id = _active_job_id
        if not job_id or job_id not in _jobs:
            raise HTTPException(status_code=409, detail="No active recording job.")
        job = _jobs[job_id]

    try:
        paths = recorder.stop()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _update_job(job_id, status="processing", message="Transcribing audio locally with faster-whisper.", session_dir=paths.session_dir)
    with _jobs_lock:
        _active_job_id = None

    t = threading.Thread(target=_process_job, args=(job, paths), daemon=True)
    t.start()
    return {"job": job.to_dict()}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str) -> Dict[str, object]:
    with _jobs_lock:
        if job_id not in _jobs:
            raise HTTPException(status_code=404, detail="Job not found.")
        return {"job": _jobs[job_id].to_dict()}


@app.get("/api/jobs")
def api_jobs() -> Dict[str, object]:
    with _jobs_lock:
        return {"jobs": [job.to_dict() for job in sorted(_jobs.values(), key=lambda j: j.created_at_epoch, reverse=True)]}


@app.get("/api/download/{job_id}/{filename}")
def api_download(job_id: str, filename: str) -> FileResponse:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job or not job.session_dir:
        raise HTTPException(status_code=404, detail="Job not found.")
    safe_names = {"notes.md", "transcript.md", "segments.json", "recording_metadata.json", "mic.wav", "system.wav"}
    if filename not in safe_names:
        raise HTTPException(status_code=400, detail="File is not downloadable.")
    path = job.session_dir / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(path, filename=filename)


def _process_job(job: JobState, paths: RecordingPaths) -> None:
    try:
        _update_job(job.job_id, status="processing", message="Transcribing audio locally with faster-whisper.")
        segments = transcribe_audio_files(paths.audio_files, whisper_model=job.whisper_model)
        transcript_md_path, segments_json_path = save_transcript(paths.session_dir, segments)
        transcript_md = transcript_md_path.read_text(encoding="utf-8")
        transcript_text = transcript_to_text(segments)

        if not transcript_text.strip():
            raise RuntimeError("No speech was detected. On macOS, check microphone permission and confirm your system audio device is BlackHole, Loopback, VB-Cable, or an aggregate device receiving meeting audio.")

        _update_job(
            job.job_id,
            status="processing",
            message="Creating meeting notes with LM Studio.",
            transcript_md=transcript_md,
            files={
                "transcript": f"/api/download/{job.job_id}/transcript.md",
                "segments": f"/api/download/{job.job_id}/segments.json",
                "metadata": f"/api/download/{job.job_id}/recording_metadata.json",
                **{label: f"/api/download/{job.job_id}/{label}.wav" for label in paths.audio_files.keys()},
            },
        )

        notes_md = generate_meeting_notes(
            transcript_text=transcript_text,
            manual_notes=job.manual_notes,
            meeting_title=job.meeting_title,
            model=job.lmstudio_model,
            base_url=job.lmstudio_base_url,
        )
        notes_path = paths.session_dir / "notes.md"
        notes_path.write_text(notes_md + "\n", encoding="utf-8")

        _update_job(
            job.job_id,
            status="done",
            message="Meeting notes are ready.",
            notes_md=notes_md,
            files={
                "notes": f"/api/download/{job.job_id}/notes.md",
                "transcript": f"/api/download/{job.job_id}/transcript.md",
                "segments": f"/api/download/{job.job_id}/segments.json",
                "metadata": f"/api/download/{job.job_id}/recording_metadata.json",
                **{label: f"/api/download/{job.job_id}/{label}.wav" for label in paths.audio_files.keys()},
            },
        )
    except Exception as exc:  # noqa: BLE001
        error_path = paths.session_dir / "error.json"
        error_path.write_text(json.dumps({"error": str(exc)}, indent=2), encoding="utf-8")
        _update_job(job.job_id, status="failed", message="Processing failed.", error=str(exc))


def _update_job(job_id: str, **updates: object) -> None:
    with _jobs_lock:
        job = _jobs[job_id]
        for key, value in updates.items():
            if key == "files" and isinstance(value, dict):
                job.files.update(value)
            elif hasattr(job, key):
                setattr(job, key, value)
        job.updated_at_epoch = time.time()


def main() -> None:
    host = os.getenv("LOCAL_MINUTES_HOST", "127.0.0.1")
    port = int(os.getenv("LOCAL_MINUTES_PORT", "8765"))
    uvicorn.run("local_minutes.server:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
