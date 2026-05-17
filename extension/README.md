# cothink VSCode extension

Skeleton for the cothink IDE-class harness — a sidebar in VSCode that talks to the cothink Python HTTP/SSE server (`cothink-serve`) on `127.0.0.1:8765`.

## What this skeleton ships (v0.5 Step 2)

- An activity-bar icon for **cothink**
- Three sidebar views: **Chat**, **Build**, **Memory** (placeholder UIs for now; full panels land in subsequent steps)
- A **Python server lifecycle manager** that auto-spawns `cothink-serve` on activation and stops it on deactivation, with a status-bar indicator
- Three commands in the command palette: `cothink: Start / Stop / Restart Server`
- Three configuration settings: `cothink.pythonPath`, `cothink.cothinkRoot`, `cothink.serverPort`

The panels intentionally render minimal placeholders that show server connection state. Full UIs (debate-mode chat, 5-node build pipeline, LEARNINGS.md browser) come in v0.5 Step 5+.

## Dev mode (recommended while iterating)

```bash
cd C:\Users\saladass\Documents\abetory\CLIs\cothink\extension
npm install
npm run compile
code .          # opens this extension folder in VSCode
```

Then **press F5** in that VSCode window. A second VSCode window opens with the extension loaded ("Extension Development Host"). Click the cothink icon in the activity bar.

After each TypeScript change: `npm run compile` (or `npm run watch` for auto-recompile) and reload the dev host (`Ctrl+R` inside it).

## Sideload install (use it like a real installed extension)

```bash
cd C:\Users\saladass\Documents\abetory\CLIs\cothink\extension
npm install -g @vscode/vsce
npm run compile
vsce package                       # produces cothink-0.5.0.vsix
code --install-extension cothink-0.5.0.vsix
```

Uninstall: `code --uninstall-extension cothink-local.cothink`

## Configuration (settings.json)

```json
{
  "cothink.cothinkRoot": "C:\\Users\\saladass\\Documents\\abetory\\CLIs\\cothink",
  "cothink.pythonPath": "",            // empty = auto-detect from cothinkRoot/.venv
  "cothink.serverPort": 8765
}
```

Auto-detect works when:
- `cothink.cothinkRoot` is set, OR
- The opened workspace folder contains `pyproject.toml`, OR
- The opened workspace folder contains `cothink/pyproject.toml`

## How the extension talks to the cothink Python server

The extension spawns `python -m cothink.server --host 127.0.0.1 --port 8765` as a child process at activation, polls `/health` until live (max 15s), then renders status. All actual LLM orchestration happens in the Python process via the existing cothink LangGraph pipeline — the extension is purely the UI surface.

## What's NOT in the skeleton

- The actual chat / build / memory panel UIs (HTML/CSS for the webviews) — coming next session
- SSE client wiring for `/build` and `/chat` streams
- `/memory` GET/POST integration
- Inline diff suggestions, marketplace publishing, etc. — v0.6+

See the cothink plan file at `~/.claude/plans/` for the full v0.5 scope.
