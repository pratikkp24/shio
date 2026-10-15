// Tokenize a .shio file with the extension's grammar (and VS Code's Python grammar) and print each token's scopes.
//   node test/tokens.js FILE [FROM_LINE] [TO_LINE]
// Needs `npm install` here (vscode-textmate, vscode-oniguruma) and VS Code's MagicPython grammar: found in a VS Code
// or Cursor install, or set MAGICPYTHON=/path/to/MagicPython.tmLanguage.json.
const fs = require("fs");
const path = require("path");
const vsctm = require("vscode-textmate");
const oniguruma = require("vscode-oniguruma");

const PY_CANDIDATES = [
  process.env.MAGICPYTHON,
  "/Applications/Visual Studio Code.app/Contents/Resources/app/extensions/python/syntaxes/MagicPython.tmLanguage.json",
  "/Applications/Cursor.app/Contents/Resources/app/extensions/python/syntaxes/MagicPython.tmLanguage.json",
  "/usr/share/code/resources/app/extensions/python/syntaxes/MagicPython.tmLanguage.json",
].filter(Boolean);

async function registry() {
  const wasm = fs.readFileSync(path.join(path.dirname(require.resolve("vscode-oniguruma")), "onig.wasm"));
  await oniguruma.loadWASM(wasm.buffer);
  const pyPath = PY_CANDIDATES.find((p) => fs.existsSync(p));
  if (!pyPath) throw new Error("MagicPython grammar not found; set MAGICPYTHON");
  const files = {
    "source.shio": path.join(__dirname, "..", "syntaxes", "shio.tmLanguage.json"),
    "source.python": pyPath,
  };
  return new vsctm.Registry({
    onigLib: Promise.resolve({
      createOnigScanner: (s) => new oniguruma.OnigScanner(s),
      createOnigString: (s) => new oniguruma.OnigString(s),
    }),
    loadGrammar: async (scope) => (files[scope] ? vsctm.parseRawGrammar(fs.readFileSync(files[scope], "utf8"), files[scope]) : null),
  });
}

async function tokenize(text) {
  const grammar = await (await registry()).loadGrammar("source.shio");
  let stack = vsctm.INITIAL;
  return text.split("\n").map((line) => {
    const r = grammar.tokenizeLine(line, stack);
    stack = r.ruleStack;
    return r.tokens.map((t) => ({ text: line.slice(t.startIndex, t.endIndex), scopes: t.scopes.slice(1) }));
  });
}

module.exports = { tokenize };

if (require.main === module) {
  const [file, from = 1, to = Infinity] = process.argv.slice(2);
  tokenize(fs.readFileSync(file, "utf8")).then((lines) => {
    lines.forEach((toks, i) => {
      if (i + 1 < from || i + 1 > to) return;
      console.log(`${i + 1}:`);
      for (const t of toks) if (t.text.trim()) console.log(`   ${JSON.stringify(t.text).padEnd(34)} ${t.scopes.join(" ")}`);
    });
  });
}
