/**
 * R2RML Mapper — data layer (no DOM).
 * N3 is loaded via classic <script> tag (global N3) — see index.html.
 */
if (!window.N3) {
  throw new Error('N3 not loaded — include the N3 script tag before mapper modules.');
}
const { Parser, Writer, Store, DataFactory } = window.N3;

const { namedNode, literal, blankNode, quad } = DataFactory;

const RR = 'http://www.w3.org/ns/r2rml#';
const RDF = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#';
const RDFS = 'http://www.w3.org/2000/01/rdf-schema#';
const OWL = 'http://www.w3.org/2002/07/owl#';

export const DEFAULT_PREFIXES = {
  rr: RR,
  rdf: RDF,
  rdfs: RDFS,
  owl: OWL,
  xsd: 'http://www.w3.org/2001/XMLSchema#',
  ex: 'http://example.org/',
};

const DB_NAME = 'r2rml-mapper';
const DB_VERSION = 1;

// ── IndexedDB ────────────────────────────────────────────────────────────────

function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains('draft')) db.createObjectStore('draft');
      if (!db.objectStoreNames.contains('ontologies')) db.createObjectStore('ontologies');
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function idbGet(store, key) {
  return openDb().then(
    (db) =>
      new Promise((resolve, reject) => {
        const tx = db.transaction(store, 'readonly');
        const req = tx.objectStore(store).get(key);
        req.onsuccess = () => resolve(req.result ?? null);
        req.onerror = () => reject(req.error);
      }),
  );
}

function idbPut(store, key, value) {
  return openDb().then(
    (db) =>
      new Promise((resolve, reject) => {
        const tx = db.transaction(store, 'readwrite');
        tx.objectStore(store).put(value, key);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
      }),
  );
}

function idbDelete(store, key) {
  return openDb().then(
    (db) =>
      new Promise((resolve, reject) => {
        const tx = db.transaction(store, 'readwrite');
        tx.objectStore(store).delete(key);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
      }),
  );
}

function idbGetAll(store) {
  return openDb().then(
    (db) =>
      new Promise((resolve, reject) => {
        const tx = db.transaction(store, 'readonly');
        const req = tx.objectStore(store).getAll();
        req.onsuccess = () => resolve(req.result ?? []);
        req.onerror = () => reject(req.error);
      }),
  );
}

export async function saveDraft(turtle, prefixes, mode = 'visual') {
  await idbPut('draft', 'current', { turtle, prefixes, mode });
}

export async function loadDraft() {
  return idbGet('draft', 'current');
}

export async function clearDraft() {
  await idbDelete('draft', 'current');
}

export async function saveOntology(filename, turtle, active = true) {
  await idbPut('ontologies', filename, { filename, turtle, active });
}

export async function loadOntologies() {
  const all = await idbGetAll('ontologies');
  return all.filter(Boolean);
}

export async function deleteOntology(filename) {
  await idbDelete('ontologies', filename);
}

export async function setOntologyActive(filename, active) {
  const existing = await idbGet('ontologies', filename);
  if (existing) await idbPut('ontologies', filename, { ...existing, active });
}

// ── Prefix table ─────────────────────────────────────────────────────────────

export function extractPrefixes(turtle) {
  const prefixes = {};
  const prefixRe = /^@prefix\s+(\w*)\s*:\s*<([^>]+)>\s*\./gim;
  const sparqlPrefixRe = /^PREFIX\s+(\w*)\s*:\s*<([^>]+)>/gim;
  const baseRe = /^@base\s+<([^>]+)>\s*\./gim;
  let m;
  while ((m = prefixRe.exec(turtle)) !== null) {
    prefixes[m[1]] = m[2];
  }
  while ((m = sparqlPrefixRe.exec(turtle)) !== null) {
    if (!(m[1] in prefixes)) prefixes[m[1]] = m[2];
  }
  while ((m = baseRe.exec(turtle)) !== null) {
    prefixes.base = m[1];
  }
  return prefixes;
}

export function mergePrefixes(target, ...sources) {
  const out = { ...target };
  for (const src of sources) {
    if (!src) continue;
    for (const [k, v] of Object.entries(src)) {
      out[k] = v;
    }
  }
  return out;
}

// ── N3 store helpers ─────────────────────────────────────────────────────────

// N3.Parser prefixes blank node labels with b{N}_ on each parse by default, which
// compounds across pane-switch round-trips (visual → text → visual). Disable it.
const CHAINED_BNODE_PREFIX = /^(?:b\d+_)+/;

function remapTerm(term, idMap) {
  if (term.termType === 'BlankNode' && idMap.has(term.value)) {
    return blankNode(idMap.get(term.value));
  }
  return term;
}

function normalizeBlankNodeIds(store) {
  const quads = store.getQuads(null, null, null, null);
  const idMap = new Map();
  const used = new Set();

  for (const q of quads) {
    for (const term of [q.subject, q.object]) {
      if (term.termType !== 'BlankNode') continue;
      const stripped = term.value.replace(CHAINED_BNODE_PREFIX, '');
      if (stripped === term.value) used.add(stripped);
    }
  }

  for (const q of quads) {
    for (const term of [q.subject, q.object]) {
      if (term.termType !== 'BlankNode') continue;
      const stripped = term.value.replace(CHAINED_BNODE_PREFIX, '');
      if (stripped === term.value || idMap.has(term.value)) continue;

      let candidate = stripped;
      let n = 0;
      while (used.has(candidate)) {
        candidate = `${stripped}_${++n}`;
      }
      idMap.set(term.value, candidate);
      used.add(candidate);
    }
  }

  if (idMap.size === 0) return store;

  const normalized = new Store();
  for (const q of quads) {
    normalized.addQuad(
      quad(
        remapTerm(q.subject, idMap),
        q.predicate,
        remapTerm(q.object, idMap),
        q.graph,
      ),
    );
  }
  return normalized;
}

function countBlankReferences(store) {
  const refs = new Map();
  for (const q of store.getQuads(null, null, null, null)) {
    if (q.object.termType === 'BlankNode') {
      refs.set(q.object.value, (refs.get(q.object.value) || 0) + 1);
    }
  }
  return refs;
}

function groupQuadsByPredicate(quads) {
  const groups = [];
  const indexByPred = new Map();
  for (const q of quads) {
    const key = q.predicate.value;
    if (!indexByPred.has(key)) {
      indexByPred.set(key, groups.length);
      groups.push({ predicate: q.predicate, objects: [] });
    }
    groups[indexByPred.get(key)].objects.push(q.object);
  }
  const ordered = [];
  for (const g of groups) {
    for (const obj of g.objects) {
      ordered.push({ predicate: g.predicate, object: obj });
    }
  }
  return ordered;
}

function collectBlankSubgraph(store, node, visited) {
  if (visited.has(node.value)) return;
  visited.add(node.value);
  for (const q of store.getQuads(node, null, null, null)) {
    if (q.object.termType === 'BlankNode') collectBlankSubgraph(store, q.object, visited);
  }
}

function collectTriplesMapSubgraph(store, mapNode) {
  const visited = new Set([mapNode.value]);
  for (const q of store.getQuads(mapNode, null, null, null)) {
    if (q.object.termType === 'BlankNode') collectBlankSubgraph(store, q.object, visited);
  }
  return visited;
}

function objectTermForWriter(writer, store, obj, blankRefs) {
  if (obj.termType !== 'BlankNode' || blankRefs.get(obj.value) > 1) return obj;
  const children = groupQuadsByPredicate(store.getQuads(obj, null, null, null)).map(
    ({ predicate, object }) => ({
      predicate,
      object: objectTermForWriter(writer, store, object, blankRefs),
    }),
  );
  return writer.blank(children);
}

function serializeTriplesMap(writer, store, mapNode, blankRefs) {
  const mapQuads = store.getQuads(mapNode, null, null, null);
  const typePred = namedNode(`${RDF}type`);
  const typeQuad = mapQuads.find((q) => q.predicate.equals(typePred));
  const otherQuads = mapQuads.filter((q) => !q.predicate.equals(typePred));

  const orderedPreds = [
    namedNode(`${RR}logicalTable`),
    namedNode(`${RR}subjectMap`),
    namedNode(`${RR}predicateObjectMap`),
  ];
  const sortKey = (q) => {
    const idx = orderedPreds.findIndex((p) => p.equals(q.predicate));
    return idx >= 0 ? idx : orderedPreds.length;
  };
  otherQuads.sort((a, b) => sortKey(a) - sortKey(b) || a.predicate.value.localeCompare(b.predicate.value));

  if (typeQuad) {
    writer.addQuad(mapNode, typeQuad.predicate, typeQuad.object);
  }
  for (const q of otherQuads) {
    const obj =
      q.object.termType === 'BlankNode'
        ? objectTermForWriter(writer, store, q.object, blankRefs)
        : q.object;
    writer.addQuad(mapNode, q.predicate, obj);
  }
}

function serializeStructured(store, prefixes) {
  const merged = mergePrefixes(DEFAULT_PREFIXES, prefixes);
  const writer = new Writer({ prefixes: merged });
  const blankRefs = countBlankReferences(store);
  const mapNodes = store
    .getQuads(null, namedNode(`${RDF}type`), namedNode(`${RR}TriplesMap`), null)
    .map((q) => q.subject);

  const coveredSubjects = new Set();
  for (const mapNode of mapNodes) {
    for (const id of collectTriplesMapSubgraph(store, mapNode)) coveredSubjects.add(id);
    coveredSubjects.add(mapNode.value);
    serializeTriplesMap(writer, store, mapNode, blankRefs);
  }

  for (const q of store.getQuads(null, null, null, null)) {
    const subKey = q.subject.value;
    const objKey = q.object.termType === 'BlankNode' ? q.object.value : null;
    if (coveredSubjects.has(subKey) || (objKey && coveredSubjects.has(objKey))) continue;
    writer.addQuad(q);
  }

  return new Promise((resolve, reject) => {
    writer.end((err, out) => (err ? reject(err) : resolve(out)));
  });
}

export function parseTurtle(turtle, prefixes = {}) {
  const merged = mergePrefixes(DEFAULT_PREFIXES, prefixes);
  const parserOpts = { blankNodePrefix: '' };
  if (merged.base) parserOpts.baseIRI = merged.base;
  const parser = new Parser(parserOpts);
  // N3's callback API is async (microtask); sync parse returns quads immediately.
  const quads = parser.parse(turtle);
  const store = new Store();
  for (const q of quads) store.addQuad(q);
  return normalizeBlankNodeIds(store);
}

function storeToQuads(storeOrQuads) {
  if (storeOrQuads instanceof Store) {
    return storeOrQuads.getQuads(null, null, null, null);
  }
  return storeOrQuads;
}

function asStore(storeOrQuads) {
  if (storeOrQuads instanceof Store) return storeOrQuads;
  const store = new Store();
  for (const q of storeOrQuads) store.addQuad(q);
  return store;
}

export function serializeStore(quadsOrStore, prefixes = {}) {
  const store = asStore(quadsOrStore);
  return serializeStructured(store, prefixes);
}

function termStr(term) {
  if (!term) return '';
  if (term.termType === 'NamedNode') return term.value;
  if (term.termType === 'Literal') return JSON.stringify(term.value);
  if (term.termType === 'BlankNode') return term.value;
  return String(term);
}

function fragmentId(term) {
  if (!term) return '';
  const v = term.value;
  const idx = v.lastIndexOf('#');
  return idx >= 0 ? v.slice(idx) : v;
}

function resolvePrefixed(iri, prefixes) {
  if (!iri) return iri;
  if (iri.startsWith('<') && iri.endsWith('>')) return iri.slice(1, -1);
  const colon = iri.indexOf(':');
  if (colon > 0 && prefixes[iri.slice(0, colon)]) {
    return prefixes[iri.slice(0, colon)] + iri.slice(colon + 1);
  }
  return iri;
}

export function compactIri(iri, prefixes) {
  if (!iri) return iri;
  for (const [p, ns] of Object.entries(prefixes)) {
    if (iri.startsWith(ns)) return `${p}:${iri.slice(ns.length)}`;
  }
  if (iri.includes('#')) {
    const frag = iri.slice(iri.lastIndexOf('#'));
    return `<${frag}>`;
  }
  return iri;
}

// ── R2RML card model ─────────────────────────────────────────────────────────

const UNSUPPORTED_PROPS = new Set([
  `${RR}language`,
  `${RR}graphMap`,
  `${RR}graph`,
]);

export const XSD_DATATYPES = [
  'xsd:string',
  'xsd:boolean',
  'xsd:decimal',
  'xsd:float',
  'xsd:double',
  'xsd:integer',
  'xsd:long',
  'xsd:int',
  'xsd:short',
  'xsd:byte',
  'xsd:nonPositiveInteger',
  'xsd:negativeInteger',
  'xsd:nonNegativeInteger',
  'xsd:unsignedLong',
  'xsd:unsignedInt',
  'xsd:unsignedShort',
  'xsd:unsignedByte',
  'xsd:positiveInteger',
  'xsd:dateTime',
  'xsd:date',
  'xsd:time',
  'xsd:gYearMonth',
  'xsd:gYear',
  'xsd:gMonthDay',
  'xsd:gDay',
  'xsd:gMonth',
  'xsd:duration',
  'xsd:anyURI',
  'xsd:language',
  'xsd:base64Binary',
  'xsd:hexBinary',
  'xsd:normalizedString',
  'xsd:token',
];

function getObjects(store, subject, predicate) {
  return store.getQuads(subject, predicate, null, null).map((q) => q.object);
}

function getLiteral(store, subject, predicate) {
  const objs = getObjects(store, subject, predicate);
  const lit = objs.find((o) => o.termType === 'Literal');
  return lit ? lit.value : objs[0] ? termStr(objs[0]) : '';
}

function getNamedNodeIri(store, subject, predicate) {
  const objs = getObjects(store, subject, predicate);
  const nn = objs.find((o) => o.termType === 'NamedNode');
  return nn ? nn.value : '';
}

function hasUnsupportedProps(store, subject) {
  for (const q of store.getQuads(subject, null, null, null)) {
    if (UNSUPPORTED_PROPS.has(q.predicate.value)) return true;
  }
  return false;
}

function parseLogicalTable(store, ltNode) {
  const tableNamePred = namedNode(`${RR}tableName`);
  const sqlPred = namedNode(`${RR}SQLQuery`);
  if (getObjects(store, ltNode, tableNamePred).length > 0) {
    return { type: 'tableName', value: getLiteral(store, ltNode, tableNamePred) };
  }
  if (getObjects(store, ltNode, sqlPred).length > 0) {
    return { type: 'sqlQuery', value: getLiteral(store, ltNode, sqlPred) };
  }
  return { type: 'unknown', value: '' };
}

function parseSubjectMap(store, smNode) {
  return {
    template: getLiteral(store, smNode, namedNode(`${RR}template`)),
    column: getLiteral(store, smNode, namedNode(`${RR}column`)),
    class: getLiteral(store, smNode, namedNode(`${RR}class`)),
    _unsupported: hasUnsupportedProps(store, smNode),
  };
}

function parseJoinCondition(store, jcNode) {
  return {
    child: getLiteral(store, jcNode, namedNode(`${RR}child`)),
    parent: getLiteral(store, jcNode, namedNode(`${RR}parent`)),
  };
}

function parseObjectMap(store, omNode) {
  if (hasUnsupportedProps(store, omNode)) {
    return { type: 'stub', reason: 'unsupported construct (language/graph)' };
  }

  const datatype = getNamedNodeIri(store, omNode, namedNode(`${RR}datatype`));

  const parentRefs = getObjects(store, omNode, namedNode(`${RR}parentTriplesMap`));
  const joinConds = getObjects(store, omNode, namedNode(`${RR}joinCondition`));

  if (parentRefs.length > 0 || joinConds.length > 0) {
    const parent = parentRefs[0];
    const jcNode = joinConds[0];
    return {
      type: 'parentJoin',
      parentTriplesMap: parent ? fragmentId(parent) : '',
      joinCondition: jcNode ? parseJoinCondition(store, jcNode) : { child: '', parent: '' },
    };
  }

  const columnPred = namedNode(`${RR}column`);
  if (getObjects(store, omNode, columnPred).length > 0) {
    return { type: 'column', column: getLiteral(store, omNode, columnPred), datatype };
  }

  const templatePred = namedNode(`${RR}template`);
  if (getObjects(store, omNode, templatePred).length > 0) {
    return { type: 'template', template: getLiteral(store, omNode, templatePred), datatype };
  }

  const constant = getObjects(store, omNode, namedNode(`${RR}constant`));
  if (constant.length > 0) {
    const c = constant[0];
    return {
      type: 'constant',
      constant: c.termType === 'Literal' ? c.value : termStr(c),
      isIri: c.termType === 'NamedNode',
    };
  }

  // Linked object map with no type quads (legacy drafts) — default to empty column.
  if (store.getQuads(omNode, null, null, null).length === 0) {
    return { type: 'column', column: '' };
  }

  return { type: 'stub', reason: 'unsupported object map' };
}

function parsePredicateObjectMap(store, pomNode) {
  const predicate = getLiteral(store, pomNode, namedNode(`${RR}predicate`));
  const omNodes = getObjects(store, pomNode, namedNode(`${RR}objectMap`));
  const objectMap = omNodes[0] ? parseObjectMap(store, omNodes[0]) : { type: 'stub', reason: 'missing objectMap' };
  const unsupported = hasUnsupportedProps(store, pomNode);
  return { predicate, objectMap, _unsupported: unsupported };
}

export function parseTriplesMaps(storeOrQuads) {
  const store = asStore(storeOrQuads);
  const maps = [];
  const mapNodes = store
    .getQuads(null, namedNode(`${RDF}type`), namedNode(`${RR}TriplesMap`), null)
    .map((q) => q.subject);

  for (const mapNode of mapNodes) {
    const id = fragmentId(mapNode);
    const ltNodes = getObjects(store, mapNode, namedNode(`${RR}logicalTable`));
    const smNodes = getObjects(store, mapNode, namedNode(`${RR}subjectMap`));
    const pomNodes = getObjects(store, mapNode, namedNode(`${RR}predicateObjectMap`));

    const mapUnsupported = hasUnsupportedProps(store, mapNode);
    const logicalTable = ltNodes[0] ? parseLogicalTable(store, ltNodes[0]) : { type: 'unknown', value: '' };
    const subjectMap = smNodes[0]
      ? parseSubjectMap(store, smNodes[0])
      : { template: '', column: '', class: '', _unsupported: false };

    const predicateObjectMaps = pomNodes.map((pom) => parsePredicateObjectMap(store, pom));

    const hasStubs =
      mapUnsupported ||
      logicalTable.type === 'unknown' ||
      subjectMap._unsupported ||
      predicateObjectMaps.some((p) => p._unsupported || p.objectMap.type === 'stub');

    maps.push({
      id,
      logicalTable,
      subjectMap: {
        template: subjectMap.template,
        column: subjectMap.column,
        class: subjectMap.class,
      },
      predicateObjectMaps: predicateObjectMaps.map(({ predicate, objectMap }) => ({
        predicate,
        objectMap,
      })),
      hasStubs,
    });
  }
  return maps;
}

export function tableToFragmentId(tableName) {
  const seg = (tableName || 'Map').split('.').pop() || 'Map';
  const pascal = seg
    .split(/[_\-.]+/)
    .filter(Boolean)
    .map((p) => p.charAt(0).toUpperCase() + p.slice(1).toLowerCase())
    .join('');
  return `#${pascal || 'Map'}`;
}

function iriTerm(iri, prefixes) {
  if (!iri) return null;
  if (iri.startsWith('#')) return namedNode(iri);
  const resolved = resolvePrefixed(iri, prefixes);
  return namedNode(resolved);
}

function literalTerm(value) {
  return literal(value);
}

function sanitizeMapId(mapId) {
  return mapId.replace(/[^a-zA-Z0-9]/g, '');
}

function normalizeMapId(mapId) {
  return mapId.startsWith('#') ? mapId : `#${mapId}`;
}

function mapIriTerm(id) {
  return namedNode(normalizeMapId(id));
}

function findTriplesMap(store, mapId) {
  const norm = normalizeMapId(mapId);
  const mapNodes = store
    .getQuads(null, namedNode(`${RDF}type`), namedNode(`${RR}TriplesMap`), null)
    .map((q) => q.subject);
  return mapNodes.find((n) => fragmentId(n) === norm) ?? null;
}

function getLinkedBlank(store, subject, predicate) {
  const pred = typeof predicate === 'string' ? namedNode(predicate) : predicate;
  const objs = getObjects(store, subject, pred);
  return objs.find((o) => o.termType === 'BlankNode') ?? null;
}

function setObjectLiteral(store, subject, predicate, value) {
  const pred = typeof predicate === 'string' ? namedNode(predicate) : predicate;
  setObjectTerm(store, subject, pred, value !== '' && value != null ? literal(value) : null);
}

// Object maps need a type-discriminating quad even when the value is empty (new POM).
function setObjectMapLiteral(store, subject, predicate, value) {
  const pred = typeof predicate === 'string' ? namedNode(predicate) : predicate;
  setObjectTerm(store, subject, pred, literal(value ?? ''));
}

function setObjectTerm(store, subject, predicate, term) {
  const pred = typeof predicate === 'string' ? namedNode(predicate) : predicate;
  for (const q of store.getQuads(subject, pred, null, null)) {
    store.removeQuad(q);
  }
  if (term) {
    store.addQuad(quad(subject, pred, term));
  }
}

function removeQuadsForSubject(store, subject) {
  const quads = store.getQuads(subject, null, null, null);
  for (const q of quads) {
    if (q.object.termType === 'BlankNode') {
      removeQuadsForSubject(store, q.object);
    }
    store.removeQuad(q);
  }
}

function removeObjectMapSubgraph(store, omNode) {
  const joinConds = getObjects(store, omNode, namedNode(`${RR}joinCondition`));
  for (const jc of joinConds) {
    removeQuadsForSubject(store, jc);
  }
  removeQuadsForSubject(store, omNode);
}

function removeTriplesMapSubgraph(store, mapId) {
  const mapNode = findTriplesMap(store, mapId);
  if (!mapNode) return;

  const ltNode = getLinkedBlank(store, mapNode, namedNode(`${RR}logicalTable`));
  const smNode = getLinkedBlank(store, mapNode, namedNode(`${RR}subjectMap`));
  const pomNodes = getObjects(store, mapNode, namedNode(`${RR}predicateObjectMap`));

  for (const q of store.getQuads(mapNode, null, null, null)) {
    store.removeQuad(q);
  }

  if (ltNode) removeQuadsForSubject(store, ltNode);
  if (smNode) removeQuadsForSubject(store, smNode);

  for (const pomNode of pomNodes) {
    const omNodes = getObjects(store, pomNode, namedNode(`${RR}objectMap`));
    for (const om of omNodes) {
      removeObjectMapSubgraph(store, om);
    }
    removeQuadsForSubject(store, pomNode);
  }
}

function writeObjectMapDatatype(store, omNode, objectMap, prefixes) {
  const datatypePred = namedNode(`${RR}datatype`);
  for (const q of store.getQuads(omNode, datatypePred, null, null)) {
    store.removeQuad(q);
  }
  if (objectMap.datatype) {
    const dt = iriTerm(objectMap.datatype, prefixes);
    if (dt) store.addQuad(quad(omNode, datatypePred, dt));
  }
}

function writeObjectMap(store, omNode, objectMap, prefixes, mapId, idx) {
  const sanitized = sanitizeMapId(mapId);
  const omType = objectMap.type;

  if (omType === 'column') {
    setObjectMapLiteral(store, omNode, namedNode(`${RR}column`), objectMap.column);
    writeObjectMapDatatype(store, omNode, objectMap, prefixes);
  } else if (omType === 'template') {
    setObjectMapLiteral(store, omNode, namedNode(`${RR}template`), objectMap.template);
    writeObjectMapDatatype(store, omNode, objectMap, prefixes);
  } else if (omType === 'constant') {
    const val = objectMap.isIri
      ? iriTerm(objectMap.constant, prefixes)
      : literalTerm(objectMap.constant);
    if (val) store.addQuad(quad(omNode, namedNode(`${RR}constant`), val));
  } else if (omType === 'parentJoin') {
    const parentId = objectMap.parentTriplesMap;
    const parentIri = parentId.startsWith('#') ? namedNode(parentId) : namedNode(`#${parentId}`);
    store.addQuad(quad(omNode, namedNode(`${RR}parentTriplesMap`), parentIri));
    const jc = blankNode(`jc_${sanitized}_${idx}`);
    store.addQuad(quad(omNode, namedNode(`${RR}joinCondition`), jc));
    store.addQuad(
      quad(jc, namedNode(`${RR}child`), literalTerm(objectMap.joinCondition.child)),
    );
    store.addQuad(
      quad(jc, namedNode(`${RR}parent`), literalTerm(objectMap.joinCondition.parent)),
    );
  }
}

export function addTriplesMap(store, card, prefixes = {}) {
  const p = mergePrefixes(DEFAULT_PREFIXES, prefixes);
  const mapIri = mapIriTerm(card.id);
  const sanitized = sanitizeMapId(card.id);

  store.addQuad(quad(mapIri, namedNode(`${RDF}type`), namedNode(`${RR}TriplesMap`)));

  const lt = blankNode(`lt_${sanitized}`);
  store.addQuad(quad(mapIri, namedNode(`${RR}logicalTable`), lt));
  if (card.logicalTable.type === 'sqlQuery') {
    store.addQuad(quad(lt, namedNode(`${RR}SQLQuery`), literalTerm(card.logicalTable.value)));
  } else {
    store.addQuad(quad(lt, namedNode(`${RR}tableName`), literalTerm(card.logicalTable.value)));
  }

  const sm = blankNode(`sm_${sanitized}`);
  store.addQuad(quad(mapIri, namedNode(`${RR}subjectMap`), sm));
  if (card.subjectMap.template) {
    store.addQuad(quad(sm, namedNode(`${RR}template`), literalTerm(card.subjectMap.template)));
  }
  if (card.subjectMap.column) {
    store.addQuad(quad(sm, namedNode(`${RR}column`), literalTerm(card.subjectMap.column)));
  }
  if (card.subjectMap.class) {
    const cls = iriTerm(card.subjectMap.class, p);
    if (cls) store.addQuad(quad(sm, namedNode(`${RR}class`), cls));
  }

  card.predicateObjectMaps.forEach((pom, idx) => {
    if (pom.objectMap.type === 'stub') return;
    const pomNode = blankNode(`pom_${sanitized}_${idx}`);
    store.addQuad(quad(mapIri, namedNode(`${RR}predicateObjectMap`), pomNode));
    const pred = iriTerm(pom.predicate, p);
    if (pred) store.addQuad(quad(pomNode, namedNode(`${RR}predicate`), pred));

    const om = blankNode(`om_${sanitized}_${idx}`);
    store.addQuad(quad(pomNode, namedNode(`${RR}objectMap`), om));
    writeObjectMap(store, om, pom.objectMap, p, card.id, idx);
  });
}

export function removeTriplesMap(store, mapId) {
  removeTriplesMapSubgraph(store, mapId);
}

export function renameTriplesMap(store, oldId, newId) {
  const oldNorm = normalizeMapId(oldId);
  const newNorm = normalizeMapId(newId);
  if (oldNorm === newNorm) return;

  const oldNode = mapIriTerm(oldNorm);
  const newNode = mapIriTerm(newNorm);

  const mapQuads = store.getQuads(oldNode, null, null, null);
  if (mapQuads.length === 0) return;

  for (const q of mapQuads) {
    store.removeQuad(q);
    store.addQuad(quad(newNode, q.predicate, q.object, q.graph));
  }

  const parentPred = namedNode(`${RR}parentTriplesMap`);
  for (const q of store.getQuads(null, parentPred, oldNode, null)) {
    store.removeQuad(q);
    store.addQuad(quad(q.subject, q.predicate, newNode, q.graph));
  }
}

export function updateLogicalTable(store, mapId, logicalTable) {
  const mapNode = findTriplesMap(store, mapId);
  if (!mapNode) return;

  let ltNode = getLinkedBlank(store, mapNode, namedNode(`${RR}logicalTable`));
  if (!ltNode) {
    ltNode = blankNode(`lt_${sanitizeMapId(mapId)}`);
    store.addQuad(quad(mapNode, namedNode(`${RR}logicalTable`), ltNode));
  }

  const tableNamePred = namedNode(`${RR}tableName`);
  const sqlPred = namedNode(`${RR}SQLQuery`);
  for (const q of store.getQuads(ltNode, tableNamePred, null, null)) store.removeQuad(q);
  for (const q of store.getQuads(ltNode, sqlPred, null, null)) store.removeQuad(q);

  if (logicalTable.type === 'sqlQuery') {
    setObjectLiteral(store, ltNode, sqlPred, logicalTable.value);
  } else {
    setObjectLiteral(store, ltNode, tableNamePred, logicalTable.value);
  }
}

export function updateSubjectMap(store, mapId, subjectMap, prefixes = {}) {
  const mapNode = findTriplesMap(store, mapId);
  if (!mapNode) return;

  const p = mergePrefixes(DEFAULT_PREFIXES, prefixes);
  let smNode = getLinkedBlank(store, mapNode, namedNode(`${RR}subjectMap`));
  if (!smNode) {
    smNode = blankNode(`sm_${sanitizeMapId(mapId)}`);
    store.addQuad(quad(mapNode, namedNode(`${RR}subjectMap`), smNode));
  }

  setObjectLiteral(store, smNode, namedNode(`${RR}template`), subjectMap.template || '');
  setObjectLiteral(store, smNode, namedNode(`${RR}column`), subjectMap.column || '');
  const clsTerm = subjectMap.class ? iriTerm(subjectMap.class, p) : null;
  setObjectTerm(store, smNode, namedNode(`${RR}class`), clsTerm);
}

export function addPredicateObjectMap(store, mapId) {
  const mapNode = findTriplesMap(store, mapId);
  if (!mapNode) return;

  const existingPoms = getObjects(store, mapNode, namedNode(`${RR}predicateObjectMap`));
  const idx = existingPoms.length;
  const sanitized = sanitizeMapId(mapId);
  const pomNode = blankNode(`pom_${sanitized}_${idx}`);
  const omNode = blankNode(`om_${sanitized}_${idx}`);

  store.addQuad(quad(mapNode, namedNode(`${RR}predicateObjectMap`), pomNode));
  store.addQuad(quad(pomNode, namedNode(`${RR}objectMap`), omNode));
  setObjectMapLiteral(store, omNode, namedNode(`${RR}column`), '');
}

export function removePredicateObjectMap(store, mapId, pomIndex) {
  const mapNode = findTriplesMap(store, mapId);
  if (!mapNode) return;

  const pomNodes = getObjects(store, mapNode, namedNode(`${RR}predicateObjectMap`));
  const pomNode = pomNodes[pomIndex];
  if (!pomNode) return;

  for (const q of store.getQuads(mapNode, namedNode(`${RR}predicateObjectMap`), pomNode, null)) {
    store.removeQuad(q);
  }

  const omNodes = getObjects(store, pomNode, namedNode(`${RR}objectMap`));
  for (const om of omNodes) {
    removeObjectMapSubgraph(store, om);
  }
  removeQuadsForSubject(store, pomNode);
}

export function updatePredicateObjectMap(store, mapId, pomIndex, pom, prefixes = {}) {
  const mapNode = findTriplesMap(store, mapId);
  if (!mapNode) return;
  if (pom.objectMap.type === 'stub') return;

  const p = mergePrefixes(DEFAULT_PREFIXES, prefixes);
  const pomNodes = getObjects(store, mapNode, namedNode(`${RR}predicateObjectMap`));
  let pomNode = pomNodes[pomIndex];

  if (!pomNode) {
    const sanitized = sanitizeMapId(mapId);
    pomNode = blankNode(`pom_${sanitized}_${pomIndex}`);
    store.addQuad(quad(mapNode, namedNode(`${RR}predicateObjectMap`), pomNode));
  }

  const pred = iriTerm(pom.predicate, p);
  setObjectTerm(store, pomNode, namedNode(`${RR}predicate`), pred);

  const omPred = namedNode(`${RR}objectMap`);
  const oldOm = getLinkedBlank(store, pomNode, omPred);
  if (oldOm) {
    for (const q of store.getQuads(pomNode, omPred, oldOm, null)) {
      store.removeQuad(q);
    }
    removeObjectMapSubgraph(store, oldOm);
  }

  const omNode = blankNode(`om_${sanitizeMapId(mapId)}_${pomIndex}`);
  store.addQuad(quad(pomNode, omPred, omNode));
  writeObjectMap(store, omNode, pom.objectMap, p, mapId, pomIndex);
}

export function newEmptyCard(tableName = '') {
  const id = tableName ? tableToFragmentId(tableName) : '#NewMap';
  return {
    id,
    logicalTable: tableName
      ? { type: 'tableName', value: tableName }
      : { type: 'tableName', value: '' },
    subjectMap: { template: '', column: '', class: '' },
    predicateObjectMaps: [],
    hasStubs: false,
  };
}

// ── Ontology term index ──────────────────────────────────────────────────────

const CLASS_TYPES = new Set([
  `${OWL}Class`,
  `${RDFS}Class`,
]);

const PROPERTY_TYPES = new Set([
  `${RDF}Property`,
  `${OWL}DatatypeProperty`,
  `${OWL}ObjectProperty`,
]);

function extractOntologyTerms(turtle, prefixes = {}) {
  const classes = new Set();
  const properties = new Set();
  let store;
  try {
    store = parseTurtle(turtle, prefixes);
  } catch (err) {
    console.warn('extractOntologyTerms: failed to parse ontology', err);
    return { classes: [], properties: [] };
  }

  for (const q of storeToQuads(store)) {
    if (q.predicate.value === `${RDF}type`) {
      if (CLASS_TYPES.has(q.object.value)) classes.add(q.subject.value);
      if (PROPERTY_TYPES.has(q.object.value)) properties.add(q.subject.value);
    }
  }
  return {
    classes: [...classes].sort(),
    properties: [...properties].sort(),
  };
}

export function buildOntologyIndex(ontologies, globalPrefixes) {
  const classes = new Set();
  const properties = new Set();
  for (const ont of ontologies) {
    if (!ont.active) continue;
    const prefixes = mergePrefixes(globalPrefixes, extractPrefixes(ont.turtle));
    const terms = extractOntologyTerms(ont.turtle, prefixes);
    terms.classes.forEach((c) => classes.add(c));
    terms.properties.forEach((p) => properties.add(p));
  }
  return {
    classes: [...classes].sort(),
    properties: [...properties].sort(),
  };
}

export function mergeTurtleIntoStore(store, newTurtle, prefixes) {
  const incoming = storeToQuads(parseTurtle(newTurtle, prefixes));
  const seen = new Set(
    store.getQuads(null, null, null, null).map(
      (q) => `${q.subject.value}|${q.predicate.value}|${q.object.value}`,
    ),
  );
  for (const q of incoming) {
    const key = `${q.subject.value}|${q.predicate.value}|${q.object.value}`;
    if (!seen.has(key)) {
      store.addQuad(q);
      seen.add(key);
    }
  }
}

export function validateTurtle(turtle, prefixes) {
  parseTurtle(turtle, prefixes);
  return true;
}
