/**
 * background.js — Service worker for TCG Utils extension.
 *
 * Holds the native messaging port so the watcher keeps running when the
 * popup is closed. Relays messages between the popup and the native host.
 *
 * Message protocol (popup ↔ background):
 *   Popup sends:  { type: "start"|"stop"|"status"|"get_config", config?: {...} }
 *   Background sends to popup: whatever the native host returns, plus
 *                              { type: "connection_error", message: "..." }
 *
 * The background also buffers the last ~50 log lines so a freshly opened
 * popup can display recent history immediately.
 */

const HOST_NAME = "com.twocorgistcg.host";
const LOG_BUFFER_SIZE = 50;

console.log("background.js: loaded");

let port = null;          // Native messaging port
let logBuffer = [];       // Recent log lines
let lastStatus = null;    // Most recent status object from the host
let popupPort = null;     // Message channel to the open popup (if any)

// ---------------------------------------------------------------------------
// Native host connection
// ---------------------------------------------------------------------------

function connectToHost() {
  if (port) return; // Already connected
  console.log("background.js: connectToHost");

  try {
    port = chrome.runtime.connectNative(HOST_NAME);
    console.log("background.js: connectNative returned", port);
  } catch (err) {
    broadcastToPopup({ type: "connection_error", message: err.message });
    port = null;
    return;
  }

  port.onMessage.addListener((msg) => {
    if (msg.type === "log") {
      logBuffer.push(msg);
      if (logBuffer.length > LOG_BUFFER_SIZE) logBuffer.shift();
    } else if (msg.type === "status") {
      lastStatus = msg;
    }
    broadcastToPopup(msg);
  });

  port.onDisconnect.addListener(() => {
    const err = chrome.runtime.lastError?.message || "Native host disconnected.";
    port = null;
    lastStatus = lastStatus ? { ...lastStatus, running: false } : null;
    broadcastToPopup({ type: "connection_error", message: err });
  });
}

function disconnectFromHost() {
  if (port) {
    port.disconnect();
    port = null;
  }
}

function sendToHost(msg) {
  if (!port) {
    connectToHost();
    // Give the port a moment to establish, then retry once
    if (!port) {
      broadcastToPopup({ type: "connection_error", message: "Could not connect to native host. Is the package installed and tcg-setup run?" });
      return;
    }
  }
  port.postMessage(msg);
}

// ---------------------------------------------------------------------------
// Popup communication
// ---------------------------------------------------------------------------

function broadcastToPopup(msg) {
  if (popupPort) {
    try {
      popupPort.postMessage(msg);
    } catch (_) {
      popupPort = null;
    }
  }
}

// Listen for connections from the popup
chrome.runtime.onConnect.addListener((incomingPort) => {
  console.log("background.js: onConnect", incomingPort.name);
  if (incomingPort.name !== "popup") return;

  popupPort = incomingPort;

  // Send buffered logs and last known status immediately
  for (const entry of logBuffer) {
    popupPort.postMessage(entry);
  }
  if (lastStatus) {
    popupPort.postMessage(lastStatus);
  }

  // If we're not connected to the host yet, fetch current config/status
  if (!port) {
    connectToHost();
    if (port) {
      port.postMessage({ type: "get_config" });
    }
  } else {
    port.postMessage({ type: "status" });
  }

  incomingPort.onMessage.addListener((msg) => {
    if (msg.type === "start") {
      connectToHost();
    }
    if (msg.type === "stop") {
      // After stopping, we can disconnect from the host too
      sendToHost(msg);
      return;
    }
    sendToHost(msg);
  });

  incomingPort.onDisconnect.addListener(() => {
    popupPort = null;
    // Keep the native port open if the watcher is running
    if (lastStatus && !lastStatus.running && port) {
      disconnectFromHost();
    }
  });
});
