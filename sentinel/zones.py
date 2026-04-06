"""Zone management — polygon-based region monitoring per camera."""

from __future__ import annotations

import cv2
import numpy as np

from sentinel.models import Track, Zone


class ZoneManager:
    """Manages surveillance zones and checks track positions against them."""

    def __init__(self, zones_config: dict):
        """Initialize zones from config.

        Args:
            zones_config: Dict keyed by camera channel (str), each value is a list
                         of zone dicts with name, polygon, alert_level, classes, etc.
        """
        self.zones: dict[int, list[Zone]] = {}  # camera -> list of zones
        self._parse_config(zones_config)

    def _parse_config(self, zones_config: dict) -> None:
        for cam_key, zone_list in zones_config.items():
            camera = int(cam_key)
            self.zones[camera] = []

            if not isinstance(zone_list, list):
                continue

            for zdef in zone_list:
                try:
                    polygon = np.array(zdef["polygon"], dtype=np.int32)
                    if polygon.shape[0] < 3 or polygon.ndim != 2 or polygon.shape[1] != 2:
                        print(f"[ZONE] Skipping invalid polygon for {zdef.get('name', '?')}")
                        continue

                    zone = Zone(
                        name=zdef["name"],
                        camera=camera,
                        polygon=polygon,
                        alert_level=zdef.get("alert_level", "info"),
                        classes=zdef.get("classes", ["person"]),
                        night_boost=zdef.get("night_boost", False),
                        linger_seconds=zdef.get("linger_seconds", 10.0),
                    )
                    self.zones[camera].append(zone)
                except (KeyError, ValueError, TypeError) as e:
                    print(f"[ZONE] Error parsing zone: {e}")

        total = sum(len(zl) for zl in self.zones.values())
        print(f"[ZONE] Loaded {total} zones across {len(self.zones)} cameras")

    def check(self, channel: int, tracks: list[Track]) -> list[tuple[Track, Zone]]:
        """Check which tracks are inside which zones for a camera.

        Args:
            channel: Camera channel number.
            tracks: Active tracks from the tracker.

        Returns:
            List of (track, zone) tuples where the track center is inside the zone.
        """
        camera_zones = self.zones.get(channel, [])
        if not camera_zones:
            return []

        hits: list[tuple[Track, Zone]] = []

        for track in tracks:
            cx, cy = track.center
            point = (float(cx), float(cy))

            for zone in camera_zones:
                # Skip if track class not in zone's watched classes
                if track.class_name not in zone.classes:
                    continue

                # Point-in-polygon test
                result = cv2.pointPolygonTest(
                    zone.polygon.astype(np.float32),
                    point,
                    measureDist=False,
                )
                if result >= 0:  # Inside or on edge
                    hits.append((track, zone))

        return hits

    def get_zones_for_camera(self, channel: int) -> list[Zone]:
        return self.zones.get(channel, [])

    def get_all_zones(self) -> dict[int, list[dict]]:
        result: dict[int, list[dict]] = {}
        for cam, zones in self.zones.items():
            result[cam] = [z.to_dict() for z in zones]
        return result
