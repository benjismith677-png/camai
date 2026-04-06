"""Core data models for Sentinel detection pipeline."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass
class BBox:
    """Bounding box in pixel coordinates (top-left origin)."""
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1

    @property
    def area(self) -> float:
        return max(0, self.width) * max(0, self.height)

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2)

    def iou(self, other: BBox) -> float:
        """Compute Intersection over Union with another bbox."""
        ix1 = max(self.x1, other.x1)
        iy1 = max(self.y1, other.y1)
        ix2 = min(self.x2, other.x2)
        iy2 = min(self.y2, other.y2)
        inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
        union = self.area + other.area - inter
        if union <= 0:
            return 0.0
        return inter / union

    def to_dict(self) -> dict:
        return {"x1": self.x1, "y1": self.y1, "x2": self.x2, "y2": self.y2}


@dataclass
class Detection:
    """Single object detection from YOLO."""
    class_name: str
    confidence: float
    bbox: BBox
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "class_name": self.class_name,
            "confidence": round(self.confidence, 3),
            "bbox": self.bbox.to_dict(),
        }


@dataclass
class Track:
    """Tracked object across multiple frames."""
    track_id: int
    class_name: str
    confidence: float
    bbox: BBox
    path: list[tuple[float, float]] = field(default_factory=list)
    first_seen: float = 0.0
    last_seen: float = 0.0
    hits: int = 0
    age: int = 0
    consecutive_misses: int = 0

    @property
    def center(self) -> tuple[float, float]:
        return self.bbox.center

    @property
    def duration(self) -> float:
        return self.last_seen - self.first_seen

    def to_dict(self) -> dict:
        return {
            "track_id": self.track_id,
            "class_name": self.class_name,
            "confidence": round(self.confidence, 3),
            "bbox": self.bbox.to_dict(),
            "duration": round(self.duration, 1),
            "path_length": len(self.path),
        }


@dataclass
class Zone:
    """Camera surveillance zone with alert configuration."""
    name: str
    camera: int
    polygon: np.ndarray  # shape (N, 2) int32
    alert_level: str = "info"  # info | warning | critical
    classes: list[str] = field(default_factory=lambda: ["person"])
    night_boost: bool = False
    linger_seconds: float = 10.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "camera": self.camera,
            "alert_level": self.alert_level,
            "classes": self.classes,
            "night_boost": self.night_boost,
            "linger_seconds": self.linger_seconds,
            "polygon": self.polygon.tolist(),
        }


@dataclass
class Alert:
    """Generated alert from sentinel pipeline."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    channel: int = 0
    zone_name: str = ""
    alert_level: str = "info"
    class_name: str = ""
    track_id: int = 0
    confidence: float = 0.0
    behavior: Optional[str] = None
    snapshot_path: Optional[str] = None
    clip_path: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "channel": self.channel,
            "zone_name": self.zone_name,
            "alert_level": self.alert_level,
            "class_name": self.class_name,
            "track_id": self.track_id,
            "confidence": round(self.confidence, 3),
            "behavior": self.behavior,
            "snapshot_path": self.snapshot_path,
            "clip_path": self.clip_path,
        }


@dataclass
class Event:
    """Persistent event record for database storage."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    channel: int = 0
    zone_name: str = ""
    alert_level: str = "info"
    class_name: str = ""
    track_id: int = 0
    confidence: float = 0.0
    behavior: Optional[str] = None
    snapshot_path: Optional[str] = None
    clip_path: Optional[str] = None
    duration: float = 0.0

    @classmethod
    def from_alert(cls, alert: Alert, duration: float = 0.0) -> Event:
        return cls(
            id=alert.id,
            timestamp=alert.timestamp,
            channel=alert.channel,
            zone_name=alert.zone_name,
            alert_level=alert.alert_level,
            class_name=alert.class_name,
            track_id=alert.track_id,
            confidence=alert.confidence,
            behavior=alert.behavior,
            snapshot_path=alert.snapshot_path,
            clip_path=alert.clip_path,
            duration=duration,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "channel": self.channel,
            "zone_name": self.zone_name,
            "alert_level": self.alert_level,
            "class_name": self.class_name,
            "track_id": self.track_id,
            "confidence": round(self.confidence, 3),
            "behavior": self.behavior,
            "snapshot_path": self.snapshot_path,
            "clip_path": self.clip_path,
            "duration": round(self.duration, 1),
        }
