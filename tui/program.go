// Package tui — package-level *tea.Program handle so SSE goroutines can
// fan-in via program.Send(msg).
//
// Bubble Tea Cmds return ONE tea.Msg each; for continuous SSE streaming
// (many events per stream) we need a way to push messages from a long-
// running goroutine. The idiomatic pattern is to hold the program ref and
// call program.Send(msg) per event.
//
// main.go sets ActiveProgram once at startup; update.go's runSSEStream
// uses it to dispatch each parsed SSE event into the Bubble Tea loop.
package tui

import tea "github.com/charmbracelet/bubbletea"

// ActiveProgram is the running Bubble Tea program. Set by main; read by
// the SSE streaming goroutine. nil until tea.NewProgram is called.
var ActiveProgram *tea.Program

// Dispatch forwards a tea.Msg into the Bubble Tea event loop. Safe to call
// from any goroutine. No-op if the program hasn't been initialized yet.
func Dispatch(msg tea.Msg) {
	if ActiveProgram != nil {
		ActiveProgram.Send(msg)
	}
}
