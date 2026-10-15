// Runs `python -m shio.cli check FILE` and turns its output into a result the extension can show.
// No `vscode` import here, so it can be tested with plain node.
const { execFile } = require("child_process");
const fs = require("fs");
const path = require("path");

// The compiler's error format (ShioError.format in shio/compiler.py): "PATH:LINE: error: MSG"
const ERROR = /^(.*?):(\d+): error: (.*)$/m;

// The Shio repo root above `file`, if the file lives in a checkout (so `python -m shio.cli` works without pip install).
function shioRoot(file) {
  let dir = path.dirname(path.resolve(file));
  for (;;) {
    if (fs.existsSync(path.join(dir, "shio", "compiler.py")) && fs.existsSync(path.join(dir, "pyproject.toml"))) {
      return dir;
    }
    const up = path.dirname(dir);
    if (up === dir) return null;
    dir = up;
  }
}

// -> {ok: true} | {line, message} (1-based line) | {setup: message} when Shio itself could not be run
function parse(code, stdout, stderr) {
  const out = `${stderr || ""}\n${stdout || ""}`;
  if (code === 0) return { ok: true };
  const m = ERROR.exec(out);
  if (m) return { line: Number(m[2]), message: m[3].trim() };
  if (/No module named '?shio|ENOENT|not found|is not recognized/i.test(out)) {
    return { setup: out.trim().split("\n").pop() };
  }
  const lines = out.trim().split("\n").filter(Boolean);
  return { line: 1, message: `shio check failed: ${lines.length ? lines[lines.length - 1] : `exit code ${code}`}` };
}

// -> {child, done: Promise<result>}
function check(python, file, { timeout = 20000 } = {}) {
  const root = shioRoot(file);
  const env = { ...process.env, PYTHONIOENCODING: "utf-8" };
  if (root) env.PYTHONPATH = root + (env.PYTHONPATH ? path.delimiter + env.PYTHONPATH : "");
  let child;
  const done = new Promise((resolve) => {
    child = execFile(python, ["-m", "shio.cli", "check", file], { cwd: path.dirname(file), env, timeout },
      (err, stdout, stderr) => {
        if (err && err.code === "ENOENT") return resolve({ setup: `'${python}' was not found` });
        if (err && err.killed) return resolve({ killed: true });
        resolve(parse(err ? (typeof err.code === "number" ? err.code : 1) : 0, stdout, stderr));
      });
  });
  return { child, done };
}

module.exports = { check, parse, shioRoot, ERROR };
