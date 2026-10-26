const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const zlib = require('node:zlib');

function fixture() {
  const elements = new Map(), calls = [], timers = new Map();
  let authenticated = true, locked = false, downloads = 0, timerId = 0;
  let state = { ok: true, phase: 'idle', job_id: 'checked', installed_version: '0.4.1', version: '0.5.0' };
  let prepareWait = null;
  let rejectApply = false;
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      hidden: false, value: '', checked: false, disabled: false, open: false, textContent: '', listeners: {},
      addEventListener(type, handler) { this.listeners[type] = handler; },
      showModal() { this.open = true; }, close() { this.open = false; },
      async emit(type) { return this.listeners[type]?.({ preventDefault() {} }); },
    });
    return elements.get(id);
  }
  const context = {
    window: {}, document: { getElementById: element, body: { append() {} },
      createElement() { return { click() { downloads++; }, remove() {} }; } },
    location: { reload() {} }, Blob, Uint8Array, atob,
    URL: { createObjectURL: () => 'blob:fixture', revokeObjectURL() {} },
    setTimeout(handler) { const id = ++timerId; timers.set(id, handler); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../overlay/www/assets/update.js'), 'utf8'), context);
  const updater = context.window.createFirmwareUpdater({
    hasSession: () => authenticated, installedVersion: () => '0.4.1', online: () => true, isDemo: () => false,
    setLocked(value) { locked = value; },
    async call(method, args) {
      calls.push({ method, args: JSON.parse(JSON.stringify(args)) });
      if (method === 'update_check') { state.phase = 'available'; return { ok: true, job_id: state.job_id }; }
      if (method === 'update_status') return { ...state };
      if (method === 'update_prepare') {
        if (prepareWait) await prepareWait;
        state = { ...state, phase: 'preparing', job_id: 'prepared', received_bytes: 123, size: 1000 };
        return { ok: true, job_id: state.job_id };
      }
      if (method === 'update_backup') return { ok: true, filename: 'fixture.tar.gz', base64: zlib.gzipSync('fixture').toString('base64') };
      if (method === 'update_apply') {
        if (rejectApply) {
          state.phase = 'error'; state.message = 'Настройки изменились. Повторите подготовку.';
          throw Object.assign(new Error(state.message), { rejected: true });
        }
        return { ok: true, job_id: state.job_id };
      }
      throw new Error('Unexpected fixture method');
    },
  });
  return { element, updater, calls,
    get locked() { return locked; }, get downloads() { return downloads; },
    logout() { authenticated = false; updater.reset(); },
    deferPreparation(value) { prepareWait = value; },
    rejectApply() { rejectApply = true; },
    async makeReady() {
      state.phase = 'ready';
      const next = [...timers.entries()][0];
      assert.ok(next, 'Preparation should poll asynchronously');
      timers.delete(next[0]); await next[1]();
    },
  };
}

async function prepared() {
  const f = fixture();
  await f.element('firmware').emit('click');
  await f.element('update-prepare').emit('click');
  await f.makeReady();
  return f;
}

test('checking and preparing never write firmware; percentage uses transferred bytes', async () => {
  const f = fixture();
  await f.element('firmware').emit('click');
  assert.equal(f.element('update-available').hidden, false);
  await f.element('update-prepare').emit('click');
  assert.equal(f.element('update-percent').textContent, '12%');
  assert.equal(f.element('update-ready').hidden, true);
  assert.equal(f.calls.some(call => call.method === 'update_apply'), false);
});

test('apply is blocked before archive retrieval and explicit saved/action confirmations', async () => {
  const f = await prepared();
  f.element('update-backup-saved').checked = true;
  f.element('update-confirmation').value = 'ОБНОВИТЬ';
  await f.element('update-apply-form').emit('submit');
  assert.equal(f.calls.some(call => call.method === 'update_apply'), false);
  await f.element('update-backup').emit('click');
  assert.equal(f.downloads, 1);
  f.element('update-backup-saved').checked = false;
  await f.element('update-apply-form').emit('submit');
  f.element('update-backup-saved').checked = true;
  f.element('update-confirmation').value = 'да';
  await f.element('update-apply-form').emit('submit');
  assert.equal(f.calls.some(call => call.method === 'update_apply'), false);
});

test('reviewed job is applied once and changing settings stays locked during reboot', async () => {
  const f = await prepared();
  await f.element('update-backup').emit('click');
  f.element('update-backup-saved').checked = true;
  f.element('update-confirmation').value = ' обновить ';
  await f.element('update-confirmation').emit('input');
  assert.equal(f.element('update-apply').disabled, false);
  await f.element('update-apply-form').emit('submit');
  await f.element('update-apply-form').emit('submit');
  const writes = f.calls.filter(call => call.method === 'update_apply');
  assert.equal(writes.length, 1);
  assert.deepEqual(writes[0].args, { job_id: 'prepared', confirmation: 'ОБНОВИТЬ', backup_saved: true });
  assert.equal(f.locked, true);
  assert.equal(f.element('update-close').disabled, true);
  assert.equal(f.element('update-reload').hidden, false);
});

test('closing a prepared dialog never applies the update', async () => {
  const f = await prepared();
  await f.element('update-close').emit('click');
  assert.equal(f.element('update-dialog').open, false);
  assert.equal(f.calls.some(call => call.method === 'update_apply'), false);
});

test('explicit validation refusal allows immediate fresh preparation', async () => {
  const f = await prepared();
  await f.element('update-backup').emit('click');
  f.element('update-backup-saved').checked = true;
  f.element('update-confirmation').value = 'ОБНОВИТЬ';
  f.rejectApply();
  await f.element('update-apply-form').emit('submit');
  assert.equal(f.locked, false);
  assert.equal(f.element('update-ready').hidden, true);
  assert.equal(f.element('update-check').disabled, false);
  assert.match(f.element('update-stage').textContent, /Настройки изменились/);
  await f.element('update-check').emit('click');
  assert.equal(f.element('update-prepare').hidden, false);
  assert.equal(f.element('update-backup-saved').checked, false);
});

test('logout discards a late preparation response and never applies it', async () => {
  const f = fixture();
  await f.element('firmware').emit('click');
  let resolve;
  f.deferPreparation(new Promise(done => { resolve = done; }));
  const preparation = f.element('update-prepare').emit('click');
  f.logout(); resolve(); await preparation;
  assert.equal(f.element('update-dialog').open, false);
  assert.equal(f.element('update-ready').hidden, true);
  assert.equal(f.calls.some(call => call.method === 'update_apply'), false);
});
