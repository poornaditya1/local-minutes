# Local Minutes for macOS

Local Minutes is a local-first meeting notes prototype tuned for Mac. It records your microphone and meeting audio, transcribes locally with `faster-whisper`, then sends the transcript plus your rough notes to LM Studio's local OpenAI-compatible server to produce meeting minutes.

The app is intentionally bot-free. It does not join Zoom, Teams, Meet, or Webex. It listens to audio devices on your Mac, which is closer to the AI notepad workflow used by apps such as Granola.

## What changed for Mac

- Defaults to 48 kHz recording on macOS for better compatibility with meeting apps and virtual audio devices.
- Streams WAV files directly to disk during recording, so long meetings do not build up in memory.
- Detects common macOS virtual devices such as BlackHole, Loopback, VB-Cable, Soundflower, and aggregate inputs.
- Shows live mic and system audio levels so you can confirm both streams are being captured before you finish a meeting.
- Defaults the Whisper model to `small`, which is a better starting point on Apple Silicon than `base` while still being practical.
- Adds macOS setup scripts and a device diagnostic script.

## Requirements

- macOS on Apple Silicon or Intel.
- Python 3.10 or newer.
- uv package manager.
- LM Studio with a chat model loaded and the Local Server started.
- A virtual audio device for system audio, unless you only want microphone transcription.

Recommended virtual audio devices:

- BlackHole 2ch, free and open source.
- Loopback by Rogue Amoeba, paid and polished.
- VB-Cable, another virtual audio cable option.

## Install on macOS

From Terminal:

```bash
cd local-minutes
chmod +x scripts/setup_macos.sh run_macos.command
./scripts/setup_macos.sh
```

Then run:

```bash
./run_macos.command
```

Open this in your browser:

```text
http://127.0.0.1:8765
```

You can also run it manually:

```bash
cd local-minutes
uv venv .venv
uv sync
uv run python run.py
```

## Start LM Studio

1. Open LM Studio.
2. Download a chat or instruct model.
3. Load the model.
4. Open the Local Server tab and start the server.
5. Keep the app URL as `http://localhost:1234/v1`, unless your LM Studio server uses a different URL.
6. In Local Minutes, click Refresh next to the LM Studio model field.

## macOS system audio setup with BlackHole

This is the recommended free setup.

1. Install BlackHole 2ch from the official BlackHole site.
2. Open **Audio MIDI Setup** on your Mac.
3. Click the plus button and create a **Multi-Output Device**.
4. Select your headphones or speakers and **BlackHole 2ch** inside that Multi-Output Device.
5. Set the Multi-Output Device as your Mac's output device.
6. In Local Minutes, set **Microphone** to your normal microphone.
7. Set **System audio** to **BlackHole 2ch**.
8. Start a recording and confirm the live diagnostics show movement for both `mic` and `system`.

Use headphones when possible. It reduces echo and prevents your microphone from re-recording meeting audio from speakers.

## Permissions on macOS

The first time you record, macOS may ask for microphone permission. Approve permission for the app you are running from, usually Terminal, Python, VS Code, PyCharm, or another IDE.

To check or fix permissions:

1. Open **System Settings**.
2. Go to **Privacy & Security**.
3. Open **Microphone**.
4. Enable Terminal, Python, or your IDE.
5. Restart the Local Minutes server.

## Device diagnostics

After setup, run:

```bash
source .venv/bin/activate
python scripts/macos_audio_check.py
```

The script prints detected input devices and flags likely system-audio candidates. If it does not list BlackHole, Loopback, VB-Cable, Soundflower, or an aggregate input, your virtual audio driver is not visible to Python yet.

## Using the app

1. Start LM Studio and load a model.
2. Start Local Minutes with `./run_macos.command` or `python run.py`.
3. Enter the meeting title and optional rough notes.
4. Click Refresh for LM Studio models.
5. Choose your microphone.
6. Choose BlackHole, Loopback, VB-Cable, or an aggregate input for system audio.
7. Click Start recording.
8. Watch the live diagnostics. Both `mic` and `system` should show RMS values above silence when people are talking.
9. Click Stop and generate notes.
10. Download `notes.md`, `transcript.md`, `segments.json`, and the WAV files if needed.

## Output files

Each session is saved under:

```text
data/<session-id>/
```

Files include:

```text
notes.md
transcript.md
segments.json
recording_metadata.json
mic.wav
system.wav
```

## Environment variables

```bash
# Store sessions elsewhere
export LOCAL_MINUTES_DATA_DIR=/path/to/meeting-data

# Run server on another port
export LOCAL_MINUTES_PORT=8765

# Override recording sample rate. macOS defaults to 48000.
export LOCAL_MINUTES_SAMPLE_RATE=48000

# faster-whisper device options
export LOCAL_MINUTES_WHISPER_DEVICE=cpu
export LOCAL_MINUTES_WHISPER_COMPUTE=int8
```

## Notes about native macOS capture

macOS has native APIs for screen and audio capture, but using them cleanly normally means building a signed native Mac app with the right permissions flow. This Python prototype uses the virtual-audio-device approach because it is simple, reliable, and easy to run locally from Terminal.

## Limitations

- It does not identify individual remote speakers. It separates your mic from system audio only.
- It does not join meetings as a bot.
- It does not use native ScreenCaptureKit or Core Audio process taps.
- Quality depends heavily on virtual audio routing and microphone permissions.
- Production use should add authentication, encryption at rest, retention controls, audit logs, and enterprise consent controls.
