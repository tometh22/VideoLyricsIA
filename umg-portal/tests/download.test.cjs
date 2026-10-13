const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.template.html'), 'utf8');
const downloadCode = html.slice(html.indexOf('async function downloadDelivery('), html.indexOf('// ───── Approve / Reject'));
const rowCode = html.slice(html.indexOf('function renderVersionRow('), html.indexOf('// Open the same post-render editor'));
const approvalCode = html.slice(html.indexOf('async function approveDelivery('), html.indexOf('// ───── Lightbox'));

function response(status, body) {
  return { status, ok: status >= 200 && status < 300, json: async () => body, headers: { get: () => null } };
}

function portal(responses) {
  const urls = [], requests = [], downloads = [], alerts = [];
  const context = {
    PRORES_PREPARES: new Set(), PRORES_PREPARE_ERRORS: new Map(),
    renderMain() {}, reloadItems: async () => {},
    setTimeout: callback => callback(),
    startFileDownload: url => downloads.push(url),
    alert: message => alerts.push(message),
    confirm: () => true,
    clearAuth() {}, location: { reload() {} },
    apiFetch: async (url, options) => {
      urls.push([url, options?.method || 'GET']);
      requests.push([url, options]);
      assert.ok(responses.length, `Unexpected request: ${url}`);
      return responses.shift();
    },
    apiFetchStaging: () => { throw new Error('A portal export must not depend on the staging Job'); },
    escapeHtml: text => String(text ?? ''), dlIconSvg: () => '',
  };
  vm.createContext(context);
  vm.runInContext(downloadCode + '\n' + rowCode + '\n' + approvalCode, context);
  return { context, urls, requests, downloads, alerts };
}

test('a cached ProRes downloads directly', async () => {
  const p = portal([response(200, { status: 'ready', url: 'https://files/current.mov' })]);
  await p.context.downloadDelivery(1, 'umg_master');
  assert.deepEqual(p.downloads, ['https://files/current.mov']);
  assert.equal(p.urls.length, 1);
});

test('one click generates a missing master and automatically downloads it', async () => {
  const p = portal([
    response(202, { status: 'prores_missing', can_prepare: true }),
    response(202, { status: 'queued' }),
    response(202, { status: 'processing', retry_after: 5 }),
    response(200, { status: 'ready' }),
    response(200, { status: 'ready', url: 'https://files/current.mov' }),
  ]);
  await p.context.downloadDelivery(1, 'umg_master');
  assert.deepEqual(p.downloads, ['https://files/current.mov']);
  assert.equal(p.context.PRORES_PREPARES.size, 0);
  assert.equal(p.alerts.length, 0);
  assert.equal(p.urls.filter(([url, method]) => url.endsWith('/prepare-prores') && method === 'POST').length, 1);
});

test('a correction published during preparation follows the new cut', async () => {
  const p = portal([
    response(202, { status: 'prores_missing', can_prepare: true }),
    response(202, { status: 'queued' }),
    response(200, { status: 'not_started' }),
    response(202, { status: 'queued' }),
    response(200, { status: 'ready' }),
    response(200, { status: 'ready', url: 'https://files/corrected.mov' }),
  ]);
  await p.context.downloadDelivery(1, 'umg_master');
  assert.deepEqual(p.downloads, ['https://files/corrected.mov']);
  assert.equal(p.alerts.length, 0);
  assert.equal(p.urls.filter(([url, method]) => url.endsWith('/prepare-prores') && method === 'POST').length, 2);
});

test('a correction between the ready poll and download prepares the current cut', async () => {
  const p = portal([
    response(202, { status: 'prores_missing', can_prepare: true }),
    response(202, { status: 'queued' }),
    response(200, { status: 'ready' }),
    response(202, { status: 'prores_missing', can_prepare: true }),
    response(202, { status: 'queued' }),
    response(200, { status: 'ready' }),
    response(200, { status: 'ready', url: 'https://files/corrected.mov' }),
  ]);
  await p.context.downloadDelivery(1, 'umg_master');
  assert.deepEqual(p.downloads, ['https://files/corrected.mov']);
  assert.equal(p.alerts.length, 0);
});

test('a failed export leaves an actionable retry and a retry succeeds', async () => {
  const responses = [
    response(202, { status: 'prores_missing', can_prepare: true }),
    response(202, { status: 'queued' }),
    response(200, { status: 'failed', message: 'Podés reintentar.' }),
  ];
  const p = portal(responses);
  await p.context.downloadDelivery(1, 'umg_master');
  assert.equal(p.context.PRORES_PREPARES.size, 0);
  assert.equal(p.context.PRORES_PREPARE_ERRORS.get('1:umg_master').action, 'retry');
  assert.equal(p.downloads.length, 0);
  responses.push(
    response(202, { status: 'prores_missing', can_prepare: true }),
    response(202, { status: 'queued' }),
    response(200, { status: 'ready' }),
    response(200, { status: 'ready', url: 'https://files/retried.mov' }),
  );
  await p.context.downloadDelivery(1, 'umg_master');
  assert.deepEqual(p.downloads, ['https://files/retried.mov']);
  assert.equal(p.context.PRORES_PREPARE_ERRORS.size, 0);
});

test('lyrics and art tracks both offer enabled preparation buttons', () => {
  const p = portal([]);
  for (const label of ['Art Track', 'Campaña', 'Renderizado']) {
    const version = { delivery_id: 1, label, files: [
      { type: 'video', available: true, size: '10 MB' },
      { type: 'umg_master', label: 'ProRes Master', available: false, can_prepare: true },
    ] };
    const rendered = p.context.renderVersionRow({ artist: 'Artist', song: 'Song', versions: [version] }, version, 0);
    assert.match(rendered, /Generar y descargar/);
    assert.match(rendered, /onclick="downloadDelivery\(1, &quot;umg_master&quot;\)"/);
    assert.doesNotMatch(rendered, /onclick="downloadDelivery\(1, &quot;umg_master&quot;\)" disabled/);
  }
});

test('a preparation remains visible even when listing data says a cached master is available', () => {
  const p = portal([]);
  p.context.PRORES_PREPARES.add('1:umg_master');
  const version = { delivery_id: 1, files: [{ type: 'umg_master', label: 'ProRes', available: true, size: '1 GB' }] };
  const rendered = p.context.renderVersionRow({ artist: 'Artist', song: 'Song', versions: [version] }, version, 0);
  assert.match(rendered, /Preparando…/);
  assert.match(rendered, /onclick="downloadDelivery\(1, &quot;umg_master&quot;\)" disabled/);
});

test('approval sends the revision and update timestamp reviewed by the client', async () => {
  const p = portal([response(200, { ok: true })]);
  await p.context.approveDelivery(7, 'Artist — Song', 3, '2026-10-03T13:00:00+00:00');
  assert.equal(p.requests[0][0], '/api/deliveries/7/approve');
  assert.deepEqual(JSON.parse(p.requests[0][1].body), {
    expected_revision: 3,
    expected_content_updated_at: '2026-10-03T13:00:00+00:00',
  });
  assert.equal(p.alerts.length, 0);
});

test('approval button waits while the pointer cut is updating or hidden', () => {
  const p = portal([]);
  for (const flag of ['updating', 'files_hidden']) {
    const version = { delivery_id: 7, revision: 3, content_updated_at: null, files: [], [flag]: true };
    const rendered = p.context.renderVersionRow({ artist: 'Artist', song: 'Song', versions: [version] }, version, 0);
    assert.match(rendered, /Actualizando…<\/button>/);
    assert.doesNotMatch(rendered, /onclick="approveDelivery\(/);
  }
});
