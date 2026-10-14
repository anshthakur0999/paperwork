'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const {IDBFactory} = require('fake-indexeddb');

const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const functions = source.slice(0, source.indexOf("\ndocument.querySelectorAll('[data-category]').forEach(button=>button.addEventListener"));
const LocalDraftStore = new Function('Blob', source.slice(0, source.indexOf('\nconst tools =')) + '\nreturn LocalDraftStore;')(Blob);
const tick = () => new Promise(resolve => setImmediate(resolve));

function storeHarness(maxBytes) {
  let now = Date.now();
  const store = new LocalDraftStore({indexedDB: new IDBFactory(), now: () => now, maxBytes});
  return {store, advance: milliseconds => {now += milliseconds;}};
}
function draft(tool = 'delete', options = {pages: '2,4-5'}, size = 5) {
  return {tool, files: [new File(['a'.repeat(size)], 'sample.pdf', {type: 'application/pdf', lastModified: 42})], options, selectedIndex: 0, previewPage: 2};
}

test('documents, file order and edit settings survive a database reopen; passwords do not', async () => {
  const {store} = storeHarness();
  const snapshot = draft('merge', {pages: '3,1', password: 'original-secret', user_password: 'new-secret', owner_password: 'owner-secret'});
  snapshot.files.push(new File(['second document'], 'second.pdf', {type: 'application/pdf'}));
  await store.save(snapshot);
  (await store.open()).close();
  store.connection = null;
  const restored = await store.load('merge');
  assert.deepEqual(restored.files.map(file => file.name), ['sample.pdf', 'second.pdf']);
  assert.equal(await restored.files[1].blob.text(), 'second document');
  assert.deepEqual(restored.options, {pages: '3,1'});
  assert.equal(restored.selectedIndex, 0);
  assert.equal(restored.previewPage, 2);
});

test('expiry is exactly 24 hours after the edit and resuming does not extend it', async () => {
  const {store, advance} = storeHarness();
  const {draft: saved} = await store.save(draft());
  advance(LocalDraftStore.retention - 1);
  assert.equal((await store.load('delete')).expiresAt, saved.expiresAt);
  assert.equal((await store.list())[0].updatedAt, saved.updatedAt);
  advance(1);
  assert.equal(await store.load('delete'), null);
  assert.deepEqual(await store.list(), []);
  const db = await store.open();
  const recordCount = await new Promise(resolve => {
    const request = db.transaction('drafts', 'readonly').objectStore('drafts').count();
    request.onsuccess = () => resolve(request.result);
  });
  assert.equal(recordCount, 0, 'expired document data is deleted from storage');
});

test('a new edit renews expiry and replaces only that tool draft', async () => {
  const {store, advance} = storeHarness();
  const first = (await store.save(draft())).draft;
  await store.save(draft('reorder', {pages: '3,1,2'}));
  advance(3600000);
  const second = (await store.save(draft('delete', {pages: '3'}))).draft;
  assert.equal(second.expiresAt - first.expiresAt, 3600000);
  assert.deepEqual((await store.load('delete')).options, {pages: '3'});
  assert.equal((await store.list()).length, 2);
  assert.deepEqual((await store.load('reorder')).options, {pages: '3,1,2'});
});

test('expired drafts are purged at startup, and total storage evicts the oldest draft', async () => {
  const {store, advance} = storeHarness(12);
  await store.save(draft('delete', {}, 5));
  advance(1);
  await store.save(draft('reorder', {}, 5));
  advance(1);
  const result = await store.save(draft('merge', {}, 5));
  assert.deepEqual(result.evicted, ['delete']);
  assert.deepEqual((await store.list()).map(item => item.tool), ['merge', 'reorder']);
  advance(LocalDraftStore.retention);
  assert.deepEqual(await store.list(), []);
});

test('clear operations remove saved documents; oversized writes keep the old draft intact', async () => {
  const {store} = storeHarness(10);
  await store.save(draft());
  await assert.rejects(store.save(draft('delete', {pages: '1'}, 11)), /cannot be saved/);
  assert.deepEqual((await store.load('delete')).options, {pages: '2,4-5'});
  await store.save(draft('reorder'));
  await store.remove('delete');
  assert.equal(await store.load('delete'), null);
  assert.equal((await store.list()).length, 1);
  await store.clear();
  assert.deepEqual(await store.list(), []);
  await assert.rejects(new LocalDraftStore({indexedDB: null}).list(), /unavailable/);
});

function appHarness(tool = 'delete') {
  const nodes = new Map(), {store} = storeHarness();
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, {
      value: '', hidden: false, disabled: false, checked: false,
      classList: {add() {}, remove() {}, toggle() {}},
      setAttribute(name, value) { this[name] = value; }, removeAttribute(name) { delete this[name]; },
      setCustomValidity(value) { this.validationMessage = value; },
      querySelector() { return node(id + '-child'); }, querySelectorAll() { return []; },
      replaceChildren() { this.innerHTML = ''; }, prepend() {}
    });
    return nodes.get(id);
  }
  node('tool-catalog').textContent = fs.readFileSync(path.join(__dirname, '../static/tools.json'), 'utf8');
  Object.assign(node('option-pages'), {name: 'pages', type: 'text'});
  node('tool-options').querySelectorAll = () => [node('option-pages')];
  const requests = [];
  const context = vm.createContext({
    Blob, File, FormData, TextDecoder, Uint8Array, AbortController, externalStore: store,
    setTimeout(fn, delay) { const timer = setTimeout(fn, delay); timer.unref(); return timer; }, clearTimeout,
    URL: {revokeObjectURL() {}},
    document: {getElementById: node, querySelectorAll() { return []; }, createElement() { return {dataset: {}, setAttribute() {}}; }},
    async fetch(url, options) {
      requests.push({url, options});
      if (url === '/api/inspect') return {ok: true, json: async () => ({page_count: 5})};
      assert.equal(url, '/api/thumbnails');
      let delivered = false;
      return {ok: true, body: {getReader: () => ({async read() {
        if (delivered) return {done: true};
        delivered = true;
        return {done: false, value: Buffer.from('{"complete":true}\n')};
      }})}};
    }
  });
  const run = code => vm.runInContext(code, context);
  run(functions);
  run(`draftStore=externalStore; activeTool=tools.find(tool=>tool.id==='${tool}');`);
  return {run, node, store, requests};
}

test('restoring Delete Pages rebuilds its selections without saving or retaining a password', async () => {
  const h = appHarness();
  const saved = (await h.store.save(draft())).draft;
  await h.run("restoreToolDraft('delete')");
  await tick();
  assert.equal(h.node('option-pages').value, '2,4-5');
  assert.equal(h.run('deleteSelection.size'), 3);
  assert.equal(h.node('process-button').disabled, false);
  assert.equal(h.requests[0].options.body.get('password'), '');
  assert.equal((await h.store.load('delete')).expiresAt, saved.expiresAt);
  assert.equal(h.run('pendingDraft'), null);
});

test('restoring Reorder Pages preserves order and repeated pages through reinspection', async () => {
  const h = appHarness('reorder');
  await h.store.save(draft('reorder', {pages: '3,1,1,5,2'}));
  await h.run("restoreToolDraft('reorder')");
  await tick();
  assert.equal(h.run('reorderOrder.map(entry=>entry.page).join(",")'), '3,1,1,5,2');
  assert.equal(h.node('option-pages').value, '3,1,1,5,2');
});

test('a delayed draft read never overwrites work added while it is loading', async () => {
  const h = appHarness();
  await h.store.save(draft());
  const stored = await h.store.load('delete');
  let resolve;
  h.store.load = () => new Promise(done => {resolve = done;});
  const restoring = h.run("restoreToolDraft('delete')");
  await tick();
  h.run('draftRevision++; files=[{id:99,file:new File(["new"],"new.pdf")}]; selectedId=99;');
  resolve(stored);
  await restoring;
  assert.equal(h.run('files[0].file.name'), 'new.pdf');
  assert.equal(h.node('option-pages').value, '');
});

test('clearing saved work cancels debounced writes and waits for writes already in progress', async () => {
  const h = appHarness();
  h.run('files=[{id:1,file:new File(["original"],"original.pdf")}]; selectedId=1;');
  h.node('option-pages').value = '2';
  h.run('queueDraftSave(); flushDraftSave()');
  h.node('option-pages').value = '3';
  h.run('queueDraftSave()');
  await h.run('clearSavedWork()');
  assert.deepEqual(await h.store.list(), []);
  assert.equal(h.run('pendingDraft'), null);
  assert.equal(h.run('files[0].file.name'), 'original.pdf', 'clearing saved copies leaves active work intact');
  await h.run('flushDraftSave()');
  assert.deepEqual(await h.store.list(), [], 'unload flushing cannot recreate a cleared draft');
});

test('page-click edits start saving immediately and remain pending until committed', async () => {
  const h = appHarness();
  h.run('files=[{id:1,file:new File(["original"],"original.pdf")}]; selectedId=1; beginDelete(files[0]); initializeDelete(5);');
  h.run('toggleDeletePage(2)');
  assert.equal(h.run('pendingDraft'), null, 'click edits are not left in the typing debounce');
  assert.equal(h.run('draftWritesInFlight'), 1, 'unload protection stays active while writing');
  await h.run('draftWriteQueue');
  assert.equal(h.run('draftWritesInFlight'), 0);
  assert.deepEqual((await h.store.load('delete')).options, {pages: '2'});
});
