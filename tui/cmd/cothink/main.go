// Command cothink launches the dual-brain TUI.
//
// Talks to the existing cothink Python FastAPI server on localhost:8765
// (started separately via `cothink-serve` or auto-spawned in a future
// version). The Go binary is a pure view layer; all the dual-brain logic
// lives in the Python LangGraph backend.
//
// Usage:
//
//	cothink                  # open TUI in cwd, expect server on :8765
//	cothink --port 9000      # use a different server port
//	cothink --dir /path      # work against a specific project dir
package main

import (
	"flag"
	"fmt"
	"os"

	tea "github.com/charmbracelet/bubbletea"

	"github.com/cothink/tui"
)

func main() {
	port := flag.Int("port", 8765, "cothink Python server port (default 8765)")
	dir := flag.String("dir", "", "project directory (default: cwd)")
	flag.Parse()

	projectDir := *dir
	if projectDir == "" {
		cwd, err := os.Getwd()
		if err != nil {
			fmt.Fprintf(os.Stderr, "cothink: cannot resolve cwd: %v\n", err)
			os.Exit(2)
		}
		projectDir = cwd
	}

	model := tui.NewModel(projectDir, *port)
	p := tea.NewProgram(
		model,
		tea.WithAltScreen(),       // takes over the terminal — like Crush
		tea.WithMouseCellMotion(), // mouse scroll in the viewport
	)
	tui.ActiveProgram = p // SSE goroutines can now fan in via tui.Dispatch

	if _, err := p.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "cothink: %v\n", err)
		os.Exit(1)
	}
}
