"use strict";
const $ = (id) => document.getElementById(id);
const state = { report: null, jobId: null, token: null, generation: 0, config: null, intelligenceTimer: null, intelligenceDepth: "short", intelligenceMissingPolls: 0 };
const nativeLabels = { PASS: "Pass", FAIL: "Missing", WARNING: "Warning", UNKNOWN: "Not evaluated", NOT_APPLICABLE: "N/A", PENDING: "Waiting" };
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
  clearTimeout(state.intelligenceTimer); state.intelligenceTimer = null;
  state.intelligenceMissingPolls = 0;
  state.report = null; state.jobId = null; state.renderedKey = null;
  $("results").hidden = true; $("empty-state").hidden = false;
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
  if (report?.score?.total === null || report?.score?.total === undefined) return "We could not measure enough of this site to calculate a score yet.";
  return `${model.passed} checks are in place. ${model.failed} opportunities can improve how agents discover and use your site.${model.uncertain ? ` ${model.uncertain} areas need more information.` : ""}`;
}
function renderReportExperience(report) {
  const model = modelReport(report);
  $("report-headline").textContent = report.score.final === false ? "Calculating your score" : report.score.total === null ? "More evidence needed." : model.failed ? "There is room to improve." : model.uncertain ? "Good signals, incomplete picture." : "Measured checks look good.";
  $("report-summary").textContent = reportSummary(report);
  $("report-counts").replaceChildren();
  for (const [count, label, tone] of [[model.passed,"Passed","positive"],[model.failed,"Missing","negative"],[model.uncertain,"Not scored","neutral"]]) {
    const item = element("div", "count-item " + tone);
    item.append(element("strong", "", count), element("span", "", label)); $("report-counts").append(item);
  }
  $("action-list").replaceChildren();
  const checks = Array.isArray(report.checks) ? report.checks : [];
  const cards = [];
  const covered = new Set();
  for (const fix of Array.isArray(report.recommendations) ? report.recommendations : []) {
    if (!fix || typeof fix.recommendation !== "string" || !fix.recommendation.trim()) continue;
    const check = checks.find(row => row.check_id === fix.check_id);
    if (check && check.scoring_status !== "FAIL") continue;
    if (check) covered.add(check.check_id);
    cards.push({check, title: check?.title || "Improve agent readiness", action: fix.recommendation,
      source: check?.scoring_source || fix.source || null, providerRecommendation: fix.source === "cloudflare",
      pickableAuthored: fix.source === "pickable"});
  }
  for (const check of model.actions) {
    if (covered.has(check.check_id)) continue;
    const guidance = checkGuidance[check.check_id];
    if (guidance) cards.push({check, title: check.title, action: guidance[1], source: check.scoring_source, pickableAuthored: true});
  }
  $("action-count").textContent = `${cards.length} recommendations`;
  for (const [index, cardData] of cards.entries()) {
    const card = element("article", "action-card");
    card.append(element("span", "action-number", String(index + 1).padStart(2,"0")), element("h3", "", cardData.title));
    if (cardData.pickableAuthored) card.append(element("span", "action-basis", "Pickable recommendation · evidence from " + (cardData.source === "cloudflare" ? "Cloudflare" : cardData.source === "ora" ? "Ora" : "site checks")));
    else if (cardData.source === "cloudflare") card.append(element("span", "action-basis", cardData.providerRecommendation ? "Cloudflare recommendation" : "Evidence from Cloudflare · recommendation by Pickable"));
    else if (cardData.source === "ora") card.append(element("span", "action-basis", "Evidence reported by Ora · recommended improvement"));
    card.append(element("p", "", cardData.action));
    $("action-list").append(card);
  }
  if (!cards.length) $("action-list").append(element("p", "report-empty", model.uncertain ? "No changes to recommend from the available results. Some areas need more information." : "No improvements were identified in this scan."));
}

function renderReadinessFindings(report) {
  const target = $("check-findings");
  target.replaceChildren();
  const checks = Array.isArray(report?.checks) ? report.checks.filter(check =>
    check && typeof check === "object" && typeof check.check_id === "string" &&
    typeof check.category === "string" && typeof check.title === "string") : [];
  const recommendations = Array.isArray(report?.recommendations) ? report.recommendations.filter(item =>
    item && typeof item === "object" && typeof item.check_id === "string" &&
    typeof item.recommendation === "string" && item.recommendation.trim()) : [];
  const layers = report?.score?.breakdown && typeof report.score.breakdown === "object" ? report.score.breakdown : {};
  const categoryLabels = {discovery: "Discoverability", content: "Content", usability: "Usability"};
  const categoryKeys = [...new Set([...Object.keys(layers), ...checks.map(check => check.category)])]
    .filter(category => Object.hasOwn(categoryLabels, category));
  const providerNames = {ora: "Ora", cloudflare: "Cloudflare"};
  for (const category of categoryKeys) {
    const layer = layers[category] && typeof layers[category] === "object" ? layers[category]
      : {label: categoryLabels[category], points: null, max_points: 0, measured: 0,
          defined: checks.filter(check => check.category === category).length};
    const section = element("section", "finding-category");
    const heading = element("div", "category-heading");
    const copy = element("div");
    const title = scalar(layer.label, category);
    copy.append(element("h3", "", title), element("p", "", `${scalar(layer.measured, "0")}/${scalar(layer.defined, "0")} checks measured`));
    const result = layer.points === null || layer.points === undefined
      ? "Not scored" : `${scalar(layer.points)}/${scalar(layer.max_points)}`;
    heading.append(copy, element("span", "category-stat", result));
    section.append(heading);
    const grouped = checks.filter(check => check.category === category);
    for (const check of grouped) {
      const row = element("details", "finding-row");
      const summary = element("summary");
      summary.append(badge(check.scoring_status), element("span", "finding-title", check.title), element("span", "finding-arrow", "+"));
      row.append(summary);
      const body = element("div", "finding-body");
      const meaning = scalar(check.measurement_definition) || checkGuidance[check.check_id]?.[0] || "This check is part of the layer score.";
      body.append(element("p", "finding-purpose", meaning));
      const source = providerNames[check.scoring_source];
      if (source) body.append(element("p", "finding-context", `Evidence reported by ${source}.`));
      if (check.scoring_status === "FAIL") {
        const fix = recommendations.find(item => item.check_id === check.check_id)?.recommendation || checkGuidance[check.check_id]?.[1];
        if (fix) body.append(element("p", "finding-context", `Recommended next step: ${fix}`));
      } else if (check.scoring_status === "UNKNOWN" || check.scoring_status === "WARNING" || check.scoring_status === "PENDING") {
        body.append(element("p", "finding-context", check.scoring_status === "PENDING"
          ? "This check is waiting for source evidence." : "This check does not count as a pass or a failure in the score."));
      } else if (check.scoring_status === "PASS") {
        body.append(element("p", "finding-context", "This check passed in the available evidence."));
      }
      row.append(body);
      section.append(row);
    }
    if (!grouped.length) section.append(element("p", "finding-context", "No individual checks are available for this score layer."));
    target.append(section);
  }
}

// Score hero + layer strip; prefix "" = agent readiness, "mk-" = marketing playbook.
function renderScore(score, prefix) {
  const pending = score.final === false;  // never show a number a still-running source could change
  $(prefix + "score-total").parentElement.classList.toggle("pending", pending);
  if (pending) { const spinner = element("span", "spinner"); spinner.setAttribute("role", "status"); spinner.setAttribute("aria-label", "Calculating score"); $(prefix + "score-total").replaceChildren(spinner); }
  else $(prefix + "score-total").textContent = score.total ?? "—";
  const coverage = typeof score.coverage === "number" && Number.isFinite(score.coverage) ? score.coverage : 0;
  if (pending) $(prefix + "score-coverage").textContent = "Waiting for the remaining sources before scoring.";
  else if (score.total === null || score.total === undefined) $(prefix + "score-coverage").textContent = `${coverage}% of weighted checks evaluated · Insufficient data`;
  else $(prefix + "score-coverage").textContent = `${coverage}% of weighted checks evaluated${coverage < 100 ? " · incomplete coverage" : ""}`;
  $(prefix + "layers").replaceChildren();
  for (const layer of Object.values(score.breakdown || {})) {
    const row = element("div", "layer"); const track = element("div", "layer-track"); const fill = element("div", "layer-fill"); if (pending) track.classList.add("loading");
    fill.style.width = `${pending || layer.points === null ? 0 : Math.min(100, Math.max(0, layer.points / layer.max_points * 100))}%`; track.append(fill);
    row.append(element("span", "", layer.label), track, element("span", "layer-value", pending ? "Calculating" : layer.points === null ? "Not measured" : `${layer.points}/${layer.max_points}`));
    row.title = `${layer.measured}/${layer.defined} checks measured`; $(prefix + "layers").append(row);
    if (!pending) row.append(element("span", "layer-sub", `${scalar(layer.measured, "0")}/${scalar(layer.defined, "0")} checks measured`));
  }
}

const marketingCopy = {
  social: { title: "Social presence", question: "Can potential customers discover and connect with your brand across social platforms?",
    intro: "An accessible social presence helps visitors learn more about your business, explore your activity, and engage with your brand beyond your website.", index: "01" },
  content: { title: "Content & blog", question: "Does your website provide useful, discoverable content for potential customers?",
    intro: "A content strategy can help your business educate its audience, address customer questions, demonstrate expertise, and attract relevant visitors through search and other distribution channels.", index: "02" },
};
// Two-layer check copy: `summary` is the one-line closed explanation;
// `evaluate` / `why` / `success` open inside the <details> body.
const marketingGuidance = {
  "social.profiles": {
    summary: "Whether your site links to official brand profiles visitors can verify and follow.",
    evaluate: "We look for publicly accessible links to official brand profiles on platforms such as LinkedIn, Instagram, X, YouTube, and other relevant social networks.",
    why: "Clear links to official profiles make it easier for visitors to verify your brand identity, discover company updates, and engage through their preferred channels. They can also provide additional signals of business activity and credibility.",
    success: "Your website clearly links to at least one identifiable official social media profile. This confirms discoverability, not necessarily audience engagement or social media performance.",
    action: "Link your active social profiles (e.g. LinkedIn, X, Instagram) from the site header or footer." },
  "content.blog": {
    summary: "Whether your site offers an identifiable collection of informational content.",
    evaluate: "We check whether your website includes an accessible blog, resource library, insights section, knowledge center, or comparable collection of informational content.",
    why: "Useful content gives prospective customers opportunities to understand the problems your product solves before making a purchasing decision. Relevant, well-structured pages may also become entry points through search engines and other discovery channels.",
    success: "Your website contains an identifiable content section accessible to visitors. This confirms content availability, not its quality or traffic performance.",
    action: "Start a blog (e.g. /blog) and link it from your main navigation." },
  "content.blog_recent": {
    summary: "Whether your content section shows recently published material.",
    evaluate: "We examine available publication dates to determine whether new content has been added recently.",
    why: "Recent, relevant content can demonstrate that your business continues to publish information about its product, industry, or customer needs. However, publication recency alone does not establish content quality, credibility, or SEO performance.",
    success: "We can verify that your content section includes at least one publication within the evaluation's defined recency period.",
    action: "Publish a new post; aim for at least one every month or two." },
  "content.blog_cadence": {
    summary: "Whether your publishing history shows a recurring pattern rather than isolated updates.",
    evaluate: "We examine the dates of multiple published articles to assess whether the website demonstrates a recurring publishing pattern rather than isolated updates.",
    why: "A consistent publishing process can support long-term audience development and sustained content distribution. Its effectiveness still depends on relevance, quality, competition, and whether the content reaches the intended audience.",
    success: "Available publication history provides evidence of recurring content activity. This does not independently prove growth in search traffic, engagement, or conversions.",
    action: "Plan a simple content calendar with at least one post a month." },
};
function marketingStatus(check) {
  if (check.scoring_status === "PASS") return "Passed";
  if (check.scoring_status === "FAIL") return "Missing";
  if (check.scoring_status === "PENDING") return "Waiting";
  return "Not evaluated";
}
function renderMarketing(marketing) {
  const model = modelReport(marketing);
  const checks = Array.isArray(marketing.checks) ? marketing.checks : [];
  const insufficient = marketing.score.total === null || marketing.score.total === undefined;
  const unevaluated = checks.filter(c => !["PASS", "FAIL", "PENDING"].includes(c.scoring_status)).length;
  renderScore(marketing.score, "mk-");
  if (marketing.score.final === false) {
    $("mk-headline").textContent = "Calculating your score";
    $("mk-summary").textContent = "We are still collecting evidence from your website. Results appear as soon as each check finishes.";
  } else if (insufficient) {
    $("mk-headline").textContent = "We couldn't complete your website analysis";
    $("mk-summary").textContent = "Pickable couldn't retrieve enough information from this website to evaluate its marketing presence. "
      + "This doesn't mean your website has failed these checks or that your marketing setup is incomplete. "
      + "It means we don't yet have sufficient evidence to make a reliable assessment. "
      + "Your Marketing Playbook Score reflects how effectively your website supports essential marketing activities, including social media visibility and content publishing. "
      + "A score will be calculated once sufficient evidence has been collected."
      + (marketing.source?.error ? ` Details: ${marketing.source.error}` : "");
  } else {
    $("mk-headline").textContent = model.failed ? "There is room to grow." : model.uncertain ? "Good basics, incomplete picture." : "Your marketing basics look solid.";
    $("mk-summary").textContent = marketing.source?.error ? `The marketing checks could not load your site: ${marketing.source.error}` : `${model.passed} of ${checks.length} playbook checks passed, ${model.failed} need work.`;
  }
  $("mk-counts").replaceChildren();
  for (const [count, label, tone] of [[model.passed,"Passed","positive"],[model.failed,"Missing","negative"],[unevaluated,"Not evaluated","neutral"]]) {
    const item = element("div", "count-item " + tone); item.append(element("strong", "", count), element("span", "", label)); $("mk-counts").append(item);
  }
  $("mk-action-count").textContent = marketing.score.final === false ? "Checking now" : insufficient ? "Not available yet" : `${model.actions.length} recommendations`;
  $("mk-action-list").replaceChildren();
  for (const [index, check] of model.actions.entries()) {
    const card = element("article", "action-card");
    const guidance = marketingGuidance[check.check_id];
    const actionText = typeof guidance === "object" ? guidance.action : guidance?.[1] || check.measurement_definition;
    card.append(element("span", "action-number", String(index + 1).padStart(2,"0")), element("h3", "", check.title), element("p", "", actionText));
    $("mk-action-list").append(card);
  }
  if (marketing.score.final === false) $("mk-action-list").append(element("p", "report-empty", "Checking your site now."));
  else if (insufficient) $("mk-action-list").append(element("p", "report-empty",
    "Recommendations are not available yet. Pickable generates recommendations from issues identified during an actual website assessment. "
    + "Because the required checks couldn't be completed, we can't reliably identify missing marketing elements or recommend improvements. "
    + "Suggested action: retry the scan. If the issue persists, confirm that your website is publicly accessible and doesn't block automated requests."));
  else if (!model.actions.length) $("mk-action-list").append(element("p", "report-empty", model.uncertain ? "No changes to recommend from the available results. Some areas need more information." : "No improvements were identified in this scan."));
  $("mk-findings").replaceChildren();
  for (const [key, copy] of Object.entries(marketingCopy)) {
    const section = element("section", "finding-category"); const heading = element("div", "category-heading"); const titles = element("div");
    titles.append(element("span", "category-number", copy.index), element("h3", "", copy.title), element("p", "", copy.question), element("p", "finding-context", copy.intro)); heading.append(titles); section.append(heading);
    for (const check of checks.filter(c => c.category === key)) {
      const row = element("details", "finding-row"); const summary = element("summary");
      summary.append(badge(check.scoring_status), element("span", "finding-title", check.title), element("span", "finding-arrow", "+")); row.append(summary);
      const body = element("div", "finding-body");
      const guidance = marketingGuidance[check.check_id];
      const summaryText = typeof guidance === "object" ? guidance.summary : guidance?.[0] || check.measurement_definition;
      if (summaryText) body.append(element("p", "finding-purpose", summaryText));
      body.append(element("p", "finding-context", `Status: ${marketingStatus(check)}`));
      if (typeof guidance === "object") {
        body.append(element("p", "finding-context", `What we evaluate: ${guidance.evaluate}`));
        body.append(element("p", "finding-context", `Why it matters: ${guidance.why}`));
        body.append(element("p", "finding-context", `What a successful result means: ${guidance.success}`));
      }
      if (check.message) body.append(element("p", "finding-context", check.message));
      const profiles = check.details?.profiles;
      if (profiles && typeof profiles === "object") {
        const list = element("ul", "profile-list");
        for (const [network, url] of Object.entries(profiles)) list.append(element("li", "", `${network} · ${scalar(url)}`));
        body.append(list);
      }
      if (check.scoring_status === "FAIL") {
        const actionText = typeof guidance === "object" ? guidance.action : guidance?.[1];
        if (actionText) body.append(element("p", "finding-context", `Recommended next step: ${actionText}`));
      } else if (!["PASS", "FAIL"].includes(check.scoring_status)) {
        body.append(element("p", "finding-context", check.scoring_status === "PENDING"
          ? "This check is waiting for source evidence and doesn't affect the score yet."
          : "This check couldn't be evaluated from the available evidence, so it doesn't count as a pass or a failure."));
      }
      row.append(body); section.append(row);
    }
    $("mk-findings").append(section);
  }
}

// Loading screen: site preview (from the site's own og:image/title) and real per-source progress.
function resetPreview(domain) {
  $("preview-url").textContent = domain;
  $("preview-title").textContent = domain; $("preview-description").textContent = "";
  $("preview-image").hidden = true; $("preview-image").removeAttribute("src");
  $("preview-icon").hidden = true; $("preview-icon").removeAttribute("src");
  $("preview-card").hidden = false;
  updateSteps([]);
}
function showPreview(preview) {
  if (!preview || typeof preview !== "object") return;
  if (scalar(preview.title)) $("preview-title").textContent = preview.title;
  if (scalar(preview.description)) $("preview-description").textContent = preview.description;
  const https = (url) => typeof url === "string" && url.startsWith("https://");
  if (https(preview.icon)) { const icon = $("preview-icon"); icon.onerror = () => { icon.hidden = true; }; icon.onload = () => { icon.hidden = false; }; icon.src = preview.icon; }
  if (https(preview.image)) { const image = $("preview-image"); image.onerror = () => { image.hidden = true; $("preview-card").hidden = false; }; image.onload = () => { image.hidden = false; $("preview-card").hidden = true; }; image.src = preview.image; }
}
function loadPreview(id, generation) {
  api(`/api/scans/${encodeURIComponent(id)}/preview`).then((r) => { if (generation === state.generation) showPreview(r.preview); }).catch(() => {});
}
function updateSteps(sources = []) {
  const list = Array.isArray(sources) ? sources : [];
  let done = 0;
  for (const id of ["growth", "ora", "cloudflare"]) {
    const status = scalar(list.find((s) => s && (s.id || s.source_id) === id)?.status, "running");
    const finished = !["running", "pending"].includes(status);
    done += finished;
    $("step-" + id).className = status === "completed" ? "done" : finished ? "failed" : "";
    $("step-" + id).lastElementChild.textContent = status === "completed" ? "Done" : finished ? "Unavailable" : "";
  }
  const percent = Math.max(6, Math.round(done / 3 * 100));
  $("loading-progress-fill").style.width = percent + "%";
  $("loading-progress-fill").parentElement.setAttribute("aria-valuenow", String(percent));
}

function setMode(mode) {
  state.mode = mode === "agent" ? "agent" : "normal";
  document.body.dataset.mode = state.mode;
  for (const button of document.querySelectorAll("[data-mode-button]")) button.setAttribute("aria-pressed", String(button.dataset.modeButton === state.mode));
  try { localStorage.setItem("pickable-mode", state.mode); } catch {}
}

function renderReport(report) {
  state.report = report;
  $("empty-state").hidden = true; $("results").hidden = false;
  $("report-domain").textContent = report.domain;
  $("report-time").textContent = `${formatTime(report.scanned_at)}${state.config?.mode === "demo" ? " · TEST DATA" : ""}`;
  const model = modelReport(report);
  const total = report.score?.total ?? "—";
  const copy = $("copy-summary");
  if (copy) {
    copy.onclick = () => {
      const text = `${report.domain} readiness: ${total}/100; ${model.passed} passed, ${model.failed} missing across ${model.passed + model.failed + model.uncertain} evaluated checks.`;
      if (navigator.clipboard?.writeText) navigator.clipboard.writeText(text).then(() => { copy.textContent = "✓ Copied"; }).catch(() => { copy.textContent = "Copy unavailable"; });
    };
    if (copy.textContent !== "Copy summary ↗") copy.textContent = "Copy summary ↗";
  }
  renderScore(report.score, "");
  renderReportExperience(report);
  renderReadinessFindings(report);
  if (report.marketing) renderMarketing(report.marketing);
  renderIntelligence(null);
  if (state.jobId) pollIntelligence(state.jobId, state.intelligenceDepth);
  setView("report");
}

function renderIntelligence(job) {
  const panel = $("intelligence-panel");
  panel.hidden = false;
  $("intelligence-details").replaceChildren(); $("intelligence-sources").replaceChildren();
  const status = job?.status || "queued";
  const localModel = state.config?.intelligence_provider === "ollama";
  const preliminary = job?.report?.analysis_stage === "preliminary";
  $("intelligence-status").textContent = preliminary && ["queued", "running"].includes(status)
    ? "Quick page summary · detailed analysis continues"
    : ({queued:"Queued",running:"Reading the public site…",completed:"Based on public evidence",unavailable:"AI analysis is unavailable",failed:"Analysis could not finish",partial:"Partial analysis · quick page summary available",interrupted:"Analysis was interrupted · quick page summary available"})[status] || "Preparing analysis";
  $("deep-analysis").textContent = localModel ? "Analyze this page in more detail ↗" : "Research deeper ↗";
  $("deep-analysis").disabled = ["queued", "running"].includes(status)
    || (status === "unavailable" && /not configured/i.test(String(job?.error || "")));
  if (!job?.report) {
    if (status === "unavailable") {
      const notConfigured = /not configured/i.test(String(job?.error || ""));
      $("intelligence-summary").textContent = notConfigured
        ? "Product analysis is not enabled on this server. The readiness scores and marketing checks above are unaffected."
        : "Product analysis unavailable. Pickable was unable to extract enough reliable information from this website to identify its core offering and intended audience.";
      for (const [title, value] of [["Product positioning", "What the product does and which problem it aims to solve."], ["Target audience", "Which individuals, roles, or businesses the product appears to serve."], ["Value proposition", "What benefits the product communicates and how it differentiates itself."], ["Use cases", "Which real-world needs and workflows the product is designed to address."]]) {
        const item = element("article", "intelligence-fact"); item.append(element("h3", "", title), element("p", "", value)); $("intelligence-details").append(item);
      }
      $("intelligence-sources").append(element("p", "intelligence-unknown", "Action: retry website analysis. No marketing weaknesses or opportunities have been confirmed for this website yet. Findings based on indirect evidence should be treated as hypotheses, not verified facts."));
    } else $("intelligence-summary").textContent = status === "failed" ? "The readiness report is available; product analysis did not complete." : status === "interrupted" ? "The readiness report is available; product analysis was interrupted. Reload to resume." : "We are identifying the product, its audience, and the value described on your URL.";
    if (job?.error) $("intelligence-sources").append(element("p", "intelligence-unknown", String(job.error).slice(0, 300)));
    return;
  }
  const report = job.report;
  $("intelligence-summary").textContent = report.product_summary;
  for (const [title, value] of [["Who it serves", report.target_customers], ["Problem addressed", report.problem], ["Value offered", report.value_proposition]]) {
    const item = element("article", "intelligence-fact"); item.append(element("h3", "", title), element("p", "", value)); $("intelligence-details").append(item);
  }
  const competition = element("article", "intelligence-fact"); competition.append(element("h3", "", "Competitive landscape"), element("p", "", report.competitive_landscape || "Not assessed.")); $("intelligence-details").append(competition);
  for (const workflow of Array.isArray(report.workflows) ? report.workflows : []) $("intelligence-details").append(element("p", "intelligence-workflow", workflow));
  for (const unknown of Array.isArray(report.unknowns) ? report.unknowns : []) $("intelligence-details").append(element("p", "intelligence-unknown", "Needs verification: " + unknown));
  for (const source of Array.isArray(report.sources) ? report.sources : []) {
    if (typeof source?.url !== "string" || !/^https?:\/\//i.test(source.url)) continue;
    const link = element("a", "intelligence-source", source.label || "Public homepage"); link.href = source.url; link.target = "_blank"; link.rel = "noopener noreferrer"; $("intelligence-sources").append(link);
  }
  for (const source of Array.isArray(report.web_sources) ? report.web_sources : []) {
    if (typeof source?.url !== "string" || !/^https:\/\//i.test(source.url)) continue;
    const link = element("a", "intelligence-source", source.label || "Research source"); link.href = source.url; link.target = "_blank"; link.rel = "noopener noreferrer"; $("intelligence-sources").append(link);
  }
  if (report.research_note) $("intelligence-sources").append(element("p", "intelligence-unknown", report.research_note));
  if (job.error && ["partial", "failed", "interrupted"].includes(job.status)) $("intelligence-sources").append(element("p", "intelligence-unknown", "Note: " + String(job.error).slice(0, 300)));
  $("deep-analysis").textContent = job.depth === "deep"
    ? (localModel ? "Detailed page analysis complete" : "Deep research requested ↗")
    : (localModel ? "Analyze this page in more detail ↗" : "Research deeper ↗");
  $("deep-analysis").disabled = job.depth === "deep" || ["queued", "running"].includes(status);
}

async function pollIntelligence(scanId, depth = "short") {
  if (state.intelligenceDepth !== depth) state.intelligenceMissingPolls = 0;
  clearTimeout(state.intelligenceTimer); state.intelligenceDepth = depth;
  try {
    const data = await api(`/api/scans/${encodeURIComponent(scanId)}/intelligence${depth === "deep" ? "/deep" : ""}`);
    if (state.jobId !== scanId || state.view !== "report") return;
    const job = data.intelligence; renderIntelligence(job);
    if (job && ["queued", "running"].includes(job.status)) state.intelligenceTimer = setTimeout(() => pollIntelligence(scanId, depth), 1800);
    else if (!job && state.intelligenceMissingPolls++ < 6) state.intelligenceTimer = setTimeout(() => pollIntelligence(scanId, depth), 1200);
    else if (!job) renderIntelligence({status:"failed"});
  } catch {
    // Transient disconnect (laptop sleep, backend wake, proxy hiccup):
    // keep the readiness report and retry polling instead of going empty.
    if (state.jobId !== scanId || state.view !== "report") return;
    if (state.intelligenceMissingPolls++ < 8) state.intelligenceTimer = setTimeout(() => pollIntelligence(scanId, depth), 2500);
  }
}

async function requestDeepAnalysis() {
  if (!state.jobId) return;
  $("deep-analysis").disabled = true;
  try {
    const result = await api(`/api/scans/${encodeURIComponent(state.jobId)}/intelligence`, { method: "POST", body: "{}" });
    state.intelligenceDepth = "deep";
    notify(state.config?.intelligence_provider === "ollama"
      ? "Detailed page analysis started. This local run does not search the web."
      : "Deep research started. We will update this section when it is ready.");
    pollIntelligence(state.jobId, "deep");
  } catch (error) { $("deep-analysis").disabled = false; notify(error.message); }
}

async function loadScan(id, generation) {
  if (generation !== state.generation) return;
  const result = await api(`/api/scans/${encodeURIComponent(id)}`);
  if (generation !== state.generation) return;
  state.jobId = id;
  const status = result.status || result.job?.status;
  updateSteps(result.sources);
  if (["pending", "queued", "running"].includes(status)) {
    const key = JSON.stringify(result.sources || []);
    if (result.report && key !== state.renderedKey) { state.renderedKey = key; renderReport(result.report); }
    else if (!result.report) setView("loading");
    $("loading-domain").textContent = scalar(result.domain || result.job?.domain, $("domain").value);
    busy(true); notify(result.report ? "" : "Sources are running. This can take a minute; results update as they arrive.");
    setTimeout(() => loadScan(id, generation).catch((error) => { if (generation === state.generation) { busy(false); setView("landing"); notify(error.message + " You can reopen the saved scan from history."); } }), 1800);
  } else {
    if (result.report) renderReport(result.report); else setView("landing");
    busy(false);
    const sources = result.report?.sources || result.sources || [];
    const failed = Array.isArray(sources) && sources.filter((s) => s.status !== "completed");
    notify(["failed", "interrupted"].includes(status) ? "The scan could not finish. Start a new scan to try again." : failed?.length ? "Some checks could not finish. Recommendations use the information available." : "");
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
        clearReport(); busy(true); $("history-dialog").close(); setView("loading"); $("loading-domain").textContent = job.domain; resetPreview(job.domain); loadPreview(job.id || job.job_id, generation); notify("Loading saved scan…");
        loadScan(job.id || job.job_id, generation).catch((error) => { if (generation === state.generation) { busy(false); setView("landing"); notify(error.message); } });
      });
      $("history").append(button);
    }
    if (!scans.length) $("history").append(element("p", "", "No scans yet."));
  } catch (error) { $("history").replaceChildren(element("p", "", "Could not load history. Check the backend connection.")); }
}

$("scan-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const generation = ++state.generation; busy(true); notify("");
  const url = $("domain").value.trim();
  clearReport(); setView("loading"); $("loading-domain").textContent = url; resetPreview(url);
  try {
    if (!state.config) throw new Error("Backend is not ready. Reload the page or start the backend.");
    const result = await api("/api/scans", { method: "POST", body: JSON.stringify({ url }) });
    if (generation !== state.generation) return;
    const id = result.job_id || result.id;
    if (!id) throw new Error("Backend did not return a scan id.");
    loadPreview(id, generation);
    await loadScan(id, generation);
  } catch (error) { if (generation === state.generation) { busy(false); setView("landing"); notify(error.message); } }
});
$("refresh-history").addEventListener("click", refreshHistory);
$("new-analysis").addEventListener("click", returnToLanding);
$("new-analysis-top").addEventListener("click", returnToLanding);
$("deep-analysis").addEventListener("click", requestDeepAnalysis);
$("cancel-analysis").addEventListener("click", returnToLanding);
$("open-history").addEventListener("click", () => { refreshHistory(); $("history-dialog").showModal(); });
$("close-history").addEventListener("click", () => $("history-dialog").close());
for (const button of document.querySelectorAll("[data-mode-button]")) button.addEventListener("click", () => setMode(button.dataset.modeButton));

/* Ambient discovery-field animation: restrained premium motion.
   - 10 drifting nodes on slow organic (Lissajous) paths + glow pulsation.
   - Connections rebuilt from live node positions each frame (shared model).
   - 2 light pulses lerped along live edges (no getPointAtLength jumps).
   - Gentle pointer parallax (lerped) on background layers only; hero
     text and form are never moved. Static SVG remains as no-JS fallback.
   - Single rAF loop, transform/opacity only, pauses when tab hidden or
     hero off-screen, skips entirely on reduced-motion or small screens
     (where the field is hidden by CSS). */
(function initAmbientField() {
  try {
    if (typeof document === "undefined" || typeof document.querySelector !== "function") return;
    if (typeof window === "undefined" || typeof window.requestAnimationFrame !== "function") return;
    if (typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    var field = document.querySelector(".discovery-field");
    var svg = field ? field.querySelector(".field-links") : null;
    var orbit = document.querySelector(".hero-orbit");
    var landing = document.querySelector(".landing") || document.body;
    if (!field || !svg || typeof svg.querySelectorAll !== "function") return;
    if (typeof window.matchMedia === "function" && window.matchMedia("(max-width: 900px)").matches) return;
    if (typeof svg.getBoundingClientRect === "function" && field.offsetParent === null && window.getComputedStyle(field).display === "none") return;

    var circles = Array.prototype.slice.call(svg.querySelectorAll("circle"));
    var link = svg.querySelector("path");
    if (!circles.length || !link) return;

    // Shared animation model: every node drifts independently, edges are
    // rebuilt from live node positions each frame so lines never detach.
    // Edges mirror the static path: 0-1, 1-2, 3-4, 4-5, 6-7, 8-9.
    var edges = [[0, 1], [1, 2], [3, 4], [4, 5], [6, 7], [8, 9]].filter(function (e) {
      return e[0] < circles.length && e[1] < circles.length;
    });
    var nodes = circles.map(function (el, k) {
      el.classList.add("drift-node");
      return {
        el: el,
        bx: parseFloat(el.getAttribute("cx")) || 0,
        by: parseFloat(el.getAttribute("cy")) || 0,
        base: parseFloat(el.getAttribute("opacity")) || el.opacity || 0.64,
        x: parseFloat(el.getAttribute("cx")) || 0,
        y: parseFloat(el.getAttribute("cy")) || 0,
        ax: 10 + (k % 4) * 4, ay: 8 + ((k + 1) % 4) * 3,
        w1: (2 * Math.PI) / (9000 + k * 1800), w2: (2 * Math.PI) / (11000 + k * 1500),
        p1: k * 1.7, p2: k * 2.3,
        wo: (2 * Math.PI) / (9000 + k * 2100), po: k * 1.1,
        rx: 0, ry: 0,
      };
    });

    // Traveling light pulses: sparse, one per edge-group, lerped in
    // model space so they stay exactly on the live connection.
    var pulses = [];
    if (typeof document.createElementNS === "function") {
      var NS = "http://www.w3.org/2000/svg";
      var defs = [
        { dur: 9000, phase: 0, edge: 1 },
        { dur: 13000, phase: 0.45, edge: 3 },
      ];
      for (var d = 0; d < defs.length; d++) {
        if (defs[d].edge >= edges.length) continue;
        var dot = document.createElementNS(NS, "circle");
        dot.setAttribute("class", "pulse-dot");
        dot.setAttribute("r", "3");
        svg.appendChild(dot);
        pulses.push({ el: dot, dur: defs[d].dur, phase: defs[d].phase, edge: defs[d].edge });
      }
    }

    // Pointer parallax targets (background layers only).
    var tx = 0, ty = 0, cx = 0, cy = 0, px = 0, py = 0, hasPointer = false;
    var pointerSvgX = 0, pointerSvgY = 0;
    function svgPoint(clientX, clientY) {
      try {
        var r = svg.getBoundingClientRect();
        if (!r.width || !r.height) return null;
        return { x: (clientX - r.left) / r.width * 1200, y: (clientY - r.top) / r.height * 560 };
      } catch (e) { return null; }
    }
    if (typeof window.addEventListener === "function") {
      window.addEventListener("pointermove", function (e) {
        if (e.pointerType === "touch") return;
        var nx = (e.clientX / window.innerWidth) * 2 - 1;
        var ny = (e.clientY / window.innerHeight) * 2 - 1;
        tx = Math.max(-1, Math.min(1, nx)) * 10;
        ty = Math.max(-1, Math.min(1, ny)) * 8;
        px = -tx * 1.6; py = -ty * 1.6;
        var p = svgPoint(e.clientX, e.clientY);
        if (p) { pointerSvgX = p.x; pointerSvgY = p.y; hasPointer = true; }
      }, { passive: true });
      window.addEventListener("pointerleave", function () { hasPointer = false; tx = ty = px = py = 0; });
    }

    var running = true;
    var rafId = 0;
    function frame(now) {
      if (!running) return;
      rafId = window.requestAnimationFrame(frame);
      if (document.hidden) return;
      var t = now || 0;
      // Lerped parallax so the background never snaps.
      cx += (tx - cx) * 0.045; cy += (ty - cy) * 0.045;
      var ox = px, oy = py;
      if (orbit) {
        var ocx = parseFloat(orbit.dataset.ox || "0"), ocy = parseFloat(orbit.dataset.oy || "0");
        ocx += (ox - ocx) * 0.035; ocy += (oy - ocy) * 0.035;
        orbit.dataset.ox = String(ocx); orbit.dataset.oy = String(ocy);
        orbit.style.setProperty("--opx", ocx.toFixed(2) + "px");
        orbit.style.setProperty("--opy", ocy.toFixed(2) + "px");
      }
      field.style.setProperty("--px", cx.toFixed(2) + "px");
      field.style.setProperty("--py", cy.toFixed(2) + "px");

      for (var i = 0; i < nodes.length; i++) {
        var n = nodes[i];
        var dx = n.ax * Math.sin(n.w1 * t + n.p1);
        var dy = n.ay * Math.sin(n.w2 * t + n.p2);
        // Gentle cursor repulsion (<=7px), smoothly eased per node.
        var gx = 0, gy = 0;
        if (hasPointer) {
          var vx = (n.bx + dx) - pointerSvgX, vy = (n.by + dy) - pointerSvgY;
          var dist = Math.sqrt(vx * vx + vy * vy);
          if (dist > 1 && dist < 150) {
            var f = (1 - dist / 150) * 7;
            gx = (vx / dist) * f; gy = (vy / dist) * f;
          }
        }
        n.rx += (gx - n.rx) * 0.06; n.ry += (gy - n.ry) * 0.06;
        n.x = n.bx + dx + n.rx; n.y = n.by + dy + n.ry;
        n.el.setAttribute("transform", "translate(" + (dx + n.rx).toFixed(2) + " " + (dy + n.ry).toFixed(2) + ")");
        var glow = 0.5 + 0.5 * Math.sin(n.wo * t + n.po);
        n.el.setAttribute("opacity", (n.base * (0.55 + 0.45 * glow)).toFixed(3));
      }

      // Rebuild connections from live positions: zero deviation by construction.
      try {
        var parts = [];
        for (var e = 0; e < edges.length; e++) {
          var a = nodes[edges[e][0]], b = nodes[edges[e][1]];
          parts.push("M" + a.x.toFixed(1) + " " + a.y.toFixed(1) + " L" + b.x.toFixed(1) + " " + b.y.toFixed(1));
        }
        link.setAttribute("d", parts.join(" "));
      } catch (e) { /* static fallback stays */ }

      for (var j = 0; j < pulses.length; j++) {
        var pu = pulses[j];
        var prog = ((t / pu.dur) + pu.phase) % 1;
        var pa = nodes[edges[pu.edge][0]], pb = nodes[edges[pu.edge][1]];
        pu.el.setAttribute("cx", (pa.x + (pb.x - pa.x) * prog).toFixed(1));
        pu.el.setAttribute("cy", (pa.y + (pb.y - pa.y) * prog).toFixed(1));
        var env = Math.sin(prog * Math.PI); // fade in/out at path ends
        pu.el.setAttribute("opacity", (env * 0.9).toFixed(3));
      }
    }

    if ("IntersectionObserver" in window && landing) {
      try {
        new IntersectionObserver(function (entries) {
          var visible = entries[0] && entries[0].isIntersecting;
          if (visible && !running) { running = true; rafId = window.requestAnimationFrame(frame); }
          else if (!visible && running) { running = false; window.cancelAnimationFrame(rafId); }
        }, { threshold: 0 }).observe(landing);
      } catch (e) { /* run unconditionally */ }
    }
    if (typeof document.addEventListener === "function") {
      document.addEventListener("visibilitychange", function () {
        if (!document.hidden && running && !rafId) rafId = window.requestAnimationFrame(frame);
      });
    }
    if (typeof window.addEventListener === "function") {
      window.addEventListener("pagehide", function () { running = false; window.cancelAnimationFrame(rafId); });
    }
    rafId = window.requestAnimationFrame(frame);
  } catch (e) { /* ambient animation is decorative; never break the scan flow */ }
})();

async function initialize() {
  let saved = null; try { saved = localStorage.getItem("pickable-mode"); } catch {}
  setMode(saved || "normal");
  setView("landing");
  try {
    state.config = await api("/api/config"); state.token = state.config.csrf_token;
    $("demo-badge").hidden = state.config.mode !== "demo";
  } catch (error) { notify(error.message); }
  await refreshHistory();
}
initialize();
