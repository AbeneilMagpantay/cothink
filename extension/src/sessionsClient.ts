/**
 * Fetch wrappers for cothink's v0.6 session endpoints.
 *
 * All endpoints are JSON request/response (no SSE) — sessions are a metadata
 * surface, not a streaming surface. The /build endpoint stays in sseClient.ts;
 * it now accepts an optional session_id so each build turn appends into the
 * named session's JSONL.
 */

const BASE_URL = (port: number): string => `http://127.0.0.1:${port}`;

export interface SessionMeta {
  session_id: string;
  name: string;
  created_at: string;
  last_active: string;
  message_count: number;
  user_message_count: number;
  forked_from: string | null;
}

export interface SessionEntry {
  type?: "session_header";
  role?: "user" | "assistant";
  content?: string;
  turn_id?: string;
  halt_reason?: string | null;
  pre_execute_commit_hash?: string | null;
  proposed_diffs?: Array<{ file_path?: string; contract_bullet_quoted?: string }>;
  design_contract?: string[];
  // header-only fields
  session_id?: string;
  name?: string;
  created_at?: string;
  forked_from?: string;
}

async function jfetch<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, {
    headers: { "content-type": "application/json" },
    ...init,
  });
  if (!resp.ok) {
    const detail = await resp.text().catch(() => "");
    throw new Error(`${resp.status} ${resp.statusText}: ${detail || url}`);
  }
  return (await resp.json()) as T;
}

export async function listSessions(
  port: number,
  projectDir: string,
): Promise<SessionMeta[]> {
  const url = `${BASE_URL(port)}/sessions?project_dir=${encodeURIComponent(projectDir)}`;
  const { sessions } = await jfetch<{ sessions: SessionMeta[] }>(url);
  return sessions;
}

export async function createSession(
  port: number,
  projectDir: string,
  name?: string,
): Promise<string> {
  const url = `${BASE_URL(port)}/sessions`;
  const body = JSON.stringify({ project_dir: projectDir, name: name ?? null });
  const { session_id } = await jfetch<{ session_id: string }>(url, {
    method: "POST",
    body,
  });
  return session_id;
}

export async function getSession(
  port: number,
  projectDir: string,
  sessionId: string,
): Promise<SessionEntry[]> {
  const url = `${BASE_URL(port)}/sessions/${encodeURIComponent(sessionId)}?project_dir=${encodeURIComponent(projectDir)}`;
  const { entries } = await jfetch<{ entries: SessionEntry[] }>(url);
  return entries;
}

export async function renameSession(
  port: number,
  projectDir: string,
  sessionId: string,
  newName: string,
): Promise<void> {
  const url = `${BASE_URL(port)}/sessions/${encodeURIComponent(sessionId)}`;
  await jfetch(url, {
    method: "PATCH",
    body: JSON.stringify({ project_dir: projectDir, name: newName }),
  });
}

export async function deleteSession(
  port: number,
  projectDir: string,
  sessionId: string,
): Promise<void> {
  const url = `${BASE_URL(port)}/sessions/${encodeURIComponent(sessionId)}?project_dir=${encodeURIComponent(projectDir)}`;
  await jfetch(url, { method: "DELETE" });
}

export async function forkSession(
  port: number,
  projectDir: string,
  sessionId: string,
  pivotTurnId: string | null,
  newName?: string,
): Promise<string> {
  const url = `${BASE_URL(port)}/sessions/${encodeURIComponent(sessionId)}/fork`;
  const body = JSON.stringify({
    project_dir: projectDir,
    pivot_turn_id: pivotTurnId,
    new_name: newName ?? null,
  });
  const { session_id } = await jfetch<{ session_id: string }>(url, {
    method: "POST",
    body,
  });
  return session_id;
}

export type RewindMode = "code" | "conversation" | "both";

export interface RewindResult {
  rewound: boolean;
  mode: RewindMode;
  git_reset_done: boolean;
  git_error: string | null;
  kept_messages: number;
}

export async function rewindSession(
  port: number,
  projectDir: string,
  sessionId: string,
  targetTurnId: string,
  mode: RewindMode,
): Promise<RewindResult> {
  const url = `${BASE_URL(port)}/sessions/${encodeURIComponent(sessionId)}/rewind`;
  const body = JSON.stringify({
    project_dir: projectDir,
    target_turn_id: targetTurnId,
    mode,
  });
  return jfetch<RewindResult>(url, { method: "POST", body });
}
