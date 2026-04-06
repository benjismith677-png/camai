const { ipcRenderer } = require("electron");
const SERVER = "http://127.0.0.1:5555";
const WS_URL = "ws://127.0.0.1:5556";
let config = null;
let expandedChannel = null;
let alwaysOnChannel = 1;
let streamsActive = true;

// ===================================================================
//  EVENT TIMELINE STATE
// ===================================================================
let eventPanelOpen = false;
let eventPollTimer = null;
let wsConnection = null;
let cachedEvents = [];

// ===================================================================
//  ZONE EDITOR STATE
// ===================================================================
let zoneEditorActive = false;
let zoneDrawingVertices = [];  // [{x, y}] in image-normalized coords
let zoneDrawingCamera = null;  // which camera we're drawing on
let zoneMousePos = null;       // current mouse position for line preview
let cachedZones = {};          // zones from server

async function init() {
  // Check if setup is needed
  let needsSetup = false;
  try {
    const setupResp = await fetch(`${SERVER}/setup/status`);
    const setupData = await setupResp.json();
    if (!setupData.config_valid) {
      needsSetup = true;
    }
  } catch {
    // Server not running yet — show setup wizard after a delay
    needsSetup = true;
  }

  if (needsSetup) {
    // Try again after server starts up
    setTimeout(async () => {
      try {
        const setupResp = await fetch(`${SERVER}/setup/status`);
        const setupData = await setupResp.json();
        if (!setupData.config_valid) {
          showSetupWizard();
          return;
        }
      } catch {
        showSetupWizard();
        return;
      }
      // Config became valid, proceed normally
      await loadAndStart();
    }, 2000);
    return;
  }

  await loadAndStart();
}

async function loadAndStart() {
  try {
    const resp = await fetch(`${SERVER}/config`);
    config = await resp.json();
  } catch {
    config = {
      channels: 0,
      cameras: [],
      channel_names: [],
      title: "CAMAI",
    };
  }

  // Normalize: derive channels/channel_names from cameras array if present
  if (config.cameras && config.cameras.length > 0) {
    config.channels = config.cameras.length;
    config.channel_names = config.cameras.map(c => c.name);
  }
  if (!config.channels) config.channels = 0;
  if (!config.channel_names) config.channel_names = [];

  alwaysOnChannel = config.always_on_channel || 1;

  const camCount = config.channels;
  const camTypes = config.cameras ? [...new Set(config.cameras.map(c => c.type))].join("/").toUpperCase() : "N/A";

  document.getElementById("title").textContent = config.title;
  document.getElementById("status").textContent = `${camCount}/${camCount} ONLINE`;
  document.getElementById("footer-text").textContent = camCount > 0 ? `${camTypes} \xb7 ${camCount} camera${camCount !== 1 ? "s" : ""}` : "Not configured";

  // Activate all streams when app opens
  ipcRenderer.send("streams-activate");

  buildGrid();
  checkHealth();
  setInterval(checkHealth, 5000);
  updateClock();
  setInterval(updateClock, 1000);

  // Init event panel controls
  initEventPanel();

  // Init zone editor controls
  initZoneEditor();

  // Fetch zones
  fetchZones();

  // Connect WebSocket for real-time events
  connectWebSocket();

  // Power toggle
  initPowerToggle();
}

// ===================================================================
//  SETUP WIZARD
// ===================================================================

function showSetupWizard() {
  const grid = document.getElementById("grid");
  const footer = document.getElementById("footer");
  const wizard = document.getElementById("setup-wizard");

  grid.style.display = "none";
  footer.style.display = "none";
  wizard.style.display = "flex";

  document.getElementById("title").textContent = "CAMAI";
  document.getElementById("status").textContent = "SETUP";
  document.getElementById("status-dot").style.background = "rgba(255,255,255,0.15)";

  let currentStep = 0;
  const totalSteps = 5;

  // Wizard data
  const wizardData = {
    camera_type: "rtsp",  // "rtsp" or "dahua"
    // RTSP cameras
    cameras: [{ name: "Camera 1", url: "" }],
    // Dahua DVR (legacy)
    dvr_ip: "", dvr_port: 80, dvr_user: "admin", dvr_pass: "",
    channels: 4, channel_names: [],
    // Common
    title: "CAMAI",
    fal_api_key: "",
    telegram_bot_token: "", telegram_chat_id: "", telegram_min_level: "warning",
  };

  function renderStep() {
    const dots = Array.from({length: totalSteps}, (_, i) => {
      const cls = i < currentStep ? "done" : i === currentStep ? "active" : "";
      return `<div class="setup-step-dot ${cls}"></div>`;
    }).join("");

    if (currentStep === 0) {
      // Step 0: Camera Type Selection + Connection
      const isRTSP = wizardData.camera_type === "rtsp";
      wizard.innerHTML = `
        <h2>Camera Connection</h2>
        <p class="setup-subtitle">Choose your camera type and configure</p>
        <div class="setup-steps">${dots}</div>
        <div class="setup-form">
          <div class="setup-field">
            <label class="setup-label">Camera Type</label>
            <div class="setup-type-toggle">
              <button class="setup-type-btn ${isRTSP ? 'active' : ''}" id="s-type-rtsp">RTSP / IP Camera</button>
              <button class="setup-type-btn ${!isRTSP ? 'active' : ''}" id="s-type-dahua">Dahua DVR</button>
            </div>
          </div>
          <div id="s-camera-fields"></div>
          <div class="setup-btn-row">
            <button class="setup-btn" id="s-next">NEXT</button>
          </div>
        </div>
      `;

      function renderCameraFields() {
        const fields = document.getElementById("s-camera-fields");
        if (wizardData.camera_type === "rtsp") {
          let camerasHtml = wizardData.cameras.map((cam, i) => `
            <div class="setup-camera-entry" data-idx="${i}">
              <div class="setup-input-row">
                <div class="setup-field" style="flex:1">
                  <label class="setup-label">Name</label>
                  <input class="setup-input rtsp-name" value="${cam.name}" data-idx="${i}" placeholder="Camera ${i + 1}">
                </div>
                <div class="setup-field" style="flex:3">
                  <label class="setup-label">RTSP URL</label>
                  <input class="setup-input rtsp-url" value="${cam.url}" data-idx="${i}" placeholder="rtsp://admin:pass@192.168.1.100:554/stream1">
                </div>
                <div class="setup-field" style="flex:0 0 auto; display:flex; align-items:flex-end; gap:4px">
                  <button class="setup-btn test-btn small rtsp-test" data-idx="${i}" style="padding:8px 12px; font-size:11px">TEST</button>
                  ${wizardData.cameras.length > 1 ? `<button class="setup-btn secondary small rtsp-remove" data-idx="${i}" style="padding:8px 10px; font-size:11px; color:#ff4444">X</button>` : ''}
                </div>
              </div>
              <div class="rtsp-test-result" id="rtsp-result-${i}" style="font-size:11px; margin:-4px 0 8px 0; min-height:16px"></div>
            </div>
          `).join("");

          fields.innerHTML = `
            ${camerasHtml}
            <div class="setup-btn-row" style="justify-content:flex-start">
              <button class="setup-btn secondary small" id="s-add-camera" style="font-size:11px; padding:6px 14px">+ ADD CAMERA</button>
            </div>
            <div class="setup-hint" style="margin-top:12px">
              <p class="setup-hint-title" style="margin-bottom:6px">Common RTSP URL formats:</p>
              <div style="font-size:11px; opacity:0.6; line-height:1.7; font-family:monospace">
                Hikvision: rtsp://admin:pass@IP:554/Streaming/Channels/101<br>
                Reolink: rtsp://admin:pass@IP:554/h264Preview_01_main<br>
                Dahua: rtsp://admin:pass@IP:554/cam/realmonitor?channel=1&subtype=0<br>
                Generic: rtsp://IP:554/stream1
              </div>
            </div>
          `;

          // Test buttons
          fields.querySelectorAll(".rtsp-test").forEach(btn => {
            btn.addEventListener("click", async () => {
              const idx = parseInt(btn.dataset.idx);
              const urlInput = fields.querySelector(`.rtsp-url[data-idx="${idx}"]`);
              const resultEl = document.getElementById(`rtsp-result-${idx}`);
              btn.textContent = "...";
              resultEl.textContent = "";
              resultEl.style.color = "";
              try {
                const resp = await fetch(`${SERVER}/setup/test`, {
                  method: "POST",
                  headers: {"Content-Type": "application/json"},
                  body: JSON.stringify({ type: "rtsp", url: urlInput.value }),
                });
                const data = await resp.json();
                if (data.success) {
                  resultEl.textContent = data.message;
                  resultEl.style.color = "#00ff88";
                } else {
                  resultEl.textContent = data.error || "Failed";
                  resultEl.style.color = "#ff4444";
                }
              } catch {
                resultEl.textContent = "Server error";
                resultEl.style.color = "#ff4444";
              }
              btn.textContent = "TEST";
            });
          });

          // Remove buttons
          fields.querySelectorAll(".rtsp-remove").forEach(btn => {
            btn.addEventListener("click", () => {
              saveCameraInputs();
              wizardData.cameras.splice(parseInt(btn.dataset.idx), 1);
              renderCameraFields();
            });
          });

          // Add camera button
          const addBtn = document.getElementById("s-add-camera");
          if (addBtn) {
            addBtn.addEventListener("click", () => {
              saveCameraInputs();
              wizardData.cameras.push({ name: `Camera ${wizardData.cameras.length + 1}`, url: "" });
              renderCameraFields();
            });
          }
        } else {
          // Dahua DVR fields
          fields.innerHTML = `
            <div class="setup-input-row">
              <div class="setup-field" style="flex:3">
                <label class="setup-label">DVR IP Address</label>
                <input class="setup-input" id="s-ip" placeholder="192.168.1.100" value="${wizardData.dvr_ip}">
              </div>
              <div class="setup-field" style="flex:1">
                <label class="setup-label">Port</label>
                <input class="setup-input" id="s-port" type="number" value="${wizardData.dvr_port}">
              </div>
            </div>
            <div class="setup-input-row">
              <div class="setup-field">
                <label class="setup-label">Username</label>
                <input class="setup-input" id="s-user" value="${wizardData.dvr_user}">
              </div>
              <div class="setup-field">
                <label class="setup-label">Password</label>
                <input class="setup-input" id="s-pass" type="password" value="${wizardData.dvr_pass}" placeholder="DVR password">
              </div>
            </div>
            <div class="setup-btn-row">
              <button class="setup-btn test-btn" id="s-test">TEST CONNECTION</button>
            </div>
          `;

          document.getElementById("s-test").addEventListener("click", async () => {
            const btn = document.getElementById("s-test");
            btn.textContent = "TESTING...";
            btn.className = "setup-btn test-btn";
            try {
              const resp = await fetch(`${SERVER}/setup/test`, {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                  type: "dahua",
                  ip: document.getElementById("s-ip").value,
                  user: document.getElementById("s-user").value,
                  pass: document.getElementById("s-pass").value,
                }),
              });
              const data = await resp.json();
              if (data.success) {
                btn.textContent = "CONNECTED";
                btn.className = "setup-btn test-btn success";
              } else {
                btn.textContent = data.error || "FAILED";
                btn.className = "setup-btn test-btn fail";
              }
            } catch {
              btn.textContent = "SERVER ERROR";
              btn.className = "setup-btn test-btn fail";
            }
            setTimeout(() => { btn.textContent = "TEST CONNECTION"; btn.className = "setup-btn test-btn"; }, 3000);
          });
        }
      }

      function saveCameraInputs() {
        if (wizardData.camera_type === "rtsp") {
          document.querySelectorAll(".rtsp-name").forEach(inp => {
            const idx = parseInt(inp.dataset.idx);
            if (wizardData.cameras[idx]) wizardData.cameras[idx].name = inp.value || `Camera ${idx + 1}`;
          });
          document.querySelectorAll(".rtsp-url").forEach(inp => {
            const idx = parseInt(inp.dataset.idx);
            if (wizardData.cameras[idx]) wizardData.cameras[idx].url = inp.value;
          });
        }
      }

      renderCameraFields();

      // Type toggle
      document.getElementById("s-type-rtsp").addEventListener("click", () => {
        if (wizardData.camera_type === "dahua") {
          // Save Dahua fields first
          const ipEl = document.getElementById("s-ip");
          if (ipEl) {
            wizardData.dvr_ip = ipEl.value;
            wizardData.dvr_port = parseInt(document.getElementById("s-port").value) || 80;
            wizardData.dvr_user = document.getElementById("s-user").value;
            wizardData.dvr_pass = document.getElementById("s-pass").value;
          }
        }
        wizardData.camera_type = "rtsp";
        renderStep();
      });
      document.getElementById("s-type-dahua").addEventListener("click", () => {
        if (wizardData.camera_type === "rtsp") saveCameraInputs();
        wizardData.camera_type = "dahua";
        renderStep();
      });

      document.getElementById("s-next").addEventListener("click", () => {
        if (wizardData.camera_type === "rtsp") {
          saveCameraInputs();
        } else {
          wizardData.dvr_ip = document.getElementById("s-ip").value;
          wizardData.dvr_port = parseInt(document.getElementById("s-port").value) || 80;
          wizardData.dvr_user = document.getElementById("s-user").value;
          wizardData.dvr_pass = document.getElementById("s-pass").value;
        }
        currentStep = 1;
        renderStep();
      });
    }

    else if (currentStep === 1) {
      // Step 1: Camera Names (for RTSP, names already set; for Dahua, channel count + names)
      if (wizardData.camera_type === "rtsp") {
        // RTSP: names already configured in step 0, skip to title
        wizard.innerHTML = `
          <h2>Camera Names</h2>
          <p class="setup-subtitle">Review your camera names</p>
          <div class="setup-steps">${dots}</div>
          <div class="setup-form">
            <div class="setup-field">
              <label class="setup-label">Cameras (${wizardData.cameras.length})</label>
              <div class="setup-channel-list" id="s-names"></div>
            </div>
            <div class="setup-btn-row">
              <button class="setup-btn secondary" id="s-back">BACK</button>
              <button class="setup-btn" id="s-next">NEXT</button>
            </div>
          </div>
        `;
        const namesList = document.getElementById("s-names");
        wizardData.cameras.forEach((cam, i) => {
          namesList.innerHTML += `
            <div class="setup-channel-row">
              <span class="setup-channel-num">${i + 1}.</span>
              <input class="setup-input ch-name" style="flex:1" value="${cam.name}" data-idx="${i}">
            </div>
          `;
        });
        document.getElementById("s-back").addEventListener("click", () => { currentStep = 0; renderStep(); });
        document.getElementById("s-next").addEventListener("click", () => {
          document.querySelectorAll(".ch-name").forEach(inp => {
            const idx = parseInt(inp.dataset.idx);
            if (wizardData.cameras[idx]) wizardData.cameras[idx].name = inp.value || `Camera ${idx + 1}`;
          });
          currentStep = 2;
          renderStep();
        });
      } else {
        // Dahua: channel count + names
        wizard.innerHTML = `
          <h2>Camera Setup</h2>
          <p class="setup-subtitle">How many cameras and their names</p>
          <div class="setup-steps">${dots}</div>
          <div class="setup-form">
            <div class="setup-field">
              <label class="setup-label">Number of Channels (1-16)</label>
              <input class="setup-input" id="s-channels" type="number" min="1" max="16" value="${wizardData.channels}">
            </div>
            <div class="setup-field">
              <label class="setup-label">Channel Names</label>
              <div class="setup-channel-list" id="s-names"></div>
            </div>
            <div class="setup-btn-row">
              <button class="setup-btn secondary" id="s-back">BACK</button>
              <button class="setup-btn" id="s-next">NEXT</button>
            </div>
          </div>
        `;

        const chInput = document.getElementById("s-channels");
        const namesList = document.getElementById("s-names");

        function renderNames() {
          const count = parseInt(chInput.value) || 4;
          namesList.innerHTML = "";
          for (let i = 0; i < count; i++) {
            const existing = wizardData.channel_names[i] || `Camera ${i + 1}`;
            namesList.innerHTML += `
              <div class="setup-channel-row">
                <span class="setup-channel-num">${i + 1}.</span>
                <input class="setup-input ch-name" style="flex:1" value="${existing}" data-idx="${i}">
              </div>
            `;
          }
        }
        renderNames();
        chInput.addEventListener("input", renderNames);

        document.getElementById("s-back").addEventListener("click", () => { currentStep = 0; renderStep(); });
        document.getElementById("s-next").addEventListener("click", () => {
          wizardData.channels = parseInt(chInput.value) || 4;
          wizardData.channel_names = [];
          document.querySelectorAll(".ch-name").forEach(inp => {
            wizardData.channel_names.push(inp.value || `Camera ${parseInt(inp.dataset.idx) + 1}`);
          });
          currentStep = 2;
          renderStep();
        });
      }
    }

    else if (currentStep === 2) {
      wizard.innerHTML = `
        <h2>System Name</h2>
        <p class="setup-subtitle">Title shown in the app header</p>
        <div class="setup-steps">${dots}</div>
        <div class="setup-form">
          <div class="setup-field">
            <label class="setup-label">System Title</label>
            <input class="setup-input" id="s-title" value="${wizardData.title}" placeholder="MY CAMERAS">
          </div>
          <p class="setup-hint">Appears in the top-left of the camera viewer</p>
          <div class="setup-btn-row">
            <button class="setup-btn secondary" id="s-back">BACK</button>
            <button class="setup-btn" id="s-next">NEXT</button>
          </div>
        </div>
      `;
      document.getElementById("s-back").addEventListener("click", () => { currentStep = 1; renderStep(); });
      document.getElementById("s-next").addEventListener("click", () => {
        wizardData.title = document.getElementById("s-title").value || "CAMAI";
        currentStep = 3;
        renderStep();
      });
    }

    else if (currentStep === 3) {
      wizard.innerHTML = `
        <h2>AI Enhancement</h2>
        <p class="setup-subtitle">Optional: fal.ai API key for super-resolution</p>
        <div class="setup-steps">${dots}</div>
        <div class="setup-form">
          <div class="setup-field">
            <label class="setup-label">fal.ai API Key</label>
            <input class="setup-input" id="s-fal" value="${wizardData.fal_api_key}" placeholder="Optional">
          </div>
          <p class="setup-hint">Get a free key at fal.ai/dashboard/keys</p>
          <div class="setup-btn-row">
            <button class="setup-btn secondary" id="s-back">BACK</button>
            <button class="setup-btn" id="s-next">NEXT</button>
          </div>
        </div>
      `;
      document.getElementById("s-back").addEventListener("click", () => { currentStep = 2; renderStep(); });
      document.getElementById("s-next").addEventListener("click", () => {
        wizardData.fal_api_key = document.getElementById("s-fal").value;
        currentStep = 4;
        renderStep();
      });
    }

    else if (currentStep === 4) {
      wizard.innerHTML = `
        <h2>Notifications</h2>
        <p class="setup-subtitle">Optional: Get security alerts on your phone via Telegram</p>
        <div class="setup-steps">${dots}</div>
        <div class="setup-form">
          <div class="setup-field">
            <label class="setup-label">Telegram Bot Token</label>
            <input class="setup-input" id="s-tg-token" value="${wizardData.telegram_bot_token}" placeholder="123456:ABC-DEF...">
          </div>
          <div class="setup-field">
            <label class="setup-label">Telegram Chat ID</label>
            <input class="setup-input" id="s-tg-chat" value="${wizardData.telegram_chat_id}" placeholder="Your numeric chat ID">
          </div>
          <div class="setup-field">
            <label class="setup-label">Minimum Alert Level</label>
            <select class="setup-input" id="s-tg-level" style="padding:8px 10px">
              <option value="warning" selected>Warning & Critical</option>
              <option value="critical">Critical Only</option>
              <option value="info">All (Info + Warning + Critical)</option>
            </select>
          </div>
          <div class="setup-btn-row">
            <button class="setup-btn test-btn" id="s-tg-test">SEND TEST MESSAGE</button>
          </div>
          <div class="setup-tg-help">
            <p class="setup-hint-title">How to set up Telegram alerts:</p>
            <ol class="setup-hint-list">
              <li>Open Telegram and search for <strong>@BotFather</strong></li>
              <li>Send <code>/newbot</code> and follow the prompts</li>
              <li>Copy the bot token and paste it above</li>
              <li>Send a message to your new bot, then visit:<br><code>https://api.telegram.org/bot&lt;TOKEN&gt;/getUpdates</code></li>
              <li>Find your <code>chat.id</code> in the response and paste it above</li>
            </ol>
          </div>
          <div class="setup-btn-row">
            <button class="setup-btn secondary" id="s-back">BACK</button>
            <button class="setup-btn" id="s-save">SAVE & START</button>
          </div>
        </div>
      `;

      // Telegram test button
      document.getElementById("s-tg-test").addEventListener("click", async () => {
        const btn = document.getElementById("s-tg-test");
        const token = document.getElementById("s-tg-token").value.trim();
        const chatId = document.getElementById("s-tg-chat").value.trim();
        if (!token || !chatId) {
          btn.textContent = "FILL IN TOKEN & CHAT ID FIRST";
          btn.className = "setup-btn test-btn fail";
          setTimeout(() => { btn.textContent = "SEND TEST MESSAGE"; btn.className = "setup-btn test-btn"; }, 3000);
          return;
        }
        btn.textContent = "SENDING...";
        btn.className = "setup-btn test-btn";
        try {
          const resp = await fetch(`${SERVER}/setup/test-telegram`, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ bot_token: token, chat_id: chatId }),
          });
          const data = await resp.json();
          if (data.success) {
            btn.textContent = "SENT! CHECK TELEGRAM";
            btn.className = "setup-btn test-btn success";
          } else {
            btn.textContent = data.error || "FAILED";
            btn.className = "setup-btn test-btn fail";
          }
        } catch {
          btn.textContent = "SERVER ERROR";
          btn.className = "setup-btn test-btn fail";
        }
        setTimeout(() => { btn.textContent = "SEND TEST MESSAGE"; btn.className = "setup-btn test-btn"; }, 4000);
      });

      document.getElementById("s-back").addEventListener("click", () => { currentStep = 3; renderStep(); });
      document.getElementById("s-save").addEventListener("click", async () => {
        wizardData.telegram_bot_token = document.getElementById("s-tg-token").value.trim();
        wizardData.telegram_chat_id = document.getElementById("s-tg-chat").value.trim();
        wizardData.telegram_min_level = document.getElementById("s-tg-level").value;

        const saveBtn = document.getElementById("s-save");
        saveBtn.textContent = "SAVING...";

        // Build sentinel config
        const sentinelCfg = {
          enabled: true,
          model: { path: "models/yolov8n.pt", confidence: 0.50, max_detect_fps: 5 },
          motion: { global_threshold: 0.002, per_camera: {} },
          zones: {},
          alerts: {
            cooldown_seconds: { info: 30, warning: 15, critical: 5 },
            macos_notifications: true,
            websocket: true,
            telegram: {
              enabled: !!(wizardData.telegram_bot_token && wizardData.telegram_chat_id),
              bot_token: wizardData.telegram_bot_token,
              chat_id: wizardData.telegram_chat_id,
              min_level: wizardData.telegram_min_level || "warning",
            },
          },
          recording: { ring_buffer_seconds: 5, save_snapshots: true, save_clips: true, data_dir: "data" },
          behavior: { linger_threshold_seconds: { person: 15, car: 30, motorcycle: 20, dog: 10, cat: 10, default: 20 } },
          daynight: { latitude: 40.7, longitude: -74.0, night_start_hour: 20, night_end_hour: 6, brightness_threshold: 0.3 },
        };

        try {
          // Build save payload based on camera type
          const savePayload = {
            camera_type: wizardData.camera_type,
            title: wizardData.title,
            sentinel: sentinelCfg,
            fal_api_key: wizardData.fal_api_key,
            telegram_bot_token: wizardData.telegram_bot_token,
            telegram_chat_id: wizardData.telegram_chat_id,
            always_on_channel: 1,
          };

          if (wizardData.camera_type === "rtsp") {
            savePayload.cameras = wizardData.cameras;
          } else {
            savePayload.dvr = {
              ip: wizardData.dvr_ip,
              port: wizardData.dvr_port,
              user: wizardData.dvr_user,
              pass: wizardData.dvr_pass,
              channels: wizardData.channels,
              channel_names: wizardData.channel_names,
              title: wizardData.title,
              always_on_channel: 1,
            };
          }

          const resp = await fetch(`${SERVER}/setup/save`, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(savePayload),
          });
          const data = await resp.json();

          if (data.status === "ok") {
            wizard.innerHTML = `
              <div class="setup-complete-icon">&#9670;</div>
              <h2>Setup Complete</h2>
              <p class="setup-complete-msg">
                Configuration saved successfully.<br>
                Restart the app to connect to your cameras.
              </p>
              <div class="setup-form">
                <div class="setup-btn-row" style="justify-content:center">
                  <button class="setup-btn" id="s-restart" style="max-width:160px">RESTART APP</button>
                </div>
              </div>
            `;
            document.getElementById("s-restart").addEventListener("click", () => {
              window.location.reload();
            });
          } else {
            saveBtn.textContent = data.error || "SAVE FAILED";
            setTimeout(() => { saveBtn.textContent = "SAVE & START"; }, 3000);
          }
        } catch(err) {
          saveBtn.textContent = "CONNECTION ERROR";
          setTimeout(() => { saveBtn.textContent = "SAVE & START"; }, 3000);
        }
      });
    }
  }

  renderStep();
}

// ===================================================================
//  POWER TOGGLE -- ON/OFF streams without closing app
// ===================================================================
function initPowerToggle() {
  const btn = document.getElementById("power-toggle");
  const label = document.getElementById("power-label");
  if (!btn) return;

  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    streamsActive = !streamsActive;

    if (streamsActive) {
      btn.classList.remove("off");
      btn.classList.add("on");
      label.textContent = "ON";
      enableStreams();
    } else {
      btn.classList.remove("on");
      btn.classList.add("off");
      label.textContent = "OFF";
      disableStreams();
    }
  });
}

function disableStreams() {
  ipcRenderer.send("streams-deactivate");

  // Stop all camera images from loading
  for (let ch = 1; ch <= (config?.channels || 4); ch++) {
    const img = document.getElementById(`cam-img-${ch}`);
    if (img) {
      img.dataset.prevSrc = img.src;
      img.src = "";
      img.style.display = "none";
    }
    const placeholder = document.getElementById(`placeholder-${ch}`);
    if (placeholder) placeholder.style.display = "flex";
    const badge = document.getElementById(`cam-badge-${ch}`);
    if (badge) badge.classList.remove("visible");
  }

  // Update header
  const statusDot = document.getElementById("status-dot");
  const statusText = document.getElementById("status");
  if (statusDot) statusDot.style.background = "rgba(255,255,255,0.15)";
  if (statusText) statusText.textContent = "OFF";
}

function enableStreams() {
  ipcRenderer.send("streams-activate");

  // Wait a moment for server to reactivate streams before requesting
  setTimeout(() => {
    for (let ch = 1; ch <= (config?.channels || 4); ch++) {
      const img = document.getElementById(`cam-img-${ch}`);
      const placeholder = document.getElementById(`placeholder-${ch}`);
      const badge = document.getElementById(`cam-badge-${ch}`);
      if (!img) continue;

      const handleLoad = () => {
        if (placeholder) placeholder.style.display = "none";
        img.style.display = "block";
        if (badge) badge.classList.add("visible");
        updateHeaderStatus();
        img.removeEventListener("load", handleLoad);
      };
      img.addEventListener("load", handleLoad);

      const handleError = () => {
        img.removeEventListener("load", handleLoad);
        setTimeout(() => {
          const retryLoad = () => {
            if (placeholder) placeholder.style.display = "none";
            img.style.display = "block";
            if (badge) badge.classList.add("visible");
            updateHeaderStatus();
            img.removeEventListener("load", retryLoad);
          };
          img.addEventListener("load", retryLoad);
          img.src = `${SERVER}/stream/${ch}?t=${Date.now()}`;
        }, 1000);
        img.removeEventListener("error", handleError);
      };
      img.addEventListener("error", handleError);

      img.src = `${SERVER}/stream/${ch}?t=${Date.now()}`;
    }
  }, 500);

  const statusDot = document.getElementById("status-dot");
  if (statusDot) statusDot.style.background = "var(--green)";
  checkHealth();
}

function updateClock() {
  const now = new Date();
  const d = now.toLocaleDateString("en-US", { day: "2-digit", month: "2-digit", year: "numeric" });
  const t = now.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  const el = document.getElementById("clock");
  if (el) el.textContent = `${d}  ${t}`;
}

function buildGrid() {
  const grid = document.getElementById("grid");
  grid.innerHTML = "";

  for (let ch = 1; ch <= config.channels; ch++) {
    const name = config.channel_names[ch - 1] || `CH${ch}`;

    const card = document.createElement("div");
    card.className = "cam-card";
    card.dataset.channel = ch;

    const backBtn = document.createElement("button");
    backBtn.className = "back-btn";
    backBtn.textContent = "\u2190 Grid";
    backBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      collapseToGrid();
    });
    card.appendChild(backBtn);

    const placeholder = document.createElement("div");
    placeholder.className = "cam-placeholder";
    placeholder.id = `placeholder-${ch}`;
    placeholder.innerHTML = '<div class="dot-loader"><span></span><span></span><span></span></div>';
    card.appendChild(placeholder);

    const img = document.createElement("img");
    img.className = "cam-image";
    img.id = `cam-img-${ch}`;
    img.style.display = "none";
    img.src = `${SERVER}/stream/${ch}`;
    img.alt = name;
    img.draggable = false;

    img.addEventListener("load", function onFirstLoad() {
      placeholder.style.display = "none";
      img.style.display = "block";
      const badge = document.getElementById(`cam-badge-${ch}`);
      if (badge) badge.classList.add("visible");
      updateHeaderStatus();
      img.removeEventListener("load", onFirstLoad);
    });

    img.addEventListener("error", () => {
      setTimeout(() => { img.src = `${SERVER}/stream/${ch}?t=${Date.now()}`; }, 2000);
    });

    card.appendChild(img);

    // Zoom canvas for selection drawing
    const zoomCanvas = document.createElement("canvas");
    zoomCanvas.className = "zoom-canvas";
    card.appendChild(zoomCanvas);

    // Zone editor canvas (separate layer)
    const zoneCanvas = document.createElement("canvas");
    zoneCanvas.className = "zone-canvas";
    zoneCanvas.id = `zone-canvas-${ch}`;
    card.appendChild(zoneCanvas);

    const overlay = document.createElement("div");
    overlay.className = "cam-overlay";
    card.appendChild(overlay);

    const label = document.createElement("div");
    label.className = "cam-label";
    label.innerHTML = `
      <span class="cam-name">${name}</span>
      <span class="cam-live-badge" id="cam-badge-${ch}">
        <span class="cam-live-dot"></span>
        <span class="cam-live-text">LIVE</span>
      </span>
    `;
    card.appendChild(label);

    // Left click to expand/collapse (disabled during zone editing)
    card.addEventListener("click", (e) => {
      if (e.button !== 0) return;
      if (zoneEditorActive) return;
      if (card.dataset.zoomBusy === "1") return;
      if (expandedChannel === ch) collapseToGrid();
      else expandCamera(ch);
    });

    setupZoom(card, img, zoomCanvas, ch);
    setupZoneEditorForCard(card, zoneCanvas, ch);
    grid.appendChild(card);
  }
}

// =====================================================================
//  ZOOM SYSTEM -- 3 stages:
//  1. Right-drag -> select area (live video keeps playing)
//  2. Release -> live zoom into selected area (video still plays)
//  3. Click "ENHANCE" button -> freeze + AI upscale
// =====================================================================

function setupZoom(card, img, canvas, ch) {
  let dragging = false;
  let startX = 0, startY = 0;
  let selRect = null;
  let zoomActive = false;
  let currentSel = null;

  card.addEventListener("contextmenu", (e) => e.preventDefault());

  card.addEventListener("mousedown", (e) => {
    if (e.button !== 2) return;
    if (zoneEditorActive) return;
    e.preventDefault();
    e.stopPropagation();

    if (zoomActive) {
      resetLiveZoom(card, img, canvas);
      zoomActive = false;
      currentSel = null;
      return;
    }

    const rect = card.getBoundingClientRect();
    startX = e.clientX - rect.left;
    startY = e.clientY - rect.top;
    dragging = true;
    selRect = null;

    canvas.width = rect.width;
    canvas.height = rect.height;
    canvas.style.display = "block";
    card.dataset.zoomBusy = "1";
  });

  card.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const rect = card.getBoundingClientRect();
    const curX = Math.max(0, Math.min(e.clientX - rect.left, rect.width));
    const curY = Math.max(0, Math.min(e.clientY - rect.top, rect.height));

    const x = Math.min(startX, curX);
    const y = Math.min(startY, curY);
    const w = Math.abs(curX - startX);
    const h = Math.abs(curY - startY);

    if (w > 8 || h > 8) {
      selRect = { x, y, w, h };
      drawSelection(canvas, selRect, rect.width, rect.height);
    }
  });

  card.addEventListener("mouseup", (e) => {
    if (e.button !== 2 || !dragging) return;
    dragging = false;

    if (selRect && selRect.w > 25 && selRect.h > 25) {
      currentSel = { ...selRect };
      animateLiveZoom(card, img, canvas, selRect, ch);
      zoomActive = true;
    } else {
      canvas.style.display = "none";
    }

    setTimeout(() => { card.dataset.zoomBusy = "0"; }, 150);
  });

  card.addEventListener("mouseleave", () => {
    if (dragging) {
      dragging = false;
      canvas.style.display = "none";
      setTimeout(() => { card.dataset.zoomBusy = "0"; }, 150);
    }
  });
}

function drawSelection(canvas, sel, cw, ch) {
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, cw, ch);

  ctx.fillStyle = "rgba(0, 0, 0, 0.55)";
  ctx.fillRect(0, 0, cw, ch);
  ctx.clearRect(sel.x, sel.y, sel.w, sel.h);

  ctx.strokeStyle = "rgba(255, 255, 255, 0.7)";
  ctx.lineWidth = 1;
  ctx.strokeRect(sel.x + 0.5, sel.y + 0.5, sel.w - 1, sel.h - 1);

  const L = Math.min(14, sel.w / 3, sel.h / 3);
  ctx.strokeStyle = "#fff";
  ctx.lineWidth = 2;

  ctx.beginPath();
  ctx.moveTo(sel.x, sel.y + L); ctx.lineTo(sel.x, sel.y); ctx.lineTo(sel.x + L, sel.y);
  ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(sel.x + sel.w - L, sel.y); ctx.lineTo(sel.x + sel.w, sel.y); ctx.lineTo(sel.x + sel.w, sel.y + L);
  ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(sel.x, sel.y + sel.h - L); ctx.lineTo(sel.x, sel.y + sel.h); ctx.lineTo(sel.x + L, sel.y + sel.h);
  ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(sel.x + sel.w - L, sel.y + sel.h); ctx.lineTo(sel.x + sel.w, sel.y + sel.h); ctx.lineTo(sel.x + sel.w, sel.y + sel.h - L);
  ctx.stroke();

  const cx = sel.x + sel.w / 2, cy = sel.y + sel.h / 2;
  ctx.strokeStyle = "rgba(255,255,255,0.25)";
  ctx.lineWidth = 0.5;
  ctx.beginPath();
  ctx.moveTo(cx - 10, cy); ctx.lineTo(cx + 10, cy);
  ctx.moveTo(cx, cy - 10); ctx.lineTo(cx, cy + 10);
  ctx.stroke();

  const zoom = Math.min(cw / sel.w, ch / sel.h, 6).toFixed(1);
  ctx.font = "10px 'SF Mono', monospace";
  ctx.fillStyle = "rgba(255,255,255,0.6)";
  ctx.textAlign = "left";
  ctx.fillText(`${zoom}x`, sel.x + sel.w + 6, sel.y + 13);
}

function animateLiveZoom(card, img, canvas, sel, ch) {
  const cardRect = card.getBoundingClientRect();
  const scale = Math.min(cardRect.width / sel.w, cardRect.height / sel.h, 6);

  canvas.style.transition = "opacity 0.15s ease";
  canvas.style.opacity = "0";
  setTimeout(() => {
    canvas.style.display = "none";
    canvas.style.opacity = "1";
    canvas.style.transition = "";
  }, 150);

  img.style.transition = "transform 0.4s cubic-bezier(0.22, 1, 0.36, 1)";
  img.style.transformOrigin = `${sel.x + sel.w/2}px ${sel.y + sel.h/2}px`;
  img.style.transform = `scale(${scale})`;

  let hud = card.querySelector(".zoom-hud");
  if (!hud) {
    hud = document.createElement("div");
    hud.className = "zoom-hud";
    card.appendChild(hud);
  }
  hud.innerHTML = `
    <span class="zoom-level">${scale.toFixed(1)}x LIVE</span>
    <button class="enhance-btn" id="enhance-btn-${ch}">PHOTA AI \u25b8</button>
    <button class="reset-btn" id="reset-btn-${ch}">ESC</button>
  `;
  hud.style.display = "flex";
  hud.style.opacity = "0";
  requestAnimationFrame(() => {
    hud.style.transition = "opacity 0.3s ease 0.2s";
    hud.style.opacity = "1";
  });

  document.getElementById(`enhance-btn-${ch}`).addEventListener("click", (e) => {
    e.stopPropagation();
    triggerEnhance(card, img, canvas, sel, ch, scale);
  });

  document.getElementById(`reset-btn-${ch}`).addEventListener("click", (e) => {
    e.stopPropagation();
    resetLiveZoom(card, img, canvas);
  });
}

function triggerEnhance(card, img, canvas, sel, ch, scale) {
  const cardRect = card.getBoundingClientRect();
  const hud = card.querySelector(".zoom-hud");
  const startTime = Date.now();
  const TOTAL_DURATION = 66;

  _removePixelOverlay(card);

  const dpr = window.devicePixelRatio || 2;
  const overlay = document.createElement("canvas");
  overlay.className = "pixel-enhance-overlay";
  overlay.width = Math.floor(cardRect.width * dpr);
  overlay.height = Math.floor(cardRect.height * dpr);
  overlay.style.width = cardRect.width + "px";
  overlay.style.height = cardRect.height + "px";
  card.appendChild(overlay);

  const ctx = overlay.getContext("2d", { alpha: true });
  const W = overlay.width, H = overlay.height;

  const src = document.createElement("canvas");
  src.width = img.naturalWidth || img.width;
  src.height = img.naturalHeight || img.height;
  src.getContext("2d").drawImage(img, 0, 0);

  const pxBuf = document.createElement("canvas");
  pxBuf.width = W; pxBuf.height = H;
  const pxCtx = pxBuf.getContext("2d");

  if (hud) {
    hud.innerHTML = `
      <div class="mil-hud">
        <div class="mil-hud-row">
          <span class="mil-phase">ACQUIRING TARGET</span>
          <span class="mil-timer">00.0</span>
        </div>
        <div class="mil-bar"><div class="mil-bar-fill"></div></div>
        <div class="mil-hud-row mil-sub">
          <span class="mil-conf">CONFIDENCE 0%</span>
          <span class="mil-model">PHOTA-SR</span>
        </div>
      </div>
    `;
  }

  const ticker = document.createElement("div");
  ticker.className = "mil-ticker";
  ticker.innerHTML = `<span class="mil-tick-text"></span>`;
  card.appendChild(ticker);

  const noiseCanvas = document.createElement("canvas");
  noiseCanvas.width = 128; noiseCanvas.height = 128;
  const noiseCtx = noiseCanvas.getContext("2d");
  const noiseData = noiseCtx.createImageData(128, 128);
  for (let i = 0; i < noiseData.data.length; i += 4) {
    const v = Math.random() * 255;
    noiseData.data[i] = noiseData.data[i+1] = noiseData.data[i+2] = v;
    noiseData.data[i+3] = 255;
  }
  noiseCtx.putImageData(noiseData, 0, 0);

  const TICK_MSGS = [
    { t: 0,  msg: "SYS  REGION LOCK \u2014 CH" + ch + " QUADRANT " + (sel.x > cardRect.width/2 ? "EAST" : "WEST") + (sel.y > cardRect.height/2 ? "-SOUTH" : "-NORTH") },
    { t: 3,  msg: "NET  ENCODING PAYLOAD \u2014 " + Math.floor(sel.w * sel.h / 80) + "KB BUFFER \u2014 JPEG Q95" },
    { t: 6,  msg: "NET  UPLINK TO PHOTA-SR \u2014 FRA-1 EDGE NODE \u2014 LATENCY 23ms" },
    { t: 10, msg: "GPU  VRAM ALLOCATION \u2014 A100-80GB INSTANCE ONLINE" },
    { t: 14, msg: "GPU  LOADING MODEL WEIGHTS \u2014 6.2B PARAMETERS \u2014 FP16 PRECISION" },
    { t: 18, msg: "AI   PASS 1/4 STRUCTURE RECOVERY \u2014 LOW FREQUENCY RECONSTRUCTION" },
    { t: 24, msg: "AI   PASS 2/4 TEXTURE SYNTHESIS \u2014 PERCEPTUAL LOSS OPTIMIZATION" },
    { t: 32, msg: "AI   PASS 3/4 DETAIL HALLUCINATION \u2014 DIFFUSION STEP 847/1000" },
    { t: 40, msg: "AI   PASS 4/4 EDGE REFINEMENT \u2014 GUIDED FILTER \u2014 SHARPNESS +4.2dB" },
    { t: 48, msg: "POST COLOR CORRECTION \u2014 HISTOGRAM EQ \u2014 GAMMA 1.02 \u2014 WB AUTO" },
    { t: 54, msg: "NET  DOWNLOADING 4K FRAME \u2014 " + Math.floor(2400 + Math.random()*800) + "KB \u2014 ETA 3s" },
    { t: 60, msg: "SYS  RENDER COMPLETE \u2014 CONFIDENCE 99% \u2014 STANDBY FOR REVEAL" },
  ];
  let lastTickIdx = -1;

  overlay._running = true;
  overlay._animFrame = null;
  let frame = 0;
  let currentPxSize = 56;
  let reticleAngle = 0;
  let procRegions = [];
  let edgeFlashT = -999;

  const smoothstep = (a, b, x) => { const t = Math.max(0, Math.min(1, (x-a)/(b-a))); return t*t*(3-2*t); };

  function animate() {
    if (!overlay._running) return;

    const t = (Date.now() - startTime) / 1000;
    const progress = Math.min(1, t / TOTAL_DURATION);

    let targetPx;
    if (t < 8)        targetPx = 56 - smoothstep(0, 8, t) * 28;
    else if (t < 20)  targetPx = 28 - smoothstep(8, 20, t) * 14;
    else if (t < 40)  targetPx = 14 - smoothstep(20, 40, t) * 9;
    else if (t < 55)  targetPx = 5 - smoothstep(40, 55, t) * 3;
    else              targetPx = 2 - smoothstep(55, 66, t) * 1 + Math.sin(t * 2) * 0.3;

    currentPxSize += (targetPx - currentPxSize) * 0.06;
    currentPxSize = Math.max(1, currentPxSize);

    const cellW = Math.max(2, Math.floor(W / currentPxSize));
    const cellH = Math.max(2, Math.floor(H / currentPxSize));

    pxCtx.imageSmoothingEnabled = true;
    pxCtx.imageSmoothingQuality = currentPxSize > 10 ? "low" : "medium";
    pxCtx.drawImage(src, 0, 0, cellW, cellH);
    ctx.clearRect(0, 0, W, H);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(pxBuf, 0, 0, cellW, cellH, 0, 0, W, H);

    const grainAlpha = Math.max(0, 0.07 * (1 - smoothstep(0, 45, t)));
    if (grainAlpha > 0.003) {
      ctx.globalAlpha = grainAlpha;
      ctx.globalCompositeOperation = "overlay";
      const ox = (frame * 37) % 64, oy = (frame * 53) % 64;
      for (let nx = -ox; nx < W; nx += 128)
        for (let ny = -oy; ny < H; ny += 128)
          ctx.drawImage(noiseCanvas, nx, ny);
      ctx.globalAlpha = 1;
      ctx.globalCompositeOperation = "source-over";
    }

    const scanA = 0.025 * (1 - smoothstep(0, 30, t));
    if (scanA > 0.002) {
      ctx.fillStyle = `rgba(0,0,0,${scanA})`;
      for (let sy = 0; sy < H; sy += 4) ctx.fillRect(0, sy, W, 1);
    }

    ctx.globalCompositeOperation = "overlay";
    ctx.fillStyle = `rgba(25,70,35,${0.035 * (1 - smoothstep(10, 60, t))})`;
    ctx.fillRect(0, 0, W, H);
    ctx.globalCompositeOperation = "source-over";

    const sweepSpeed = 0.3 + progress * 0.5;
    const sweepY = ((t * sweepSpeed * H) % (H * 1.3)) - H * 0.15;
    const sweepA = 0.06 + Math.sin(t * 0.7) * 0.02;
    const sweepW = 30 + progress * 20;
    const sg = ctx.createLinearGradient(0, sweepY - sweepW, 0, sweepY + sweepW);
    sg.addColorStop(0, "rgba(62,207,113,0)");
    sg.addColorStop(0.35, `rgba(62,207,113,${sweepA * 0.2})`);
    sg.addColorStop(0.5, `rgba(62,207,113,${sweepA})`);
    sg.addColorStop(0.65, `rgba(62,207,113,${sweepA * 0.2})`);
    sg.addColorStop(1, "rgba(62,207,113,0)");
    ctx.fillStyle = sg;
    ctx.fillRect(0, sweepY - sweepW, W, sweepW * 2);

    if (sweepY > 0 && sweepY < H) {
      const trailGrad = ctx.createLinearGradient(0, sweepY, 0, sweepY + H * 0.15);
      trailGrad.addColorStop(0, `rgba(62,207,113,${sweepA * 0.15})`);
      trailGrad.addColorStop(1, "rgba(62,207,113,0)");
      ctx.fillStyle = trailGrad;
      ctx.fillRect(0, sweepY, W, H * 0.15);
    }

    if (t > 10 && t < 50 && frame % 70 === 0 && procRegions.length < 4) {
      procRegions.push({
        x: 0.05 + Math.random() * 0.7, y: 0.05 + Math.random() * 0.7,
        w: 0.06 + Math.random() * 0.2, h: 0.06 + Math.random() * 0.18,
        birth: t, life: 3 + Math.random() * 4,
      });
    }
    procRegions = procRegions.filter(r => t - r.birth < r.life);
    for (const r of procRegions) {
      const age = (t - r.birth) / r.life;
      const alpha = smoothstep(0, 0.1, age) * (1 - smoothstep(0.75, 1, age));
      const rx = r.x * W, ry = r.y * H, rw = r.w * W, rh = r.h * H;
      ctx.strokeStyle = `rgba(62,207,113,${0.2 * alpha})`;
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 4]);
      ctx.strokeRect(rx, ry, rw, rh);
      ctx.setLineDash([]);
      if (currentPxSize > 3) {
        ctx.save();
        ctx.globalAlpha = 0.35 * alpha;
        ctx.beginPath();
        ctx.rect(rx + 1, ry + 1, rw - 2, rh - 2);
        ctx.clip();
        ctx.imageSmoothingEnabled = true;
        ctx.drawImage(src, r.x * src.width, r.y * src.height, r.w * src.width, r.h * src.height, rx, ry, rw, rh);
        ctx.restore();
      }
      ctx.font = `${8 * dpr}px "SF Mono", monospace`;
      ctx.fillStyle = `rgba(62,207,113,${0.35 * alpha})`;
      ctx.fillText(`SECTOR ${String.fromCharCode(65 + (r.x * 10) % 26)}${Math.floor(r.y * 10)}`, rx + 3, ry - 3);
    }

    if ((Math.abs(t - 12) < 0.05 || Math.abs(t - 35) < 0.05) && t - edgeFlashT > 5) {
      edgeFlashT = t;
    }
    if (t - edgeFlashT < 1.5) {
      const flashAge = (t - edgeFlashT) / 1.5;
      const flashAlpha = 0.08 * (1 - smoothstep(0, 1, flashAge));
      ctx.globalCompositeOperation = "screen";
      ctx.filter = `contrast(3) brightness(2)`;
      ctx.globalAlpha = flashAlpha;
      ctx.drawImage(pxBuf, 0, 0, cellW, cellH, 0, 0, W, H);
      ctx.filter = "none";
      ctx.globalAlpha = 1;
      ctx.globalCompositeOperation = "source-over";
    }

    const reticleVis = 1 - smoothstep(15, 25, t);
    if (reticleVis > 0.01) {
      const cx = W / 2, cy = H / 2;
      reticleAngle += 0.01;
      const rAlpha = (0.12 + Math.sin(t * 1.5) * 0.04) * reticleVis;
      const rR = Math.min(W, H) * 0.13;
      ctx.save();
      ctx.translate(cx, cy);
      ctx.strokeStyle = `rgba(62,207,113,${rAlpha * 0.4})`;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.arc(0, 0, rR, reticleAngle, reticleAngle + 0.5); ctx.stroke();
      ctx.beginPath(); ctx.arc(0, 0, rR, reticleAngle + Math.PI * 0.7, reticleAngle + Math.PI * 0.7 + 0.5); ctx.stroke();
      ctx.beginPath(); ctx.arc(0, 0, rR, reticleAngle + Math.PI * 1.4, reticleAngle + Math.PI * 1.4 + 0.5); ctx.stroke();
      ctx.strokeStyle = `rgba(62,207,113,${rAlpha * 0.6})`;
      ctx.beginPath(); ctx.arc(0, 0, rR * 0.45, -reticleAngle * 0.6, -reticleAngle * 0.6 + 0.7); ctx.stroke();
      const cL = rR * 0.3, cG = rR * 0.1;
      ctx.strokeStyle = `rgba(62,207,113,${rAlpha})`;
      ctx.beginPath();
      ctx.moveTo(-cL, 0); ctx.lineTo(-cG, 0);
      ctx.moveTo(cG, 0); ctx.lineTo(cL, 0);
      ctx.moveTo(0, -cL); ctx.lineTo(0, -cG);
      ctx.moveTo(0, cG); ctx.lineTo(0, cL);
      ctx.stroke();
      ctx.fillStyle = `rgba(62,207,113,${rAlpha * 1.5})`;
      ctx.beginPath(); ctx.arc(0, 0, 1.5, 0, Math.PI * 2); ctx.fill();
      ctx.restore();
    }

    const bLen = 18 * dpr, bOff = 5 * dpr;
    const bA = (0.25 + Math.sin(t * 0.6) * 0.08);
    ctx.strokeStyle = `rgba(62,207,113,${bA})`;
    ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(bOff, bOff + bLen); ctx.lineTo(bOff, bOff); ctx.lineTo(bOff + bLen, bOff); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(W-bOff-bLen, bOff); ctx.lineTo(W-bOff, bOff); ctx.lineTo(W-bOff, bOff+bLen); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(bOff, H-bOff-bLen); ctx.lineTo(bOff, H-bOff); ctx.lineTo(bOff+bLen, H-bOff); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(W-bOff-bLen, H-bOff); ctx.lineTo(W-bOff, H-bOff); ctx.lineTo(W-bOff, H-bOff-bLen); ctx.stroke();

    const rdA = 0.28 + Math.sin(t * 0.8) * 0.04;
    ctx.font = `${7 * dpr}px "SF Mono", monospace`;
    ctx.fillStyle = `rgba(62,207,113,${rdA})`;
    ctx.textAlign = "left";
    ctx.fillText(`${Math.floor(sel.x)},${Math.floor(sel.y)}`, bOff + bLen + 3, bOff + 9 * dpr);
    ctx.textAlign = "right";
    const resLabel = currentPxSize > 12 ? "ACQUIRING" : currentPxSize > 6 ? `${Math.floor(W/dpr)}px` : `${Math.floor(W/dpr * 4)}px SR`;
    ctx.fillText(resLabel, W - bOff - bLen - 3, bOff + 9 * dpr);
    const conf = Math.min(99, Math.floor(progress * 100));
    ctx.textAlign = "left";
    ctx.fillText(`CONF ${conf}%`, bOff + bLen + 3, H - bOff - 3);
    ctx.textAlign = "right";
    ctx.fillText(`BLK ${Math.max(1, Math.floor(currentPxSize))}px`, W - bOff - bLen - 3, H - bOff - 3);
    ctx.textAlign = "left";

    if (Math.random() < Math.max(0.001, 0.012 * (1 - progress))) {
      const gy = Math.floor(Math.random() * H);
      const gh = 1 + Math.floor(Math.random() * 3);
      const shift = Math.floor((Math.random() - 0.5) * (4 + (1-progress) * 4));
      try {
        const slice = ctx.getImageData(0, Math.max(0,gy), W, Math.min(gh, H-gy));
        ctx.putImageData(slice, shift, gy);
      } catch(e) {}
    }

    const vg = ctx.createRadialGradient(W/2, H/2, W*0.22, W/2, H/2, W*0.82);
    vg.addColorStop(0, "rgba(0,0,0,0)");
    vg.addColorStop(1, `rgba(0,0,0,${0.3 - progress * 0.1})`);
    ctx.fillStyle = vg;
    ctx.fillRect(0, 0, W, H);

    if (hud && frame % 4 === 0) {
      const phaseEl = hud.querySelector(".mil-phase");
      const timerEl = hud.querySelector(".mil-timer");
      const barEl = hud.querySelector(".mil-bar-fill");
      const confEl = hud.querySelector(".mil-conf");
      if (phaseEl) {
        if (t < 6)       phaseEl.textContent = "ACQUIRING TARGET";
        else if (t < 14) phaseEl.textContent = "UPLOADING FRAME";
        else if (t < 24) phaseEl.textContent = "NEURAL PROCESSING";
        else if (t < 40) phaseEl.textContent = "TEXTURE SYNTHESIS";
        else if (t < 55) phaseEl.textContent = "DETAIL RECOVERY";
        else             phaseEl.textContent = "RENDERING OUTPUT";
      }
      if (timerEl) timerEl.textContent = t < 10 ? `0${t.toFixed(1)}` : t.toFixed(1);
      if (barEl) barEl.style.width = `${Math.min(98, progress * 100)}%`;
      if (confEl) confEl.textContent = `CONFIDENCE ${conf}%`;
    }

    for (let i = TICK_MSGS.length - 1; i >= 0; i--) {
      if (t >= TICK_MSGS[i].t && i > lastTickIdx) {
        lastTickIdx = i;
        const el = ticker.querySelector(".mil-tick-text");
        if (el) {
          el.style.animation = "none"; el.offsetHeight;
          el.textContent = TICK_MSGS[i].msg;
          el.style.animation = "tick-type 0.5s steps(50) forwards";
        }
        break;
      }
    }

    frame++;
    overlay._animFrame = requestAnimationFrame(animate);
  }

  animate();

  // Fetch enhanced image
  const nx = sel.x / cardRect.width;
  const ny = sel.y / cardRect.height;
  const nw = sel.w / cardRect.width;
  const nh = sel.h / cardRect.height;

  fetch(`${SERVER}/enhance/${ch}?x=${nx.toFixed(3)}&y=${ny.toFixed(3)}&w=${nw.toFixed(3)}&h=${nh.toFixed(3)}&mode=fal`)
    .then(r => r.ok ? r.blob() : null)
    .then(blob => {
      if (!blob) { _removePixelOverlay(card); return; }
      const url = URL.createObjectURL(blob);
      const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);

      let enhImg = card.querySelector(".enhanced-overlay");
      if (!enhImg) { enhImg = document.createElement("img"); enhImg.className = "enhanced-overlay"; card.appendChild(enhImg); }

      enhImg.onload = () => _transitionPixelToClear(card, overlay, enhImg, img, hud, canvas, ticker, elapsed, W, H, dpr, src);
      enhImg.src = url;
    })
    .catch(() => {
      _removePixelOverlay(card);
      if (hud) hud.innerHTML = `<span class="zoom-level">${scale.toFixed(1)}x LIVE</span>`;
    });
}


function _removePixelOverlay(card) {
  const el = card.querySelector(".pixel-enhance-overlay");
  if (el) { el._running = false; if (el._animFrame) cancelAnimationFrame(el._animFrame); el.remove(); }
  card.querySelector(".mil-ticker")?.remove();
}

function _transitionPixelToClear(card, overlay, enhImg, origImg, hud, canvas, ticker, elapsed, W, H, dpr, src) {
  overlay._running = false;
  if (overlay._animFrame) cancelAnimationFrame(overlay._animFrame);

  const ctx = overlay.getContext("2d");

  enhImg.style.display = "block";
  enhImg.style.opacity = "1";
  enhImg.style.left = "0"; enhImg.style.top = "0";
  enhImg.style.width = "100%"; enhImg.style.height = "100%";
  enhImg.style.zIndex = "3";

  origImg.style.opacity = "0";

  const DISSOLVE_MS = 2000;
  const dissolveStart = Date.now();
  let lastPxSize = 3;

  function dissolveFrame() {
    const elapsed = Date.now() - dissolveStart;
    const p = Math.min(1, elapsed / DISSOLVE_MS);
    const e = 1 - Math.pow(1 - p, 3);

    if (p < 0.4) {
      const resolveP = p / 0.4;
      const pxSize = Math.max(1, lastPxSize * (1 - resolveP));
      const cellW = Math.max(4, Math.floor(W / pxSize));
      const cellH = Math.max(4, Math.floor(H / pxSize));

      const pxBuf = document.createElement("canvas");
      pxBuf.width = W; pxBuf.height = H;
      const pxCtx = pxBuf.getContext("2d");
      pxCtx.imageSmoothingEnabled = true;
      pxCtx.imageSmoothingQuality = "high";
      pxCtx.drawImage(src, 0, 0, cellW, cellH);

      ctx.clearRect(0, 0, W, H);
      ctx.imageSmoothingEnabled = pxSize < 1.5;
      ctx.drawImage(pxBuf, 0, 0, cellW, cellH, 0, 0, W, H);

      const bLen = 18 * dpr, bOff = 5 * dpr;
      const bA = 0.25 * (1 - resolveP);
      if (bA > 0.02) {
        ctx.strokeStyle = `rgba(62,207,113,${bA})`;
        ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.moveTo(bOff, bOff+bLen); ctx.lineTo(bOff, bOff); ctx.lineTo(bOff+bLen, bOff); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(W-bOff-bLen, bOff); ctx.lineTo(W-bOff, bOff); ctx.lineTo(W-bOff, bOff+bLen); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(bOff, H-bOff-bLen); ctx.lineTo(bOff, H-bOff); ctx.lineTo(bOff+bLen, H-bOff); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(W-bOff-bLen, H-bOff); ctx.lineTo(W-bOff, H-bOff); ctx.lineTo(W-bOff, H-bOff-bLen); ctx.stroke();
      }
      ctx.font = `${7 * dpr}px "SF Mono", monospace`;
      ctx.fillStyle = `rgba(62,207,113,${0.25 * (1 - resolveP)})`;
      ctx.textAlign = "left";
      ctx.fillText("RESOLVING...", bOff + bLen + 3, H - bOff - 3);
      ctx.textAlign = "left";
    } else {
      const fadeP = (p - 0.4) / 0.6;
      const fadeE = 1 - Math.pow(1 - fadeP, 2);
      ctx.clearRect(0, 0, W, H);
      ctx.imageSmoothingEnabled = true;
      ctx.globalAlpha = 1 - fadeE;
      ctx.drawImage(src, 0, 0, W, H);
      if (fadeP < 0.6) {
        const ringAlpha = 0.06 * (1 - fadeP / 0.6);
        const rg = ctx.createRadialGradient(W/2, H/2, W*0.3, W/2, H/2, W*0.55);
        rg.addColorStop(0, "rgba(62,207,113,0)");
        rg.addColorStop(0.7, `rgba(62,207,113,${ringAlpha})`);
        rg.addColorStop(1, "rgba(62,207,113,0)");
        ctx.globalAlpha = 1;
        ctx.fillStyle = rg;
        ctx.fillRect(0, 0, W, H);
      }
      ctx.globalAlpha = 1;
    }

    if (p < 1) {
      requestAnimationFrame(dissolveFrame);
    } else {
      overlay.remove();
      if (ticker) {
        ticker.style.transition = "opacity 0.6s ease";
        ticker.style.opacity = "0";
        setTimeout(() => ticker.remove(), 600);
      }
      const flash = document.createElement("div");
      flash.className = "enhance-flash";
      card.appendChild(flash);
      setTimeout(() => flash.remove(), 1200);
      if (hud) {
        hud.innerHTML = `
          <span class="zoom-level enhance-complete">
            <span class="enhance-icon-done">\u25C6</span>
            ENHANCED \u00b7 ${elapsed}s
          </span>
          <button class="reset-btn" onclick="event.stopPropagation()">ESC</button>
        `;
        hud.querySelector(".reset-btn").addEventListener("click", (ev) => {
          ev.stopPropagation();
          resetLiveZoom(card, origImg, canvas);
        });
      }
    }
  }

  const tickEl = ticker?.querySelector(".mil-tick-text");
  if (tickEl) {
    tickEl.style.animation = "none"; tickEl.offsetHeight;
    tickEl.textContent = "SYS  IMAGE LOCKED \u2014 DISSOLVING MOSAIC \u2014 REVEALING ENHANCED FRAME";
    tickEl.style.animation = "tick-type 0.5s steps(50) forwards";
  }

  const phaseEl = hud?.querySelector(".mil-phase");
  const barEl = hud?.querySelector(".mil-bar-fill");
  if (phaseEl) phaseEl.textContent = "REVEAL";
  if (barEl) { barEl.style.width = "100%"; barEl.style.transition = "width 0.5s ease"; }

  requestAnimationFrame(dissolveFrame);
}

function resetLiveZoom(card, img, canvas) {
  const sl = card.querySelector(".scanline-effect");
  if (sl) sl.style.display = "none";

  _removePixelOverlay(card);

  const enhImg = card.querySelector(".enhanced-overlay");
  if (enhImg && enhImg.style.display !== "none") {
    enhImg.style.transition = "opacity 0.25s ease";
    enhImg.style.opacity = "0";
    setTimeout(() => {
      enhImg.style.display = "none";
      enhImg.style.transition = "";
      if (enhImg.src?.startsWith("blob:")) URL.revokeObjectURL(enhImg.src);
      enhImg.src = "";
    }, 250);
  }

  img.style.opacity = "1";
  img.style.transition = "transform 0.35s cubic-bezier(0.22, 1, 0.36, 1), opacity 0.25s ease";
  img.style.transform = "none";
  setTimeout(() => {
    img.style.transition = "";
    img.style.transformOrigin = "";
  }, 350);

  if (canvas) canvas.style.display = "none";

  const hud = card.querySelector(".zoom-hud");
  if (hud) {
    hud.style.transition = "opacity 0.2s ease";
    hud.style.opacity = "0";
    setTimeout(() => { hud.style.display = "none"; }, 200);
  }
}

// =====================================================================

function expandCamera(channel) {
  expandedChannel = channel;
  const grid = document.getElementById("grid");
  grid.classList.add("expanded");
  grid.querySelectorAll(".cam-card").forEach((card) => {
    const ch = parseInt(card.dataset.channel);
    if (ch === channel) { card.classList.add("expanded"); card.classList.remove("hidden"); }
    else { card.classList.add("hidden"); card.classList.remove("expanded"); }
  });
}

function collapseToGrid() {
  expandedChannel = null;
  const grid = document.getElementById("grid");
  grid.classList.remove("expanded");
  grid.querySelectorAll(".cam-card").forEach((card) => {
    card.classList.remove("expanded", "hidden");
    const img = card.querySelector(".cam-image");
    const canvas = card.querySelector(".zoom-canvas");
    if (img && img.style.transform && img.style.transform !== "none") {
      resetLiveZoom(card, img, canvas);
    }
  });
}

function updateHeaderStatus() {
  const liveCount = document.querySelectorAll(".cam-live-badge.visible").length;
  const total = config.channels;
  const statusEl = document.getElementById("status");
  const dotEl = document.getElementById("status-dot");
  if (liveCount === total) {
    statusEl.textContent = `${total}/${total} ONLINE`;
    if (dotEl) dotEl.style.background = "var(--green)";
  } else if (liveCount > 0) {
    statusEl.textContent = `${liveCount}/${total} ONLINE`;
    if (dotEl) dotEl.style.background = "#ffaa00";
  } else {
    statusEl.textContent = "CONNECTING";
    if (dotEl) dotEl.style.background = "rgba(255,255,255,0.25)";
  }
}

async function checkHealth() {
  try {
    const resp = await fetch(`${SERVER}/health`);
    const data = await resp.json();
    for (const [ch, alive] of Object.entries(data.streams)) {
      const badge = document.getElementById(`cam-badge-${ch}`);
      if (badge && alive) badge.classList.add("visible");
    }
    updateHeaderStatus();
  } catch {}
}

// =====================================================================
//  EVENT TIMELINE
// =====================================================================

function initEventPanel() {
  const panel = document.getElementById("event-panel");
  const closeBtn = document.getElementById("event-panel-close");
  const toggleBtn = document.getElementById("btn-events");
  const clipOverlay = document.getElementById("clip-overlay");
  const clipClose = document.getElementById("clip-close");

  toggleBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    toggleEventPanel();
  });

  closeBtn.addEventListener("click", () => toggleEventPanel());

  clipClose.addEventListener("click", () => closeClipPlayer());
  clipOverlay.addEventListener("click", (e) => {
    if (e.target === clipOverlay) closeClipPlayer();
  });
}

function toggleEventPanel() {
  const panel = document.getElementById("event-panel");
  const btn = document.getElementById("btn-events");
  eventPanelOpen = !eventPanelOpen;

  if (eventPanelOpen) {
    panel.classList.add("open");
    btn.classList.add("active");
    fetchEvents();
    eventPollTimer = setInterval(fetchEvents, 10000);
  } else {
    panel.classList.remove("open");
    btn.classList.remove("active");
    if (eventPollTimer) {
      clearInterval(eventPollTimer);
      eventPollTimer = null;
    }
  }
}

async function fetchEvents() {
  try {
    const resp = await fetch(`${SERVER}/api/events?limit=50`);
    const data = await resp.json();
    cachedEvents = data.events || [];
    const countEl = document.getElementById("event-count");
    if (countEl) countEl.textContent = `${cachedEvents.length} events`;
    renderEventList(cachedEvents);
  } catch {}
}

let selectedEventIdx = -1;

function renderEventList(events) {
  const list = document.getElementById("event-list");
  const detail = document.getElementById("event-detail");

  if (!events || events.length === 0) {
    list.innerHTML = '<div class="event-empty">No events yet</div>';
    if (detail) detail.style.display = "none";
    return;
  }

  list.innerHTML = "";
  const now = Date.now() / 1000;

  events.forEach((evt, idx) => {
    const card = document.createElement("div");
    card.className = "event-card" + (idx === selectedEventIdx ? " selected" : "");
    card.dataset.idx = idx;

    const timeAgo = relativeTime(now - evt.timestamp);
    const camName = config.channel_names[evt.channel - 1] || `CH${evt.channel}`;

    card.innerHTML = `
      <img class="event-thumb" src="${SERVER}/api/events/${evt.id}/snapshot" alt="" loading="lazy"
           onerror="this.style.display='none'">
      <div class="event-info">
        <div class="event-top-row">
          <span class="event-time">${timeAgo}</span>
          <span class="event-level-dot ${evt.alert_level}"></span>
        </div>
        <div class="event-camera">${camName}</div>
        <div class="event-class">${evt.class_name} ${(evt.confidence * 100).toFixed(0)}%</div>
      </div>
    `;

    card.addEventListener("click", () => showEventDetail(idx));
    list.appendChild(card);
  });

  if (selectedEventIdx >= 0 && selectedEventIdx < events.length) {
    showEventDetail(selectedEventIdx);
  }
}

function showEventDetail(idx) {
  selectedEventIdx = idx;
  const evt = cachedEvents[idx];
  if (!evt) return;

  document.querySelectorAll(".event-card").forEach((c, i) => {
    c.classList.toggle("selected", i === idx);
  });

  const detail = document.getElementById("event-detail");
  if (!detail) return;

  const camName = config.channel_names[evt.channel - 1] || `CH${evt.channel}`;
  const ts = new Date(evt.timestamp * 1000);
  const timeStr = ts.toLocaleTimeString("en-US", { hour:"2-digit", minute:"2-digit", second:"2-digit", hour12: false });
  const dateStr = ts.toLocaleDateString("en-US", { day:"2-digit", month:"2-digit" });
  const hasClip = !!evt.clip_path;

  detail.style.display = "flex";
  detail.innerHTML = `
    <img class="detail-snapshot" src="${SERVER}/api/events/${evt.id}/snapshot" alt=""
         onerror="this.style.background='var(--surface)'">
    <div class="detail-info">
      <div class="detail-header">
        <span class="detail-level ${evt.alert_level}">${evt.alert_level.toUpperCase()}</span>
        <span class="detail-time">${dateStr} ${timeStr}</span>
      </div>
      <div class="detail-row">${camName} · ${evt.zone_name}</div>
      <div class="detail-row">${evt.class_name} <span class="detail-conf">${(evt.confidence*100).toFixed(0)}%</span></div>
      ${evt.behavior ? `<div class="detail-behavior">${evt.behavior}</div>` : ""}
      <div class="detail-actions">
        ${hasClip ? `<button class="detail-play-btn" onclick="openClipPlayer('${evt.id}')">CLIP</button>` : ""}
        ${idx > 0 ? `<button class="detail-nav-btn" onclick="showEventDetail(${idx-1})">Newer</button>` : ""}
        ${idx < cachedEvents.length-1 ? `<button class="detail-nav-btn" onclick="showEventDetail(${idx+1})">Older</button>` : ""}
      </div>
    </div>
  `;
}

function relativeTime(seconds) {
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function addRealtimeEvent(eventData) {
  if (!eventPanelOpen) return;
  cachedEvents.unshift(eventData);
  if (cachedEvents.length > 50) cachedEvents = cachedEvents.slice(0, 50);
  renderEventList(cachedEvents);
  const firstCard = document.querySelector(".event-card");
  if (firstCard) {
    firstCard.classList.add("new-event");
    setTimeout(() => firstCard.classList.remove("new-event"), 500);
  }
}

function openClipPlayer(eventId) {
  const overlay = document.getElementById("clip-overlay");
  const player = document.getElementById("clip-player");
  player.src = `${SERVER}/api/events/${eventId}/clip`;
  overlay.classList.add("active");
  player.play().catch(() => {});
}

function closeClipPlayer() {
  const overlay = document.getElementById("clip-overlay");
  const player = document.getElementById("clip-player");
  player.pause();
  player.src = "";
  overlay.classList.remove("active");
}

// =====================================================================
//  WEBSOCKET -- Real-time alerts
// =====================================================================

function connectWebSocket() {
  try {
    wsConnection = new WebSocket(WS_URL);

    wsConnection.onopen = () => {
      console.log("[WS] Connected to Sentinel");
    };

    wsConnection.onmessage = (msg) => {
      try {
        const data = JSON.parse(msg.data);
        if (data.type === "alert" && data.data) {
          addRealtimeEvent(data.data);
        }
      } catch {}
    };

    wsConnection.onclose = () => {
      console.log("[WS] Disconnected, reconnecting in 5s...");
      setTimeout(connectWebSocket, 5000);
    };

    wsConnection.onerror = () => {
      wsConnection.close();
    };
  } catch {
    setTimeout(connectWebSocket, 5000);
  }
}

// =====================================================================
//  ZONE EDITOR
// =====================================================================

function initZoneEditor() {
  const toggleBtn = document.getElementById("btn-zones");
  const saveBtn = document.getElementById("zone-save-btn");
  const cancelBtn = document.getElementById("zone-cancel-btn");

  toggleBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    toggleZoneEditor();
  });

  saveBtn.addEventListener("click", () => saveNewZone());
  cancelBtn.addEventListener("click", () => cancelZoneDrawing());
}

function toggleZoneEditor() {
  zoneEditorActive = !zoneEditorActive;
  const btn = document.getElementById("btn-zones");

  if (zoneEditorActive) {
    btn.classList.add("active");
    document.querySelectorAll(".zone-canvas").forEach(c => c.classList.add("active"));
    showZoneModeIndicator(true);
    redrawAllZones();
  } else {
    btn.classList.remove("active");
    document.querySelectorAll(".zone-canvas").forEach(c => c.classList.remove("active"));
    showZoneModeIndicator(false);
    cancelZoneDrawing();
  }
}

function showZoneModeIndicator(show) {
  let indicator = document.querySelector(".zone-mode-indicator");
  if (!indicator) {
    indicator = document.createElement("div");
    indicator.className = "zone-mode-indicator";
    indicator.textContent = "ZONE EDITOR \xb7 Click to draw polygon \xb7 Double-click to close";
    document.body.appendChild(indicator);
  }
  if (show) indicator.classList.add("active");
  else indicator.classList.remove("active");
}

function setupZoneEditorForCard(card, canvas, ch) {
  canvas.addEventListener("click", (e) => {
    if (!zoneEditorActive) return;
    e.stopPropagation();

    const rect = canvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;

    if (zoneDrawingCamera === null) {
      zoneDrawingCamera = ch;
      zoneDrawingVertices = [];
    }

    if (zoneDrawingCamera !== ch) return;

    zoneDrawingVertices.push({ x, y });
    redrawZoneCanvas(ch);
  });

  canvas.addEventListener("dblclick", (e) => {
    if (!zoneEditorActive) return;
    if (zoneDrawingCamera !== ch) return;
    e.stopPropagation();

    if (zoneDrawingVertices.length < 3) return;

    document.getElementById("zone-form").classList.add("active");
    document.getElementById("zone-name-input").focus();
  });

  canvas.addEventListener("mousemove", (e) => {
    if (!zoneEditorActive) return;
    if (zoneDrawingCamera !== ch) return;
    if (zoneDrawingVertices.length === 0) return;

    const rect = canvas.getBoundingClientRect();
    zoneMousePos = { x: e.clientX - rect.left, y: e.clientY - rect.top };
    redrawZoneCanvas(ch);
  });
}

async function fetchZones() {
  try {
    const resp = await fetch(`${SERVER}/api/zones`);
    cachedZones = await resp.json();
  } catch {
    cachedZones = {};
  }
}

function redrawAllZones() {
  for (let ch = 1; ch <= (config ? config.channels : 4); ch++) {
    redrawZoneCanvas(ch);
  }
}

function redrawZoneCanvas(ch) {
  const canvas = document.getElementById(`zone-canvas-${ch}`);
  if (!canvas) return;

  const card = canvas.closest(".cam-card");
  const rect = card.getBoundingClientRect();
  canvas.width = rect.width;
  canvas.height = rect.height;

  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  const cameraZones = cachedZones[ch] || cachedZones[String(ch)] || [];
  for (const zone of cameraZones) {
    drawZonePolygon(ctx, zone.polygon, zone.alert_level, zone.name, canvas.width, canvas.height);
  }

  if (zoneDrawingCamera === ch && zoneDrawingVertices.length > 0) {
    ctx.beginPath();
    ctx.moveTo(zoneDrawingVertices[0].x, zoneDrawingVertices[0].y);
    for (let i = 1; i < zoneDrawingVertices.length; i++) {
      ctx.lineTo(zoneDrawingVertices[i].x, zoneDrawingVertices[i].y);
    }

    if (zoneMousePos) {
      ctx.lineTo(zoneMousePos.x, zoneMousePos.y);
    }

    ctx.strokeStyle = "rgba(62, 207, 113, 0.8)";
    ctx.lineWidth = 2;
    ctx.setLineDash([4, 4]);
    ctx.stroke();
    ctx.setLineDash([]);

    for (const v of zoneDrawingVertices) {
      ctx.beginPath();
      ctx.arc(v.x, v.y, 4, 0, Math.PI * 2);
      ctx.fillStyle = "#3ecf71";
      ctx.fill();
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 1;
      ctx.stroke();
    }
  }
}

function drawZonePolygon(ctx, polygon, alertLevel, name, canvasW, canvasH) {
  const imgW = 704, imgH = 576;
  const scaleX = canvasW / imgW;
  const scaleY = canvasH / imgH;

  const colors = {
    critical: { stroke: "rgba(255, 60, 60, 0.45)" },
    warning:  { stroke: "rgba(255, 180, 40, 0.35)" },
    info:     { stroke: "rgba(80, 140, 255, 0.30)" },
  };
  const color = colors[alertLevel] || colors.info;

  const pts = polygon.map(p => [p[0] * scaleX, p[1] * scaleY]);

  ctx.beginPath();
  ctx.moveTo(pts[0][0], pts[0][1]);
  for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
  ctx.closePath();
  ctx.strokeStyle = color.stroke;
  ctx.lineWidth = 1;
  ctx.setLineDash([4, 3]);
  ctx.stroke();
  ctx.setLineDash([]);

  if (name) {
    ctx.font = "7px 'SF Mono', monospace";
    ctx.fillStyle = color.stroke;
    ctx.textAlign = "left";
    ctx.fillText(name, pts[0][0] + 3, pts[0][1] - 3);
  }
}

async function saveNewZone() {
  const nameInput = document.getElementById("zone-name-input");
  const levelSelect = document.getElementById("zone-level-select");
  const name = nameInput.value.trim();
  const level = levelSelect.value;

  if (!name || zoneDrawingVertices.length < 3 || zoneDrawingCamera === null) return;

  const canvas = document.getElementById(`zone-canvas-${zoneDrawingCamera}`);
  const imgW = 704, imgH = 576;
  const scaleX = imgW / canvas.width;
  const scaleY = imgH / canvas.height;

  const polygon = zoneDrawingVertices.map(v => [
    Math.round(v.x * scaleX),
    Math.round(v.y * scaleY),
  ]);

  try {
    await fetch(`${SERVER}/api/zones`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        camera: zoneDrawingCamera,
        name: name,
        polygon: polygon,
        alert_level: level,
        classes: ["person", "car", "dog", "cat"],
        night_boost: true,
        linger_seconds: 10,
      }),
    });

    await fetchZones();
    cancelZoneDrawing();
    redrawAllZones();
  } catch (err) {
    console.error("[ZONE] Save failed:", err);
  }
}

function cancelZoneDrawing() {
  zoneDrawingVertices = [];
  zoneDrawingCamera = null;
  zoneMousePos = null;
  document.getElementById("zone-form").classList.remove("active");
  document.getElementById("zone-name-input").value = "";
  redrawAllZones();
}

// =====================================================================
//  KEYBOARD SHORTCUTS
// =====================================================================

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    const clipOverlay = document.getElementById("clip-overlay");
    if (clipOverlay.classList.contains("active")) {
      closeClipPlayer();
      return;
    }

    if (zoneEditorActive && zoneDrawingVertices.length > 0) {
      cancelZoneDrawing();
      return;
    }

    if (zoneEditorActive) {
      toggleZoneEditor();
      return;
    }

    if (eventPanelOpen) {
      toggleEventPanel();
      return;
    }

    let hadZoom = false;
    document.querySelectorAll(".cam-card").forEach((card) => {
      const img = card.querySelector(".cam-image");
      if (img && img.style.transform && img.style.transform !== "none") {
        resetLiveZoom(card, img, card.querySelector(".zoom-canvas"));
        hadZoom = true;
      }
    });
    if (!hadZoom && expandedChannel !== null) collapseToGrid();
  }

  if (e.key === "e" || e.key === "E") {
    if (!e.target.closest("input, select, textarea")) {
      toggleEventPanel();
    }
  }

  if (e.key === "z" || e.key === "Z") {
    if (!e.target.closest("input, select, textarea")) {
      toggleZoneEditor();
    }
  }
});

document.addEventListener("contextmenu", (e) => e.preventDefault());

init();
