import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { describe, it } from 'node:test';
import { fileURLToPath } from 'node:url';
import { Store } from 'n3';

import {
  addPredicateObjectMap,
  addTriplesMap,
  buildOntologyIndex,
  compactIri,
  extractPrefixes,
  mergePrefixes,
  mergeTurtleIntoStore,
  newEmptyCard,
  parseTriplesMaps,
  parseTurtle,
  removeTriplesMap,
  renameTriplesMap,
  serializeStore,
  tableToFragmentId,
  updateLogicalTable,
  updatePredicateObjectMap,
  updateSubjectMap,
  validateTurtle,
} from './model.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const MAPPING_TTL = readFileSync(
  join(__dirname, '../../../../mappings/mapping.ttl'),
  'utf8',
);

const PREFIXES = {
  ex: 'http://example.org/tpch/',
  rr: 'http://www.w3.org/ns/r2rml#',
};

describe('tableToFragmentId', () => {
  it('uses the whole fully qualified name so same-named tables do not collide', () => {
    assert.equal(tableToFragmentId('samples.tpch.customer'), '#samples.tpch.customer');
    assert.equal(tableToFragmentId('other.crm.customer'), '#other.crm.customer');
    assert.equal(tableToFragmentId('my_db.public.line_item'), '#my_db.public.line_item');
  });

  it('leaves the RFC 3986 unreserved set unescaped', () => {
    assert.equal(tableToFragmentId('cat.sch.my_table-v1~2'), '#cat.sch.my_table-v1~2');
  });

  // Expected values are the output of Python's quote(fqn, safe="") in
  // src/app/routes/autogenerate.py; the two paths must agree byte for byte.
  it('escapes the characters encodeURIComponent leaves alone', () => {
    assert.equal(tableToFragmentId("cat.sch.a!b'c(d)e*f"), '#cat.sch.a%21b%27c%28d%29e%2Af');
  });

  it('percent-encodes characters Turtle forbids in an IRI', () => {
    assert.equal(
      tableToFragmentId('cat.sch.a b#c<d>e"f{g}h|i^j`k\\l%m'),
      '#cat.sch.a%20b%23c%3Cd%3Ee%22f%7Bg%7Dh%7Ci%5Ej%60k%5Cl%25m',
    );
  });

  it('percent-encodes non-ASCII as UTF-8 with uppercase hex', () => {
    assert.equal(tableToFragmentId('cat.sch.naïve'), '#cat.sch.na%C3%AFve');
    assert.equal(tableToFragmentId('cat.sch.客户表'), '#cat.sch.%E5%AE%A2%E6%88%B7%E8%A1%A8');
    assert.equal(tableToFragmentId('cat.sch.🙂'), '#cat.sch.%F0%9F%99%82');
  });

  it('returns #Map for empty input', () => {
    assert.equal(tableToFragmentId(''), '#Map');
  });
});

describe('extractPrefixes', () => {
  it('extracts @prefix declarations', () => {
    const turtle = `@prefix ex: <http://example.org/> .
@prefix rr: <http://www.w3.org/ns/r2rml#> .
`;
    const prefixes = extractPrefixes(turtle);
    assert.equal(prefixes.ex, 'http://example.org/');
    assert.equal(prefixes.rr, 'http://www.w3.org/ns/r2rml#');
  });

  it('extracts SPARQL PREFIX lines', () => {
    const turtle = `PREFIX ex: <http://example.org/>
PREFIX rr: <http://www.w3.org/ns/r2rml#>
`;
    const prefixes = extractPrefixes(turtle);
    assert.equal(prefixes.ex, 'http://example.org/');
    assert.equal(prefixes.rr, 'http://www.w3.org/ns/r2rml#');
  });

  it('extracts @base', () => {
    const turtle = `@base <http://example.org/base/> .`;
    const prefixes = extractPrefixes(turtle);
    assert.equal(prefixes.base, 'http://example.org/base/');
  });
});

describe('mergePrefixes', () => {
  it('merges multiple prefix maps with later sources winning', () => {
    const base = { ex: 'http://example.org/' };
    const extra = { rr: 'http://www.w3.org/ns/r2rml#', ex: 'http://override/' };
    const merged = mergePrefixes(base, extra);
    assert.equal(merged.ex, 'http://override/');
    assert.equal(merged.rr, 'http://www.w3.org/ns/r2rml#');
  });

  it('skips null/undefined sources', () => {
    const merged = mergePrefixes({ ex: 'http://example.org/' }, null, undefined);
    assert.deepEqual(merged, { ex: 'http://example.org/' });
  });
});

describe('compactIri', () => {
  const prefixes = { ex: 'http://example.org/tpch/' };

  it('compacts known namespace to prefixed form', () => {
    assert.equal(compactIri('http://example.org/tpch/Customer', prefixes), 'ex:Customer');
  });

  it('wraps unknown IRIs with fragment in angle brackets', () => {
    assert.equal(compactIri('http://other.org/vocab#Thing', prefixes), '<#Thing>');
  });
});

describe('parseTurtle / serializeStore round-trip', () => {
  const minimalTurtle = `@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix ex: <http://example.org/> .

<#TestMap> a rr:TriplesMap ;
  rr:logicalTable [ rr:tableName "db.schema.table" ] ;
  rr:subjectMap [ rr:template "http://example.org/item/{id}" ] .
`;

  it('preserves triples map structure across parse and serialize', async () => {
    const store = parseTurtle(minimalTurtle);
    const mapsBefore = parseTriplesMaps(store);

    const serialized = await serializeStore(store);
    const store2 = parseTurtle(serialized);
    const mapsAfter = parseTriplesMaps(store2);

    assert.deepEqual(mapsAfter, mapsBefore);

    const serializedAgain = await serializeStore(store2);
    const store3 = parseTurtle(serializedAgain);
    assert.deepEqual(parseTriplesMaps(store3), mapsBefore);
  });

  it('keeps blank-node structure stable across repeated round-trips', async () => {
    let store = parseTurtle(minimalTurtle);
    for (let i = 0; i < 3; i += 1) {
      const turtle = await serializeStore(store);
      store = parseTurtle(turtle);
    }

    const maps = parseTriplesMaps(store);
    assert.equal(maps.length, 1);
    assert.equal(maps[0].id, '#TestMap');
    assert.equal(maps[0].logicalTable.value, 'db.schema.table');
    assert.equal(maps[0].subjectMap.template, 'http://example.org/item/{id}');
  });
});

describe('mapping.ttl fixture', () => {
  it('parses and yields triples maps with expected table names', () => {
    const store = parseTurtle(MAPPING_TTL, PREFIXES);
    const maps = parseTriplesMaps(store);

    assert.ok(maps.length >= 3);

    const customer = maps.find((m) => m.id === '#Customer');
    assert.ok(customer);
    assert.equal(customer.logicalTable.type, 'tableName');
    assert.equal(customer.logicalTable.value, 'samples.tpch.customer');

    const order = maps.find((m) => m.id === '#Order');
    assert.ok(order);
    assert.equal(order.logicalTable.value, 'samples.tpch.orders');

    const lineItem = maps.find((m) => m.id === '#LineItem');
    assert.ok(lineItem);
    assert.equal(lineItem.logicalTable.value, 'samples.tpch.lineitem');
  });

  it('parses parent join object maps from the production mapping', () => {
    const store = parseTurtle(MAPPING_TTL, PREFIXES);
    const order = parseTriplesMaps(store).find((m) => m.id === '#Order');
    assert.ok(order);

    const placedBy = order.predicateObjectMaps.find(
      (pom) => pom.predicate === 'http://example.org/tpch/placedBy',
    );
    assert.ok(placedBy);
    assert.equal(placedBy.objectMap.type, 'parentJoin');
    assert.equal(placedBy.objectMap.parentTriplesMap, '#Customer');
    assert.deepEqual(placedBy.objectMap.joinCondition, {
      child: 'o_custkey',
      parent: 'c_custkey',
    });
  });
});

describe('parent join serialization', () => {
  function parentJoinStore(joinCondition) {
    const store = new Store();
    addTriplesMap(store, newEmptyCard('samples.tpch.customer'), PREFIXES);
    addTriplesMap(store, newEmptyCard('samples.tpch.orders'), PREFIXES);
    addPredicateObjectMap(store, '#samples.tpch.orders');
    updatePredicateObjectMap(
      store,
      '#samples.tpch.orders',
      0,
      {
        predicate: 'ex:placedBy',
        objectMap: {
          type: 'parentJoin',
          parentTriplesMap: '#samples.tpch.customer',
          joinCondition,
        },
      },
      PREFIXES,
    );
    return store;
  }

  it('emits joinCondition triples only when both columns are set', async () => {
    const withJoin = parentJoinStore({ child: 'o_custkey', parent: 'c_custkey' });
    const turtle = await serializeStore(withJoin, PREFIXES);
    assert.match(turtle, /rr:joinCondition/);
    assert.match(turtle, /rr:child "o_custkey"/);
    assert.match(turtle, /rr:parent "c_custkey"/);

    const withoutJoin = parentJoinStore({ child: '', parent: '' });
    const emptyTurtle = await serializeStore(withoutJoin, PREFIXES);
    assert.doesNotMatch(emptyTurtle, /rr:joinCondition/);
    assert.doesNotMatch(emptyTurtle, /rr:child "/);
    assert.doesNotMatch(emptyTurtle, /rr:parent "/);
    assert.match(emptyTurtle, /rr:parentTriplesMap/);
  });

  it('omits joinCondition when only one column is set', async () => {
    const partialChild = parentJoinStore({ child: 'o_custkey', parent: '' });
    const partialParent = parentJoinStore({ child: '', parent: 'c_custkey' });
    for (const store of [partialChild, partialParent]) {
      const turtle = await serializeStore(store, PREFIXES);
      assert.doesNotMatch(turtle, /rr:joinCondition/);
      assert.doesNotMatch(turtle, /rr:child "/);
      assert.doesNotMatch(turtle, /rr:parent "/);
    }
  });

  it('round-trips parent join with both columns', () => {
    const store = parentJoinStore({ child: 'o_custkey', parent: 'c_custkey' });
    const maps = parseTriplesMaps(store);
    const order = maps.find((m) => m.id === '#samples.tpch.orders');
    assert.deepEqual(order.predicateObjectMaps[0].objectMap.joinCondition, {
      child: 'o_custkey',
      parent: 'c_custkey',
    });
  });

  it('round-trips a parent join through Turtle with dotted fragment ids', async () => {
    const store = parentJoinStore({ child: 'o_custkey', parent: 'c_custkey' });
    const turtle = await serializeStore(store, PREFIXES);
    const order = parseTriplesMaps(parseTurtle(turtle, PREFIXES)).find(
      (m) => m.id === '#samples.tpch.orders',
    );
    assert.ok(order);
    assert.equal(
      order.predicateObjectMaps[0].objectMap.parentTriplesMap,
      '#samples.tpch.customer',
    );
    assert.deepEqual(order.predicateObjectMaps[0].objectMap.joinCondition, {
      child: 'o_custkey',
      parent: 'c_custkey',
    });
  });
});

describe('fully qualified fragment ids', () => {
  it('round-trips a card whose fragment contains dots', async () => {
    const store = new Store();
    addTriplesMap(store, newEmptyCard('samples.tpch.customer'), PREFIXES);
    updateSubjectMap(
      store,
      '#samples.tpch.customer',
      { template: 'http://example.org/tpch/customer/{c_custkey}', column: '', class: 'ex:Customer' },
      PREFIXES,
    );

    const before = parseTriplesMaps(store);
    const turtle = await serializeStore(store, PREFIXES);
    assert.match(turtle, /<#samples\.tpch\.customer>/);

    const after = parseTriplesMaps(parseTurtle(turtle, PREFIXES));
    assert.deepEqual(after, before);
    assert.equal(after[0].id, '#samples.tpch.customer');
    assert.equal(after[0].logicalTable.value, 'samples.tpch.customer');
  });

  it('round-trips a card whose fragment contains percent escapes', async () => {
    const store = new Store();
    const table = 'cat.sch.odd name!';
    const card = newEmptyCard(table);
    assert.equal(card.id, '#cat.sch.odd%20name%21');
    addTriplesMap(store, card, PREFIXES);

    const turtle = await serializeStore(store, PREFIXES);
    const maps = parseTriplesMaps(parseTurtle(turtle, PREFIXES));
    assert.equal(maps.length, 1);
    assert.equal(maps[0].id, card.id);
    assert.equal(maps[0].logicalTable.value, table);
  });

  it('keeps logical tables separate for ids that differ only in dot placement', async () => {
    const store = new Store();
    addTriplesMap(store, newEmptyCard('main.sales.orders'), PREFIXES);
    addTriplesMap(store, newEmptyCard('main.sale.sorders'), PREFIXES);

    const maps = parseTriplesMaps(parseTurtle(await serializeStore(store, PREFIXES), PREFIXES));
    assert.equal(maps.length, 2);
    assert.deepEqual(
      maps.map((m) => m.logicalTable.value).sort(),
      ['main.sale.sorders', 'main.sales.orders'],
    );
  });
});

describe('legacy PascalCase documents', () => {
  const legacyTurtle = `@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix ex: <http://example.org/tpch/> .

<#Customer> a rr:TriplesMap ;
  rr:logicalTable [ rr:tableName "samples.tpch.customer" ] ;
  rr:subjectMap [ rr:template "http://example.org/tpch/customer/{c_custkey}" ; rr:class ex:Customer ] .
`;

  it('parses and edits a document with arbitrary fragment names', async () => {
    const store = parseTurtle(legacyTurtle, PREFIXES);
    assert.equal(parseTriplesMaps(store)[0].id, '#Customer');

    updateLogicalTable(store, '#Customer', { type: 'tableName', value: 'samples.tpch.customer2' });
    addPredicateObjectMap(store, '#Customer');
    updatePredicateObjectMap(
      store,
      '#Customer',
      0,
      { predicate: 'ex:name', objectMap: { type: 'column', column: 'c_name' } },
      PREFIXES,
    );

    const maps = parseTriplesMaps(parseTurtle(await serializeStore(store, PREFIXES), PREFIXES));
    assert.equal(maps.length, 1);
    assert.equal(maps[0].id, '#Customer');
    assert.equal(maps[0].logicalTable.value, 'samples.tpch.customer2');
    assert.equal(maps[0].subjectMap.class, 'http://example.org/tpch/Customer');
    assert.equal(maps[0].predicateObjectMaps[0].objectMap.column, 'c_name');
  });

  it('coexists with a newly created fully qualified card', async () => {
    const store = parseTurtle(legacyTurtle, PREFIXES);
    addTriplesMap(store, newEmptyCard('samples.tpch.orders'), PREFIXES);

    const maps = parseTriplesMaps(parseTurtle(await serializeStore(store, PREFIXES), PREFIXES));
    assert.deepEqual(maps.map((m) => m.id).sort(), ['#Customer', '#samples.tpch.orders']);
  });
});

describe('newEmptyCard', () => {
  it('builds a card from a table name', () => {
    const card = newEmptyCard('samples.tpch.orders');
    assert.equal(card.id, '#samples.tpch.orders');
    assert.deepEqual(card.logicalTable, {
      type: 'tableName',
      value: 'samples.tpch.orders',
    });
    assert.deepEqual(card.subjectMap, { template: '', column: '', class: '' });
    assert.deepEqual(card.predicateObjectMaps, []);
    assert.equal(card.hasStubs, false);
  });

  it('uses #NewMap when no table name is given', () => {
    const card = newEmptyCard();
    assert.equal(card.id, '#NewMap');
    assert.equal(card.logicalTable.value, '');
  });

  // mapper.js renames a card only while its id is still the #NewMap placeholder.
  it('renames the placeholder card once a table is picked', async () => {
    const store = new Store();
    const card = newEmptyCard();
    addTriplesMap(store, card, PREFIXES);
    assert.equal(card.id, '#NewMap');

    const table = 'samples.tpch.orders';
    updateLogicalTable(store, card.id, { type: 'tableName', value: table });
    renameTriplesMap(store, card.id, tableToFragmentId(table));

    const maps = parseTriplesMaps(parseTurtle(await serializeStore(store, PREFIXES), PREFIXES));
    assert.equal(maps.length, 1);
    assert.equal(maps[0].id, '#samples.tpch.orders');
    assert.equal(maps[0].logicalTable.value, table);
  });
});

describe('card CRUD', () => {
  it('adds, updates, and removes a triples map', async () => {
    const store = new Store();
    const card = newEmptyCard('samples.tpch.orders');
    addTriplesMap(store, card, PREFIXES);

    let maps = parseTriplesMaps(store);
    assert.equal(maps.length, 1);
    assert.equal(maps[0].id, '#samples.tpch.orders');
    assert.equal(maps[0].logicalTable.value, 'samples.tpch.orders');

    updateLogicalTable(store, '#samples.tpch.orders', {
      type: 'tableName',
      value: 'samples.tpch.order_renamed',
    });
    maps = parseTriplesMaps(store);
    assert.equal(maps[0].logicalTable.value, 'samples.tpch.order_renamed');

    updateSubjectMap(
      store,
      '#samples.tpch.orders',
      {
        template: 'http://example.org/tpch/order/{o_orderkey}',
        column: '',
        class: 'ex:Order',
      },
      PREFIXES,
    );
    maps = parseTriplesMaps(store);
    assert.equal(maps[0].subjectMap.template, 'http://example.org/tpch/order/{o_orderkey}');
    assert.equal(maps[0].subjectMap.class, 'http://example.org/tpch/Order');

    addPredicateObjectMap(store, '#samples.tpch.orders');
    maps = parseTriplesMaps(store);
    assert.equal(maps[0].predicateObjectMaps.length, 1);
    assert.equal(maps[0].predicateObjectMaps[0].objectMap.type, 'column');
    assert.equal(maps[0].predicateObjectMaps[0].objectMap.column, '');

    updatePredicateObjectMap(
      store,
      '#samples.tpch.orders',
      0,
      {
        predicate: 'ex:orderKey',
        objectMap: { type: 'column', column: 'o_orderkey', datatype: 'xsd:integer' },
      },
      PREFIXES,
    );
    maps = parseTriplesMaps(store);
    assert.equal(maps[0].predicateObjectMaps[0].predicate, 'http://example.org/tpch/orderKey');
    assert.equal(maps[0].predicateObjectMaps[0].objectMap.column, 'o_orderkey');
    assert.equal(maps[0].predicateObjectMaps[0].objectMap.datatype, 'http://www.w3.org/2001/XMLSchema#integer');

    renameTriplesMap(store, '#samples.tpch.orders', '#Order');
    maps = parseTriplesMaps(store);
    assert.ok(!maps.some((m) => m.id === '#samples.tpch.orders'));
    assert.ok(maps.some((m) => m.id === '#Order'));

    removeTriplesMap(store, '#Order');
    maps = parseTriplesMaps(store);
    assert.equal(maps.length, 0);
  });
});

describe('validateTurtle', () => {
  it('returns true for valid Turtle', () => {
    assert.equal(
      validateTurtle('@prefix ex: <http://example.org/> . ex:a ex:b ex:c .', {}),
      true,
    );
  });

  it('throws for malformed Turtle', () => {
    assert.throws(() => validateTurtle('not valid turtle {{{', {}));
  });
});

describe('mergeTurtleIntoStore', () => {
  it('adds non-overlapping triples without duplicating existing ones', () => {
    const store = parseTurtle('@prefix ex: <http://example.org/> . ex:a ex:p1 ex:o1 .');
    const countBefore = store.getQuads(null, null, null, null).length;

    mergeTurtleIntoStore(store, '@prefix ex: <http://example.org/> . ex:a ex:p2 ex:o2 .', {});
    const countAfterMerge = store.getQuads(null, null, null, null).length;
    assert.equal(countAfterMerge, countBefore + 1);

    mergeTurtleIntoStore(store, '@prefix ex: <http://example.org/> . ex:a ex:p1 ex:o1 .', {});
    const countAfterDuplicate = store.getQuads(null, null, null, null).length;
    assert.equal(countAfterDuplicate, countAfterMerge);
  });
});

describe('buildOntologyIndex', () => {
  const ontologyTurtle = `@prefix ex: <http://example.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

ex:Person a owl:Class .
ex:name a owl:DatatypeProperty .
ex:worksFor a owl:ObjectProperty .
`;

  it('extracts class and property IRIs from active ontologies', () => {
    const index = buildOntologyIndex(
      [{ turtle: ontologyTurtle, active: true }],
      { ex: 'http://example.org/' },
    );

    assert.deepEqual(index.classes, ['http://example.org/Person']);
    assert.deepEqual(index.properties, [
      'http://example.org/name',
      'http://example.org/worksFor',
    ]);
  });

  it('skips inactive ontologies', () => {
    const index = buildOntologyIndex(
      [{ turtle: ontologyTurtle, active: false }],
      { ex: 'http://example.org/' },
    );
    assert.deepEqual(index.classes, []);
    assert.deepEqual(index.properties, []);
  });
});
