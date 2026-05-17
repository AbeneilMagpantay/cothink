// Package tui — SSE client for cothink's FastAPI backend.
//
// 1:1 conceptual port of extension/src/sseClient.ts. Streams events from
// POST /build (and any other SSE endpoint), invokes a callback per event.
// The Go TUI consumes thinking_chunk / node_complete / images_attached /
// turn_id / done / error events to drive Bubble Tea state updates.
package tui

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
)

// SSEEvent is one decoded `event: ... \n data: ... \n\n` block.
type SSEEvent struct {
	Event string
	Data  map[string]any
	// Raw text when JSON parsing fails. Useful for debugging server-side
	// regressions without crashing the TUI.
	Raw string
}

// SSEOptions configures one SSE call. Body is JSON-marshaled if non-nil.
type SSEOptions struct {
	Method string // default "POST"
	Body   any
}

// StreamSSE opens an SSE connection and invokes onEvent for each parsed
// event block. Returns when the server closes the stream, the context is
// cancelled, or an unrecoverable read error occurs.
//
// Mirrors extension/src/sseClient.ts:streamSSE almost line for line.
func StreamSSE(
	ctx context.Context,
	url string,
	opts SSEOptions,
	onEvent func(SSEEvent),
) error {
	method := opts.Method
	if method == "" {
		method = "POST"
	}

	var body io.Reader
	if opts.Body != nil {
		buf, err := json.Marshal(opts.Body)
		if err != nil {
			return fmt.Errorf("marshal body: %w", err)
		}
		body = bytes.NewReader(buf)
	}

	req, err := http.NewRequestWithContext(ctx, method, url, body)
	if err != nil {
		return fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("content-type", "application/json")
	req.Header.Set("accept", "text/event-stream")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return fmt.Errorf("http: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 300 {
		detail, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("SSE request failed: %d %s: %s",
			resp.StatusCode, resp.Status, string(detail))
	}

	reader := bufio.NewReader(resp.Body)
	var block strings.Builder
	for {
		line, err := reader.ReadString('\n')
		if err != nil {
			if err == io.EOF {
				// Drain any final pending block.
				if block.Len() > 0 {
					if ev, ok := parseEventBlock(block.String()); ok {
						onEvent(ev)
					}
				}
				return nil
			}
			return fmt.Errorf("read: %w", err)
		}
		line = strings.TrimRight(line, "\r\n")
		if line == "" {
			if block.Len() > 0 {
				if ev, ok := parseEventBlock(block.String()); ok {
					onEvent(ev)
				}
				block.Reset()
			}
			continue
		}
		block.WriteString(line)
		block.WriteByte('\n')
	}
}

// parseEventBlock takes a multi-line `event: ...\n data: ...` block and
// returns one SSEEvent. Skips empty / comment-only blocks.
func parseEventBlock(raw string) (SSEEvent, bool) {
	ev := SSEEvent{Event: "message"}
	var dataLines []string
	for _, line := range strings.Split(raw, "\n") {
		if line == "" || strings.HasPrefix(line, ":") {
			continue
		}
		if strings.HasPrefix(line, "event:") {
			ev.Event = strings.TrimSpace(line[len("event:"):])
		} else if strings.HasPrefix(line, "data:") {
			dataLines = append(dataLines, strings.TrimSpace(line[len("data:"):]))
		}
	}
	if len(dataLines) == 0 {
		return SSEEvent{}, false
	}
	joined := strings.Join(dataLines, "\n")
	ev.Raw = joined
	var decoded map[string]any
	if err := json.Unmarshal([]byte(joined), &decoded); err == nil {
		ev.Data = decoded
	}
	return ev, true
}
