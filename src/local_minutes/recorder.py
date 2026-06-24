from __future__ import annotations

import json
import os
import platform
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import soundcard as sc
import soundfile as sf


def default_sample_rate() -> int:
    """Use a macOS-friendly default, while keeping Whisper-friendly rates elsewhere."""
    env_value = os.getenv("LOCAL_MINUTES_SAMPLE_RATE")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass
    return 48000 if platform.system() == "Darwin" else 16000


@dataclass
class RecordingPaths:
    session_id: str
    session_dir: Path
    started_at_epoch: float
    stopped_at_epoch: float
    audio_files: Dict[str, Path]
    metadata_path: Path


@dataclass
class _StreamState:
    label: str
    device_name: str
    wav_path: Path
    gain: float = 1.0
    error: Optional[str] = None
    frames: int = 0
    chunks_seen: int = 0
    last_rms_dbfs: Optional[float] = None
    last_peak_dbfs: Optional[float] = None
    last_update_epoch: Optional[float] = None


class SessionRecorder:
    """Records mic and system/loopback audio into separate WAV files.

    macOS optimization notes:
    - Use a 48 kHz default sample rate because virtual devices such as BlackHole
      and most meeting apps commonly run at 48 kHz.
    - Stream WAV files directly to disk so long meetings do not sit in memory.
    - Surface per-stream levels so BlackHole/Loopback routing problems are obvious.
    """

    def __init__(self, data_dir: Path, sample_rate: Optional[int] = None, block_seconds: float = 0.5):
        self.data_dir = data_dir
        self.sample_rate = sample_rate or default_sample_rate()

        # `soundcard` uses `blocksize` for the low-level CoreAudio buffer size.
        # On many macOS devices, including BlackHole, CoreAudio only accepts a
        # small blocksize such as 15-512 frames. The old code passed 24,000
        # frames at 48 kHz, which caused: "blocksize must be between 15.0 and 512".
        # Keep the app-level recording chunk at about 0.5 seconds, but request a
        # safe CoreAudio block size separately.
        self.record_numframes = max(1024, int(self.sample_rate * block_seconds))
        self.soundcard_blocksize = _soundcard_blocksize_from_env()

        self._stop_event = threading.Event()
        self._threads: List[threading.Thread] = []
        self._streams: Dict[str, _StreamState] = {}
        self._lock = threading.Lock()
        self._active_session_id: Optional[str] = None
        self._session_dir: Optional[Path] = None
        self._started_at_epoch: Optional[float] = None

    @staticmethod
    def list_devices() -> Dict[str, object]:
        system_name = platform.system()
        inputs = []
        seen = set()
        default_input_name = None
        try:
            default_input_name = str(sc.default_microphone().name)
        except Exception:  # noqa: BLE001 - device probing must stay best-effort.
            default_input_name = None

        for mic in sc.all_microphones(include_loopback=True):
            name = str(mic.name)
            if name in seen:
                continue
            seen.add(name)
            info = _classify_input_device(name, default_input_name)
            inputs.append(info)

        # Prefer common macOS virtual loopback devices near the top, then default mic.
        inputs.sort(key=lambda d: (
            0 if d.get("recommended_system") else 1,
            0 if d.get("is_default_input") else 1,
            str(d.get("name", "")).lower(),
        ))

        speakers = []
        seen_speakers = set()
        for speaker in sc.all_speakers():
            name = str(speaker.name)
            if name in seen_speakers:
                continue
            seen_speakers.add(name)
            speakers.append({"id": name, "name": name, "kind": "speaker/output"})

        warnings: List[str] = []
        if system_name == "Darwin":
            has_virtual = any(item.get("recommended_system") for item in inputs)
            if not has_virtual:
                warnings.append(
                    "No obvious macOS loopback input was detected. Install BlackHole, Loopback, VB-Cable, or another virtual audio device, then refresh devices."
                )

        return {
            "platform": system_name,
            "sample_rate": default_sample_rate(),
            "inputs": inputs,
            "speakers": speakers,
            "warnings": warnings,
            "macos_help": system_name == "Darwin",
        }

    def start(
        self,
        mic_device: Optional[str],
        system_device: Optional[str],
        *,
        mic_gain: float = 1.0,
        system_gain: float = 1.0,
    ) -> str:
        with self._lock:
            if self._active_session_id is not None:
                raise RuntimeError("A recording session is already running.")
            if not mic_device and not system_device:
                raise ValueError("Select at least one microphone or system audio device.")

            session_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
            session_dir = self.data_dir / session_id
            session_dir.mkdir(parents=True, exist_ok=True)

            self._active_session_id = session_id
            self._session_dir = session_dir
            self._started_at_epoch = time.time()
            self._stop_event.clear()
            self._threads = []
            self._streams = {}

            if mic_device:
                self._streams["mic"] = _StreamState(
                    label="mic",
                    device_name=mic_device,
                    wav_path=session_dir / "mic.wav",
                    gain=_safe_gain(mic_gain),
                )
            if system_device and system_device != mic_device:
                self._streams["system"] = _StreamState(
                    label="system",
                    device_name=system_device,
                    wav_path=session_dir / "system.wav",
                    gain=_safe_gain(system_gain),
                )

            for stream_state in self._streams.values():
                t = threading.Thread(target=self._capture_worker, args=(stream_state,), daemon=True)
                t.start()
                self._threads.append(t)

            return session_id

    def stop(self) -> RecordingPaths:
        with self._lock:
            if self._active_session_id is None or self._started_at_epoch is None or self._session_dir is None:
                raise RuntimeError("No recording session is running.")
            session_id = self._active_session_id
            session_dir = self._session_dir
            started_at = self._started_at_epoch

        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=10)

        stopped_at = time.time()
        audio_files: Dict[str, Path] = {}
        metadata = {
            "session_id": session_id,
            "started_at_epoch": started_at,
            "stopped_at_epoch": stopped_at,
            "sample_rate": self.sample_rate,
            "record_numframes": self.record_numframes,
            "soundcard_blocksize": self.soundcard_blocksize,
            "platform": platform.platform(),
            "streams": {},
        }

        with self._lock:
            streams = dict(self._streams)
            self._active_session_id = None
            self._session_dir = None
            self._started_at_epoch = None
            self._threads = []
            self._streams = {}

        for label, stream_state in streams.items():
            path = stream_state.wav_path
            metadata["streams"][label] = {
                "device_name": stream_state.device_name,
                "frames": stream_state.frames,
                "seconds": round(stream_state.frames / self.sample_rate, 2) if self.sample_rate else 0,
                "chunks_seen": stream_state.chunks_seen,
                "gain": stream_state.gain,
                "last_rms_dbfs": stream_state.last_rms_dbfs,
                "last_peak_dbfs": stream_state.last_peak_dbfs,
                "error": stream_state.error,
                "wav_path": str(path),
            }
            if path.exists() and path.stat().st_size > 44 and stream_state.frames > 0:
                audio_files[label] = path

        metadata_path = session_dir / "recording_metadata.json"
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        return RecordingPaths(session_id, session_dir, started_at, stopped_at, audio_files, metadata_path)

    def status(self) -> Dict[str, object]:
        with self._lock:
            return {
                "active_session_id": self._active_session_id,
                "started_at_epoch": self._started_at_epoch,
                "sample_rate": self.sample_rate,
                "record_numframes": self.record_numframes,
                "soundcard_blocksize": self.soundcard_blocksize,
                "platform": platform.system(),
                "streams": {
                    label: {
                        "device_name": state.device_name,
                        "frames": state.frames,
                        "error": state.error,
                        "seconds": round(state.frames / self.sample_rate, 1) if self.sample_rate else 0,
                        "last_rms_dbfs": state.last_rms_dbfs,
                        "last_peak_dbfs": state.last_peak_dbfs,
                        "last_update_epoch": state.last_update_epoch,
                    }
                    for label, state in self._streams.items()
                },
            }

    def _capture_worker(self, stream_state: _StreamState) -> None:
        try:
            mic = sc.get_microphone(stream_state.device_name, include_loopback=True)
            with _open_recorder_with_fallbacks(
                mic,
                samplerate=self.sample_rate,
                preferred_blocksize=self.soundcard_blocksize,
            ) as rec:
                with sf.SoundFile(
                    stream_state.wav_path,
                    mode="w",
                    samplerate=self.sample_rate,
                    channels=1,
                    subtype="PCM_16",
                ) as wav:
                    while not self._stop_event.is_set():
                        data = rec.record(numframes=self.record_numframes)
                        if data is None or len(data) == 0:
                            continue
                        audio = self._to_mono(np.asarray(data, dtype=np.float32))
                        if stream_state.gain != 1.0:
                            audio = audio * stream_state.gain
                        audio = np.nan_to_num(audio, nan=0.0, posinf=0.0, neginf=0.0)
                        audio = np.clip(audio, -1.0, 1.0).astype(np.float32)
                        wav.write(audio)
                        rms_dbfs, peak_dbfs = _levels_dbfs(audio)
                        with self._lock:
                            stream_state.frames += int(audio.shape[0])
                            stream_state.chunks_seen += 1
                            stream_state.last_rms_dbfs = rms_dbfs
                            stream_state.last_peak_dbfs = peak_dbfs
                            stream_state.last_update_epoch = time.time()
        except Exception as exc:  # noqa: BLE001 - keep recorder alive and surface issue in UI.
            with self._lock:
                stream_state.error = f"{type(exc).__name__}: {exc}"
                stream_state.last_update_epoch = time.time()

    @staticmethod
    def _to_mono(audio: np.ndarray) -> np.ndarray:
        if audio.ndim == 1:
            return audio
        if audio.shape[1] == 1:
            return audio[:, 0]
        return audio.mean(axis=1)



def _soundcard_blocksize_from_env() -> Optional[int]:
    """Return a CoreAudio-safe block size for SoundCard, or None for default.

    This value is intentionally separate from SessionRecorder.record_numframes.
    - record_numframes controls how much audio we read from SoundCard per loop.
    - soundcard_blocksize controls the low-level device buffer requested from CoreAudio.

    If a device rejects the requested value, _open_recorder_with_fallbacks tries
    several smaller values and finally lets SoundCard use its default.
    """
    env_value = os.getenv("LOCAL_MINUTES_SOUNDCARD_BLOCKSIZE", "512").strip()
    if not env_value or env_value.lower() in {"auto", "default", "none"}:
        return None
    try:
        blocksize = int(float(env_value))
    except ValueError:
        return 512
    return max(15, min(blocksize, 512))


class _SoundCardRecorderWithFallbacks:
    """Context manager that retries SoundCard recorder creation with safe block sizes."""

    def __init__(self, mic: object, *, samplerate: int, preferred_blocksize: Optional[int]):
        self.mic = mic
        self.samplerate = samplerate
        self.preferred_blocksize = preferred_blocksize
        self._ctx = None

    def __enter__(self):  # noqa: ANN001
        candidates: List[Optional[int]] = []
        if self.preferred_blocksize is not None:
            candidates.append(self.preferred_blocksize)
        candidates.extend([512, 256, 128, 64, 32, None])

        seen = set()
        last_exc: Optional[Exception] = None
        for blocksize in candidates:
            key = "default" if blocksize is None else blocksize
            if key in seen:
                continue
            seen.add(key)
            try:
                if blocksize is None:
                    ctx = self.mic.recorder(samplerate=self.samplerate)
                else:
                    ctx = self.mic.recorder(samplerate=self.samplerate, blocksize=blocksize)
                recorder = ctx.__enter__()
                self._ctx = ctx
                return recorder
            except TypeError as exc:
                if "blocksize" not in str(exc).lower():
                    raise
                last_exc = exc
                continue

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Could not open audio recorder.")

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
        if self._ctx is None:
            return False
        return self._ctx.__exit__(exc_type, exc, tb)


def _open_recorder_with_fallbacks(mic: object, *, samplerate: int, preferred_blocksize: Optional[int]):
    """Open a SoundCard recorder while handling macOS blocksize limits.

    Some macOS/CoreAudio devices report tight valid ranges such as 15-512.
    This prevents one invalid blocksize from killing both the microphone and
    BlackHole streams.
    """
    return _SoundCardRecorderWithFallbacks(
        mic,
        samplerate=samplerate,
        preferred_blocksize=preferred_blocksize,
    )

def _classify_input_device(name: str, default_input_name: Optional[str]) -> Dict[str, object]:
    lname = name.lower()
    virtual_terms = [
        "blackhole",
        "loopback",
        "vb-cable",
        "vbcable",
        "soundflower",
        "rogue amoeba",
        "aggregate device",
        "multi-output",
    ]
    generic_loopback_terms = ["monitor", "output", "what u hear", "stereo mix"]
    recommended_system = any(term in lname for term in virtual_terms)
    likely_loopback = recommended_system or any(term in lname for term in generic_loopback_terms)
    is_default_input = bool(default_input_name and name == default_input_name)

    if recommended_system:
        kind = "macOS virtual loopback/system"
    elif likely_loopback:
        kind = "loopback/system"
    else:
        kind = "microphone/input"

    return {
        "id": name,
        "name": name,
        "kind": kind,
        "recommended_system": recommended_system,
        "likely_loopback": likely_loopback,
        "is_default_input": is_default_input,
    }


def _levels_dbfs(audio: np.ndarray) -> tuple[float, float]:
    if audio.size == 0:
        return -120.0, -120.0
    rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))
    peak = float(np.max(np.abs(audio)))
    floor = 1e-6
    return round(20 * np.log10(max(rms, floor)), 1), round(20 * np.log10(max(peak, floor)), 1)


def _safe_gain(value: float) -> float:
    try:
        gain = float(value)
    except Exception:  # noqa: BLE001
        gain = 1.0
    return max(0.1, min(gain, 4.0))
