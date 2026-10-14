'use strict';

// Files and edit settings stay in this browser for 24 hours after the last edit.
class LocalDraftStore {
  static retention = 24 * 60 * 60 * 1000;
  constructor({indexedDB=globalThis.indexedDB,now=()=>Date.now(),maxBytes=200*1024*1024,name='paperwork-drafts'}={}) {
    this.indexedDB=indexedDB;this.now=now;this.maxBytes=maxBytes;this.name=name;this.connection=null;
  }
  open() {
    if(!this.indexedDB) return Promise.reject(new Error('Browser storage is unavailable.'));
    if(this.connection) return this.connection;
    this.connection=new Promise((resolve,reject)=>{
      const request=this.indexedDB.open(this.name,1);
      let blocked=false;
      request.onupgradeneeded=()=>request.result.createObjectStore('drafts',{keyPath:'tool'});
      request.onerror=()=>reject(request.error);
      request.onblocked=()=>{blocked=true;reject(new Error('Close other Paperwork tabs to enable saved work.'));};
      request.onsuccess=()=>{
        const db=request.result;
        if(blocked) {db.close();return;}
        db.onversionchange=()=>{db.close();this.connection=null;};
        resolve(db);
      };
    }).catch(error=>{this.connection=null;throw error;});
    return this.connection;
  }
  async transaction(action) {
    const db=await this.open();
    return new Promise((resolve,reject)=>{
      const transaction=db.transaction('drafts','readwrite'), store=transaction.objectStore('drafts');
      let result, failure;
      transaction.oncomplete=()=>resolve(result);
      transaction.onabort=()=>reject(failure || transaction.error || new Error('Saved work could not be updated.'));
      transaction.onerror=()=>{};
      const finish=value=>{result=value;};
      const fail=error=>{failure=error;transaction.abort();};
      try {action(store,finish,fail);} catch(error) {fail(error);}
    });
  }
  valid(draft) {
    return draft && draft.version===1 && typeof draft.tool==='string' && /^[a-z-]+$/.test(draft.tool)
      && Number.isFinite(draft.updatedAt) && Number.isFinite(draft.expiresAt)
      && draft.expiresAt>this.now() && draft.expiresAt===draft.updatedAt+LocalDraftStore.retention
      && Array.isArray(draft.files) && draft.files.length>0 && draft.files.length<=100
      && draft.files.every(file=>file && file.blob instanceof Blob && typeof file.name==='string' && file.blob.size>0)
      && draft.files.reduce((total,file)=>total+file.blob.size,0)<=this.maxBytes;
  }
  metadata(draft) {
    return {tool:draft.tool,updatedAt:draft.updatedAt,expiresAt:draft.expiresAt,count:draft.files.length,
      name:draft.files[0].name,bytes:draft.files.reduce((total,file)=>total+file.blob.size,0)};
  }
  list() {
    return this.transaction((store,finish)=>{
      const request=store.getAll();
      request.onsuccess=()=>{
        const drafts=[];
        for(const draft of request.result) {
          if(this.valid(draft)) drafts.push(this.metadata(draft));
          else store.delete(draft.tool);
        }
        finish(drafts.sort((a,b)=>b.updatedAt-a.updatedAt));
      };
    });
  }
  load(tool) {
    return this.transaction((store,finish)=>{
      const request=store.get(tool);
      request.onsuccess=()=>{
        const draft=request.result;
        if(draft && !this.valid(draft)) store.delete(tool);
        finish(this.valid(draft)?draft:null);
      };
    });
  }
  save(snapshot) {
    const updatedAt=snapshot.updatedAt ?? this.now();
    const options={};
    for(const [name,value] of Object.entries(snapshot.options || {})) {
      if(!/password/i.test(name) && ['string','number','boolean'].includes(typeof value)) options[name]=value;
    }
    const draft={version:1,tool:snapshot.tool,files:snapshot.files.map(file=>({blob:file,name:file.name,type:file.type,lastModified:file.lastModified})),options,
      selectedIndex:snapshot.selectedIndex,previewPage:snapshot.previewPage,
      updatedAt,expiresAt:updatedAt+LocalDraftStore.retention};
    if(!this.valid(draft)) return Promise.reject(new Error('This work cannot be saved in the browser.'));
    return this.transaction((store,finish)=>{
      const request=store.getAll();
      request.onsuccess=()=>{
        const others=[],evicted=[];
        for(const previous of request.result) {
          if(!this.valid(previous)) store.delete(previous.tool);
          else if(previous.tool!==draft.tool) others.push(previous);
        }
        let total=this.metadata(draft).bytes+others.reduce((sum,item)=>sum+this.metadata(item).bytes,0);
        for(const previous of others.sort((a,b)=>a.updatedAt-b.updatedAt)) {
          if(total<=this.maxBytes) break;
          store.delete(previous.tool);total-=this.metadata(previous).bytes;evicted.push(previous.tool);
        }
        store.put(draft);
        finish({draft:this.metadata(draft),evicted});
      };
    });
  }
  remove(tool) { return this.transaction((store,finish)=>{store.delete(tool);finish();}); }
  clear() { return this.transaction((store,finish)=>{store.clear();finish();}); }
}

const tools = JSON.parse(document.getElementById('tool-catalog').textContent);
const appConfig = JSON.parse(document.getElementById('app-config')?.textContent || '{}');
const hosted = appConfig.mode==='hosted';
const appTitle = hosted?'Paperwork - Free Online PDF Tools | Merge, Split & Delete Pages':'Paperwork - Local PDF Studio';
const $ = id => document.getElementById(id);
const icon = name => `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const categoryNames = {all:'All tools',convert:'Convert',organize:'Organize',optimize:'Optimize',personalize:'Personalize',security:'Security'};
const maxBytes = Number(appConfig.max_upload_bytes) || 200 * 1024 * 1024;
let activeTool = null;
let category = 'all';
let files = [];
let fileId = 0;
let selectedId = null;
let inspectingVersion = 0;
let inspectController = null;
let previewController = null;
let passwordTimer = null;
let imagePreviewTimer = null;
let imagePreviewVersion = 0;
const previewCache = new Map();
let previewURL = null;
let resultURL = null;
let extractedText = '';
let textCopyTimer = null;
let previewPage = 1;
let processing = false;
let optionDirty = new Set();
let thumbnailController = null;
let reorderFileId = null;
let reorderPageCount = 0;
let reorderOrder = [];
let reorderKey = 0;
let reorderImages = new Map();
let reorderErrors = new Map();
let reorderLoading = false;
let deleteFileId = null;
let deletePageCount = 0;
let deleteSelection = new Set();
const deleteImages = new Map();
const deleteErrors = new Map();
let deleteLoading = false;
let deleteSelectionError = '';
let pageReorderDrag = null;
let imageReorderDrag = null;
const imageThumbURLs = new Map();
let draftStore = null;
let savedDrafts = new Map();
let draftSaveTimer = null;
let draftExpiryTimer = null;
let draftWriteQueue = Promise.resolve();
let pendingDraft = null;
let draftRevision = 0;
let draftRestoreVersion = 0;
let restoredPageOrder = null;
let restoredPreviewPage = 1;
let savedWorkMessage = '';
let draftWritesInFlight = 0;

function announce(text) { $('live-status').textContent = text; }
function bytes(size) { if(size < 1024) return `${size} B`; if(size < 1048576) return `${(size/1024).toFixed(1)} KB`; return `${(size/1048576).toFixed(1)} MB`; }
function setError(text) { $('error-message').textContent = text; $('error-message').hidden = !text; if(text) announce(text); }
function clearResult() { if(resultURL) URL.revokeObjectURL(resultURL); resultURL = null; extractedText=''; clearTimeout(textCopyTimer); $('result-panel').classList.remove('has-text-result'); $('text-result').hidden=true; $('extracted-text').value=''; $('copy-text').textContent='Copy text'; $('result-panel').hidden = true; $('download-button').removeAttribute('href'); }
function cancelInspect() { inspectingVersion++; inspectController?.abort(); previewController?.abort(); thumbnailController?.abort(); clearTimeout(passwordTimer); clearTimeout(imagePreviewTimer); finishPageDrag(); finishImageDrag(); }
function clearPreview() { if(previewURL) URL.revokeObjectURL(previewURL); previewURL=null; $('preview-image').removeAttribute('src'); $('preview-image').hidden=true; $('document-preview').hidden=true; $('document-preview').setAttribute('aria-busy','false'); }
function resetFiles() { draftRevision++;draftRestoreVersion++;restoredPageOrder=null;restoredPreviewPage=1;cancelInspect(); files=[]; selectedId=null; clearImageThumbs(); previewCache.clear(); clearPreview(); clearReorder(); clearDelete(); clearResult(); setError(''); renderFiles(); }

function renderTools() {
  $('category-tabs').querySelector('[data-category="all"] span').textContent=tools.length;
  const query = $('tool-search').value.trim().toLowerCase();
  const matches = tools.filter(tool => (category === 'all' || tool.category === category) && `${tool.name} ${tool.description} ${tool.category}`.toLowerCase().includes(query));
  const grid=$('tool-grid');
  const cards=Array.from(grid.children);
  // Keep the initial HTML cards in place; update only when the visible tools change.
  if(cards.length!==matches.length || cards.some((card,index)=>card.dataset.tool!==matches[index].id)) {
    grid.innerHTML = matches.map(tool => `<button class="tool-card" data-tool="${tool.id}"><span class="tool-icon ${tool.category}">${icon(tool.icon)}</span>${icon('arrow').replace('class="icon"','class="icon card-arrow"')}<h3>${tool.name}</h3><p>${tool.description}</p></button>`).join('');
  }
  $('no-tools').hidden = matches.length > 0;
  document.querySelectorAll('[data-category]').forEach(button => { const selected=button.dataset.category===category; button.classList.toggle('active',selected); button.setAttribute('aria-pressed',String(selected)); });
  $('tools-heading').textContent = query ? `Results for “${$('tool-search').value.trim()}”` : category === 'all' ? 'PDF tools' : `${categoryNames[category]} tools`;
  $('tools-description').textContent = `${matches.length} ${matches.length === 1 ? 'tool' : 'tools'}`;
}

function field(name,label,type='text',value='',help='',attrs='') {
  return `<div class="field"><label for="option-${name}">${label}</label><input id="option-${name}" name="${name}" type="${type}" value="${escapeHTML(value)}" ${attrs}>${help ? `<p class="field-help">${help}</p>` : ''}</div>`;
}
function select(name,label,options,value,help='') {
  return `<div class="field"><label for="option-${name}">${label}</label><select id="option-${name}" name="${name}">${options.map(([v,l])=>`<option value="${v}"${v===value?' selected':''}>${l}</option>`).join('')}</select>${help?`<p class="field-help">${help}</p>`:''}</div>`;
}
function checkbox(name,label,checked) { return `<label class="checkbox-field"><input name="${name}" type="checkbox"${checked?' checked':''}>${label}</label>`; }
function pages(label='Pages to include',value='all',help='Use all, or a selection such as 1,3-5.') { return field('pages',label,'text',value,help,'placeholder="all or 1,3-5" required'); }
function optionsFor(tool) {
  switch(tool.id) {
    case 'images-to-pdf': return select('page_size','Page size',[['auto','Fit each image'],['a4','A4'],['letter','US Letter']],'auto') + `<div id="fixed-page-options">${select('orientation','Orientation',[['portrait','Portrait'],['landscape','Landscape']],'portrait')}${field('margin','Margin (points)','number',24,'Applies to A4 and US Letter pages.','min="0" max="144" step="1" required')}</div>`;
    case 'pdf-to-images': return select('format','Image format',[['png','PNG — sharp and lossless'],['jpg','JPG — smaller files']],'png') + select('dpi','Resolution',[['72','72 DPI — screen size'],['144','144 DPI — recommended'],['200','200 DPI — more detail'],['300','300 DPI — high detail']],'144') + pages();
    case 'merge': return '<p class="field-help">Choose two or more PDFs. Every page is included, in the file order shown on the left.</p>';
    case 'split': return select('mode','Split method',[['each','One PDF per page'],['chunks','Groups of pages'],['ranges','Custom page ranges']],'each') + `<div id="split-chunks">${field('chunk_size','Pages per file','number',1,'The last file may have fewer pages.','min="1" max="10000" step="1" required')}</div><div id="split-ranges">${field('ranges','Page ranges','text','','Separate output files with a semicolon. Example: 1-3;4-6;7','placeholder="1-3;4-6;7"')}</div>`;
    case 'extract': return pages('Pages to keep','all');
    case 'reorder': return '<p class="reorder-summary" id="reorder-summary">Choose a PDF to arrange its pages.</p><p class="field-help">Arrange the page previews, then save your reordered PDF.</p><details class="reorder-advanced"><summary>Enter page numbers instead</summary>' + field('pages','New page order','text','','Example: 3,1,2. Repeat a number to duplicate that page. Leave blank to include every page.','placeholder="1,2,3"') + '</details>';
    case 'delete': return '<p class="reorder-summary" id="delete-summary">Choose a PDF to see all its pages.</p><p class="field-help">Click × on any page preview to mark it for removal. Restore a page if you change your mind, then choose Delete pages to save.</p>' + field('pages','Pages to remove','text','','You can also enter page numbers, such as 2,4-6. At least one page must remain.','placeholder="Select pages in the preview" maxlength="20000"');
    case 'rotate': return select('angle','Rotate clockwise',[['90','90° — quarter turn'],['180','180° — upside down'],['270','270° — three-quarter turn']],'90') + pages('Pages to rotate');
    case 'compress': return select('level','Compression level',[['lossless','Lossless — keep image quality'],['balanced','Balanced — smaller, good quality'],['strong','Strong — prioritize file size']],'balanced','Balanced and strong may reduce image quality. Lossless savings may be modest.') + '<p class="field-help">Selectable text and page layout are preserved.</p>';
    case 'watermark': return field('text','Watermark text','text','CONFIDENTIAL','English / Latin text.','maxlength="200" required') + select('position','Position',[['center','Center'],['top','Top'],['bottom','Bottom']],'center') + '<div class="field-grid">' + field('font_size','Font size','number',42,'','min="8" max="160" required') + field('opacity','Opacity','number',0.22,'0 = invisible · 1 = opaque','min="0.01" max="1" step="0.01" required') + '</div>' + field('color','Text color','color','#64748b') + pages();
    case 'page-numbers': return pages('Pages to number') + '<div class="field-grid">' + field('start','Start at','number',1,'','min="1" max="1000000" required') + field('font_size','Font size','number',11,'','min="6" max="48" required') + '</div>' + select('position','Position',[['left','Bottom left'],['center','Bottom center'],['right','Bottom right']],'center') + field('margin','Bottom margin (points)','number',24,'','min="5" max="144" required') + '<div class="field-grid">' + field('prefix','Prefix','text','','Example: Page ','maxlength="60" placeholder="Page "') + field('suffix','Suffix','text','','Optional Latin text','maxlength="60"') + '</div>';
    case 'protect': return field('user_password','New opening password','password','','Required. Maximum 40 UTF-8 bytes.','required autocomplete="new-password"') + field('owner_password','Owner password (optional)','password','','Generated automatically when blank. Maximum 40 UTF-8 bytes.','autocomplete="new-password"') + '<label>Allow these actions</label>' + checkbox('allow_print','Printing',true) + checkbox('allow_copy','Copying text and images',false) + checkbox('allow_modify','Editing the document',false);
    case 'unlock': return '<p class="field-help">The output will open without a password. Enter the existing password in the PDF password field below.</p>';
    case 'extract-text': return select('format','Output format',[['txt','Plain text (.txt)'],['md','Markdown (.md) — keeps tables'],['json','Structured text by page (.json)']],'txt') + select('text_layout','Text layout',[['readable','Readable — columns & tables'],['simple','Simple — original lines, faster']],'readable') + pages('Pages to include','all','Use all, or a selection such as 1,3-5. Up to 500 pages at a time.');
    case 'extract-images': return pages();
    default: return '';
  }
}

function syncOptionVisibility() {
  if(activeTool?.id==='extract-text') {
    const markdown=$('option-format').value==='md';
    if(markdown) $('option-text_layout').value='readable';
    $('option-text_layout').disabled=markdown || processing;
  }
  if(activeTool?.id==='images-to-pdf') {
    const automatic=$('option-page_size').value==='auto';
    $('fixed-page-options').hidden=automatic;
    $('fixed-page-options').querySelectorAll('input,select').forEach(input=>input.disabled=automatic || processing);
  }
  if(activeTool?.id==='split') {
    const mode=$('option-mode').value;
    $('split-chunks').hidden=mode!=='chunks';
    $('split-ranges').hidden=mode!=='ranges';
    $('option-ranges').required=mode==='ranges';
    $('option-ranges').disabled=mode!=='ranges' || processing;
    $('option-chunk_size').disabled=mode!=='chunks' || processing;
  }
}

function openTool(id,updateHash=true,restore=true) {
  const tool=tools.find(item=>item.id===id);
  if(!tool) { showHome(updateHash); return; }
  if(processing) return;
  flushDraftSave();
  resetFiles();
  activeTool=tool;
  optionDirty=new Set();
  $('home-view').hidden=true;
  $('workspace-view').hidden=false;
  $('workspace-title').textContent=tool.name;
  $('workspace-description').textContent=tool.description;
  $('workspace-icon').className=`tool-icon ${tool.category}`;
  $('workspace-icon').innerHTML=icon(tool.icon);
  $('tool-options').innerHTML=optionsFor(tool);
  $('tool-tip-text').textContent=tool.tip;
  $('file-input').accept=tool.accept || '.pdf,application/pdf';
  $('file-input').multiple=Boolean(tool.multiple);
  $('drop-title').textContent=tool.id==='images-to-pdf'?'Drop your images here':tool.multiple?'Drop your PDFs here':'Drop your PDF here';
  $('drop-subtitle').textContent=tool.multiple?'or click to choose files':'or click to choose a file';
  $('drop-hint').textContent=`${tool.id==='images-to-pdf'?'JPG, PNG, WebP, TIFF & more':'PDF files'} · up to ${maxBytes / 1048576} MB total`;
  $('password-section').hidden=['images-to-pdf','merge','split'].includes(tool.id);
  $('input-password').value='';
  $('process-label').textContent=tool.action;
  $('processing-note').textContent='Your original files remain unchanged.';
  document.title=`${tool.name} - Paperwork`;
  syncOptionVisibility();
  renderFiles();
  if(updateHash) history.pushState(null,'',`#${id}`);
  window.scrollTo({top:0,behavior:'instant'});
  announce(`${tool.name} tool opened. Choose your files to begin.`);
  renderSavedWork();
  if(restore && draftStore) restoreToolDraft(id);
}

function showHome(updateHash=true) {
  if(processing) return;
  flushDraftSave();
  resetFiles();
  activeTool=null;
  $('workspace-view').hidden=true;
  $('home-view').hidden=false;
  $('input-password').value='';
  document.title=appTitle;
  if(updateHash) history.pushState(null,'',location.pathname);
  renderTools();
  window.scrollTo({top:0,behavior:'instant'});
  renderSavedWork();
}

function clearImageThumbs() {
  imageThumbURLs.forEach(url=>URL.revokeObjectURL(url));
  imageThumbURLs.clear();
}

function imageThumbnail(item) {
  if(!imageThumbURLs.has(item.id)) imageThumbURLs.set(item.id,URL.createObjectURL(item.file));
  return `<img src="${escapeHTML(imageThumbURLs.get(item.id))}" alt="Preview of ${escapeHTML(item.file.name)}" loading="lazy" decoding="async" draggable="false"><span class="image-thumb-fallback" hidden>${icon('image')}<span>Preview unavailable</span></span>`;
}

function renderFiles(focusImageId=null) {
  const imageTool=activeTool?.id==='images-to-pdf';
  const list=$('file-list');
  list.classList.toggle('image-reorder-grid',imageTool && files.length>0);
  $('image-order-help').hidden=!imageTool || files.length===0;
  $('image-order-keyboard-help').hidden=!imageTool || files.length===0;
  if(imageTool) {
    list.setAttribute('role','list');
    list.setAttribute('aria-label','Images in PDF order');
    list.setAttribute('aria-describedby','image-order-keyboard-help');
    const present=new Set(files.map(item=>item.id));
    imageThumbURLs.forEach((url,id)=>{if(!present.has(id)){URL.revokeObjectURL(url);imageThumbURLs.delete(id);}});
    list.innerHTML=files.map((item,index)=>`<div class="reorder-page image-card${item.id===selectedId?' selected':''}" data-image-key="${item.id}" role="listitem" tabindex="0" aria-label="${escapeHTML(item.file.name)}, position ${index+1} of ${files.length}" draggable="${!processing && files.length>1}"><button class="icon-button reorder-drag-handle" type="button" aria-label="Drag ${escapeHTML(item.file.name)}, position ${index+1}" title="Drag to move this image"${processing || files.length<2?' disabled':''}>${icon('grid')}</button><div class="reorder-thumb" data-view="${item.id}">${imageThumbnail(item)}</div><div class="reorder-page-footer"><strong class="image-file-name" title="${escapeHTML(item.file.name)}">${escapeHTML(item.file.name)}</strong><span class="reorder-position">Position ${index+1} · ${bytes(item.file.size)}</span><div class="reorder-page-actions"><button class="icon-button view-file" type="button" data-view="${item.id}" aria-label="Preview ${escapeHTML(item.file.name)}">${icon('search')}</button><button class="icon-button remove-file" type="button" data-remove="${item.id}" aria-label="Remove ${escapeHTML(item.file.name)}">${icon('close')}</button></div></div></div>`).join('');
  } else {
    clearImageThumbs();
    list.removeAttribute('role');
    list.removeAttribute('aria-label');
    list.removeAttribute('aria-describedby');
    list.innerHTML=files.map((item,index)=>`<div class="file-row${item.id===selectedId?' selected':''}"><span class="tool-icon">${icon('file')}</span><div class="file-info"><strong title="${escapeHTML(item.file.name)}">${escapeHTML(item.file.name)}</strong><small>${bytes(item.file.size)}${item.info?.page_count?` · ${item.info.page_count} ${item.info.page_count===1?'page':'pages'}`:''}${item.info?.encrypted?' · Encrypted':''}</small></div><div class="file-actions">${files.length>1?`<button class="icon-button view-file" data-view="${item.id}" aria-label="Preview ${escapeHTML(item.file.name)}">View</button><button class="icon-button" data-move="${item.id}" data-direction="-1" aria-label="Move ${escapeHTML(item.file.name)} up"${index===0?' disabled':''}>${icon('up')}</button><button class="icon-button" data-move="${item.id}" data-direction="1" aria-label="Move ${escapeHTML(item.file.name)} down"${index===files.length-1?' disabled':''}>${icon('down')}</button>`:''}<button class="icon-button remove-file" data-remove="${item.id}" aria-label="Remove ${escapeHTML(item.file.name)}">${icon('close')}</button></div></div>`).join('');
  }
  if(activeTool?.id==='merge') $('file-list').querySelectorAll('.view-file').forEach(button=>button.remove());
  $('drop-zone').classList.toggle('compact',files.length>0);
  const pageGridTool=['reorder','delete'].includes(activeTool?.id);
  $('drop-zone').hidden=pageGridTool && files.length>0;
  if(pageGridTool && files.length) {
    const change=document.createElement('button');
    change.type='button';change.className='text-button change-file-button';change.dataset.changeFile='true';
    change.textContent='Change';change.setAttribute('aria-label','Choose another PDF');
    $('file-list').querySelector('.file-actions').prepend(change);
  }
  $('clear-files').hidden=files.length===0;
  $('add-files').hidden=!activeTool?.multiple || files.length===0;
  $('process-button').disabled=processing || files.length===0 || (activeTool?.id==='reorder' && !reorderPageCount);
  if(activeTool?.id==='delete') syncDeleteControls();
  document.querySelectorAll('.file-actions button').forEach(button=>{if(processing) button.disabled=true;});
  if(imageTool) {
    syncImagePreviewNavigation();
    list.querySelectorAll('button').forEach(button=>{if(processing) button.disabled=true;});
    if(focusImageId!==null) list.querySelector(`[data-image-key="${focusImageId}"]`)?.focus({preventScroll:true});
  }
}

function moveImageFile(id,destination) {
  if(processing || activeTool?.id!=='images-to-pdf') return;
  const source=files.findIndex(item=>item.id===id);
  destination=Math.max(0,Math.min(files.length-1,destination));
  if(source<0 || source===destination) return;
  const [item]=files.splice(source,1);
  files.splice(destination,0,item);
  clearResult();setError('');renderFiles(id);
  queueDraftSave(true);
  announce(`${item.file.name} moved to position ${destination+1} of ${files.length}.`);
}

function addFiles(incoming) {
  if(!activeTool || processing) return;
  const next=Array.from(incoming);
  if(!next.length) return;
  const extensions=activeTool.id==='images-to-pdf'?/\.(jpe?g|png|webp|tiff?|bmp|gif)$/i:/\.pdf$/i;
  if(next.some(file=>!extensions.test(file.name))) {setError(activeTool.id==='images-to-pdf'?'Please choose JPG, PNG, WebP, TIFF, BMP, or GIF images.':'Please choose PDF files.');return;}
  if(next.some(file=>file.size===0)) {setError('An empty file cannot be processed. Please select a file with content.');return;}
  if(!activeTool.multiple && next.length>1) {setError('This tool handles one PDF at a time. Please select one file.');return;}
  const total=next.reduce((n,file)=>n+file.size,0)+(activeTool.multiple?files.reduce((n,item)=>n+item.file.size,0):0);
  if(total>maxBytes) {setError(`The selected files exceed ${maxBytes / 1048576} MB in total. Choose fewer or smaller files.`);return;}
  cancelInspect();
  clearResult();
  setError('');
  const additions=next.map(file=>({id:++fileId,file,info:null}));
  if(activeTool.multiple) files.push(...additions); else files=additions;
  selectedId=additions[0].id;
  renderFiles();
  inspectSelected();
  queueDraftSave(true);
  announce(`${next.length} ${next.length===1?'file':'files'} added.`);
}

async function responseError(response) {
  const type=response.headers.get('Content-Type') || '';
  if(type.includes('json')) {try {const body=await response.json(); return typeof body.error==='string'?body.error:typeof body.detail==='string'?body.detail:body.message || `The PDF server returned an error (${response.status}).`;}catch(_){}}
  return `The PDF server returned an error (${response.status}). Please try again.`;
}

function previewMessage(text) { $('preview-image').hidden=true; $('preview-placeholder').hidden=false; $('preview-placeholder').querySelector('span').textContent=text; }

function clearDelete() {
  deleteFileId=null;
  deletePageCount=0;
  deleteSelection.clear();
  deleteImages.clear();
  deleteErrors.clear();
  deleteLoading=false;
  deleteSelectionError='';
  $('delete-panel').hidden=true;
  $('delete-pages').replaceChildren();
  $('delete-pages').setAttribute('aria-busy','false');
  if(activeTool?.id==='delete' && $('delete-summary')) {
    $('delete-summary').textContent='Choose a PDF to see all its pages.';
    $('option-pages').value='';
    $('option-pages').setCustomValidity('');
    optionDirty.delete('pages');
  }
}

function beginDelete(item) {
  if(deleteFileId!==item.id) clearDelete();
  deleteFileId=item.id;
  deletePageCount=0;
  deleteImages.clear();
  deleteErrors.clear();
  deleteLoading=false;
  $('delete-panel').hidden=false;
  $('delete-pages').replaceChildren();
  $('delete-reset').disabled=true;
  $('delete-status').textContent='Reading your PDF…';
  $('delete-summary').textContent='Reading your PDF…';
  renderFiles();
}

function deleteThumbnail(page) {
  const image=deleteImages.get(page);
  if(image) return `<img src="${escapeHTML(image)}" alt="Preview of page ${page}" loading="lazy" decoding="async" draggable="false">`;
  return `<span>${icon('file')}<span>${deleteErrors.has(page)?'Preview unavailable':deleteLoading?'Loading preview…':'Preview not loaded'}</span></span>`;
}

function syncDeleteControls() {
  $('delete-panel').classList.toggle('is-busy',processing);
  $('delete-reset').disabled=processing || (!deleteSelection.size && !$('option-pages')?.value);
  const lastPage=deletePageCount-deleteSelection.size<=1;
  $('delete-pages').querySelectorAll('[data-delete-page]').forEach(button=>{
    const page=Number(button.dataset.deletePage), removed=deleteSelection.has(page);
    button.disabled=processing || (!removed && lastPage);
    button.setAttribute('aria-label',`${removed?'Restore':'Remove'} page ${page}`);
    button.title=removed?'Restore this page':lastPage?'At least one page must remain':'Remove this page';
    button.innerHTML=icon(removed?'back':'close');
    const card=button.closest('[data-page-number]');
    card.classList.toggle('is-removed',removed);
    card.setAttribute('aria-label',`Page ${page}, ${removed?'marked for removal':'keep'}`);
    card.querySelector('.delete-page-state').textContent=removed?'Will be removed':'Keep page';
  });
  if(activeTool?.id==='delete') {
    $('process-button').disabled=processing || !files.length || !deletePageCount || !deleteSelection.size || !!deleteSelectionError;
    if(deletePageCount) $('delete-summary').textContent=deleteSelectionError?'Check the page numbers below.':deleteSelection.size?`${deleteSelection.size} ${deleteSelection.size===1?'page':'pages'} to remove · ${deletePageCount-deleteSelection.size} to keep.`:`All ${deletePageCount} ${deletePageCount===1?'page':'pages'} will be kept. Select pages to remove.`;
  }
}

function updateDeleteStatus(message='') {
  const loaded=deleteImages.size+deleteErrors.size;
  $('delete-pages').setAttribute('aria-busy',String(deleteLoading));
  $('delete-status').textContent=message || (deleteLoading?`Loading page previews… ${loaded} of ${deletePageCount}`:loaded<deletePageCount?`${loaded} of ${deletePageCount} previews available. You can still select pages by number.`:deleteErrors.size?`${deleteErrors.size} page ${deleteErrors.size===1?'preview is':'previews are'} unavailable. You can still select every page.`:deletePageCount===1?'This PDF has one page. At least one page must remain.':`${deletePageCount} pages · Click × to remove a page. Use the restore arrow to undo.`);
}

function initializeDelete(count) {
  deletePageCount=count;
  deleteLoading=true;
  $('delete-pages').innerHTML=Array.from({length:count},(_,index)=>{
    const page=index+1;
    return `<div class="reorder-page delete-page is-loading" data-page-number="${page}" role="listitem"><div class="reorder-thumb">${deleteThumbnail(page)}</div><button class="icon-button delete-page-toggle" type="button" data-delete-page="${page}" aria-label="Remove page ${page}">${icon('close')}</button><div class="delete-page-footer"><strong class="reorder-page-label">Page ${page}</strong><span class="delete-page-state">Keep page</span></div></div>`;
  }).join('');
  syncDeleteFromInput();
  renderFiles();
}

function readDeleteSelection(value,count) {
  if(value.length>20000) throw new Error('Page selection is too long.');
  if(!value.trim()) return new Set();
  const selection=new Set();
  if(value.trim().toLowerCase()==='all') throw new Error('At least one page must remain in the PDF.');
  for(const part of value.split(',')) {
    const match=part.match(/^\s*(\d+)\s*(?:-\s*(\d+)\s*)?$/);
    if(!match) throw new Error('Use page numbers and ranges such as 1,3-5.');
    const first=Number(match[1]), last=Number(match[2] || match[1]);
    if(first<1 || last>count || last<first) throw new Error(`Choose forward page ranges between 1 and ${count}.`);
    for(let page=first;page<=last;page++) {
      if(selection.has(page)) throw new Error('A page was selected more than once.');
      selection.add(page);
    }
  }
  if(selection.size>=count) throw new Error('At least one page must remain in the PDF.');
  return selection;
}

function syncDeleteFromInput(showError=false) {
  if(!deletePageCount) return false;
  try {
    deleteSelection=readDeleteSelection($('option-pages').value,deletePageCount);
    deleteSelectionError='';
  } catch(error) {
    deleteSelectionError=error.message;
    if(showError) setError(error.message);
  }
  $('option-pages').setCustomValidity(deleteSelectionError);
  syncDeleteControls();
  return !deleteSelectionError;
}

function writeDeleteSelection() {
  const sorted=Array.from(deleteSelection).sort((a,b)=>a-b), ranges=[];
  for(let index=0;index<sorted.length;index++) {
    const first=sorted[index];
    while(sorted[index+1]===sorted[index]+1) index++;
    ranges.push(first===sorted[index]?String(first):`${first}-${sorted[index]}`);
  }
  $('option-pages').value=ranges.join(',');
  $('option-pages').setCustomValidity('');
  deleteSelectionError='';
  optionDirty.add('pages');
  clearResult();setError('');syncDeleteControls();
  queueDraftSave(true);
}

function toggleDeletePage(page) {
  if(processing || activeTool?.id!=='delete' || !Number.isInteger(page) || page<1 || page>deletePageCount || !syncDeleteFromInput(true)) return;
  const restoring=deleteSelection.has(page);
  if(restoring) deleteSelection.delete(page);
  else {
    if(deletePageCount-deleteSelection.size<=1) {setError('At least one page must remain in the PDF.');return;}
    deleteSelection.add(page);
  }
  writeDeleteSelection();
  announce(`Page ${page} ${restoring?'restored':'marked for removal'}. ${deletePageCount-deleteSelection.size} ${deletePageCount-deleteSelection.size===1?'page remains':'pages remain'}.`);
}

function clearReorder() {
  finishPageDrag();
  reorderFileId=null;
  reorderPageCount=0;
  reorderOrder=[];
  reorderImages.clear();
  reorderErrors.clear();
  reorderLoading=false;
  $('reorder-panel').hidden=true;
  $('reorder-pages').replaceChildren();
  $('reorder-pages').setAttribute('aria-busy','false');
  if(activeTool?.id==='reorder' && $('reorder-summary')) {
    $('reorder-summary').textContent='Choose a PDF to arrange its pages.';
    $('option-pages').value='';
    optionDirty.delete('pages');
  }
}

function beginReorder(item) {
  if(reorderFileId!==item.id) {
    restoredPageOrder=null;
    reorderOrder=[];
    optionDirty.delete('pages');
    $('option-pages').value='';
  }
  reorderFileId=item.id;
  reorderPageCount=0;
  reorderImages.clear();
  reorderErrors.clear();
  reorderLoading=false;
  $('reorder-panel').hidden=false;
  $('reorder-pages').replaceChildren();
  $('reorder-reset').disabled=true;
  $('reorder-status').textContent='Reading your PDF…';
  $('reorder-summary').textContent='Reading your PDF…';
  renderFiles();
}

function pageThumbnail(page) {
  const image=reorderImages.get(page);
  if(image) return `<img src="${escapeHTML(image)}" alt="Preview of page ${page}" loading="lazy" decoding="async" draggable="false">`;
  return `<span>${icon('file')}<span>${reorderErrors.has(page)?'Preview unavailable':reorderLoading?'Loading preview…':'Preview not loaded'}</span></span>`;
}

function syncReorderControls() {
  $('reorder-panel').classList.toggle('is-busy',processing);
  $('reorder-reset').disabled=processing || !reorderPageCount;
  $('reorder-pages').querySelectorAll('.reorder-page').forEach((card,index)=>{
    card.draggable=!processing;
    card.querySelectorAll('[data-page-move]').forEach(button=>{
      const direction=Number(button.dataset.pageMove);
      button.disabled=processing || index+direction<0 || index+direction>=reorderOrder.length;
    });
    card.querySelector('.reorder-drag-handle').disabled=processing;
  });
}

function updateReorderStatus(message='') {
  if(activeTool?.id!=='reorder') return;
  const loaded=reorderImages.size+reorderErrors.size;
  $('reorder-pages').setAttribute('aria-busy',String(reorderLoading));
  $('reorder-status').textContent=message || (reorderLoading?`Loading page previews… ${loaded} of ${reorderPageCount}`:loaded<reorderPageCount?`${loaded} of ${reorderPageCount} previews available.`:reorderErrors.size?`${reorderErrors.size} page ${reorderErrors.size===1?'preview is':'previews are'} unavailable. You can still arrange every page.`:`${reorderPageCount} ${reorderPageCount===1?'page':'pages'} · Saved from left to right, top to bottom.`);
}

function renderReorder(focusKey=null) {
  $('reorder-pages').innerHTML=reorderOrder.map((entry,index)=>`<div class="reorder-page${!reorderImages.has(entry.page)?reorderErrors.has(entry.page)?' is-error':reorderLoading?' is-loading':'':''}" data-page-key="${entry.key}" data-page-number="${entry.page}" role="listitem" tabindex="0" aria-label="Page ${entry.page}, position ${index+1} of ${reorderOrder.length}" draggable="${!processing}"><button class="icon-button reorder-drag-handle" type="button" aria-label="Drag page ${entry.page}, position ${index+1}" title="Drag to move this page">${icon('grid')}</button><div class="reorder-thumb">${pageThumbnail(entry.page)}</div><div class="reorder-page-footer"><strong class="reorder-page-label">Page ${entry.page}</strong><span class="reorder-position">Position ${index+1}</span><div class="reorder-page-actions"><button class="icon-button" type="button" data-page-move="-1" aria-label="Move page ${entry.page} earlier, position ${index+1}" title="Move earlier">${icon('back')}</button><button class="icon-button" type="button" data-page-move="1" aria-label="Move page ${entry.page} later, position ${index+1}" title="Move later">${icon('arrow')}</button></div></div></div>`).join('');
  $('option-pages').value=reorderOrder.map(entry=>entry.page).join(',');
  $('reorder-summary').textContent=`${reorderOrder.length} ${reorderOrder.length===1?'page':'pages'} in your new order.`;
  syncReorderControls();
  if(focusKey!==null) $('reorder-pages').querySelector(`[data-page-key="${focusKey}"]`)?.focus({preventScroll:true});
}

function initializeReorder(count) {
  reorderPageCount=count;
  reorderLoading=true;
  let invalidRestoredOrder=null;
  if(restoredPageOrder!==null) {
    const value=restoredPageOrder;restoredPageOrder=null;
    try {reorderOrder=readPageOrder(value,count).map(page=>({key:++reorderKey,page}));}
    catch(error) {invalidRestoredOrder=value;setError(error.message);}
  }
  if(!reorderOrder.length || reorderOrder.some(entry=>entry.page>count)) reorderOrder=Array.from({length:count},(_,index)=>({key:++reorderKey,page:index+1}));
  renderReorder();
  if(invalidRestoredOrder!==null) $('option-pages').value=invalidRestoredOrder;
  renderFiles();
}

function readPageOrder(value,count) {
  if(value.length>20000) throw new Error('The page order is too long.');
  if(!value.trim() || value.trim().toLowerCase()==='all') return Array.from({length:count},(_,index)=>index+1);
  const result=[];
  for(const part of value.split(',')) {
    const match=part.match(/^\s*(\d+)\s*(?:-\s*(\d+)\s*)?$/);
    if(!match) throw new Error('Use page numbers such as 3,1,2 or ranges such as 1-3.');
    const first=Number(match[1]), last=Number(match[2] || match[1]);
    if(first<1 || last>count || last<first) throw new Error(`Choose pages between 1 and ${count}. To reverse pages, enter them separately.`);
    for(let page=first;page<=last;page++) {
      result.push(page);
      if(result.length>3000) throw new Error('The result cannot contain more than 3000 pages.');
    }
  }
  return result;
}

function syncReorderFromInput() {
  if(!reorderPageCount) return false;
  try {
    const order=readPageOrder($('option-pages').value,reorderPageCount);
    if(order.join(',')!==reorderOrder.map(entry=>entry.page).join(',')) {
      reorderOrder=order.map(page=>({key:++reorderKey,page}));
      renderReorder();
      clearResult();
    }
    $('option-pages').value=order.join(',');
    return true;
  } catch(error) {setError(error.message);return false;}
}

function moveReorderPage(key,destination) {
  if(processing) return;
  const source=reorderOrder.findIndex(entry=>entry.key===key);
  destination=Math.max(0,Math.min(reorderOrder.length-1,destination));
  if(source<0 || source===destination) return;
  const [entry]=reorderOrder.splice(source,1);
  reorderOrder.splice(destination,0,entry);
  optionDirty.add('pages');
  clearResult();
  setError('');
  renderReorder(key);
  queueDraftSave(true);
  announce(`Page ${entry.page} moved to position ${destination+1} of ${reorderOrder.length}.`);
}

function reorderDestination(keys,sourceKey,targetKey,after) {
  const source=keys.indexOf(sourceKey), target=keys.indexOf(targetKey);
  if(source<0 || target<0 || source===target) return null;
  const insertion=target+(after?1:0);
  return insertion-(source<insertion?1:0);
}

function dropReorderPage(sourceKey,targetKey,after) {
  const destination=reorderDestination(reorderOrder.map(entry=>entry.key),sourceKey,targetKey,after);
  if(destination!==null) moveReorderPage(sourceKey,destination);
}

function dropImageFile(sourceId,targetId,after) {
  const destination=reorderDestination(files.map(item=>item.id),sourceId,targetId,after);
  if(destination!==null) moveImageFile(sourceId,destination);
}

function finishPageDrag() { pageReorderDrag?.finish(); }
function finishImageDrag() { imageReorderDrag?.finish(); }

function createReorderDragController({container,selector,keyProperty,getKeys,onMove,onDrop,isEnabled}) {
  let draggedKey=null, pointerDrag=null, scrollFrame=null;
  const findCard=target=>{
    const card=target?.closest(selector);
    return card && container.contains(card)?card:null;
  };
  const keyOf=card=>Number(card.dataset[keyProperty]);
  const afterCard=(card,x)=>{const rect=card.getBoundingClientRect();return x>rect.left+rect.width/2;};
  function showTarget(card,after) {
    container.querySelectorAll('.drop-before,.drop-after').forEach(node=>node.classList.remove('drop-before','drop-after'));
    if(card && keyOf(card)!==draggedKey) card.classList.add(after?'drop-after':'drop-before');
  }
  function finish() {
    if(scrollFrame!==null) cancelAnimationFrame(scrollFrame);
    scrollFrame=null;
    const capture=pointerDrag;
    pointerDrag=null;draggedKey=null;
    if(capture?.handle.isConnected && capture.handle.hasPointerCapture(capture.id)) capture.handle.releasePointerCapture(capture.id);
    container.querySelectorAll('.dragging,.drop-before,.drop-after').forEach(card=>card.classList.remove('dragging','drop-before','drop-after'));
  }
  function trackTarget() {
    if(!pointerDrag?.started) return;
    const card=findCard(document.elementFromPoint(pointerDrag.x,pointerDrag.y));
    pointerDrag.targetKey=card?keyOf(card):null;
    pointerDrag.after=card?afterCard(card,pointerDrag.x):false;
    showTarget(card,pointerDrag.after);
  }
  function scrollDrag() {
    if(!pointerDrag?.started) return;
    if(pointerDrag.y<80) window.scrollBy(0,-14);
    else if(pointerDrag.y>window.innerHeight-80) window.scrollBy(0,14);
    trackTarget();
    scrollFrame=requestAnimationFrame(scrollDrag);
  }
  container.addEventListener('keydown',event=>{
    if(!event.altKey || !isEnabled() || !['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End'].includes(event.key)) return;
    const card=findCard(event.target);
    if(!card) return;
    event.preventDefault();
    const keys=getKeys(), key=keyOf(card), index=keys.indexOf(key);
    const destination=event.key==='Home'?0:event.key==='End'?keys.length-1:index+(['ArrowLeft','ArrowUp'].includes(event.key)?-1:1);
    if(index>=0) onMove(key,destination);
  });
  container.addEventListener('dragstart',event=>{
    const card=findCard(event.target);
    if(!isEnabled() || !card || pointerDrag || event.target.closest('.remove-file,.reorder-page-actions')) {event.preventDefault();return;}
    draggedKey=keyOf(card);
    event.dataTransfer.effectAllowed='move';
    event.dataTransfer.setData('text/plain',String(draggedKey));
    card.classList.add('dragging');
  });
  container.addEventListener('dragover',event=>{
    if(draggedKey===null || !isEnabled()) return;
    event.preventDefault();event.dataTransfer.dropEffect='move';
    const card=findCard(event.target);
    if(card) showTarget(card,afterCard(card,event.clientX));
  });
  container.addEventListener('dragleave',event=>{if(!container.contains(event.relatedTarget)) showTarget(null,false);});
  container.addEventListener('drop',event=>{
    if(draggedKey===null || !isEnabled()) return;
    event.preventDefault();
    const card=findCard(event.target);
    if(card) onDrop(draggedKey,keyOf(card),afterCard(card,event.clientX));
    finish();
  });
  container.addEventListener('dragend',finish);
  container.addEventListener('pointerdown',event=>{
    const handle=event.target.closest('.reorder-drag-handle'), card=findCard(handle);
    if(!isEnabled() || !handle || !card || event.button!==0) return;
    event.preventDefault();
    draggedKey=keyOf(card);
    pointerDrag={id:event.pointerId,handle,startX:event.clientX,startY:event.clientY,x:event.clientX,y:event.clientY,started:false,targetKey:null,after:false};
    handle.setPointerCapture(event.pointerId);
    card.focus({preventScroll:true});
  });
  document.addEventListener('pointermove',event=>{
    if(!pointerDrag || pointerDrag.id!==event.pointerId) return;
    if(!isEnabled()) {finish();return;}
    pointerDrag.x=event.clientX;pointerDrag.y=event.clientY;
    if(!pointerDrag.started && Math.hypot(event.clientX-pointerDrag.startX,event.clientY-pointerDrag.startY)>5) {
      pointerDrag.started=true;
      container.querySelector(`${selector}[data-${keyProperty.replace(/[A-Z]/g,c=>'-'+c.toLowerCase())}="${draggedKey}"]`)?.classList.add('dragging');
      scrollDrag();
    }
    trackTarget();
  });
  document.addEventListener('pointerup',event=>{
    if(!pointerDrag || pointerDrag.id!==event.pointerId) return;
    if(isEnabled() && pointerDrag.started && pointerDrag.targetKey!==null) onDrop(draggedKey,pointerDrag.targetKey,pointerDrag.after);
    finish();
  });
  document.addEventListener('pointercancel',event=>{if(pointerDrag?.id===event.pointerId) finish();});
  container.addEventListener('lostpointercapture',event=>{if(pointerDrag?.id===event.pointerId) finish();});
  return {finish};
}

async function loadReorderThumbnails(version) {
  return loadPageThumbnails(version,{pageCount:reorderPageCount,images:reorderImages,errors:reorderErrors,container:$('reorder-pages'),thumbnail:pageThumbnail,updateStatus:updateReorderStatus,setLoading:value=>{reorderLoading=value;}});
}

async function loadDeleteThumbnails(version) {
  return loadPageThumbnails(version,{pageCount:deletePageCount,images:deleteImages,errors:deleteErrors,container:$('delete-pages'),thumbnail:deleteThumbnail,updateStatus:updateDeleteStatus,setLoading:value=>{deleteLoading=value;}});
}

async function loadPageThumbnails(version,{pageCount,images,errors,container,thumbnail,updateStatus,setLoading}) {
  const item=files.find(file=>file.id===selectedId);
  if(!item || !pageCount) return;
  const controller=new AbortController();
  thumbnailController=controller;
  const isCurrent=()=>version===inspectingVersion && item.id===selectedId && thumbnailController===controller;
  setLoading(true);
  updateStatus();
  const body=new FormData();body.append('file',item.file);body.append('password',$('input-password').value);
  let complete=false;
  let failure='';
  try {
    const response=await fetch('/api/thumbnails',{method:'POST',body,signal:controller.signal});
    if(!response.ok) throw new Error(await responseError(response));
    if(!response.body) throw new Error('Page previews are unavailable in this browser.');
    const reader=response.body.getReader(), decoder=new TextDecoder();
    let pending='';
    function receive(line) {
      if(!line.trim() || !isCurrent()) return;
      const record=JSON.parse(line);
      if(record.complete) {complete=true;return;}
      if(!Number.isInteger(record.page) || record.page<1 || record.page>pageCount) {
        if(record.error) throw new Error(record.error);
        return;
      }
      if(typeof record.image==='string' && record.image.startsWith('data:image/jpeg;base64,')) images.set(record.page,record.image);
      else errors.set(record.page,record.error || 'Preview unavailable');
      container.querySelectorAll(`[data-page-number="${record.page}"]`).forEach(card=>{
        card.classList.remove('is-loading');
        card.classList.toggle('is-error',errors.has(record.page));
        card.querySelector('.reorder-thumb').innerHTML=thumbnail(record.page);
      });
      updateStatus();
    }
    while(true) {
      const {done,value}=await reader.read();
      if(!isCurrent()) {await reader.cancel();return;}
      pending+=decoder.decode(value || new Uint8Array(),{stream:!done});
      let newline;
      while((newline=pending.indexOf('\n'))!==-1) {receive(pending.slice(0,newline));pending=pending.slice(newline+1);}
      if(done) {receive(pending);break;}
    }
    if(!complete) throw new Error('The page preview transfer stopped. Choose the PDF again to retry.');
  } catch(error) {
    if(error.name!=='AbortError' && isCurrent()) {
      failure=error.message || 'Page previews could not be loaded. Choose the PDF again to retry.';
      controller.abort();
    }
  } finally {
    if(isCurrent()) {
      setLoading(false);
      container.querySelectorAll('.is-loading').forEach(card=>{
        card.classList.remove('is-loading');
        card.querySelector('.reorder-thumb').innerHTML=thumbnail(Number(card.dataset.pageNumber));
      });
      updateStatus(failure);
    }
  }
}

function syncImagePreviewNavigation() {
  const index=files.findIndex(item=>item.id===selectedId);
  $('preview-page-count').textContent=index<0?'':`Image ${index+1} of ${files.length}`;
  $('preview-previous').disabled=index<=0;
  $('preview-next').disabled=index<0 || index>=files.length-1;
}

function cachedPreview(key) {
  const blob=previewCache.get(key);
  if(blob) {previewCache.delete(key);previewCache.set(key,blob);}
  return blob;
}

function cachePreview(key,blob) {
  previewCache.set(key,blob);
  if(previewCache.size>8) previewCache.delete(previewCache.keys().next().value);
}

async function showDecodedPreview(blob,alt,isCurrent) {
  const nextURL=URL.createObjectURL(blob);
  let displayed=false;
  try {
    const ready=new Image();ready.src=nextURL;
    await ready.decode();
    if(!isCurrent()) return;
    const previousURL=previewURL;
    previewURL=nextURL;displayed=true;
    $('preview-image').src=previewURL;
    $('preview-image').alt=alt;
    $('preview-image').hidden=false;
    $('preview-placeholder').hidden=true;
    if(previousURL) URL.revokeObjectURL(previousURL);
  } finally {if(!displayed) URL.revokeObjectURL(nextURL);}
}

async function loadImagePreview(version=inspectingVersion) {
  const item=files.find(file=>file.id===selectedId);
  if(!item || activeTool?.id!=='images-to-pdf') return;
  const render=++imagePreviewVersion;
  const options=collectOptions();
  const automatic=options.page_size==='auto';
  const margin=automatic?0:options.margin;
  if(!automatic && !$('option-margin').validity.valid) {
    $('document-preview').setAttribute('aria-busy','false');
    previewMessage('Enter a margin from 0 to 144 points to preview this page.');
    return;
  }
  $('preview-page-label').textContent=automatic?'Fit each image':`${options.page_size==='a4'?'A4':'US Letter'} · ${options.orientation==='landscape'?'Landscape':'Portrait'}`;
  if($('preview-image').hidden) previewMessage('Loading page preview…');
  $('document-preview').setAttribute('aria-busy','true');
  const key=`image:${item.id}:${options.page_size}:${automatic?'':options.orientation}:${margin}`;
  let sourceURL=null;
  try {
    let blob=cachedPreview(key);
    if(!blob) {
      sourceURL=URL.createObjectURL(item.file);
      const image=new Image();
      image.src=sourceURL;
      await image.decode();
      if(version!==inspectingVersion || render!==imagePreviewVersion || item.id!==selectedId) return;
      let width,height;
      if(automatic) {
        width=image.naturalWidth*0.75;height=image.naturalHeight*0.75;
        const shrink=Math.min(1,2000/Math.max(width,height));
        width*=shrink;height*=shrink;
      } else {
        [width,height]=options.page_size==='a4'?[595,842]:[612,792];
        if(options.orientation==='landscape') [width,height]=[height,width];
      }
      // Match the PDF engine: a white page with the image centered inside its margins.
      const canvas=document.createElement('canvas');
      const scale=Math.min(1,720/Math.max(width,height));
      canvas.width=Math.max(1,Math.round(width*scale));
      canvas.height=Math.max(1,Math.round(height*scale));
      const context=canvas.getContext('2d');
      context.scale(canvas.width/width,canvas.height/height);
      context.fillStyle='#fff';context.fillRect(0,0,width,height);
      const fit=Math.min((width-margin*2)/image.naturalWidth,(height-margin*2)/image.naturalHeight);
      const imageWidth=image.naturalWidth*fit,imageHeight=image.naturalHeight*fit;
      context.drawImage(image,(width-imageWidth)/2,(height-imageHeight)/2,imageWidth,imageHeight);
      blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));
      if(version!==inspectingVersion || render!==imagePreviewVersion || item.id!==selectedId) return;
      if(!blob) throw new Error('Preview unavailable.');
      cachePreview(key,blob);
    }
    await showDecodedPreview(blob,`PDF page preview of ${item.file.name}`,
      ()=>version===inspectingVersion && render===imagePreviewVersion && item.id===selectedId);
  } catch(error) {
    if(version===inspectingVersion && render===imagePreviewVersion && item.id===selectedId) previewMessage('This image cannot be previewed here. You can still process it.');
  } finally {
    if(sourceURL) URL.revokeObjectURL(sourceURL);
    if(version===inspectingVersion && render===imagePreviewVersion) $('document-preview').setAttribute('aria-busy','false');
  }
}

function navigatePreview(direction) {
  if(activeTool?.id==='images-to-pdf') {
    const index=files.findIndex(item=>item.id===selectedId);
    const target=index+direction;
    if(index<0 || target<0 || target>=files.length) return;
    selectedId=files[target].id;
    renderFiles();
    inspectSelected();
    announce(`Image ${target+1} of ${files.length}: ${files[target].file.name}.`);
    return;
  }
  const count=files.find(item=>item.id===selectedId)?.info?.page_count || 0;
  const target=previewPage+direction;
  if(target<1 || target>count) return;
  previewPage=target;
  loadPreview();
}

async function inspectSelected() {
  cancelInspect();
  const item=files.find(file=>file.id===selectedId);
  if(!item || !activeTool) {clearPreview();clearReorder();clearDelete();return;}
  if(activeTool.id==='merge') {clearPreview();return;}
  const imageTool=activeTool.id==='images-to-pdf';
  $('document-preview').classList.add('smooth-preview');
  if(imageTool) {
    $('document-preview').hidden=false;
    $('preview-previous').setAttribute('aria-label','Previous image');
    $('preview-next').setAttribute('aria-label','Next image');
    syncImagePreviewNavigation();
    await loadImagePreview();
    return;
  }
  clearPreview();
  const reordering=activeTool.id==='reorder';
  const deleting=activeTool.id==='delete';
  if(reordering) beginReorder(item);
  if(deleting) beginDelete(item);
  const version=inspectingVersion;
  const controller=new AbortController();
  inspectController=controller;
  previewPage=restoredPreviewPage;
  restoredPreviewPage=1;
  $('document-preview').hidden=reordering || deleting;
  $('preview-page-label').textContent=activeTool.id==='images-to-pdf'?'Image preview':'Page 1';
  $('preview-page-count').textContent='';
  $('preview-previous').disabled=true;
  $('preview-next').disabled=true;
  const previewUnit=activeTool.id==='images-to-pdf'?'image':'page';
  $('preview-previous').setAttribute('aria-label',`Previous ${previewUnit}`);
  $('preview-next').setAttribute('aria-label',`Next ${previewUnit}`);
  previewMessage('Loading preview…');
  const body=new FormData();body.append('file',item.file);body.append('password',$('input-password').value);
  try {
    const response=await fetch('/api/inspect',{method:'POST',body,signal:controller.signal});
    if(version!==inspectingVersion) return;
    if(!response.ok) throw new Error(await responseError(response));
    const info=await response.json();
    if(version!==inspectingVersion) return;
    item.info=info;
    renderFiles();
    if(info.password_required) {
      previewMessage('Enter this PDF’s password in the options to preview it.');
      $('preview-page-label').textContent='Password protected';
      $('preview-page-count').textContent='Locked document';
      if(reordering) {
        $('reorder-status').textContent='Enter this PDF’s password to see its pages.';
        $('reorder-summary').textContent='Enter the PDF password below to arrange its pages.';
      }
      if(deleting) {
        $('delete-status').textContent='Enter this PDF’s password to see its pages.';
        $('delete-summary').textContent='Enter the PDF password below to select pages.';
      }
      return;
    }
    if(reordering) {
      initializeReorder(info.page_count);
      await loadReorderThumbnails(version);
      return;
    }
    if(deleting) {
      initializeDelete(info.page_count);
      await loadDeleteThumbnails(version);
      return;
    }
    previewPage=Math.min(previewPage,info.page_count);
    $('preview-page-count').textContent=`${previewPage} of ${info.page_count}`;
    $('preview-next').disabled=info.page_count<=1;
    await loadPreview(version);
  } catch(error) {
    if(error.name==='AbortError' || version!==inspectingVersion) return;
    previewMessage(error.message || 'The preview is unavailable.');
    $('preview-page-label').textContent='Preview unavailable';
    if(reordering) {
      $('reorder-status').textContent=error.message || 'The PDF could not be read.';
      $('reorder-summary').textContent='Choose a readable PDF to arrange its pages.';
    }
    if(deleting) {
      $('delete-status').textContent=error.message || 'The PDF could not be read.';
      $('delete-summary').textContent='Choose a readable PDF to select its pages.';
    }
    if(error instanceof TypeError) setError(serverUnavailableMessage());
    else setError(error.message);
  }
}

async function loadPreview(version=inspectingVersion) {
  const item=files.find(file=>file.id===selectedId);
  if(!item?.info?.page_count || !activeTool || activeTool.id==='images-to-pdf') return;
  previewController?.abort();
  const controller=new AbortController(); previewController=controller;
  const page=previewPage;
  const isCurrent=()=>version===inspectingVersion && page===previewPage && item.id===selectedId && previewController===controller && !controller.signal.aborted;
  const key=`pdf:${item.id}:${version}:${page}`;
  $('preview-page-label').textContent=`Page ${page}`;
  $('preview-page-count').textContent=`${page} of ${item.info.page_count}`;
  $('preview-previous').disabled=page<=1;
  $('preview-next').disabled=page>=item.info.page_count;
  if($('preview-image').hidden) previewMessage('Loading preview…');
  $('document-preview').setAttribute('aria-busy','true');
  try {
    let blob=cachedPreview(key);
    if(!blob) {
      const body=new FormData();body.append('file',item.file);body.append('password',$('input-password').value);body.append('page',String(page));
      const response=await fetch('/api/preview',{method:'POST',body,signal:controller.signal});
      if(!response.ok) throw new Error('Preview unavailable. You can still process this file.');
      blob=await response.blob();
      if(!isCurrent()) return;
      cachePreview(key,blob);
    }
    await showDecodedPreview(blob,`Page ${page} of ${item.file.name}`,isCurrent);
  } catch(error) {
    if(error.name!=='AbortError' && isCurrent()) previewMessage(error.message);
  } finally {if(isCurrent()) $('document-preview').setAttribute('aria-busy','false');}
}

function collectOptions() {
  const result={};
  if(!$('password-section').hidden) result.password=$('input-password').value;
  $('tool-options').querySelectorAll('input,select,textarea').forEach(input=>{
    result[input.name]=input.type==='checkbox'?input.checked:input.type==='number'?Number(input.value):input.name==='dpi'?Number(input.value):input.value;
  });
  if(activeTool?.id==='images-to-pdf' && result.page_size==='auto') result.margin=0;
  return result;
}

function setBusy(busy) {
  processing=busy;
  if(busy) {finishPageDrag();finishImageDrag();}
  $('options-form').querySelectorAll('input,select,textarea').forEach(input=>input.disabled=busy);
  for(const id of ['drop-zone','add-files','clear-files','back-button']) $(id).disabled=busy;
  $('file-list').querySelectorAll('button').forEach(button=>button.disabled=busy);
  $('file-list').classList.toggle('is-busy',busy);
  $('file-list').querySelectorAll('[data-image-key]').forEach(card=>card.draggable=!busy && files.length>1);
  $('process-button').disabled=busy || files.length===0;
  $('process-button').setAttribute('aria-busy',String(busy));
  $('process-button').innerHTML=busy?`<span class="loading-spinner" aria-hidden="true"></span><span id="process-label">${hosted?'Processing your files…':'Processing locally…'}</span>`:`${icon('arrow')}<span id="process-label">${activeTool?.action || 'Process PDF'}</span>`;
  $('processing-note').textContent=busy?'Large documents can take a moment. Keep this tab open.':'Your original files remain unchanged.';
  syncReorderControls();
  syncDeleteControls();
  renderSavedWork();
  if(!busy) {syncOptionVisibility();renderFiles();}
}

function resultFilename(header,type) {
  let name='';
  const utf=header?.match(/filename\*=UTF-8''([^;]+)/i);
  if(utf) {try{name=decodeURIComponent(utf[1]);}catch(_){}}
  if(!name) name=header?.match(/filename="([^"]*)"/i)?.[1] || header?.match(/filename=([^;]+)/i)?.[1] || '';
  name=name.replace(/[\x00-\x1f\x7f<>:"/\\|?*]/g,'_').trim().replace(/[. ]+$/,'');
  if(!name || /^(con|prn|aux|nul|com\d|lpt\d)(\.|$)/i.test(name)) name=`paperwork-result${type.includes('zip')?'.zip':type.includes('json')?'.json':type.includes('text')?'.txt':type.includes('image/png')?'.png':type.includes('image/jpeg')?'.jpg':'.pdf'}`;
  return name.slice(0,200);
}

async function processFiles(event) {
  event.preventDefault();
  if(processing || !activeTool || !files.length) return;
  setError('');
  if(activeTool.id==='merge' && files.length<2) {setError('Choose at least two PDFs to merge.');return;}
  if(activeTool.id==='reorder' && !syncReorderFromInput()) return;
  if(activeTool.id==='delete' && (!syncDeleteFromInput(true) || !deleteSelection.size)) {if(!deleteSelection.size && !deleteSelectionError) setError('Select at least one page to remove.');return;}
  const options=collectOptions();
  if(activeTool.id==='protect' && (!options.user_password || new TextEncoder().encode(options.user_password).length>40 || new TextEncoder().encode(options.owner_password).length>40)) {setError('Choose an opening password, and keep both passwords within 40 UTF-8 bytes.');return;}
  if(activeTool.id==='watermark' && [...options.text].some(c=>c.codePointAt(0)>255)) {setError('The watermark font supports English and Latin characters. Please use Latin text.');return;}
  if(activeTool.id==='page-numbers' && [...options.prefix+options.suffix].some(c=>c.codePointAt(0)>255)) {setError('Page number prefixes and suffixes support English and Latin characters.');return;}
  flushDraftSave();
  clearResult();
  if(['reorder','delete'].includes(activeTool.id)) {thumbnailController?.abort();finishPageDrag();}
  setBusy(true);
  announce(hosted?'Processing your files on the PDF server.':'Processing your files on this computer.');
  const body=new FormData();files.forEach(item=>body.append('files',item.file));body.append('options',JSON.stringify(options));
  try {
    const response=await fetch(`/api/process/${activeTool.id}`,{method:'POST',body});
    if(!response.ok) throw new Error(await responseError(response));
    const blob=await response.blob();
    const name=resultFilename(response.headers.get('Content-Disposition'),blob.type);
    let summary={};
    try {summary=JSON.parse(decodeURIComponent(response.headers.get('X-Result-Summary') || '%7B%7D'));} catch(_) {}
    resultURL=URL.createObjectURL(blob);
    $('download-button').href=resultURL;
    $('download-button').download=name;
    $('download-button').innerHTML=`${icon('download')} Download ${name.toLowerCase().endsWith('.zip')?'ZIP':'file'}`;
    $('result-title').textContent='All done. Your file is ready.';
    const input=Number(response.headers.get('X-Input-Bytes')) || files.reduce((total,item)=>total+item.file.size,0);
    const output=Number(response.headers.get('X-Output-Bytes')) || blob.size;
    let details=`${name} · ${bytes(output)}`;
    if(activeTool.id==='compress') {
      const saved=input-output;
      details += saved>0?` · ${(saved/input*100).toFixed(1)}% smaller`:' · This file could not be reduced further at this setting.';
    }
    if(summary.pages) details+=` · ${summary.pages} ${summary.pages===1?'page':'pages'}`;
    if(activeTool.id==='extract-text' && typeof summary.characters==='number') details+=` · ${summary.characters.toLocaleString()} characters`;
    $('result-details').textContent=details;
    $('result-warnings').replaceChildren();
    for(const warning of Array.isArray(summary.warnings)?summary.warnings:[]) {
      const line=document.createElement('div');line.className='result-warning';line.textContent=warning;$('result-warnings').append(line);
    }
    if(activeTool.id==='extract-text') {
      const raw=await blob.text();
      extractedText=options.format==='json'?JSON.parse(raw).pages.map(item=>`--- Page ${item.page} ---\n${item.text}`).join('\n\n'):raw;
      const limit=100000;
      $('extracted-text').value=extractedText.slice(0,limit);
      $('text-preview-status').textContent=extractedText.length>limit?'Showing the first 100,000 characters. Copy text and Download file include the full result.':'Review the extracted text, copy it, or download your file.';
      $('text-result').hidden=false;
      $('result-panel').classList.add('has-text-result');
    }
    $('result-panel').hidden=false;
    $('result-panel').scrollIntoView({behavior:'smooth',block:'nearest'});
    $('result-panel').focus({preventScroll:true});
    announce(`Your result is ready. ${name}, ${bytes(output)}. Use Download file to save it.`);
  } catch(error) {
    setError(error instanceof TypeError?serverUnavailableMessage():error.message || 'The files could not be processed. Please try again.');
  } finally {setBusy(false);}
}

async function copyExtractedText() {
  if(!extractedText) return;
  const button=$('copy-text');
  button.disabled=true;
  try {
    await navigator.clipboard.writeText(extractedText);
    button.textContent='Copied';
    announce('Extracted text copied to the clipboard.');
    clearTimeout(textCopyTimer);
    textCopyTimer=setTimeout(()=>{button.textContent='Copy text';},1800);
  } catch(_) {
    $('extracted-text').focus();$('extracted-text').select();
    $('text-preview-status').textContent='Select the preview and press Ctrl+C to copy it, or download the full file.';
    announce('Select the preview and press Ctrl+C, or download the full file.');
  } finally {button.disabled=false;}
}

function serverUnavailableMessage() {
  return hosted?'The PDF server is unavailable. Please wait a moment and reload this page.':'The local server is unavailable. Start Paperwork again, then reload this page.';
}

async function checkHealth() {
  try {
    const response=await fetch('/api/health');
    if(!response.ok) throw new Error('Unavailable');
    const health=await response.json();
    if(health.status!=='ok') throw new Error('Unavailable');
  } catch(_) {
    announce(serverUnavailableMessage());
  }
}

function draftSnapshot() {
  if(!activeTool || !files.length) return null;
  const options={};
  $('tool-options').querySelectorAll('input,select,textarea').forEach(input=>{
    if(input.type!=='password' && !/password/i.test(input.name)) options[input.name]=input.type==='checkbox'?input.checked:input.value;
  });
  return {tool:activeTool.id,files:files.map(item=>item.file),options,
    selectedIndex:Math.max(0,files.findIndex(item=>item.id===selectedId)),previewPage,updatedAt:Date.now()};
}

function queueDraftSave(immediate=false) {
  draftRevision++;
  if(!draftStore) return;
  pendingDraft=draftSnapshot();
  clearTimeout(draftSaveTimer);
  if(!pendingDraft) return;
  savedWorkMessage='Saving your work in this browser…';
  renderSavedWork();
  if(immediate) flushDraftSave();else draftSaveTimer=setTimeout(flushDraftSave,250);
}

function writeDraftAction(action) {
  draftWriteQueue=draftWriteQueue.then(action).catch(error=>{
    savedWorkMessage=error.name==='QuotaExceededError'?'Browser storage is full. This change was not saved; you can still download your result.':`Saved work is unavailable. ${error.message || 'You can still process and download your files.'}`;
    renderSavedWork();
  });
  return draftWriteQueue;
}

function flushDraftSave() {
  clearTimeout(draftSaveTimer);
  const snapshot=pendingDraft;
  pendingDraft=null;
  if(!draftStore || !snapshot) return draftWriteQueue;
  draftWritesInFlight++;
  return writeDraftAction(async()=>{
    try {
      const {draft,evicted}=await draftStore.save(snapshot);
      savedDrafts.set(draft.tool,draft);
      evicted.forEach(tool=>savedDrafts.delete(tool));
      savedWorkMessage=evicted.length?'Work saved. Older drafts were cleared to keep saved documents within 200 MB.':'';
      scheduleDraftExpiry();
    } finally {draftWritesInFlight--;renderSavedWork();}
  });
}

function draftExpiryLabel(time) {
  return new Date(time).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
}

function renderSavedWork() {
  if(!draftStore) return;
  const entries=Array.from(savedDrafts.values()).filter(draft=>draft.expiresAt>Date.now() && tools.some(tool=>tool.id===draft.tool)).sort((a,b)=>b.updatedAt-a.updatedAt);
  $('clear-saved-work').hidden=!entries.length;
  $('clear-saved-work').disabled=processing;
  $('saved-work').hidden=!entries.length;
  $('saved-drafts').innerHTML=entries.map(draft=>{
    const tool=tools.find(item=>item.id===draft.tool);
    return `<div class="saved-draft"><button class="saved-draft-resume" type="button" data-resume-tool="${tool.id}"><span class="tool-icon ${tool.category}">${icon(tool.icon)}</span><span><strong>${escapeHTML(tool.name)}</strong><small>${escapeHTML(draft.name)}${draft.count>1?` + ${draft.count-1} more`:''} · Expires ${escapeHTML(draftExpiryLabel(draft.expiresAt))}</small></span><span class="saved-draft-action">Resume ${icon('arrow')}</span></button><button class="icon-button" type="button" data-discard-tool="${tool.id}" aria-label="Clear saved ${escapeHTML(tool.name)} work" title="Clear this saved draft">${icon('close')}</button></div>`;
  }).join('');
  const current=savedDrafts.get(activeTool?.id);
  $('draft-status').textContent=(pendingDraft || draftWritesInFlight?'Saving your work in this browser…':savedWorkMessage) || (current && current.expiresAt>Date.now()?`Saved in this browser · expires ${draftExpiryLabel(current.expiresAt)}. Passwords are not saved.`:'Documents and edits are saved in this browser for 24 hours after your last change. Passwords are not saved.');
}

function scheduleDraftExpiry() {
  clearTimeout(draftExpiryTimer);
  const expiry=Math.min(...Array.from(savedDrafts.values()).map(draft=>draft.expiresAt));
  if(Number.isFinite(expiry)) draftExpiryTimer=setTimeout(()=>refreshSavedDrafts(),Math.max(1,expiry-Date.now()+1));
}

async function refreshSavedDrafts() {
  if(!draftStore) return;
  await writeDraftAction(async()=>{
    const entries=await draftStore.list();
    savedDrafts=new Map(entries.map(draft=>[draft.tool,draft]));
    renderSavedWork();scheduleDraftExpiry();
  });
}

async function restoreToolDraft(id,revision=draftRevision) {
  if(!draftStore) return;
  const version=++draftRestoreVersion;
  try {
    await draftWriteQueue;
    const draft=await draftStore.load(id);
    if(version!==draftRestoreVersion || revision!==draftRevision || activeTool?.id!==id || files.length) return;
    if(!draft) {savedDrafts.delete(id);renderSavedWork();return;}
    files=draft.files.map(saved=>({id:++fileId,file:new File([saved.blob],saved.name,{type:saved.type,lastModified:saved.lastModified}),info:null}));
    selectedId=files[Math.max(0,Math.min(files.length-1,Number(draft.selectedIndex)||0))].id;
    $('tool-options').querySelectorAll('input,select,textarea').forEach(input=>{
      if(input.type==='password' || !Object.hasOwn(draft.options || {},input.name)) return;
      if(input.type==='checkbox') input.checked=draft.options[input.name]===true;
      else input.value=String(draft.options[input.name]);
      optionDirty.add(input.name);
    });
    if(id==='reorder') {reorderFileId=selectedId;restoredPageOrder=$('option-pages').value;}
    if(id==='delete') deleteFileId=selectedId;
    restoredPreviewPage=Number.isInteger(draft.previewPage)?Math.max(1,draft.previewPage):1;
    savedDrafts.set(id,draftStore.metadata(draft));
    savedWorkMessage='';
    syncOptionVisibility();renderFiles();renderSavedWork();inspectSelected();
    announce('Saved work restored. Your original expiry time is unchanged.');
  } catch(error) {
    savedWorkMessage=`This saved draft could not be restored. ${error.message || 'Choose your file again.'}`;
    renderSavedWork();
  }
}

function discardSavedDraft(tool=activeTool?.id) {
  clearTimeout(draftSaveTimer);pendingDraft=null;draftRestoreVersion++;draftRevision++;
  if(!draftStore || !tool) return Promise.resolve();
  return writeDraftAction(async()=>{
    await draftStore.remove(tool);savedDrafts.delete(tool);savedWorkMessage='Saved draft cleared. New edits will be saved for 24 hours.';
    renderSavedWork();scheduleDraftExpiry();
  });
}

function clearSavedWork() {
  if(processing || !draftStore) return;
  clearTimeout(draftSaveTimer);pendingDraft=null;draftRestoreVersion++;draftRevision++;
  return writeDraftAction(async()=>{
    await draftStore.clear();savedDrafts.clear();clearTimeout(draftExpiryTimer);
    savedWorkMessage='Saved copies cleared. New edits will be saved for 24 hours.';
    renderSavedWork();announce(savedWorkMessage);
  });
}

async function initializeDraftStorage() {
  draftStore=new LocalDraftStore();
  const revision=draftRevision;
  await refreshSavedDrafts();
  if(revision===draftRevision && activeTool && !files.length) await restoreToolDraft(activeTool.id,revision);
}

document.querySelectorAll('[data-category]').forEach(button=>button.addEventListener('click',()=>{
  if(processing) return;
  category=button.dataset.category;
  if(activeTool) showHome(); else renderTools();
}));
$('tool-search').addEventListener('input',renderTools);
$('tool-grid').addEventListener('click',event=>{const button=event.target.closest('[data-tool]');if(button) openTool(button.dataset.tool);});
$('clear-search').addEventListener('click',()=>{category='all';$('tool-search').value='';renderTools();});
$('back-button').addEventListener('click',()=>showHome());
$('drop-zone').addEventListener('click',()=>$('file-input').click());
$('add-files').addEventListener('click',()=>$('file-input').click());
$('file-input').addEventListener('change',event=>{addFiles(event.target.files);event.target.value='';});
for(const name of ['dragenter','dragover']) $('drop-zone').addEventListener(name,event=>{event.preventDefault();if(!processing)$('drop-zone').classList.add('dragging');});
for(const name of ['dragleave','drop']) $('drop-zone').addEventListener(name,event=>{event.preventDefault();$('drop-zone').classList.remove('dragging');if(name==='drop')addFiles(event.dataTransfer.files);});
document.addEventListener('dragover',event=>event.preventDefault());
document.addEventListener('drop',event=>event.preventDefault());
$('clear-files').addEventListener('click',()=>{discardSavedDraft();resetFiles();});
$('clear-saved-work').addEventListener('click',clearSavedWork);
$('saved-drafts').addEventListener('click',event=>{
  if(processing) return;
  const resume=event.target.closest('[data-resume-tool]'), discard=event.target.closest('[data-discard-tool]');
  if(resume) openTool(resume.dataset.resumeTool);
  else if(discard) {
    const tool=discard.dataset.discardTool;
    if(pendingDraft?.tool!==tool) flushDraftSave();
    discardSavedDraft(tool);
  }
});
$('file-list').addEventListener('click',event=>{
  if(processing) return;
  const remove=event.target.closest('[data-remove]');
  const move=event.target.closest('[data-move]');
  const view=event.target.closest('[data-view]');
  if(event.target.closest('[data-change-file]')) {$('file-input').click();}
  else if(remove) {
    const id=Number(remove.dataset.remove);files=files.filter(item=>item.id!==id);clearResult();setError('');
    if(id===selectedId) {selectedId=files[0]?.id ?? null;inspectSelected();}
    renderFiles();
    if(files.length) queueDraftSave(true);else discardSavedDraft();
  } else if(move) {
    const index=files.findIndex(item=>item.id===Number(move.dataset.move));const target=index+Number(move.dataset.direction);
    if(target>=0 && target<files.length) {[files[index],files[target]]=[files[target],files[index]];clearResult();renderFiles();queueDraftSave(true);announce(`Moved file to position ${target+1}.`);}
  } else if(view) {selectedId=Number(view.dataset.view);renderFiles();setError('');inspectSelected();}
});
$('options-form').addEventListener('input',event=>{
  if(event.target.name && event.target.name!=='password') optionDirty.add(event.target.name);
  clearResult();setError('');
  if(activeTool?.id==='delete' && event.target.id==='option-pages') syncDeleteFromInput();
  if(event.target.id==='input-password') {cancelInspect();passwordTimer=setTimeout(inspectSelected,500);}
  if(activeTool?.id==='images-to-pdf' && event.target.name==='margin') {
    clearTimeout(imagePreviewTimer);imagePreviewVersion++;
    imagePreviewTimer=setTimeout(()=>loadImagePreview(),120);
  }
  if(event.target.name && event.target.type!=='password') queueDraftSave();
});
$('options-form').addEventListener('change',event=>{
  syncOptionVisibility();
  if(activeTool?.id==='images-to-pdf' && ['page_size','orientation','margin'].includes(event.target.name)) {
    clearTimeout(imagePreviewTimer);loadImagePreview();
  }
  if(activeTool?.id==='reorder' && event.target.id==='option-pages') syncReorderFromInput();
  if(activeTool?.id==='delete' && event.target.id==='option-pages') syncDeleteFromInput(true);
  if(event.target.name && event.target.type!=='password') queueDraftSave(true);
});
$('options-form').addEventListener('focusout',event=>{
  if(activeTool?.id==='reorder' && event.target.id==='option-pages') syncReorderFromInput();
  if(activeTool?.id==='delete' && event.target.id==='option-pages') syncDeleteFromInput(true);
});
$('options-form').addEventListener('submit',processFiles);
$('copy-text').addEventListener('click',copyExtractedText);
$('delete-pages').addEventListener('click',event=>{
  const button=event.target.closest('[data-delete-page]');
  if(button) toggleDeletePage(Number(button.dataset.deletePage));
});
$('delete-reset').addEventListener('click',()=>{
  if(processing || activeTool?.id!=='delete') return;
  deleteSelection.clear();
  writeDeleteSelection();
  announce('All pages restored. Select pages to remove.');
});
$('reorder-reset').addEventListener('click',()=>{
  if(processing || !reorderPageCount) return;
  finishPageDrag();
  reorderOrder=Array.from({length:reorderPageCount},(_,index)=>({key:++reorderKey,page:index+1}));
  optionDirty.delete('pages');
  clearResult();setError('');renderReorder();
  queueDraftSave(true);
  announce('Pages restored to their original order.');
});
$('reorder-pages').addEventListener('click',event=>{
  const button=event.target.closest('[data-page-move]');
  if(!button || processing) return;
  const key=Number(button.closest('[data-page-key]').dataset.pageKey);
  moveReorderPage(key,reorderOrder.findIndex(entry=>entry.key===key)+Number(button.dataset.pageMove));
});
pageReorderDrag=createReorderDragController({
  container:$('reorder-pages'),selector:'[data-page-key]',keyProperty:'pageKey',
  getKeys:()=>reorderOrder.map(entry=>entry.key),onMove:moveReorderPage,onDrop:dropReorderPage,
  isEnabled:()=>activeTool?.id==='reorder' && !processing
});
imageReorderDrag=createReorderDragController({
  container:$('file-list'),selector:'[data-image-key]',keyProperty:'imageKey',
  getKeys:()=>files.map(item=>item.id),onMove:moveImageFile,onDrop:dropImageFile,
  isEnabled:()=>activeTool?.id==='images-to-pdf' && !processing && files.length>1
});
$('file-list').addEventListener('keydown',event=>{
  if(processing || activeTool?.id!=='images-to-pdf' || event.altKey || !['Enter',' '].includes(event.key)) return;
  const card=event.target.closest('[data-image-key]');
  if(!card || event.target!==card) return;
  event.preventDefault();
  selectedId=Number(card.dataset.imageKey);renderFiles(selectedId);setError('');inspectSelected();
});
$('file-list').addEventListener('error',event=>{
  if(event.target.tagName!=='IMG') return;
  const thumb=event.target.closest('.reorder-thumb');
  if(!thumb) return;
  event.target.hidden=true;
  thumb.querySelector('.image-thumb-fallback').hidden=false;
},true);
$('preview-previous').addEventListener('click',()=>navigatePreview(-1));
$('preview-next').addEventListener('click',()=>navigatePreview(1));
$('start-over-button').addEventListener('click',()=>{if(activeTool){discardSavedDraft();openTool(activeTool.id,false,false);}});
$('preview-image').addEventListener('error',()=>previewMessage('This image cannot be previewed here. You can still process it.'));
document.addEventListener('keydown',event=>{
  if(event.key==='/' && !activeTool && !['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)) {event.preventDefault();$('tool-search').focus();}
});
window.addEventListener('popstate',()=>{const id=location.hash.slice(1);if(id)openTool(id,false);else showHome(false);});
window.addEventListener('beforeunload',event=>{if(processing || pendingDraft || draftWritesInFlight){flushDraftSave();event.preventDefault();event.returnValue='';}});
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='hidden') flushDraftSave();});
window.addEventListener('pagehide',()=>{flushDraftSave();cancelInspect();clearPreview();clearResult();});
window.addEventListener('pageshow',event=>{if(event.persisted){refreshSavedDrafts();if(files.length)inspectSelected();}});
window.addEventListener('focus',()=>refreshSavedDrafts());
renderTools();
if(location.hash.slice(1)) openTool(location.hash.slice(1),false);
checkHealth();
initializeDraftStorage();
