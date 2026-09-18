"use strict";

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}

async function loadPlan() {
  loadShortlist();
  const status = document.getElementById("plan-status");
  const button = document.getElementById("reload-plan");
  const cards = document.getElementById("action-plan");
  button.disabled = true;
  status.textContent = "Loading your recent scoring evidence…";
  cards.replaceChildren();
  document.getElementById("plan-summary").classList.add("hidden");
  document.getElementById("evidence-panel").classList.add("hidden");
  try {
    const response = await fetch("/api/outlook/monthly");
    if (!response.ok) throw new Error("Could not load your plan. Check the connection and try Refresh plan.");
    const data = await response.json();
    const plan = data.plan || [];
    if (!plan.length) {
      status.textContent = "No scored roles in the last 30 days yet. Score a relevant job from the dashboard to start building your plan.";
      return;
    }
    status.textContent = "";
    const summary = document.getElementById("plan-summary");
    summary.replaceChildren(element("p", `${plan.length} priorities · ${data.roles_scored} scored roles · Last ${data.days} days`),
      element("p", "Start at the top. Ranking reflects speed to act, not a guaranteed score increase. Each item distinguishes observed gaps from suggested evidence checks."));
    summary.classList.remove("hidden");
    for (const activity of plan) {
      const card = element("article", "", "plan-card");
      const heading = element("div", "", "plan-heading");
      const title = element("div");
      title.append(element("h2", activity.title), element("p", `${activity.timing} · ${activity.effort}`, "plan-meta"));
      heading.append(element("span", String(activity.rank).padStart(2, "0"), "plan-rank"), title);
      const body = element("div", "", "plan-body");
      const diagnosis = element("div");
      diagnosis.append(element("h3", "Drawback to address"), element("p", activity.drawback),
        element("h3", "What this changes"), element("p", activity.impact));
      const instructions = element("div");
      const steps = element("ol");
      for (const step of activity.steps) steps.append(element("li", step));
      instructions.append(element("h3", "Do this"), steps);
      body.append(diagnosis, instructions);
      const deliverable = element("div", "", "deliverable");
      deliverable.append(element("h3", "Done when you have"), element("p", activity.deliverable));
      const evidence = element("details", "", "plan-evidence");
      evidence.append(element("summary", "Why this is in your plan"), element("p", activity.evidence));
      if (activity.examples.length) evidence.append(element("p", "Example roles: " + activity.examples.join("; ")));
      card.append(heading, body, deliverable, evidence);
      cards.append(card);
    }
    const evidence = document.getElementById("score-evidence");
    evidence.replaceChildren(element("p", Object.entries(data.dimension_averages).map(([name, score]) =>
      `${name.replaceAll("_", " ")}: ${score}/100`).join(" · ")),
      element("p", "Policy signals: " + Object.entries(data.policy_signals).map(([name, count]) =>
        `${name.replaceAll("_", " ")}: ${count}`).join(" · ")));
    document.getElementById("method-note").textContent = data.note;
    document.getElementById("evidence-panel").classList.remove("hidden");
  } catch (error) { status.textContent = error.message; }
  finally { button.disabled = false; }
}
document.getElementById("reload-plan").addEventListener("click", loadPlan);
loadPlan();

async function loadShortlist() {
  const status = document.getElementById("shortlist-status");
  const matches = document.getElementById("shortlist-matches");
  const review = document.getElementById("shortlist-review");
  matches.replaceChildren();
  review.replaceChildren();
  status.textContent = "Reviewing saved LA and US-remote roles…";
  try {
    const response = await fetch("/api/shortlist");
    if (!response.ok) throw new Error("Could not load shortlist. Use Refresh plan to retry.");
    const data = await response.json();
    status.textContent = `${data.matches.length} matches from ${data.reviewed.length} reviewed (${data.candidate_count} candidates). ${data.note}`;
    if (!data.matches.length) matches.append(element("p", "No credible matches yet. See the concerns below; the shortlist updates as discovery finds suitable roles."));
    for (const job of data.matches) {
      const card = element("article", "", "deliverable");
      card.append(element("h3", `${job.company} — ${job.title}`), element("p", job.reason), element("p", `Use resume: ${job.resume}`));
      const link = element("a", "Open job posting", "open-job");
      const url = new URL(job.url);
      if (["https:", "http:"].includes(url.protocol)) link.href = url.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      card.append(link);
      matches.append(card);
    }
    for (const job of data.reviewed) {
      review.append(element("p", `${job.company} — ${job.title}: ${job.concerns.join(" ") || job.reason}`));
    }
  } catch (error) { status.textContent = error.message; }
}
