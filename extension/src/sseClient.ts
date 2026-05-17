/**
 * Minimal Server-Sent Events client for the cothink HTTP bridge.
 *
 * Node 20+ (and therefore VSCode/Electron) ships `fetch` with native
 * ReadableStream support, so we don't need a third-party SSE library —
 * we just consume the response body line by line, split on the SSE
 * "\n\n" event boundary, and parse `event:` / `data:` lines.
 */

export interface SSEEvent {
  event: string;
  data: unknown;
}

export interface StreamSSEOptions {
  method?: "POST" | "GET";
  headers?: Record<string, string>;
  body?: unknown;
  signal?: AbortSignal;
}

export async function streamSSE(
  url: string,
  opts: StreamSSEOptions,
  onEvent: (event: SSEEvent) => void,
): Promise<void> {
  const init: RequestInit = {
    method: opts.method ?? "POST",
    headers: {
      "content-type": "application/json",
      accept: "text/event-stream",
      ...(opts.headers ?? {}),
    },
    signal: opts.signal,
  };
  if (opts.body !== undefined) {
    init.body = JSON.stringify(opts.body);
  }

  const resp = await fetch(url, init);
  if (!resp.ok || !resp.body) {
    const detail = await resp.text().catch(() => "");
    throw new Error(`SSE request failed: ${resp.status} ${resp.statusText} ${detail}`);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // Each SSE event is terminated by an empty line (\n\n). Spec also
    // allows \r\n\r\n; normalize.
    buffer = buffer.replace(/\r\n/g, "\n");
    let boundary: number;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const rawEvent = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const parsed = parseEventBlock(rawEvent);
      if (parsed) onEvent(parsed);
    }
  }
}

function parseEventBlock(block: string): SSEEvent | null {
  let eventName = "message";
  let dataLines: string[] = [];
  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue; // empty or comment
    if (line.startsWith("event:")) {
      eventName = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trim());
    }
  }
  if (dataLines.length === 0) return null;
  const dataText = dataLines.join("\n");
  let data: unknown = dataText;
  try {
    data = JSON.parse(dataText);
  } catch {
    // leave as string
  }
  return { event: eventName, data };
}
