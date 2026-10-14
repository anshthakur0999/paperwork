'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const functions = source.slice(0, source.indexOf("\ndocument.querySelectorAll('[data-category]').forEach(button=>button.addEventListener"));

function harness(count = 5) {
  const nodes = new Map(), requests = [];
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, {
      value: '', disabled: false, hidden: false,
      classList: {add() {}, remove() {}, toggle() {}},
      setAttribute(name, value) { this[name] = value; },
      removeAttribute(name) { delete this[name]; },
      setCustomValidity(value) { this.validationMessage = value; },
      querySelector() { return node(id + '-child'); },
      querySelectorAll() { return []; },
      replaceChildren() { this.innerHTML = ''; },
      prepend() {}
    });
    return nodes.get(id);
  }
  node('tool-catalog').textContent = '[]';
  node('tool-options').querySelectorAll = () => [{name: 'pages', value: node('option-pages').value}];
  const context = vm.createContext({
    AbortController, FormData, Blob, TextDecoder, Uint8Array, setTimeout, clearTimeout,
    URL: {revokeObjectURL() {}},
    document: {getElementById: node, querySelectorAll() { return []; }, createElement() { return {dataset: {}, setAttribute() {}}; }},
    fetch(url, options) {
      return new Promise(resolve => requests.push({url, ...options, resolve}));
    }
  });
  const run = code => vm.runInContext(code, context);
  run(functions);
  run(`activeTool={id:'delete'}; files=[{id:1,file:new Blob(['pdf'])}]; selectedId=1; beginDelete(files[0]); initializeDelete(${count});`);
  return {node, requests, run, selection: () => JSON.parse(run('JSON.stringify(Array.from(deleteSelection).sort((a,b)=>a-b))'))};
}

test('multiple preview removals and restores produce the correct submitted page numbers', () => {
  const h = harness();
  assert.equal(h.node('process-button').disabled, true);
  h.run('toggleDeletePage(2); toggleDeletePage(4); toggleDeletePage(5)');
  assert.equal(h.node('option-pages').value, '2,4-5');
  assert.equal(h.node('delete-summary').textContent, '3 pages to remove · 2 to keep.');
  assert.equal(h.node('process-button').disabled, false);
  assert.deepEqual(JSON.parse(h.run('JSON.stringify(collectOptions())')), {password: '', pages: '2,4-5'});
  h.run('toggleDeletePage(4)');
  assert.equal(h.node('option-pages').value, '2,5');
  assert.deepEqual(h.selection(), [2, 5]);
  h.run('deleteSelection.clear(); writeDeleteSelection()');
  assert.equal(h.node('option-pages').value, '');
  assert.equal(h.node('process-button').disabled, true);
});

test('manual ranges update the preview and invalid selections block saving', () => {
  const h = harness();
  h.node('option-pages').value = '2,4-5';
  assert.equal(h.run('syncDeleteFromInput()'), true);
  assert.deepEqual(h.selection(), [2, 4, 5]);
  for (const value of ['all', '1-5', '6', '0', '4-2', '2,2', 'two', '2,']) {
    h.node('option-pages').value = value;
    assert.equal(h.run('syncDeleteFromInput(true)'), false, value);
    assert.equal(h.node('process-button').disabled, true, value);
    assert.ok(h.node('option-pages').validationMessage, value);
  }
  h.node('option-pages').value = '1';
  h.run('syncDeleteFromInput()');
  assert.equal(h.node('option-pages').validationMessage, '');
  assert.deepEqual(h.selection(), [1]);
  assert.equal(h.node('process-button').disabled, false);
});

test('the final remaining page is protected and a one-page PDF cannot be emptied', () => {
  const h = harness(3);
  h.run('toggleDeletePage(1); toggleDeletePage(2); toggleDeletePage(3)');
  assert.deepEqual(h.selection(), [1, 2]);
  assert.match(h.node('error-message').textContent, /At least one page/);
  const single = harness(1);
  single.run('toggleDeletePage(1)');
  assert.deepEqual(single.selection(), []);
  assert.equal(single.node('process-button').disabled, true);
});

test('processing freezes selection and a replacement PDF clears selection and invalidity', () => {
  const h = harness();
  h.run('toggleDeletePage(2); processing=true; toggleDeletePage(3)');
  assert.deepEqual(h.selection(), [2]);
  h.run('processing=false; beginDelete(files[0]); initializeDelete(5)');
  assert.deepEqual(h.selection(), [2], 'password reinspection preserves the selection');
  h.node('option-pages').value = 'invalid';
  h.run('syncDeleteFromInput(); files=[{id:2,file:new Blob(["replacement"])}]; selectedId=2; beginDelete(files[0]); initializeDelete(2)');
  assert.deepEqual(h.selection(), []);
  assert.equal(h.node('option-pages').value, '');
  assert.equal(h.node('option-pages').validationMessage, '');
  assert.equal(h.node('process-button').disabled, true);
});

function streamReply(request, records) {
  let delivered = false;
  request.resolve({ok: true, body: {getReader: () => ({
    async read() {
      if (delivered) return {done: true};
      delivered = true;
      return {done: false, value: Buffer.from(records.map(record => JSON.stringify(record)).join('\n') + '\n')};
    },
    async cancel() {}
  })}});
}

test('all streamed previews load and individual failures leave page selection available', async () => {
  const h = harness(3);
  const loading = h.run('loadDeleteThumbnails(inspectingVersion)');
  assert.equal(h.requests[0].url, '/api/thumbnails');
  streamReply(h.requests[0], [
    {page: 1, image: 'data:image/jpeg;base64,first'},
    {page: 2, error: 'This page could not be previewed.'},
    {page: 3, image: 'data:image/jpeg;base64,last'},
    {complete: true, page_count: 3}
  ]);
  await loading;
  assert.equal(h.run('deleteImages.size'), 2);
  assert.equal(h.run('deleteErrors.size'), 1);
  assert.equal(h.node('delete-pages')['aria-busy'], 'false');
  h.run('toggleDeletePage(2)');
  assert.equal(h.node('option-pages').value, '2');
  assert.equal(h.node('process-button').disabled, false);
});

test('late thumbnails cannot repopulate the preview after a file reset', async () => {
  const h = harness(3);
  const loading = h.run('loadDeleteThumbnails(inspectingVersion)');
  h.run('resetFiles()');
  assert.equal(h.requests[0].signal.aborted, true);
  streamReply(h.requests[0], [{page: 1, image: 'data:image/jpeg;base64,old'}, {complete: true}]);
  await loading;
  assert.equal(h.run('deleteImages.size'), 0);
  assert.equal(h.node('delete-panel').hidden, true);
  assert.equal(h.node('delete-pages').innerHTML, '');
});
