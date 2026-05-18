import * as vscode from "vscode";
import { CothinkServer } from "./server";
import { WorkbenchPanelProvider } from "./views/workbenchPanel";

let server: CothinkServer | undefined;
let workbenchProvider: WorkbenchPanelProvider | undefined;
let composerEditorPanel: vscode.WebviewPanel | undefined;

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  server = new CothinkServer(context);

  context.subscriptions.push(
    vscode.commands.registerCommand("cothink.startServer", async () => {
      await server?.start();
    }),
    vscode.commands.registerCommand("cothink.stopServer", async () => {
      await server?.stop();
    }),
    vscode.commands.registerCommand("cothink.restartServer", async () => {
      await server?.stop();
      await server?.start();
    }),
    vscode.commands.registerCommand("cothink.setApiKey", async () => {
      // Prompt for whichever keys the sidecar reported as missing in its
      // last /health probe.  Empty input cancels for that key (without
      // overwriting any existing stored value).
      const missing = server?.missingApiKeys.length
        ? server.missingApiKeys
        : ["GEMINI_API_KEY"];
      for (const keyName of missing) {
        const value = await vscode.window.showInputBox({
          title: `cothink — set ${keyName}`,
          prompt:
            keyName === "GEMINI_API_KEY"
              ? "Get a key at https://aistudio.google.com/app/apikey"
              : `Paste your ${keyName}`,
          password: true,
          ignoreFocusOut: true,
          placeHolder: "Stored in VSCode's secret storage; never logged.",
        });
        if (value === undefined || value === "") {
          vscode.window.showWarningMessage(
            `cothink: ${keyName} not set. /build and /chat will continue to return 503.`,
          );
          return;
        }
        const secretKey =
          keyName === "GEMINI_API_KEY"
            ? "cothink.geminiApiKey"
            : "cothink.anthropicApiKey";
        await context.secrets.store(secretKey, value);
      }
      // Restart so the sidecar's _load_env() picks up the new env on boot.
      await server?.stop();
      await server?.start();
    }),
    vscode.commands.registerCommand("cothink.openComposer", () => {
      // v0.6: open the composer in the editor area (Cursor-shape default).
      // If a panel is already open, reveal it instead of creating a duplicate.
      if (composerEditorPanel) {
        composerEditorPanel.reveal(vscode.ViewColumn.One);
        return;
      }
      composerEditorPanel = vscode.window.createWebviewPanel(
        "cothink.composer",
        "cothink",
        vscode.ViewColumn.One,
        {
          enableScripts: true,
          // Keep the webview's state alive when hidden behind another tab —
          // otherwise the conversation history wipes on every focus switch.
          retainContextWhenHidden: true,
        },
      );
      composerEditorPanel.onDidDispose(() => {
        composerEditorPanel = undefined;
      });
      workbenchProvider?.bindEditorPanel(composerEditorPanel);
    }),
  );

  const getPort = (): number =>
    vscode.workspace.getConfiguration("cothink").get<number>("serverPort", 8765);

  workbenchProvider = new WorkbenchPanelProvider(
    context,
    () => server?.healthState,
    getPort,
  );
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(
      "cothink.workbenchView",
      workbenchProvider,
    ),
  );

  // v0.6 — Cursor-shape first-run layout: hide VSCode chrome, apply cothink
  // theme, open the composer in the editor area.  Runs ONCE per install
  // (gated by globalState flag).  User can re-toggle anything via Ctrl+,.
  await applyFirstRunLayoutOnce(context);

  await server.start();
}

/** First-run layout: only fires on the very first activation per install.
 *  After this runs, the extension is dormant about defaults — user is free
 *  to re-show the activity bar, switch themes, etc., and we won't override. */
async function applyFirstRunLayoutOnce(
  context: vscode.ExtensionContext,
): Promise<void> {
  const FIRST_RUN_KEY = "cothink.firstRun.completed";
  if (context.globalState.get<boolean>(FIRST_RUN_KEY)) return;

  const cfg = vscode.workspace.getConfiguration();
  // Hide VSCode's default welcome page so the next launch goes straight to
  // the composer.
  await cfg.update(
    "workbench.startupEditor",
    "none",
    vscode.ConfigurationTarget.Global,
  );
  // Tuck the activity bar out of sight; Ctrl+Shift+B (added later) toggles
  // it back when the user wants the file explorer.
  await cfg.update(
    "workbench.activityBar.location",
    "hidden",
    vscode.ConfigurationTarget.Global,
  );
  // cothink-branded dark theme as the default.
  await cfg.update(
    "workbench.colorTheme",
    "cothink Dark",
    vscode.ConfigurationTarget.Global,
  );

  // Close any auto-opened welcome editors so the composer takes the whole
  // editor area cleanly.
  try {
    await vscode.commands.executeCommand("workbench.action.closeAllEditors");
  } catch {
    // ignore
  }
  // Hide the sidebar visually (workbench.sideBar.visible isn't a real
  // settings key on most VSCode versions; the imperative command is more
  // reliable).
  try {
    await vscode.commands.executeCommand(
      "workbench.action.closeSidebar",
    );
  } catch {
    // ignore
  }
  // Open the composer in the editor area.
  await vscode.commands.executeCommand("cothink.openComposer");

  await context.globalState.update(FIRST_RUN_KEY, true);
}

export async function deactivate(): Promise<void> {
  await server?.stop();
}
