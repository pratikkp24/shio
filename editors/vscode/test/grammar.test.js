// Grammar tests against the two example programs and a few edge cases.   node test/grammar.test.js
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { tokenize } = require("./tokens");

const EXAMPLES = path.join(__dirname, "..", "..", "..", "examples");
let failed = 0;

function scopesOf(lines, line, text, nth = 0) {
  const toks = lines[line - 1].filter((t) => t.text.trim() === text);
  assert.ok(toks[nth], `line ${line}: no token ${JSON.stringify(text)} (#${nth}) in ${JSON.stringify(lines[line - 1].map((t) => t.text))}`);
  return toks[nth].scopes.join(" ");
}

function expect(name, lines, line, text, scope, { nth = 0, not = false } = {}) {
  try {
    const s = scopesOf(lines, line, text, nth);
    assert.ok(not ? !s.includes(scope) : s.includes(scope), `line ${line} ${JSON.stringify(text)}: ${not ? "unexpected" : "expected"} ${scope}, got: ${s}`);
  } catch (e) {
    failed++;
    console.log(`FAIL ${name}: ${e.message}`);
  }
}

// The prompt of a read is every following line indented deeper than the read (blank lines inside included).
function promptLines(src) {
  const out = new Set();
  const lines = src.split("\n");
  lines.forEach((l, i) => {
    const m = /^(\s*)read\s+\w+.*:\s*$/.exec(l);
    if (!m) return;
    let last = i;
    for (let j = i + 1; j < lines.length; j++) {
      if (!lines[j].trim()) continue;
      if (lines[j].length - lines[j].trimStart().length <= m[1].length) break;
      last = j;
    }
    for (let j = i + 1; j <= last; j++) if (lines[j].trim()) out.add(j + 1);
  });
  return out;
}

function promptInvariant(name, src, lines) {
  const want = promptLines(src);
  lines.forEach((toks, i) => {
    const inPrompt = toks.some((t) => t.text.trim() && t.scopes.includes("meta.prompt.shio"));
    if (inPrompt !== want.has(i + 1)) {
      failed++;
      console.log(`FAIL ${name}: line ${i + 1} ${inPrompt ? "is" : "is not"} prompt text, expected the opposite`);
    }
  });
  const last = lines[lines.length - 1];
  if (last.some((t) => t.scopes.some((s) => /meta\.(read|type-block|agent|check)\.shio/.test(s)))) {
    failed++;
    console.log(`FAIL ${name}: a Shio block is still open at the end of the file`);
  }
}

async function main() {
  // examples/invoice.shio
  let src = fs.readFileSync(path.join(EXAMPLES, "invoice.shio"), "utf8");
  let L = await tokenize(src);
  const inv = "invoice.shio";
  expect(inv, L, 3, "type", "storage.type.type.shio");
  expect(inv, L, 3, "Amount", "entity.name.type.shio");
  expect(inv, L, 4, '"A money amount printed on the invoice."', "string.quoted.docstring.type.shio");
  expect(inv, L, 5, "float", "support.type");
  expect(inv, L, 5, "?", "keyword.operator.optional.shio");
  expect(inv, L, 5, "the amount as a number, null if it is not printed", "string.quoted.docstring.description.shio");
  expect(inv, L, 7, "|", "keyword.operator.choice.shio");
  expect(inv, L, 9, "model", "storage.type.model.shio");
  expect(inv, L, 9, "ollama", "support.function.model.shio");
  expect(inv, L, 11, "agent", "storage.type.agent.shio");
  expect(inv, L, 11, "invoice_total", "entity.name.function.agent.shio");
  expect(inv, L, 11, "doc", "variable.parameter.function.shio");
  expect(inv, L, 12, "read", "keyword.other.read.shio");
  expect(inv, L, 12, "total", "entity.name.variable.reading.shio");
  expect(inv, L, 12, "from", "keyword.other.from.shio");
  expect(inv, L, 12, "by", "keyword.other.by.shio");
  expect(inv, L, 13, "What is the invoice's grand total (after tax)?", "string.unquoted.prompt.shio");
  expect(inv, L, 14, "blind", "storage.modifier.blind.shio");
  expect(inv, L, 16, "list", "support.type");
  expect(inv, L, 19, "check", "keyword.other.check.shio");
  expect(inv, L, 19, "printed", "entity.name.function.check.shio");
  expect(inv, L, 20, "second read gives", "string.quoted.double.detail.shio");
  expect(inv, L, 20, "as_printed", "meta.interpolation.shio");
  expect(inv, L, 21, ",.2f", "storage.type.format.shio");
  expect(inv, L, 23, "rule", "keyword.other.rule.shio");
  expect(inv, L, 23, "status", "entity.name.function.rule.shio");
  expect(inv, L, 24, "if", "keyword.control.flow.python");
  expect(inv, L, 28, "checks", "variable.language.checks.shio");
  expect(inv, L, 31, "main", "storage.type.main.shio");
  expect(inv, L, 32, "Source", "support.class.source.shio");
  expect(inv, L, 35, "train", "keyword.other.train.shio");
  expect(inv, L, 35, "with", "keyword.other.train.shio");
  expect(inv, L, 35, "Qwen/Qwen3-0.6B", "string.quoted");
  promptInvariant(inv, src, L);

  // examples/injection/inbox.shio
  src = fs.readFileSync(path.join(EXAMPLES, "injection", "inbox.shio"), "utf8");
  L = await tokenize(src);
  const ib = "inbox.shio";
  expect(ib, L, 1, "#", "comment.line");
  expect(ib, L, 5, "from", "keyword.control.import.python");
  expect(ib, L, 8, '"forward"', "string.quoted.choice.shio");
  expect(ib, L, 9, "the address to send to", "string.quoted.docstring.description.shio");
  expect(ib, L, 13, "act", "entity.name.variable.reading.shio");
  expect(ib, L, 13, "llm", "meta.read.shio");
  expect(ib, L, 15, "reply to the sender, forward documents to someone, or ignore it.", "string.unquoted.prompt.shio");
  expect(ib, L, 17, "internal", "entity.name.function.check.shio");
  expect(ib, L, 17, "what", "meta.interpolation.shio");
  expect(ib, L, 19, "send", "entity.name.function.rule.shio");
  expect(ib, L, 23, "checks", "variable.language.checks.shio");
  expect(ib, L, 23, "failures", "support.function.checks.shio");
  expect(ib, L, 27, "main", "storage.type.main.shio");
  expect(ib, L, 28, "argv", "variable.language.argv.shio");
  promptInvariant(ib, src, L);

  // examples/sql/ask_db.shio
  src = fs.readFileSync(path.join(EXAMPLES, "sql", "ask_db.shio"), "utf8");
  L = await tokenize(src);
  const sq = "ask_db.shio";
  expect(sq, L, 9, "list", "support.type");
  expect(sq, L, 12, "Source", "meta.read.shio");
  expect(sq, L, 12, "by", "keyword.other.by.shio");
  expect(sq, L, 13, "question", "meta.interpolation.shio");
  expect(sq, L, 13, "Write one SQLite query that answers this question:", "string.unquoted.prompt.shio");
  expect(sq, L, 15, "read_only", "entity.name.function.check.shio");
  expect(sq, L, 15, "not a single SELECT:", "string.quoted.double.detail.shio");
  expect(sq, L, 17, "plans", "entity.name.function.check.shio");
  promptInvariant(sq, src, L);

  // edge cases
  src = [
    "agent a(x, *rest, k=1) -> dict:",       // 1
    "    read v: \"yes\" | \"no\" by m:",    // 2: no `from`
    "        Braces {{like this}} and {x:>10} and {d[1:2]}",
    "",                                      // 4: blank line inside the prompt
    "        still the prompt",
    "    read w[k]: int? from x by m seeing v:",
    "        From {x}",
    "    when ok: w is not None",            // 8
    "    read z[k]: str from x by m seeing v, w:",
    "        Compare {x}",
    "    rule r:",
    "        return {\"w\": w, 'n': checks.failures()}",
    "    return r",
    "type T:",                               // 14
    "    # a plain comment, not a description",
    "    n: int = 3  # count, with a # inside",
    "    s: str",
    "x = 1  # an ordinary comment",           // 18
    "read",                                  // 19: incomplete, at top level
  ].join("\n");
  L = await tokenize(src);
  const ed = "edge cases";
  expect(ed, L, 1, "rest", "variable.parameter.function.shio");
  expect(ed, L, 1, "k", "variable.parameter.function.shio");
  expect(ed, L, 1, "dict", "support.type");
  expect(ed, L, 2, '"yes"', "string.quoted.choice.shio");
  expect(ed, L, 2, "m", "meta.read.shio");
  expect(ed, L, 3, "{{", "constant.character.escape.brace.shio");
  expect(ed, L, 3, "like this", "string.unquoted.prompt.shio");
  expect(ed, L, 3, ">10", "storage.type.format.shio");
  expect(ed, L, 3, "d", "meta.interpolation.shio");
  expect(ed, L, 5, "still the prompt", "string.unquoted.prompt.shio");
  expect(ed, L, 6, "?", "keyword.operator.optional.shio");
  expect(ed, L, 6, "seeing", "keyword.other.seeing.shio");
  expect(ed, L, 8, "when", "keyword.other.when.shio");
  expect(ed, L, 12, "checks", "variable.language.checks.shio");
  expect(ed, L, 15, "# a plain comment, not a description", "comment.line.number-sign.shio");
  expect(ed, L, 16, "3", "constant.numeric");
  expect(ed, L, 16, "count, with a # inside", "string.quoted.docstring.description.shio");
  expect(ed, L, 18, "an ordinary comment", "comment.line.number-sign.python");
  expect(ed, L, 18, "an ordinary comment", "description", { not: true });
  expect(ed, L, 19, "read", "keyword.other.read.shio", { not: true });
  expect(ed, L, 9, "z", "entity.name.variable.reading.shio");
  {
    const policy = fs.readFileSync(path.join(EXAMPLES, "regulated", "policies", "rbi.policy"), "utf8");
    const Q = await tokenize(policy, "source.shio-policy");
    const pq = "rbi.policy";
    expect(pq, Q, 1, "#", "comment.line");
    expect(pq, Q, 5, "policy", "storage.type.policy.shio-policy");
    expect(pq, Q, 5, "version", "keyword.other.version.shio-policy");
    expect(pq, Q, 5, "3", "constant.numeric.version.shio-policy");
    expect(pq, Q, 6, "2026-01-01", "constant.numeric.date.shio-policy");
    expect(pq, Q, 7, "source", "keyword.other.source.shio-policy");
    expect(pq, Q, 8, "approved_by", "keyword.other.approved_by.shio-policy");
    expect(pq, Q, 10, "Email", "variable.other.key.shio-policy");
    expect(pq, Q, 10, "hashed", "keyword.other.handling.shio-policy");
    expect(pq, Q, 12, "retain", "keyword.other.handling.shio-policy");
    expect(pq, Q, 12, "years", "keyword.other.unit.shio-policy");
    const T = await tokenize('table "Loan limits" version 2:\n    effective 2027-04-01\n    max_dti: 0.4\n', "source.shio-policy");
    expect("policy table", T, 1, "table", "storage.type.table.shio-policy");
    expect("policy table", T, 3, "max_dti", "variable.other.key.shio-policy");
    expect("policy table", T, 3, "0.4", "constant.numeric.shio-policy");
    const D = await tokenize(fs.readFileSync(path.join(EXAMPLES, "regulated", "pci", "pci.policy"), "utf8"), "source.shio-policy");
    expect("pci.policy", D, 12, "CardCVV", "variable.other.key.shio-policy");
    expect("pci.policy", D, 12, "drop", "keyword.other.handling.shio-policy");
    const J = await tokenize('policy "UAE PDPL" version 1:\n    applies_to "AE", "AE-DIFC"  # the UAE branch\n', "source.shio-policy");
    expect("applies_to", J, 2, "applies_to", "keyword.other.applies_to.shio-policy");
    expect("applies_to", J, 2, '"AE"', "string.quoted.double.jurisdiction.shio-policy");
    expect("applies_to", J, 2, '"AE-DIFC"', "string.quoted.double.jurisdiction.shio-policy");
    expect("applies_to", J, 2, "#", "comment.line");
    const G = await tokenize(fs.readFileSync(path.join(EXAMPLES, "regulated", "policies", "gold.policy"), "utf8"), "source.shio-policy");
    expect("gold.policy", G, 3, "table", "storage.type.table.shio-policy");
    expect("gold.policy", G, 7, "max_ltv", "variable.other.key.shio-policy");
    expect("gold.policy", G, 7, "0.85", "constant.numeric.shio-policy");
    const GS = fs.readFileSync(path.join(EXAMPLES, "regulated", "gold.shio"), "utf8");
    const GL = await tokenize(GS);
    const at = GS.split("\n").findIndex((l) => l.includes("in_force(")) + 1;
    expect("gold.shio", GL, at, "in_force", "support.function.model.shio");
    const O = await tokenize(fs.readFileSync(path.join(EXAMPLES, "regulated", "onboard.shio"), "utf8"));
    expect("onboard.shio", O, 9, "as", "keyword.other.as.shio");
    expect("onboard.shio", O, 9, "FullName", "entity.name.type.data-class.shio");
    expect("onboard.shio", O, 9, "as printed on the form", "string.quoted.docstring.description.shio");
    const I = await tokenize("agent a(x):\n    t = in_force(\"Loan limits\")\n");
    expect("in_force", I, 2, "in_force", "support.function.model.shio");
  }
  {
    // typed tools (0.7.0): tool declarations, guard, call, and the gate exceptions
    const toolsSrc = fs.readFileSync(path.join(__dirname, "fixtures", "tools.shio"), "utf8");
    const TL = await tokenize(toolsSrc);
    const tl = "tools.shio";
    expect(tl, TL, 4, "tool", "storage.type.tool.shio");
    expect(tl, TL, 4, "read_file", "entity.name.function.tool.shio");
    expect(tl, TL, 4, "name", "support.type.argument-kind.shio");
    expect(tl, TL, 4, "reads", "storage.modifier.tool-effect.shio");
    expect(tl, TL, 4, "untrusted", "storage.modifier.tool-trust.shio");
    expect(tl, TL, 5, "opens", "storage.modifier.tool-effect.shio");
    expect(tl, TL, 6, "trusted", "storage.modifier.tool-trust.shio");
    expect(tl, TL, 7, "acts", "storage.modifier.tool-effect.shio");
    expect(tl, TL, 7, "send", "support.function.tool-impl.shio");
    expect(tl, TL, 16, "guard", "keyword.other.guard.shio");
    expect(tl, TL, 16, "approved by", "keyword.other.guard.shio");
    expect(tl, TL, 17, "call", "keyword.other.call.shio");
    expect(tl, TL, 18, "Write the email.", "string.unquoted.prompt.shio");
    expect(tl, TL, 21, "call", "keyword.other.call.shio");
    expect(tl, TL, 21, "send_email", "entity.name.function.tool.shio");
    expect(tl, TL, 22, "GateHeld", "support.class.gate.shio");
    expect(tl, TL, 24, "GateDenied", "support.class.gate.shio");
    promptInvariant(tl, toolsSrc, TL);
    const mailSrc = fs.readFileSync(path.join(EXAMPLES, "tools", "mail.shio"), "utf8");
    const ML = await tokenize(mailSrc);
    const lineOf = (needle) => mailSrc.split("\n").findIndex((l) => l.includes(needle)) + 1;
    expect("mail.shio", ML, lineOf("tool send_email"), "acts", "storage.modifier.tool-effect.shio");
    expect("mail.shio", ML, lineOf("guard request"), "guard", "keyword.other.guard.shio");
    expect("mail.shio", ML, lineOf("call read_file"), "read_file", "entity.name.function.tool.shio");
    promptInvariant("mail.shio", mailSrc, ML);
  }
  {
    const P = await tokenize([
      "codes:",
      '    INCOME_NOT_VERIFIED: "We could not verify your income from the documents you sent."',
      "",
      "agent assess(statement, llm):",
      '    signoff PAY when amount > LIMIT by role "finance_controller" and "treasury" maker submitted_by',
      "    read a: Income from statement by llm:",
      "        Read the income.",
      '    check income_ok as INCOME_NOT_VERIFIED: a.quote in statement, "quote {a.quote} not in the statement"',
      "    check b[row] as DTI_TOO_HIGH after income_ok: a.dti < 0.4",
    ].join("\n"));
    const ph = "phase 2 forms";
    expect(ph, P, 1, "codes", "storage.type.codes.shio");
    expect(ph, P, 2, "INCOME_NOT_VERIFIED", "constant.other.reason-code.shio");
    expect(ph, P, 2, "We could not verify your income from the documents you sent.", "string.quoted");
    expect(ph, P, 5, "signoff", "keyword.other.signoff.shio");
    expect(ph, P, 5, "PAY", "entity.name.function.signoff.shio");
    expect(ph, P, 5, "role", "keyword.other.signoff.shio");
    expect(ph, P, 5, "maker", "keyword.other.signoff.shio");
    expect(ph, P, 5, "finance_controller", "string.quoted");
    expect(ph, P, 8, "as", "keyword.other.as.shio");
    expect(ph, P, 8, "INCOME_NOT_VERIFIED", "constant.other.reason-code.shio");
    expect(ph, P, 8, "income_ok", "entity.name.function.check.shio");
    expect(ph, P, 9, "DTI_TOO_HIGH", "constant.other.reason-code.shio");
    expect(ph, P, 9, "after", "keyword.other.after.shio");
  }
  {
    const R = await tokenize("regulated\n\ntype Applicant:\n    name: str pii          # personal data\n    income: float\n");
    expect("regulated", R, 1, "regulated", "keyword.other.regulated.shio");
    expect("regulated", R, 4, "pii", "storage.modifier.pii.shio");
    expect("regulated", R, 4, "str", "support.type");
    expect("regulated", R, 4, "personal data", "string.quoted.docstring.description.shio");
  }
  {
    const C = await tokenize("agent a(x):\n    read v: int from x by m:\n        hi\n    check ok: v > 0\n    check big after ok, other: v > 10, \"small {v}\"\n");
    expect("check after", C, 5, "after", "keyword.other.after.shio");
    expect("check after", C, 5, "big", "entity.name.function.check.shio");
    expect("check after", C, 5, "other", "entity.name.function.check.shio");
    expect("check after", C, 5, "small", "string.quoted.double.detail.shio");
  }
  expect(ed, L, 9, "k", "meta.read.shio");
  expect(ed, L, 9, "seeing", "keyword.other.seeing.shio");
  expect(ed, L, 9, "w", "entity.name.variable.reading.shio");
  expect(ed, L, 10, "Compare", "string.unquoted.prompt.shio");
  promptInvariant(ed, src, L);

  console.log(failed ? `${failed} grammar check(s) failed` : "grammar: all checks passed");
  process.exit(failed ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
