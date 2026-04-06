"""Simple IoU-based multi-object tracker — no external dependencies."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from sentinel.models import BBox, Detection, Track


@dataclass
class _TrackState:
    """Internal mutable track state."""
    track_id: int
    class_name: str
    confidence: float
    bbox: BBox
    path: list[tuple[float, float]] = field(default_factory=list)
    first_seen: float = 0.0
    last_seen: float = 0.0
    hits: int = 1
    age: int = 0
    consecutive_misses: int = 0

    def to_track(self) -> Track:
        return Track(
            track_id=self.track_id,
            class_name=self.class_name,
            confidence=self.confidence,
            bbox=self.bbox,
            path=list(self.path),
            first_seen=self.first_seen,
            last_seen=self.last_seen,
            hits=self.hits,
            age=self.age,
            consecutive_misses=self.consecutive_misses,
        )


class SimpleTracker:
    """IoU-based multi-object tracker with track persistence.

    Uses greedy IoU matching (no Hungarian — fast enough for 4 cameras).
    Tracks persist for max_age frames without a match before deletion.
    Only tracks with >= min_hits are considered confirmed.
    """

    def __init__(
        self,
        max_age: int = 5,
        min_hits: int = 2,
        iou_threshold: float = 0.3,
    ):
        self._tracks: dict[int, _TrackState] = {}
        self._next_id: int = 1
        self.max_age = max_age
        self.min_hits = min_hits
        self.iou_threshold = iou_threshold

    def update(self, detections: list[Detection], timestamp: float = 0.0) -> list[Track]:
        """Match detections to existing tracks, return active tracks.

        Args:
            detections: New detections from the current frame.
            timestamp: Frame timestamp (seconds).

        Returns:
            List of confirmed Track objects.
        """
        if timestamp == 0.0:
            timestamp = time.time()

        if not detections and not self._tracks:
            return []

        # Age all existing tracks
        for ts in self._tracks.values():
            ts.age += 1
            ts.consecutive_misses += 1

        if not detections:
            return self._cleanup_and_return(timestamp)

        if not self._tracks:
            # All detections become new tracks
            for det in detections:
                self._create_track(det, timestamp)
            return self._cleanup_and_return(timestamp)

        # --- Greedy IoU matching ---
        track_ids = list(self._tracks.keys())
        det_indices = list(range(len(detections)))

        # Compute IoU matrix
        iou_matrix: list[tuple[float, int, int]] = []
        for ti, tid in enumerate(track_ids):
            ts = self._tracks[tid]
            for di in det_indices:
                iou_val = ts.bbox.iou(detections[di].bbox)
                if iou_val >= self.iou_threshold:
                    iou_matrix.append((iou_val, ti, di))

        # Sort by IoU descending for greedy assignment
        iou_matrix.sort(key=lambda x: x[0], reverse=True)

        matched_tracks: set[int] = set()
        matched_dets: set[int] = set()

        for iou_val, ti, di in iou_matrix:
            if ti in matched_tracks or di in matched_dets:
                continue
            # Match: update track with detection
            tid = track_ids[ti]
            det = detections[di]
            ts = self._tracks[tid]
            ts.bbox = det.bbox
            ts.confidence = det.confidence
            ts.class_name = det.class_name
            ts.last_seen = timestamp
            ts.hits += 1
            ts.consecutive_misses = 0
            ts.path.append(det.bbox.center)
            # Keep path from growing unbounded
            if len(ts.path) > 200:
                ts.path = ts.path[-100:]
            matched_tracks.add(ti)
            matched_dets.add(di)

        # Create new tracks for unmatched detections
        for di in det_indices:
            if di not in matched_dets:
                self._create_track(detections[di], timestamp)

        return self._cleanup_and_return(timestamp)

    def _create_track(self, det: Detection, timestamp: float) -> None:
        tid = self._next_id
        self._next_id += 1
        self._tracks[tid] = _TrackState(
            track_id=tid,
            class_name=det.class_name,
            confidence=det.confidence,
            bbox=det.bbox,
            path=[det.bbox.center],
            first_seen=timestamp,
            last_seen=timestamp,
            hits=1,
            age=0,
            consecutive_misses=0,
        )

    def _cleanup_and_return(self, timestamp: float) -> list[Track]:
        """Remove dead tracks, return confirmed tracks."""
        dead_ids = [
            tid for tid, ts in self._tracks.items()
            if ts.consecutive_misses > self.max_age
        ]
        for tid in dead_ids:
            del self._tracks[tid]

        # Return only confirmed tracks (enough hits)
        confirmed: list[Track] = []
        for ts in self._tracks.values():
            if ts.hits >= self.min_hits:
                confirmed.append(ts.to_track())
        return confirmed

    def reset(self) -> None:
        """Clear all tracks."""
        self._tracks.clear()
        self._next_id = 1
