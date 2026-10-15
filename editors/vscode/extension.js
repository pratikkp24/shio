// Shio for VS Code: `shio check` on open and save, its error shown at the .shio line it names.
const vscode = require("vscode");
const { check } = require("./lib/check");

let diagnostics;
let output;
const running = new Map();   // document uri -> child process of the check in flight
let warnedSetup = false;

function python() {
  const shio = vscode.workspace.getConfiguration("shio").get("python");
  if (shio) return shio;
  const py = vscode.workspace.getConfiguration("python").get("defaultInterpreterPath");
  if (py && py !== "python") return py;
  return process.platform === "win32" ? "python" : "python3";
}

async function checkDocument(doc, { force = false } = {}) {
  if (doc.languageId !== "shio" || doc.uri.scheme !== "file") return;
  if (!force && !vscode.workspace.getConfiguration("shio").get("checkOnSave")) return;
  const key = doc.uri.toString();
  const prev = running.get(key);
  if (prev) prev.kill();
  const py = python();
  const { child, done } = check(py, doc.uri.fsPath);
  running.set(key, child);
  const r = await done;
  if (running.get(key) !== child) return;      // a newer check replaced this one
  running.delete(key);
  if (r.killed) return;
  if (r.setup) {
    diagnostics.delete(doc.uri);
    output.appendLine(`shio check ${doc.uri.fsPath}: could not run Shio with '${py}': ${r.setup}`);
    if (!warnedSetup) {
      warnedSetup = true;
      const pick = await vscode.window.showWarningMessage(
        `Shio: could not run 'shio check' with '${py}' (${r.setup}). Set shio.python to an interpreter with Shio installed.`,
        "Open Settings");
      if (pick) vscode.commands.executeCommand("workbench.action.openSettings", "shio.python");
    }
    return;
  }
  if (r.ok) {
    diagnostics.delete(doc.uri);
    return;
  }
  const n = Math.min(Math.max(r.line - 1, 0), Math.max(doc.lineCount - 1, 0));
  const text = doc.lineAt(n);
  const range = new vscode.Range(n, text.firstNonWhitespaceCharacterIndex, n, Math.max(text.range.end.character,
    text.firstNonWhitespaceCharacterIndex + 1));
  const d = new vscode.Diagnostic(range, r.message, vscode.DiagnosticSeverity.Error);
  d.source = "shio";
  diagnostics.set(doc.uri, [d]);
}

function activate(context) {
  diagnostics = vscode.languages.createDiagnosticCollection("shio");
  output = vscode.window.createOutputChannel("Shio");
  context.subscriptions.push(
    diagnostics,
    output,
    vscode.workspace.onDidOpenTextDocument((d) => checkDocument(d)),
    vscode.workspace.onDidSaveTextDocument((d) => checkDocument(d)),
    vscode.workspace.onDidCloseTextDocument((d) => diagnostics.delete(d.uri)),
    vscode.commands.registerCommand("shio.check", () => {
      const ed = vscode.window.activeTextEditor;
      if (ed) return ed.document.save().then(() => checkDocument(ed.document, { force: true }));
    }),
    vscode.commands.registerCommand("shio.run", async () => {
      const ed = vscode.window.activeTextEditor;
      if (!ed || ed.document.languageId !== "shio") return;
      await ed.document.save();
      const t = vscode.window.createTerminal({ name: "Shio", cwd: require("path").dirname(ed.document.uri.fsPath) });
      const q = (s) => (process.platform === "win32" ? `"${s}"` : `'${s.replace(/'/g, "'\\''")}'`);
      t.show();
      t.sendText(`${q(python())} -m shio.cli run ${q(ed.document.uri.fsPath)}`);
    }),
  );
  vscode.workspace.textDocuments.forEach((d) => checkDocument(d));
}

function deactivate() {
  for (const c of running.values()) c.kill();
}

module.exports = { activate, deactivate, checkDocument };
