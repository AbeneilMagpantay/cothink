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
 *
 *  v0.8: the heavy lifting is now in the FORK at the CSS level — chrome
 *  (menubar, tab strip, sidebar, panel, auxiliary, breadcrumbs) is hidden
 *  via display:none injected into vscode/src/vs/workbench/browser/style.css
 *  by prepare_vscode.sh.  No flicker, no race, no retry loop.
 *
 *  This extension flow remains as a small "open the composer in editor
 *  area on first launch" trigger so users see the chat immediately.  Plus
 *  belt-and-suspenders config.update for users whose profiles predate the
 *  fork's configurationDefaults.
 */
async function applyFirstRunLayoutOnce(
  context: vscode.ExtensionContext,
): Promise<void> {
  const FIRST_RUN_KEY = "cothink.firstRun.v0_8.completed";
  if (context.globalState.get<boolean>(FIRST_RUN_KEY)) return;

  // Belt-and-suspenders: same keys as product.json configurationDefaults
  // applied at the user-settings layer for profiles that predate v0.8.
  // (Fresh installs of v0.8 get these via configurationDefaults; this
  //  covers users coming from v0.5/v0.6/v0.7 who already have a profile.)
  const cfg = vscode.workspace.getConfiguration();
  await cfg.update("workbench.startupEditor", "none", vscode.ConfigurationTarget.Global);
  await cfg.update("workbench.activityBar.location", "hidden", vscode.ConfigurationTarget.Global);
  await cfg.update("workbench.editor.showTabs", "none", vscode.ConfigurationTarget.Global);
  await cfg.update("workbench.colorTheme", "cothink Dark", vscode.ConfigurationTarget.Global);

  // Open the composer in the editor area on first launch — this is the
  // user's "AI IDE first impression."  The CSS does the rest.
  try {
    await vscode.commands.executeCommand("cothink.openComposer");
  } catch {
    // ignore
  }

  await context.globalState.update(FIRST_RUN_KEY, true);
}

export async function deactivate(): Promise<void> {
  await server?.stop();
}
