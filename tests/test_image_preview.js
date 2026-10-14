'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

// Run the actual application functions with controllable image decoding.
const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const functions = source.slice(0, source.indexOf("\ndocument.querySelectorAll('[data-category]').forEach(button=>button.addEventListener"));

function harness({pausePreviewDecode = false} = {}) {
  const nodes = new Map(), changes = [], pending = [], urls = new Map(), revoked = [], decoded = new Set(), requests = [];
  let nextURL = 0;
  function node(id) {
    if (!nodes.has(id)) {
      let hidden = false;
      const element = {
        value: '', src: '', alt: '', disabled: false, validity: {valid: true},
        classList: {add() {}, toggle() {}, remove() {}},
        setAttribute(name, value) { this[name] = value; },
        removeAttribute(name) { delete this[name]; },
        querySelector() { return node(id + '-child'); },
        querySelectorAll() { return []; },
        replaceChildren() {},
        focus() {},
        get hidden() { return hidden; },
        set hidden(value) { hidden = value; changes.push({id, hidden: value}); }
      };
      nodes.set(id, element);
    }
    return nodes.get(id);
  }
  node('tool-options').querySelectorAll = () => [
    {name: 'page_size', value: node('option-page_size').value},
    {name: 'orientation', value: node('option-orientation').value},
    {name: 'margin', type: 'number', value: node('option-margin').value}
  ];
  node('option-page_size').value = 'a4';
  node('option-orientation').value = 'landscape';
  node('option-margin').value = '24';
  node('tool-catalog').textContent = fs.readFileSync(path.join(__dirname, '../static/tools.json'), 'utf8');
  const context = vm.createContext({
    AbortController, FormData, setTimeout, clearTimeout,
    fetch(url, options) {
      assert.equal(url, '/api/preview');
      return new Promise((resolve, reject) => requests.push({page: Number(options.body.get('page')), signal: options.signal, resolve, reject}));
    },
    document: {
      getElementById: node,
      querySelectorAll() { return []; },
      createElement(type) {
        assert.equal(type, 'canvas');
        return {
          getContext() { return {scale() {}, fillRect() {}, drawImage() {}}; },
          toBlob(callback) { callback({preview: true, width: this.width, height: this.height}); }
        };
      }
    },
    URL: {
      createObjectURL(value) { const url = 'blob:' + ++nextURL; urls.set(url, value); return url; },
      revokeObjectURL(url) { revoked.push(url); }
    },
    Image: class {
      decode() {
        const value = urls.get(this.src);
        this.naturalWidth = value.width || 320;
        this.naturalHeight = value.height || 480;
        if (value.preview) {
          if (pausePreviewDecode) return new Promise(resolve => pending.push({name: value.name, resolve: () => {decoded.add(this.src); resolve();}}));
          decoded.add(this.src); return Promise.resolve();
        }
        return new Promise((resolve, reject) => pending.push({name: value.name, resolve, reject}));
      }
    }
  });
  const run = code => vm.runInContext(code, context);
  run(functions);
  run("activeTool={id:'images-to-pdf'}; files=[1,2,3].map(id=>({id,file:{name:'image-'+id+'.png',size:100}})); selectedId=1; previewURL=URL.createObjectURL({preview:true}); $('preview-image').src=previewURL;");
  node('preview-image').hidden = false;
  node('preview-placeholder').hidden = true;
  changes.length = 0;
  function select(id) { run(`selectedId=${id}`); return run('inspectSelected()'); }
  function resolve(name) {
    const index = pending.findIndex(entry => entry.name === name);
    assert.notEqual(index, -1, 'Image decoding was requested');
    pending.splice(index, 1)[0].resolve();
  }
  function reply(index) {
    const request = requests[index];
    request.resolve({ok: true, blob: async () => ({preview: true, name: 'page-' + request.page, width: 300, height: 400})});
  }
  return {node, changes, pending, revoked, decoded, requests, reply, run, select, resolve};
}

test('switching keeps the current preview visible until the next page is decoded', async () => {
  const h = harness(), original = h.node('preview-image').src;
  const update = h.select(2);
  assert.equal(h.node('preview-image').src, original);
  assert.equal(h.node('preview-image').hidden, false);
  assert.equal(h.node('document-preview').hidden, false);
  assert.equal(h.node('preview-placeholder').hidden, true);
  assert.equal(h.revoked.includes(original), false);
  h.resolve('image-2.png');
  await update;
  assert.equal(h.node('preview-image').alt, 'PDF page preview of image-2.png');
  assert.equal(h.decoded.has(h.node('preview-image').src), true);
  assert.equal(h.revoked.includes(original), true);
  assert.equal(h.changes.some(change => ['preview-image', 'document-preview'].includes(change.id) && change.hidden), false);
});

test('rapid switching ignores an older image that finishes decoding last', async () => {
  const h = harness();
  const older = h.select(2), latest = h.select(3);
  h.resolve('image-3.png');
  await latest;
  const current = h.node('preview-image').src;
  h.resolve('image-2.png');
  await older;
  assert.equal(h.node('preview-image').src, current);
  assert.equal(h.node('preview-image').alt, 'PDF page preview of image-3.png');
  assert.equal(h.node('preview-page-count').textContent, 'Image 3 of 3');
  assert.equal(h.node('document-preview')['aria-busy'], 'false');
});

test('revisiting an image uses its cached page and layout changes get a new preview', async () => {
  const h = harness();
  const first = h.select(2);
  h.resolve('image-2.png'); await first;
  const other = h.select(3);
  h.resolve('image-3.png'); await other;
  await h.select(2);
  assert.equal(h.pending.length, 0);
  assert.equal(h.node('preview-image').alt, 'PDF page preview of image-2.png');
  h.node('option-orientation').value = 'portrait';
  const changed = h.run('loadImagePreview()');
  assert.equal(h.pending.length, 1);
  h.resolve('image-2.png'); await changed;
  assert.equal(h.node('preview-page-label').textContent, 'A4 · Portrait');
});

test('resetting files cancels a pending preview and clears cached pages', async () => {
  const h = harness();
  const update = h.select(2);
  h.run('resetFiles()');
  h.resolve('image-2.png'); await update;
  assert.equal(h.node('document-preview').hidden, true);
  assert.equal(h.node('preview-image').src, undefined);
  assert.equal(h.run('previewCache.size'), 0);
});

test('the page preview cache remains bounded', async () => {
  const h = harness();
  h.run("files=Array.from({length:12},(_,index)=>({id:index+1,file:{name:'image-'+(index+1)+'.png',size:100}}))");
  for (let id = 1; id <= 12; id++) {
    const update = h.select(id);
    h.resolve('image-' + id + '.png'); await update;
  }
  assert.equal(h.run('previewCache.size'), 8);
});

function pdfHarness(options) {
  const h = harness(options);
  h.run("activeTool={id:'split'}; files=[{id:1,file:{name:'sample.pdf',size:100},info:{page_count:3}}]; selectedId=1; previewPage=1;");
  return h;
}

test('PDF page switching keeps the current page through fetching and decoding', async () => {
  const h = pdfHarness({pausePreviewDecode: true}), original = h.node('preview-image').src;
  const update = h.run('previewPage=2; loadPreview()');
  assert.equal(h.node('preview-image').src, original);
  assert.equal(h.node('preview-image').hidden, false);
  assert.equal(h.node('preview-placeholder').hidden, true);
  h.reply(0);
  await new Promise(setImmediate);
  assert.equal(h.node('preview-image').src, original);
  assert.equal(h.revoked.includes(original), false);
  h.resolve('page-2'); await update;
  assert.equal(h.node('preview-image').alt, 'Page 2 of sample.pdf');
  assert.equal(h.decoded.has(h.node('preview-image').src), true);
  assert.equal(h.changes.some(change => ['preview-image', 'document-preview'].includes(change.id) && change.hidden), false);
});

test('rapid PDF page clicks ignore stale responses even when returning to the same page', async () => {
  const h = pdfHarness();
  const older = h.run('previewPage=2; loadPreview()');
  const middle = h.run('previewPage=3; loadPreview()');
  const latest = h.run('previewPage=2; loadPreview()');
  assert.equal(h.requests[0].signal.aborted, true);
  h.requests[0].reject(new Error('Old request failed')); await older;
  assert.equal(h.node('preview-image').hidden, false);
  h.reply(2); await latest;
  const current = h.node('preview-image').src;
  h.reply(1); await middle;
  assert.equal(h.node('preview-image').src, current);
  assert.equal(h.node('preview-image').alt, 'Page 2 of sample.pdf');
  assert.equal(h.node('document-preview')['aria-busy'], 'false');
});

test('revisiting a PDF page uses its cached rendering without another request', async () => {
  const h = pdfHarness();
  const second = h.run('previewPage=2; loadPreview()'); h.reply(0); await second;
  const third = h.run('previewPage=3; loadPreview()'); h.reply(1); await third;
  await h.run('previewPage=2; loadPreview()');
  assert.equal(h.requests.length, 2);
  assert.equal(h.node('preview-image').alt, 'Page 2 of sample.pdf');
});

test('resetting files prevents a pending PDF preview from reappearing', async () => {
  const h = pdfHarness();
  const update = h.run('previewPage=2; loadPreview()');
  h.run('resetFiles()');
  h.reply(0); await update;
  assert.equal(h.node('document-preview').hidden, true);
  assert.equal(h.run('previewCache.size'), 0);
});

test('dragging an image after the last image keeps the selected preview and updates its position', () => {
  const h = harness(), preview = h.node('preview-image').src;
  h.run('dropImageFile(1,3,true)');
  assert.deepEqual(Array.from(h.run('files.map(item=>item.id)')), [2,3,1]);
  assert.equal(h.run('selectedId'), 1);
  assert.equal(h.node('preview-image').src, preview);
  assert.equal(h.node('preview-page-count').textContent, 'Image 3 of 3');
  assert.equal(h.node('file-list').innerHTML.includes('data-move='), false);
  assert.equal(h.node('file-list').innerHTML.includes('reorder-drag-handle'), true);
});

test('dragging the last image before the first invalidates a result created in the old order', () => {
  const h = harness();
  h.run("resultURL=URL.createObjectURL({result:true}); $('result-panel').hidden=false; dropImageFile(3,1,false);");
  assert.deepEqual(Array.from(h.run('files.map(item=>item.id)')), [3,1,2]);
  assert.equal(h.run('resultURL'), null);
  assert.equal(h.node('result-panel').hidden, true);
});

test('drops on the same image or in its current gap leave the order and result intact', () => {
  const h = harness();
  h.run("resultURL=URL.createObjectURL({result:true}); dropImageFile(1,1,true); dropImageFile(1,2,false);");
  assert.deepEqual(Array.from(h.run('files.map(item=>item.id)')), [1,2,3]);
  assert.notEqual(h.run('resultURL'), null);
});

test('image reordering is disabled while PDF creation is in progress', () => {
  const h = harness();
  h.run('processing=true; dropImageFile(1,3,true); moveImageFile(3,0);');
  assert.deepEqual(Array.from(h.run('files.map(item=>item.id)')), [1,2,3]);
});

test('removing or clearing images releases their thumbnail URLs', () => {
  const h = harness();
  h.run('renderFiles()');
  const removed = h.run('imageThumbURLs.get(2)');
  h.run('files=files.filter(item=>item.id!==2); renderFiles();');
  assert.equal(h.revoked.includes(removed), true);
  h.run('resetFiles()');
  assert.equal(h.run('imageThumbURLs.size'), 0);
});

test('the shared drop calculation preserves distinct copies of the same PDF page', () => {
  const h = harness();
  h.run("activeTool={id:'reorder'}; reorderOrder=[{key:101,page:1},{key:102,page:1},{key:103,page:3}]; dropReorderPage(102,103,true);");
  assert.deepEqual(Array.from(h.run('reorderOrder.map(entry=>entry.key)')), [101,103,102]);
  assert.deepEqual(Array.from(h.run('reorderOrder.map(entry=>entry.page)')), [1,3,1]);
});
