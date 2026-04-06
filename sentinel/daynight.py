"""Day/night detection via time-of-day and frame brightness."""

from __future__ import annotations

from datetime import datetime

import numpy as np


class DayNightDetector:
    """Detects day/night conditions using clock time and optional frame analysis.

    Uses simple hour-based check (no external deps like astral/ephem).
    Optionally analyzes frame brightness for edge cases (dusk/dawn, dark rooms).
    """

    def __init__(
        self,
        latitude: float = 40.7,
        longitude: float = -74.0,
        night_start_hour: int = 20,
        night_end_hour: int = 6,
        brightness_threshold: float = 0.3,
    ):
        self.lat = latitude
        self.lon = longitude
        self.night_start = night_start_hour
        self.night_end = night_end_hour
        self.brightness_threshold = brightness_threshold

    def is_night(self, frame: np.ndarray | None = None) -> bool:
        """Check if it's nighttime.

        Args:
            frame: Optional BGR numpy frame. If provided, also checks brightness.

        Returns:
            True if nighttime (by clock or by frame darkness).
        """
        hour = datetime.now().hour
        astronomical_night = hour >= self.night_start or hour < self.night_end

        if frame is not None:
            brightness = self.ambient_level(frame)
            # Night if BOTH clock says night AND frame is dark,
            # OR if frame is very dark regardless of clock
            if brightness < self.brightness_threshold * 0.5:
                return True  # Very dark frame = night regardless
            return astronomical_night and brightness < self.brightness_threshold

        return astronomical_night

    def ambient_level(self, frame: np.ndarray) -> float:
        """Compute ambient light level from frame brightness.

        Args:
            frame: BGR numpy array.

        Returns:
            Brightness score 0.0 (black) to 1.0 (white).
        """
        # Use grayscale mean, normalized to 0-1
        if frame is None or frame.size == 0:
            return 0.0
        gray = np.mean(frame)  # Average of all channels
        return float(gray / 255.0)
