// Package tui — Lip Gloss style definitions.
//
// Matches the VSCode webview palette so the TUI feels like the same product:
//   - Claude: cyan
//   - Gemini: magenta
//   - Pacer: green / yellow / red based on session-usage fraction
//   - Phase cards: subtle bg, accent border per state
package tui

import "github.com/charmbracelet/lipgloss"

var (
	ColorClaude    = lipgloss.Color("#4ec9b0") // cyan-ish; matches webview --claude
	ColorGemini    = lipgloss.Color("#c586c0") // magenta-ish; matches --gemini
	ColorDim       = lipgloss.Color("240")
	ColorAccent    = lipgloss.Color("#d7ba7d") // yellow — tool calls / warnings
	ColorOK        = lipgloss.Color("#6a9955") // green
	ColorErr       = lipgloss.Color("#f44747") // red
	ColorPhaseBg   = lipgloss.Color("#1f1f1f")
	ColorUserBg    = lipgloss.Color("#252526")
	ColorTextDim   = lipgloss.Color("#9e9e9e")
)

// Style: user-message card (your prompt).
var StyleUserMsg = lipgloss.NewStyle().
	Border(lipgloss.NormalBorder()).
	BorderForeground(ColorDim).
	Padding(0, 1).
	MarginBottom(1)

var StyleUserLabel = lipgloss.NewStyle().
	Foreground(ColorTextDim).
	Bold(true).
	MarginRight(1)

// Style: Claude streaming utterance.
var StyleClaude = lipgloss.NewStyle().
	BorderLeft(true).
	BorderStyle(lipgloss.ThickBorder()).
	BorderForeground(ColorClaude).
	PaddingLeft(1).
	MarginBottom(1)

var StyleClaudeLabel = lipgloss.NewStyle().
	Foreground(ColorClaude).
	Bold(true).
	MarginBottom(0)

// Style: Gemini streaming utterance.
var StyleGemini = lipgloss.NewStyle().
	BorderLeft(true).
	BorderStyle(lipgloss.ThickBorder()).
	BorderForeground(ColorGemini).
	PaddingLeft(1).
	MarginBottom(1)

var StyleGeminiLabel = lipgloss.NewStyle().
	Foreground(ColorGemini).
	Bold(true).
	MarginBottom(0)

// Style: tool_call card (yellow accent, like the webview's ▸ $ cmd line).
var StyleToolCall = lipgloss.NewStyle().
	BorderLeft(true).
	BorderStyle(lipgloss.ThickBorder()).
	BorderForeground(ColorAccent).
	PaddingLeft(1).
	Foreground(lipgloss.Color("#cccccc"))

// Style: tool_result card (green accent).
var StyleToolResult = lipgloss.NewStyle().
	BorderLeft(true).
	BorderStyle(lipgloss.ThickBorder()).
	BorderForeground(ColorOK).
	PaddingLeft(1).
	Foreground(ColorTextDim).
	MaxHeight(8)

// Style: tool_error card (red accent).
var StyleToolError = lipgloss.NewStyle().
	BorderLeft(true).
	BorderStyle(lipgloss.ThickBorder()).
	BorderForeground(ColorErr).
	PaddingLeft(1).
	Foreground(ColorErr).
	MaxHeight(8)

// Style: phase header (Discovery / Planning / etc.)
var StylePhase = lipgloss.NewStyle().
	Bold(true).
	Foreground(ColorAccent).
	MarginTop(1)

// Style: status / system banners.
var StyleSystem = lipgloss.NewStyle().
	Italic(true).
	Foreground(ColorTextDim).
	Align(lipgloss.Center)

// PacerStyle returns a styled bars string for a given turn count
// against the empirical wall (default 10 turns).
func PacerStyle(turns int, tokens int, wall int) string {
	if wall <= 0 {
		wall = 10
	}
	frac := float64(turns) / float64(wall)
	if frac > 1 {
		frac = 1
	}
	filled := int(frac * 10)
	bars := ""
	for i := 0; i < 10; i++ {
		if i < filled {
			bars += "▮"
		} else {
			bars += "▯"
		}
	}
	color := ColorOK
	if frac >= 0.8 {
		color = ColorErr
	} else if frac >= 0.5 {
		color = ColorAccent
	}
	return lipgloss.NewStyle().Foreground(color).Render(bars)
}
