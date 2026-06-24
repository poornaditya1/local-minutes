from __future__ import annotations

import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from local_minutes.lmstudio import DEFAULT_BASE_URL, list_models  # noqa: E402
from local_minutes.recorder import SessionRecorder, default_sample_rate  # noqa: E402


def main() -> int:
    print("Local Minutes macOS audio check")
    print(f"Platform: {platform.platform()}")
    print(f"Machine: {platform.machine()}")
    print(f"Python: {sys.version.split()[0]}")
    print(f"Recorder sample rate: {default_sample_rate()} Hz")
    print()

    try:
        devices = SessionRecorder.list_devices()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not list audio devices: {exc}")
        return 1

    warnings = devices.get("warnings") or []
    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"  - {warning}")
        print()

    print("Input devices:")
    inputs = devices.get("inputs") or []
    if not inputs:
        print("  No input devices were detected.")
    for item in inputs:
        badges = []
        if item.get("is_default_input"):
            badges.append("default")
        if item.get("recommended_system"):
            badges.append("recommended system audio")
        if item.get("likely_loopback") and not item.get("recommended_system"):
            badges.append("possible loopback")
        suffix = f" [{', '.join(badges)}]" if badges else ""
        print(f"  - {item['name']} ({item['kind']}){suffix}")

    print()
    try:
        models = list_models(DEFAULT_BASE_URL)
        print(f"LM Studio reachable at {DEFAULT_BASE_URL}")
        print(f"Loaded/listed models: {', '.join(models) if models else '(none listed)'}")
    except Exception as exc:  # noqa: BLE001
        print(f"LM Studio not reachable at {DEFAULT_BASE_URL}: {exc}")
        print("Start LM Studio's Local Server before generating notes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
