const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function setup(fetchData) {
  const nodes = new Map();
  function node() {
    return { textContent: '', hidden: false, disabled: false, value: 'all', children: [], style: {}, listeners: {},
      append(...children) { this.children.push(...children); },
      replaceChildren(...children) { this.children = children; },
      addEventListener(type, handler) { this.listeners[type] = handler; },
      removeAttribute(name) { delete this[name]; }, setAttribute(name, value) { this[name] = value; },
      classes: new Set(), get classList() { const c = this.classes; return { add: n => c.add(n), toggle: (n, on) => on ? c.add(n) : c.delete(n) }; },
      get parentElement() { return this.parent || (this.parent = node()); },
      get lastElementChild() { return this.children.at(-1) || this.last || (this.last = node()); }, close() {}, showModal() {}, focus() { this.focusCount = (this.focusCount || 0) + 1; this.focusText = this.textContent; },
    };
  }
  const get = id => { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); };
  const context = vm.createContext({ document: { getElementById: get, createElement: node, querySelectorAll: () => [], body: { dataset: {} } },
    AbortSignal, setTimeout() { return 1; }, clearTimeout() {}, fetch: async url => ({ ok: true, headers: { get: () => 'application/json' }, json: () => fetchData(url) }),
  });
  const code = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8').replace(/initialize\(\);\s*$/, '');
  vm.runInContext(code + '\nglobalThis.ui = {state, refreshHistory, renderReport, renderIntelligence, formatTime, updateSteps, loadScan, clearReport, initialize, modelReport: typeof modelReport === "function" ? modelReport : undefined, reportSummary: typeof reportSummary === "function" ? reportSummary : undefined, setView: typeof setView === "function" ? setView : undefined};', context);
  return { ...context.ui, get };
}
const tick = () => new Promise(resolve => setImmediate(resolve));
function visibleText(node) {
  return [node.textContent || '', ...(node.children || []).map(visibleText)].join(' ');
}
const report = () => ({ domain: 'old.example', scanned_at: '2026-10-10T12:00:00Z', score: {total: 20, coverage: 10, breakdown: {}}, checks: [], sources: [], recommendations: [] });
const jobs = [{id: 'a', domain: 'a.example'}, {id: 'b', domain: 'b.example'}];

test('preliminary intelligence is clearly labeled while enrichment continues', () => {
  const ui = setup(() => ({}));
  ui.renderIntelligence({status:'running', depth:'short', report:{analysis_stage:'preliminary',
    product_summary:'Ön bulgu: TeamFlow — Planning for distributed teams', target_customers:'Not verified',
    problem:'Not verified', value_proposition:'Not verified', competitive_landscape:'Not researched',
    workflows:[], evidence_refs:['homepage'], unknowns:[], sources:[{label:'TeamFlow', url:'https://example.com/'}]}});
  assert.match(ui.get('intelligence-status').textContent, /Quick page summary/i);
  assert.equal(ui.get('intelligence-summary').textContent, 'Ön bulgu: TeamFlow — Planning for distributed teams');
  assert.equal(ui.get('intelligence-sources').children[0].href, 'https://example.com/');
  assert.equal(ui.get('deep-analysis').disabled, true);
});

test('terminal partial intelligence does not claim enrichment is still running', () => {
  const ui = setup(() => ({}));
  ui.renderIntelligence({status:'partial', depth:'short', report:{analysis_stage:'preliminary',
    product_summary:'Page summary', target_customers:'Unknown', problem:'Unknown', value_proposition:'Unknown',
    competitive_landscape:'Not researched', workflows:[], evidence_refs:['homepage'], unknowns:[]}});
  assert.match(ui.get('intelligence-status').textContent, /Partial analysis/);
  assert.doesNotMatch(ui.get('intelligence-status').textContent, /continues/i);
});

test('history selection clears previous evidence and download links before response', async () => {
  let resolveScan;
  const ui = setup(url => url === '/api/scans' ? {scans: jobs} : new Promise(resolve => {resolveScan = resolve;}));
  ui.state.jobId = 'old'; ui.renderReport(report());
  await ui.refreshHistory();
  ui.get('history').children[0].listeners.click();
  assert.equal(ui.state.report, null);
  assert.equal(ui.get('results').hidden, true);
  assert.equal(ui.get('download-report').href, undefined);
  assert.equal(ui.get('scan-button').disabled, true);
  await tick(); resolveScan({status: 'running', sources: [], report: null}); await tick();
  assert.equal(ui.get('results').hidden, true);
});

test('superseded history failure cannot unlock current scan or replace its notice', async () => {
  let rejectA;
  const ui = setup(url => url === '/api/scans' ? {scans: jobs} : url.endsWith('/a') ? new Promise((_, reject) => {rejectA = reject;}) : {status:'running', sources:[], report:null});
  await ui.refreshHistory();
  ui.get('history').children[0].listeners.click(); await tick();
  ui.get('history').children[1].listeners.click(); await tick();
  const notice = ui.get('notice').textContent;
  rejectA(new Error('stale A failure')); await tick();
  assert.equal(ui.get('scan-button').disabled, true);
  assert.equal(ui.get('notice').textContent, notice);
});

test('missing or non-string timestamps never become epoch dates', () => {
  const ui = setup(() => ({}));
  for (const value of [null, undefined, false, 0, [], {}, 'invalid']) assert.equal(ui.formatTime(value), 'No timestamp');
});

test('malformed provider fields and recommendation containers render safely', () => {
  const ui = setup(() => ({}));
  const bad = report();
  bad.sources = [null, {id:'ora', score: {}, grade: [], level: true, check_count: {}, error: {message:'bad'}, observed_at: []}];
  bad.recommendations = {unexpected:'not an array'};
  assert.doesNotThrow(() => ui.renderReport(bad));
  assert.doesNotMatch(ui.get('ora-detail').textContent, /\[object Object\]|true\/5/);
  assert.equal(ui.get('action-list').children.length, 1);
});

test('raw provider evidence never reaches the customer recommendations', () => {
  const ui = setup(() => ({}));
  const bad = report();
  const native = {toString: 0};
  bad.checks = [{title:'Native status', sources:{ora:{status:'UNKNOWN', native_status:native}}, scoring_status:'UNKNOWN', reason:'Test'}];
  bad.source_specific = [{source:'ora', source_check_id:'extra', raw:{name:native, title:native, status:native}}];
  bad.recommendations = [{recommendation:native}];
  const before = JSON.stringify(bad);
  assert.doesNotThrow(() => ui.renderReport(bad));
  assert.equal(ui.get('action-list').children.length, 1);
  assert.doesNotMatch(ui.get('action-list').children[0].textContent, /toString|Native status/);
  assert.equal(JSON.stringify(bad), before);
});

test('initialization presents only the domain landing view before any scan', async () => {
  const ui = setup(url => url === '/api/config' ? {csrf_token:'test-token', mode:'demo'} : {scans:[]});
  await ui.initialize();
  assert.equal(ui.get('landing-view').hidden, false);
  assert.equal(ui.get('loading-view').hidden, true);
  assert.equal(ui.get('report-view').hidden, true);
  assert.equal(ui.state.report, null);
  assert.equal(ui.state.token, 'test-token');
});

test('running scan shows a partial report as soon as one exists and keeps polling', async () => {
  const partial = report();
  const ui = setup(() => ({status:'running', sources:[{id:'cloudflare', status:'completed'}], report:partial}));
  await ui.loadScan('pending', ui.state.generation);
  assert.equal(ui.get('loading-view').hidden, true);
  assert.equal(ui.get('report-view').hidden, false);
  assert.equal(ui.get('results').hidden, false);
  assert.equal(ui.get('scan-button').disabled, true);
  assert.equal(ui.get('notice').textContent, '');
  assert.equal(ui.get('notice').hidden, true);
  assert.equal(ui.get('step-ora').lastElementChild.textContent, '');
});

test('a non-final score is never displayed', () => {
  const ui = setup(() => ({}));
  const value = report();
  value.score = {total: 57.5, coverage: 60, final: false, breakdown: {discovery: {label: 'Discoverability', points: 20, max_points: 20}}};
  ui.renderReport(value);
  assert.equal(ui.get('score-total').children[0].className, 'spinner');
  assert.ok(ui.get('score-total').parentElement.classes.has('pending'));
  assert.ok(ui.get('layers').children[0].children[1].classes.has('loading'));
  assert.equal(ui.get('layers').children[0].children[2].textContent, 'Calculating');
  value.score.final = true;
  ui.renderReport(value);
  assert.equal(String(ui.get('score-total').textContent), '57.5');
  assert.ok(!ui.get('score-total').parentElement.classes.has('pending'));
});

test('marketing section renders its own score, actions and profiles', () => {
  const ui = setup(() => ({}));
  const value = report();
  value.marketing = {score: {total: 40, coverage: 100, final: true, breakdown: {}}, source: {}, checks: [
    {check_id: 'social.profiles', category: 'social', title: 'Social profiles linked', scoring_status: 'PASS', message: 'Found 1', details: {profiles: {X: 'https://x.com/acme'}}},
    {check_id: 'content.blog', category: 'content', title: 'Has a blog', scoring_status: 'FAIL', message: 'No blog'},
  ]};
  ui.renderReport(value);
  assert.equal(String(ui.get('mk-score-total').textContent), '40');
  assert.equal(ui.get('mk-action-list').children.length, 1);
  const mkText = visibleText(ui.get('mk-findings'));
  assert.match(mkText, /X · https:\/\/x\.com\/acme/);
  assert.match(mkText, /What we evaluate:/);
  assert.match(mkText, /Why it matters:/);
  assert.match(mkText, /What a successful result means:/);
});
test('marketing section with no evidence never claims no gaps were found', () => {
  const ui = setup(() => ({}));
  const value = report();
  value.marketing = {score: {total: null, coverage: 0, final: true, breakdown: {}}, source: {}, checks: [
    {check_id: 'social.profiles', category: 'social', title: 'Social profiles linked', scoring_status: 'UNKNOWN', message: '', details: {}},
    {check_id: 'content.blog', category: 'content', title: 'Has a blog', scoring_status: 'UNKNOWN', message: ''},
    {check_id: 'content.blog_recent', category: 'content', title: 'Blog updated recently', scoring_status: 'UNKNOWN', message: ''},
    {check_id: 'content.blog_cadence', category: 'content', title: 'Posts regularly', scoring_status: 'UNKNOWN', message: ''},
  ]};
  ui.renderReport(value);
  assert.equal(String(ui.get('mk-score-total').textContent), '—');
  assert.match(ui.get('mk-score-coverage').textContent, /Insufficient data/);
  assert.match(ui.get('mk-headline').textContent, /couldn't complete/i);
  assert.doesNotMatch(visibleText(ui.get('mk-action-list')), /No gaps found/);
  assert.match(visibleText(ui.get('mk-action-list')), /Recommendations are not available yet/);
  assert.match(visibleText(ui.get('mk-counts')), /Not evaluated/);
  assert.match(visibleText(ui.get('mk-findings')), /Not evaluated/);
});

test('running scan without any source report stays on the loading view', async () => {
  const ui = setup(() => ({status:'running', sources:[{id:'growth', status:'completed'}], report:null}));
  await ui.loadScan('pending', ui.state.generation);
  assert.equal(ui.get('step-growth').className, 'done');
  assert.equal(ui.get('step-ora').className, '');
  assert.equal(ui.get('loading-progress-fill').style.width, '33%');
  assert.equal(ui.get('loading-view').hidden, false);
  assert.equal(ui.get('report-view').hidden, true);
});

test('terminal scan enters the report view without provider payload downloads', async () => {
  const ui = setup(url => url === '/api/scans' ? {scans:[]} : {status:'completed', sources:[], report:report()});
  await ui.loadScan('finished', ui.state.generation);
  assert.equal(ui.get('landing-view').hidden, true);
  assert.equal(ui.get('loading-view').hidden, true);
  assert.equal(ui.get('report-view').hidden, false);
  assert.equal(ui.get('results').hidden, false);
  assert.equal(ui.get('download-report').href, undefined);
  assert.equal(ui.get('scan-button').disabled, false);
});

test('new analysis invalidates in-flight requests and returns to the clean landing', async () => {
  let resolveScan;
  const ui = setup(() => new Promise(resolve => {resolveScan = resolve;}));
  ui.state.jobId = 'old'; ui.renderReport(report());
  const previousGeneration = ui.state.generation;
  const pending = ui.loadScan('late', previousGeneration);
  await tick();
  assert.equal(typeof ui.get('new-analysis').listeners.click, 'function');
  ui.get('new-analysis').listeners.click();
  assert.ok(ui.state.generation > previousGeneration);
  assert.equal(ui.get('landing-view').hidden, false);
  assert.equal(ui.get('loading-view').hidden, true);
  assert.equal(ui.get('report-view').hidden, true);
  assert.equal(ui.state.report, null);
  assert.equal(ui.state.jobId, null);
  for (const id of ['download-report', 'ora-raw']) assert.equal(ui.get(id).href, undefined);
  assert.equal(ui.get('scan-button').disabled, false);
  resolveScan({status:'completed', sources:[], report:report()});
  await pending;
  assert.equal(ui.get('landing-view').hidden, false);
  assert.equal(ui.get('report-view').hidden, true);
  assert.equal(ui.state.report, null);
});

test('view transitions focus populated headings once and errors return focus to input', async () => {
  const ui = setup(() => ({status:'running', sources:[], report:null}));
  ui.setView('loading');
  assert.equal(ui.get('loading-title').focusCount, 1);
  await ui.loadScan('polling', ui.state.generation);
  assert.equal(ui.get('loading-title').focusCount, 1);
  ui.renderReport(report());
  assert.equal(ui.get('report-domain').focusText, 'old.example');
  ui.setView('landing');
  assert.equal(ui.get('domain').focusCount, 1);
});


test('report overview counts scoring results without treating unmeasured checks as failures', () => {
  const ui = setup(() => ({}));
  const value = report();
  value.checks = [
    {check_id:'pass', scoring_status:'PASS', sources:{}},
    {check_id:'fail', scoring_status:'FAIL', sources:{}},
    {check_id:'unknown', scoring_status:'UNKNOWN', sources:{}},
    {check_id:'warning', scoring_status:'WARNING', sources:{}},
    {check_id:'na', scoring_status:'NOT_APPLICABLE', sources:{}},
  ];
  const before = JSON.stringify(value);
  const model = ui.modelReport(value);
  assert.equal(model.passed, 1);
  assert.equal(model.failed, 1);
  assert.equal(model.uncertain, 3);
  assert.equal(model.actions.length, 1);
  assert.equal(model.actions[0], value.checks[1]);
  assert.equal(JSON.stringify(value), before);
});


test('empty report overview remains unmeasured and handles missing checks safely', () => {
  const ui = setup(() => ({}));
  for (const checks of [undefined, null, {unexpected:'not an array'}, []]) {
    const value = {...report(), checks, score:{total:null, coverage:0, breakdown:{}}};
    const model = ui.modelReport(value);
    assert.equal(model.passed, 0);
    assert.equal(model.failed, 0);
    assert.equal(model.uncertain, 0);
      assert.equal(model.actions.length, 0);
  }
});


test('customer report hides internal cache and source-disagreement metadata', () => {
  const ui = setup(() => ({}));
  const value = report();
  value.sources = [{id:'ora', status:'completed', served_from_cache:true, result_age_seconds:12698}];
  value.checks = [{check_id:'discovery.sitemap', title:'Sitemap', category:'discovery',
    scoring_status:'PASS', scoring_source:'cloudflare', sources:{}, source_disagreement:true}];
  ui.renderReport(value);
  assert.doesNotMatch(ui.get('report-summary').textContent, /cached|source disagreement/i);
  assert.doesNotMatch(ui.get('action-list').textContent, /served_from_cache|result_age_seconds/);
});

test('agent readiness exposes checks grouped by score layer with plain-language meaning, provider, and recommendation', () => {
  const ui = setup(() => ({}));
  const value = report();
  value.score.breakdown = {
    discovery: {label:'Discoverability', points:0, max_points:20, measured:1, defined:1},
    content: {label:'Readability', points:null, max_points:25, measured:0, defined:1},
    usability: {label:'Usability', points:null, max_points:35, measured:0, defined:1},
  };
  value.checks = [
    {check_id:'discovery.sitemap', category:'discovery', title:'XML sitemap', scoring_status:'FAIL',
      scoring_source:'cloudflare', measurement_definition:'A sitemap helps agents discover the pages on your site.',
      reason:'Cloudflare reported that no sitemap was found.', sources:{cloudflare:{status:'FAIL', native_status:'fail',
        raw:{title:'raw provider title', recommendation:'raw provider recommendation', debug:'provider-json-secret'}}}},
    {check_id:'content.markdown', category:'content', title:'Markdown delivery', scoring_status:'UNKNOWN',
      measurement_definition:'Markdown makes site content easier for agents to read.',
      reason:'No definitive evidence was available.', sources:{}},
  ];
  value.recommendations = [{check_id:'discovery.sitemap', source:'cloudflare',
    recommendation:'Publish /sitemap.xml and reference it from robots.txt.'}];

  ui.renderReport(value);
  const findings = ui.get('check-findings');
  const text = visibleText(findings);
  assert.ok(findings.children.length >= 3, 'findings should include a group for every existing score category');
  assert.match(text, /Discoverability/);
  assert.match(text, /Readability/);
  assert.match(text, /Usability/);
  assert.match(text, /XML sitemap/);
  assert.match(text, /Missing/);
  assert.match(text, /A sitemap helps agents discover the pages on your site\./);
  assert.match(text, /Cloudflare/);
  assert.match(text, /Publish \/sitemap\.xml and reference it from robots\.txt\./);
  assert.match(text, /Markdown delivery/);
  assert.match(text, /Not evaluated/);
  assert.doesNotMatch(text, /raw provider title|raw provider recommendation|provider-json-secret|native_status|"sources"/);
});

test('readiness findings ignore malformed checks and never show raw provider payloads', () => {
  const ui = setup(() => ({}));
  const value = report();
  value.checks = [null, {check_id:'discovery.sitemap', category:'discovery', title:'XML sitemap',
    scoring_status:'FAIL', scoring_source:'cloudflare', measurement_definition:'A sitemap helps agents discover pages.',
    reason:'Cloudflare reported a missing sitemap.', sources:{cloudflare:{raw:{native_status:{unsafe:true},
      error:'stack trace secret'}}}}, {check_id:'unknown.category', category:'unknown', title:{unsafe:true},
    scoring_status:'PASS', sources:{}}];
  assert.doesNotThrow(() => ui.renderReport(value));
  const text = visibleText(ui.get('check-findings'));
  assert.match(text, /XML sitemap/);
  assert.doesNotMatch(text, /stack trace secret|unsafe|\[object Object\]/);
});

test('landing has no step indicators, helper text, or footer in either mode', () => {
  const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
  const landing = html.split('<section id="loading-view"')[0];
  assert.doesNotMatch(landing, /01 \/ SOCIAL PRESENCE|02 \/ CONTENT|03 \/ AGENT READINESS/);
  assert.doesNotMatch(landing, /hero-bottom/);
  assert.doesNotMatch(landing, /Enter a public website URL/);
  assert.doesNotMatch(landing, /hero-note/);
  assert.doesNotMatch(html, /<footer|site-footer|footer-brand/);
  assert.doesNotMatch(html, /One address\. A clearer picture\./);
  assert.doesNotMatch(html, /href="\/(about|docs|api|pricing|index\.md|about\.md|docs\.md|api\.md|agent-scan\.md|pricing\.md)"/);
  // The bundled script must never query removed selectors, so no
  // interaction or mode switch can re-inject them into the DOM.
  const js = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
  assert.doesNotMatch(js, /hero-bottom|site-footer|footer-brand|hero-note/);
  const css = fs.readFileSync(path.join(__dirname, '../web/styles.css'), 'utf8');
  assert.doesNotMatch(css, /hero-bottom|site-footer|footer-brand|hero-note/);
  // Landing markup is static across modes; both execute cleanly.
  for (const mode of ['normal', 'agent']) {
    const ui = setup(() => ({}));
    ui.state.mode = mode;
    assert.doesNotThrow(() => ui.clearReport());
  }
});

test('landing keeps brand, toggle, history, headline, form, and ambient background', () => {
  const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
  for (const token of ['site-header', 'data-mode-button', 'open-history', 'history-dialog',
      'landing-title', 'scan-form', 'id="domain"', 'scan-button',
      'hero-orbit', 'discovery-field', 'field-links',
      'field-social', 'field-discovery', 'field-content', 'field-compatibility']) {
    assert.ok(html.includes(token), `landing must keep ${token}`);
  }
  assert.match(html, /SOCIAL PRESENCE/);
  assert.match(html, /DISCOVERABILITY/);
  assert.match(html, /CONTENT &amp; BLOG/);
  assert.match(html, /AGENT COMPATIBILITY/);
});
