import * as cp from "child_process";
import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";

const HEALTH_TIMEOUT_MS = 15_000;
const HEALTH_POLL_INTERVAL_MS = 500;

export type HealthState =
  | "starting"
  | "connected"
  | "needs api key"
  | "stopped"
  | "unreachable"
  | "error: no python"
  | "error: no cothink root"
  | "error: no sidecar";

/** Owns the Python `cothink.server` subprocess: spawns it, polls /health
 *  until live, surfaces state via a status-bar indicator, and tears it
 *  down on stop / deactivation. No HTTP traffic flows through this class
 *  beyond the /health probe; panels do their own HTTP via the webview
 *  message bridge in later steps. */
export class CothinkServer {
  private process: cp.ChildProcess | undefined;
  private statusBar: vscode.StatusBarItem;
  public healthState: HealthState = "stopped";
  /** Track which keys were reported as missing by /health so the
   *  setApiKey command knows which to prompt for. */
  public missingApiKeys: string[] = [];
  private readonly context: vscode.ExtensionContext;

  constructor(context: vscode.ExtensionContext) {
    this.context = context;
    this.statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    this.statusBar.command = "cothink.restartServer";
    this.statusBar.tooltip = "Click to restart the cothink server";
    this.statusBar.show();
    context.subscriptions.push(this.statusBar);
    this.setState("stopped");
  }

  async start(): Promise<void> {
    if (this.process) {
      vscode.window.showInformationMessage("cothink server already running.");
      return;
    }

    const config = vscode.workspace.getConfiguration("cothink");
    const port = config.get<number>("serverPort", 8765);
    this.setState("starting");

    // Pull stored API keys from context.secrets and merge into the child
    // process env so the sidecar's _load_env() picks them up.  setApiKey
    // command stores keys here when the user supplies them.
    const spawnEnv = await this.buildSpawnEnv();

    // Bundled-mode first: the cothink fork ships a standalone cothink-serve
    // binary at process.resourcesPath via electron-builder extraResources.
    // When that exists, prefer it over the dev-mode Python-interpreter path
    // — no venv, no Python install, no pyproject discovery needed.
    const sidecar = this.resolveSidecarBinary(config);
    if (sidecar) {
      this.process = cp.spawn(
        sidecar,
        ["--host", "127.0.0.1", "--port", String(port)],
        {
          stdio: ["ignore", "pipe", "pipe"],
          env: spawnEnv,
          windowsHide: true,
        },
      );
    } else {
      // Dev mode: sideload into Antigravity / vanilla VSCode against the
      // user's local Python venv. Required when the cothink-build fork
      // isn't packaging the binary yet (today, anything before v0.5).
      const cothinkRoot = this.resolveCothinkRoot(config);
      if (!cothinkRoot) {
        this.setState("error: no cothink root");
        vscode.window.showErrorMessage(
          "cothink: cannot find the cothink Python project root. " +
            "Set `cothink.cothinkRoot` in settings, or open a workspace folder containing pyproject.toml (or a /cothink subfolder containing it).",
        );
        return;
      }

      const pythonPath = this.resolvePythonPath(config, cothinkRoot);
      if (!pythonPath || !fs.existsSync(pythonPath)) {
        this.setState("error: no python");
        vscode.window.showErrorMessage(
          `cothink: Python executable not found. Looked at ${pythonPath || "(unset)"}. ` +
            "Set `cothink.pythonPath`, or ensure `.venv` exists at the cothink root.",
        );
        return;
      }

      this.process = cp.spawn(
        pythonPath,
        ["-m", "cothink.server", "--host", "127.0.0.1", "--port", String(port)],
        {
          cwd: cothinkRoot,
          stdio: ["ignore", "pipe", "pipe"],
          env: spawnEnv,
          windowsHide: true,
        },
      );
    }

    this.process.stdout?.on("data", (chunk: Buffer) =>
      console.log("[cothink stdout]", chunk.toString().trimEnd()),
    );
    this.process.stderr?.on("data", (chunk: Buffer) =>
      console.log("[cothink stderr]", chunk.toString().trimEnd()),
    );
    this.process.on("exit", (code, signal) => {
      console.log(`[cothink] server exited (code=${code}, signal=${signal})`);
      this.process = undefined;
      if (this.healthState !== "stopped") {
        this.setState("unreachable");
      }
    });
    this.process.on("error", (err) => {
      console.error("[cothink] spawn error", err);
      this.setState("unreachable");
    });

    const probe = await this.waitForHealthy(port);
    if (!probe.reachable) {
      this.setState("unreachable");
      vscode.window.showErrorMessage(
        `cothink: server failed to respond on /health within ${HEALTH_TIMEOUT_MS / 1000}s. ` +
          "Check the developer console (Help → Toggle Developer Tools) for stdout/stderr.",
      );
      return;
    }
    if (probe.ready) {
      this.missingApiKeys = [];
      this.setState("connected");
    } else {
      this.missingApiKeys = probe.missingApiKeys;
      this.setState("needs api key");
    }
  }

  async stop(): Promise<void> {
    if (!this.process) {
      this.setState("stopped");
      return;
    }
    const proc = this.process;
    const exited = new Promise<void>((resolve) => proc.once("exit", () => resolve()));
    try {
      proc.kill("SIGTERM");
    } catch {
      // already dead
    }
    await Promise.race([
      exited,
      new Promise<void>((resolve) => setTimeout(resolve, 2000)),
    ]);
    if (this.process) {
      try {
        this.process.kill("SIGKILL");
      } catch {
        // ignore
      }
      this.process = undefined;
    }
    this.setState("stopped");
  }

  private setState(state: HealthState): void {
    this.healthState = state;
    switch (state) {
      case "connected":
        this.statusBar.text = "$(check) cothink";
        this.statusBar.command = "cothink.restartServer";
        this.statusBar.tooltip = "Click to restart the cothink server";
        break;
      case "needs api key":
        this.statusBar.text = "$(warning) cothink: set API key";
        this.statusBar.command = "cothink.setApiKey";
        this.statusBar.tooltip = `Click to set ${this.missingApiKeys.join(", ")}`;
        break;
      case "starting":
        this.statusBar.text = "$(loading~spin) cothink: starting";
        this.statusBar.command = "cothink.restartServer";
        break;
      case "stopped":
        this.statusBar.text = "$(circle-slash) cothink: stopped";
        this.statusBar.command = "cothink.restartServer";
        break;
      case "unreachable":
        this.statusBar.text = "$(error) cothink: unreachable";
        this.statusBar.command = "cothink.restartServer";
        break;
      default:
        this.statusBar.text = `$(error) cothink: ${state.replace("error: ", "")}`;
        this.statusBar.command = "cothink.restartServer";
        break;
    }
  }

  /** Merge stored API-key secrets into a copy of process.env so the spawned
   *  sidecar reads them via _load_env().  We deliberately remove
   *  ELECTRON_RUN_AS_NODE — when the extension host runs inside the cothink
   *  Electron app, that var is set, and inheriting it makes cothink-serve
   *  boot as a Node interpreter (no FastAPI, no /health). */
  private async buildSpawnEnv(): Promise<NodeJS.ProcessEnv> {
    const env: NodeJS.ProcessEnv = { ...process.env };
    delete env.ELECTRON_RUN_AS_NODE;
    const gemini = await this.context.secrets.get("cothink.geminiApiKey");
    if (gemini) {
      env.GEMINI_API_KEY = gemini;
    }
    const anthropic = await this.context.secrets.get("cothink.anthropicApiKey");
    if (anthropic) {
      env.ANTHROPIC_API_KEY = anthropic;
    }
    return env;
  }

  /** Resolve the standalone cothink-serve binary if the cothink fork bundled it.
   *  Returns undefined in dev mode (Antigravity-sideload, vanilla VSCode), causing
   *  start() to fall back to the Python-interpreter path. */
  private resolveSidecarBinary(config: vscode.WorkspaceConfiguration): string | undefined {
    // Explicit override always wins (lets a dev point at a hand-built binary).
    const configured = config.get<string>("sidecarPath", "");
    if (configured && fs.existsSync(configured)) return configured;

    // electron-builder's extraResources lands files at process.resourcesPath
    // (e.g. <app>/resources/cothink-serve.exe in the packaged cothink app).
    // In sideload-into-Antigravity mode, process.resourcesPath points at
    // Antigravity's own resources dir, where cothink-serve.exe doesn't exist
    // — so we return undefined and fall back to the Python path.
    //
    // resourcesPath is injected by Electron at runtime and not in @types/node;
    // cast through unknown to access it without polluting global types.
    const resourcesPath = (process as unknown as { resourcesPath?: string })
      .resourcesPath;
    if (!resourcesPath) return undefined;
    const ext = process.platform === "win32" ? ".exe" : "";
    const bundled = path.join(resourcesPath, `cothink-serve${ext}`);
    if (fs.existsSync(bundled)) return bundled;
    return undefined;
  }

  /** Locate the cothink Python project (where pyproject.toml lives). */
  private resolveCothinkRoot(config: vscode.WorkspaceConfiguration): string {
    const configured = config.get<string>("cothinkRoot", "");
    if (configured && fs.existsSync(path.join(configured, "pyproject.toml"))) {
      return configured;
    }
    const ws = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    if (ws) {
      if (fs.existsSync(path.join(ws, "pyproject.toml"))) return ws;
      const nested = path.join(ws, "cothink");
      if (fs.existsSync(path.join(nested, "pyproject.toml"))) return nested;
    }
    return "";
  }

  /** Locate the Python executable in the cothink venv. */
  private resolvePythonPath(config: vscode.WorkspaceConfiguration, root: string): string {
    const configured = config.get<string>("pythonPath", "");
    if (configured) return configured;
    const winCandidate = path.join(root, ".venv", "Scripts", "python.exe");
    if (fs.existsSync(winCandidate)) return winCandidate;
    const unixCandidate = path.join(root, ".venv", "bin", "python");
    if (fs.existsSync(unixCandidate)) return unixCandidate;
    return "";
  }

  private async waitForHealthy(
    port: number,
  ): Promise<{ reachable: boolean; ready: boolean; missingApiKeys: string[] }> {
    const deadline = Date.now() + HEALTH_TIMEOUT_MS;
    while (Date.now() < deadline) {
      try {
        const resp = await fetch(`http://127.0.0.1:${port}/health`);
        if (resp.ok) {
          const body = (await resp.json()) as {
            ok?: boolean;
            ready?: boolean;
            missing_api_keys?: string[];
          };
          return {
            reachable: true,
            ready: body.ready === true,
            missingApiKeys: body.missing_api_keys ?? [],
          };
        }
      } catch {
        // server still booting; ignore and retry
      }
      await new Promise((r) => setTimeout(r, HEALTH_POLL_INTERVAL_MS));
    }
    return { reachable: false, ready: false, missingApiKeys: [] };
  }
}
