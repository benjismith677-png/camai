#!/usr/bin/env python3
"""
Camai — RTSP/IP Camera & Dahua DVR MJPEG Server with AI Surveillance.
Each channel available at /stream/<channel_number>

Supports:
- Generic RTSP cameras (Hikvision, Reolink, Dahua, Amcrest, etc.)
- Dahua DVR via proprietary HTTP endpoints (legacy)

Integrates Sentinel AI detection engine for real-time surveillance.
"""

import collections
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import av
import numpy as np
import requests
from requests.auth import HTTPDigestAuth
from turbojpeg import TurboJPEG
from flask import Flask, Response, jsonify, request, send_file

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

if not os.path.exists(CONFIG_PATH):
    print("=" * 60)
    print("ERROR: config.json not found!")
    print("")
    print("Copy the example config and edit it with your camera details:")
    print("  cp config.example.json config.json")
    print("  # Then edit config.json with your camera URLs or DVR details")
    print("")
    print("Or start the Electron app — it has a setup wizard.")
    print("=" * 60)
    # Create a minimal config so the server can start for setup wizard
    CFG = {
        "title": "CAMAI",
        "cameras": [],
        "always_on_channel": 1,
    }
else:
    with open(CONFIG_PATH) as f:
        CFG = json.load(f)


# ---------------------------------------------------------------------------
#  Config Parsing — supports new camera array AND legacy Dahua flat format
# ---------------------------------------------------------------------------

def parse_cameras(cfg: dict) -> list[dict]:
    """Parse camera config, supporting both new and legacy formats.

    New format:
        {"cameras": [{"name": "...", "type": "rtsp", "url": "rtsp://..."}, ...]}
    Legacy Dahua format:
        {"dvr_ip": "...", "dvr_user": "...", "dvr_pass": "...", "channels": 4}
    """
    cameras: list[dict] = []

    if "cameras" in cfg and isinstance(cfg["cameras"], list):
        # New format: array of camera objects
        for i, cam in enumerate(cfg["cameras"]):
            cameras.append({
                "id": i + 1,
                "name": cam.get("name", f"Camera {i + 1}"),
                "type": cam.get("type", "rtsp"),
                "url": cam.get("url", ""),
                "ip": cam.get("ip", ""),
                "port": cam.get("port", 80),
                "user": cam.get("user", ""),
                "pass": cam.get("pass", ""),
                "channel": cam.get("channel", i + 1),
            })
    elif "dvr_ip" in cfg:
        # Legacy Dahua flat format
        channels = cfg.get("channels", 4)
        names = cfg.get("channel_names", [])
        for i in range(channels):
            cameras.append({
                "id": i + 1,
                "name": names[i] if i < len(names) else f"Camera {i + 1}",
                "type": "dahua",
                "ip": cfg["dvr_ip"],
                "port": cfg.get("dvr_port", 80),
                "user": cfg.get("dvr_user", "admin"),
                "pass": cfg.get("dvr_pass", ""),
                "channel": i + 1,
            })

    return cameras


CAMERAS = parse_cameras(CFG)
NUM_CAMERAS = len(CAMERAS)
CHANNEL_NAMES = [c["name"] for c in CAMERAS]
TITLE = CFG.get("title", "CAMAI")

# Legacy compat globals (used by DahuaStream)
DVR_IP = CFG.get("dvr_ip", "")
DVR_USER = CFG.get("dvr_user", "admin")
DVR_PASS = CFG.get("dvr_pass", "")

MJPEG_URL = "http://{ip}/cgi-bin/mjpg/video.cgi?channel={ch}&subtype=1"
BOUNDARY = b"--myboundary"
TJPEG = TurboJPEG()
CALLBACK_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sentinel")

app = Flask(__name__)


def extract_h264(data: bytes) -> bytes:
    """Strip 36-byte Dahua header and extract H.264 NAL units."""
    if len(data) < 40:
        return b""
    p = data[36:]
    if len(p) < 4:
        return b""
    if p[:4] == b"\x00\x00\x00\x01":
        return p
    if p[:2] == b"\x00\x01":
        return b"\x00\x00" + p
    return b""


class RTSPStream:
    """Connects to any RTSP camera stream, decodes to JPEG frames.

    Works with Hikvision, Reolink, Dahua, Amcrest, generic IP cameras, etc.
    Uses PyAV (FFmpeg) for RTSP transport and H.264/H.265 decoding.

    Supports frame callbacks for sentinel integration — callbacks receive
    raw numpy arrays BEFORE JPEG encoding.

    Modes:
        "live"     — full quality (8 FPS, quality 80) for active viewing
        "sentinel" — power-save (2 FPS, quality 40) for background AI detection
    """

    MODE_LIVE = ("live", 8, 80)
    MODE_SENTINEL = ("sentinel", 2, 40)

    # Reconnection settings
    RETRY_DELAYS = [2, 2, 5, 5, 10]  # First 5 retries
    RETRY_LONG = 30  # After exhausting short retries

    def __init__(self, channel: int, url: str):
        self.channel = channel
        self.url = url
        self.running = False
        self._jpeg: bytes | None = None
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._frame_callbacks: list[Callable[[int, np.ndarray, float], None]] = []
        # Ring buffer for clip recording — stores (timestamp, bgr_array) tuples
        # 80 frames ~ 10 seconds at 8 FPS
        self._frame_buffer: collections.deque = collections.deque(maxlen=80)
        # Power management
        self._mode = "live"
        self._target_fps = 8
        self._jpeg_quality = 80
        self._last_frame_time = 0.0
        self._retry_count = 0

    def add_frame_callback(self, cb: Callable[[int, np.ndarray, float], None]) -> None:
        """Register a callback: cb(channel, numpy_bgr_array, timestamp)."""
        self._frame_callbacks.append(cb)

    def set_mode(self, mode: str) -> None:
        """Switch between 'live' and 'sentinel' mode."""
        if mode == "sentinel":
            self._mode, self._target_fps, self._jpeg_quality = self.MODE_SENTINEL
        else:
            self._mode, self._target_fps, self._jpeg_quality = self.MODE_LIVE
        print(f"[CH{self.channel}] Mode: {self._mode} ({self._target_fps} FPS, Q{self._jpeg_quality})")

    def start(self) -> None:
        self.running = True
        self._retry_count = 0
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self.running = False
        self._event.set()

    def get_jpeg(self) -> bytes | None:
        """Return latest JPEG frame, blocking briefly if none available."""
        self._event.wait(timeout=0.5)
        with self._lock:
            self._event.clear()
            return self._jpeg

    def _get_retry_delay(self) -> float:
        """Get delay before next reconnection attempt."""
        if self._retry_count < len(self.RETRY_DELAYS):
            return self.RETRY_DELAYS[self._retry_count]
        return self.RETRY_LONG

    def _loop(self) -> None:
        while self.running:
            try:
                self._run()
                # If _run returns normally (stream ended), reset retry count
                self._retry_count = 0
            except Exception as e:
                print(f"[CH{self.channel}] RTSP error: {e}")
                self._retry_count += 1

            if self.running:
                delay = self._get_retry_delay()
                print(f"[CH{self.channel}] Reconnecting in {delay}s (attempt {self._retry_count})...")
                time.sleep(delay)

    def _run(self) -> None:
        """Connect to RTSP stream and decode frames."""
        options = {
            "rtsp_transport": "tcp",
            "stimeout": "5000000",  # 5s socket timeout in microseconds
        }

        container = av.open(self.url, options=options, timeout=10.0)
        try:
            video_stream = container.streams.video[0]
            # Don't buffer too many packets
            video_stream.thread_type = "AUTO"

            print(f"[CH{self.channel}] RTSP connected: {video_stream.codec_context.name} "
                  f"{video_stream.width}x{video_stream.height}")
            self._retry_count = 0  # Reset on successful connect

            for frame in container.decode(video=0):
                if not self.running:
                    break

                # FPS throttle — skip frames based on mode
                now = time.time()
                min_interval = 1.0 / self._target_fps
                if now - self._last_frame_time < min_interval:
                    continue
                self._last_frame_time = now

                arr = frame.to_ndarray(format="bgr24")
                jpeg = TJPEG.encode(arr, quality=self._jpeg_quality)
                with self._lock:
                    self._jpeg = jpeg
                    self._event.set()

                # Buffer frame for clip recording (always, before callbacks)
                ts = now
                self._frame_buffer.append((ts, arr.copy()))

                # Fire callbacks in thread pool — never block stream
                if self._frame_callbacks:
                    ch = self.channel
                    for cb in self._frame_callbacks:
                        CALLBACK_POOL.submit(cb, ch, arr, ts)
        finally:
            container.close()


class DahuaStream:
    """Connects to Dahua DVR H.264 stream, decodes to JPEG frames.

    Supports frame callbacks for sentinel integration — callbacks receive
    raw numpy arrays BEFORE JPEG encoding.

    Modes:
        "live"     — full quality (8 FPS, quality 80) for active viewing
        "sentinel" — power-save (2 FPS, quality 40) for background AI detection
    """

    # Mode presets: (target_fps, jpeg_quality)
    MODE_LIVE = ("live", 8, 80)
    MODE_SENTINEL = ("sentinel", 2, 40)

    def __init__(self, channel: int, ip: str = "", user: str = "", password: str = "", port: int = 80):
        self.channel = channel
        self._ip = ip or DVR_IP
        self._user = user or DVR_USER
        self._pass = password or DVR_PASS
        self._port = port
        self.running = False
        self._jpeg: bytes | None = None
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._frame_callbacks: list[Callable[[int, np.ndarray, float], None]] = []
        # Ring buffer for clip recording — stores (timestamp, bgr_array) tuples
        # 80 frames ~ 10 seconds at 8 FPS
        self._frame_buffer: collections.deque = collections.deque(maxlen=80)
        # Power management
        self._mode = "live"
        self._target_fps = 8
        self._jpeg_quality = 80
        self._last_frame_time = 0.0

    def add_frame_callback(self, cb: Callable[[int, np.ndarray, float], None]) -> None:
        """Register a callback: cb(channel, numpy_bgr_array, timestamp)."""
        self._frame_callbacks.append(cb)

    def set_mode(self, mode: str) -> None:
        """Switch between 'live' and 'sentinel' mode."""
        if mode == "sentinel":
            self._mode, self._target_fps, self._jpeg_quality = self.MODE_SENTINEL
        else:
            self._mode, self._target_fps, self._jpeg_quality = self.MODE_LIVE
        print(f"[CH{self.channel}] Mode: {self._mode} ({self._target_fps} FPS, Q{self._jpeg_quality})")

    def start(self) -> None:
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self.running = False
        self._event.set()

    def get_jpeg(self) -> bytes | None:
        """Return latest JPEG frame, blocking briefly if none available."""
        self._event.wait(timeout=0.5)
        with self._lock:
            self._event.clear()
            return self._jpeg

    def _loop(self) -> None:
        while self.running:
            try:
                if self._mode == "sentinel":
                    self._run_sentinel()
                else:
                    self._run()
            except Exception as e:
                print(f"[CH{self.channel}] Error: {e}")
            if self.running:
                time.sleep(0.5)

    def _run_sentinel(self) -> None:
        """Power-save mode: grab a single snapshot every 0.5s instead of continuous stream."""
        snapshot_url = f"http://{self._ip}/cgi-bin/snapshot.cgi?channel={self.channel}"
        sess = requests.Session()
        sess.auth = HTTPDigestAuth(self._user, self._pass)

        while self.running and self._mode == "sentinel":
            try:
                resp = sess.get(snapshot_url, timeout=5)
                if resp.status_code == 200 and len(resp.content) > 100:
                    jpeg_data = resp.content
                    with self._lock:
                        self._jpeg = jpeg_data
                        self._event.set()

                    # Decode for Sentinel AI callbacks
                    if self._frame_callbacks:
                        arr = TJPEG.decode(jpeg_data)
                        ts = time.time()
                        self._frame_buffer.append((ts, arr.copy()))
                        ch = self.channel
                        for cb in self._frame_callbacks:
                            CALLBACK_POOL.submit(cb, ch, arr, ts)
            except Exception as e:
                print(f"[CH{self.channel}] Sentinel snapshot error: {e}")

            # Sleep between snapshots (0.5s = 2 FPS)
            interval = 1.0 / self._target_fps
            time.sleep(interval)

        sess.close()

    def _run(self) -> None:
        url = MJPEG_URL.format(ip=self._ip, ch=self.channel)
        sess = requests.Session()
        sess.auth = HTTPDigestAuth(self._user, self._pass)
        resp = sess.get(url, stream=True, timeout=10)
        resp.raise_for_status()
        resp.raw.decode_content = False

        codec = av.CodecContext.create("h264", "r")
        buf = b""
        got_key = False

        while self.running:
            chunk = resp.raw.read(1024)
            if not chunk:
                break
            buf += chunk
            while True:
                i1 = buf.find(BOUNDARY)
                if i1 == -1:
                    buf = buf[-(len(BOUNDARY) + 2):]
                    break
                i2 = buf.find(b"\r\n\r\n", i1)
                if i2 == -1:
                    break
                i3 = buf.find(BOUNDARY, i2 + 4)
                if i3 == -1:
                    break
                frame = buf[i2 + 4:i3].rstrip(b"\r\n")
                buf = buf[i3:]
                if len(frame) < 50:
                    continue
                h264 = extract_h264(frame)
                if not h264:
                    continue
                if not got_key:
                    if b"\x00\x00\x00\x01\x67" not in h264:
                        continue
                    got_key = True
                try:
                    for vf in codec.decode(av.Packet(h264)):
                        # FPS throttle — skip frames based on mode
                        now = time.time()
                        min_interval = 1.0 / self._target_fps
                        if now - self._last_frame_time < min_interval:
                            continue
                        self._last_frame_time = now

                        arr = vf.to_ndarray(format="bgr24")
                        jpeg = TJPEG.encode(arr, quality=self._jpeg_quality)
                        with self._lock:
                            self._jpeg = jpeg
                            self._event.set()

                        # Buffer frame for clip recording (always, before callbacks)
                        ts = now
                        self._frame_buffer.append((ts, arr.copy()))

                        # Fire callbacks in thread pool — never block stream
                        if self._frame_callbacks:
                            ch = self.channel
                            for cb in self._frame_callbacks:
                                CALLBACK_POOL.submit(cb, ch, arr, ts)
                except av.error.InvalidDataError:
                    pass

        resp.close()
        sess.close()


def create_stream(cam_config: dict):
    """Create the appropriate stream type based on camera config."""
    if cam_config["type"] == "rtsp":
        return RTSPStream(cam_config["id"], cam_config["url"])
    else:
        return DahuaStream(
            cam_config["id"],
            ip=cam_config.get("ip", ""),
            user=cam_config.get("user", ""),
            password=cam_config.get("pass", ""),
            port=cam_config.get("port", 80),
        )


# --- Global streams ---
streams: dict[int, DahuaStream | RTSPStream] = {}

# Always-on channel — runs 24/7 for Sentinel AI
ALWAYS_ON_CHANNEL = CFG.get("always_on_channel", 1)


def start_streams() -> None:
    """Start only the always-on channel. Others start on-demand."""
    if not CAMERAS:
        print("[STREAM] No cameras configured — skipping stream start")
        return

    for cam in CAMERAS:
        s = create_stream(cam)
        streams[cam["id"]] = s

    # Only start the always-on channel in sentinel (power-save) mode
    if ALWAYS_ON_CHANNEL in streams:
        streams[ALWAYS_ON_CHANNEL].set_mode("sentinel")
        streams[ALWAYS_ON_CHANNEL].start()
        print(f"[STREAM] CH{ALWAYS_ON_CHANNEL} always-on (Sentinel, power-save)")

    on_demand = [str(cam["id"]) for cam in CAMERAS if cam["id"] != ALWAYS_ON_CHANNEL]
    if on_demand:
        print(f"[STREAM] CH{', '.join(on_demand)} on-demand")


def generate_mjpeg(channel: int):
    """Generator that yields MJPEG frames for a given channel."""
    stream = streams.get(channel)
    if not stream:
        return

    # Auto-start on-demand stream when viewer connects
    if not stream.running:
        stream.start()
        print(f"[STREAM] CH{channel} started (on-demand)")
        if sentinel_engine and sentinel_engine.running:
            stream.add_frame_callback(sentinel_engine.on_frame)
            sentinel_engine.set_stream_buffers(streams)

    # Pre-buffer: serve the last cached frame instantly
    with stream._lock:
        cached = stream._jpeg
    if cached:
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Content-Length: " + str(len(cached)).encode() + b"\r\n"
            b"\r\n" + cached + b"\r\n"
        )

    # Live loop
    while True:
        jpeg = stream.get_jpeg()
        if jpeg:
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n"
                b"\r\n" + jpeg + b"\r\n"
            )


@app.route("/stream/<int:channel>")
def stream_route(channel: int):
    """Serve MJPEG stream for a camera channel."""
    if channel < 1 or channel > NUM_CAMERAS:
        return "Invalid channel", 404
    return Response(
        generate_mjpeg(channel),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/snapshot/<int:channel>")
def snapshot(channel: int):
    """Serve latest JPEG frame (single image, no streaming)."""
    if channel < 1 or channel > NUM_CAMERAS:
        return "Invalid channel", 404
    s = streams.get(channel)
    if not s or not s._jpeg:
        return "", 204
    with s._lock:
        jpeg = s._jpeg
    return Response(jpeg, mimetype="image/jpeg", headers={
        "Cache-Control": "no-cache",
        "X-Timestamp": str(time.time()),
    })


@app.route("/enhance/<int:channel>")
def enhance(channel: int):
    """AI-enhanced crop: fal.ai phota/enhance (primary) or local fallback.

    Query params:
        x, y, w, h: crop region (0.0-1.0 normalized)
        mode: "fal" (default) or "local"
    """
    import cv2
    import base64

    if channel < 1 or channel > NUM_CAMERAS:
        return "Invalid channel", 404
    s = streams.get(channel)
    if not s or not s._jpeg:
        return "", 204

    x = float(request.args.get("x", 0))
    y = float(request.args.get("y", 0))
    w = float(request.args.get("w", 1))
    h = float(request.args.get("h", 1))
    mode = request.args.get("mode", "fal")

    with s._lock:
        jpeg_bytes = s._jpeg
    arr = cv2.imdecode(np.frombuffer(jpeg_bytes, np.uint8), cv2.IMREAD_COLOR)
    if arr is None:
        return "", 204

    fh, fw = arr.shape[:2]
    x1, y1 = max(0, int(x * fw)), max(0, int(y * fh))
    x2, y2 = min(fw, int((x + w) * fw)), min(fh, int((y + h) * fh))
    crop = arr[y1:y2, x1:x2]

    if crop.size == 0:
        return "", 204

    if mode == "fal":
        return _fal_enhance(crop)

    # Local fallback (old pipeline)
    ch, cw = crop.shape[:2]
    if ch > 250 or cw > 250:
        scale = 250 / max(ch, cw)
        crop = cv2.resize(crop, (int(cw * scale), int(ch * scale)), interpolation=cv2.INTER_AREA)

    passes = int(request.args.get("passes", 2))
    upscaled = _ai_upscale(crop, passes=passes)

    denoised = cv2.bilateralFilter(upscaled, 7, 40, 40)
    lab = cv2.cvtColor(denoised, cv2.COLOR_BGR2LAB)
    l_ch, a_ch, b_ch = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
    l_ch = clahe.apply(l_ch)
    enhanced = cv2.merge([l_ch, a_ch, b_ch])
    enhanced = cv2.cvtColor(enhanced, cv2.COLOR_LAB2BGR)
    blur = cv2.GaussianBlur(enhanced, (0, 0), 2.0)
    sharpened = cv2.addWeighted(enhanced, 1.4, blur, -0.4, 0)

    result = TJPEG.encode(sharpened, quality=92)
    return Response(result, mimetype="image/jpeg", headers={
        "Cache-Control": "no-cache",
    })


# --- fal.ai Phota Enhance ---
FAL_API_KEY = os.environ.get("FAL_API_KEY", "")


def _fal_enhance(crop):
    """Send crop to fal.ai phota/enhance and return the enhanced image."""
    import cv2
    import base64

    if not FAL_API_KEY:
        print("[ENHANCE] FAL_API_KEY not set, falling back to local")
        return _fal_fallback_local(crop)

    t0 = time.time()

    # Encode crop as JPEG for upload
    _, jpeg_buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 95])
    b64_data = base64.b64encode(jpeg_buf.tobytes()).decode("utf-8")
    data_uri = f"data:image/jpeg;base64,{b64_data}"

    print(f"[ENHANCE] fal.ai request: {crop.shape[1]}x{crop.shape[0]} crop")

    try:
        resp = requests.post(
            "https://fal.run/fal-ai/phota/enhance",
            headers={
                "Authorization": f"Key {FAL_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "image_url": data_uri,
                "num_images": 1,
                "output_format": "jpeg",
            },
            timeout=180,
        )
        resp.raise_for_status()
        result = resp.json()

        if not result.get("images"):
            print("[ENHANCE] fal.ai returned no images, falling back to local")
            return _fal_fallback_local(crop)

        # Download the enhanced image
        img_url = result["images"][0]["url"]
        img_resp = requests.get(img_url, timeout=30)
        img_resp.raise_for_status()

        t1 = time.time()
        print(f"[ENHANCE] fal.ai done in {t1-t0:.1f}s")

        return Response(img_resp.content, mimetype="image/jpeg", headers={
            "Cache-Control": "no-cache",
            "X-Enhance-Time": f"{t1-t0:.1f}",
            "X-Enhance-Method": "fal-phota",
        })

    except Exception as e:
        print(f"[ENHANCE] fal.ai error: {e}, falling back to local")
        return _fal_fallback_local(crop)


def _fal_fallback_local(crop):
    """Local enhance fallback when fal.ai fails."""
    import cv2

    ch, cw = crop.shape[:2]
    if ch > 250 or cw > 250:
        scale = 250 / max(ch, cw)
        crop = cv2.resize(crop, (int(cw * scale), int(ch * scale)), interpolation=cv2.INTER_AREA)

    upscaled = _ai_upscale(crop, passes=2)
    denoised = cv2.bilateralFilter(upscaled, 7, 40, 40)
    lab = cv2.cvtColor(denoised, cv2.COLOR_BGR2LAB)
    l_ch, a_ch, b_ch = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
    l_ch = clahe.apply(l_ch)
    enhanced = cv2.merge([l_ch, a_ch, b_ch])
    enhanced = cv2.cvtColor(enhanced, cv2.COLOR_LAB2BGR)
    blur = cv2.GaussianBlur(enhanced, (0, 0), 2.0)
    sharpened = cv2.addWeighted(enhanced, 1.4, blur, -0.4, 0)

    result = TJPEG.encode(sharpened, quality=92)
    return Response(result, mimetype="image/jpeg", headers={
        "Cache-Control": "no-cache",
        "X-Enhance-Method": "local-fallback",
    })


# Lazy-loaded upscale models
_esrgan_model = None
_aura_model = None
_upscale_backend = None  # "aura" | "esrgan" | "classical"


def _init_upscale_backend():
    """Try loading best available upscale model: AuraSR > Real-ESRGAN > classical."""
    global _aura_model, _esrgan_model, _upscale_backend

    if _upscale_backend is not None:
        return

    # Try AuraSR (GigaGAN-based, much better quality)
    try:
        from aura_sr import AuraSR
        print("[ENHANCE] Loading AuraSR model...")
        _aura_model = AuraSR.from_pretrained("fal/AuraSR-v2")
        _upscale_backend = "aura"
        print("[ENHANCE] AuraSR ready (GigaGAN 4x)")
        return
    except Exception as e:
        print(f"[ENHANCE] AuraSR not available: {e}")

    # Try Real-ESRGAN via spandrel
    import torch
    model_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "RealESRGAN_x4.pth")
    if os.path.exists(model_path):
        try:
            from spandrel import ModelLoader
            print("[ENHANCE] Loading Real-ESRGAN model...")
            _esrgan_model = ModelLoader().load_from_file(model_path)
            _esrgan_model.eval()
            _esrgan_model = _esrgan_model.to(torch.device("mps"))
            _upscale_backend = "esrgan"
            print("[ENHANCE] Real-ESRGAN ready (MPS)")
            return
        except Exception as e:
            print(f"[ENHANCE] Failed to load ESRGAN: {e}")

    _upscale_backend = "classical"
    print("[ENHANCE] Using classical upscale pipeline (no AI model)")


def _esrgan_pass(crop):
    """Single Real-ESRGAN 4x pass on MPS."""
    import torch
    global _esrgan_model

    if _esrgan_model is None:
        return None

    try:
        img = crop.astype(np.float32) / 255.0
        tensor = torch.from_numpy(img).permute(2, 0, 1).unsqueeze(0).to("mps")
        with torch.no_grad():
            output = _esrgan_model(tensor)
        result = output.squeeze(0).permute(1, 2, 0).cpu().numpy()
        return (result * 255).clip(0, 255).astype(np.uint8)
    except Exception as e:
        print(f"[ENHANCE] ESRGAN error: {e}")
        return None


def _aura_pass(crop):
    """Single AuraSR 4x pass."""
    from PIL import Image

    if _aura_model is None:
        return None

    try:
        pil_img = Image.fromarray(crop[:, :, ::-1])  # BGR to RGB
        result_pil = _aura_model.upscale_4x(pil_img)
        result = np.array(result_pil)[:, :, ::-1]  # RGB back to BGR
        return result
    except Exception as e:
        print(f"[ENHANCE] AuraSR error: {e}")
        return None


def _classical_upscale(crop, scale=4):
    """High-quality classical upscale with bilateral filter + detail enhancement."""
    import cv2

    h, w = crop.shape[:2]
    denoised = cv2.bilateralFilter(crop, 5, 50, 50)
    upscaled = cv2.resize(denoised, (w * scale, h * scale), interpolation=cv2.INTER_LANCZOS4)
    enhanced = cv2.detailEnhance(upscaled, sigma_s=8, sigma_r=0.12)
    return enhanced


def _ai_upscale(crop, passes=2):
    """Multi-pass AI upscale using best available backend."""
    import cv2

    _init_upscale_backend()

    t0 = time.time()

    if _upscale_backend == "aura":
        upscale_fn = _aura_pass
    elif _upscale_backend == "esrgan":
        upscale_fn = _esrgan_pass
    else:
        upscale_fn = None

    # Pass 1
    result = upscale_fn(crop) if upscale_fn else None
    if result is None:
        result = _classical_upscale(crop, scale=4)

    t1 = time.time()
    print(f"[ENHANCE] Pass 1 ({_upscale_backend}): {crop.shape[:2]} -> {result.shape[:2]} in {t1-t0:.1f}s")

    if passes < 2:
        return result

    # Pass 2: cap input to 512px, then 4x again
    ph, pw = result.shape[:2]
    if max(ph, pw) > 512:
        scale = 512 / max(ph, pw)
        pass2_input = cv2.resize(result, (int(pw * scale), int(ph * scale)), interpolation=cv2.INTER_AREA)
    else:
        pass2_input = result

    result2 = upscale_fn(pass2_input) if upscale_fn else None
    if result2 is None:
        result2 = _classical_upscale(pass2_input, scale=4)

    t2 = time.time()
    total_scale = result2.shape[1] / crop.shape[1]
    print(f"[ENHANCE] Pass 2 ({_upscale_backend}): {pass2_input.shape[:2]} -> {result2.shape[:2]} ({total_scale:.0f}x total) in {t2-t1:.1f}s")

    return result2


# --- On-demand stream management ---
_active_viewers: dict[int, int] = {}  # channel -> viewer count
_viewer_lock = threading.Lock()


@app.route("/streams/activate", methods=["POST"])
def activate_streams():
    """Start all streams in live mode (called when Electron app opens)."""
    started = []
    for ch, s in streams.items():
        if ch == ALWAYS_ON_CHANNEL and s.running:
            s.set_mode("live")
            continue
        if not s.running:
            s.set_mode("live")
            s.start()
            if sentinel_engine and sentinel_engine.running:
                s.add_frame_callback(sentinel_engine.on_frame)
                sentinel_engine.set_stream_buffers(streams)
            started.append(ch)
            print(f"[STREAM] CH{ch} started (app opened)")
    return {"started": started, "always_on": ALWAYS_ON_CHANNEL}


@app.route("/streams/deactivate", methods=["POST"])
def deactivate_streams():
    """Stop on-demand streams, switch always-on to sentinel mode."""
    stopped = []
    for ch, s in streams.items():
        if ch == ALWAYS_ON_CHANNEL:
            s.set_mode("sentinel")
            continue
        if s.running:
            s.stop()
            stopped.append(ch)
            print(f"[STREAM] CH{ch} stopped (app closed)")
    return {"stopped": stopped, "always_on": ALWAYS_ON_CHANNEL}


@app.route("/streams/status")
def streams_status():
    """Return which streams are running and their mode."""
    return {
        "streams": {
            ch: {"running": s.running, "mode": s._mode, "fps": s._target_fps, "quality": s._jpeg_quality}
            for ch, s in streams.items()
        },
        "always_on": ALWAYS_ON_CHANNEL,
    }


@app.route("/config")
def config():
    """Return camera config as JSON (for Electron app)."""
    return {
        "cameras": [{"id": c["id"], "name": c["name"], "type": c["type"]} for c in CAMERAS],
        "channels": NUM_CAMERAS,
        "channel_names": CHANNEL_NAMES,
        "title": TITLE,
        "always_on_channel": ALWAYS_ON_CHANNEL,
    }


@app.route("/health")
def health():
    """Health check endpoint."""
    alive = {ch: (s._jpeg is not None) for ch, s in streams.items()}
    return {"status": "ok", "streams": alive}


# --- Setup Wizard Endpoints ---

@app.route("/setup/status")
def setup_status():
    """Check if config exists and is valid."""
    config_exists = os.path.exists(CONFIG_PATH)
    config_valid = False
    if config_exists:
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            # Valid if has cameras array with entries, or legacy dvr_ip
            if cfg.get("cameras") and len(cfg["cameras"]) > 0:
                config_valid = True
            elif cfg.get("dvr_ip") and cfg.get("dvr_pass"):
                config_valid = True
        except Exception:
            pass
    return jsonify({
        "config_exists": config_exists,
        "config_valid": config_valid,
    })


@app.route("/setup/save", methods=["POST"])
def setup_save():
    """Save config from setup wizard. Supports both RTSP and Dahua DVR configs."""
    data = request.get_json(force=True)

    camera_type = data.get("camera_type", "dahua")

    if camera_type == "rtsp":
        # New RTSP camera format
        cameras_data = data.get("cameras", [])
        cfg = {
            "title": data.get("title", "CAMAI"),
            "cameras": [],
            "always_on_channel": data.get("always_on_channel", 1),
        }
        for i, cam in enumerate(cameras_data):
            cfg["cameras"].append({
                "name": cam.get("name", f"Camera {i + 1}"),
                "type": "rtsp",
                "url": cam.get("url", ""),
            })
    else:
        # Legacy Dahua DVR format
        dvr_config = data.get("dvr", {})
        cfg = {
            "dvr_ip": dvr_config.get("ip", ""),
            "dvr_port": dvr_config.get("port", 80),
            "dvr_user": dvr_config.get("user", "admin"),
            "dvr_pass": dvr_config.get("pass", ""),
            "channels": dvr_config.get("channels", 4),
            "channel_names": dvr_config.get("channel_names", []),
            "title": dvr_config.get("title", data.get("title", "CAMAI")),
            "always_on_channel": dvr_config.get("always_on_channel", 1),
        }
        # Fill in missing channel names
        while len(cfg["channel_names"]) < cfg["channels"]:
            cfg["channel_names"].append(f"Camera {len(cfg['channel_names']) + 1}")

    # Write config.json
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    try:
        with open(config_path, "w") as f:
            json.dump(cfg, f, indent=4)
        print(f"[SETUP] Saved config.json")
    except Exception as e:
        return jsonify({"error": f"Failed to save config: {e}"}), 500

    # Build and write sentinel_config.json if provided
    sentinel_cfg = data.get("sentinel", {})
    if sentinel_cfg:
        sentinel_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sentinel_config.json")
        try:
            with open(sentinel_path, "w") as f:
                json.dump(sentinel_cfg, f, indent=4)
            print(f"[SETUP] Saved sentinel_config.json")
        except Exception as e:
            print(f"[SETUP] Warning: Failed to save sentinel_config: {e}")

    # Write .env if API keys provided
    env_vars = {}
    if data.get("fal_api_key"):
        env_vars["FAL_API_KEY"] = data["fal_api_key"]
    if data.get("telegram_bot_token"):
        env_vars["TELEGRAM_BOT_TOKEN"] = data["telegram_bot_token"]
    if data.get("telegram_chat_id"):
        env_vars["TELEGRAM_CHAT_ID"] = data["telegram_chat_id"]

    if env_vars:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        try:
            with open(env_path, "w") as f:
                for k, v in env_vars.items():
                    f.write(f"{k}={v}\n")
            print(f"[SETUP] Saved .env")
        except Exception as e:
            print(f"[SETUP] Warning: Failed to save .env: {e}")

    return jsonify({"status": "ok", "message": "Configuration saved. Restart the app to apply."})


@app.route("/setup/test", methods=["POST"])
def setup_test():
    """Test camera connection. Supports both RTSP and Dahua DVR."""
    data = request.get_json(force=True)
    camera_type = data.get("type", "dahua")

    if camera_type == "rtsp":
        url = data.get("url", "")
        if not url:
            return jsonify({"success": False, "error": "No RTSP URL provided"})

        try:
            options = {
                "rtsp_transport": "tcp",
                "stimeout": "5000000",
            }
            container = av.open(url, options=options, timeout=5.0)
            # Try to grab one frame
            video_stream = container.streams.video[0]
            frame = None
            for packet in container.demux(video_stream):
                for f in packet.decode():
                    frame = f
                    break
                if frame is not None:
                    break
            container.close()

            if frame is not None:
                return jsonify({
                    "success": True,
                    "message": f"Connected: {video_stream.codec_context.name} {video_stream.width}x{video_stream.height}",
                })
            else:
                return jsonify({"success": False, "error": "Connected but no frames received"})

        except av.error.InvalidDataError as e:
            return jsonify({"success": False, "error": f"Invalid stream data: {e}"})
        except av.error.OSError as e:
            return jsonify({"success": False, "error": f"Cannot connect: {e}"})
        except Exception as e:
            err_str = str(e)
            if "Connection refused" in err_str:
                return jsonify({"success": False, "error": "Connection refused. Check IP and port."})
            elif "timed out" in err_str.lower() or "timeout" in err_str.lower():
                return jsonify({"success": False, "error": "Connection timed out."})
            elif "401" in err_str or "Unauthorized" in err_str:
                return jsonify({"success": False, "error": "Authentication failed. Check username/password in URL."})
            return jsonify({"success": False, "error": err_str})

    else:
        # Legacy Dahua test
        ip = data.get("ip", "")
        user = data.get("user", "admin")
        password = data.get("pass", "")

        if not ip:
            return jsonify({"success": False, "error": "No IP provided"})

        try:
            test_url = f"http://{ip}/cgi-bin/snapshot.cgi?channel=1"
            resp = requests.get(
                test_url,
                auth=HTTPDigestAuth(user, password),
                timeout=5,
            )
            if resp.status_code == 200 and len(resp.content) > 100:
                return jsonify({"success": True, "message": "Connection successful"})
            else:
                return jsonify({"success": False, "error": f"HTTP {resp.status_code}"})
        except requests.exceptions.ConnectionError:
            return jsonify({"success": False, "error": "Cannot reach DVR. Check IP address."})
        except requests.exceptions.Timeout:
            return jsonify({"success": False, "error": "Connection timed out."})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})


@app.route("/setup/test-telegram", methods=["POST"])
def setup_test_telegram():
    """Send a test message via Telegram to verify bot token and chat ID."""
    data = request.get_json(force=True)
    bot_token = data.get("bot_token", "").strip()
    chat_id = data.get("chat_id", "").strip()

    if not bot_token or not chat_id:
        return jsonify({"success": False, "error": "Bot token and chat ID are required"})

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": "Camai connected successfully!\nYou will receive security alerts here.",
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        result = resp.json()
        if result.get("ok"):
            return jsonify({"success": True, "message": "Test message sent! Check your Telegram."})
        else:
            error_desc = result.get("description", "Unknown error")
            return jsonify({"success": False, "error": error_desc})
    except requests.exceptions.Timeout:
        return jsonify({"success": False, "error": "Telegram API timed out"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# --- Sentinel AI Engine ---
sentinel_engine = None


def start_sentinel() -> None:
    """Initialize and start the Sentinel AI engine."""
    global sentinel_engine

    sentinel_config_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "sentinel_config.json",
    )

    try:
        from sentinel.engine import SentinelEngine
        from sentinel.api import sentinel_bp, init_api

        sentinel_engine = SentinelEngine(sentinel_config_path)

        if not sentinel_engine.running is False or sentinel_engine.config.get("enabled", True):
            sentinel_engine.set_stream_buffers(streams)

            always_on_stream = streams.get(ALWAYS_ON_CHANNEL)
            if always_on_stream:
                always_on_stream.add_frame_callback(sentinel_engine.on_frame)
                print(f"[SENTINEL] Registered callback for CH{ALWAYS_ON_CHANNEL} (always-on)")

            init_api(sentinel_engine)
            app.register_blueprint(sentinel_bp)

            sentinel_engine.start()
            print("\033[96m[SENTINEL] AI surveillance active\033[0m")
        else:
            print("[SENTINEL] Disabled in config")

    except ImportError as e:
        print(f"[SENTINEL] Import error (missing deps?): {e}")
        print("[SENTINEL] Running without AI detection")
    except Exception as e:
        print(f"[SENTINEL] Init error: {e}")
        print("[SENTINEL] Running without AI detection")


if __name__ == "__main__":
    print(f"Starting Camai Server — {NUM_CAMERAS} cameras")
    if CAMERAS:
        cam_types = set(c["type"] for c in CAMERAS)
        print(f"Cameras: {', '.join(c['name'] for c in CAMERAS)} | Types: {', '.join(cam_types)} | Title: {TITLE}")
        start_streams()
        start_sentinel()
    else:
        print("No cameras configured — setup wizard available at /setup/status")
    app.run(host="127.0.0.1", port=5555, threaded=True)
