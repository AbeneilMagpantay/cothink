// Package tui — Bubble Tea View function.
//
// Renders the Model into a Lip Gloss string. Conceptual port of the webview
// HTML/CSS layout in extension/src/views/workbenchPanel.ts, minus the bits
// HTML does for free (scrolling, word-wrap). Layout:
//
//   ┌─ Server: ● connected · ▮▮▮▯▯▯▯▯▯▯ turn 3/~10 · 12k tok ────┐
//   ├──────────────────────────────────────────────────────────────┤
//   │ YOU                                                          │
//   │ Add a retry decorator                                        │
//   │                                                              │
//   │ ▾ Discovery (done)                                           │
//   │   claude · explore                                           │
//   │     I read pyproject.toml...                                 │
//   │   gemini · explore                                           │
//   │     The project uses asyncio...                              │
//   │                                                              │
//   │ ▾ Planning (running…)                                        │
//   │   …                                                          │
//   ├──────────────────────────────────────────────────────────────┤
//   │ │ Ask a question, paste a screenshot...                      │
//   └──────────────────────────────────────────────────────────────┘
package tui

import (
	"fmt"
	"strings"

	"github.com/charmbracelet/lipgloss"
)

// View renders the full Model to a string. Bubble Tea calls this every
// time Update returns, then diffs against the previous frame to draw.
func (m Model) View() string {
	if m.Width == 0 || m.Height == 0 {
		// First frame before WindowSizeMsg — render a placeholder.
		return "cothink TUI — initializing..."
	}

	header := m.renderHeader()
	composer := m.renderComposer()
	body := m.Viewport.View()

	return lipgloss.JoinVertical(
		lipgloss.Left,
		header,
		body,
		composer,
	)
}

// renderHeader builds the top status bar with server health + pacer.
func (m Model) renderHeader() string {
	healthStyle := lipgloss.NewStyle().Bold(true)
	switch m.Health {
	case "connected":
		healthStyle = healthStyle.Foreground(ColorOK)
	case "stopped", "unreachable":
		healthStyle = healthStyle.Foreground(ColorErr)
	default:
		healthStyle = healthStyle.Foreground(ColorAccent)
	}
	health := healthStyle.Render(m.Health)

	pacerBars := PacerStyle(m.Pacer.Turns, m.Pacer.Tokens, m.Pacer.TypicalWall)
	tokKLabel := fmt.Sprintf("%d tok", m.Pacer.Tokens)
	if m.Pacer.Tokens >= 1000 {
		tokKLabel = fmt.Sprintf("%.1fk tok", float64(m.Pacer.Tokens)/1000.0)
	}
	pacerText := lipgloss.NewStyle().Foreground(ColorTextDim).Render(
		fmt.Sprintf(" turn %d/~%d · %s", m.Pacer.Turns, m.Pacer.TypicalWall, tokKLabel),
	)

	leftSide := fmt.Sprintf("Server: %s", health)
	rightSide := pacerBars + pacerText

	// Pad with hint about Ctrl+C / Esc.
	hint := lipgloss.NewStyle().Foreground(ColorTextDim).Italic(true).Render(
		"  · Esc/Ctrl+C: cancel · /reset: clear",
	)

	bar := leftSide + "   " + rightSide + hint
	// Underline / divider
	divider := lipgloss.NewStyle().
		Foreground(ColorDim).
		Render(strings.Repeat("─", m.Width))
	return bar + "\n" + divider
}

// renderComposer builds the bottom input area.
func (m Model) renderComposer() string {
	composerStyle := lipgloss.NewStyle().
		BorderStyle(lipgloss.NormalBorder()).
		BorderTop(true).
		BorderForeground(ColorDim).
		Padding(0, 1)

	prefix := ""
	if len(m.PendingImages) > 0 {
		prefix = lipgloss.NewStyle().Foreground(ColorClaude).Render(
			fmt.Sprintf("📎 %d image(s) queued (will send with next message)\n", len(m.PendingImages)),
		)
	}
	if m.Busy {
		prefix += lipgloss.NewStyle().Foreground(ColorAccent).Italic(true).Render(
			"…running. Esc to cancel.\n",
		)
	}

	return composerStyle.Render(prefix + m.Composer.View())
}

// renderLog renders all turns vertically into the viewport's scroll buffer.
// Mirrors the webview's #log container.
func renderLog(m *Model) string {
	if len(m.Turns) == 0 {
		return lipgloss.NewStyle().
			Foreground(ColorTextDim).
			Italic(true).
			Align(lipgloss.Center).
			Width(m.Width).
			Padding(2).
			Render(
				"Workbench: ask a question, describe a task, or paste a screenshot.\n" +
					"Both Claude Opus 4.7 and Gemini 3.1 Pro reason on every message.\n" +
					"Discovery + Planning always run; Executing fires only when files need writing.",
			)
	}

	sections := make([]string, 0, len(m.Turns)*2)
	for _, t := range m.Turns {
		sections = append(sections, renderTurn(t, m.Width))
	}
	return strings.Join(sections, "\n\n")
}

// renderTurn renders one user message + the pipeline phases that followed.
func renderTurn(t *Turn, width int) string {
	parts := []string{renderUserMsg(t, width)}

	for _, name := range t.PhaseOrder {
		phase := t.Phases[name]
		if phase.Status == "pending" {
			continue // hide untouched phases until they activate
		}
		parts = append(parts, renderPhase(phase, width))
	}

	if t.Summary != nil {
		parts = append(parts, renderSummary(t.Summary, width))
	}
	return strings.Join(parts, "\n")
}

// renderUserMsg renders the "YOU" card with the user's prompt + image badge.
func renderUserMsg(t *Turn, width int) string {
	label := StyleUserLabel.Render("YOU")
	body := lipgloss.NewStyle().Width(width - 4).Render(t.UserText)
	attachment := ""
	if t.ImageCount > 0 {
		noun := "screenshot"
		if t.ImageCount != 1 {
			noun = "screenshots"
		}
		attachment = "\n" + lipgloss.NewStyle().Foreground(ColorClaude).Italic(true).Render(
			fmt.Sprintf("📎 %d %s attached (downsampled ≤1024px, both brains will see them)", t.ImageCount, noun),
		)
	}
	return StyleUserMsg.Width(width - 2).Render(label + "\n" + body + attachment)
}

// renderPhase renders one pipeline phase + its streaming utterances + tool cards.
func renderPhase(phase *PhaseState, width int) string {
	marker := markerFor(phase.Status)
	header := StylePhase.Render(fmt.Sprintf("%s %s", marker, phase.Label))
	if phase.Log != "" {
		header += lipgloss.NewStyle().
			Foreground(ColorTextDim).
			Render(fmt.Sprintf("  · %s", phase.Log))
	}

	lines := []string{header}

	for _, key := range phase.BufferOrder {
		u := phase.Buffers[key]
		lines = append(lines, renderUtterance(u, width))
	}

	for _, tool := range phase.Tools {
		lines = append(lines, renderTool(tool, width))
	}

	return strings.Join(lines, "\n")
}

func markerFor(status string) string {
	switch status {
	case "pending":
		return lipgloss.NewStyle().Foreground(ColorTextDim).Render("○")
	case "running":
		return lipgloss.NewStyle().Foreground(ColorAccent).Render("◌")
	case "done":
		return lipgloss.NewStyle().Foreground(ColorOK).Render("✓")
	case "fail":
		return lipgloss.NewStyle().Foreground(ColorErr).Render("✗")
	case "skipped":
		return lipgloss.NewStyle().Foreground(ColorTextDim).Render("–")
	}
	return "•"
}

// renderUtterance renders one speaker's streaming output as a side-bordered
// block in Claude cyan or Gemini magenta.
func renderUtterance(u *Utterance, width int) string {
	var style lipgloss.Style
	var labelStyle lipgloss.Style
	if u.Speaker == "claude" {
		style = StyleClaude
		labelStyle = StyleClaudeLabel
	} else {
		style = StyleGemini
		labelStyle = StyleGeminiLabel
	}

	role := u.Role
	if u.Streaming {
		role += " · thinking…"
	}
	label := labelStyle.Render(fmt.Sprintf("%s · %s", strings.ToUpper(u.Speaker), role))
	body := lipgloss.NewStyle().Width(width - 6).Render(u.Buffer)

	rendered := style.Width(width - 2).Render(label + "\n" + body)
	if u.Streaming {
		rendered += lipgloss.NewStyle().Foreground(ColorTextDim).Render("▍")
	}
	return rendered
}

// renderTool renders a single tool_call / tool_result / tool_error / thinking card.
func renderTool(tool ToolCard, width int) string {
	switch tool.Kind {
	case "tool_call":
		return StyleToolCall.Width(width - 2).Render("▸ " + tool.Text)
	case "tool_result":
		return StyleToolResult.Width(width - 2).Render(tool.Text)
	case "tool_error":
		return StyleToolError.Width(width - 2).Render(tool.Text)
	case "thinking":
		return lipgloss.NewStyle().
			Width(width - 4).
			Foreground(ColorTextDim).
			Italic(true).
			BorderLeft(true).
			BorderStyle(lipgloss.DoubleBorder()).
			BorderForeground(ColorTextDim).
			PaddingLeft(1).
			Render(tool.Text)
	}
	return tool.Text
}

// renderSummary renders the final "DONE" / "HALTED" / "Analysis" footer for a turn.
func renderSummary(s *TurnSummary, width int) string {
	var title string
	var titleStyle lipgloss.Style
	switch {
	case s.HaltReason != "":
		title = "HALTED: " + s.HaltReason
		titleStyle = lipgloss.NewStyle().Foreground(ColorErr).Bold(true)
	case len(s.ProposedDiffs) == 0:
		title = "Analysis (no files changed)"
		titleStyle = lipgloss.NewStyle().Foreground(ColorClaude).Bold(true)
	default:
		title = "DONE"
		titleStyle = lipgloss.NewStyle().Foreground(ColorOK).Bold(true)
	}

	lines := []string{titleStyle.Render(title)}

	if len(s.ProposedDiffs) > 0 {
		lines = append(lines, lipgloss.NewStyle().Foreground(ColorTextDim).Render("Files touched:"))
		for _, d := range s.ProposedDiffs {
			lines = append(lines, "  • "+d.FilePath)
		}
	}
	if s.PreExecuteCommitHash != "" {
		lines = append(lines, lipgloss.NewStyle().Foreground(ColorTextDim).Render(
			fmt.Sprintf("Rollback: git reset --hard %s", s.PreExecuteCommitHash),
		))
	}

	return lipgloss.NewStyle().
		BorderStyle(lipgloss.NormalBorder()).
		BorderForeground(ColorDim).
		Padding(0, 1).
		Width(width - 2).
		Render(strings.Join(lines, "\n"))
}
