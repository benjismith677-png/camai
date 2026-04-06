#!/bin/bash
# Start Camai — Python server + AI Surveillance + Electron app
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_PID=""

cleanup() {
  echo ""
  echo "Shutting down..."
  if [ -n "$PYTHON_PID" ] && kill -0 "$PYTHON_PID" 2>/dev/null; then
    kill "$PYTHON_PID" 2>/dev/null
    wait "$PYTHON_PID" 2>/dev/null
    echo "Python server stopped."
  fi
  exit 0
}

trap cleanup EXIT INT TERM

echo "=== Camai — AI Surveillance Camera System ==="
echo ""

# --- Check/install Python dependencies ---
echo "Checking Python dependencies..."
MISSING=""
python3 -c "import ultralytics" 2>/dev/null || MISSING="$MISSING ultralytics"
python3 -c "import cv2" 2>/dev/null || MISSING="$MISSING opencv-python"
python3 -c "import websockets" 2>/dev/null || MISSING="$MISSING websockets"

if [ -n "$MISSING" ]; then
  echo "Installing missing packages:$MISSING"
  pip3 install $MISSING
fi

# --- Create data directories ---
mkdir -p "$DIR/data/snapshots" "$DIR/data/clips" "$DIR/data/db"
mkdir -p "$DIR/models"

# --- Export CoreML model if not exists ---
if [ ! -d "$DIR/models/yolov8n.mlpackage" ]; then
  echo "Exporting YOLOv8n CoreML model (one-time)..."
  cd "$DIR"
  python3 scripts/export_coreml.py || {
    echo "CoreML export failed (will use PyTorch fallback)"
    # Ensure PT model exists at minimum
    if [ ! -f "$DIR/models/yolov8n.pt" ]; then
      echo "Downloading YOLOv8n..."
      python3 -c "from ultralytics import YOLO; m = YOLO('yolov8n.pt'); import shutil; shutil.move('yolov8n.pt', 'models/yolov8n.pt')" 2>/dev/null || true
    fi
  }
fi

# Load .env if it exists
if [ -f "$DIR/.env" ]; then
  echo "Loading .env..."
  export $(grep -v '^#' "$DIR/.env" | xargs)
fi

# Start Python MJPEG server + Sentinel
echo ""
echo "Starting MJPEG server on :5555 + AI Surveillance..."
cd "$DIR"
python3 server.py &
PYTHON_PID=$!

# Wait for server to be ready
echo "Waiting for server..."
for i in $(seq 1 30); do
  if curl -sf http://127.0.0.1:5555/health >/dev/null 2>&1; then
    echo "Server ready."
    break
  fi
  sleep 0.5
done

# Start Electron app
echo "Starting Electron app..."
cd "$DIR/electron-app"

if [ ! -d "node_modules" ]; then
  echo "Installing dependencies..."
  npm install
fi

npx electron .

echo "Electron app closed."
