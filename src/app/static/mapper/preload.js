// Browser↔Node bridge: model.js expects window.N3 from a script tag in index.html.
import { DataFactory, Parser, Store, Writer } from 'n3';

const n3 = { Parser, Writer, Store, DataFactory };
globalThis.N3 = n3;
globalThis.window = globalThis;
globalThis.window.N3 = n3;
