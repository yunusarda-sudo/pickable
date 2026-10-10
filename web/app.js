"use strict";
const $ = (id) => document.getElementById(id);
const state = { report: null, jobId: null, token: null, generation: 0, config: null };
const nativeLabels = { PASS: "Geçti", FAIL: "Eksik", WARNING: "Uyarı", UNKNOWN: "Belirsiz", NOT_APPLICABLE: "Uygulanmaz", DIFFERENT: "Farklı" };
const matchLabels = { equivalent: "Benzer ölçüt", partial: "Kısmi eşleşme", related: "İlişkili", not_comparable: "Karşılaştırılamıyor" };
const sourceLabels = { completed: "Tamamlandı", partial: "Kısmi", running: "Çalışıyor", pending: "Bekliyor", unavailable: "Erişilemedi", failed: "Hata", timeout: "Zaman aşımı", interrupted: "Kesildi" };

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function notify(message) { $("notice").textContent = message; $("notice").hidden = !message; }
function busy(on) { $("scan-button").disabled = on; $("scan-button").ariaLabel = on ? "Analiz sürüyor" : "Analizi başlat"; }
function scalar(value, fallback = "") { return typeof value === "string" || (typeof value === "number" && Number.isFinite(value)) ? String(value) : fallback; }
function display(value, fallback = "") { if (value === null || value === undefined) return fallback; return scalar(value) || JSON.stringify(value) || fallback; }
function badge(status) { const key = typeof status === "string" && Object.hasOwn(nativeLabels, status) ? status : "UNKNOWN"; return element("span", `pill ${key}`, nativeLabels[key]); }
function formatTime(value) { if (typeof value !== "string" || !value.trim()) return "Zaman bilgisi yok"; const date = new Date(value); return Number.isNaN(date.getTime()) ? "Zaman bilgisi yok" : date.toLocaleString("tr-TR"); }

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
  state.report = null; state.jobId = null;
  $("results").hidden = true; $("empty-state").hidden = false;
  for (const id of ["download-report", "ora-raw", "oss-raw"]) $(id).removeAttribute("href");
  $("evidence-dialog").close();
  updateSources();
}

async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...options.headers };
  if (options.body) { headers["Content-Type"] = "application/json"; if (state.token) headers["X-CSRF-Token"] = state.token; }
  const response = await fetch(path, { ...options, headers, signal: AbortSignal.timeout(20000) });
  const type = response.headers.get("content-type") || "";
  if (!type.includes("application/json")) throw new Error("Backend bağlantısı hazır değil. API JSON yanıtı bekleniyor.");
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.error === "string" ? data.error : `İstek tamamlanamadı (${response.status}).`);
  return data;
}

function updateSources(sources = []) {
  if (!Array.isArray(sources)) sources = sources && typeof sources === "object" ? Object.entries(sources).map(([id, value]) => ({ ...value, id })) : [];
  for (const id of ["ora", "agentready_oss"]) {
    const source = sources.find((s) => s && (s.id || s.source_id) === id);
    const status = scalar(source?.status, "pending");
    $(id + "-state").textContent = Object.hasOwn(sourceLabels, status) ? sourceLabels[status] : status;
    $(id + "-state").className = "source-state " + (status === "completed" ? "completed" : ["pending", "running"].includes(status) ? "running" : "unavailable");
    const parts = [];
    if (scalar(source?.score)) parts.push(`Kaynak skoru: ${scalar(source.score)}/100${scalar(source.grade) ? " · " + scalar(source.grade) : ""}`);
    if (scalar(source?.level)) parts.push(`Kaynak seviyesi: ${scalar(source.level)}/5`);
    if (scalar(source?.check_count)) parts.push(`${scalar(source.check_count)} kontrol`);
    if (scalar(source?.error)) parts.push(scalar(source.error));
    if (source?.observed_at) parts.push(formatTime(source.observed_at));
    $(id + "-detail").textContent = parts.join(" · ") || (status === "running" ? "Kaynak raporu bekleniyor…" : "Henüz sonuç yok");
  }
}

function openEvidence(check) {
  $("evidence-title").textContent = check.title;
  $("evidence-reason").textContent = `${check.measurement_definition} ${check.reason}`;
  for (const [id, target] of [["ora", "ora-evidence"], ["agentready_oss", "oss-evidence"]]) {
    const observation = check.sources[id];
    $(target).textContent = observation?.raw ? JSON.stringify({ source_check_id: observation.source_check_id, observed_at: observation.observed_at, raw: observation.raw }, null, 2) : "Bu kaynaktan kontrol sonucu alınamadı.";
  }
  $("evidence-dialog").showModal();
}

function renderChecks() {
  const tbody = $("checks"); tbody.replaceChildren();
  const filter = $("filter").value;
  const checks = (state.report?.checks || []).filter((check) => {
    if (filter === "all") return true;
    if (filter === "different") return new Set(Object.values(check.sources).map((s) => s.status).filter((s) => ["PASS", "FAIL"].includes(s))).size === 2;
    if (filter === "pass") return check.combined_status === "PASS";
    return check.combined_status === "UNKNOWN";
  });
  for (const check of checks) {
    const tr = element("tr");
    const title = element("td"); const button = element("button", "check-button", check.title);
    button.addEventListener("click", () => openEvidence(check));
    title.append(button, element("small", "", matchLabels[check.match_type] || check.match_type));
    tr.append(title);
    for (const source of ["ora", "agentready_oss"]) {
      const td = element("td"); const observation = check.sources[source];
      td.append(badge(observation?.status || "UNKNOWN"));
      if (observation?.native_status !== null && observation?.native_status !== undefined) td.append(element("small", "", `Kaynak: ${display(observation.native_status)}`));
      tr.append(td);
    }
    const score = element("td"); score.append(badge(check.scoring_status));
    score.append(element("small", "", check.scoring_source === "agentready_oss" ? "Python ölçütü" : check.scoring_source === "ora" ? "Ora ölçütü" : "Puana dahil değil"));
    tr.append(score, element("td", "", check.reason)); tbody.append(tr);
  }
  if (!checks.length) { const td = element("td", "", "Bu filtreye uygun kontrol yok."); td.colSpan = 5; const tr = element("tr"); tr.append(td); tbody.append(tr); }
}

const categoryCopy = {
  discovery: ["Keşfedilebilirlik", "Sitenin giriş noktaları bulunabiliyor mu?", "01"],
  content: ["Anlaşılabilirlik", "İçeriğin makine tarafından okunmaya uygun mu?", "02"],
  usability: ["Kullanılabilirlik", "Entegrasyon ve kimlik doğrulama sinyalleri var mı?", "03"],
  security: ["Güvenlik & uyum", "Bu sürümde henüz ölçülmüyor.", "04"],
  payments: ["Ödeme", "Bu sürümde henüz ölçülmüyor.", "05"],
};
const checkGuidance = {
  "discovery.sitemap": ["Sitemap, sitendeki sayfaların keşfedilmesine yardımcı olur.", "Geçerli bir XML sitemap yayımla ve robots.txt içinde konumunu belirt."],
  "discovery.link_headers": ["Link başlıkları API, dokümantasyon ve diğer makine giriş noktalarını duyurur.", "Kullandığın makine giriş noktalarını uygun HTTP Link ilişkileriyle belirt."],
  "content.llms_txt": ["llms.txt, sitendeki önemli içerik için kısa bir makine rehberi sunar.", "Kök /llms.txt dosyasını, H1 başlığı ve yararlı içerik bağlantılarıyla kontrol et."],
  "content.markdown": ["Markdown sunumu, içeriğin daha sade bir biçimde alınmasını sağlar.", "Markdown yanıtlarını, içerik türünü ve Accept/Vary davranışını birlikte kontrol et."],
  "auth.oauth_metadata": ["OAuth metadata, korumalı API'lere nasıl erişileceğini tarif eder.", "Korumalı bir API sunuyorsan kök OAuth/OIDC keşif metadata'sını kontrol et. Yalnız herkese açık içerik için gerekmeyebilir."],
  "auth.protected_resource": ["Protected Resource metadata, kaynağın kullandığı yetkilendirme sunucularını duyurur.", "Korumalı kaynakların varsa kaynak kimliğini ve yetkilendirme sunucularını metadata'da belirt."],
  "integration.mcp_card": ["MCP server card, sunduğun MCP sunucusunun keşfine yardımcı olur.", "MCP sunucun varsa keşif kartını ve duyurulan endpoint bilgilerini kontrol et."],
};
function modelReport(report) {
  const checks = Array.isArray(report?.checks) ? report.checks.filter(c => c && typeof c === "object") : [];
  return {
    passed: checks.filter(c => c.scoring_status === "PASS").length,
    failed: checks.filter(c => c.scoring_status === "FAIL").length,
    uncertain: checks.filter(c => !["PASS", "FAIL"].includes(c.scoring_status)).length,
    different: checks.filter(c => new Set(Object.values(c.sources || {}).map(s => s?.status).filter(s => ["PASS", "FAIL"].includes(s))).size === 2).length,
    actions: checks.filter(c => c.scoring_status === "FAIL"),
  };
}
function reportSummary(report) {
  const model = modelReport(report);
  if (report?.score?.total === null || report?.score?.total === undefined) return "Ön skor için yeterli kesin ölçüm yok. Kaynak ayrıntılarını inceleyebilirsin.";
  return `${model.passed} kontrol geçti, ${model.failed} kontrol eksik bildirildi. ${model.uncertain} kontrol kesin PASS/FAIL olarak puanlanmadı. Bulgular kaynak raporlarına dayanır.`;
}
function evidenceButton(check, label = "Kaynak kanıtını incele ↗") {
  const button = element("button", "evidence-link", label);
  button.addEventListener("click", () => openEvidence(check));
  return button;
}
function renderReportExperience(report) {
  const model = modelReport(report);
  $("report-headline").textContent = report.score.total === null ? "Daha fazla kanıta ihtiyaç var." : model.failed ? "Geliştirilecek alanlar var." : model.uncertain ? "İyi sinyaller, eksik bir resim." : "Ölçülen kontroller olumlu.";
  $("report-summary").textContent = reportSummary(report);
  $("report-counts").replaceChildren();
  for (const [count, label, tone] of [[model.passed,"Geçti","positive"],[model.failed,"Eksik","negative"],[model.uncertain,"Puan dışı","neutral"],[model.different,"Kaynak farkı","amber"]]) {
    const item = element("div", "count-item " + tone);
    item.append(element("strong", "", count), element("span", "", label)); $("report-counts").append(item);
  }
  $("action-count").textContent = `${model.actions.length} kontrol`;
  $("action-list").replaceChildren();
  for (const [index, check] of model.actions.entries()) {
    const card = element("article", "action-card");
    card.append(element("span", "action-number", String(index + 1).padStart(2,"0")), element("h3", "", check.title));
    card.append(element("p", "", checkGuidance[check.check_id]?.[1] || "Kaynağın bildirdiği eksikliği kanıt ayrıntılarıyla birlikte incele."));
    card.append(element("small", "action-basis", check.scoring_source === "ora" ? "Ora ölçütüne göre · Pickable önerisi" : "Python ölçütüne göre · Pickable önerisi"), evidenceButton(check));
    $("action-list").append(card);
  }
  if (!model.actions.length) $("action-list").append(element("p", "report-empty", model.uncertain ? "Kesin eksik sonucu yok. Puan dışı kontrolleri inceleyerek ölçüm boşluklarını görebilirsin." : "Ölçülen kontrollerden bir eksik listesi çıkmadı."));
  $("category-findings").replaceChildren();
  for (const [key, copy] of Object.entries(categoryCopy)) {
    const checks = (Array.isArray(report.checks) ? report.checks : []).filter(c => c?.category === key);
    const section = element("section", "finding-category");
    const heading = element("div", "category-heading");
    const titles = element("div"); titles.append(element("span", "category-number", copy[2]), element("h3", "", copy[0]), element("p", "", copy[1]));
    const valid = checks.filter(c => ["PASS","FAIL"].includes(c.scoring_status));
    heading.append(titles, element("span", "category-stat", valid.length ? `${valid.filter(c => c.scoring_status === "PASS").length}/${valid.length} kesin ölçüm geçti · ${checks.length - valid.length} puan dışı` : checks.length ? "Puanlanabilir sonuç yok" : "Ölçüm yok")); section.append(heading);
    for (const check of checks) {
      const row = element("details", "finding-row");
      const summary = element("summary");
      summary.append(badge(check.scoring_status), element("span", "finding-title", check.title), element("span", "finding-arrow", "+")); row.append(summary);
      const body = element("div", "finding-body");
      body.append(element("p", "finding-purpose", checkGuidance[check.check_id]?.[0] || check.measurement_definition));
      const observations = element("div", "observation-grid");
      for (const [id, label] of [["ora","Ora"],["agentready_oss","AgentReady"]]) {
        const observation = check.sources?.[id]; const raw = observation?.raw || {};
        const block = element("div", "observation-card");
        const head = element("div", "observation-heading"); head.append(element("strong", "", label),badge(observation?.status));
        block.append(head,element("p", "", scalar(raw.message) || scalar(raw.details) || "Açıklama için ham kanıtı incele.")); observations.append(block);
      }
      body.append(observations,element("p", "finding-context", check.reason),evidenceButton(check));
      row.append(body); section.append(row);
    }
    if (!checks.length) section.append(element("p", "unmeasured-note", "Bu katman bu sürümün tarama kapsamına dahil değil; puan kazandırmaz."));
    $("category-findings").append(section);
  }
}

function renderReport(report) {
  state.report = report;
  $("empty-state").hidden = true; $("results").hidden = false;
  $("report-domain").textContent = report.domain;
  $("report-time").textContent = `${formatTime(report.scanned_at)} · Sözlük ${report.mapping_version || "0.1"}${state.config?.mode === "demo" ? " · TEST VERİSİ" : ""}`;
  $("score-total").textContent = report.score.total ?? "—";
  $("score-coverage").textContent = `%${report.score.coverage} ağırlıklı ölçüm kapsamı${report.score.coverage < 100 ? " · eksik kapsam" : ""}`;
  $("layers").replaceChildren();
  for (const layer of Object.values(report.score.breakdown)) {
    const row = element("div", "layer"); const track = element("div", "layer-track"); const fill = element("div", "layer-fill");
    fill.style.width = `${layer.points === null ? 0 : Math.min(100, Math.max(0, layer.points / layer.max_points * 100))}%`; track.append(fill);
    row.append(element("span", "", layer.label), track, element("span", "layer-value", layer.points === null ? "Ölçülmedi" : `${layer.points}/${layer.max_points}`));
    row.title = `${layer.measured}/${layer.defined} kontrol ölçüldü`; $("layers").append(row);
  }
  updateSources(report.sources); renderChecks();
  const specific = report.source_specific || [];
  $("specific-title").textContent = `Kaynağa özel bulgular (${specific.length})`;
  $("specific-list").replaceChildren();
  for (const item of specific) {
    const row = element("details", "specific-row"); const raw = item.raw || {};
    row.append(element("summary", "", `${item.source === "ora" ? "Ora" : "AgentReady"} · ${display(raw.name) || display(raw.title) || scalar(item.source_check_id)} · ${display(raw.status, "unknown")}`), element("pre", "", JSON.stringify(raw, null, 2)));
    $("specific-list").append(row);
  }
  $("recommendations").replaceChildren();
  const recommendations = Array.isArray(report.recommendations) ? report.recommendations : [];
  for (const fix of recommendations) $("recommendations").append(element("pre", "", typeof fix === "string" ? fix : JSON.stringify(fix, null, 2)));
  if (!recommendations.length) $("recommendations").append(element("p", "", "Ora bu raporda öneri listesi sunmadı. Kontrol bazlı açıklamalar kanıt penceresinde bulunur."));
  const root = `/api/scans/${encodeURIComponent(state.jobId)}`;
  $("download-report").href = root + "/report"; $("ora-raw").href = root + "/raw/ora"; $("oss-raw").href = root + "/raw/agentready_oss";
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
    setView("loading");
    $("loading-domain").textContent = scalar(result.domain || result.job?.domain, $("domain").value);
    busy(true); notify("Kaynaklar çalışıyor. Bu işlem birkaç dakika sürebilir; sonuçlar geldikçe güncellenir.");
    setTimeout(() => loadScan(id, generation).catch((error) => { if (generation === state.generation) { busy(false); setView("landing"); notify(error.message + " Kaydedilen analizi geçmişten tekrar açabilirsiniz."); } }), 1800);
  } else {
    if (result.report) renderReport(result.report); else setView("landing");
    busy(false);
    const sources = result.report?.sources || result.sources || [];
    const failed = Array.isArray(sources) && sources.filter((s) => s.status !== "completed");
    notify(["failed", "interrupted"].includes(status) ? "Analiz tamamlanamadı. Alınabilen kaynak raporları korunur." : failed?.length ? "Bazı kaynaklar tamamlanamadı. Mevcut kanıt gösteriliyor; eksikler başarısızlık sayılmadı." : "");
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
        clearReport(); busy(true); $("history-dialog").close(); setView("loading"); $("loading-domain").textContent = job.domain; notify("Kaydedilen analiz yükleniyor…");
        loadScan(job.id || job.job_id, generation).catch((error) => { if (generation === state.generation) { busy(false); setView("landing"); notify(error.message); } });
      });
      $("history").append(button);
    }
    if (!scans.length) $("history").append(element("p", "", "Henüz analiz yok."));
  } catch (error) { $("history").replaceChildren(element("p", "", "Geçmiş yüklenemedi. Backend bağlantısını kontrol edin.")); }
}

$("scan-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const generation = ++state.generation; busy(true); notify("");
  clearReport(); setView("loading"); $("loading-domain").textContent = $("domain").value.trim();
  try {
    if (!state.config) throw new Error("Backend bağlantısı hazır değil. Sayfayı yenileyin veya backend'i başlatın.");
    const result = await api("/api/scans", { method: "POST", body: JSON.stringify({ domain: $("domain").value.trim() }) });
    if (generation !== state.generation) return;
    const id = result.job_id || result.id;
    if (!id) throw new Error("Backend analiz kimliği döndürmedi.");
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
