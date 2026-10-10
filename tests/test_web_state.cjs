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
    AbortSignal, setTimeout() {}, fetch: async url => ({ ok: true, headers: { get: () => 'application/json' }, json: () => fetchData(url) }),
  });
  const code = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8').replace(/initialize\(\);\s*$/, '');
  vm.runInContext(code + '\nglobalThis.ui = {state, refreshHistory, renderReport, formatTime, updateSources, loadScan, clearReport, initialize, modelReport: typeof modelReport === "function" ? modelReport : undefined, reportSummary: typeof reportSummary === "function" ? reportSummary : undefined, setView: typeof setView === "function" ? setView : undefined};', context);
  return { ...context.ui, get };
}
const tick = () => new Promise(resolve => setImmediate(resolve));
const report = () => ({ domain: 'old.example', scanned_at: '2026-10-10T12:00:00Z', score: {total: 20, coverage: 10, breakdown: {}}, checks: [], sources: [], recommendations: [] });
const jobs = [{id: 'a', domain: 'a.example'}, {id: 'b', domain: 'b.example'}];

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

test('malformed provider summaries and recommendation containers render safely', () => {
  const ui = setup(() => ({}));
  const bad = report();
  bad.sources = [null, {id:'ora', score: {}, grade: [], level: true, check_count: {}, error: {message:'bad'}, observed_at: []}];
  bad.recommendations = {unexpected:'not an array'};
  assert.doesNotThrow(() => ui.renderReport(bad));
  assert.doesNotMatch(ui.get('ora-detail').textContent, /\[object Object\]|true\/5/);
  assert.equal(ui.get('recommendations').children.length, 1);
});

test('non-string native fields are displayed as JSON without coercion or evidence mutation', () => {
  const ui = setup(() => ({}));
  const bad = report();
  const native = {toString: 0};
  bad.checks = [{title:'Native status', sources:{ora:{status:'UNKNOWN', native_status:native}}, scoring_status:'UNKNOWN', reason:'Test'}];
  bad.source_specific = [{source:'ora', source_check_id:'extra', raw:{name:native, title:native, status:native}}];
  bad.recommendations = [{recommendation:native}];
  const before = JSON.stringify(bad);
  assert.doesNotThrow(() => ui.renderReport(bad));
  assert.equal(ui.get('checks').children[0].children[1].children[1].textContent, 'Native: {"toString":0}');
  assert.match(ui.get('specific-list').children[0].children[0].textContent, /\{"toString":0\}/);
  assert.match(ui.get('recommendations').children[0].textContent, /"toString": 0/);
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
  assert.match(ui.get('notice').textContent, /Partial results/);
  assert.equal(ui.get('ora-state').textContent, 'Waiting');
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
  assert.equal(ui.get('mk-findings').children[0].children[1].children[1].children[2].children[0].textContent, 'X · https://x.com/acme');
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

test('terminal scan enters the report view and supplies its own download link', async () => {
  const ui = setup(url => url === '/api/scans' ? {scans:[]} : {status:'completed', sources:[], report:report()});
  await ui.loadScan('finished', ui.state.generation);
  assert.equal(ui.get('landing-view').hidden, true);
  assert.equal(ui.get('loading-view').hidden, true);
  assert.equal(ui.get('report-view').hidden, false);
  assert.equal(ui.get('results').hidden, false);
  assert.equal(ui.get('download-report').href, '/api/scans/finished/report');
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
