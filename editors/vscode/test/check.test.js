// Diagnostics tests: the output parser, real `shio check` runs, and the extension's save handler against a stub of the
// vscode API.   node test/check.test.js   (needs python3 with pydantic; Shio is found from this checkout)
const assert = require("assert");
const Module = require("module");
const path = require("path");
const { check, parse, shioRoot } = require("../lib/check");

const PY = process.env.SHIO_PYTHON || (process.platform === "win32" ? "python" : "python3");
const EXAMPLES = path.join(__dirname, "..", "..", "..", "examples");
const BOUNDARY = path.join(__dirname, "fixtures", "boundary.shio");

async function main() {
  // the parser
  assert.deepStrictEqual(parse(0, "x.shio: ok\n", ""), { ok: true });
  assert.deepStrictEqual(parse(1, "", "a/b.shio:22: error: 'total' is a model reading\n    22 |     if total.value:\n"),
    { line: 22, message: "'total' is a model reading" });
  assert.deepStrictEqual(parse(1, "", "C:\\x\\b.shio:3: error: invalid syntax: oops"), { line: 3, message: "invalid syntax: oops" });
  assert.ok(parse(1, "", "/usr/bin/python3: No module named shio").setup);
  assert.deepStrictEqual(parse(1, "", "Traceback...\nKeyError: 'x'"), { line: 1, message: "shio check failed: KeyError: 'x'" });
  assert.strictEqual(shioRoot(BOUNDARY), path.resolve(__dirname, "..", "..", ".."));

  // real runs
  for (const f of [path.join(EXAMPLES, "invoice.shio"), path.join(EXAMPLES, "injection", "inbox.shio"), path.join(EXAMPLES, "sql", "ask_db.shio")]) {
    const r = await check(PY, f).done;
    assert.deepStrictEqual(r, { ok: true }, `${f}: ${JSON.stringify(r)}`);
  }
  const bad = await check(PY, BOUNDARY).done;
  assert.strictEqual(bad.line, 22, JSON.stringify(bad));
  assert.match(bad.message, /^'total' is a model reading and cannot be used in plain code/);
  assert.ok((await check("/no/such/python", BOUNDARY).done).setup);
  console.log("check: parser and shio check runs ok");

  // the extension, with a stub vscode: a save of the boundary fixture puts one error on line 22
  const vscode = stubVscode();
  const load = Module._load;
  Module._load = function (req, ...rest) { return req === "vscode" ? vscode : load.call(this, req, ...rest); };
  const ext = require("../extension");
  ext.activate({ subscriptions: [] });
  const doc = vscode.openDoc(BOUNDARY);
  await vscode.fire("save", doc);
  const ds = vscode.collection.get(doc.uri.toString());
  assert.strictEqual(ds.length, 1);
  assert.strictEqual(ds[0].range.start.line, 21);                    // 0-based line 21 = .shio line 22
  assert.strictEqual(ds[0].range.start.character, 4);                // starts at `if`, not the indent
  assert.strictEqual(ds[0].severity, vscode.DiagnosticSeverity.Error);
  assert.strictEqual(ds[0].source, "shio");
  assert.match(ds[0].message, /'total' is a model reading/);
  console.log(`check: extension shows "${ds[0].message.slice(0, 60)}..." at line ${ds[0].range.start.line + 1}`);

  // a clean file clears it
  const ok = vscode.openDoc(path.join(EXAMPLES, "invoice.shio"));
  vscode.collection.set(ok.uri.toString(), ["stale"]);
  await vscode.fire("save", ok);
  assert.strictEqual(vscode.collection.get(ok.uri.toString()), undefined);
  console.log("check: all checks passed");
}

function stubVscode() {
  const fs = require("fs");
  const handlers = {};
  const collection = new Map();
  class Range { constructor(a, b, c, d) { this.start = { line: a, character: b }; this.end = { line: c, character: d }; } }
  class Diagnostic { constructor(range, message, severity) { Object.assign(this, { range, message, severity }); } }
  const on = (k) => (fn) => { handlers[k] = fn; return { dispose() {} }; };
  return {
    Range, Diagnostic, collection,
    DiagnosticSeverity: { Error: 0, Warning: 1 },
    languages: {
      createDiagnosticCollection: () => ({
        set: (uri, ds) => collection.set(uri.toString(), ds),
        delete: (uri) => collection.delete(uri.toString()),
        dispose() {},
      }),
    },
    window: {
      createOutputChannel: () => ({ appendLine: (s) => console.log(`  [output] ${s}`), dispose() {} }),
      showWarningMessage: async (m) => { console.log(`  [warning] ${m}`); },
    },
    workspace: {
      textDocuments: [],
      getConfiguration: (s) => ({ get: (k) => ({ "shio.python": PY, "shio.checkOnSave": true })[`${s}.${k}`] }),
      onDidOpenTextDocument: on("open"), onDidSaveTextDocument: on("save"), onDidCloseTextDocument: on("close"),
    },
    commands: { registerCommand: () => ({ dispose() {} }), executeCommand() {} },
    openDoc(file) {
      const lines = fs.readFileSync(file, "utf8").split("\n");
      return {
        languageId: "shio", lineCount: lines.length,
        uri: { scheme: "file", fsPath: file, toString: () => `file://${file}` },
        lineAt: (n) => ({
          firstNonWhitespaceCharacterIndex: lines[n].length - lines[n].trimStart().length,
          range: { end: { character: lines[n].length } },
        }),
      };
    },
    fire: (k, doc) => handlers[k](doc),
  };
}

main().catch((e) => { console.error(e); process.exit(1); });
