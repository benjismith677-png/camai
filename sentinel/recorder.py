"""Event recorder — snapshots with annotations, video clips from ring buffer."""

from __future__ import annotations

import os
import time
from collections import deque
from datetime import datetime
from typing import Optional

import cv2
import numpy as np

from sentinel.models import Detection, Event, Zone


class EventRecorder:
    """Records event snapshots and video clips.

    Maintains a per-channel ring buffer of recent frames for clip extraction.
    Draws bounding boxes and zone polygons on snapshot images.
    """

    def __init__(self, data_dir: str = "data", ring_seconds: int = 5, fps: int = 15):
        self.data_dir = data_dir
        self.ring_maxlen = ring_seconds * fps
        self.target_fps = fps
        self.ring_buffers: dict[int, deque[tuple[float, np.ndarray]]] = {}
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        for sub in ["snapshots", "clips"]:
            os.makedirs(os.path.join(self.data_dir, sub), exist_ok=True)

    def _date_dir(self, subdir: str) -> str:
        """Return date-partitioned directory, creating if needed."""
        date_str = datetime.now().strftime("%Y-%m-%d")
        path = os.path.join(self.data_dir, subdir, date_str)
        os.makedirs(path, exist_ok=True)
        return path

    def buffer_frame(self, channel: int, frame: np.ndarray, timestamp: float) -> None:
        """Add frame to ring buffer (called for every frame).

        Args:
            channel: Camera channel.
            frame: BGR numpy array — stored as reference, NOT copied (caller must not reuse).
            timestamp: Frame timestamp.
        """
        if channel not in self.ring_buffers:
            self.ring_buffers[channel] = deque(maxlen=self.ring_maxlen)
        self.ring_buffers[channel].append((timestamp, frame))

    def save_snapshot(
        self,
        event: Event,
        frame: np.ndarray,
        detections: list[Detection],
        zones: list[Zone],
    ) -> str:
        """Draw annotations on frame and save as JPEG.

        Args:
            event: The event to snapshot.
            frame: BGR numpy array.
            detections: All detections to draw.
            zones: Zone polygons to overlay.

        Returns:
            Path to saved snapshot file.
        """
        annotated = frame.copy()

        # Draw zone polygons
        for zone in zones:
            color = self._zone_color(zone.alert_level)
            cv2.polylines(
                annotated,
                [zone.polygon],
                isClosed=True,
                color=color,
                thickness=2,
                lineType=cv2.LINE_AA,
            )
            # Zone label
            top_left = tuple(zone.polygon[0])
            cv2.putText(
                annotated,
                zone.name,
                (top_left[0], top_left[1] - 5),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                color,
                1,
                cv2.LINE_AA,
            )

        # Draw detection bboxes
        for det in detections:
            b = det.bbox
            color = (0, 255, 0) if det.class_name != "person" else (0, 0, 255)
            cv2.rectangle(
                annotated,
                (int(b.x1), int(b.y1)),
                (int(b.x2), int(b.y2)),
                color,
                2,
            )
            label = f"{det.class_name} {det.confidence:.0%}"
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            cv2.rectangle(
                annotated,
                (int(b.x1), int(b.y1) - th - 6),
                (int(b.x1) + tw, int(b.y1)),
                color,
                -1,
            )
            cv2.putText(
                annotated,
                label,
                (int(b.x1), int(b.y1) - 4),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (255, 255, 255),
                1,
                cv2.LINE_AA,
            )

        # Timestamp overlay
        ts_str = datetime.fromtimestamp(event.timestamp).strftime("%Y-%m-%d %H:%M:%S")
        cv2.putText(
            annotated,
            f"CH{event.channel} | {ts_str} | {event.alert_level.upper()}",
            (10, annotated.shape[0] - 10),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )

        # Save
        out_dir = self._date_dir("snapshots")
        filename = f"ch{event.channel}_{datetime.fromtimestamp(event.timestamp).strftime('%H%M%S')}_{event.id[:8]}.jpg"
        out_path = os.path.join(out_dir, filename)
        cv2.imwrite(out_path, annotated, [cv2.IMWRITE_JPEG_QUALITY, 85])

        return out_path

    def save_clip_from_frames(
        self, event: Event, channel: int, frames: list[tuple[float, np.ndarray]]
    ) -> Optional[str]:
        """Save clip from externally provided frame list (e.g., DahuaStream buffer).

        Args:
            event: The triggering event.
            channel: Camera channel.
            frames: List of (timestamp, bgr_ndarray) tuples.

        Returns:
            Path to saved clip file, or None if insufficient frames.
        """
        if not frames or len(frames) < 5:
            return None

        out_dir = self._date_dir("clips")
        filename = f"ch{channel}_{datetime.fromtimestamp(event.timestamp).strftime('%H%M%S')}_{event.id[:8]}.mp4"
        out_path = os.path.join(out_dir, filename)

        try:
            h, w = frames[0][1].shape[:2]
            # Estimate FPS from timestamps
            if len(frames) > 1:
                duration = frames[-1][0] - frames[0][0]
                fps = max(1, len(frames) / max(0.1, duration))
            else:
                fps = self.target_fps

            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            writer = cv2.VideoWriter(out_path, fourcc, fps, (w, h))

            for _, frame in frames:
                writer.write(frame)

            writer.release()
            return out_path
        except Exception as e:
            print(f"[RECORD] Clip save error: {e}")
            return None

    def save_clip(self, event: Event, channel: int) -> Optional[str]:
        """Save ring buffer contents as MP4 clip.

        Args:
            event: The triggering event.
            channel: Camera channel.

        Returns:
            Path to saved clip file, or None if insufficient frames.
        """
        buf = self.ring_buffers.get(channel)
        if not buf or len(buf) < 5:
            return None

        frames = list(buf)  # Snapshot the deque

        out_dir = self._date_dir("clips")
        filename = f"ch{channel}_{datetime.fromtimestamp(event.timestamp).strftime('%H%M%S')}_{event.id[:8]}.mp4"
        out_path = os.path.join(out_dir, filename)

        try:
            h, w = frames[0][1].shape[:2]
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            writer = cv2.VideoWriter(out_path, fourcc, self.target_fps, (w, h))

            for _, frame in frames:
                writer.write(frame)

            writer.release()
            return out_path
        except Exception as e:
            print(f"[RECORD] Clip save error: {e}")
            return None

    def _zone_color(self, alert_level: str) -> tuple[int, int, int]:
        """BGR color for zone overlay based on alert level."""
        colors = {
            "critical": (0, 0, 255),   # Red
            "warning": (0, 165, 255),   # Orange
            "info": (255, 255, 0),      # Cyan
        }
        return colors.get(alert_level, (200, 200, 200))
