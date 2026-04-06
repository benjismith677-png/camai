"""Motion detection using background subtraction."""

from __future__ import annotations

import cv2
import numpy as np


class MotionDetector:
    """MOG2-based motion detector per camera channel.

    Returns a motion score (0.0-1.0) representing the fraction of
    pixels in the foreground mask after morphological cleanup.
    """

    def __init__(self, camera_id: int, threshold: float = 0.003):
        self.camera_id = camera_id
        self.threshold = threshold
        self.bg = cv2.createBackgroundSubtractorMOG2(
            history=200,
            varThreshold=25,
            detectShadows=False,  # Shadows cause missed detections
        )
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        self._frame_count = 0
        self._warmup_frames = 15

    def detect(self, frame: np.ndarray) -> float:
        """Compute motion score for a frame."""
        self._frame_count += 1

        # Downsample for speed
        h, w = frame.shape[:2]
        if w > 352:
            small = cv2.resize(frame, (352, 288), interpolation=cv2.INTER_AREA)
        else:
            small = frame

        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        fg_mask = self.bg.apply(gray)

        # Light morphological cleanup only
        fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN, self.kernel)

        total_pixels = fg_mask.shape[0] * fg_mask.shape[1]
        motion_pixels = cv2.countNonZero(fg_mask)
        score = motion_pixels / total_pixels

        # Suppress during warmup (BG model stabilizing)
        if self._frame_count < self._warmup_frames:
            return 0.0

        return score

    def reset(self) -> None:
        """Reset the background model."""
        self.bg = cv2.createBackgroundSubtractorMOG2(
            history=200, varThreshold=25, detectShadows=False,
        )
        self._frame_count = 0
