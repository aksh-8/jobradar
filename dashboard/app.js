"use strict";

const state = { jobs: [], status: "ALL", search: "", activeOutreachJobId: null };
const jobsElement = document.getElementById("jobs");
const notice = document.getElementById("notice");
const urlScoreForm = document.getElementById("url-score-form");
const urlScoreResult = document.getElementById("url-score-result");
const outreachOverlay = document.getElementById("outreach-overlay");
let outreachReturnFocus = null;

function showNotice(message = "") {
  notice.textContent = message;
  notice.classList.toggle("hidden", !message);
}

function details(job) {
  return job.score_details || {};
}

function updateMetrics() {
  const scored = state.jobs.filter((job) => Number.isFinite(job.overall_score));
  const average = scored.length
    ? Math.round(scored.reduce((sum, job) => sum + job.overall_score, 0) / scored.length)
    : null;
  document.getElementById("metric-total").textContent = state.jobs.length;
  document.getElementById("metric-qualified").textContent = state.jobs.filter(
    (job) => details(job).verdict === "QUALIFIED",
  ).length;
  document.getElementById("metric-applied").textContent = state.jobs.filter(
    (job) => job.status === "APPLIED",
  ).length;
  document.getElementById("metric-average").textContent = average ?? "—";
}

function filteredJobs() {
  const query = state.search.toLowerCase();
  return state.jobs.filter((job) => {
    const matchesStatus = state.status === "ALL"
      || (state.status === "QUALIFIED" && details(job).verdict === "QUALIFIED")
      || job.status === state.status;
    const matchesSearch = !query || `${job.title} ${job.company}`.toLowerCase().includes(query);
    return matchesStatus && matchesSearch;
  });
}

function render() {
  jobsElement.replaceChildren();
  const visible = filteredJobs();
  document.getElementById("empty").classList.toggle("hidden", visible.length > 0);
  for (const job of visible) {
    const card = document.getElementById("job-template").content.firstElementChild.cloneNode(true);
    card.dataset.jobId = job.id;
    card.querySelector(".score strong").textContent = job.overall_score ?? "—";
    card.querySelector(".company").textContent = job.company;
    card.querySelector("h2").textContent = job.title;
    card.querySelector(".status").textContent = job.status;
    card.querySelector(".open-job").href = job.url;
    card.querySelector(".job-location").textContent = job.location_tier
      ? `${job.location_tier} • ${job.location}`
      : job.location || "US location";
    const scoreDetails = details(job);
    card.querySelector(".delete-job").addEventListener("click", () => deleteJob(job));
    card.querySelector(".resume-note strong").textContent = scoreDetails.recommended_resume
      || scoreDetails.resume_profile_id
      || "Not recorded";
    const dimensions = scoreDetails.dimensions || {};
    card.querySelector(".signals").replaceChildren(...Object.entries(dimensions).map(([name, value]) => {
      const signal = document.createElement("span");
      signal.className = "signal";
      signal.textContent = `${name.replaceAll("_", " ")} ${value}`;
      return signal;
    }));
    card.querySelector(".rationale").textContent = (scoreDetails.rationale || []).join(" ")
      || "No scoring rationale is available.";
    card.querySelector(".outreach-button").addEventListener("click", (event) => {
      openOutreach(job, event.currentTarget);
    });
    for (const button of card.querySelectorAll("button[data-action]")) {
      const isCurrentStatus = button.dataset.action === job.status;
      button.disabled = isCurrentStatus;
      button.classList.toggle("current", isCurrentStatus);
      button.addEventListener("click", () => changeStatus(job.id, button.dataset.action));
    }
    jobsElement.append(card);
  }
  updateMetrics();
}

function renderList(elementId, values, emptyMessage) {
  const list = document.getElementById(elementId);
  const items = values?.length ? values : [emptyMessage];
  list.replaceChildren(...items.map((value) => {
    const item = document.createElement("li");
    item.textContent = value;
    return item;
  }));
}

async function openOutreach(job, returnFocus) {
  outreachReturnFocus = returnFocus;
  state.activeOutreachJobId = job.id;
  document.body.classList.add("panel-open");
  outreachOverlay.classList.remove("hidden");
  outreachOverlay.setAttribute("aria-hidden", "false");
  document.getElementById("outreach-title").textContent = job.title;
  document.getElementById("outreach-company").textContent = job.company;
  document.getElementById("outreach-loading").classList.remove("hidden");
  document.getElementById("outreach-error").classList.add("hidden");
  document.getElementById("outreach-content").classList.add("hidden");
  document.getElementById("contact-results").replaceChildren();
  document.getElementById("contact-status").textContent = "";
  document.getElementById("contact-status").classList.remove("error");
  const contactButton = document.getElementById("find-contacts");
  contactButton.disabled = false;
  contactButton.textContent = "Find people";
  contactButton.dataset.refresh = "false";
  document.getElementById("close-outreach").focus();

  try {
    const response = await fetch(`/api/jobs/${job.id}/outreach`);
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || "Could not load outreach guidance.");
    document.getElementById("outreach-score").textContent = `${body.overall_score}/100 fit`;
    document.getElementById("outreach-resume").textContent = `Resume: ${body.recommended_resume || "Review"}`;
    document.getElementById("outreach-location").textContent = body.location || "United States";
    document.getElementById("outreach-strategy").textContent = body.strategy;
    document.getElementById("outreach-recruiter").textContent = body.recruiter_message;
    document.getElementById("outreach-referral").textContent = body.referral_request;
    document.getElementById("outreach-cold-email").textContent = body.cold_email;
    renderList("outreach-missing", body.missing_skills, "No material skill gaps recorded.");
    renderList("outreach-questions", body.questions, "No additional questions recorded.");
    document.getElementById("outreach-content").classList.remove("hidden");
  } catch (error) {
    const errorElement = document.getElementById("outreach-error");
    errorElement.textContent = error.message;
    errorElement.classList.remove("hidden");
  } finally {
    document.getElementById("outreach-loading").classList.add("hidden");
  }
}

function closeOutreach() {
  document.body.classList.remove("panel-open");
  outreachOverlay.classList.add("hidden");
  outreachOverlay.setAttribute("aria-hidden", "true");
  outreachReturnFocus?.focus();
  outreachReturnFocus = null;
  state.activeOutreachJobId = null;
}

function contactTypeLabel(contactType) {
  return {
    RECRUITER: "Recruiter",
    HIRING_MANAGER: "Hiring manager",
    TEAM_MEMBER: "Team member",
  }[contactType] || "Public contact";
}

function renderContacts(suggestions) {
  const results = document.getElementById("contact-results");
  results.replaceChildren(...suggestions.map((suggestion) => {
    const card = document.createElement("article");
    card.className = "contact-card";

    const heading = document.createElement("div");
    heading.className = "contact-card-head";
    const link = document.createElement("a");
    link.href = suggestion.profile_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = suggestion.name;
    const type = document.createElement("span");
    type.className = "contact-type";
    type.textContent = contactTypeLabel(suggestion.contact_type);
    heading.append(link, type);

    const title = document.createElement("p");
    title.className = "contact-title";
    title.textContent = suggestion.title;
    const evidence = document.createElement("p");
    evidence.className = "contact-evidence";
    evidence.textContent = `${suggestion.confidence}% confidence - ${suggestion.source}. ${suggestion.evidence}`;
    card.append(heading, title, evidence);
    return card;
  }));
}

async function findContacts(refresh = false) {
  const jobId = state.activeOutreachJobId;
  if (!jobId) return;
  const button = document.getElementById("find-contacts");
  const statusElement = document.getElementById("contact-status");
  button.disabled = true;
  button.textContent = "Searching...";
  statusElement.classList.remove("error");
  statusElement.textContent = "Searching public results for relevant people...";
  try {
    const response = await fetch(`/api/jobs/${jobId}/contacts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh }),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || "Could not search for contacts.");
    if (state.activeOutreachJobId !== jobId) return;
    renderContacts(body.suggestions);
    statusElement.textContent = body.suggestions.length
      ? `${body.suggestions.length} public suggestion${body.suggestions.length === 1 ? "" : "s"}${body.cached ? " (cached)" : ""}.`
      : "No credible public suggestions found. Try a manual LinkedIn company-people search.";
    button.textContent = "Refresh results";
    button.dataset.refresh = "true";
  } catch (error) {
    if (state.activeOutreachJobId !== jobId) return;
    statusElement.textContent = error.message;
    statusElement.classList.add("error");
    button.textContent = "Try again";
  } finally {
    if (state.activeOutreachJobId === jobId) button.disabled = false;
  }
}

function showUrlScoreResult(message = "", isError = false) {
  urlScoreResult.textContent = message;
  urlScoreResult.classList.toggle("error", isError);
  urlScoreResult.classList.toggle("hidden", !message);
}

async function scoreJobUrl(event) {
  event.preventDefault();
  const button = document.getElementById("score-url");
  const url = document.getElementById("job-url").value.trim();
  if (!url) return;

  button.disabled = true;
  button.textContent = "Scoring…";
  showUrlScoreResult("Reading the job page and choosing the best resume…");
  try {
    const response = await fetch("/api/score-url", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, profile_id: "auto" }),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || "Could not score this job page.");
    const resume = body.recommended_resume || body.resume_profile_id || "recommended profile";
    showUrlScoreResult(`Scored ${body.overall_score}/100. Apply with ${resume}.`);
    urlScoreForm.reset();
    await loadJobs();
  } catch (error) {
    showUrlScoreResult(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Score job";
  }
}

async function loadJobs() {
  showNotice("");
  try {
    const [healthResponse, jobsResponse, discoveryResponse] = await Promise.all([
      fetch("/health"),
      fetch("/api/jobs?us_only=true"),
      fetch("/api/discovery/status"),
    ]);
    if (!healthResponse.ok || !jobsResponse.ok) throw new Error("The JobRadar API is unavailable.");
    state.jobs = await jobsResponse.json();
    document.getElementById("health-dot").style.background = "#2b9861";
    document.getElementById("health-text").textContent = "API ready";
    if (discoveryResponse.ok) {
      const discovery = await discoveryResponse.json();
      const discoveryDot = document.getElementById("discovery-dot");
      const discoveryText = document.getElementById("discovery-text");
      discoveryDot.style.background = discovery.status === "ok"
        ? "#2b9861"
        : discovery.status === "failed" ? "#b84b45" : "#c89431";
      discoveryText.textContent = discovery.status === "ok"
        ? "Discovery healthy"
        : discovery.status === "failed" ? "Discovery failed" : "Discovery unknown";
      if (discovery.status === "failed") showNotice(discovery.message);
    }
    render();
  } catch (error) {
    document.getElementById("health-dot").style.background = "#b84b45";
    document.getElementById("health-text").textContent = "API offline";
    showNotice(error.message);
  }
}

async function changeStatus(jobId, status) {
  let skipReason = null;
  if (status === "SKIPPED") {
    skipReason = window.prompt("Why are you skipping this role?");
    if (!skipReason?.trim()) return;
  }
  try {
    const response = await fetch(`/api/jobs/${jobId}/status`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status, skip_reason: skipReason }),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || "Could not update this job.");
    state.jobs = state.jobs.map((job) => job.id === jobId ? body : job);
    render();
  } catch (error) {
    showNotice(error.message);
  }
}

async function deleteJob(job) {
  const confirmed = window.confirm(
    `Permanently remove "${job.title}" at ${job.company} from JobRadar? This cannot be undone.`,
  );
  if (!confirmed) return;
  try {
    const response = await fetch(`/api/jobs/${job.id}`, { method: "DELETE" });
    if (!response.ok) {
      const body = await response.json();
      throw new Error(body.detail || "Could not delete this job.");
    }
    state.jobs = state.jobs.filter((candidate) => candidate.id !== job.id);
    render();
    showNotice("Job deleted.");
  } catch (error) {
    showNotice(error.message);
  }
}

document.getElementById("refresh").addEventListener("click", loadJobs);
document.getElementById("close-outreach").addEventListener("click", closeOutreach);
document.getElementById("find-contacts").addEventListener("click", (event) => {
  findContacts(event.currentTarget.dataset.refresh === "true");
});
outreachOverlay.addEventListener("click", (event) => {
  if (event.target === outreachOverlay) closeOutreach();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !outreachOverlay.classList.contains("hidden")) {
    closeOutreach();
  }
});
for (const button of document.querySelectorAll("button[data-copy]")) {
  button.addEventListener("click", async () => {
    const source = document.getElementById(button.dataset.copy);
    await navigator.clipboard.writeText(source.textContent);
    const original = button.textContent;
    button.textContent = "Copied";
    setTimeout(() => { button.textContent = original; }, 1200);
  });
}
urlScoreForm.addEventListener("submit", scoreJobUrl);
document.getElementById("search").addEventListener("input", (event) => {
  state.search = event.target.value.trim();
  render();
});
for (const button of document.querySelectorAll(".filter")) {
  button.addEventListener("click", () => {
    state.status = button.dataset.status;
    document.querySelector(".filter.active")?.classList.remove("active");
    button.classList.add("active");
    render();
  });
}
loadJobs();
