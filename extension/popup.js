/**
 * popup.js — UI logic for the TCG Utils extension popup.
 *
 * Connects to background.js via a long-lived port named "popup".
 * All native host communication is proxied through the background service worker.
 */

// ---------------------------------------------------------------------------
// DOM refs
// ---------------------------------------------------------------------------

const watchDirInput   = document.getElementById("watch-dir");
const outputDirInput  = document.getElementById("output-dir");
const headerHtInput   = document.getElementById("header-height");
const btnStart        = document.getElementById("btn-start");
const btnStop         = document.getElementById("btn-stop");
const btnClearLog     = document.getElementById("btn-clear-log");
const logOutput       = document.getElementById("log-output");
const statusBadge     = document.getElementById("status-badge");
const extensionIdHint = document.getElementById("extension-id-hint");

// ---------------------------------------------------------------------------
// Background port
// ---------------------------------------------------------------------------

const bgPort = chrome.runtime.connect({ name: "popup" });

bgPort.onMessage.addListener(handleMessage);
bgPort.onDisconnect.addListener(() => {
  appendLog("Lost connection to background.", "error");
});

// ---------------------------------------------------------------------------
// Message handling
// ---------------------------------------------------------------------------

function handleMessage(msg) {
  switch (msg.type) {
    case "status":
      applyStatus(msg);
      break;
    case "log":
      appendLog(msg.message, msg.level || "info");
      break;
    case "error":
      appendLog(msg.message, "error");
      setRunning(false);
      break;
    case "connection_error":
      appendLog(msg.message, "error");
      setRunning(false);
      break;
  }
}

function applyStatus(status) {
  if (status.watch_dir)     watchDirInput.value  = status.watch_dir;
  if (status.output_dir)    outputDirInput.value = status.output_dir;
  if (status.header_height) headerHtInput.value  = status.header_height;

  setRunning(status.running);
  // Persist whatever the host sent back
  saveFieldsToStorage();

  if (!status.has_header) {
    appendLog(
      "No header cached. Run: tcg-merge <orders.csv> <template.pdf>",
      "warning"
    );
  }
}

// ---------------------------------------------------------------------------
// UI state
// ---------------------------------------------------------------------------

function setRunning(running) {
  statusBadge.textContent = running ? "Running" : "Stopped";
  statusBadge.className   = `badge badge--${running ? "running" : "stopped"}`;
  btnStart.disabled = running;
  btnStop.disabled  = !running;
  watchDirInput.disabled  = running;
  outputDirInput.disabled = running;
  headerHtInput.disabled  = running;
}

function appendLog(message, level = "info") {
  const now = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const line = document.createElement("div");
  line.className = `log-line log-line--${level}`;
  line.textContent = `${now}  ${message}`;
  logOutput.appendChild(line);
  logOutput.scrollTop = logOutput.scrollHeight;
}

// ---------------------------------------------------------------------------
// Button handlers
// ---------------------------------------------------------------------------

btnStart.addEventListener("click", () => {
  const config = {
    watch_dir:     watchDirInput.value.trim(),
    output_dir:    outputDirInput.value.trim(),
    header_height: parseFloat(headerHtInput.value) || 2.5,
  };

  if (!config.watch_dir || !config.output_dir) {
    appendLog("Please set both watch and output folders.", "warning");
    return;
  }

  bgPort.postMessage({ type: "start", config });
});

btnStop.addEventListener("click", () => {
  bgPort.postMessage({ type: "stop" });
});

btnClearLog.addEventListener("click", () => {
  logOutput.innerHTML = "";
});

// ---------------------------------------------------------------------------
// Persist settings to storage on every change
// ---------------------------------------------------------------------------

function saveFieldsToStorage() {
  chrome.storage.local.set({
    watch_dir:     watchDirInput.value,
    output_dir:    outputDirInput.value,
    header_height: parseFloat(headerHtInput.value) || 2.5,
  });
}

[watchDirInput, outputDirInput, headerHtInput].forEach((el) =>
  el.addEventListener("change", saveFieldsToStorage)
);

// ---------------------------------------------------------------------------
// Restore saved field values on open
// ---------------------------------------------------------------------------

chrome.storage.local.get(["watch_dir", "output_dir", "header_height"], (saved) => {
  if (saved.watch_dir)     watchDirInput.value  = saved.watch_dir;
  if (saved.output_dir)    outputDirInput.value = saved.output_dir;
  if (saved.header_height) headerHtInput.value  = saved.header_height;
});

// ---------------------------------------------------------------------------
// Show extension ID in footer to assist with tcg-setup --chrome-id
// ---------------------------------------------------------------------------

extensionIdHint.textContent = `Extension ID: ${chrome.runtime.id}`;
