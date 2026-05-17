import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";

import { streamSSE } from "../sseClient";

const SESSION_REL_PATH = "_collab/chat_session.jsonl";

/**
 * Chat panel — debate-mode chat backed by the cothink Python /chat SSE endpoint.
 *
 * UX matches Claude Code's extension pattern (chat at bottom, scrollback above,
 * streaming messages). cothink's twist is that each round shows two attributed
 * speakers (Claude in cyan, Gemini in magenta) instead of one assistant voice.
 *
 * Webview ↔ Extension protocol:
 *   webview → extension:
 *     { type: "ready" }              — webview loaded, send current status
 *     { type: "send", text }         — user submitted a message
 *     { type: "reset" }              — wipe history (also clears webview log)
 *     { type: "stop" }               — abort the active SSE stream
 *   extension → webview:
 *     { type: "status", health }     — server health badge
 *     { type: "user_message", text } — echo back user message for rendering
 *     { type: "round_start", round, speaker }
 *     { type: "round_chunk", round, speaker, text }
 *     { type: "round_complete", round, speaker, is_stop_signal }
 *     { type: "done", total_rounds } — debate finished
 *     { type: "error", reason }
 *     { type: "reset_ack" }
 */
export class ChatPanelProvider implements vscode.WebviewViewProvider {
  private view: vscode.WebviewView | undefined;
  private history: unknown[] = [];
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

    // v0.5.1: hydrate prior session from disk if present. Fire-and-forget;
    // the webview will receive a session_loaded message when ready.
    this.loadSession().catch((e) =>
      console.error("[cothink] loadSession failed:", e),
    );

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
      case "send":
        await this.sendUserMessage(String(msg.text ?? ""));
        return;
      case "reset":
        this.activeAbort?.abort();
        this.history = [];
        await this.clearSession();
        this.post({ type: "reset_ack" });
        return;
      case "stop":
        this.activeAbort?.abort();
        return;
    }
  }

  private async sendUserMessage(text: string): Promise<void> {
    const trimmed = text.trim();
    if (!trimmed) return;

    const projectDir = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    if (!projectDir) {
      this.post({
        type: "error",
        reason: "No workspace folder open. Open a project folder before chatting.",
      });
      return;
    }

    this.post({ type: "user_message", text: trimmed });

    this.activeAbort = new AbortController();
    let totalRounds = 0;
    let debateCompleted = false;
    try {
      await streamSSE(
        `http://127.0.0.1:${this.getServerPort()}/chat`,
        {
          method: "POST",
          body: {
            message: trimmed,
            history: this.history,
            project_dir: projectDir,
          },
          signal: this.activeAbort.signal,
        },
        (ev) => {
          const data = (ev.data ?? {}) as Record<string, unknown>;
          switch (ev.event) {
            case "round_start":
              this.post({
                type: "round_start",
                round: data.round,
                speaker: data.speaker,
              });
              break;
            case "round_chunk":
              this.post({
                type: "round_chunk",
                round: data.round,
                speaker: data.speaker,
                text: data.text,
              });
              break;
            case "round_complete":
              this.post({
                type: "round_complete",
                round: data.round,
                speaker: data.speaker,
                is_stop_signal: data.is_stop_signal,
              });
              totalRounds = Number(data.round) || totalRounds;
              break;
            case "done":
              if (Array.isArray(data.compressed_history)) {
                this.history = data.compressed_history;
              }
              if (typeof data.total_rounds === "number") {
                totalRounds = data.total_rounds;
              }
              debateCompleted = true;
              this.post({ type: "done", total_rounds: totalRounds });
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
      // Abort errors from user-initiated /stop are expected — surface a softer message.
      if (this.activeAbort?.signal.aborted) {
        this.post({ type: "error", reason: "stopped" });
      } else {
        this.post({ type: "error", reason });
      }
    } finally {
      this.activeAbort = undefined;
    }

    // v0.5.1: persist after the debate completes. Skipped on abort/error so
    // we don't save a partial conversation that the LLMs didn't get to wrap up.
    if (debateCompleted) {
      try {
        await this.saveSession();
      } catch (e) {
        console.error("[cothink] saveSession failed:", e);
      }
    }
  }

  // -------------------------------------------------------------------------
  // v0.5.1: per-project session persistence to <workspace>/_collab/chat_session.jsonl
  // -------------------------------------------------------------------------

  private sessionFilePath(): string | undefined {
    const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    if (!workspaceRoot) return undefined;
    return path.join(workspaceRoot, SESSION_REL_PATH);
  }

  private async loadSession(): Promise<void> {
    const sessionPath = this.sessionFilePath();
    if (!sessionPath) {
      console.warn("[cothink] no workspace folder open; chat persistence disabled");
      return;
    }
    if (!fs.existsSync(sessionPath)) return;

    const content = await fs.promises.readFile(sessionPath, "utf-8");
    const entries: unknown[] = [];
    for (const line of content.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        entries.push(JSON.parse(trimmed));
      } catch (e) {
        console.warn("[cothink] skipping malformed session line:", trimmed.slice(0, 80));
      }
    }
    if (entries.length === 0) return;
    this.history = entries;
    this.post({ type: "session_loaded", entries, path: sessionPath });
  }

  private async saveSession(): Promise<void> {
    const sessionPath = this.sessionFilePath();
    if (!sessionPath) return;
    await fs.promises.mkdir(path.dirname(sessionPath), { recursive: true });
    const body =
      this.history
        .map((entry) => JSON.stringify(entry))
        .join("\n") + (this.history.length ? "\n" : "");
    await fs.promises.writeFile(sessionPath, body, "utf-8");
  }

  private async clearSession(): Promise<void> {
    const sessionPath = this.sessionFilePath();
    if (!sessionPath) return;
    if (!fs.existsSync(sessionPath)) return;
    await fs.promises.writeFile(sessionPath, "", "utf-8");
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

    #log {
      flex: 1;
      overflow-y: auto;
      padding: 10px;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    #log:empty::before {
      content: "Ask a question. Claude and Gemini will debate it across multiple rounds until one says 'I think we've covered this.'";
      color: var(--vscode-descriptionForeground);
      font-style: italic;
      align-self: center;
      text-align: center;
      max-width: 280px;
      padding: 40px 8px;
    }

    .msg {
      padding: 8px 10px;
      border-radius: 4px;
      word-wrap: break-word;
    }
    .msg .label {
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.5px;
      text-transform: uppercase;
      margin-bottom: 4px;
      color: var(--vscode-descriptionForeground);
    }
    .msg .body {
      white-space: pre-wrap;
      word-break: break-word;
      font-size: 13px;
      line-height: 1.5;
    }

    .msg.user {
      background: var(--vscode-input-background);
      border: 1px solid var(--vscode-input-border, transparent);
    }
    .msg.user .label { color: var(--vscode-charts-foreground, inherit); }

    .msg.claude { border-left: 3px solid var(--claude); padding-left: 10px; background: transparent; }
    .msg.claude .label { color: var(--claude); }
    .msg.gemini { border-left: 3px solid var(--gemini); padding-left: 10px; background: transparent; }
    .msg.gemini .label { color: var(--gemini); }

    /* Prior-session entries: same shape, dimmed so the live debate stands out */
    .msg.prior { opacity: 0.7; }
    .msg.prior .body { font-size: 12px; }
    .msg.summary { border-left: 3px solid var(--vscode-descriptionForeground, #888); padding-left: 10px; background: transparent; }
    .msg.summary .label { color: var(--vscode-descriptionForeground); }

    .msg.streaming .body::after {
      content: "▌";
      opacity: 0.6;
      animation: blink 1.1s steps(2) infinite;
      margin-left: 2px;
    }
    @keyframes blink { 50% { opacity: 0; } }

    .system {
      text-align: center;
      font-size: 11px;
      color: var(--vscode-descriptionForeground);
      font-style: italic;
      padding: 4px 0;
    }
    .system.error { color: var(--vscode-errorForeground); font-style: normal; }

    #composer {
      flex-shrink: 0;
      border-top: 1px solid var(--vscode-panel-border);
      padding: 8px;
      display: flex;
      gap: 6px;
      align-items: flex-end;
    }
    #input {
      flex: 1;
      resize: none;
      min-height: 32px;
      max-height: 200px;
      padding: 6px 8px;
      background: var(--vscode-input-background);
      color: var(--vscode-input-foreground);
      border: 1px solid var(--vscode-input-border, transparent);
      font-family: inherit;
      font-size: 13px;
      line-height: 1.4;
      outline: none;
    }
    #input:focus { border-color: var(--vscode-focusBorder); }
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

    #toolbar {
      display: flex;
      gap: 4px;
      padding: 4px 10px;
      border-bottom: 1px solid var(--vscode-panel-border);
      flex-shrink: 0;
    }
  </style>
</head>
<body>
  <div id="status-bar">
    Server: <span id="status" class="badge warn">connecting…</span>
  </div>
  <div id="toolbar">
    <button id="reset" class="secondary" title="Clear chat history">Reset</button>
    <button id="stop" class="secondary" title="Stop the current debate" disabled>Stop</button>
  </div>
  <div id="log"></div>
  <div id="composer">
    <textarea id="input" rows="2" placeholder="Ask Claude and Gemini something (Enter to send, Shift+Enter for newline; /reset to clear)"></textarea>
    <button id="send">Send</button>
  </div>

  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    const log = document.getElementById("log");
    const input = document.getElementById("input");
    const sendBtn = document.getElementById("send");
    const stopBtn = document.getElementById("stop");
    const resetBtn = document.getElementById("reset");
    const statusEl = document.getElementById("status");

    let activeRound = null;

    function appendMessage(cls, labelText, bodyText) {
      const div = document.createElement("div");
      div.className = "msg " + cls;
      const label = document.createElement("div");
      label.className = "label";
      label.textContent = labelText;
      const body = document.createElement("div");
      body.className = "body";
      body.textContent = bodyText;
      div.appendChild(label);
      div.appendChild(body);
      log.appendChild(div);
      scrollToBottom();
      return div;
    }

    function appendSystem(text, isError) {
      const div = document.createElement("div");
      div.className = "system" + (isError ? " error" : "");
      div.textContent = text;
      log.appendChild(div);
      scrollToBottom();
    }

    function scrollToBottom() {
      requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
    }

    function startRound(round, speaker) {
      const cls = String(speaker).toLowerCase();
      const div = appendMessage(cls + " streaming", "round " + round + " · " + speaker, "");
      activeRound = div;
    }

    function appendChunk(text) {
      if (!activeRound) return;
      activeRound.querySelector(".body").textContent += text;
      scrollToBottom();
    }

    function finishRound(isStop) {
      if (!activeRound) return;
      activeRound.classList.remove("streaming");
      if (isStop) {
        appendSystem("(model emitted the stop phrase — debate ends)", false);
      }
      activeRound = null;
    }

    function setBusy(busy) {
      input.disabled = busy;
      sendBtn.disabled = busy;
      stopBtn.disabled = !busy;
      if (!busy) input.focus();
    }

    function send() {
      const text = input.value.trim();
      if (!text) return;
      if (text === "/reset") {
        log.innerHTML = "";
        vscode.postMessage({ type: "reset" });
        input.value = "";
        return;
      }
      input.value = "";
      setBusy(true);
      vscode.postMessage({ type: "send", text });
    }

    sendBtn.addEventListener("click", send);
    stopBtn.addEventListener("click", () => vscode.postMessage({ type: "stop" }));
    resetBtn.addEventListener("click", () => {
      log.innerHTML = "";
      vscode.postMessage({ type: "reset" });
    });

    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        send();
      }
    });

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
        case "user_message":
          appendMessage("user", "you", m.text || "");
          break;
        case "round_start":
          startRound(m.round, m.speaker);
          break;
        case "round_chunk":
          appendChunk(String(m.text || ""));
          break;
        case "round_complete":
          finishRound(!!m.is_stop_signal);
          break;
        case "done":
          appendSystem("debate ended (" + (m.total_rounds || 0) + " rounds)");
          setBusy(false);
          break;
        case "error":
          appendSystem("error: " + (m.reason || "unknown"), true);
          setBusy(false);
          if (activeRound) {
            activeRound.classList.remove("streaming");
            activeRound = null;
          }
          break;
        case "reset_ack":
          appendSystem("history cleared");
          break;
        case "session_loaded": {
          const entries = Array.isArray(m.entries) ? m.entries : [];
          for (const e of entries) {
            if (!e || typeof e !== "object") continue;
            if (e.role === "user") {
              appendMessage("user prior", "you · prior", String(e.content || ""));
            } else if (e.role === "assistant") {
              const speaker = String(e.speaker || "team");
              const cls = speaker === "team" ? "summary prior" : speaker.toLowerCase() + " prior";
              const label = speaker === "team" ? "summary · prior" : speaker + " · prior";
              appendMessage(cls, label, String(e.content || ""));
            }
          }
          if (entries.length) {
            appendSystem("— prior session loaded (" + entries.length + " entries) —");
          }
          break;
        }
      }
    });

    vscode.postMessage({ type: "ready" });
    input.focus();
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
