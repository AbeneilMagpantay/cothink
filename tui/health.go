// Package tui — /health probe helper.
package tui

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"
)

// HealthResponse mirrors the Python server's /health payload.
type HealthResponse struct {
	OK          bool   `json:"ok"`
	ClaudeModel string `json:"claude_model"`
	GeminiModel string `json:"gemini_model"`
	FlashModel  string `json:"flash_model"`
}

// getHealth probes /health with a short timeout. Used by the pacer header
// to render the "Server: connected / unreachable" badge.
func getHealth(port int) (HealthResponse, error) {
	url := fmt.Sprintf("http://127.0.0.1:%d/health", port)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return HealthResponse{}, err
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return HealthResponse{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return HealthResponse{}, fmt.Errorf("status %d", resp.StatusCode)
	}
	var h HealthResponse
	if err := json.NewDecoder(resp.Body).Decode(&h); err != nil {
		return HealthResponse{}, err
	}
	return h, nil
}
