# Shio for VS Code

Language support for Shio, the agent language in which the model reads and code decides.

- **Highlighting** for `.shio` files: Python, plus `type`, `model`, `agent`, `read ... from ... by ... [blind] [seeing]`,
  `check`, `when`, `rule`, `train ... on ... with model` and `main:`. A read's indented prompt is shown as text, with
  its `{expr}` interpolations as code. A field's trailing comment in a `type` block is shown as a description, since
  the model sees it. A check's detail string gets the same `{expr}` highlighting. ` ```shio ` blocks in Markdown are
  highlighted too.
- **Errors on save.** Opening or saving a `.shio` file runs `shio check` on it and puts the error on the line it
  names, in the editor and the Problems panel. Using a reading in plain code, for example, shows up like this:

  ```
  'total' is a model reading and cannot be used in plain code. A reading can only be looked at in a check, a when,
  a rule, or a later read's 'seeing' clause; write a check or a rule that decides from it.        shio  [Ln 22]
  ```

- **Editing:** `#` comments, bracket matching and auto-closing, indent after a line ending in `:`, folding by
  indentation.
- **Commands:** `Shio: Check This File` and `Shio: Run This File` (runs `shio run` in a terminal).

## Install

Shio itself must be installed for errors to show (highlighting works without it):

```bash
cd path/to/shio
pip install -e .
```

Build and install the extension (Node 18 or later):

```bash
cd path/to/shio/editors/vscode
npm install
npx vsce package --allow-missing-repository
code --install-extension shio-0.1.0.vsix
```

`code` is VS Code's command-line launcher (in VS Code: *Shell Command: Install 'code' command in PATH*). Cursor and
other VS Code forks take the same `.vsix` with their own launcher, for example `cursor --install-extension
shio-0.1.0.vsix`. You can also install it from the Extensions view: `...` menu, *Install from VSIX...*.

## Settings

| Setting | Default | |
|---|---|---|
| `shio.python` | empty | The Python that has Shio installed. Empty means `python.defaultInterpreterPath`, then `python3` (`python` on Windows). |
| `shio.checkOnSave` | `true` | Run `shio check` when a `.shio` file is opened or saved. |

The check runs `PYTHON -m shio.cli check FILE` from the file's folder. When the file is inside a Shio checkout, the
checkout is put on `PYTHONPATH`, so the examples in this repo work without `pip install`. If Shio cannot be run, the
extension says so once and logs the details to the *Shio* output channel.

## How it works

- `syntaxes/shio.tmLanguage.json` is a TextMate grammar that handles the Shio forms itself and hands everything else
  to VS Code's built-in Python grammar (`source.python`). A read's prompt is everything indented under its header,
  the same rule the compiler uses.
- `extension.js` and `lib/check.js` run the compiler's checker and parse its `PATH:LINE: error: MESSAGE` output.
  Errors come from the compiler, so the editor never disagrees with `shio check`. The compiler stops at the first
  error, so one error shows at a time.

## Tests

```bash
npm install
npm test
```

`test/grammar.test.js` tokenizes `examples/invoice.shio`, `examples/injection/inbox.shio`, `examples/sql/ask_db.shio` and a set of edge
cases, and checks the scopes of the Shio forms, prompts, interpolations and descriptions. It needs VS Code's Python
grammar, which it finds in a VS Code or Cursor install (or set `MAGICPYTHON` to `MagicPython.tmLanguage.json`).
`test/check.test.js` runs `shio check` on the examples and on `test/fixtures/boundary.shio` (the invoice example with
`if total.value > 1000:` added), and drives the extension's save handler against a stub of the VS Code API.
`node test/tokens.js FILE [FROM] [TO]` prints every token's scopes, for working on the grammar.

## Not yet

A language server, so errors show as you type rather than on save; completion; go to definition. See
`SPEC.md` in the Shio repo, under *Next*.
