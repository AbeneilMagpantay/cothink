import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";

import { streamSSE } from "../sseClient";

const SESSION_REL_PATH = "_collab/workbench_session.jsonl";

/**
 * v0.6.2 — paste payload format the webview sends to the extension host
 * (and which the extension forwards to the Python /build endpoint in the
 * `images` array). The Python image_handler decodes + downsamples server-side.
 */
interface PastedImage {
  filename: string;
  data_base64: string;
}

/**
 * v0.6.6 Context Chips — one @-mention from the composer.
 * Mirrors the Python-side MentionPayload schema (server.py).
 */
interface Mention {
  kind: "file" | "folder" | "symbol";
  path: string;   // workspace-relative
  symbol?: string;
}

/**
 * Unified Workbench panel (v0.5.4) — replaces the separate Chat and Build panels.
 *
 * One textarea, one conversation thread. Every user message runs through the
 * full integrity pipeline (Discovery → Planning → optionally Executing →
 * Mechanical Gate → Contract Review). Planning emits a `build_needed` flag in
 * its verdict — when false (the message was a question/analysis, not a build
 * task), Executing/Gate/Review are skipped and the Discovery+Planning output
 * IS the response. When true, the full pipeline runs and files get written.
 *
 * UX: Cursor/Claude-Code-shaped chat with each "turn" being one pipeline run
 * shown inline as a collapsible group below the user message. Dual-brain
 * exchanges (Claude/Gemini per-node dialogue) appear as cyan/magenta cards
 * inside each phase.
 *
 * Session persistence: `<workspace>/_collab/workbench_session.jsonl`.
 * One JSON line per persisted entry; same schema spirit as the v0.5.1 chat
 * session file. Each completed turn writes a user_message line + a turn
 * summary line. Reset truncates.
 */
export class WorkbenchPanelProvider implements vscode.WebviewViewProvider {
  private view: vscode.WebviewView | undefined;
  private activeAbort: AbortController | undefined;
  private statusInterval: NodeJS.Timeout | undefined;
  private history: unknown[] = [];

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

    this.loadSession().catch((e) =>
      console.error("[cothink] workbench loadSession failed:", e),
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
        // v0.6.2: webview now sends `images` alongside `text` when the user
        // pasted screenshots.
        // v0.6.6: webview also sends `mentions` when the user picked @-chips.
        await this.sendTurn(
          String(msg.text ?? ""),
          Array.isArray(msg.images) ? (msg.images as PastedImage[]) : [],
          Array.isArray(msg.mentions) ? (msg.mentions as Mention[]) : [],
        );
        return;
      case "stop":
        this.activeAbort?.abort();
        return;
      case "open_file":
        await this.openFile(String(msg.path ?? ""));
        return;
      case "request_mention_picker":
        // v0.6.6 Context Chips: webview asked us to pop a QuickPick.
        await this.pickMention(String(msg.requestId ?? ""));
        return;
      case "reset":
        this.activeAbort?.abort();
        this.history = [];
        await this.clearSession();
        this.post({ type: "reset_ack" });
        return;
    }
  }

  private async sendTurn(
    text: string,
    images: PastedImage[] = [],
    mentions: Mention[] = [],
  ): Promise<void> {
    const trimmed = text.trim();
    // Allow an image-only or mention-only turn (no text required).
    if (!trimmed && images.length === 0 && mentions.length === 0) return;

    const projectDir = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    if (!projectDir) {
      this.post({
        type: "error",
        reason: "No workspace folder open. Open a project folder before sending a message.",
      });
      return;
    }

    const turnId = "t" + Date.now() + "-" + Math.floor(Math.random() * 1e6);
    this.post({
      type: "turn_started",
      turn_id: turnId,
      text: trimmed,
      project_dir: projectDir,
      image_count: images.length,
      mention_count: mentions.length,
    });
    this.history.push({
      role: "user",
      content: trimmed,
      turn_id: turnId,
      ...(images.length > 0 ? { image_count: images.length } : {}),
      ...(mentions.length > 0 ? { mentions } : {}),
    });

    this.activeAbort = new AbortController();
    let turnSummary: Record<string, unknown> | null = null;

    try {
      await streamSSE(
        `http://127.0.0.1:${this.getServerPort()}/build`,
        {
          method: "POST",
          body: {
            task: trimmed,
            project_dir: projectDir,
            ...(images.length > 0 ? { images } : {}),
            ...(mentions.length > 0 ? { mentions } : {}),
          },
          signal: this.activeAbort.signal,
        },
        (ev) => {
          const data = (ev.data ?? {}) as Record<string, unknown>;
          switch (ev.event) {
            case "images_attached":
              // v0.6.2: server confirms the downsampled image paths it saved.
              // Surface to the webview so the user sees "📎 N image(s) attached".
              this.post({
                type: "images_attached",
                turn_id: turnId,
                paths: data.paths,
              });
              break;
            case "thinking_chunk":
              this.post({
                type: "thinking_chunk",
                turn_id: turnId,
                node: data.node,
                speaker: data.speaker,
                role: data.role,
                text: data.text,
              });
              break;
            case "node_complete":
              this.post({
                type: "node_complete",
                turn_id: turnId,
                node_name: data.node_name,
                log: data.log,
                dialogue_delta: data.dialogue_delta,
                halt_reason: data.halt_reason,
              });
              break;
            case "done":
              turnSummary = {
                halt_reason: data.halt_reason,
                pre_execute_commit_hash: data.pre_execute_commit_hash,
                proposed_diffs: data.proposed_diffs,
                design_contract: data.design_contract,
              };
              this.post({ type: "turn_done", turn_id: turnId, ...turnSummary });
              break;
            case "error":
              this.post({
                type: "error",
                turn_id: turnId,
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
        this.post({ type: "error", turn_id: turnId, reason: "stopped" });
      } else {
        this.post({ type: "error", turn_id: turnId, reason });
      }
    } finally {
      this.activeAbort = undefined;
    }

    if (turnSummary !== null) {
      const summary = turnSummary as Record<string, unknown>;
      this.history.push({
        role: "assistant",
        turn_id: turnId,
        halt_reason: summary.halt_reason,
        pre_execute_commit_hash: summary.pre_execute_commit_hash,
        proposed_diffs: summary.proposed_diffs,
        design_contract: summary.design_contract,
      });
      try {
        await this.saveSession();
      } catch (e) {
        console.error("[cothink] workbench saveSession failed:", e);
      }
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

  /**
   * v0.6.6 Context Chips — pops a native VSCode QuickPick listing workspace
   * files + folders. User picks one; we post `mention_picked` back to the
   * webview with `{kind, path}`. The webview converts it to a pill in the
   * chip-tray and removes the `@` trigger from the composer.
   *
   * Using QuickPick instead of a custom webview dropdown gets us:
   *  - native fuzzy search
   *  - keyboard-first nav (up/down/enter)
   *  - workspace integration (Antigravity / VSCode handles ignored files)
   *  - zero CSS to maintain
   *
   * The downside is the picker pops in the center of the IDE rather than
   * inline under the composer. Trade-off accepted for v0.6.6 — can rebuild
   * as inline dropdown later if dogfood surfaces it as a friction point.
   */
  private async pickMention(requestId: string): Promise<void> {
    const folders = vscode.workspace.workspaceFolders;
    if (!folders || folders.length === 0) {
      this.post({
        type: "mention_picked",
        requestId,
        cancelled: true,
        reason: "No workspace folder open.",
      });
      return;
    }
    const workspaceRoot = folders[0].uri.fsPath;

    // Two-step pick: kind first, then resource. Single-step would mix
    // files + folders in one giant list — confusing fuzzy match.
    const kind = await vscode.window.showQuickPick(
      [
        { label: "$(file) File", description: "Pin a specific file", value: "file" as const },
        { label: "$(folder) Folder", description: "Pin a folder listing", value: "folder" as const },
      ],
      { placeHolder: "What kind of context do you want to pin?" },
    );
    if (!kind) {
      this.post({ type: "mention_picked", requestId, cancelled: true });
      return;
    }

    let pickedPath: string | undefined;
    if (kind.value === "file") {
      const uris = await vscode.workspace.findFiles(
        "**/*",
        "{**/node_modules/**,**/.git/**,**/__pycache__/**,**/.venv/**,**/dist/**,**/build/**,**/out/**}",
        2000,
      );
      const items = uris.map((u) => {
        const rel = path.relative(workspaceRoot, u.fsPath).replace(/\\/g, "/");
        return { label: rel, description: "", uri: u };
      });
      const pick = await vscode.window.showQuickPick(items, {
        placeHolder: `Select a file (${items.length} candidates)`,
        matchOnDescription: true,
      });
      pickedPath = pick?.label;
    } else {
      // Walk directories from workspace root; depth-limited so massive repos
      // don't freeze the picker.
      const dirs = await this.listFolders(workspaceRoot, 6);
      const items = dirs.map((d) => ({ label: d || "." }));
      const pick = await vscode.window.showQuickPick(items, {
        placeHolder: `Select a folder (${items.length} candidates)`,
      });
      pickedPath = pick?.label;
    }

    if (!pickedPath) {
      this.post({ type: "mention_picked", requestId, cancelled: true });
      return;
    }

    this.post({
      type: "mention_picked",
      requestId,
      mention: { kind: kind.value, path: pickedPath },
    });
  }

  /** v0.6.6 helper: workspace-relative folder list, capped depth. */
  private async listFolders(root: string, maxDepth: number): Promise<string[]> {
    const out: string[] = [""];   // root itself, rendered as "."
    const skip = new Set([
      "node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build", "out", ".next",
    ]);
    const fs = await import("fs/promises");

    const walk = async (dirAbs: string, rel: string, depth: number): Promise<void> => {
      if (depth >= maxDepth) return;
      let entries: { name: string; isDirectory: () => boolean }[] = [];
      try {
        entries = await fs.readdir(dirAbs, { withFileTypes: true }) as unknown as {
          name: string; isDirectory: () => boolean;
        }[];
      } catch {
        return;
      }
      for (const e of entries) {
        if (!e.isDirectory()) continue;
        if (skip.has(e.name) || e.name.startsWith(".")) continue;
        const childRel = rel ? `${rel}/${e.name}` : e.name;
        out.push(childRel);
        await walk(path.join(dirAbs, e.name), childRel, depth + 1);
      }
    };
    await walk(root, "", 0);
    out.sort();
    return out;
  }

  private sessionFilePath(): string | undefined {
    const root = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    if (!root) return undefined;
    return path.join(root, SESSION_REL_PATH);
  }

  private async loadSession(): Promise<void> {
    const sessionPath = this.sessionFilePath();
    if (!sessionPath || !fs.existsSync(sessionPath)) return;
    const content = await fs.promises.readFile(sessionPath, "utf-8");
    const entries: unknown[] = [];
    for (const line of content.split("\n")) {
      const t = line.trim();
      if (!t) continue;
      try {
        entries.push(JSON.parse(t));
      } catch {
        // skip malformed lines silently — same forgiving pattern as chat session
      }
    }
    if (!entries.length) return;
    this.history = entries;
    this.post({ type: "session_loaded", entries, path: sessionPath });
  }

  private async saveSession(): Promise<void> {
    const sessionPath = this.sessionFilePath();
    if (!sessionPath) return;
    await fs.promises.mkdir(path.dirname(sessionPath), { recursive: true });
    const body = this.history.map((e) => JSON.stringify(e)).join("\n") + (this.history.length ? "\n" : "");
    await fs.promises.writeFile(sessionPath, body, "utf-8");
  }

  private async clearSession(): Promise<void> {
    const sessionPath = this.sessionFilePath();
    if (!sessionPath || !fs.existsSync(sessionPath)) return;
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
      display: flex; flex-direction: column;
    }
    #status-bar {
      padding: 4px 10px; font-size: 11px;
      color: var(--vscode-descriptionForeground);
      border-bottom: 1px solid var(--vscode-panel-border);
      flex-shrink: 0; display: flex; align-items: center; gap: 8px;
    }
    .badge { font-weight: 600; padding: 1px 6px; border-radius: 3px; }
    .badge.ok { color: var(--vscode-charts-green); }
    .badge.err { color: var(--vscode-errorForeground); }
    .badge.warn { color: var(--vscode-charts-yellow); }
    #pacer {
      display: flex; align-items: center; gap: 4px;
      margin-left: 12px; font-size: 10.5px;
      color: var(--vscode-descriptionForeground);
    }
    #pacer-bars { letter-spacing: -1px; font-family: var(--vscode-editor-font-family, monospace); }
    #pacer-bars.ok { color: var(--vscode-charts-green); }
    #pacer-bars.warn { color: var(--vscode-charts-yellow); }
    #pacer-bars.danger { color: var(--vscode-errorForeground); }
    #toolbar { display: flex; gap: 4px; margin-left: auto; }
    button.toolbar-btn {
      padding: 2px 8px; font-size: 11px;
      background: var(--vscode-button-secondaryBackground, transparent);
      color: var(--vscode-button-secondaryForeground, var(--vscode-foreground));
      border: 1px solid var(--vscode-input-border, transparent);
      cursor: pointer; font-family: inherit;
    }
    button.toolbar-btn:hover { background: var(--vscode-button-secondaryHoverBackground, var(--vscode-list-hoverBackground)); }
    button.toolbar-btn:disabled { opacity: 0.5; cursor: not-allowed; }

    #log {
      flex: 1; overflow-y: auto; padding: 10px; display: flex;
      flex-direction: column; gap: 14px;
    }
    #log:empty::before {
      content: "Workbench: type a question or a build task. Both Claude and Gemini reason on every message — Discovery + Planning run automatically; Executing fires only when files actually need writing.";
      color: var(--vscode-descriptionForeground);
      font-style: italic; text-align: center; max-width: 320px;
      margin: 60px auto; padding: 0 8px;
    }

    .user-msg {
      padding: 8px 10px; border-radius: 4px;
      background: var(--vscode-input-background);
      border: 1px solid var(--vscode-input-border, transparent);
    }
    .user-msg .label {
      font-size: 10px; font-weight: 700; letter-spacing: 0.5px;
      text-transform: uppercase; margin-bottom: 4px;
      color: var(--vscode-descriptionForeground);
    }
    .user-msg .body { white-space: pre-wrap; font-size: 13px; line-height: 1.5; }

    .turn {
      display: flex; flex-direction: column; gap: 6px;
      padding-left: 8px; border-left: 2px solid var(--vscode-panel-border);
    }
    .phase {
      display: flex; flex-direction: column; gap: 4px;
      padding: 6px 8px; border-radius: 3px;
      background: var(--vscode-editor-inactiveSelectionBackground);
    }
    .phase-header {
      display: flex; align-items: center; gap: 8px;
      cursor: pointer; user-select: none;
    }
    .phase .marker { width: 14px; text-align: center; }
    .phase .phase-name { font-weight: 600; font-size: 12px; }
    .phase .phase-log {
      font-size: 11px; color: var(--vscode-descriptionForeground);
      flex: 1; word-break: break-word;
    }
    .phase .phase-body { padding: 6px 0 4px 22px; display: none; }
    .phase.open .phase-body { display: block; }
    .phase.pending .marker { color: var(--vscode-descriptionForeground); }
    .phase.pending .phase-name { opacity: 0.6; }
    .phase.running { background: var(--vscode-list-hoverBackground); }
    .phase.running .marker { animation: spin 1s linear infinite; display: inline-block; }
    .phase.done { border-left: 3px solid var(--vscode-charts-green); }
    .phase.done .marker { color: var(--vscode-charts-green); }
    .phase.fail { border-left: 3px solid var(--vscode-errorForeground); }
    .phase.fail .marker { color: var(--vscode-errorForeground); }
    .phase.skipped { opacity: 0.5; }
    .phase.skipped .marker { color: var(--vscode-descriptionForeground); }
    @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }

    .utterance {
      padding: 6px 8px; margin-bottom: 6px;
      border-radius: 2px; word-break: break-word;
    }
    .utterance .who {
      font-size: 10px; font-weight: 700; letter-spacing: 0.3px;
      text-transform: uppercase; margin-bottom: 3px;
    }
    .utterance .text {
      font-size: 12px; white-space: pre-wrap; line-height: 1.4;
    }
    .utterance.claude { border-left: 3px solid var(--claude); padding-left: 8px; }
    .utterance.claude .who { color: var(--claude); }
    .utterance.gemini { border-left: 3px solid var(--gemini); padding-left: 8px; }
    .utterance.gemini .who { color: var(--gemini); }
    .utterance.streaming .text::after {
      content: "▍"; animation: blink 1s steps(2) infinite;
      color: var(--vscode-descriptionForeground);
      margin-left: 1px;
    }
    @keyframes blink { 50% { opacity: 0; } }

    .tool {
      margin: 4px 0; border-radius: 3px; overflow: hidden;
      font-family: var(--vscode-editor-font-family, monospace);
      font-size: 11.5px;
      background: var(--vscode-textCodeBlock-background, rgba(0,0,0,0.25));
      border-left: 3px solid var(--vscode-charts-yellow, #d7ba7d);
    }
    .tool .tool-cmd {
      padding: 4px 8px; white-space: pre-wrap; word-break: break-all;
      color: var(--vscode-terminal-foreground, var(--vscode-foreground));
    }
    .tool .tool-cmd::before {
      content: "▸ "; color: var(--vscode-charts-yellow, #d7ba7d); font-weight: 700;
    }
    .tool.result { border-left-color: var(--vscode-charts-green, #6a9955); }
    .tool.result .tool-out {
      padding: 4px 8px; white-space: pre-wrap; word-break: break-word;
      color: var(--vscode-descriptionForeground);
      max-height: 240px; overflow-y: auto;
      border-top: 1px solid var(--vscode-panel-border);
    }
    .tool.error { border-left-color: var(--vscode-errorForeground); }
    .tool.error .tool-out { color: var(--vscode-errorForeground); }

    .thinking-block {
      margin: 4px 0; padding: 6px 8px; border-radius: 3px;
      background: var(--vscode-editor-inactiveSelectionBackground);
      border-left: 2px dashed var(--vscode-descriptionForeground);
      font-size: 11.5px; font-style: italic;
      color: var(--vscode-descriptionForeground);
      white-space: pre-wrap; word-break: break-word;
    }

    .summary {
      padding: 8px 10px; border-radius: 3px;
      background: var(--vscode-editor-inactiveSelectionBackground);
    }
    .summary.done { border-left: 3px solid var(--vscode-charts-green); }
    .summary.halted { border-left: 3px solid var(--vscode-errorForeground); }
    .summary.analysis { border-left: 3px solid var(--vscode-charts-blue, #4ec9b0); }
    .summary .title { font-weight: 700; font-size: 12px; margin-bottom: 4px; }
    .summary .files { display: flex; flex-direction: column; gap: 3px; margin-top: 6px; }
    .summary .file-row {
      padding: 4px 8px; background: var(--vscode-textCodeBlock-background, rgba(0,0,0,0.2));
      border-radius: 2px; cursor: pointer;
      font-family: var(--vscode-editor-font-family); font-size: 11px;
      word-break: break-all;
    }
    .summary .file-row:hover { background: var(--vscode-list-hoverBackground); }
    .summary .rollback {
      font-family: var(--vscode-editor-font-family); font-size: 11px;
      background: var(--vscode-textCodeBlock-background, rgba(0,0,0,0.2));
      padding: 4px 6px; margin-top: 6px; border-radius: 2px;
      word-break: break-all; user-select: all;
    }
    .small { font-size: 11px; color: var(--vscode-descriptionForeground); }

    .system { text-align: center; font-size: 11px; color: var(--vscode-descriptionForeground); font-style: italic; padding: 4px 0; }
    .system.error { color: var(--vscode-errorForeground); font-style: normal; }

    #composer-wrap {
      flex-shrink: 0; border-top: 1px solid var(--vscode-panel-border);
      display: flex; flex-direction: column;
    }
    /* v0.6.6 Context Chips — pills above the composer showing what was @-pinned */
    #chip-tray {
      display: none; padding: 6px 8px 0; gap: 4px; flex-wrap: wrap;
    }
    #chip-tray.has-chips { display: flex; }
    .chip {
      display: inline-flex; align-items: center; gap: 4px;
      padding: 2px 6px 2px 8px; border-radius: 11px;
      background: var(--vscode-badge-background, #4f4f4f);
      color: var(--vscode-badge-foreground, #fff);
      font-size: 11px; font-family: var(--vscode-editor-font-family, monospace);
      max-width: 280px;
    }
    .chip .kind {
      font-size: 9px; text-transform: uppercase; letter-spacing: 0.5px;
      opacity: 0.7; margin-right: 2px;
    }
    .chip .path {
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
      direction: rtl; text-align: left;
    }
    .chip .x {
      cursor: pointer; user-select: none;
      width: 14px; height: 14px; line-height: 13px; text-align: center;
      border-radius: 50%; font-size: 11px;
      background: rgba(255,255,255,0.15);
    }
    .chip .x:hover { background: var(--vscode-errorForeground); }

    #image-tray {
      display: none; padding: 6px 8px 0; gap: 6px; flex-wrap: wrap;
    }
    #image-tray.has-images { display: flex; }
    #image-tray .thumb {
      position: relative; width: 56px; height: 56px;
      border: 1px solid var(--vscode-panel-border); border-radius: 3px;
      overflow: hidden; background: var(--vscode-editor-inactiveSelectionBackground);
    }
    #image-tray .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
    #image-tray .thumb .x {
      position: absolute; top: 1px; right: 1px;
      width: 16px; height: 16px; border-radius: 50%;
      background: rgba(0,0,0,0.7); color: #fff;
      font-size: 12px; line-height: 14px; text-align: center;
      cursor: pointer; user-select: none;
    }
    #image-tray .thumb .x:hover { background: var(--vscode-errorForeground); }
    #composer {
      padding: 8px; display: flex; gap: 6px; align-items: flex-end;
    }
    #input {
      flex: 1; resize: none; min-height: 36px; max-height: 240px;
      padding: 6px 8px; background: var(--vscode-input-background);
      color: var(--vscode-input-foreground);
      border: 1px solid var(--vscode-input-border, transparent);
      font-family: inherit; font-size: 13px; line-height: 1.4; outline: none;
    }
    #input:focus { border-color: var(--vscode-focusBorder); }
    button.primary {
      padding: 6px 12px;
      background: var(--vscode-button-background);
      color: var(--vscode-button-foreground);
      border: none; cursor: pointer; font-family: inherit; font-size: 12px;
    }
    button.primary:hover { background: var(--vscode-button-hoverBackground); }
    button.primary:disabled { opacity: 0.5; cursor: not-allowed; }

    .image-attached {
      font-size: 10.5px; color: var(--vscode-charts-blue, #4ec9b0);
      margin-top: 2px; font-style: italic;
    }
  </style>
</head>
<body>
  <div id="status-bar">
    Server: <span id="status" class="badge warn">connecting…</span>
    <div id="pacer" title="Rolling session usage. Subscription auth exposes no hard limit; this is an empirical heuristic to help you pace before hitting a 3 AM rate-limit wall.">
      <span id="pacer-bars" class="ok">▮▯▯▯▯▯▯▯▯▯</span>
      <span id="pacer-text">turn 0/~10 · 0 tok</span>
    </div>
    <div id="toolbar">
      <button id="reset" class="toolbar-btn" title="Clear all turns and reset session history">Reset</button>
      <button id="stop" class="toolbar-btn" disabled title="Abort the current turn">Stop</button>
    </div>
  </div>

  <div id="log"></div>

  <div id="composer-wrap">
    <div id="chip-tray" aria-label="@-mentioned files/folders queued for next message"></div>
    <div id="image-tray" aria-label="Pasted screenshots queued for next message"></div>
    <div id="composer">
      <textarea id="input" rows="2" placeholder="Ask a question, paste a screenshot (Ctrl+V), or type @ to pin a file/folder. Enter to send, Shift+Enter for newline."></textarea>
      <button id="send" class="primary">Send</button>
    </div>
  </div>

  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    const statusEl = document.getElementById("status");
    const log = document.getElementById("log");
    const input = document.getElementById("input");
    const sendBtn = document.getElementById("send");
    const stopBtn = document.getElementById("stop");
    const resetBtn = document.getElementById("reset");

    const NODE_ORDER = [
      "discovery", "planning", "executing", "mechanical",
      "learnings_enforcer", "contract_review", "project_state",
      "human_fallback",
    ];
    const NODE_LABEL = {
      discovery: "Discovery",
      planning: "Planning",
      executing: "Executing",
      mechanical: "Mechanical Gate",
      learnings_enforcer: "Learnings Enforcer",
      contract_review: "Contract Review",
      project_state: "Project State Journal",
      human_fallback: "Human Fallback",
    };

    const turns = {}; // turn_id → { container, phases: {name: el}, dialogueBox, summarySlot }

    function setBusy(busy) {
      input.disabled = busy;
      sendBtn.disabled = busy;
      stopBtn.disabled = !busy;
      if (!busy) input.focus();
    }
    function scrollToBottom() {
      requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
    }

    function addUserMsg(text, imageCount, mentionCount) {
      const div = document.createElement("div");
      div.className = "user-msg";
      const label = document.createElement("div");
      label.className = "label"; label.textContent = "you";
      const body = document.createElement("div");
      body.className = "body"; body.textContent = text;
      div.appendChild(label); div.appendChild(body);
      if (imageCount && imageCount > 0) {
        const att = document.createElement("div");
        att.className = "image-attached";
        att.textContent =
          "📎 " + imageCount + " screenshot" + (imageCount === 1 ? "" : "s") +
          " attached (downsampled ≤1024px, both brains will see them)";
        div.appendChild(att);
      }
      if (mentionCount && mentionCount > 0) {
        const att = document.createElement("div");
        att.className = "image-attached";
        att.textContent =
          "📌 " + mentionCount + " @-mention" + (mentionCount === 1 ? "" : "s") +
          " pinned (pre-loaded into Discovery context)";
        div.appendChild(att);
      }
      log.appendChild(div);
    }

    function addSystem(text, error) {
      const div = document.createElement("div");
      div.className = "system" + (error ? " error" : "");
      div.textContent = text;
      log.appendChild(div);
      scrollToBottom();
    }

    function startTurn(turn_id) {
      const container = document.createElement("div");
      container.className = "turn";
      container.dataset.turnId = turn_id;
      const phases = {};
      for (const name of NODE_ORDER) {
        if (name === "human_fallback") continue;
        const phase = createPhase(name, "pending");
        phases[name] = phase;
        container.appendChild(phase.row);
      }
      // First phase starts in "running" state
      const first = phases[NODE_ORDER[0]];
      setPhaseState(first, "running", "running…");
      // Summary slot — populated on turn_done
      const summarySlot = document.createElement("div");
      summarySlot.className = "summary-slot";
      container.appendChild(summarySlot);
      log.appendChild(container);
      turns[turn_id] = { container, phases, summarySlot };
      scrollToBottom();
    }

    function createPhase(name, state) {
      const row = document.createElement("div");
      row.className = "phase " + state;
      const header = document.createElement("div");
      header.className = "phase-header";
      const marker = document.createElement("span");
      marker.className = "marker"; marker.textContent = markerFor(state);
      const nameEl = document.createElement("span");
      nameEl.className = "phase-name"; nameEl.textContent = NODE_LABEL[name] || name;
      const logEl = document.createElement("span");
      logEl.className = "phase-log"; logEl.textContent = state === "pending" ? "" : "";
      header.appendChild(marker); header.appendChild(nameEl); header.appendChild(logEl);
      const body = document.createElement("div");
      body.className = "phase-body";
      row.appendChild(header); row.appendChild(body);
      header.addEventListener("click", () => { row.classList.toggle("open"); });
      return { row, header, body, marker, logEl, state };
    }

    function setPhaseState(phase, state, logText) {
      phase.row.className = "phase " + state + (phase.row.classList.contains("open") ? " open" : "");
      phase.marker.textContent = markerFor(state);
      if (logText !== undefined) phase.logEl.textContent = logText;
      phase.state = state;
    }

    function markerFor(state) {
      switch (state) {
        case "pending": return "○";
        case "running": return "◌";
        case "done": return "✓";
        case "fail": return "✗";
        case "skipped": return "–";
        default: return "•";
      }
    }

    function appendDialogue(phase, entries) {
      if (!phase || !Array.isArray(entries) || entries.length === 0) return;
      phase.row.classList.add("open");
      for (const e of entries) {
        if (!e || typeof e !== "object") continue;
        const speaker = String(e.speaker || "team").toLowerCase();
        const role = String(e.role || "");
        const content = String(e.content || "");
        // If we already have a live-streaming utterance for this speaker/role
        // in this phase, the dialogue entry is the "final" summary — skip it
        // to avoid double-rendering. The streamed version IS the canonical text.
        const liveKey = "live-" + speaker + "-" + role;
        if (phase.live && phase.live[liveKey]) {
          delete phase.live[liveKey];
          continue;
        }
        const wrap = document.createElement("div");
        wrap.className = "utterance " + speaker;
        const who = document.createElement("div");
        who.className = "who";
        who.textContent = speaker + (role ? " · " + role : "");
        const txt = document.createElement("div");
        txt.className = "text";
        txt.textContent = content.length > 4000 ? content.slice(0, 4000) + "\\n…(truncated)" : content;
        wrap.appendChild(who); wrap.appendChild(txt);
        phase.body.appendChild(wrap);
      }
    }

    // Append a streamed chunk into the live utterance box for (phase, speaker, role).
    // Creates a fresh utterance box on first chunk; appends text on subsequent.
    //
    // Special roles render as separate elements instead of stream-bubbles:
    //   - tool_call   → one-line terminal command card (Claude using Bash/Read/etc.)
    //   - tool_result → output block beneath the most recent tool_call card
    //   - tool_error  → same as tool_result but red
    //   - thinking    → dashed italic "internal reasoning" block
    // Everything else (propose / explore / merge / review) streams into a
    // standard speaker-colored utterance bubble.
    function appendChunk(phase, speaker, role, text) {
      if (!phase || !text) return;
      phase.row.classList.add("open");
      if (!phase.live) phase.live = {};

      if (role === "tool_call") {
        const tool = document.createElement("div");
        tool.className = "tool call";
        const cmd = document.createElement("div");
        cmd.className = "tool-cmd"; cmd.textContent = text;
        tool.appendChild(cmd);
        phase.body.appendChild(tool);
        // Stash so the next tool_result attaches inside this card.
        phase.lastTool = tool;
        // Tool call is a hard boundary — close any open streaming utterance.
        closeLiveUtterances(phase, speaker);
        scrollToBottom();
        return;
      }
      if (role === "tool_result" || role === "tool_error") {
        const host = phase.lastTool || (() => {
          const w = document.createElement("div");
          w.className = "tool " + (role === "tool_error" ? "error" : "result");
          phase.body.appendChild(w);
          return w;
        })();
        host.classList.add(role === "tool_error" ? "error" : "result");
        const out = document.createElement("div");
        out.className = "tool-out"; out.textContent = text;
        host.appendChild(out);
        scrollToBottom();
        return;
      }
      if (role === "thinking") {
        const div = document.createElement("div");
        div.className = "thinking-block";
        div.textContent = text;
        phase.body.appendChild(div);
        scrollToBottom();
        return;
      }

      const key = "live-" + speaker + "-" + role;
      let live = phase.live[key];
      if (!live) {
        const wrap = document.createElement("div");
        wrap.className = "utterance " + speaker + " streaming";
        const who = document.createElement("div");
        who.className = "who";
        who.textContent = speaker + (role ? " · " + role : "") + " · thinking…";
        const txt = document.createElement("div");
        txt.className = "text";
        wrap.appendChild(who); wrap.appendChild(txt);
        phase.body.appendChild(wrap);
        live = { wrap, who, txt, buffer: "" };
        phase.live[key] = live;
      }
      live.buffer += text;
      live.txt.textContent = live.buffer;
      scrollToBottom();
    }

    // Strip the blinking cursor + "thinking…" suffix from any live utterances
    // for the given speaker. Called when a tool_call interrupts the stream so
    // the prior reasoning bubble visibly settles before the command card.
    function closeLiveUtterances(phase, speaker) {
      if (!phase.live) return;
      for (const k of Object.keys(phase.live)) {
        if (!k.startsWith("live-" + speaker + "-")) continue;
        const live = phase.live[k];
        live.wrap.classList.remove("streaming");
        const parts = k.split("-"); // ["live", speaker, role...]
        const sp = parts[1] || "";
        const ro = parts.slice(2).join("-");
        live.who.textContent = sp + (ro ? " · " + ro : "");
        delete phase.live[k];
      }
    }

    function finalizeTurn(turn_id, payload) {
      const turn = turns[turn_id];
      if (!turn) return;

      // Mark unfinished phases as skipped (build_needed=false short-circuits past executing)
      for (const name of NODE_ORDER) {
        if (name === "human_fallback") continue;
        const phase = turn.phases[name];
        if (phase && phase.state === "pending") {
          setPhaseState(phase, "skipped", "not run");
        }
        if (phase && phase.state === "running") {
          // last running phase but no node_complete arrived — treat as done with no log
          setPhaseState(phase, "done", "");
        }
      }

      const halt = payload.halt_reason;
      const diffs = Array.isArray(payload.proposed_diffs) ? payload.proposed_diffs : [];
      const hash = payload.pre_execute_commit_hash;
      const hadExec = (turn.phases.executing && turn.phases.executing.state === "done");

      const sum = document.createElement("div");
      sum.className = "summary " + (halt ? "halted" : (hadExec ? "done" : "analysis"));
      const title = document.createElement("div");
      title.className = "title";
      title.textContent = halt
        ? "HALTED: " + halt
        : (hadExec ? "DONE" : "Analysis (no files changed)");
      sum.appendChild(title);

      if (diffs.length > 0) {
        const h = document.createElement("div");
        h.className = "small"; h.textContent = "Files touched (click to open):";
        sum.appendChild(h);
        const files = document.createElement("div");
        files.className = "files";
        const seen = new Set();
        for (const d of diffs) {
          const fp = String(d.file_path || "");
          if (!fp || seen.has(fp)) continue;
          seen.add(fp);
          const row = document.createElement("div");
          row.className = "file-row";
          row.textContent = fp;
          row.addEventListener("click", () => vscode.postMessage({ type: "open_file", path: fp }));
          files.appendChild(row);
        }
        sum.appendChild(files);
      }

      if (hash) {
        const h = document.createElement("div");
        h.className = "small"; h.style.marginTop = "6px";
        h.textContent = "Rollback (click to copy):";
        sum.appendChild(h);
        const cmd = document.createElement("div");
        cmd.className = "rollback";
        cmd.textContent = 'git reset --hard ' + String(hash);
        sum.appendChild(cmd);
      }

      turn.summarySlot.appendChild(sum);
      scrollToBottom();
    }

    // v0.6.3 — token/turn pacer. Subscription auth (Claude Pro + Gemini Ultra)
    // exposes ZERO rate-limit telemetry; the DR shows users hit 429 in 20min-1h
    // under dual-LLM 100k-context turns. This is a visual heuristic so you pace
    // before catastrophe. Tokens estimated at chars/4 from chunk text.
    // TYPICAL_WALL is empirical — starts at 10 turns and could be calibrated
    // from observed 429s in future work.
    const TYPICAL_WALL_TURNS = 10;
    const pacerBarsEl = document.getElementById("pacer-bars");
    const pacerTextEl = document.getElementById("pacer-text");
    let sessionTurns = 0;
    let sessionTokens = 0;
    let currentTurnTokens = 0;

    function renderPacer() {
      const frac = Math.min(sessionTurns / TYPICAL_WALL_TURNS, 1);
      const filled = Math.round(frac * 10);
      const bars = "▮".repeat(filled) + "▯".repeat(10 - filled);
      pacerBarsEl.textContent = bars;
      pacerBarsEl.className = frac < 0.5 ? "ok" : frac < 0.8 ? "warn" : "danger";
      const ktok = sessionTokens >= 1000 ? (sessionTokens / 1000).toFixed(1) + "k" : String(sessionTokens);
      pacerTextEl.textContent = "turn " + sessionTurns + "/~" + TYPICAL_WALL_TURNS + " · " + ktok + " tok";
    }
    renderPacer();

    // v0.6.2 — pasted-image queue. Each entry: { filename, data_base64, blobUrl }.
    // Blob URLs power the thumbnail tray; the base64 payload ships to /build.
    const imageTrayEl = document.getElementById("image-tray");
    let pendingImages = [];

    function renderImageTray() {
      imageTrayEl.innerHTML = "";
      if (pendingImages.length === 0) {
        imageTrayEl.classList.remove("has-images");
        return;
      }
      imageTrayEl.classList.add("has-images");
      pendingImages.forEach((img, idx) => {
        const thumb = document.createElement("div");
        thumb.className = "thumb";
        const i = document.createElement("img");
        i.src = img.blobUrl;
        i.alt = img.filename;
        const x = document.createElement("span");
        x.className = "x";
        x.textContent = "×";
        x.title = "Remove this attachment";
        x.addEventListener("click", () => {
          try { URL.revokeObjectURL(img.blobUrl); } catch {}
          pendingImages.splice(idx, 1);
          renderImageTray();
        });
        thumb.appendChild(i);
        thumb.appendChild(x);
        imageTrayEl.appendChild(thumb);
      });
    }

    // v0.6.6 Context Chips — @-mention queue. Each entry: { kind, path, symbol? }.
    // When user types '@' the extension host pops a QuickPick; the picked
    // mention lands here and renders as a chip.
    const chipTrayEl = document.getElementById("chip-tray");
    let pendingMentions = [];
    let pendingMentionRequests = {}; // requestId → {atIndex, queryStart} for stripping the '@...' trigger

    function renderChipTray() {
      chipTrayEl.innerHTML = "";
      if (pendingMentions.length === 0) {
        chipTrayEl.classList.remove("has-chips");
        return;
      }
      chipTrayEl.classList.add("has-chips");
      pendingMentions.forEach((m, idx) => {
        const chip = document.createElement("span");
        chip.className = "chip";
        const kind = document.createElement("span");
        kind.className = "kind";
        kind.textContent = m.kind === "folder" ? "📁" : (m.kind === "symbol" ? "ƒ" : "📄");
        const p = document.createElement("span");
        p.className = "path";
        p.textContent = m.path;
        p.title = m.path + (m.symbol ? " · " + m.symbol : "");
        const x = document.createElement("span");
        x.className = "x"; x.textContent = "×"; x.title = "Remove this mention";
        x.addEventListener("click", () => {
          pendingMentions.splice(idx, 1);
          renderChipTray();
        });
        chip.appendChild(kind);
        chip.appendChild(p);
        chip.appendChild(x);
        chipTrayEl.appendChild(chip);
      });
    }

    // Detect '@' typed as a word-start (preceded by whitespace or BOF) and
    // pop the mention picker. We don't try to do inline autocomplete — too
    // much CSS for v0.6.6. The QuickPick that pops is native VSCode.
    let lastInputValue = "";
    input.addEventListener("input", () => {
      const val = input.value;
      // Find any new '@' that wasn't there before AND is at a word-start.
      if (val.length > lastInputValue.length) {
        const caret = input.selectionStart ?? val.length;
        const justTyped = val.slice(Math.max(0, caret - 1), caret);
        if (justTyped === "@") {
          const before = caret >= 2 ? val[caret - 2] : "";
          const isWordStart = caret === 1 || /\s/.test(before);
          if (isWordStart) {
            const requestId = "mp-" + Date.now() + "-" + Math.floor(Math.random() * 1e6);
            pendingMentionRequests[requestId] = { atIndex: caret - 1 };
            vscode.postMessage({ type: "request_mention_picker", requestId });
          }
        }
      }
      lastInputValue = val;
    });

    async function blobToBase64(blob) {
      const buf = await blob.arrayBuffer();
      const bytes = new Uint8Array(buf);
      let bin = "";
      for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
      return btoa(bin);
    }

    input.addEventListener("paste", async (e) => {
      const items = e.clipboardData?.items || [];
      const imageItems = [];
      for (const item of items) {
        if (item.kind === "file" && item.type.startsWith("image/")) {
          const file = item.getAsFile();
          if (file) imageItems.push({ file, mime: item.type });
        }
      }
      if (imageItems.length === 0) return; // let the normal text paste happen
      e.preventDefault();
      for (const { file, mime } of imageItems) {
        try {
          const b64 = await blobToBase64(file);
          const ext = (mime.split("/")[1] || "png").replace(/[^a-z0-9]/gi, "") || "png";
          pendingImages.push({
            filename: "paste-" + Date.now() + "-" + pendingImages.length + "." + ext,
            data_base64: "data:" + mime + ";base64," + b64,
            blobUrl: URL.createObjectURL(file),
          });
        } catch (err) {
          console.error("[cothink] failed to encode pasted image:", err);
        }
      }
      renderImageTray();
    });

    function send() {
      const text = input.value.trim();
      // Allow image- or mention-only send (no text required).
      if (!text && pendingImages.length === 0 && pendingMentions.length === 0) return;
      if (text === "/reset") {
        log.innerHTML = "";
        vscode.postMessage({ type: "reset" });
        input.value = "";
        return;
      }
      // Snapshot + clear before postMessage so subsequent typing doesn't race.
      const payloadImages = pendingImages.map((p) => ({
        filename: p.filename,
        data_base64: p.data_base64,
      }));
      const payloadMentions = pendingMentions.slice();
      // Revoke object URLs (the base64 data is the canonical copy now).
      for (const p of pendingImages) {
        try { URL.revokeObjectURL(p.blobUrl); } catch {}
      }
      pendingImages = [];
      pendingMentions = [];
      renderImageTray();
      renderChipTray();
      input.value = "";
      lastInputValue = "";
      setBusy(true);
      vscode.postMessage({
        type: "send", text,
        images: payloadImages,
        mentions: payloadMentions,
      });
    }

    sendBtn.addEventListener("click", send);
    stopBtn.addEventListener("click", () => vscode.postMessage({ type: "stop" }));
    resetBtn.addEventListener("click", () => {
      log.innerHTML = "";
      vscode.postMessage({ type: "reset" });
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
    });

    window.addEventListener("message", (ev) => {
      const m = ev.data;
      if (!m || typeof m.type !== "string") return;
      switch (m.type) {
        case "status": {
          const h = m.health || "unknown";
          statusEl.textContent = h;
          statusEl.className = "badge " + (h === "connected" ? "ok" :
            (h === "stopped" || h === "unreachable" || String(h).startsWith("error")) ? "err" : "warn");
          break;
        }
        case "turn_started":
          addUserMsg(
            String(m.text || ""),
            Number(m.image_count) || 0,
            Number(m.mention_count) || 0,
          );
          startTurn(String(m.turn_id || ""));
          break;
        case "mention_picked": {
          // v0.6.6: extension host returned the user's QuickPick selection
          // (or cancelled). Strip the trigger '@' from the composer either way,
          // then add the chip if not cancelled.
          const requestId = String(m.requestId || "");
          const req = pendingMentionRequests[requestId];
          delete pendingMentionRequests[requestId];
          if (req && typeof req.atIndex === "number") {
            const v = input.value;
            const at = req.atIndex;
            // Strip just the '@' (the picker pops immediately on '@', so the
            // user hasn't typed a query). If they DID type more before the
            // picker resolved, strip from '@' to current caret.
            const caret = input.selectionStart ?? v.length;
            const newVal = v.slice(0, at) + v.slice(Math.max(at + 1, caret));
            input.value = newVal;
            lastInputValue = newVal;
            try { input.setSelectionRange(at, at); } catch {}
          }
          if (m.cancelled) break;
          const picked = m.mention;
          if (!picked || !picked.kind || !picked.path) break;
          pendingMentions.push({
            kind: String(picked.kind),
            path: String(picked.path),
            ...(picked.symbol ? { symbol: String(picked.symbol) } : {}),
          });
          renderChipTray();
          input.focus();
          break;
        }
        case "images_attached": {
          // Server confirmed the downsampled paths. Already shown to user via
          // the "📎 N image(s)" line on the user-msg card; no further DOM
          // work needed unless we want to expose the saved paths later.
          break;
        }
        case "thinking_chunk": {
          const tid = String(m.turn_id || "");
          const turn = turns[tid];
          if (!turn) break;
          const node = String(m.node || "");
          const phase = turn.phases[node];
          if (!phase) break;
          // v0.6.3: token estimate (chars/4 industry proxy). Bumps the
          // pacer LIVE so user sees usage growing during the run.
          const t = String(m.text || "");
          if (t) {
            currentTurnTokens += Math.ceil(t.length / 4);
            sessionTokens += Math.ceil(t.length / 4);
            renderPacer();
          }
          // If this chunk arrives for a phase still marked pending (e.g. the
          // graph entered Planning while Discovery's running marker hadn't
          // shifted), promote it to running so the user sees activity.
          if (phase.state === "pending") {
            setPhaseState(phase, "running", "running…");
          }
          appendChunk(phase, String(m.speaker || "team").toLowerCase(),
                      String(m.role || ""), t);
          break;
        }
        case "node_complete": {
          const tid = String(m.turn_id || "");
          const turn = turns[tid];
          if (!turn) break;
          const name = String(m.node_name || "");
          const phase = turn.phases[name];
          if (!phase) break;
          const log = m.log === null || m.log === undefined ? "" : String(m.log);
          const halt = m.halt_reason ? String(m.halt_reason) : "";
          setPhaseState(phase, halt ? "fail" : "done", log + (halt ? "  · " + halt : ""));
          // Stop the blinking cursor on any live-streaming utterances in this phase
          // and update their header to drop the "thinking…" suffix.
          if (phase.live) {
            for (const k of Object.keys(phase.live)) {
              const live = phase.live[k];
              live.wrap.classList.remove("streaming");
              const parts = k.split("-"); // ["live", speaker, role...]
              const sp = parts[1] || "";
              const ro = parts.slice(2).join("-");
              live.who.textContent = sp + (ro ? " · " + ro : "");
            }
          }
          appendDialogue(phase, m.dialogue_delta);
          // Move the next phase to running
          let crossed = false;
          for (const n of NODE_ORDER) {
            if (n === name) { crossed = true; continue; }
            if (crossed && turn.phases[n] && turn.phases[n].state === "pending") {
              setPhaseState(turn.phases[n], "running", "running…");
              break;
            }
          }
          break;
        }
        case "turn_done":
          finalizeTurn(String(m.turn_id || ""), {
            halt_reason: m.halt_reason,
            pre_execute_commit_hash: m.pre_execute_commit_hash,
            proposed_diffs: m.proposed_diffs,
            design_contract: m.design_contract,
          });
          sessionTurns += 1;
          currentTurnTokens = 0;
          renderPacer();
          setBusy(false);
          break;
        case "error":
          addSystem("error: " + String(m.reason || "unknown"), true);
          setBusy(false);
          break;
        case "reset_ack":
          addSystem("session reset");
          sessionTurns = 0;
          sessionTokens = 0;
          currentTurnTokens = 0;
          renderPacer();
          break;
        case "session_loaded": {
          const entries = Array.isArray(m.entries) ? m.entries : [];
          for (const e of entries) {
            if (!e || typeof e !== "object") continue;
            if (e.role === "user") {
              addUserMsg(String(e.content || ""));
            } else if (e.role === "assistant") {
              // Render a compact "prior turn" summary card
              const sum = document.createElement("div");
              sum.className = "summary " + (e.halt_reason ? "halted" : (Array.isArray(e.proposed_diffs) && e.proposed_diffs.length > 0 ? "done" : "analysis"));
              const title = document.createElement("div");
              title.className = "title";
              title.textContent = e.halt_reason ? "HALTED · prior: " + String(e.halt_reason)
                : (Array.isArray(e.proposed_diffs) && e.proposed_diffs.length > 0 ? "DONE · prior" : "Analysis · prior");
              sum.appendChild(title);
              const diffs = Array.isArray(e.proposed_diffs) ? e.proposed_diffs : [];
              if (diffs.length) {
                const fl = document.createElement("div");
                fl.className = "small"; fl.textContent = "Files (prior):";
                sum.appendChild(fl);
                const files = document.createElement("div");
                files.className = "files";
                const seen = new Set();
                for (const d of diffs) {
                  const fp = String(d.file_path || "");
                  if (!fp || seen.has(fp)) continue;
                  seen.add(fp);
                  const row = document.createElement("div");
                  row.className = "file-row";
                  row.textContent = fp;
                  row.addEventListener("click", () => vscode.postMessage({ type: "open_file", path: fp }));
                  files.appendChild(row);
                }
                sum.appendChild(files);
              }
              log.appendChild(sum);
            }
          }
          if (entries.length) addSystem("— prior session loaded (" + entries.length + " entries) —");
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
