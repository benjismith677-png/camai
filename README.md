# Camai

**AI-Powered Surveillance Camera System for macOS**

Camai is a macOS menu bar application that connects to any RTSP IP camera or Dahua DVR system, providing live camera feeds with AI-powered object detection, zone-based alerts, and image super-resolution enhancement.

## Supported Cameras

- **Any RTSP IP camera** — Hikvision, Reolink, Dahua, Amcrest, TP-Link, Tapo, Uniview, and more
- **Dahua DVR** — Legacy support via proprietary HTTP endpoints

## Features

- **Menu Bar App** — Lives in your macOS menu bar, drops down with a click
- **Multi-Camera Grid** — 2x2 grid view with click-to-expand single camera mode
- **AI Object Detection** — YOLOv8 with CoreML/Apple Neural Engine acceleration
- **Zone-Based Alerts** — Draw custom surveillance zones with configurable alert levels
- **Behavior Analysis** — Detects lingering, approach patterns, night activity
- **AI Image Enhancement** — Right-click drag to select and enhance any region with fal.ai super-resolution
- **Event Timeline** — Browse detected events with snapshots and video clips
- **Telegram Alerts** — Get notified on your phone for security events
- **macOS Notifications** — Native notification center integration
- **Power Management** — Sentinel mode reduces bandwidth when app is hidden
- **Real-time WebSocket** — Live event streaming to the UI
- **Setup Wizard** — First-run configuration wizard built into the app

## Requirements

- **macOS** (Apple Silicon recommended for CoreML acceleration)
- **Python 3.10+**
- **Node.js 18+**
- **RTSP-capable IP camera(s)** or **Dahua DVR** with network access

## Quick Start

1. **Clone the repository**

```bash
git clone https://github.com/hrrcne/camai.git
cd camai
```

2. **Install Python dependencies**

```bash
pip3 install -r requirements.txt
```

3. **Configure your cameras**

Copy the example config and edit with your camera details:

```bash
cp config.example.json config.json
cp sentinel_config.example.json sentinel_config.json
```

Edit `config.json` for **RTSP cameras**:
```json
{
    "title": "MY HOME",
    "cameras": [
        {
            "name": "Front Door",
            "type": "rtsp",
            "url": "rtsp://admin:password@192.168.1.100:554/stream1"
        },
        {
            "name": "Backyard",
            "type": "rtsp",
            "url": "rtsp://admin:password@192.168.1.101:554/h264Preview_01_main"
        }
    ],
    "always_on_channel": 1
}
```

Or for **Dahua DVR** (legacy format):
```json
{
    "dvr_ip": "192.168.1.100",
    "dvr_user": "admin",
    "dvr_pass": "your_password",
    "channels": 4,
    "channel_names": ["Front Door", "Backyard", "Garage", "Driveway"],
    "title": "MY HOME",
    "always_on_channel": 1
}
```

Or skip this step and use the built-in **Setup Wizard** when you first launch the app.

4. **Run**

```bash
./start.sh
```

This will:
- Check and install any missing Python packages
- Export the YOLOv8n CoreML model (first run only)
- Start the Python MJPEG server on port 5555
- Launch the Electron menu bar app

## Common RTSP URL Formats

| Brand | URL Format |
|-------|-----------|
| Hikvision | `rtsp://admin:pass@IP:554/Streaming/Channels/101` |
| Reolink | `rtsp://admin:pass@IP:554/h264Preview_01_main` |
| Dahua | `rtsp://admin:pass@IP:554/cam/realmonitor?channel=1&subtype=0` |
| Amcrest | `rtsp://admin:pass@IP:554/cam/realmonitor?channel=1&subtype=0` |
| TP-Link Tapo | `rtsp://admin:pass@IP:554/stream1` |
| Uniview | `rtsp://admin:pass@IP:554/unicast/c1/s0/live` |
| Generic | `rtsp://IP:554/stream` or `rtsp://IP:554/h264` |

**Note**: Most cameras use port 554 for RTSP. Username/password are embedded in the URL.

## Configuration

### Camera Config (`config.json`)

#### RTSP Format (recommended)

| Field | Description |
|-------|-------------|
| `title` | Title shown in the app header |
| `cameras` | Array of camera objects |
| `cameras[].name` | Display name for the camera |
| `cameras[].type` | `"rtsp"` for IP cameras |
| `cameras[].url` | Full RTSP URL including credentials |
| `always_on_channel` | Channel that runs 24/7 for AI detection |

#### Dahua DVR Format (legacy)

| Field | Description |
|-------|-------------|
| `dvr_ip` | DVR IP address on your network |
| `dvr_user` | DVR username (usually `admin`) |
| `dvr_pass` | DVR password |
| `channels` | Number of camera channels (1-16) |
| `channel_names` | Array of display names for each camera |
| `title` | Title shown in the app header |
| `always_on_channel` | Channel that runs 24/7 for AI detection |

### Sentinel Config (`sentinel_config.json`)

Controls the AI detection engine:

- **Model** — YOLOv8n path, confidence threshold, max detection FPS
- **Motion** — Global and per-camera motion sensitivity
- **Zones** — Surveillance zone polygons (created via the Zone Editor UI)
- **Alerts** — Cooldown timers, macOS notifications, Telegram settings
- **Recording** — Snapshot and video clip settings
- **Behavior** — Lingering thresholds per object class
- **Day/Night** — Latitude/longitude for astronomical night detection

See `sentinel_config.example.json` for all options.

## AI Image Enhancement

Camai supports AI super-resolution for zoomed camera regions:

1. Right-click and drag to select a region on any camera
2. Click the **PHOTA AI** button that appears
3. Watch the military-style enhancement animation
4. The region is enhanced using fal.ai's super-resolution model

### Setup fal.ai (Optional)

1. Create a free account at [fal.ai](https://fal.ai)
2. Get your API key from [fal.ai/dashboard/keys](https://fal.ai/dashboard/keys)
3. Add to `.env`:
   ```
   FAL_API_KEY=your_api_key_here
   ```

Without fal.ai, Camai falls back to local classical upscaling.

## Telegram Alerts (Optional)

1. Create a bot via [@BotFather](https://t.me/botfather) on Telegram
2. Get your chat ID (send a message to your bot, then check `https://api.telegram.org/bot<TOKEN>/getUpdates`)
3. Configure in `sentinel_config.json` under `alerts.telegram`, or add to `.env`:
   ```
   TELEGRAM_BOT_TOKEN=your_bot_token
   TELEGRAM_CHAT_ID=your_chat_id
   ```

## Building the App

To package as a standalone macOS app:

```bash
cd electron-app
npm install
npm run build
```

The built app will be in `electron-app/dist/`.

## Architecture

```
camai/
├── server.py              # Flask MJPEG server (RTSP + Dahua) + AI enhance endpoints
├── sentinel/              # AI detection engine
│   ├── engine.py          # Main orchestrator
│   ├── detector.py        # YOLOv8 inference (CoreML/MPS)
│   ├── tracker.py         # IoU-based multi-object tracker
│   ├── zones.py           # Polygon zone management
│   ├── behavior.py        # Lingering/approach analysis
│   ├── alerter.py         # Alert dispatch (macOS/Telegram/WS)
│   ├── recorder.py        # Snapshot + video clip recording
│   ├── db.py              # SQLite event database
│   ├── motion.py          # MOG2 motion detection
│   ├── daynight.py        # Day/night detection
│   ├── ws_server.py       # WebSocket alert server
│   ├── api.py             # REST API blueprint
│   └── config.py          # Config loader with hot-reload
├── electron-app/          # Electron menu bar UI
│   ├── main.js            # Main process (tray, window)
│   ├── renderer.js        # Camera grid, zoom, enhance, events, zones
│   ├── index.html         # App layout
│   └── style.css          # Dark theme styles
└── scripts/
    └── export_coreml.py   # One-time CoreML model export
```

### Data Flow

```
RTSP Camera / Dahua DVR  -->  server.py (decode, MJPEG serve)
                                  |
                                  v
                            Sentinel Engine
                            (motion -> YOLO -> track -> zone -> behavior -> alert)
                                  |
                                  +-- macOS notifications
                                  +-- Telegram messages
                                  +-- WebSocket -> Electron UI
                                  +-- SQLite database
                                  +-- Snapshot/clip recording
```

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| Click camera | Expand to full view |
| Right-click drag | Select region for zoom/enhance |
| `E` | Toggle event timeline |
| `Z` | Toggle zone editor |
| `ESC` | Close panels, reset zoom, collapse grid |

## License

MIT License - see [LICENSE](LICENSE) for details.
