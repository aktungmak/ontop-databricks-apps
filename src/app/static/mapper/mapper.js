/**
 * R2RML Mapper — UI layer (all DOM).
 */
import {
  DEFAULT_PREFIXES,
  mergePrefixes,
  extractPrefixes,
  parseTurtle,
  parseTriplesMaps,
  serializeStore,
  validateTurtle,
  mergeTurtleIntoStore,
  newEmptyCard,
  tableToFragmentId,
  addTriplesMap,
  removeTriplesMap,
  renameTriplesMap,
  updateLogicalTable,
  updateSubjectMap,
  addPredicateObjectMap,
  removePredicateObjectMap,
  updatePredicateObjectMap,
  saveDraft,
  loadDraft,
  clearDraft,
  loadOntologies,
  saveOntology,
  deleteOntology,
  setOntologyActive,
  buildOntologyIndex,
  compactIri,
} from './model.js';

const state = {
  mode: 'visual',
  dirty: false,
  savedFingerprint: '',
  prefixes: { ...DEFAULT_PREFIXES },
  store: null,
  codeMirror: null,
  ontologies: [],
  ontologyIndex: { classes: [], properties: [] },
  ucCache: { catalogs: null },
  autogenErrors: [],
};

const $ = (sel) => document.querySelector(sel);
const visualEditorPanel = $('#visual-editor-panel');
const visualCards = $('#visual-cards');
const textEditorPanel = $('#text-editor-panel');
const btnModeVisual = $('#btn-mode-visual');
const btnModeText = $('#btn-mode-text');
const prefixTbody = $('#prefix-tbody');
const ontologyList = $('#ontology-list');
const autogenDialog = $('#dialog-autogenerate');
const PREFIX_COLLAPSED_KEY = 'mapper-prefixes-collapsed';

function fingerprint() {
  const quads = state.store.getQuads(null, null, null, null);
  const quadKeys = quads
    .map((q) => `${q.subject.value}|${q.predicate.value}|${q.object.value}`)
    .sort();
  return JSON.stringify({ prefixes: state.prefixes, quads: quadKeys, mode: state.mode });
}

function markDirty() {
  state.dirty = fingerprint() !== state.savedFingerprint;
}

function markClean() {
  state.savedFingerprint = fingerprint();
  state.dirty = false;
}

async function getTurtleFromStore() {
  return serializeStore(state.store, state.prefixes);
}

async function persistDraft() {
  let turtle;
  if (state.mode === 'text' && state.codeMirror) {
    turtle = state.codeMirror.getValue();
  } else {
    turtle = await getTurtleFromStore();
  }
  await saveDraft(turtle, state.prefixes, state.mode);
  markClean();
}

async function loadTurtleIntoState(turtle) {
  state.prefixes = mergePrefixes(state.prefixes, extractPrefixes(turtle));
  state.store = parseTurtle(turtle, state.prefixes);
  renderPrefixTable();
  if (state.mode === 'visual') renderVisualEditor();
  else if (state.codeMirror) state.codeMirror.setValue(await serializeStore(state.store, state.prefixes));
}

async function restoreDraft() {
  const draft = await loadDraft();
  if (!draft?.turtle) return false;
  state.prefixes = mergePrefixes(DEFAULT_PREFIXES, draft.prefixes || {});
  state.mode = draft.mode || 'visual';
  await loadTurtleIntoState(draft.turtle);
  return true;
}

async function switchMode(newMode) {
  if (newMode === state.mode) return;

  if (state.mode === 'visual' && newMode === 'text') {
    const turtle = await getTurtleFromStore();
    if (state.codeMirror) state.codeMirror.setValue(turtle);
  } else if (state.mode === 'text' && newMode === 'visual') {
    const turtle = state.codeMirror?.getValue() || '';
    try {
      validateTurtle(turtle, state.prefixes);
      await loadTurtleIntoState(turtle);
    } catch (e) {
      alert(`Cannot switch to Visual: invalid Turtle — ${e.message}`);
      updateModeVisibility();
      return;
    }
  }

  state.mode = newMode;
  updateModeVisibility();
  markDirty();
  await persistDraft();
  if (newMode === 'visual') renderVisualEditor();
}

function updateModeVisibility() {
  const isVisual = state.mode === 'visual';
  visualEditorPanel.classList.toggle('visible', isVisual);
  textEditorPanel.classList.toggle('visible', !isVisual);
  btnModeVisual.classList.toggle('active', isVisual);
  btnModeText.classList.toggle('active', !isVisual);
}

function initCodeMirror() {
  state.codeMirror = CodeMirror.fromTextArea($('#turtle-textarea'), {
    mode: 'text/turtle',
    lineNumbers: true,
    lineWrapping: true,
  });
  state.codeMirror.on('change', () => markDirty());
}

function renderPrefixTable() {
  prefixTbody.innerHTML = '';
  for (const [prefix, iri] of Object.entries(state.prefixes)) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><input type="text" class="input" value="${esc(prefix)}" data-prefix-key /></td>
      <td><input type="text" class="input" value="${esc(iri)}" data-prefix-iri /></td>
      <td><button type="button" class="btn btn-ghost btn-icon" data-remove-prefix>×</button></td>
    `;
    tr.querySelector('[data-prefix-key]').addEventListener('change', () => { syncPrefixesFromTable(); markDirty(); });
    tr.querySelector('[data-prefix-iri]').addEventListener('change', () => { syncPrefixesFromTable(); markDirty(); });
    tr.querySelector('[data-remove-prefix]').addEventListener('click', () => {
      syncPrefixesFromTable();
      const key = tr.querySelector('[data-prefix-key]').value.trim();
      if (key && key !== 'rr') delete state.prefixes[key];
      renderPrefixTable();
      markDirty();
    });
    prefixTbody.appendChild(tr);
  }
}

function syncPrefixesFromTable() {
  const next = { rr: DEFAULT_PREFIXES.rr };
  prefixTbody.querySelectorAll('tr').forEach((tr) => {
    const k = tr.querySelector('[data-prefix-key]').value.trim();
    const v = tr.querySelector('[data-prefix-iri]').value.trim();
    if (k && v) next[k] = v;
  });
  state.prefixes = next;
}

async function refreshOntologyList() {
  state.ontologies = await loadOntologies();
  ontologyList.innerHTML = '';
  for (const ont of state.ontologies) {
    const div = document.createElement('div');
    div.className = 'ontology-item';
    div.innerHTML = `
      <label>
        <input type="checkbox" data-ontology="${esc(ont.filename)}" ${ont.active ? 'checked' : ''} />
        ${esc(ont.filename)}
      </label>
      <button type="button" class="btn btn-outline" data-delete-ontology="${esc(ont.filename)}">Delete</button>
    `;
    div.querySelector('input').addEventListener('change', async (e) => {
      await setOntologyActive(ont.filename, e.target.checked);
      await refreshOntologyList();
      await refreshOntologyIndex();
    });
    div.querySelector('[data-delete-ontology]').addEventListener('click', async () => {
      if (!confirm(`Delete ontology ${ont.filename}?`)) return;
      await deleteOntology(ont.filename);
      await refreshOntologyList();
      await refreshOntologyIndex();
    });
    ontologyList.appendChild(div);
  }
}

async function refreshOntologyIndex() {
  state.ontologies = await loadOntologies();
  for (const ont of state.ontologies) {
    state.prefixes = mergePrefixes(state.prefixes, extractPrefixes(ont.turtle));
  }
  state.ontologyIndex = buildOntologyIndex(state.ontologies, state.prefixes);
  renderPrefixTable();
}

async function fetchJson(url) {
  const res = await fetch(url);
  const text = await res.text();
  if (!res.ok) {
    let message = '';
    if (text) {
      try {
        const body = JSON.parse(text);
        if (typeof body.detail === 'string') message = body.detail;
        else if (Array.isArray(body.detail)) {
          message = body.detail.map((d) => d.msg || JSON.stringify(d)).join('; ');
        } else if (typeof body.message === 'string') message = body.message;
        else if (typeof body.error === 'string') message = body.error;
        else message = text.slice(0, 400);
      } catch {
        message = text.slice(0, 400);
      }
    }
    if (!message) {
      if (res.status === 401) {
        message = 'Not authorized. Open this app in Databricks and approve user authorization scopes.';
      } else if (res.status === 403) {
        message = 'Access denied. Check Unity Catalog permissions and app user_api_scopes.';
      } else {
        message = `Request failed (HTTP ${res.status})`;
      }
    }
    throw new Error(message);
  }
  return text ? JSON.parse(text) : null;
}

async function loadCatalogs() {
  if (state.ucCache.catalogs) return state.ucCache.catalogs;
  const data = await fetchJson('/api/uc/catalogs');
  state.ucCache.catalogs = data.catalogs || data;
  return state.ucCache.catalogs;
}

async function loadSchemas(catalog) {
  const data = await fetchJson(`/api/uc/schemas?catalog=${encodeURIComponent(catalog)}`);
  return data.schemas || data;
}

async function loadTables(catalog, schema) {
  const data = await fetchJson(
    `/api/uc/tables?catalog=${encodeURIComponent(catalog)}&schema=${encodeURIComponent(schema)}`,
  );
  return data.tables || data;
}

async function loadColumns(catalog, schema, table) {
  const data = await fetchJson(
    `/api/uc/columns?catalog=${encodeURIComponent(catalog)}&schema=${encodeURIComponent(schema)}&table=${encodeURIComponent(table)}`,
  );
  return data.columns || data;
}

function parseTableName(fullName) {
  const parts = (fullName || '').split('.');
  if (parts.length >= 3) return { catalog: parts[0], schema: parts[1], table: parts.slice(2).join('.') };
  return { catalog: '', schema: '', table: '' };
}

function attachAutocomplete(input, getOptions, { prepare } = {}) {
  if (!input || input.closest('.combobox-wrap')) return;

  const wrap = document.createElement('div');
  wrap.className = 'combobox-wrap';
  input.parentNode.insertBefore(wrap, input);
  wrap.appendChild(input);
  const list = document.createElement('div');
  list.className = 'combobox-list';
  wrap.appendChild(list);

  let loadGen = 0;

  function hideList() {
    list.classList.remove('open');
  }

  function renderList(options) {
    if (document.activeElement !== input) return;

    const query = (input.value || '').toLowerCase();
    const opts = options.filter((o) => !query || String(o).toLowerCase().includes(query));
    list.innerHTML = '';
    if (!opts.length) {
      hideList();
      return;
    }
    opts.slice(0, 50).forEach((o) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.textContent = o;
      btn.addEventListener('mousedown', (e) => {
        e.preventDefault();
        input.value = o;
        hideList();
        input.dispatchEvent(new Event('change', { bubbles: true }));
      });
      list.appendChild(btn);
    });
    list.classList.add('open');
  }

  async function refreshList() {
    const gen = ++loadGen;
    if (prepare) {
      try {
        await prepare();
      } catch {
        if (gen !== loadGen || document.activeElement !== input) return;
        renderList([]);
        return;
      }
    }
    if (gen !== loadGen || document.activeElement !== input) return;
    const options = getOptions();
    renderList(Array.isArray(options) ? options : []);
  }

  input.addEventListener('focus', refreshList);
  input.addEventListener('input', refreshList);
  input.addEventListener('blur', () => {
    loadGen += 1;
    hideList();
  });
}

async function ensureColumnsCached(getTableName) {
  const p = parseTableName(getTableName());
  if (!p.catalog || !p.schema || !p.table) return [];
  const key = `${p.catalog}.${p.schema}.${p.table}`;
  if (!state._colCache) state._colCache = {};
  if (!state._colCache[key]) {
    try {
      const cols = await loadColumns(p.catalog, p.schema, p.table);
      state._colCache[key] = cols.map((c) => c.name);
    } catch {
      state._colCache[key] = [];
    }
  }
  return state._colCache[key];
}

function insertAtCursor(input, text) {
  const start = input.selectionStart ?? input.value.length;
  const end = input.selectionEnd ?? input.value.length;
  input.value = input.value.slice(0, start) + text + input.value.slice(end);
  const pos = start + text.length;
  input.setSelectionRange(pos, pos);
  input.focus();
  input.dispatchEvent(new Event('change'));
}

function setupColumnInsertSelect(select, getTableName, onInsert) {
  if (!select) return;
  async function populate() {
    const cols = await ensureColumnsCached(getTableName);
    select.innerHTML = '<option value="">+ column…</option>';
    cols.forEach((name) => {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      select.appendChild(opt);
    });
  }
  select.addEventListener('focus', populate);
  select.addEventListener('mousedown', populate);
  select.addEventListener('change', () => {
    if (!select.value) return;
    onInsert(`{${select.value}}`);
    select.value = '';
  });
}

function classOptions() {
  const active = state.ontologies.some((o) => o.active);
  if (!active) return [];
  return state.ontologyIndex.classes.map((c) => compactIri(c, state.prefixes));
}

function propertyOptions() {
  const active = state.ontologies.some((o) => o.active);
  if (!active) return [];
  return state.ontologyIndex.properties.map((p) => compactIri(p, state.prefixes));
}

function createUcPicker(container, initial = {}, onChange) {
  const picker = { catalog: initial.catalog || '', schema: initial.schema || '', table: initial.table || '' };
  container.innerHTML = `
    <select class="select" data-uc="catalog"><option value="">Catalog…</option></select>
    <select class="select" data-uc="schema" disabled><option value="">Schema…</option></select>
    <select class="select" data-uc="table" disabled><option value="">Table…</option></select>
  `;
  const selCatalog = container.querySelector('[data-uc="catalog"]');
  const selSchema = container.querySelector('[data-uc="schema"]');
  const selTable = container.querySelector('[data-uc="table"]');

  async function populateCatalogs() {
    try {
      const catalogs = await loadCatalogs();
      selCatalog.innerHTML = '<option value="">Catalog…</option>';
      catalogs.forEach((c) => {
        const opt = document.createElement('option');
        opt.value = c; opt.textContent = c;
        if (c === picker.catalog) opt.selected = true;
        selCatalog.appendChild(opt);
      });
    } catch (e) {
      selCatalog.innerHTML = '<option value="">Catalog unavailable</option>';
      const errOpt = document.createElement('option');
      errOpt.disabled = true;
      errOpt.textContent = e.message || 'Failed to load catalogs';
      selCatalog.appendChild(errOpt);
      console.error('Failed to load catalogs:', e);
    }
  }

  async function populateSchemas() {
    selSchema.innerHTML = '<option value="">Schema…</option>';
    selSchema.disabled = !picker.catalog;
    if (!picker.catalog) return;
    const schemas = await loadSchemas(picker.catalog);
    schemas.forEach((s) => {
      const opt = document.createElement('option');
      opt.value = s; opt.textContent = s;
      if (s === picker.schema) opt.selected = true;
      selSchema.appendChild(opt);
    });
    selSchema.disabled = false;
  }

  async function populateTables() {
    selTable.innerHTML = '<option value="">Table…</option>';
    selTable.disabled = !picker.schema;
    if (!picker.catalog || !picker.schema) return;
    const tables = await loadTables(picker.catalog, picker.schema);
    tables.forEach((t) => {
      const opt = document.createElement('option');
      opt.value = t; opt.textContent = t;
      if (t === picker.table) opt.selected = true;
      selTable.appendChild(opt);
    });
    selTable.disabled = false;
  }

  selCatalog.addEventListener('change', async () => {
    picker.catalog = selCatalog.value;
    picker.schema = '';
    picker.table = '';
    await populateSchemas();
    selTable.innerHTML = '<option value="">Table…</option>';
    selTable.disabled = true;
    onChange?.(picker);
  });
  selSchema.addEventListener('change', async () => {
    picker.schema = selSchema.value;
    picker.table = '';
    await populateTables();
    onChange?.(picker);
  });
  selTable.addEventListener('change', () => {
    picker.table = selTable.value;
    onChange?.(picker);
  });

  populateCatalogs().then(async () => {
    if (picker.catalog) await populateSchemas();
    if (picker.schema) await populateTables();
  });

  return {
    getValue: () => ({ ...picker }),
    getFullTableName: () =>
      picker.catalog && picker.schema && picker.table
        ? `${picker.catalog}.${picker.schema}.${picker.table}`
        : '',
  };
}

function setupColumnAutocomplete(input, getTableName) {
  if (!input) return;
  attachAutocomplete(
    input,
    () => {
      const p = parseTableName(getTableName());
      const key = `${p.catalog}.${p.schema}.${p.table}`;
      return state._colCache?.[key] || [];
    },
    { prepare: () => ensureColumnsCached(getTableName) },
  );
}

function setupOntologyAutocomplete(input, getOptions) {
  if (!input) return;
  attachAutocomplete(input, getOptions);
}

function renderVisualEditor() {
  const cards = parseTriplesMaps(state.store);
  visualCards.innerHTML = '';
  cards.forEach((card) => {
    visualCards.appendChild(renderTriplesMapCard(card, cards));
  });
}

function logicalTableUiType(logicalTable) {
  if (logicalTable.type === 'sqlQuery') return 'sqlQuery';
  return 'tableName';
}

function renderTriplesMapCard(card, cards) {
  const article = document.createElement('article');
  article.className = 'triples-map-card card';
  const ltType = logicalTableUiType(card.logicalTable);
  const parsed = parseTableName(card.logicalTable.value);
  const isTableName = ltType === 'tableName';

  article.innerHTML = `
    <header>
      <h3>Triples Map: <input type="text" class="input" value="${esc(card.id)}" data-field="id" style="width:120px" /></h3>
      <button type="button" class="btn btn-outline" data-remove-map>Remove</button>
    </header>
    ${card.hasStubs ? '<div class="stub-card alert alert-warning">Contains unsupported constructs — preserved in Text Editor</div>' : ''}
    <div class="field-row">
      <label>Logical table</label>
      <select class="select" data-field="lt-type">
        <option value="tableName" ${ltType === 'tableName' ? 'selected' : ''}>tableName</option>
        <option value="sqlQuery" ${ltType === 'sqlQuery' ? 'selected' : ''}>SQLQuery</option>
      </select>
    </div>
    <div data-lt-tableName class="${ltType === 'tableName' ? '' : 'hidden'}">
      <div class="field-row">
        <label>Table</label>
        <div class="uc-picker" data-uc-container></div>
      </div>
      <input type="text" class="input" placeholder="catalog.schema.table" value="${esc(card.logicalTable.value)}" data-field="tableName" />
    </div>
    <div data-lt-sqlQuery class="${ltType === 'sqlQuery' ? '' : 'hidden'}">
      <textarea class="input" data-field="sqlQuery" rows="4">${esc(card.logicalTable.value)}</textarea>
    </div>
    <h4>Subject map</h4>
    <div class="field-row">
      <label>Template</label>
      <div class="template-input-row">
        <input type="text" class="input" value="${esc(card.subjectMap.template)}" data-field="sm-template" placeholder="http://example.org/{id}" />
        <select data-sm-template-columns class="select column-insert-select" ${isTableName ? '' : 'disabled'} title="Insert column placeholder">
          <option value="">+ column…</option>
        </select>
      </div>
    </div>
    <div class="field-row"><label>Class</label><input type="text" class="input" value="${esc(card.subjectMap.class)}" data-field="sm-class" /></div>
    <h4>Predicate-object maps</h4>
    <div data-pom-container></div>
    <button type="button" class="btn btn-outline" data-add-pom>New predicate-object map</button>
  `;

  const ucPicker = createUcPicker(article.querySelector('[data-uc-container]'), parsed, () => {
    const full = ucPicker.getFullTableName();
    if (full) {
      article.querySelector('[data-field="tableName"]').value = full;
      updateLogicalTable(state.store, card.id, { type: 'tableName', value: full });
      if (!card.id || card.id === '#NewMap') {
        const newId = tableToFragmentId(full);
        renameTriplesMap(state.store, card.id, newId);
        renderVisualEditor();
      }
      markDirty();
    }
  });

  article.querySelector('[data-field="lt-type"]').addEventListener('change', (e) => {
    const ltType = e.target.value;
    const value = ltType === 'tableName'
      ? article.querySelector('[data-field="tableName"]').value
      : article.querySelector('[data-field="sqlQuery"]').value;
    updateLogicalTable(state.store, card.id, { type: ltType, value });
    article.querySelector('[data-lt-tableName]').classList.toggle('hidden', ltType !== 'tableName');
    article.querySelector('[data-lt-sqlQuery]').classList.toggle('hidden', ltType !== 'sqlQuery');
    markDirty();
  });
  article.querySelector('[data-field="tableName"]').addEventListener('change', (e) => {
    updateLogicalTable(state.store, card.id, { type: 'tableName', value: e.target.value });
    markDirty();
  });
  article.querySelector('[data-field="sqlQuery"]').addEventListener('change', (e) => {
    updateLogicalTable(state.store, card.id, { type: 'sqlQuery', value: e.target.value });
    markDirty();
  });
  article.querySelector('[data-field="id"]').addEventListener('change', (e) => {
    const oldId = card.id;
    let newId = e.target.value.trim();
    if (!newId.startsWith('#')) newId = `#${newId}`;
    renameTriplesMap(state.store, oldId, newId);
    renderVisualEditor();
    markDirty();
  });
  const smTemplateInput = article.querySelector('[data-field="sm-template"]');
  smTemplateInput.addEventListener('change', (e) => {
    updateSubjectMap(state.store, card.id, { ...card.subjectMap, template: e.target.value }, state.prefixes);
    markDirty();
  });
  const smClassInput = article.querySelector('input[data-field="sm-class"]');
  smClassInput?.addEventListener('change', (e) => {
    updateSubjectMap(state.store, card.id, { ...card.subjectMap, class: e.target.value }, state.prefixes);
    markDirty();
  });

  if (isTableName) {
    setupColumnInsertSelect(
      article.querySelector('[data-sm-template-columns]'),
      () => card.logicalTable.value,
      (token) => {
        insertAtCursor(smTemplateInput, token);
        updateSubjectMap(state.store, card.id, { ...card.subjectMap, template: smTemplateInput.value }, state.prefixes);
        markDirty();
      },
    );
  }
  setupOntologyAutocomplete(smClassInput, classOptions);

  const pomContainer = article.querySelector('[data-pom-container]');
  (card.predicateObjectMaps || []).forEach((pom, pomIdx) => {
    pomContainer.appendChild(renderPomCard(card, pom, pomIdx, isTableName, cards));
  });

  article.querySelector('[data-add-pom]').addEventListener('click', () => {
    addPredicateObjectMap(state.store, card.id);
    renderVisualEditor();
    markDirty();
  });
  article.querySelector('[data-remove-map]').addEventListener('click', () => {
    if (!confirm('Remove this triples map?')) return;
    removeTriplesMap(state.store, card.id);
    renderVisualEditor();
    markDirty();
  });

  return article;
}

function renderPomCard(card, pom, pomIdx, isTableName, cards) {
  const div = document.createElement('div');
  div.className = 'pom-section';

  if (pom.objectMap?.type === 'stub') {
    div.innerHTML = `<div class="stub-card alert alert-warning">${esc(pom.objectMap.reason || 'unsupported — edit in Text Editor')}</div>`;
    return div;
  }

  const omType = pom.objectMap?.type || 'column';
  const fragmentIds = cards.map((c) => c.id);

  div.innerHTML = `
    <div class="field-row"><label>Predicate</label><input type="text" class="input" value="${esc(pom.predicate)}" data-pom-predicate /></div>
    <div class="field-row"><label>Object type</label>
      <select class="select" data-pom-om-type>
        <option value="column" ${omType === 'column' ? 'selected' : ''}>column</option>
        <option value="template" ${omType === 'template' ? 'selected' : ''}>template</option>
        <option value="constant" ${omType === 'constant' ? 'selected' : ''}>constant</option>
        <option value="parentJoin" ${omType === 'parentJoin' ? 'selected' : ''}>parent join</option>
      </select>
    </div>
    <div data-om-column class="${omType === 'column' ? '' : 'hidden'}">
      <div class="field-row"><label>Column</label><input type="text" class="input" value="${esc(pom.objectMap.column || '')}" data-om-column /></div>
    </div>
    <div data-om-template class="${omType === 'template' ? '' : 'hidden'}">
      <div class="field-row">
        <label>Template</label>
        <div class="template-input-row">
          <input type="text" class="input" value="${esc(pom.objectMap.template || '')}" data-om-template />
          <select data-om-template-columns class="select column-insert-select" ${isTableName ? '' : 'disabled'} title="Insert column placeholder">
            <option value="">+ column…</option>
          </select>
        </div>
      </div>
    </div>
    <div data-om-constant class="${omType === 'constant' ? '' : 'hidden'}">
      <div class="field-row"><label>Constant</label><input type="text" class="input" value="${esc(pom.objectMap.constant || '')}" data-om-constant /></div>
    </div>
    <div data-om-parentJoin class="${omType === 'parentJoin' ? '' : 'hidden'}">
      <div class="field-row"><label>Parent map</label>
        <select class="select" data-om-parent-select>
          <option value="">— select —</option>
          ${fragmentIds.map((id) => `<option value="${esc(id)}" ${pom.objectMap.parentTriplesMap === id ? 'selected' : ''}>${esc(id)}</option>`).join('')}
        </select>
        <input type="text" class="input" placeholder="or manual IRI" value="${esc(pom.objectMap.parentTriplesMap || '')}" data-om-parent-manual />
      </div>
      <div data-parent-warning></div>
      <div class="field-row"><label>Join child</label><input type="text" class="input" value="${esc(pom.objectMap.joinCondition?.child || '')}" data-om-jc-child /></div>
      <div class="field-row"><label>Join parent</label><input type="text" class="input" value="${esc(pom.objectMap.joinCondition?.parent || '')}" data-om-jc-parent /></div>
    </div>
    <button type="button" class="btn btn-outline" data-remove-pom>Remove POM</button>
  `;

  const pomPredicateInput = div.querySelector('input[data-pom-predicate]');

  function currentPomFromDom() {
    const omType = div.querySelector('[data-pom-om-type]')?.value || 'column';
    const objectMap = { type: omType };
    if (omType === 'column') {
      objectMap.column = div.querySelector('input[data-om-column]')?.value ?? '';
    } else if (omType === 'template') {
      objectMap.template = div.querySelector('input[data-om-template]')?.value ?? '';
    } else if (omType === 'constant') {
      objectMap.constant = div.querySelector('input[data-om-constant]')?.value ?? '';
    } else if (omType === 'parentJoin') {
      objectMap.parentTriplesMap = div.querySelector('[data-om-parent-manual]')?.value ?? '';
      objectMap.joinCondition = {
        child: div.querySelector('[data-om-jc-child]')?.value ?? '',
        parent: div.querySelector('[data-om-jc-parent]')?.value ?? '',
      };
    }
    return {
      predicate: pomPredicateInput?.value ?? pom.predicate,
      objectMap,
    };
  }

  const showOm = (type) => {
    const objectMap = { type };
    if (type === 'column') objectMap.column = '';
    if (type === 'template') objectMap.template = '';
    if (type === 'constant') objectMap.constant = '';
    if (type === 'parentJoin') {
      objectMap.parentTriplesMap = '';
      objectMap.joinCondition = { child: '', parent: '' };
    }
    updatePredicateObjectMap(state.store, card.id, pomIdx, {
      predicate: pomPredicateInput?.value ?? pom.predicate,
      objectMap,
    }, state.prefixes);
    renderVisualEditor();
    markDirty();
  };

  div.querySelector('[data-pom-om-type]').addEventListener('change', (e) => showOm(e.target.value));
  pomPredicateInput?.addEventListener('change', (e) => {
    const current = currentPomFromDom();
    updatePredicateObjectMap(state.store, card.id, pomIdx, {
      predicate: e.target.value,
      objectMap: current.objectMap,
    }, state.prefixes);
    markDirty();
  });
  setupOntologyAutocomplete(pomPredicateInput, propertyOptions);
  const omColumnInput = div.querySelector('input[data-om-column]');
  omColumnInput?.addEventListener('change', (e) => {
    const current = currentPomFromDom();
    updatePredicateObjectMap(state.store, card.id, pomIdx, {
      predicate: current.predicate,
      objectMap: { ...current.objectMap, column: e.target.value },
    }, state.prefixes);
    markDirty();
  });
  const omTemplateInput = div.querySelector('input[data-om-template]');
  omTemplateInput?.addEventListener('change', (e) => {
    const current = currentPomFromDom();
    updatePredicateObjectMap(state.store, card.id, pomIdx, {
      predicate: current.predicate,
      objectMap: { ...current.objectMap, template: e.target.value },
    }, state.prefixes);
    markDirty();
  });
  div.querySelector('input[data-om-constant]')?.addEventListener('change', (e) => {
    const current = currentPomFromDom();
    updatePredicateObjectMap(state.store, card.id, pomIdx, {
      predicate: current.predicate,
      objectMap: { ...current.objectMap, constant: e.target.value },
    }, state.prefixes);
    markDirty();
  });

  if (isTableName) {
    setupColumnAutocomplete(omColumnInput, () => card.logicalTable.value);
    setupColumnAutocomplete(div.querySelector('[data-om-jc-child]'), () => card.logicalTable.value);
    setupColumnInsertSelect(
      div.querySelector('[data-om-template-columns]'),
      () => card.logicalTable.value,
      (token) => {
        insertAtCursor(omTemplateInput, token);
        const current = currentPomFromDom();
        updatePredicateObjectMap(state.store, card.id, pomIdx, {
          predicate: current.predicate,
          objectMap: { ...current.objectMap, template: omTemplateInput.value },
        }, state.prefixes);
        markDirty();
      },
    );
  }

  setupColumnAutocomplete(div.querySelector('[data-om-jc-parent]'), () => {
    const parentCard = cards.find((c) => c.id === pom.objectMap.parentTriplesMap);
    return parentCard?.logicalTable?.type === 'tableName' ? parentCard.logicalTable.value : '';
  });

  const warnEl = div.querySelector('[data-parent-warning]');
  const updateParentWarning = () => {
    const pid = div.querySelector('[data-om-parent-manual]')?.value || pom.objectMap.parentTriplesMap;
    warnEl.innerHTML = pid && !cards.some((c) => c.id === pid)
      ? '<span class="warning-text">Warning: parent triples map not found in editor</span>'
      : '';
  };

  div.querySelector('[data-om-parent-select]')?.addEventListener('change', (e) => {
    div.querySelector('[data-om-parent-manual]').value = e.target.value;
    const current = currentPomFromDom();
    updatePredicateObjectMap(state.store, card.id, pomIdx, {
      predicate: current.predicate,
      objectMap: { ...current.objectMap, parentTriplesMap: e.target.value },
    }, state.prefixes);
    updateParentWarning();
    markDirty();
  });
  div.querySelector('[data-om-parent-manual]')?.addEventListener('change', (e) => {
    const current = currentPomFromDom();
    updatePredicateObjectMap(state.store, card.id, pomIdx, {
      predicate: current.predicate,
      objectMap: { ...current.objectMap, parentTriplesMap: e.target.value },
    }, state.prefixes);
    updateParentWarning();
    markDirty();
  });
  div.querySelector('[data-om-jc-child]')?.addEventListener('change', (e) => {
    const current = currentPomFromDom();
    updatePredicateObjectMap(state.store, card.id, pomIdx, {
      predicate: current.predicate,
      objectMap: {
        ...current.objectMap,
        joinCondition: { ...current.objectMap.joinCondition, child: e.target.value },
      },
    }, state.prefixes);
    markDirty();
  });
  div.querySelector('[data-om-jc-parent]')?.addEventListener('change', (e) => {
    const current = currentPomFromDom();
    updatePredicateObjectMap(state.store, card.id, pomIdx, {
      predicate: current.predicate,
      objectMap: {
        ...current.objectMap,
        joinCondition: { ...current.objectMap.joinCondition, parent: e.target.value },
      },
    }, state.prefixes);
    markDirty();
  });
  updateParentWarning();

  div.querySelector('[data-remove-pom]').addEventListener('click', () => {
    removePredicateObjectMap(state.store, card.id, pomIdx);
    renderVisualEditor();
    markDirty();
  });

  return div;
}

async function loadLiveMapping() {
  if (state.dirty && !confirm('Replace current draft with live mapping?')) return;
  try {
    const data = await fetchJson('/api/mapping/live');
    await loadTurtleIntoState(data.turtle);
    markDirty();
    await persistDraft();
  } catch (e) {
    alert(`Failed to load live mapping: ${e.message}`);
  }
}

function uploadFile(file) {
  const reader = new FileReader();
  reader.onload = async () => {
    await loadTurtleIntoState(reader.result);
    markDirty();
    await persistDraft();
  };
  reader.readAsText(file);
}

async function downloadMapping() {
  let turtle;
  if (state.mode === 'text' && state.codeMirror) turtle = state.codeMirror.getValue();
  else turtle = await getTurtleFromStore();
  const filename = prompt('Download filename:', 'mapping.ttl');
  if (!filename) return;
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([turtle], { type: 'text/turtle' }));
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

async function clearDraftAction() {
  if (!confirm('Clear draft and start blank?')) return;
  await clearDraft();
  state.prefixes = { ...DEFAULT_PREFIXES };
  state.store = parseTurtle('', state.prefixes);
  renderPrefixTable();
  if (state.mode === 'visual') renderVisualEditor();
  else if (state.codeMirror) state.codeMirror.setValue('');
  markClean();
}

let autogenUcPicker = null;
let autogenPollTimer = null;

function showAutogenDialog() {
  autogenDialog.showModal();
  autogenUcPicker = createUcPicker($('#auto-uc-pickers'), {});
  $('#auto-progress').style.display = 'none';
  $('#btn-auto-retry').style.display = 'none';
  state.autogenErrors = [];
}

async function submitAutogenerate(retryOnly = false) {
  const mode = document.querySelector('input[name="auto-mode"]:checked')?.value;
  const picker = autogenUcPicker.getValue();
  if (!picker.catalog || !picker.schema) { alert('Select catalog and schema'); return; }
  if (mode === 'table' && !picker.table) { alert('Select a table'); return; }

  const mappingTurtle = state.mode === 'text' && state.codeMirror
    ? state.codeMirror.getValue()
    : await getTurtleFromStore();

  let ontologyTurtle = '';
  for (const ont of state.ontologies.filter((o) => o.active)) {
    ontologyTurtle += ont.turtle + '\n';
  }

  const body = {
    mode,
    catalog: picker.catalog,
    schema: picker.schema,
    table: mode === 'table' ? picker.table : null,
    prefixes: state.prefixes,
    ontologyTurtle,
    mappingTurtle,
    retryErrors: retryOnly ? state.autogenErrors : null,
  };

  $('#auto-progress').style.display = 'block';
  $('#auto-status-text').textContent = 'Submitting…';

  try {
    const res = await fetch('/api/autogenerate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    const { jobId } = await res.json();
    pollAutogenerate(jobId);
  } catch (e) {
    $('#auto-status-text').textContent = `Error: ${e.message}`;
  }
}

function pollAutogenerate(jobId) {
  if (autogenPollTimer) clearInterval(autogenPollTimer);
  autogenPollTimer = setInterval(async () => {
    try {
      const status = await fetchJson(`/api/autogenerate/${jobId}`);
      const pct = status.tablesTotal ? Math.round((status.tablesCompleted / status.tablesTotal) * 100) : 0;
      $('#auto-progress-fill').style.width = `${pct}%`;
      $('#auto-status-text').textContent = status.status === 'running'
        ? `Processing ${status.currentTable || '…'} (${status.tablesCompleted}/${status.tablesTotal})`
        : `Status: ${status.status}`;

      const errList = $('#auto-errors');
      errList.innerHTML = '';
      (status.errors || []).forEach((e) => {
        const li = document.createElement('li');
        li.textContent = `${e.table}: ${e.error}`;
        errList.appendChild(li);
      });

      if (status.status !== 'running') {
        clearInterval(autogenPollTimer);
        autogenPollTimer = null;
        state.autogenErrors = status.errors || [];

        if (status.status === 'complete' || status.status === 'partial') {
          try {
            validateTurtle(status.turtle, state.prefixes);
            state.prefixes = mergePrefixes(state.prefixes, extractPrefixes(status.turtle));
            mergeTurtleIntoStore(state.store, status.turtle, state.prefixes);
            renderPrefixTable();
            if (state.mode === 'visual') renderVisualEditor();
            else if (state.codeMirror) state.codeMirror.setValue(await serializeStore(state.store, state.prefixes));
            markDirty();
            await persistDraft();
          } catch (e) {
            $('#auto-status-text').textContent = `Merge failed: ${e.message}`;
          }
        }
        if (status.errors?.length) $('#btn-auto-retry').style.display = '';
      }
    } catch (e) {
      clearInterval(autogenPollTimer);
      $('#auto-status-text').textContent = `Poll error: ${e.message}`;
    }
  }, 2000);
}

function setupNavGuard() {
  document.querySelectorAll('[data-nav-link]').forEach((link) => {
    link.addEventListener('click', async (e) => {
      if (!state.dirty) return;
      e.preventDefault();
      if (confirm('You have unsaved changes. OK = save and leave, Cancel = stay')) {
        await persistDraft();
        window.location.href = link.href;
      }
    });
  });
  window.addEventListener('beforeunload', (e) => {
    markDirty();
    if (state.dirty) { e.preventDefault(); e.returnValue = ''; }
  });
}

function esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function setPrefixSectionCollapsed(collapsed) {
  const section = $('#prefix-section');
  const toggle = $('#btn-toggle-prefixes');
  if (!section || !toggle) return;
  section.classList.toggle('collapsed', collapsed);
  toggle.setAttribute('aria-expanded', String(!collapsed));
  try {
    localStorage.setItem(PREFIX_COLLAPSED_KEY, collapsed ? '1' : '0');
  } catch {
    /* storage unavailable */
  }
}

function initPrefixSectionCollapse() {
  const toggle = $('#btn-toggle-prefixes');
  if (!toggle) return;
  let collapsed = false;
  try {
    collapsed = localStorage.getItem(PREFIX_COLLAPSED_KEY) === '1';
  } catch {
    /* storage unavailable */
  }
  setPrefixSectionCollapsed(collapsed);
  toggle.addEventListener('click', () => {
    setPrefixSectionCollapsed($('#prefix-section').classList.contains('collapsed') === false);
  });
}

async function init() {
  initCodeMirror();
  setupNavGuard();

  const restored = await restoreDraft();
  if (!restored) {
    state.prefixes = { ...DEFAULT_PREFIXES };
    state.store = parseTurtle('', state.prefixes);
  }

  updateModeVisibility();
  renderPrefixTable();
  await refreshOntologyList();
  await refreshOntologyIndex();

  if (state.mode === 'visual') renderVisualEditor();
  else if (state.codeMirror) state.codeMirror.setValue(await serializeStore(state.store, state.prefixes));

  markClean();

  initPrefixSectionCollapse();
  btnModeVisual.addEventListener('click', () => switchMode('visual'));
  btnModeText.addEventListener('click', () => switchMode('text'));
  $('#btn-new-map').addEventListener('click', () => {
    addTriplesMap(state.store, newEmptyCard(), state.prefixes);
    renderVisualEditor();
    markDirty();
  });
  $('#btn-load-live').addEventListener('click', loadLiveMapping);
  $('#input-upload').addEventListener('change', (e) => { if (e.target.files[0]) uploadFile(e.target.files[0]); e.target.value = ''; });
  $('#btn-download').addEventListener('click', downloadMapping);
  $('#btn-clear').addEventListener('click', clearDraftAction);
  $('#btn-autogenerate').addEventListener('click', showAutogenDialog);
  $('#btn-add-prefix').addEventListener('click', () => {
    syncPrefixesFromTable();
    state.prefixes[`p${Object.keys(state.prefixes).length}`] = 'http://example.org/';
    renderPrefixTable();
    markDirty();
  });
  $('#input-ontology').addEventListener('change', async (e) => {
    for (const file of e.target.files) {
      const turtle = await file.text();
      await saveOntology(file.name, turtle, true);
      state.prefixes = mergePrefixes(state.prefixes, extractPrefixes(turtle));
    }
    renderPrefixTable();
    await refreshOntologyList();
    await refreshOntologyIndex();
    e.target.value = '';
  });
  $('#btn-auto-start').addEventListener('click', () => submitAutogenerate(false));
  $('#btn-auto-retry').addEventListener('click', () => submitAutogenerate(true));
  autogenDialog.querySelectorAll('[data-close]').forEach((btn) => {
    btn.addEventListener('click', () => autogenDialog.close());
  });
  window.addEventListener('pagehide', () => { if (state.dirty) persistDraft(); });
}

init().catch((e) => {
  console.error('Mapper init failed:', e);
  alert(`Mapper failed to initialize: ${e.message}`);
});
