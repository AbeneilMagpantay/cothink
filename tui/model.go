// Package tui — Bubble Tea Model (state).
//
// 1:1 conceptual port of extension/src/views/workbenchPanel.ts state.
// The Model holds:
//   - The conversation log (user messages + per-turn pipeline phases)
//   - The current composer input
//   - Health + pacer state for the header bar
//   - Active SSE stream context (so we can cancel on Ctrl+C / Stop)
package tui

import (
	"context"
	"time"

	"github.com/charmbracelet/bubbles/textarea"
	"github.com/charmbracelet/bubbles/viewport"
)

// Turn represents one user message + the pipeline phases that ran in response.
type Turn struct {
	ID         string
	UserText   string
	ImageCount int

	// Phase name → utterances and tool blocks accumulated during streaming.
	// Phase names match the Python NODE_ORDER:
	//   discovery, planning, executing, mechanical, learnings_enforcer,
	//   contract_review, project_state.
	Phases map[string]*PhaseState
	// PhaseOrder preserves the insertion order so View renders them top-to-bottom.
	PhaseOrder []string

	Summary *TurnSummary // nil until the `done` event lands
}

// PhaseState tracks one node's streaming output within a turn.
type PhaseState struct {
	Name   string
	Label  string
	Status string // "pending" | "running" | "done" | "fail" | "skipped"
	Log    string

	// Per-speaker streaming buffers (Claude cyan, Gemini magenta).
	// Key: speaker+"-"+role (e.g. "claude-propose", "gemini-verdict").
	Buffers map[string]*Utterance
	// BufferOrder preserves insertion order for rendering.
	BufferOrder []string

	// Tool call / result cards (yellow ▸ $ cmd → green output / red error).
	Tools []ToolCard
}

// Utterance is one streaming speaker's accumulated output in a phase.
type Utterance struct {
	Speaker  string // "claude" | "gemini"
	Role     string // "explore" | "propose" | "critique" | "merge" | "verdict" | "review" | "enforce"
	Buffer   string
	Streaming bool
}

// ToolCard renders a single tool_call or tool_result block (terminal-style).
type ToolCard struct {
	Kind   string // "tool_call" | "tool_result" | "tool_error" | "thinking"
	Text   string
}

// TurnSummary captures the final `done` event's payload.
type TurnSummary struct {
	HaltReason            string
	PreExecuteCommitHash  string
	ProposedDiffs         []DiffSummary
	DesignContractCount   int
}

type DiffSummary struct {
	FilePath              string
	ContractBulletQuoted  string
}

// PacerState is the rolling token/turn heuristic in the header.
type PacerState struct {
	Turns          int
	Tokens         int // estimate: chars/4 from chunk text
	TypicalWall    int // empirical, default 10
}

// Model is the full Bubble Tea state for cothink's TUI.
type Model struct {
	// UI components
	Composer  textarea.Model
	Viewport  viewport.Model

	// Conversation state
	Turns     []*Turn
	TurnIDMap map[string]*Turn // fast lookup by turn_id during streaming

	// Header / status
	Health     string // "connected" | "connecting" | "unreachable" | "stopped" | "error: ..."
	ServerPort int
	ProjectDir string
	Pacer      PacerState

	// Active stream
	Busy        bool
	ActiveTurnID string
	Cancel       context.CancelFunc // cancels the current SSE stream

	// Image queue (paste before send)
	PendingImages []PendingImage

	// Window dimensions (set on tea.WindowSizeMsg)
	Width  int
	Height int

	// Last status poll time — so we can re-poll /health periodically.
	LastHealthPoll time.Time
}

// PendingImage is a clipboard image queued for the next send.
type PendingImage struct {
	Filename   string
	DataBase64 string // including the data:image/...;base64, prefix
}

// NewModel constructs the initial Model. project_dir comes from os.Getwd()
// in main; serverPort defaults to 8765.
func NewModel(projectDir string, serverPort int) Model {
	ta := textarea.New()
	ta.Placeholder = "Ask a question, describe a task, or paste a screenshot (Ctrl+V). Enter to send, Shift+Enter for newline."
	ta.Prompt = "│ "
	ta.CharLimit = 0
	ta.SetWidth(80)
	ta.SetHeight(3)
	ta.ShowLineNumbers = false
	ta.Focus()

	vp := viewport.New(80, 20)

	return Model{
		Composer:    ta,
		Viewport:    vp,
		Turns:       []*Turn{},
		TurnIDMap:   map[string]*Turn{},
		Health:      "connecting",
		ServerPort:  serverPort,
		ProjectDir:  projectDir,
		Pacer:       PacerState{TypicalWall: 10},
		PendingImages: nil,
	}
}

// PhaseLabel returns the human label for a node name (matches webview).
func PhaseLabel(name string) string {
	switch name {
	case "discovery":
		return "Discovery"
	case "planning":
		return "Planning"
	case "executing":
		return "Executing"
	case "mechanical":
		return "Mechanical Gate"
	case "learnings_enforcer":
		return "Learnings Enforcer"
	case "contract_review":
		return "Contract Review"
	case "project_state":
		return "Project State Journal"
	case "human_fallback":
		return "Human Fallback"
	}
	return name
}
