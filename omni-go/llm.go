package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strings"
	"time"
)

type ChatMessage struct {
	Role       string     `json:"role"`
	Content    string     `json:"content,omitempty"`
	ToolCalls  []ToolCall `json:"tool_calls,omitempty"`
	ToolCallID string     `json:"tool_call_id,omitempty"`
	Name       string     `json:"name,omitempty"`
}

type ToolCall struct {
	ID       string `json:"id"`
	Type     string `json:"type"`
	Function struct {
		Name      string `json:"name"`
		Arguments string `json:"arguments"`
	} `json:"function"`
}

type Tool struct {
	Type     string `json:"type"`
	Function struct {
		Name        string                 `json:"name"`
		Description string                 `json:"description"`
		Parameters  map[string]interface{} `json:"parameters"`
	} `json:"function"`
}

type Route struct {
	Provider string
	Pool     string
	Model    string
	Fallback bool
}

type FailError struct {
	Kind      string
	Msg       string
	Transient bool
	Wait      time.Duration
}

func (f *FailError) Error() string { return f.Msg }

var HTTPClient = &http.Client{Timeout: 300 * time.Second}

func HTTPGet(url, apiKey string, timeout time.Duration) ([]byte, error) {
	req, _ := http.NewRequest("GET", url, nil)
	req.Header.Set("User-Agent", "omni/3.0")
	if apiKey != "" {
		req.Header.Set("Authorization", "Bearer "+apiKey)
	}
	c := &http.Client{Timeout: timeout}
	resp, err := c.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return body, fmt.Errorf("HTTP %d: %s", resp.StatusCode, truncate(string(body), 300))
	}
	return body, nil
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}

// Reasoning models that break when sent system role or temperature.
var reasoningRe = regexp.MustCompile(`(?i)^(o1|o3|o4|deepseek-r1|deepseek-reasoner|qwq|reasoning)`)

func isReasoningModel(model string) bool {
	return reasoningRe.MatchString(model)
}

// buildBody assembles a request body respecting the model's quirks.
func buildBody(model string, messages []ChatMessage, tools []Tool, temperature float64, stream bool) map[string]interface{} {
	body := map[string]interface{}{
		"model":    model,
		"messages": messages,
	}
	if !isReasoningModel(model) {
		body["temperature"] = temperature
	}
	if len(tools) > 0 {
		body["tools"] = tools
		body["tool_choice"] = "auto"
	}
	if stream {
		body["stream"] = true
	}
	return body
}

// ChatEx tries every pool, model and key. Returns when one answers.
// If streamHandler is non-nil, it is called with each text delta as it arrives.
func ChatEx(
	cfg *Config,
	messages []ChatMessage,
	tools []Tool,
	notify func(string),
	streamHandler func(string),
) (ChatMessage, Route, error) {
	m := cfg.Providers
	if notify == nil {
		notify = func(string) {}
	}
	deadline := time.Now().Add(time.Duration(m.MaxWait) * time.Second)

	var lastErr error
	for {
		var waits []time.Duration
		for _, pool := range m.Pools() {
			models := append([]string{m.ModelOf(pool)}, m.Fallbacks[pool]...)
			for _, model := range models {
				for _, p := range m.PickOrder(pool) {
					msg, err := attempt(cfg, p, model, messages, tools, streamHandler)
					if err != nil {
						lastErr = err
						notify(fmt.Sprintf("%s [%s] %v", p.Name, model, err))
						var fe *FailError
						if errors.As(err, &fe) && fe.Transient {
							waits = append(waits, fe.Wait)
						}
						continue
					}
					m.NoteUsed(pool, p.Name)
					return msg, Route{
						Provider: p.Name, Pool: pool, Model: model,
						Fallback: model != m.ModelOf(pool),
					}, nil
				}
			}
		}
		if len(waits) > 0 {
			min := waits[0]
			for _, w := range waits {
				if w < min {
					min = w
				}
			}
			if time.Now().Add(min).Before(deadline) {
				notify(fmt.Sprintf("every key busy; waiting %v", min))
				time.Sleep(min + 500*time.Millisecond)
				continue
			}
		}
		if lastErr == nil {
			lastErr = errors.New("no usable key configured")
		}
		return ChatMessage{}, Route{}, lastErr
	}
}

func attempt(
	cfg *Config,
	p *Provider,
	model string,
	messages []ChatMessage,
	tools []Tool,
	onDelta func(string),
) (ChatMessage, error) {
	useStream := onDelta != nil
	body := buildBody(model, messages, tools, cfg.Temperature, useStream)
	raw, _ := json.Marshal(body)

	req, _ := http.NewRequest("POST", strings.TrimRight(p.BaseURL, "/")+"/chat/completions", bytes.NewReader(raw))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", "omni/3.0")

	switch p.EffectiveAuthStyle() {
	case "x-api-key":
		req.Header.Set("x-api-key", p.APIKey)
		req.Header.Set("anthropic-version", "2023-06-01")
	case "query":
		q := req.URL.Query()
		q.Set("key", p.APIKey)
		req.URL.RawQuery = q.Encode()
	case "none":
	default:
		req.Header.Set("Authorization", "Bearer "+p.APIKey)
	}

	resp, err := HTTPClient.Do(req)
	if err != nil {
		return ChatMessage{}, &FailError{Kind: "network", Msg: err.Error(), Transient: true, Wait: 15 * time.Second}
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		respBody, _ := io.ReadAll(resp.Body)
		return ChatMessage{}, classifyHTTP(resp.StatusCode, respBody, resp.Header.Get("Retry-After"))
	}

	if !useStream {
		respBody, _ := io.ReadAll(resp.Body)
		return parseFullResponse(respBody)
	}
	return readStream(resp.Body, onDelta)
}

func classifyHTTP(status int, body []byte, retryAfter string) *FailError {
	msg := truncate(string(body), 300)
	switch {
	case status == 429:
		return &FailError{Kind: "rate", Msg: "rate limited: " + msg, Transient: true, Wait: parseRetryAfter(body, retryAfter)}
	case status == 401, status == 403:
		return &FailError{Kind: "auth", Msg: "invalid key: " + msg}
	case status == 402:
		return &FailError{Kind: "billing", Msg: "billing: " + msg}
	case status == 404:
		return &FailError{Kind: "model", Msg: "model not found: " + msg, Transient: true, Wait: 10 * time.Minute}
	case status >= 500:
		return &FailError{Kind: "server", Msg: msg, Transient: true, Wait: 20 * time.Second}
	}
	return &FailError{Kind: "fatal", Msg: fmt.Sprintf("HTTP %d: %s", status, msg)}
}

func parseFullResponse(raw []byte) (ChatMessage, error) {
	var d struct {
		Choices []struct {
			Message ChatMessage `json:"message"`
		} `json:"choices"`
		Error *struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.Unmarshal(raw, &d); err != nil {
		return ChatMessage{}, &FailError{Kind: "parse", Msg: "bad JSON from gateway: " + truncate(string(raw), 200)}
	}
	if d.Error != nil {
		return ChatMessage{}, classifyHTTP(400, raw, "")
	}
	if len(d.Choices) == 0 {
		return ChatMessage{}, &FailError{Kind: "empty", Msg: "empty choices"}
	}
	return d.Choices[0].Message, nil
}

// readStream parses SSE from an OpenAI-compatible stream and calls onDelta for each chunk.
// Tool calls are accumulated and returned in the final message.
func readStream(r io.Reader, onDelta func(string)) (ChatMessage, error) {
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 64*1024), 4*1024*1024)

	var content strings.Builder
	toolAcc := map[int]*ToolCall{}

	for sc.Scan() {
		line := sc.Text()
		if !strings.HasPrefix(line, "data: ") {
			continue
		}
		payload := strings.TrimPrefix(line, "data: ")
		if payload == "[DONE]" {
			break
		}
		var chunk struct {
			Choices []struct {
				Delta struct {
					Content   string `json:"content"`
					ToolCalls []struct {
						Index    int    `json:"index"`
						ID       string `json:"id"`
						Type     string `json:"type"`
						Function struct {
							Name      string `json:"name"`
							Arguments string `json:"arguments"`
						} `json:"function"`
					} `json:"tool_calls"`
				} `json:"delta"`
			} `json:"choices"`
		}
		if err := json.Unmarshal([]byte(payload), &chunk); err != nil {
			continue
		}
		for _, ch := range chunk.Choices {
			if ch.Delta.Content != "" {
				content.WriteString(ch.Delta.Content)
				if onDelta != nil {
					onDelta(ch.Delta.Content)
				}
			}
			for _, tc := range ch.Delta.ToolCalls {
				acc, ok := toolAcc[tc.Index]
				if !ok {
					acc = &ToolCall{ID: tc.ID, Type: "function"}
					acc.Function.Name = tc.Function.Name
					toolAcc[tc.Index] = acc
				}
				if tc.ID != "" {
					acc.ID = tc.ID
				}
				if tc.Function.Name != "" {
					acc.Function.Name = tc.Function.Name
				}
				acc.Function.Arguments += tc.Function.Arguments
			}
		}
	}

	msg := ChatMessage{Role: "assistant", Content: content.String()}
	if len(toolAcc) > 0 {
		for i := 0; i < len(toolAcc); i++ {
			if tc, ok := toolAcc[i]; ok {
				msg.ToolCalls = append(msg.ToolCalls, *tc)
			}
		}
	}
	return msg, nil
}

var retryRe = regexp.MustCompile(`retryDelay["\s:]+(\d+(?:\.\d+)?)s`)

func parseRetryAfter(body []byte, h string) time.Duration {
	if h != "" {
		if n, err := time.ParseDuration(h + "s"); err == nil {
			return n
		}
	}
	if m := retryRe.FindSubmatch(body); m != nil {
		var f float64
		fmt.Sscanf(string(m[1]), "%f", &f)
		return time.Duration(f * float64(time.Second))
	}
	return 30 * time.Second
}

func Ping(cfg *Config, p *Provider) (string, error) {
	msg, _, err := ChatEx(cfg,
		[]ChatMessage{{Role: "user", Content: "Reply with the single word: pong"}},
		nil, nil, nil)
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(msg.Content), nil
}