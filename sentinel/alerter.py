"""Alert dispatcher — track-aware, static-object suppression, counting."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from typing import Any

import requests as http_requests

from sentinel.models import Alert


class AlertDispatcher:
    """Smart alert system that distinguishes individual objects.

    Rules:
    - New object enters zone → alert ONCE
    - Object stays still > 2 min → mark "static", no more alerts for it
    - Second object arrives (different track) → new alert even if same class
    - Counts: "2 persons, 1 car" style summaries
    - Global cooldown per zone: max 1 notification per 30 seconds
    """

    def __init__(self, config: dict):
        self._lock = threading.Lock()
        self.macos_enabled: bool = config.get("macos_notifications", True)
        self.ws_enabled: bool = config.get("websocket", True)

        # Telegram integration
        telegram_cfg = config.get("telegram", {})
        self.telegram_enabled: bool = telegram_cfg.get("enabled", False)
        self.telegram_token: str = telegram_cfg.get("bot_token", "")
        self.telegram_chat_id: str = telegram_cfg.get("chat_id", "")
        self.telegram_min_level: str = telegram_cfg.get("min_level", "warning")
        self._alert_level_rank = {"info": 0, "warning": 1, "critical": 2}

        # Track-based state
        self._alerted_tracks: set[str] = set()      # "ch_trackid" → already notified
        self._static_tracks: set[str] = set()        # "ch_trackid" → parked/static, suppress
        self._track_first_seen: dict[str, float] = {}  # "ch_trackid" → timestamp
        self._track_last_pos: dict[str, tuple] = {}    # "ch_trackid" → (cx, cy)

        # Zone cooldown: max 1 macOS notification per zone per 30s
        self._zone_last_notif: dict[str, float] = {}  # "ch_zone" → timestamp
        self._zone_cooldown = 30  # seconds

        # Active object counts per zone
        self._zone_objects: dict[str, dict[str, int]] = {}  # "ch_zone" → {class: count}

        self.ws_clients: set[Any] = set()
        self._history: list[dict] = []
        self._max_history = 100
        self.total_dispatched = 0
        self.total_suppressed = 0

    def dispatch(self, alert: Alert) -> bool:
        """Process alert with track-aware logic."""
        track_key = f"{alert.channel}_{alert.track_id}"
        zone_key = f"{alert.channel}_{alert.zone_name}"

        with self._lock:
            now = time.time()

            # 1. Is this track already marked static? (parked car etc.)
            if track_key in self._static_tracks:
                self.total_suppressed += 1
                return False

            # 2. Check if track is becoming static (same position > 2 min)
            if track_key in self._track_first_seen:
                age = now - self._track_first_seen[track_key]
                if age > 120:  # 2 minutes in same zone
                    self._static_tracks.add(track_key)
                    self.total_suppressed += 1
                    return False
            else:
                self._track_first_seen[track_key] = now

            # 3. Already alerted for this specific track? (one alert per track)
            if track_key in self._alerted_tracks:
                self.total_suppressed += 1
                return False

            # Mark this track as alerted
            self._alerted_tracks.add(track_key)
            self.total_dispatched += 1

        # Log
        level_color = {"critical": "\033[91m", "warning": "\033[93m", "info": "\033[94m"}
        reset = "\033[0m"
        color = level_color.get(alert.alert_level, "")
        print(
            f"{color}[ALERT] CH{alert.channel} {alert.zone_name}: "
            f"{alert.class_name} (track #{alert.track_id}, {alert.confidence:.0%}) "
            f"[{alert.alert_level.upper()}]{reset}"
        )

        alert_dict = alert.to_dict()
        with self._lock:
            self._history.append(alert_dict)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]

        # macOS notification — rate limited per zone (max 1 per 30s)
        if self.macos_enabled:
            with self._lock:
                last = self._zone_last_notif.get(zone_key, 0)
                if now - last >= self._zone_cooldown:
                    self._zone_last_notif[zone_key] = now
                    self._notify_macos(alert)

        # Telegram — rate limited same as macOS (per zone cooldown)
        if self.telegram_enabled and self.telegram_token and self.telegram_chat_id:
            alert_rank = self._alert_level_rank.get(alert.alert_level, 0)
            min_rank = self._alert_level_rank.get(self.telegram_min_level, 1)
            if alert_rank >= min_rank:
                with self._lock:
                    tg_key = f"tg_{zone_key}"
                    last = self._zone_last_notif.get(tg_key, 0)
                    if now - last >= self._zone_cooldown:
                        self._zone_last_notif[tg_key] = now
                        print(f"[TELEGRAM] Sending: CH{alert.channel} {alert.zone_name} {alert.class_name} [{alert.alert_level}]")
                        threading.Thread(
                            target=self._notify_telegram,
                            args=(alert,),
                            daemon=True,
                        ).start()
                    else:
                        print(f"[TELEGRAM] Cooldown skip: {tg_key} ({now - last:.0f}s < {self._zone_cooldown}s)")
            else:
                print(f"[TELEGRAM] Level skip: {alert.alert_level} < {self.telegram_min_level}")
        elif not self.telegram_enabled:
            print(f"[TELEGRAM] Disabled in config")

        # WebSocket — always push (UI handles display)
        if self.ws_enabled and self.ws_clients:
            self._notify_ws(alert_dict)

        return True

    def _notify_macos(self, alert: Alert) -> None:
        try:
            sound = "Basso" if alert.alert_level == "critical" else ""
            title = f"Camera {alert.channel}: {alert.zone_name}"
            msg = f"{alert.class_name} detected ({alert.confidence:.0%})"
            if alert.behavior:
                msg += f" — {alert.behavior}"

            script = f'display notification "{msg}" with title "{title}"'
            if sound:
                script += f' sound name "{sound}"'

            subprocess.Popen(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def _notify_telegram(self, alert: Alert) -> None:
        """Send alert to Telegram bot with optional snapshot photo."""
        try:
            level_emoji = {
                "critical": "\xf0\x9f\x9a\xa8",  # siren
                "warning": "\xe2\x9a\xa0\xef\xb8\x8f",  # warning
                "info": "\xe2\x84\xb9\xef\xb8\x8f",  # info
            }
            emoji = level_emoji.get(alert.alert_level, "")
            msg = (
                f"{emoji} <b>Camera {alert.channel}: {alert.zone_name}</b>\n"
                f"{alert.class_name} ({alert.confidence:.0%})"
            )
            if alert.behavior:
                msg += f"\n{alert.behavior}"
            msg += f"\n<i>[{alert.alert_level.upper()}]</i>"

            base_url = f"https://api.telegram.org/bot{self.telegram_token}"

            # Send snapshot as photo if available
            if alert.snapshot_path and os.path.exists(alert.snapshot_path):
                with open(alert.snapshot_path, "rb") as f:
                    http_requests.post(
                        f"{base_url}/sendPhoto",
                        data={"chat_id": self.telegram_chat_id, "caption": msg, "parse_mode": "HTML"},
                        files={"photo": f},
                        timeout=10,
                    )
            else:
                # Text only
                http_requests.post(
                    f"{base_url}/sendMessage",
                    json={"chat_id": self.telegram_chat_id, "text": msg, "parse_mode": "HTML"},
                    timeout=10,
                )
        except Exception as e:
            print(f"[TELEGRAM] Send error: {e}")

    def _notify_ws(self, alert_dict: dict) -> None:
        message = json.dumps({"type": "alert", "data": alert_dict})
        dead: set[Any] = set()
        for ws in list(self.ws_clients):
            try:
                if hasattr(ws, "put_nowait"):
                    ws.put_nowait(message)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self.ws_clients.discard(ws)

    def cleanup_stale(self, active_track_ids: dict[int, set[int]]) -> None:
        """Called periodically — remove state for tracks that no longer exist.

        Args:
            active_track_ids: {channel: {track_id, ...}}
        """
        with self._lock:
            stale_keys = []
            for key in list(self._alerted_tracks):
                parts = key.split("_")
                if len(parts) == 2:
                    ch, tid = int(parts[0]), int(parts[1])
                    active = active_track_ids.get(ch, set())
                    if tid not in active:
                        stale_keys.append(key)

            for k in stale_keys:
                self._alerted_tracks.discard(k)
                self._static_tracks.discard(k)
                self._track_first_seen.pop(k, None)
                self._track_last_pos.pop(k, None)

    def get_history(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return list(self._history[-limit:])

    def get_stats(self) -> dict:
        return {
            "total_dispatched": self.total_dispatched,
            "total_suppressed": self.total_suppressed,
            "active_cooldowns": len(self._zone_last_notif),
            "tracked_objects": len(self._alerted_tracks),
            "static_objects": len(self._static_tracks),
            "ws_clients": len(self.ws_clients),
        }
