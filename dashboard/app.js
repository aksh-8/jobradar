"use strict";

const state = { jobs: [], status: "ALL", search: "" };
const jobsElement = document.getElementById("jobs");
const notice = document.getElementById("notice");
const urlScoreForm = document.getElementById("url-score-form");
const urlScoreResult = document.getElementById("url-score-result");

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
    const matchesStatus = state.status === "ALL" || job.status === state.status;
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
    const scoreDetails = details(job);
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
    const [healthResponse, jobsResponse] = await Promise.all([fetch("/health"), fetch("/api/jobs")]);
    if (!healthResponse.ok || !jobsResponse.ok) throw new Error("The JobRadar API is unavailable.");
    state.jobs = await jobsResponse.json();
    document.getElementById("health-dot").style.background = "#2b9861";
    document.getElementById("health-text").textContent = "API ready";
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

document.getElementById("refresh").addEventListener("click", loadJobs);
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
