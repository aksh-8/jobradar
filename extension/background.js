"use strict";

const DEFAULT_SETTINGS = Object.freeze({
  apiBaseUrl: "http://127.0.0.1:8000",
  profileId: "auto",
});

async function settings() {
  return chrome.storage.local.get(DEFAULT_SETTINGS);
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) throw new Error("No active browser tab is available.");
  if (!/^https?:/.test(tab.url || "")) {
    throw new Error("Open an HTTP or HTTPS job page before scoring.");
  }
  return tab;
}

async function extractFromTab(tabId) {
  await chrome.scripting.executeScript({
    target: { tabId },
    files: ["parser.js", "content.js"],
  });
  const response = await chrome.tabs.sendMessage(tabId, { type: "JOBRADAR_EXTRACT" });
  if (!response?.ok) throw new Error(response?.error || "Job extraction failed.");
  return response.data;
}

async function requestScore(extracted) {
  const configured = await settings();
  const response = await fetch(`${configured.apiBaseUrl.replace(/\/$/, "")}/api/score`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      profile_id: configured.profileId,
      source: "browser-extension",
      url: extracted.page_url,
      posting: extracted.posting,
    }),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail || `JobRadar API returned HTTP ${response.status}.`);
  }
  return { ...body, page_url: extracted.page_url, posting: extracted.posting };
}

async function updateStatus(jobId, status, skipReason = null) {
  const configured = await settings();
  const response = await fetch(
    `${configured.apiBaseUrl.replace(/\/$/, "")}/api/jobs/${jobId}/status`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status, skip_reason: skipReason }),
    },
  );
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail || `JobRadar API returned HTTP ${response.status}.`);
  }
  return body;
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "JOBRADAR_SCORE_ACTIVE_TAB") {
    (async () => {
      const tab = await activeTab();
      return requestScore(await extractFromTab(tab.id));
    })()
      .then((result) => sendResponse({ ok: true, data: result }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message?.type === "JOBRADAR_SAVE_SETTINGS") {
    chrome.storage.local
      .set({ apiBaseUrl: message.apiBaseUrl, profileId: message.profileId })
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message?.type === "JOBRADAR_UPDATE_STATUS") {
    updateStatus(message.jobId, message.status, message.skipReason)
      .then((job) => sendResponse({ ok: true, data: job }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  return false;
});
