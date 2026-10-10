"use strict";
const $ = (id) => document.getElementById(id);
const state = { report: null, jobId: null, token: null, generation: 0, config: null };
const nativeLabels = { PASS: "Pass", FAIL: "Missing", WARNING: "Warning", UNKNOWN: "Unclear", NOT_APPLICABLE: "N/A", PENDING: "Waiting" };
const SOURCES = [["ora", "Ora"], ["cloudflare", "Cloudflare"]];
const sourceName = (id) => (SOURCES.find(([key]) => key === id) || [, "Source"])[1];
const sourceLabels = { completed: "Completed", partial: "Partial", running: "Running", pending: "Waiting", unavailable: "Unavailable", failed: "Error", timeout: "Timed out", interrupted: "Interrupted" };

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function notify(message) { $("notice").textContent = message; $("notice").hidden = !message; }
function busy(on) { $("scan-button").disabled = on; $("scan-button").ariaLabel = on ? "Scan in progress" : "Start scan"; }
function scalar(value, fallback = "") { return typeof value === "string" || (typeof value === "number" && Number.isFinite(value)) ? String(value) : fallback; }
function display(value, fallback = "") { if (value === null || value === undefined) return fallback; return scalar(value) || JSON.stringify(value) || fallback; }
function badge(status) { const key = typeof status === "string" && Object.hasOwn(nativeLabels, status) ? status : "UNKNOWN"; return element("span", `pill ${key}`, nativeLabels[key]); }
function formatTime(value) { if (typeof value !== "string" || !value.trim()) return "No timestamp"; const date = new Date(value); return Number.isNaN(date.getTime()) ? "No timestamp" : date.toLocaleString("en-US"); }

function setView(view) {
  const changed = state.view !== view;
  state.view = view;
  if (view !== "report") $("results").hidden = true;
  for (const name of ["landing", "loading", "report"]) $(name + "-view").hidden = name !== view;
  if (changed && view === "loading") $("loading-title").focus();
  if (changed && view === "report") $("report-domain").focus();
  if (changed && view === "landing") $("domain").focus();
}

function returnToLanding() {
  ++state.generation; clearReport(); busy(false); notify(""); setView("landing");
}

function clearReport() {
  state.report = null; state.jobId = null; state.renderedKey = null;
  $("results").hidden = true; $("empty-state").hidden = false;
  for (const id of ["download-report", "ora-raw", "cloudflare-raw"]) $(id).removeAttribute("href");
  $("evidence-dialog").close();
  updateSources();
}

async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...options.headers };
  if (options.body) { headers["Content-Type"] = "application/json"; if (state.token) headers["X-CSRF-Token"] = state.token; }
  const response = await fetch(path, { ...options, headers, signal: AbortSignal.timeout(20000) });
  const type = response.headers.get("content-type") || "";
  if (!type.includes("application/json")) throw new Error("Backend is not ready. Expected a JSON API response.");
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.error === "string" ? data.error : `Request failed (${response.status}).`);
  return data;
}

function updateSources(sources = []) {
  if (!Array.isArray(sources)) sources = sources && typeof sources === "object" ? Object.entries(sources).map(([id, value]) => ({ ...value, id })) : [];
  for (const [id] of SOURCES) {
    const source = sources.find((s) => s && (s.id || s.source_id) === id);
    const status = scalar(source?.status, "pending");
    $(id + "-state").textContent = Object.hasOwn(sourceLabels, status) ? sourceLabels[status] : status;
    $(id + "-state").className = "source-state " + (status === "completed" ? "completed" : ["pending", "running"].includes(status) ? "running" : "unavailable");
    const parts = [];
    if (scalar(source?.score)) parts.push(`Source score: ${scalar(source.score)}/100${scalar(source.grade) ? " · " + scalar(source.grade) : ""}`);
    if (scalar(source?.level)) parts.push(`Source level: ${scalar(source.level)}/5`);
    if (scalar(source?.check_count)) parts.push(`${scalar(source.check_count)} checks`);
    if (scalar(source?.error)) parts.push(scalar(source.error));
    if (source?.observed_at) parts.push(formatTime(source.observed_at));
    $(id + "-detail").textContent = parts.join(" · ") || (["running", "pending"].includes(status) ? "Waiting for source report…" : "No results yet");
  }
}

function openEvidence(check) {
  $("evidence-title").textContent = check.title;
  $("evidence-reason").textContent = `${check.measurement_definition} ${check.reason}`;
  for (const [id] of SOURCES) {
    const target = id + "-evidence";
    const observation = check.sources[id];
    $(target).textContent = observation?.raw ? JSON.stringify({ source_check_id: observation.source_check_id, observed_at: observation.observed_at, raw: observation.raw }, null, 2) : "No check result from this source.";
  }
  $("evidence-dialog").showModal();
}

function renderChecks() {
  const tbody = $("checks"); tbody.replaceChildren();
  const filter = $("filter").value;
  const checks = (state.report?.checks || []).filter((check) => {
    if (filter === "all") return true;
    if (filter === "pass") return check.combined_status === "PASS";
    return check.combined_status === "UNKNOWN";
  });
  for (const check of checks) {
    const tr = element("tr");
    const title = element("td"); const button = element("button", "check-button", check.title);
    button.addEventListener("click", () => openEvidence(check));
    title.append(button);
    tr.append(title);
    for (const [source] of SOURCES) {
      const td = element("td"); const observation = check.sources[source];
      td.append(badge(observation?.status || "UNKNOWN"));
      if (observation?.native_status !== null && observation?.native_status !== undefined) td.append(element("small", "", `Native: ${display(observation.native_status)}`));
      tr.append(td);
    }
    const score = element("td"); score.append(badge(check.scoring_status));
    score.append(element("small", "", check.scoring_source ? `${sourceName(check.scoring_source)} result` : "Not scored"));
    tr.append(score, element("td", "", check.reason)); tbody.append(tr);
  }
  if (!checks.length) { const td = element("td", "", "No checks match this filter."); td.colSpan = 5; const tr = element("tr"); tr.append(td); tbody.append(tr); }
}

const categoryCopy = {
  discovery: ["Discoverability", "Can agents find your entry points?", "01"],
  content: ["Readability", "Is your content easy for machines to read?", "02"],
  usability: ["Usability", "Are there integration and authentication signals?", "03"],
  security: ["Security & compliance", "Not measured in this version yet.", "04"],
  payments: ["Payments", "Not measured in this version yet.", "05"],
};
const checkGuidance = {
  "discovery.sitemap": ["A sitemap helps agents discover the pages on your site.", "Publish a valid XML sitemap and reference it in robots.txt."],
  "discovery.link_headers": ["Link headers announce your API, docs and other machine entry points.", "Advertise your machine entry points with the right HTTP Link relations."],
  "content.llms_txt": ["llms.txt gives agents a short machine guide to your key content.", "Add a root /llms.txt with an H1 title and links to useful content."],
  "content.markdown": ["Serving Markdown lets agents fetch your content in a cleaner form.", "Check Markdown responses, content type and Accept/Vary behavior together."],
  "auth.oauth_metadata": ["OAuth metadata describes how to access your protected APIs.", "If you offer a protected API, publish root OAuth/OIDC discovery metadata. Not needed for public-only content."],
  "auth.protected_resource": ["Protected Resource metadata announces which authorization servers a resource uses.", "If you have protected resources, declare the resource id and authorization servers in metadata."],
  "integration.mcp_card": ["An MCP server card helps agents discover your MCP server.", "If you run an MCP server, publish its discovery card with the advertised endpoints."],
};
function modelReport(report) {
  const checks = Array.isArray(report?.checks) ? report.checks.filter(c => c && typeof c === "object") : [];
  return {
    passed: checks.filter(c => c.scoring_status === "PASS").length,
    failed: checks.filter(c => c.scoring_status === "FAIL").length,
    uncertain: checks.filter(c => !["PASS", "FAIL", "PENDING"].includes(c.scoring_status)).length,
    actions: checks.filter(c => c.scoring_status === "FAIL"),
  };
}
function reportSummary(report) {
  const model = modelReport(report);
  if (report?.score?.total === null || report?.score?.total === undefined) return "Not enough definitive measurements for a preliminary score. See source details below.";
  return `${model.passed} checks passed, ${model.failed} reported missing. ${model.uncertain} were not scored as a definitive PASS/FAIL. Findings are based on source reports.`;
}
function evidenceButton(check, label = "View source evidence ↗") {
  const button = element("button", "evidence-link", label);
  button.addEventListener("click", () => openEvidence(check));
  return button;
}
function renderReportExperience(report) {
  const model = modelReport(report);
  $("report-headline").textContent = report.score.final === false ? "Finalizing your score…" : report.score.total === null ? "More evidence needed." : model.failed ? "There is room to improve." : model.uncertain ? "Good signals, incomplete picture." : "Measured checks look good.";
  $("report-summary").textContent = reportSummary(report);
  $("report-counts").replaceChildren();
  for (const [count, label, tone] of [[model.passed,"Passed","positive"],[model.failed,"Missing","negative"],[model.uncertain,"Not scored","neutral"]]) {
    const item = element("div", "count-item " + tone);
    item.append(element("strong", "", count), element("span", "", label)); $("report-counts").append(item);
  }
  $("action-count").textContent = `${model.actions.length} checks`;
  $("action-list").replaceChildren();
  for (const [index, check] of model.actions.entries()) {
    const card = element("article", "action-card");
    card.append(element("span", "action-number", String(index + 1).padStart(2,"0")), element("h3", "", check.title));
    card.append(element("p", "", checkGuidance[check.check_id]?.[1] || "Review the reported gap together with its evidence."));
    card.append(element("small", "action-basis", `Based on ${sourceName(check.scoring_source)} · Pickable suggestion`), evidenceButton(check));
    $("action-list").append(card);
  }
  if (!model.actions.length) $("action-list").append(element("p", "report-empty", model.uncertain ? "No definitive gaps. Review unscored checks to see measurement gaps." : "No gaps found in the measured checks."));
  $("category-findings").replaceChildren();
  for (const [key, copy] of Object.entries(categoryCopy)) {
    const checks = (Array.isArray(report.checks) ? report.checks : []).filter(c => c?.category === key);
    const section = element("section", "finding-category");
    const heading = element("div", "category-heading");
    const titles = element("div"); titles.append(element("span", "category-number", copy[2]), element("h3", "", copy[0]), element("p", "", copy[1]));
    const valid = checks.filter(c => ["PASS","FAIL"].includes(c.scoring_status));
    heading.append(titles, element("span", "category-stat", valid.length ? `${valid.filter(c => c.scoring_status === "PASS").length}/${valid.length} definitive checks passed · ${checks.length - valid.length} not scored` : checks.length ? "No scorable results" : "Not measured")); section.append(heading);
    for (const check of checks) {
      const row = element("details", "finding-row");
      const summary = element("summary");
      summary.append(badge(check.scoring_status), element("span", "finding-title", check.title), element("span", "finding-arrow", "+")); row.append(summary);
      const body = element("div", "finding-body");
      body.append(element("p", "finding-purpose", checkGuidance[check.check_id]?.[0] || check.measurement_definition));
      const observations = element("div", "observation-grid");
      for (const [id, label] of SOURCES.filter(([id]) => check.sources?.[id])) {
        const observation = check.sources?.[id]; const raw = observation?.raw || {};
        const block = element("div", "observation-card");
        const head = element("div", "observation-heading"); head.append(element("strong", "", label),badge(observation?.status));
        block.append(head,element("p", "", scalar(raw.message) || scalar(raw.details) || "See the raw evidence for details.")); observations.append(block);
      }
      body.append(observations,element("p", "finding-context", check.reason),evidenceButton(check));
      row.append(body); section.append(row);
    }
    if (!checks.length) section.append(element("p", "unmeasured-note", "This layer is not covered by this version; it earns no points."));
    $("category-findings").append(section);
  }
}

function renderReport(report) {
  state.report = report;
  $("empty-state").hidden = true; $("results").hidden = false;
  $("report-domain").textContent = report.domain;
  $("report-time").textContent = `${formatTime(report.scanned_at)} · Mapping ${report.mapping_version || "0.1"}${state.config?.mode === "demo" ? " · TEST DATA" : ""}`;
  const pending = report.score.final === false;  // never show a number a still-running source could change
  $("score-total").textContent = pending ? "…" : report.score.total ?? "—";
  $("score-coverage").textContent = pending ? "Waiting for the remaining sources before scoring." : `${report.score.coverage}% weighted measurement coverage${report.score.coverage < 100 ? " · incomplete coverage" : ""}`;
  $("layers").replaceChildren();
  for (const layer of Object.values(report.score.breakdown)) {
    const row = element("div", "layer"); const track = element("div", "layer-track"); const fill = element("div", "layer-fill");
    fill.style.width = `${pending || layer.points === null ? 0 : Math.min(100, Math.max(0, layer.points / layer.max_points * 100))}%`; track.append(fill);
    row.append(element("span", "", layer.label), track, element("span", "layer-value", pending ? "…" : layer.points === null ? "Not measured" : `${layer.points}/${layer.max_points}`));
    row.title = `${layer.measured}/${layer.defined} checks measured`; $("layers").append(row);
  }
  updateSources(report.sources); renderChecks();
  const specific = report.source_specific || [];
  $("specific-title").textContent = `Source-specific findings (${specific.length})`;
  $("specific-list").replaceChildren();
  for (const item of specific) {
    const row = element("details", "specific-row"); const raw = item.raw || {};
    row.append(element("summary", "", `${sourceName(item.source)} · ${display(raw.name) || display(raw.title) || scalar(item.source_check_id)} · ${display(raw.status, "unknown")}`), element("pre", "", JSON.stringify(raw, null, 2)));
    $("specific-list").append(row);
  }
  $("recommendations").replaceChildren();
  const recommendations = Array.isArray(report.recommendations) ? report.recommendations : [];
  for (const fix of recommendations) $("recommendations").append(element("pre", "", typeof fix === "string" ? fix : JSON.stringify(fix, null, 2)));
  if (!recommendations.length) $("recommendations").append(element("p", "", "Ora did not provide recommendations in this report. Per-check explanations are in the evidence dialog."));
  const root = `/api/scans/${encodeURIComponent(state.jobId)}`;
  $("download-report").href = root + "/report"; $("ora-raw").href = root + "/raw/ora"; $("cloudflare-raw").href = root + "/raw/cloudflare";
  renderReportExperience(report);
  setView("report");
}

async function loadScan(id, generation) {
  if (generation !== state.generation) return;
  const result = await api(`/api/scans/${encodeURIComponent(id)}`);
  if (generation !== state.generation) return;
  state.jobId = id;
  const status = result.status || result.job?.status;
  updateSources(result.sources || result.source_runs || result.report?.sources);
  if (["pending", "queued", "running"].includes(status)) {
    const key = JSON.stringify(result.sources || []);
    if (result.report && key !== state.renderedKey) { state.renderedKey = key; renderReport(result.report); updateSources(result.sources); }  // live job status beats the partial report's "unavailable" placeholder
    else if (!result.report) setView("loading");
    $("loading-domain").textContent = scalar(result.domain || result.job?.domain, $("domain").value);
    busy(true); notify(result.report ? "Partial results — more sources are still running; the report updates automatically." : "Sources are running. This can take a minute; results update as they arrive.");
    setTimeout(() => loadScan(id, generation).catch((error) => { if (generation === state.generation) { busy(false); setView("landing"); notify(error.message + " You can reopen the saved scan from history."); } }), 1800);
  } else {
    if (result.report) renderReport(result.report); else setView("landing");
    busy(false);
    const sources = result.report?.sources || result.sources || [];
    const failed = Array.isArray(sources) && sources.filter((s) => s.status !== "completed");
    notify(["failed", "interrupted"].includes(status) ? "The scan could not finish. Any source reports received are kept." : failed?.length ? "Some sources did not complete. Showing available evidence; gaps are not counted as failures." : "");
    refreshHistory();
  }
}

async function refreshHistory() {
  try {
    const response = await api("/api/scans");
    const scans = response.scans || response.jobs || [];
    $("history").replaceChildren();
    for (const job of scans) {
      const button = element("button", "", job.domain);
      button.append(element("small", "", `${formatTime(job.created_at)} · ${sourceLabels[job.status] || job.status}`));
      button.addEventListener("click", () => {
        const generation = ++state.generation;
        clearReport(); busy(true); $("history-dialog").close(); setView("loading"); $("loading-domain").textContent = job.domain; notify("Loading saved scan…");
        loadScan(job.id || job.job_id, generation).catch((error) => { if (generation === state.generation) { busy(false); setView("landing"); notify(error.message); } });
      });
      $("history").append(button);
    }
    if (!scans.length) $("history").append(element("p", "", "No scans yet."));
  } catch (error) { $("history").replaceChildren(element("p", "", "Could not load history. Check the backend connection.")); }
}

$("scan-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const generation = ++state.generation; busy(true); notify("");
  clearReport(); setView("loading"); $("loading-domain").textContent = $("domain").value.trim();
  try {
    if (!state.config) throw new Error("Backend is not ready. Reload the page or start the backend.");
    const result = await api("/api/scans", { method: "POST", body: JSON.stringify({ domain: $("domain").value.trim() }) });
    if (generation !== state.generation) return;
    const id = result.job_id || result.id;
    if (!id) throw new Error("Backend did not return a scan id.");
    await loadScan(id, generation);
  } catch (error) { if (generation === state.generation) { busy(false); setView("landing"); notify(error.message); } }
});
$("filter").addEventListener("change", renderChecks);
$("refresh-history").addEventListener("click", refreshHistory);
$("close-dialog").addEventListener("click", () => $("evidence-dialog").close());
$("new-analysis").addEventListener("click", returnToLanding);
$("cancel-analysis").addEventListener("click", returnToLanding);
$("open-history").addEventListener("click", () => { refreshHistory(); $("history-dialog").showModal(); });
$("close-history").addEventListener("click", () => $("history-dialog").close());
async function initialize() {
  setView("landing");
  try {
    state.config = await api("/api/config"); state.token = state.config.csrf_token;
    $("demo-badge").hidden = state.config.mode !== "demo";
  } catch (error) { notify(error.message); }
  await refreshHistory();
}
initialize();
