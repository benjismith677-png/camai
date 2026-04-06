"""Sentinel Engine — main AI orchestrator coordinating all detection pipelines."""

from __future__ import annotations

import os
import queue
import threading
import time
import uuid
from typing import Optional

import numpy as np

from sentinel.alerter import AlertDispatcher
from sentinel.behavior import BehaviorAnalyzer
from sentinel.config import ConfigWatcher, load_config, validate_config
from sentinel.daynight import DayNightDetector
from sentinel.db import EventDB
from sentinel.detector import ObjectDetector
from sentinel.models import Alert, Event
from sentinel.motion import MotionDetector
from sentinel.recorder import EventRecorder
from sentinel.tracker import SimpleTracker
from sentinel.ws_server import AlertWSServer
from sentinel.zones import ZoneManager


class SentinelEngine:
    """Main AI engine — coordinates motion detection, YOLO inference,
    tracking, zone checks, behavior analysis, alerting, and recording.

    Frame flow:
        DahuaStream -> on_frame() -> motion gate -> detect queue
        -> detection worker -> track -> zone check -> behavior
        -> alert -> record -> DB
    """

    def __init__(self, config_path: str = "sentinel_config.json"):
        self.config_path = config_path
        self.config = load_config(config_path)

        warnings = validate_config(self.config)
        for w in warnings:
            print(f"[SENTINEL] Config warning: {w}")

        if not self.config.get("enabled", True):
            print("[SENTINEL] Engine DISABLED in config")
            self.running = False
            return

        # Core components
        model_cfg = self.config.get("model", {})
        self.detector: Optional[ObjectDetector] = None  # Lazy-loaded
        self._model_path = model_cfg.get("path", "models/yolov8n.pt")
        self._confidence = model_cfg.get("confidence", 0.35)
        self._max_detect_fps = model_cfg.get("max_detect_fps", 5)

        self.zone_mgr = ZoneManager(self.config.get("zones", {}))
        self.alerter = AlertDispatcher(self.config.get("alerts", {}))
        self.recorder = EventRecorder(
            data_dir=self.config.get("recording", {}).get("data_dir", "data"),
            ring_seconds=self.config.get("recording", {}).get("ring_buffer_seconds", 5),
        )
        self.db = EventDB(
            db_path=os.path.join(
                self.config.get("recording", {}).get("data_dir", "data"),
                "events.db",
            )
        )

        daynight_cfg = self.config.get("daynight", {})
        self.daynight = DayNightDetector(
            latitude=daynight_cfg.get("latitude", 40.7),
            longitude=daynight_cfg.get("longitude", -74.0),
            night_start_hour=daynight_cfg.get("night_start_hour", 20),
            night_end_hour=daynight_cfg.get("night_end_hour", 6),
            brightness_threshold=daynight_cfg.get("brightness_threshold", 0.3),
        )

        # WebSocket server
        self.ws_server = AlertWSServer()

        # Per-camera components
        self.motion_detectors: dict[int, MotionDetector] = {}
        self.trackers: dict[int, SimpleTracker] = {}
        self.behavior_analyzers: dict[int, BehaviorAnalyzer] = {}

        # Detection queue — serializes ANE/GPU access
        self._detect_queue: queue.Queue[tuple[int, np.ndarray, float]] = queue.Queue(maxsize=8)

        # Stats
        self._frames_received: dict[int, int] = {}
        self._frames_processed: dict[int, int] = {}
        self._detections_total = 0
        self._last_detect_time: dict[int, float] = {}

        # Config watcher
        self._config_watcher = ConfigWatcher(config_path, self._on_config_reload)

        self.running = False
        self._stream_refs: dict = {}  # DahuaStream references for frame buffers

    def set_stream_buffers(self, streams: dict) -> None:
        """Store references to DahuaStream objects for frame buffer access.

        Args:
            streams: Dict of channel -> DahuaStream from server.py.
        """
        self._stream_refs = streams
        print(f"[SENTINEL] Stream buffers linked for {len(streams)} channels")

    def _ensure_camera(self, channel: int) -> None:
        """Lazily initialize per-camera components."""
        if channel not in self.motion_detectors:
            motion_cfg = self.config.get("motion", {})
            per_cam = motion_cfg.get("per_camera", {}).get(str(channel), {})
            threshold = per_cam.get("threshold", motion_cfg.get("global_threshold", 0.005))

            self.motion_detectors[channel] = MotionDetector(channel, threshold)
            self.trackers[channel] = SimpleTracker(max_age=5, min_hits=2, iou_threshold=0.3)
            self.behavior_analyzers[channel] = BehaviorAnalyzer(
                self.config.get("behavior", {})
            )
            self._frames_received[channel] = 0
            self._frames_processed[channel] = 0
            self._last_detect_time[channel] = 0

    def _load_detector(self) -> None:
        """Lazy-load YOLO model on first detection request."""
        if self.detector is None:
            print("[SENTINEL] Loading YOLO model...")
            self.detector = ObjectDetector(self._model_path)
            print(f"[SENTINEL] Model loaded ({self.detector._backend})")

    def start(self) -> None:
        """Start the engine and all background threads."""
        self.running = True

        # Start detection worker thread
        threading.Thread(
            target=self._detection_worker, daemon=True, name="sentinel-detect"
        ).start()

        # Start WebSocket server
        self.ws_server.start()

        # Start config watcher
        self._config_watcher.start()

        print("\033[96m[SENTINEL] Engine started — waiting for frames\033[0m")

    def stop(self) -> None:
        """Stop the engine gracefully."""
        self.running = False
        self.ws_server.stop()
        self._config_watcher.stop()
        self.db.close()
        print("[SENTINEL] Engine stopped")

    def on_frame(self, channel: int, frame: np.ndarray, timestamp: float) -> None:
        """Called by DahuaStream for every decoded frame.

        This is the main entry point — called from the stream thread.
        Must be fast: only buffers frame and checks motion.

        Args:
            channel: Camera channel number.
            frame: BGR numpy array (raw, not JPEG).
            timestamp: Frame timestamp.
        """
        if channel not in self.motion_detectors:
            self._ensure_camera(channel)
        self._frames_received[channel] = self._frames_received.get(channel, 0) + 1

        # Rate limit — max 5 detections/sec per camera
        min_interval = 1.0 / self._max_detect_fps
        if timestamp - self._last_detect_time.get(channel, 0) < min_interval:
            return

        self._frames_processed[channel] = self._frames_processed.get(channel, 0) + 1

        # 4. Queue for YOLO detection (don't block stream thread)
        try:
            self._detect_queue.put_nowait((channel, frame.copy(), timestamp))
            self._last_detect_time[channel] = timestamp
        except queue.Full:
            pass  # Drop frame if detection is backed up

    def _detection_worker(self) -> None:
        """Single thread for YOLO inference — serializes ANE/GPU access."""
        while self.running:
            try:
                channel, frame, timestamp = self._detect_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            try:
                self._process_frame(channel, frame, timestamp)
            except Exception as e:
                print(f"\033[91m[SENTINEL] Detection error CH{channel}: {e}\033[0m")

    def _process_frame(self, channel: int, frame: np.ndarray, timestamp: float) -> None:
        """Full detection pipeline for a single frame."""
        # Lazy load model
        self._load_detector()
        assert self.detector is not None

        # 4. YOLO detect
        detections = self.detector.detect(frame, self._confidence)
        self._detections_total += len(detections)

        if detections:
            classes = ", ".join(f"{d.class_name}({d.confidence:.0%})" for d in detections)
            print(f"\033[92m[DETECT] CH{channel}: {classes}\033[0m")

        if not detections:
            return

        # 5. Track
        tracks = self.trackers[channel].update(detections, timestamp)

        # Cleanup stale alert state periodically
        if self._frames_processed.get(channel, 0) % 50 == 0:
            active_ids: dict[int, set[int]] = {}
            for ch, tracker in self.trackers.items():
                active_ids[ch] = {ts.track_id for ts in tracker._tracks.values()}
            self.alerter.cleanup_stale(active_ids)

        if not tracks:
            return

        # 6. Zone check
        zone_hits = self.zone_mgr.check(channel, tracks)

        # 7. For each track in a zone, analyze behavior and alert
        is_night = self.daynight.is_night(frame)
        triggered_zones = set()

        for track, zone in zone_hits:
            # Avoid duplicate alerts for same zone in same frame
            zone_key = (zone.name, track.class_name)
            if zone_key in triggered_zones:
                continue
            triggered_zones.add(zone_key)

            # Behavior analysis
            result = self.behavior_analyzers[channel].analyze(
                track, zone, timestamp, is_night
            )
            behavior = result[0] if result else None
            alert_level = result[1] if result else zone.alert_level

            # 8. Create alert
            alert = Alert(
                id=str(uuid.uuid4()),
                timestamp=timestamp,
                channel=channel,
                zone_name=zone.name,
                alert_level=alert_level,
                class_name=track.class_name,
                track_id=track.track_id,
                confidence=track.confidence,
                behavior=behavior,
            )

            # 9. Record snapshot
            if self.config.get("recording", {}).get("save_snapshots", True):
                try:
                    snapshot = self.recorder.save_snapshot(
                        Event.from_alert(alert),
                        frame,
                        detections,
                        [zone],
                    )
                    alert.snapshot_path = snapshot
                except Exception as e:
                    print(f"[RECORD] Snapshot error: {e}")

            # 10. Dispatch alert (macOS + WS)
            dispatched = self.alerter.dispatch(alert)

            if dispatched:
                # Push to WebSocket
                self.ws_server.push_alert(alert.to_dict())

                # Save clip from stream's ring buffer
                if self.config.get("recording", {}).get("save_clips", True):
                    try:
                        event = Event.from_alert(alert, duration=track.duration)
                        # Use DahuaStream's frame buffer (more complete than recorder's)
                        stream_ref = self._stream_refs.get(channel)
                        if stream_ref and hasattr(stream_ref, "_frame_buffer") and len(stream_ref._frame_buffer) >= 5:
                            clip = self.recorder.save_clip_from_frames(
                                event, channel, list(stream_ref._frame_buffer)
                            )
                        else:
                            clip = self.recorder.save_clip(event, channel)
                        if clip:
                            alert.clip_path = clip
                            # Update event in DB with clip path
                            self.db.update_clip_path(event.id, clip)
                    except Exception as e:
                        print(f"[RECORD] Clip error: {e}")

                # 11. Store in DB
                try:
                    event = Event.from_alert(alert, duration=track.duration)
                    self.db.insert_event(event)
                except Exception as e:
                    print(f"[DB] Insert error: {e}")

    def _on_config_reload(self, new_config: dict) -> None:
        """Called when sentinel_config.json changes."""
        self.config = new_config

        # Update zone manager
        self.zone_mgr = ZoneManager(new_config.get("zones", {}))

        # Update alert config
        alert_cfg = new_config.get("alerts", {})
        self.alerter.cooldown_seconds = alert_cfg.get("cooldown_seconds", {
            "info": 30, "warning": 15, "critical": 5,
        })
        self.alerter.macos_enabled = alert_cfg.get("macos_notifications", True)

        # Update motion thresholds
        for ch, md in self.motion_detectors.items():
            motion_cfg = new_config.get("motion", {})
            per_cam = motion_cfg.get("per_camera", {}).get(str(ch), {})
            md.threshold = per_cam.get("threshold", motion_cfg.get("global_threshold", 0.005))

        # Update behavior analyzers
        behavior_cfg = new_config.get("behavior", {})
        for ch in self.behavior_analyzers:
            self.behavior_analyzers[ch] = BehaviorAnalyzer(behavior_cfg)

        print("\033[96m[SENTINEL] Config reloaded successfully\033[0m")

    def get_status(self) -> dict:
        """Return engine status for API."""
        return {
            "running": self.running,
            "model_backend": self.detector._backend if self.detector else "not loaded",
            "avg_infer_ms": round(self.detector.avg_infer_ms, 1) if self.detector else 0,
            "frames_received": dict(self._frames_received),
            "frames_processed": dict(self._frames_processed),
            "detections_total": self._detections_total,
            "detect_queue_size": self._detect_queue.qsize(),
            "ws_clients": self.ws_server.client_count,
            "is_night": self.daynight.is_night(),
            "db_stats": self.db.get_stats(),
            "alert_stats": self.alerter.get_stats(),
        }
