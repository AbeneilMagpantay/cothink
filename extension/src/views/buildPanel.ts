import * as path from "path";
import * as vscode from "vscode";

import { streamSSE } from "../sseClient";

/**
 * Build panel — wraps the cothink `/build` SSE endpoint, which runs the full
 * 5-node integrity pipeline (Discovery → Planning → Executing → Mechanical
 * Gate → Contract Review) end-to-end.
 *
 * Panel layout:
 *   - Task input + Run / Stop buttons (with status badge)
 *   - 5-node progress list, updating live as `node_complete` events arrive
 *   - Files-written list (post-done), clickable to open in editor
 *   - Collapsible debate log showing Claude/Gemini exchanges per node
 *   - Final summary: halt reason (if any) + git reset --hard <sha> rollback hint
 *
 * Webview ↔ Extension protocol:
 *   webview → extension:
 *     { type: "ready" }
 *     { type: "run", task }
 *     { type: "stop" }
 *     { type: "open_file", path } — open a written file in the editor
 *   extension → webview:
 *     { type: "status", health }
 *     { type: "build_started", project_dir }
 *     { type: "node_complete", node_name, log, dialogue_delta, halt_reason }
 *     { type: "build_done", halt_reason, pre_execute_commit_hash, proposed_diffs, design_contract }
 *     { type: "error", reason }
 */
export class BuildPanelProvider implements vscode.WebviewViewProvider {
  private view: vscode.WebviewView | undefined;
  private activeAbort: AbortController | undefined;
  private statusInterval: NodeJS.Timeout | undefined;

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly getHealth: () => string | undefined,
    private readonly getServerPort: () => number,
  ) {}

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    view.webview.options = { enableScripts: true };
    view.webview.html = this.html();
    view.webview.onDidReceiveMessage((msg) => this.handleMessage(msg));

    this.statusInterval = setInterval(() => {
      this.post({ type: "status", health: this.getHealth() ?? "unknown" });
    }, 1000);

    view.onDidDispose(() => {
      if (this.statusInterval) clearInterval(this.statusInterval);
      this.activeAbort?.abort();
      this.view = undefined;
    });
  }

  private async handleMessage(msg: { type: string; [k: string]: unknown }): Promise<void> {
    switch (msg.type) {
      case "ready":
        this.post({ type: "status", health: this.getHealth() ?? "unknown" });
        return;
      case "run":
        await this.runBuild(String(msg.task ?? ""));
        return;
      case "stop":
        this.activeAbort?.abort();
        return;
      case "open_file":
        await this.openFile(String(msg.path ?? ""));
        return;
    }
  }

  private async runBuild(task: string): Promise<void> {
    const trimmed = task.trim();
    if (!trimmed) return;

    const projectDir = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    if (!projectDir) {
      this.post({
        type: "error",
        reason: "No workspace folder open. Open a project folder before running a build.",
      });
      return;
    }

    this.post({ type: "build_started", project_dir: projectDir });
    this.activeAbort = new AbortController();

    try {
      await streamSSE(
        `http://127.0.0.1:${this.getServerPort()}/build`,
        {
          method: "POST",
          body: { task: trimmed, project_dir: projectDir },
          signal: this.activeAbort.signal,
        },
        (ev) => {
          const data = (ev.data ?? {}) as Record<string, unknown>;
          switch (ev.event) {
            case "node_complete":
              this.post({
                type: "node_complete",
                node_name: data.node_name,
                log: data.log,
                dialogue_delta: data.dialogue_delta,
                halt_reason: data.halt_reason,
              });
              break;
            case "done":
              this.post({
                type: "build_done",
                halt_reason: data.halt_reason,
                pre_execute_commit_hash: data.pre_execute_commit_hash,
                proposed_diffs: data.proposed_diffs,
                design_contract: data.design_contract,
              });
              break;
            case "error":
              this.post({
                type: "error",
                reason: typeof data.reason === "string" ? data.reason : "stream error",
              });
              break;
          }
        },
      );
    } catch (e: unknown) {
      const reason =
        e && typeof e === "object" && "message" in e ? String((e as Error).message) : String(e);
      if (this.activeAbort?.signal.aborted) {
        this.post({ type: "error", reason: "stopped" });
      } else {
        this.post({ type: "error", reason });
      }
    } finally {
      this.activeAbort = undefined;
    }
  }

  private async openFile(filePathRaw: string): Promise<void> {
    if (!filePathRaw) return;
    const projectDir = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    if (!projectDir) return;
    const abs = path.isAbsolute(filePathRaw) ? filePathRaw : path.join(projectDir, filePathRaw);
    try {
      const doc = await vscode.workspace.openTextDocument(abs);
      await vscode.window.showTextDocument(doc, { preview: false });
    } catch (e: unknown) {
      const reason =
        e && typeof e === "object" && "message" in e ? String((e as Error).message) : String(e);
      this.post({ type: "error", reason: `open_file failed: ${reason}` });
    }
  }

  private post(msg: Record<string, unknown>): void {
    this.view?.webview.postMessage(msg);
  }

  private html(): string {
    const nonce = genNonce();
    return /* html */ `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';" />
  <style>
    :root {
      --claude: var(--vscode-charts-blue, #4ec9b0);
      --gemini: var(--vscode-charts-purple, #c586c0);
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; margin: 0; padding: 0; }
    body {
      font-family: var(--vscode-font-family);
      font-size: var(--vscode-font-size, 13px);
      color: var(--vscode-foreground);
      background: var(--vscode-sideBar-background);
      display: flex;
      flex-direction: column;
    }

    #status-bar {
      padding: 4px 10px;
      font-size: 11px;
      color: var(--vscode-descriptionForeground);
      border-bottom: 1px solid var(--vscode-panel-border);
      flex-shrink: 0;
    }
    .badge { font-weight: 600; padding: 1px 6px; border-radius: 3px; }
    .badge.ok { color: var(--vscode-charts-green); }
    .badge.err { color: var(--vscode-errorForeground); }
    .badge.warn { color: var(--vscode-charts-yellow); }

    #composer {
      flex-shrink: 0;
      padding: 8px;
      border-bottom: 1px solid var(--vscode-panel-border);
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    #task {
      width: 100%;
      resize: vertical;
      min-height: 64px;
      max-height: 240px;
      padding: 6px 8px;
      background: var(--vscode-input-background);
      color: var(--vscode-input-foreground);
      border: 1px solid var(--vscode-input-border, transparent);
      font-family: inherit;
      font-size: 13px;
      line-height: 1.4;
      outline: none;
    }
    #task:focus { border-color: var(--vscode-focusBorder); }
    #composer .controls { display: flex; gap: 6px; }
    button {
      padding: 6px 12px;
      background: var(--vscode-button-background);
      color: var(--vscode-button-foreground);
      border: 1px solid transparent;
      cursor: pointer;
      font-family: inherit;
      font-size: 12px;
    }
    button:hover { background: var(--vscode-button-hoverBackground); }
    button:disabled { opacity: 0.5; cursor: not-allowed; }
    button.secondary {
      background: var(--vscode-button-secondaryBackground, transparent);
      color: var(--vscode-button-secondaryForeground, var(--vscode-foreground));
      border-color: var(--vscode-input-border, transparent);
    }
    button.secondary:hover { background: var(--vscode-button-secondaryHoverBackground, var(--vscode-list-hoverBackground)); }
    .small { font-size: 11px; color: var(--vscode-descriptionForeground); }

    #content {
      flex: 1;
      overflow-y: auto;
      padding: 10px;
    }
    #content:empty::before {
      content: "Enter a task and click Run. The 5-node integrity pipeline (Discovery → Planning → Executing → Mechanical Gate → Contract Review) runs end-to-end. Live progress streams in.";
      color: var(--vscode-descriptionForeground);
      font-style: italic;
      display: block;
      text-align: center;
      max-width: 320px;
      margin: 40px auto;
    }

    .section { margin-bottom: 14px; }
    .section h4 {
      margin: 0 0 6px 0;
      font-size: 11px;
      letter-spacing: 0.4px;
      text-transform: uppercase;
      color: var(--vscode-descriptionForeground);
      font-weight: 700;
    }
    .nodes { display: flex; flex-direction: column; gap: 4px; }
    .node {
      display: flex;
      align-items: flex-start;
      gap: 8px;
      padding: 6px 8px;
      border-left: 2px solid var(--vscode-input-border, #444);
      background: var(--vscode-editor-inactiveSelectionBackground);
      border-radius: 2px;
    }
    .node .marker { width: 14px; text-align: center; flex-shrink: 0; }
    .node .body { flex: 1; min-width: 0; }
    .node .name { font-weight: 600; font-size: 12px; }
    .node .log { font-size: 11px; color: var(--vscode-descriptionForeground); word-break: break-word; }
    .node.pending .marker { color: var(--vscode-descriptionForeground); }
    .node.pending .name { opacity: 0.7; }
    .node.running { border-left-color: var(--vscode-charts-yellow); }
    .node.running .marker { animation: spin 1s linear infinite; display: inline-block; }
    .node.done { border-left-color: var(--vscode-charts-green); }
    .node.done .marker { color: var(--vscode-charts-green); }
    .node.fail { border-left-color: var(--vscode-errorForeground); }
    .node.fail .marker { color: var(--vscode-errorForeground); }
    @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }

    .files { display: flex; flex-direction: column; gap: 3px; }
    .file-row {
      padding: 5px 8px;
      background: var(--vscode-editor-inactiveSelectionBackground);
      border-radius: 2px;
      cursor: pointer;
      font-family: var(--vscode-editor-font-family);
      font-size: 12px;
      word-break: break-all;
    }
    .file-row:hover { background: var(--vscode-list-hoverBackground); }
    .file-row .bullet {
      font-size: 10px;
      color: var(--vscode-descriptionForeground);
      margin-top: 2px;
    }

    details.dialogue { margin-top: 8px; }
    details.dialogue > summary {
      cursor: pointer;
      padding: 4px 0;
      color: var(--vscode-descriptionForeground);
      font-size: 11px;
      letter-spacing: 0.3px;
      text-transform: uppercase;
      user-select: none;
    }
    details.dialogue[open] > summary { margin-bottom: 6px; }
    .utterance {
      padding: 6px 8px;
      margin-bottom: 6px;
      border-radius: 2px;
      word-break: break-word;
    }
    .utterance .who { font-size: 10px; font-weight: 700; letter-spacing: 0.3px; text-transform: uppercase; margin-bottom: 3px; }
    .utterance .text { font-size: 12px; white-space: pre-wrap; line-height: 1.4; }
    .utterance.claude { border-left: 3px solid var(--claude); padding-left: 8px; }
    .utterance.claude .who { color: var(--claude); }
    .utterance.gemini { border-left: 3px solid var(--gemini); padding-left: 8px; }
    .utterance.gemini .who { color: var(--gemini); }

    .summary {
      padding: 10px;
      background: var(--vscode-editor-inactiveSelectionBackground);
      border-radius: 3px;
      margin-bottom: 10px;
    }
    .summary.halted { border-left: 3px solid var(--vscode-errorForeground); }
    .summary.done { border-left: 3px solid var(--vscode-charts-green); }
    .summary .title { font-weight: 700; margin-bottom: 4px; }
    .summary .rollback {
      font-family: var(--vscode-editor-font-family);
      font-size: 11px;
      background: var(--vscode-textCodeBlock-background, rgba(0,0,0,0.2));
      padding: 4px 6px;
      margin-top: 6px;
      border-radius: 2px;
      word-break: break-all;
      user-select: all;
    }

    .error { color: var(--vscode-errorForeground); padding: 8px; font-size: 12px; }
  </style>
</head>
<body>
  <div id="status-bar">Server: <span id="status" class="badge warn">connecting…</span></div>

  <div id="composer">
    <textarea id="task" placeholder="Describe what to build. Example: Write a Python file utils/retry.py with a retry_with_backoff decorator that takes max_attempts and base_delay, raises on final failure, jitters between retries."></textarea>
    <div class="controls">
      <button id="run">Run build</button>
      <button id="stop" class="secondary" disabled>Stop</button>
      <span class="small" id="proj-hint">workspace: (detecting)</span>
    </div>
  </div>

  <div id="content"></div>

  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    const statusEl = document.getElementById("status");
    const taskEl = document.getElementById("task");
    const runBtn = document.getElementById("run");
    const stopBtn = document.getElementById("stop");
    const content = document.getElementById("content");
    const projHint = document.getElementById("proj-hint");

    const NODE_ORDER = ["discovery", "planning", "executing", "mechanical", "contract_review", "human_fallback"];
    const NODE_LABEL = {
      discovery: "Discovery",
      planning: "Planning",
      executing: "Executing",
      mechanical: "Mechanical Gate",
      contract_review: "Contract Review",
      human_fallback: "Human Fallback",
    };

    let nodeRows = {};       // node_name → { row, log, marker, state }
    let dialogueBox = null;  // <div> inside <details> for utterances
    let started = false;

    function resetUI() {
      content.innerHTML = "";
      nodeRows = {};
      dialogueBox = null;
      started = false;
    }

    function startBuildUI() {
      resetUI();
      const sec = document.createElement("div");
      sec.className = "section";
      const h = document.createElement("h4");
      h.textContent = "Pipeline progress";
      sec.appendChild(h);
      const nodes = document.createElement("div");
      nodes.className = "nodes";
      for (const name of NODE_ORDER) {
        if (name === "human_fallback") continue; // shown only if it actually fires
        nodeRows[name] = createNodeRow(name, "pending");
        nodes.appendChild(nodeRows[name].row);
      }
      sec.appendChild(nodes);
      content.appendChild(sec);

      const det = document.createElement("details");
      det.className = "dialogue";
      const sum = document.createElement("summary");
      sum.textContent = "Debate log (Claude / Gemini exchanges)";
      det.appendChild(sum);
      dialogueBox = document.createElement("div");
      det.appendChild(dialogueBox);
      content.appendChild(det);

      started = true;
    }

    function createNodeRow(name, state) {
      const row = document.createElement("div");
      row.className = "node " + state;
      const marker = document.createElement("div");
      marker.className = "marker";
      marker.textContent = markerFor(state);
      const body = document.createElement("div");
      body.className = "body";
      const nameEl = document.createElement("div");
      nameEl.className = "name";
      nameEl.textContent = NODE_LABEL[name] || name;
      const logEl = document.createElement("div");
      logEl.className = "log";
      logEl.textContent = state === "pending" ? "waiting…" : "";
      body.appendChild(nameEl);
      body.appendChild(logEl);
      row.appendChild(marker);
      row.appendChild(body);
      return { row, log: logEl, marker, state };
    }

    function markerFor(state) {
      switch (state) {
        case "pending": return "○";
        case "running": return "◌";
        case "done": return "✓";
        case "fail": return "✗";
        default: return "•";
      }
    }

    function setNodeState(name, state, logText) {
      let row = nodeRows[name];
      if (!row) {
        // human_fallback fires on demand only — inject a row when it arrives
        if (name === "human_fallback") {
          row = createNodeRow("human_fallback", state);
          nodeRows[name] = row;
          content.querySelector(".nodes")?.appendChild(row.row);
        } else {
          return;
        }
      }
      row.row.className = "node " + state;
      row.marker.textContent = markerFor(state);
      if (logText !== undefined) row.log.textContent = logText;
      row.state = state;
    }

    function appendDialogue(entries) {
      if (!dialogueBox || !Array.isArray(entries)) return;
      for (const e of entries) {
        if (!e || typeof e !== "object") continue;
        const speaker = String(e.speaker || "team").toLowerCase();
        const role = String(e.role || "");
        const node = String(e.node || "");
        const content_text = String(e.content || "");
        const wrap = document.createElement("div");
        wrap.className = "utterance " + speaker;
        const who = document.createElement("div");
        who.className = "who";
        who.textContent = (node ? node + " · " : "") + speaker + (role ? " · " + role : "");
        const txt = document.createElement("div");
        txt.className = "text";
        // Truncate very long utterances inline; users can expand the details for the full log.
        txt.textContent = content_text.length > 1200 ? content_text.slice(0, 1200) + "\\n…(truncated)" : content_text;
        wrap.appendChild(who);
        wrap.appendChild(txt);
        dialogueBox.appendChild(wrap);
      }
    }

    function renderSummary(payload) {
      const sec = document.createElement("div");
      sec.className = "section";
      const h = document.createElement("h4");
      h.textContent = "Result";
      sec.appendChild(h);
      const summary = document.createElement("div");
      summary.className = "summary " + (payload.halt_reason ? "halted" : "done");
      const title = document.createElement("div");
      title.className = "title";
      title.textContent = payload.halt_reason
        ? "HALTED: " + payload.halt_reason
        : "DONE";
      summary.appendChild(title);
      if (Array.isArray(payload.proposed_diffs) && payload.proposed_diffs.length > 0) {
        const filesH = document.createElement("div");
        filesH.className = "small";
        filesH.textContent = "Files touched (click to open):";
        filesH.style.marginTop = "6px";
        summary.appendChild(filesH);
        const files = document.createElement("div");
        files.className = "files";
        // Deduplicate by file_path
        const seen = new Set();
        for (const d of payload.proposed_diffs) {
          const fp = String(d.file_path || "");
          if (!fp || seen.has(fp)) continue;
          seen.add(fp);
          const row = document.createElement("div");
          row.className = "file-row";
          const name = document.createElement("div");
          name.textContent = fp;
          row.appendChild(name);
          const bullet = String(d.contract_bullet_quoted || "");
          if (bullet) {
            const b = document.createElement("div");
            b.className = "bullet";
            b.textContent = bullet;
            row.appendChild(b);
          }
          row.addEventListener("click", () => vscode.postMessage({ type: "open_file", path: fp }));
          files.appendChild(row);
        }
        summary.appendChild(files);
      }
      if (payload.pre_execute_commit_hash) {
        const rollH = document.createElement("div");
        rollH.className = "small";
        rollH.textContent = "Rollback (click to copy):";
        rollH.style.marginTop = "6px";
        summary.appendChild(rollH);
        const cmd = document.createElement("div");
        cmd.className = "rollback";
        cmd.textContent = 'git reset --hard ' + String(payload.pre_execute_commit_hash);
        summary.appendChild(cmd);
      }
      sec.appendChild(summary);
      content.insertBefore(sec, content.firstChild);
    }

    function setRunning(running) {
      runBtn.disabled = running;
      stopBtn.disabled = !running;
      taskEl.disabled = running;
    }

    runBtn.addEventListener("click", () => {
      const task = taskEl.value.trim();
      if (!task) return;
      setRunning(true);
      startBuildUI();
      vscode.postMessage({ type: "run", task });
    });

    stopBtn.addEventListener("click", () => vscode.postMessage({ type: "stop" }));

    window.addEventListener("message", (ev) => {
      const m = ev.data;
      if (!m || typeof m.type !== "string") return;
      switch (m.type) {
        case "status": {
          const h = m.health || "unknown";
          statusEl.textContent = h;
          statusEl.className = "badge " +
            (h === "connected" ? "ok" :
             (h === "stopped" || h === "unreachable" || String(h).startsWith("error")) ? "err" : "warn");
          break;
        }
        case "build_started":
          if (m.project_dir) projHint.textContent = "workspace: " + String(m.project_dir);
          break;
        case "node_complete": {
          const name = String(m.node_name || "");
          const log = m.log === null || m.log === undefined ? "" : String(m.log);
          const halt = m.halt_reason ? String(m.halt_reason) : "";
          const state = halt ? "fail" : "done";
          setNodeState(name, state, log + (halt ? "  · " + halt : ""));
          // Mark earlier nodes that fired as done (in case we missed any) and the next as running.
          let crossed = false;
          for (const n of NODE_ORDER) {
            if (n === name) { crossed = true; continue; }
            if (!crossed && nodeRows[n] && nodeRows[n].state === "pending") {
              setNodeState(n, "done");
            }
            if (crossed && nodeRows[n] && nodeRows[n].state === "pending") {
              setNodeState(n, "running", "running…");
              break;
            }
          }
          appendDialogue(m.dialogue_delta);
          break;
        }
        case "build_done":
          // Any still-pending node never fired — leave it visually pending (the pipeline may have short-circuited)
          renderSummary({
            halt_reason: m.halt_reason,
            pre_execute_commit_hash: m.pre_execute_commit_hash,
            proposed_diffs: m.proposed_diffs,
            design_contract: m.design_contract,
          });
          setRunning(false);
          break;
        case "error": {
          const err = document.createElement("div");
          err.className = "error";
          err.textContent = "Error: " + String(m.reason || "unknown");
          content.appendChild(err);
          setRunning(false);
          break;
        }
      }
    });

    vscode.postMessage({ type: "ready" });
  </script>
</body>
</html>`;
  }
}

function genNonce(): string {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  let s = "";
  for (let i = 0; i < 32; i++) s += chars[Math.floor(Math.random() * chars.length)];
  return s;
}
