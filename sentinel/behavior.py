"""Behavior analysis — lingering, approach escalation, night boost."""

from __future__ import annotations

import time

from sentinel.models import Track, Zone


class BehaviorAnalyzer:
    """Analyzes track behavior within zones.

    Key principle: alert ONCE per behavior state change, not continuously.
    - Object enters zone → alert once
    - Object starts lingering → alert once
    - Object leaves → reset
    Stationary objects (parked cars) get one alert then silence.
    """

    def __init__(self, config: dict):
        self.linger_thresholds: dict[str, float] = config.get(
            "linger_threshold_seconds",
            {"person": 30, "car": 120, "default": 60},
        )
        # (track_id, zone_name) -> first_seen timestamp
        self._track_zone_entry: dict[tuple[int, str], float] = {}
        # Track which behaviors already fired — prevent re-alerting
        self._alerted_states: set[str] = set()  # "trackid_zone_behavior"
        self._check_count = 0

    def analyze(
        self,
        track: Track,
        zone: Zone,
        timestamp: float,
        is_night: bool,
    ) -> tuple[str, str] | None:
        self._check_count += 1
        if self._check_count % 200 == 0:
            self._cleanup_stale(timestamp)

        key = (track.track_id, zone.name)
        alert_level = zone.alert_level

        # Record zone entry
        if key not in self._track_zone_entry:
            self._track_zone_entry[key] = timestamp

        time_in_zone = timestamp - self._track_zone_entry[key]

        # --- Determine behavior ---
        behavior = None
        linger_threshold = self.linger_thresholds.get(
            track.class_name,
            self.linger_thresholds.get("default", 60),
        )

        if time_in_zone >= linger_threshold:
            behavior = f"lingering ({time_in_zone:.0f}s)"
            alert_level = self._escalate(alert_level)
        elif time_in_zone < 3:
            behavior = "entered"
        # Between 3s and linger threshold: no behavior (just presence)

        # Night boost
        if is_night and zone.night_boost:
            alert_level = self._escalate(alert_level)
            if behavior:
                behavior += " (night)"
            else:
                behavior = "night detection"

        if not behavior:
            return None

        # --- Dedup: only alert ONCE per behavior state ---
        # "entered" alerts once per track entering zone
        # "lingering" alerts once per track lingering in zone
        state_key = f"{track.track_id}_{zone.name}_{behavior.split(' ')[0]}"
        if state_key in self._alerted_states:
            return None  # Already alerted for this state

        self._alerted_states.add(state_key)
        return (behavior, alert_level)

    def _escalate(self, level: str) -> str:
        if level == "info":
            return "warning"
        if level == "warning":
            return "critical"
        return "critical"

    def _cleanup_stale(self, now: float) -> None:
        stale = [k for k, ts in self._track_zone_entry.items() if now - ts > 600]
        for k in stale:
            del self._track_zone_entry[k]
        # Clean old alerted states (keep last 500)
        if len(self._alerted_states) > 500:
            self._alerted_states = set(list(self._alerted_states)[-200:])

    def reset(self) -> None:
        self._track_zone_entry.clear()
        self._alerted_states.clear()
