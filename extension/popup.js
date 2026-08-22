"use strict";

const sections = ["idle", "loading", "result", "error"];

function show(section) {
  for (const id of sections) document.getElementById(id).classList.toggle("hidden", id !== section);
}

function renderResult(result) {
  document.getElementById("score-value").textContent = result.overall_score;
  document.getElementById("verdict").textContent = result.verdict.replaceAll("_", " ");
  document.getElementById("provider").textContent = result.provider
    ? `Scored by ${result.provider}${result.fallback_used ? " fallback" : ""}`
    : "Resolved by hard-filter policy";

  const flags = [...(result.hard_flags || []), ...(result.review_flags || [])];
  document.getElementById("flags").replaceChildren(...flags.map((flag) => {
    const item = document.createElement("p");
    item.textContent = flag.message;
    return item;
  }));
  document.getElementById("rationale").replaceChildren(...(result.rationale || []).map((reason) => {
    const item = document.createElement("li");
    item.textContent = reason;
    return item;
  }));
  show("result");
}

async function score() {
  show("loading");
  let response;
  try {
    response = await chrome.runtime.sendMessage({ type: "JOBRADAR_SCORE_ACTIVE_TAB" });
    if (response?.ok) return renderResult(response.data);
  } catch (error) {
    response = { error: error.message };
  }
  document.getElementById("error-message").textContent = response?.error || "Unknown extension error.";
  show("error");
}

async function loadSettings() {
  const values = await chrome.storage.local.get({
    apiBaseUrl: "http://127.0.0.1:8000",
    profileId: "akash-biswal",
  });
  document.getElementById("api-url").value = values.apiBaseUrl;
  document.getElementById("profile-id").value = values.profileId;
}

async function saveSettings() {
  const apiBaseUrl = document.getElementById("api-url").value.trim();
  const profileId = document.getElementById("profile-id").value.trim();
  const status = document.getElementById("save-status");
  if (!/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(apiBaseUrl) || !profileId) {
    status.textContent = "Use a loopback HTTP URL and a profile ID.";
    return;
  }
  const response = await chrome.runtime.sendMessage({
    type: "JOBRADAR_SAVE_SETTINGS",
    apiBaseUrl,
    profileId,
  });
  status.textContent = response?.ok ? "Saved." : response?.error || "Could not save settings.";
}

document.getElementById("score").addEventListener("click", score);
document.getElementById("rescore").addEventListener("click", score);
document.getElementById("retry").addEventListener("click", score);
document.getElementById("save").addEventListener("click", saveSettings);
loadSettings();
