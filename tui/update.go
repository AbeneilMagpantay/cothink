// Package tui — Bubble Tea Update function.
//
// Receives tea.Msg events (keypresses, SSE chunks, window resize, health polls)
// and returns the updated Model + any commands to fire (new SSE streams,
// timers, etc.). 1:1 conceptual port of extension/src/views/workbenchPanel.ts
// message handler.
package tui

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
)

// Custom message types for SSE events + health polling.
type (
	// healthMsg fires every 2s to refresh the connected/unreachable badge.
	healthTickMsg struct{}
	healthMsg     struct{ State string }

	// sseStartedMsg is emitted when a new /build stream begins.
	sseStartedMsg struct {
		TurnID string
	}

	// sseEventMsg is one event from streamSSE (already parsed).
	sseEventMsg struct {
		TurnID string
		Event  SSEEvent
	}

	// sseDoneMsg signals the stream finished cleanly (server emitted `done`
	// or closed). The Bubble Tea Update transitions Model.Busy=false.
	sseDoneMsg struct {
		TurnID string
	}

	// sseErrorMsg surfaces a stream error to the conversation log.
	sseErrorMsg struct {
		TurnID string
		Reason string
	}
)

// Init returns the initial command(s) Bubble Tea fires on program start.
// We kick off the first health poll immediately and schedule recurring ones.
func (m Model) Init() tea.Cmd {
	return tea.Batch(
		textinputCmd(),
		pollHealth(m.ServerPort),
		tickHealth(),
	)
}

// Update is the main reducer. Returns the new Model + an optional Cmd.
func (m Model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {

	case tea.WindowSizeMsg:
		m.Width = msg.Width
		m.Height = msg.Height
		// Reserve 4 lines for header + composer; the viewport gets the rest.
		composerH := 3
		headerH := 2
		m.Viewport.Width = msg.Width
		m.Viewport.Height = msg.Height - composerH - headerH - 2
		m.Composer.SetWidth(msg.Width - 4)
		return m, nil

	case tea.KeyMsg:
		switch msg.String() {
		case "ctrl+c":
			if m.Busy && m.Cancel != nil {
				// First Ctrl+C cancels the stream; second exits.
				m.Cancel()
				m.Busy = false
				return m, nil
			}
			return m, tea.Quit
		case "esc":
			if m.Busy && m.Cancel != nil {
				m.Cancel()
				m.Busy = false
			}
			return m, nil
		case "enter":
			// Shift+Enter inserts newline; bare Enter sends.
			if msg.Alt {
				// Alt+Enter as newline alternative (Shift detection in TTYs is
				// flaky). Falls through to default composer handling.
				break
			}
			if !m.Busy {
				return sendTurn(m)
			}
			return m, nil
		}

	case healthTickMsg:
		return m, tea.Batch(pollHealth(m.ServerPort), tickHealth())

	case healthMsg:
		m.Health = msg.State
		return m, nil

	case sseStartedMsg:
		t := newTurn(msg.TurnID, m.Composer.Value(), 0)
		m.Turns = append(m.Turns, t)
		m.TurnIDMap[msg.TurnID] = t
		m.Composer.Reset()
		m.refreshViewport()
		return m, nil

	case sseEventMsg:
		handleSSEEvent(&m, msg.TurnID, msg.Event)
		m.refreshViewport()
		return m, nil

	case sseDoneMsg:
		m.Busy = false
		m.ActiveTurnID = ""
		m.Pacer.Turns++
		m.refreshViewport()
		return m, nil

	case sseErrorMsg:
		// Surface as a system line in the active turn.
		if t, ok := m.TurnIDMap[msg.TurnID]; ok && t.Summary == nil {
			t.Summary = &TurnSummary{HaltReason: "stream: " + msg.Reason}
		}
		m.Busy = false
		m.ActiveTurnID = ""
		m.refreshViewport()
		return m, nil
	}

	// Default: route to composer textarea (typing, paste, cursor moves).
	var cmd tea.Cmd
	m.Composer, cmd = m.Composer.Update(msg)
	return m, cmd
}

// refreshViewport re-renders the conversation log into the scroll viewport.
// Called after any state change that affects what's visible.
func (m *Model) refreshViewport() {
	m.Viewport.SetContent(renderLog(m))
	m.Viewport.GotoBottom()
}

// sendTurn builds the /build request from the composer + pending images,
// transitions Model.Busy=true, and kicks off the SSE stream as a tea.Cmd.
func sendTurn(m Model) (tea.Model, tea.Cmd) {
	text := strings.TrimSpace(m.Composer.Value())
	if text == "" && len(m.PendingImages) == 0 {
		return m, nil
	}
	if text == "/reset" {
		// Clear turns + reset pacer; no server call.
		m.Turns = nil
		m.TurnIDMap = map[string]*Turn{}
		m.Pacer = PacerState{TypicalWall: m.Pacer.TypicalWall}
		m.Composer.Reset()
		m.refreshViewport()
		return m, nil
	}

	// Client-side turn id; the server will emit its own turn_id event
	// and the View can correlate. For now we use a 12-hex marker.
	turnID := "t" + randHex(6)
	m.Busy = true
	m.ActiveTurnID = turnID

	// Snapshot the pending image queue, clear it (so subsequent typing
	// doesn't race).
	images := append([]PendingImage(nil), m.PendingImages...)
	m.PendingImages = nil

	body := buildRequest(text, m.ProjectDir, images)
	ctx, cancel := context.WithCancel(context.Background())
	m.Cancel = cancel

	// Pre-emit turn-started so the View renders the user-msg card immediately
	// (vs waiting for the server's first chunk).
	return m, tea.Batch(
		func() tea.Msg { return sseStartedMsg{TurnID: turnID} },
		runSSEStream(ctx, m.ServerPort, body, turnID),
	)
}

// newTurn allocates an empty Turn with all phases in "pending" state.
func newTurn(id, userText string, imageCount int) *Turn {
	t := &Turn{
		ID:         id,
		UserText:   userText,
		ImageCount: imageCount,
		Phases:     map[string]*PhaseState{},
		PhaseOrder: []string{},
	}
	for _, name := range nodeOrder {
		if name == "human_fallback" {
			continue
		}
		t.Phases[name] = &PhaseState{
			Name:        name,
			Label:       PhaseLabel(name),
			Status:      "pending",
			Buffers:     map[string]*Utterance{},
			BufferOrder: []string{},
		}
		t.PhaseOrder = append(t.PhaseOrder, name)
	}
	// First phase starts in "running" state.
	if first, ok := t.Phases[nodeOrder[0]]; ok {
		first.Status = "running"
		first.Log = "running…"
	}
	return t
}

// handleSSEEvent dispatches one SSE event into the Turn's phase state.
// Mirrors the workbench webview's message switch.
func handleSSEEvent(m *Model, turnID string, ev SSEEvent) {
	t, ok := m.TurnIDMap[turnID]
	if !ok {
		return
	}
	switch ev.Event {
	case "turn_id":
		// Server's authoritative turn_id — remap.
		if id, ok := ev.Data["turn_id"].(string); ok && id != turnID {
			m.TurnIDMap[id] = t
		}
	case "images_attached":
		// Server confirmed paths. Already shown to user via image_count
		// on user-msg; nothing extra to do.
	case "thinking_chunk":
		node, _ := ev.Data["node"].(string)
		speaker, _ := ev.Data["speaker"].(string)
		role, _ := ev.Data["role"].(string)
		text, _ := ev.Data["text"].(string)
		if text == "" {
			return
		}
		// Pacer: chars/4 industry proxy for token count.
		m.Pacer.Tokens += (len(text) + 3) / 4

		phase, ok := t.Phases[node]
		if !ok {
			return
		}
		if phase.Status == "pending" {
			phase.Status = "running"
			phase.Log = "running…"
		}
		appendChunkToPhase(phase, speaker, role, text)

	case "node_complete":
		nodeName, _ := ev.Data["node_name"].(string)
		logLine, _ := ev.Data["log"].(string)
		halt, _ := ev.Data["halt_reason"].(string)
		phase, ok := t.Phases[nodeName]
		if !ok {
			return
		}
		if halt != "" {
			phase.Status = "fail"
			phase.Log = logLine + " · " + halt
		} else {
			phase.Status = "done"
			phase.Log = logLine
		}
		// Stop the streaming-cursor on any live utterances in this phase.
		for _, u := range phase.Buffers {
			u.Streaming = false
		}
		// Promote next pending phase to running.
		seen := false
		for _, n := range nodeOrder {
			if n == nodeName {
				seen = true
				continue
			}
			if seen {
				if next, ok := t.Phases[n]; ok && next.Status == "pending" {
					next.Status = "running"
					next.Log = "running…"
					break
				}
			}
		}

	case "done":
		halt, _ := ev.Data["halt_reason"].(string)
		hash, _ := ev.Data["pre_execute_commit_hash"].(string)
		var diffs []DiffSummary
		if raw, ok := ev.Data["proposed_diffs"].([]any); ok {
			for _, item := range raw {
				if m, ok := item.(map[string]any); ok {
					fp, _ := m["file_path"].(string)
					cb, _ := m["contract_bullet_quoted"].(string)
					diffs = append(diffs, DiffSummary{FilePath: fp, ContractBulletQuoted: cb})
				}
			}
		}
		contractCount := 0
		if raw, ok := ev.Data["design_contract"].([]any); ok {
			contractCount = len(raw)
		}
		t.Summary = &TurnSummary{
			HaltReason:           halt,
			PreExecuteCommitHash: hash,
			ProposedDiffs:        diffs,
			DesignContractCount:  contractCount,
		}
		// Mark unfinished phases as skipped (e.g. build_needed=false short-circuits).
		for _, name := range t.PhaseOrder {
			ph := t.Phases[name]
			if ph.Status == "pending" {
				ph.Status = "skipped"
				ph.Log = "not run"
			}
			if ph.Status == "running" {
				ph.Status = "done"
			}
		}

	case "error":
		reason, _ := ev.Data["reason"].(string)
		t.Summary = &TurnSummary{HaltReason: "error: " + reason}
	}
}

// appendChunkToPhase routes a thinking_chunk to the right utterance bucket.
// tool_call / tool_result / tool_error / thinking get rendered as cards.
func appendChunkToPhase(phase *PhaseState, speaker, role, text string) {
	switch role {
	case "tool_call":
		phase.Tools = append(phase.Tools, ToolCard{Kind: "tool_call", Text: text})
		// Stop any active streaming utterance for this speaker (visual settle).
		for k, u := range phase.Buffers {
			if u.Speaker == speaker {
				u.Streaming = false
				_ = k
			}
		}
		return
	case "tool_result":
		phase.Tools = append(phase.Tools, ToolCard{Kind: "tool_result", Text: text})
		return
	case "tool_error":
		phase.Tools = append(phase.Tools, ToolCard{Kind: "tool_error", Text: text})
		return
	case "thinking":
		phase.Tools = append(phase.Tools, ToolCard{Kind: "thinking", Text: text})
		return
	}
	// Plain utterance — append to streaming buffer.
	key := speaker + "-" + role
	u, ok := phase.Buffers[key]
	if !ok {
		u = &Utterance{Speaker: speaker, Role: role, Streaming: true}
		phase.Buffers[key] = u
		phase.BufferOrder = append(phase.BufferOrder, key)
	}
	u.Buffer += text
	u.Streaming = true
}

// nodeOrder matches the webview NODE_ORDER exactly so phases render in
// the same sequence in both surfaces.
var nodeOrder = []string{
	"discovery", "planning", "executing", "mechanical",
	"learnings_enforcer", "contract_review", "project_state",
	"human_fallback",
}

// tickHealth schedules the next health-poll tick (every 2s).
func tickHealth() tea.Cmd {
	return tea.Tick(2*time.Second, func(time.Time) tea.Msg {
		return healthTickMsg{}
	})
}

// pollHealth fires off a /health GET and dispatches a healthMsg.
func pollHealth(port int) tea.Cmd {
	return func() tea.Msg {
		state, err := getHealth(port)
		if err != nil {
			return healthMsg{State: "unreachable"}
		}
		_ = state
		return healthMsg{State: "connected"}
	}
}

// runSSEStream spawns an SSE goroutine and uses Dispatch() (which calls
// *tea.Program.Send) to fan every event into the Bubble Tea loop. The
// returned tea.Cmd does NOT wait for the stream — it returns immediately
// with a nil msg. The goroutine lives until the server closes or ctx
// cancels; each parsed event becomes a sseEventMsg, and we emit a
// terminal sseDoneMsg / sseErrorMsg when the stream ends.
func runSSEStream(ctx context.Context, port int, body any, turnID string) tea.Cmd {
	go func() {
		err := StreamSSE(
			ctx,
			fmt.Sprintf("http://127.0.0.1:%d/build", port),
			SSEOptions{Method: "POST", Body: body},
			func(ev SSEEvent) {
				Dispatch(sseEventMsg{TurnID: turnID, Event: ev})
			},
		)
		if err != nil && ctx.Err() == nil {
			Dispatch(sseErrorMsg{TurnID: turnID, Reason: err.Error()})
			return
		}
		Dispatch(sseDoneMsg{TurnID: turnID})
	}()
	// Return a no-op Cmd; events arrive via Dispatch from the goroutine above.
	return func() tea.Msg { return nil }
}

// buildRequest constructs the /build POST body matching server.py's BuildRequest.
func buildRequest(task, projectDir string, images []PendingImage) map[string]any {
	body := map[string]any{
		"task":        task,
		"project_dir": projectDir,
	}
	if len(images) > 0 {
		imgList := make([]map[string]string, 0, len(images))
		for _, img := range images {
			imgList = append(imgList, map[string]string{
				"filename":    img.Filename,
				"data_base64": img.DataBase64,
			})
		}
		body["images"] = imgList
	}
	return body
}

// randHex returns 2n hex characters of cryptographic randomness.
func randHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// textinputCmd is a no-op placeholder for Bubble Tea command batching.
// Reserved for future textarea-blink commands etc.
func textinputCmd() tea.Cmd { return nil }
