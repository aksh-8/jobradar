"use strict";

const sections = ["idle", "loading", "result", "error"];
let lastResult = null;

function show(section) {
  for (const id of sections) document.getElementById(id).classList.toggle("hidden", id !== section);
}

function renderResult(result) {
  lastResult = result;
  const scoreValue = document.getElementById("score-value");
  scoreValue.textContent = result.overall_score;
  scoreValue.className = result.overall_score >= 75 ? "good" : result.overall_score >= 55 ? "warn" : "bad";
  document.getElementById("verdict").textContent = (result.action_verdict || result.verdict)
    .replaceAll("_", " ");
  document.getElementById("provider").textContent = result.provider
    ? `Scored by ${result.provider}${result.fallback_used ? " fallback" : ""}`
    : "Resolved by hard-filter policy";
  document.getElementById("resume-name").textContent = result.recommended_resume
    || "Manual selection required";
  document.getElementById("sponsorship").textContent = result.sponsorship_signal || "UNKNOWN";
  document.getElementById("salary").textContent = salaryText(result);

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
  renderChips("matched", result.matched_requirements || [], false);
  renderChips("missing", result.missing_requirements || [], true);
  document.getElementById("action-status").textContent = "";
  for (const id of ["apply", "skip"]) {
    document.getElementById(id).disabled = !result.job_id;
  }
  show("result");
}

function renderChips(elementId, values, missing) {
  const chips = values.map((value) => {
    const chip = document.createElement("span");
    chip.className = missing ? "chip missing" : "chip";
    chip.textContent = value;
    return chip;
  });
  if (!chips.length) {
    const chip = document.createElement("span");
    chip.className = "muted";
    chip.textContent = "None identified";
    chips.push(chip);
  }
  document.getElementById(elementId).replaceChildren(...chips);
}

function salaryText(result) {
  const minimum = result.base_salary_min_usd;
  const maximum = result.base_salary_max_usd;
  const dollars = (value) => `$${Number(value).toLocaleString("en-US")}`;
  if (minimum != null && maximum != null) return `${dollars(minimum)}-${dollars(maximum)} base`;
  if (minimum != null) return `From ${dollars(minimum)} base`;
  if (maximum != null) return `Up to ${dollars(maximum)} base`;
  return "Unknown - verify manually";
}

async function updateLifecycle(status) {
  if (!lastResult?.job_id) return;
  let skipReason = null;
  if (status === "SKIPPED") {
    skipReason = window.prompt("Why are you skipping this role?");
    if (!skipReason?.trim()) return;
  }
  const response = await chrome.runtime.sendMessage({
    type: "JOBRADAR_UPDATE_STATUS",
    jobId: lastResult.job_id,
    status,
    skipReason,
  });
  document.getElementById("action-status").textContent = response?.ok
    ? `Saved as ${status}.`
    : response?.error || "Could not update this job.";
}

async function draftOutreach() {
  if (!lastResult?.posting) return;
  const evidence = lastResult.matched_requirements?.[0] || "platform and automation engineering";
  const message = `Hi - I applied for the ${lastResult.posting.title} role at ${lastResult.posting.company}. My recent work includes ${evidence}, production automation, and end-to-end platform delivery. The role looks closely aligned. Could you point me to the recruiter or hiring manager responsible?`;
  try {
    await navigator.clipboard.writeText(message);
    document.getElementById("action-status").textContent = "Recruiter message copied.";
  } catch (_error) {
    document.getElementById("action-status").textContent = message;
  }
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
    profileId: "auto",
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
document.getElementById("apply").addEventListener("click", () => updateLifecycle("APPLIED"));
document.getElementById("skip").addEventListener("click", () => updateLifecycle("SKIPPED"));
document.getElementById("outreach").addEventListener("click", draftOutreach);
loadSettings();
