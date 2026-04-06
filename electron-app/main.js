const { app, BrowserWindow, Tray, nativeImage, screen, net, ipcMain } = require("electron");
const path = require("path");
const { spawn } = require("child_process");

const FLASK_SERVER = "http://127.0.0.1:5555";
let serverProcess = null;

let tray = null;
let win = null;

const WINDOW_WIDTH = 820;
const WINDOW_HEIGHT = 540;

app.dock?.hide();

function createWindow() {
  win = new BrowserWindow({
    width: WINDOW_WIDTH,
    height: WINDOW_HEIGHT,
    show: false,
    frame: false,
    transparent: true,
    resizable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    hasShadow: true,
    vibrancy: "under-window",
    visualEffectState: "active",
    webPreferences: {
      nodeIntegration: true,
      contextIsolation: false,
    },
  });

  win.loadFile(path.join(__dirname, "index.html"));

  win.on("blur", () => {
    if (win && win.isVisible()) {
      win.hide();
    }
  });

  win.on("closed", () => {
    win = null;
  });
}

function toggleWindow() {
  if (!win) {
    createWindow();
    positionAndShow();
    return;
  }

  if (win.isVisible()) {
    win.hide();
  } else {
    positionAndShow();
  }
}

function positionAndShow() {
  if (!tray || !win) return;

  const trayBounds = tray.getBounds();
  const display = screen.getDisplayNearestPoint({
    x: trayBounds.x,
    y: trayBounds.y,
  });

  const winBounds = win.getBounds();
  const x = Math.round(
    trayBounds.x + trayBounds.width / 2 - winBounds.width / 2
  );
  const y = Math.round(trayBounds.y + trayBounds.height + 4);

  // Clamp to screen
  const maxX = display.bounds.x + display.bounds.width - winBounds.width - 10;
  const clampedX = Math.max(display.bounds.x + 10, Math.min(x, maxX));

  win.setPosition(clampedX, y);
  win.show();
  win.focus();
}

// IPC: renderer toggle stream on/off
ipcMain.on("streams-activate", () => { activateStreams(); });
ipcMain.on("streams-deactivate", () => { deactivateStreams(); });

// Start Python server if not already running
function startServer() {
  const http = require("http");
  const req = http.get(FLASK_SERVER + "/health", (res) => {
    console.log("[Camai] Server already running");
  });
  req.on("error", () => {
    console.log("[Camai] Starting Python server...");
    const serverDir = path.resolve(__dirname, "..");
    serverProcess = spawn("/opt/homebrew/bin/python3", ["server.py"], {
      cwd: serverDir,
      stdio: "ignore",
      detached: false,
    });
    serverProcess.on("error", (err) => console.error("[Camai] Server error:", err));
    serverProcess.on("exit", (code) => { serverProcess = null; console.log("[Camai] Server exited:", code); });
  });
  req.end();
}

app.whenReady().then(() => {
  startServer();

  // Create proper 22x22 tray icon — camera dot
  const icon = nativeImage.createEmpty();
  const size = { width: 22, height: 22 };
  const canvas = Buffer.alloc(22 * 22 * 4, 0);
  // Draw a filled circle (6px radius, centered)
  for (let y = 0; y < 22; y++) {
    for (let x = 0; x < 22; x++) {
      const dx = x - 11, dy = y - 11;
      if (dx * dx + dy * dy <= 36) { // radius 6
        const i = (y * 22 + x) * 4;
        canvas[i] = 0; canvas[i+1] = 0; canvas[i+2] = 0; canvas[i+3] = 255;
      }
    }
  }
  const trayIcon = nativeImage.createFromBuffer(canvas, { width: 22, height: 22 });
  trayIcon.setTemplateImage(true);

  tray = new Tray(trayIcon);
  tray.setToolTip("Camai \u2014 AI Surveillance");

  tray.on("click", () => {
    toggleWindow();
  });

  tray.on("right-click", () => {
    deactivateStreams();
    if (serverProcess) { serverProcess.kill(); serverProcess = null; }
    app.quit();
  });

  createWindow();
});

app.on("window-all-closed", (e) => {
  e.preventDefault();
});

function deactivateStreams() {
  try {
    const request = net.request({
      method: "POST",
      url: `${FLASK_SERVER}/streams/deactivate`,
    });
    request.end();
  } catch {}
}

function activateStreams() {
  try {
    const request = net.request({
      method: "POST",
      url: `${FLASK_SERVER}/streams/activate`,
    });
    request.end();
  } catch {}
}
