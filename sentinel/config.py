"""Sentinel configuration loader with validation and hot-reload."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "model": {
        "path": "models/yolov8n.pt",
        "confidence": 0.35,
        "max_detect_fps": 5,
    },
    "motion": {
        "global_threshold": 0.005,
        "per_camera": {},
    },
    "zones": {},
    "alerts": {
        "cooldown_seconds": {
            "info": 30,
            "warning": 15,
            "critical": 5,
        },
        "macos_notifications": True,
        "websocket": True,
    },
    "recording": {
        "ring_buffer_seconds": 5,
        "save_snapshots": True,
        "save_clips": True,
        "data_dir": "data",
    },
    "behavior": {
        "linger_threshold_seconds": {
            "person": 15,
            "car": 30,
            "default": 20,
        },
    },
    "daynight": {
        "latitude": 40.7,
        "longitude": -74.0,
        "night_start_hour": 20,
        "night_end_hour": 6,
        "brightness_threshold": 0.3,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base, returning new dict."""
    result = base.copy()
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def load_config(path: str = "sentinel_config.json") -> dict[str, Any]:
    """Load config from JSON file, merging with defaults."""
    if not os.path.exists(path):
        print(f"[SENTINEL] Config not found at {path}, using defaults")
        return DEFAULT_CONFIG.copy()

    try:
        with open(path, "r") as f:
            user_config = json.load(f)
        merged = _deep_merge(DEFAULT_CONFIG, user_config)
        print(f"[SENTINEL] Config loaded from {path}")
        return merged
    except (json.JSONDecodeError, OSError) as e:
        print(f"[SENTINEL] Config load error: {e}, using defaults")
        return DEFAULT_CONFIG.copy()


def validate_config(config: dict[str, Any]) -> list[str]:
    """Validate config structure, return list of warnings."""
    warnings: list[str] = []

    model = config.get("model", {})
    if not isinstance(model.get("confidence", 0), (int, float)):
        warnings.append("model.confidence must be a number")
    elif not (0.0 < model.get("confidence", 0.35) < 1.0):
        warnings.append("model.confidence should be between 0 and 1")

    motion = config.get("motion", {})
    thresh = motion.get("global_threshold", 0)
    if not isinstance(thresh, (int, float)) or thresh < 0:
        warnings.append("motion.global_threshold must be a positive number")

    zones = config.get("zones", {})
    for cam_key, cam_zones in zones.items():
        if not isinstance(cam_zones, list):
            warnings.append(f"zones.{cam_key} must be a list")
            continue
        for i, z in enumerate(cam_zones):
            if "name" not in z:
                warnings.append(f"zones.{cam_key}[{i}] missing 'name'")
            if "polygon" not in z:
                warnings.append(f"zones.{cam_key}[{i}] missing 'polygon'")
            elif not isinstance(z["polygon"], list) or len(z["polygon"]) < 3:
                warnings.append(f"zones.{cam_key}[{i}] polygon needs >= 3 points")

    return warnings


class ConfigWatcher:
    """Watch config file for changes and reload automatically."""

    def __init__(self, path: str, callback: Any):
        self.path = os.path.abspath(path)
        self.callback = callback
        self._last_mtime: float = 0.0
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._last_mtime = self._get_mtime()
        self._thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._thread.start()
        print(f"[SENTINEL] Config watcher started: {self.path}")

    def stop(self) -> None:
        self._running = False

    def _get_mtime(self) -> float:
        try:
            return os.path.getmtime(self.path)
        except OSError:
            return 0.0

    def _watch_loop(self) -> None:
        while self._running:
            time.sleep(2.0)
            mtime = self._get_mtime()
            if mtime > self._last_mtime:
                self._last_mtime = mtime
                print(f"[SENTINEL] Config changed, reloading...")
                try:
                    new_config = load_config(self.path)
                    warns = validate_config(new_config)
                    for w in warns:
                        print(f"[SENTINEL] Config warning: {w}")
                    self.callback(new_config)
                except Exception as e:
                    print(f"[SENTINEL] Config reload error: {e}")
