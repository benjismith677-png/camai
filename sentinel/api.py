"""Flask Blueprint for Sentinel REST API."""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

from flask import Blueprint, jsonify, request, send_file

if TYPE_CHECKING:
    from sentinel.engine import SentinelEngine

sentinel_bp = Blueprint("sentinel", __name__)

# Engine reference — set by server.py after creation
_engine: SentinelEngine | None = None


def init_api(engine: SentinelEngine) -> None:
    """Set the engine reference for API handlers."""
    global _engine
    _engine = engine


@sentinel_bp.route("/api/events")
def get_events():
    """Query events with filters.

    Query params: camera, level, class, zone, since, until, limit
    """
    if not _engine:
        return jsonify({"error": "Sentinel not initialized"}), 503

    camera = request.args.get("camera", type=int)
    level = request.args.get("level")
    class_name = request.args.get("class")
    zone = request.args.get("zone")
    since = request.args.get("since", type=float)
    until = request.args.get("until", type=float)
    limit = request.args.get("limit", default=50, type=int)

    events = _engine.db.query_events(
        camera=camera,
        level=level,
        class_name=class_name,
        zone=zone,
        since=since,
        until=until,
        limit=min(limit, 500),
    )
    return jsonify({"events": events, "count": len(events)})


@sentinel_bp.route("/api/events/<event_id>")
def get_event(event_id: str):
    """Get a single event by ID."""
    if not _engine:
        return jsonify({"error": "Sentinel not initialized"}), 503

    event = _engine.db.get_event(event_id)
    if not event:
        return jsonify({"error": "Event not found"}), 404
    return jsonify(event)


@sentinel_bp.route("/api/events/<event_id>/snapshot")
def get_snapshot(event_id: str):
    """Serve event snapshot JPEG."""
    if not _engine:
        return jsonify({"error": "Sentinel not initialized"}), 503

    event = _engine.db.get_event(event_id)
    if not event:
        return jsonify({"error": "Event not found"}), 404

    snapshot_path = event.get("snapshot_path")
    if not snapshot_path or not os.path.exists(snapshot_path):
        return jsonify({"error": "Snapshot not found"}), 404

    return send_file(
        snapshot_path,
        mimetype="image/jpeg",
        as_attachment=False,
        download_name=f"event_{event_id[:8]}.jpg",
    )


@sentinel_bp.route("/api/events/<event_id>/clip")
def get_clip(event_id: str):
    """Serve event video clip."""
    if not _engine:
        return jsonify({"error": "Sentinel not initialized"}), 503

    event = _engine.db.get_event(event_id)
    if not event:
        return jsonify({"error": "Event not found"}), 404

    clip_path = event.get("clip_path")
    if not clip_path or not os.path.exists(clip_path):
        return jsonify({"error": "Clip not found"}), 404

    return send_file(
        clip_path,
        mimetype="video/mp4",
        as_attachment=False,
        download_name=f"clip_{event_id[:8]}.mp4",
    )


@sentinel_bp.route("/api/zones")
def get_zones():
    """Return all zone configurations."""
    if not _engine:
        return jsonify({"error": "Sentinel not initialized"}), 503
    return jsonify(_engine.zone_mgr.get_all_zones())


@sentinel_bp.route("/api/zones", methods=["POST"])
def add_zone():
    """Add or update a zone and persist to sentinel_config.json.

    Expected JSON body:
        camera (int), name (str), polygon (list of [x,y]),
        alert_level (str), classes (list), night_boost (bool), linger_seconds (float)
    """
    if not _engine:
        return jsonify({"error": "Sentinel not initialized"}), 503

    data = request.get_json(force=True)
    required = ["camera", "name", "polygon"]
    for field in required:
        if field not in data:
            return jsonify({"error": f"Missing field: {field}"}), 400

    camera = int(data["camera"])
    zone_def = {
        "name": data["name"],
        "polygon": data["polygon"],
        "alert_level": data.get("alert_level", "warning"),
        "classes": data.get("classes", ["person"]),
        "night_boost": data.get("night_boost", True),
        "linger_seconds": data.get("linger_seconds", 10),
    }

    # Update config in memory
    zones_cfg = _engine.config.setdefault("zones", {})
    cam_key = str(camera)
    if cam_key not in zones_cfg:
        zones_cfg[cam_key] = []

    # Replace if same name exists, else append
    existing = [z for z in zones_cfg[cam_key] if z["name"] != data["name"]]
    existing.append(zone_def)
    zones_cfg[cam_key] = existing

    # Persist to file
    _save_config(_engine.config_path, _engine.config)

    # Reload zone manager
    _engine.zone_mgr = __import__("sentinel.zones", fromlist=["ZoneManager"]).ZoneManager(zones_cfg)

    return jsonify({"status": "ok", "zone": zone_def})


@sentinel_bp.route("/api/zones/<int:camera>/<name>", methods=["DELETE"])
def delete_zone(camera: int, name: str):
    """Delete a zone by camera and name."""
    if not _engine:
        return jsonify({"error": "Sentinel not initialized"}), 503

    zones_cfg = _engine.config.get("zones", {})
    cam_key = str(camera)
    if cam_key not in zones_cfg:
        return jsonify({"error": "Camera not found"}), 404

    original_len = len(zones_cfg[cam_key])
    zones_cfg[cam_key] = [z for z in zones_cfg[cam_key] if z["name"] != name]

    if len(zones_cfg[cam_key]) == original_len:
        return jsonify({"error": "Zone not found"}), 404

    # Persist
    _save_config(_engine.config_path, _engine.config)

    # Reload
    _engine.zone_mgr = __import__("sentinel.zones", fromlist=["ZoneManager"]).ZoneManager(zones_cfg)

    return jsonify({"status": "ok"})


def _save_config(config_path: str, config: dict) -> None:
    """Save sentinel config to JSON file."""
    import json as _json
    try:
        with open(config_path, "w") as f:
            _json.dump(config, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"[API] Config save error: {e}")


@sentinel_bp.route("/api/status")
def get_sentinel_status():
    """Engine stats: fps, detection counts, memory, model info."""
    if not _engine:
        return jsonify({"error": "Sentinel not initialized", "running": False}), 503
    return jsonify(_engine.get_status())


@sentinel_bp.route("/api/alerts/history")
def get_alert_history():
    """Get recent alert history from dispatcher."""
    if not _engine:
        return jsonify({"error": "Sentinel not initialized"}), 503

    limit = request.args.get("limit", default=50, type=int)
    history = _engine.alerter.get_history(limit=min(limit, 200))
    return jsonify({"alerts": history, "count": len(history)})


@sentinel_bp.route("/api/stats")
def get_stats():
    """Get aggregate event statistics."""
    if not _engine:
        return jsonify({"error": "Sentinel not initialized"}), 503
    return jsonify(_engine.db.get_stats())


@sentinel_bp.route("/api/stats/hourly")
def get_hourly_stats():
    """Get hourly event aggregates."""
    if not _engine:
        return jsonify({"error": "Sentinel not initialized"}), 503

    hours = request.args.get("hours", default=24, type=int)
    stats = _engine.db.get_hourly_stats(hours=min(hours, 168))
    return jsonify({"hourly": stats})
