import * as vscode from "vscode";
import { CothinkServer } from "./server";
import { WorkbenchPanelProvider } from "./views/workbenchPanel";

let server: CothinkServer | undefined;

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
  );

  const getPort = (): number =>
    vscode.workspace.getConfiguration("cothink").get<number>("serverPort", 8765);

  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(
      "cothink.workbenchView",
      new WorkbenchPanelProvider(context, () => server?.healthState, getPort),
    ),
  );

  await server.start();
}

export async function deactivate(): Promise<void> {
  await server?.stop();
}
