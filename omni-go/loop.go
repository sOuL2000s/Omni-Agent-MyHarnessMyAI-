package main

import (
	"encoding/json"
	"fmt"
	"strings"
)

const systemPrompt = `You are Omni, a highly capable autonomous assistant running on the user's own computer.
You can read and edit ANY file on the machine, run shell commands, browse the web, and edit Word/Excel/PowerPoint files.

You are also a general-purpose assistant: answer questions, explain concepts, write text, plan, calculate, translate — whatever the user asks. Tools are for when you need real-world data, not for showing off.

CRITICAL RULES — never break these:
1. NEVER invent file contents, command output, URLs, numbers, quotes or facts. If you are not certain, either use a tool to verify or say plainly "I don't know" / "I can't verify that". Guessing is a failure.
2. ALWAYS read a file with read_file before editing it. The edit_file tool refuses unless you have read the current content.
3. When the user asks something about their machine, their files, current events, or specific data — use a tool. Do not guess.
4. When you cite something from a tool (a number, a line of code, a URL), quote it exactly. Do not paraphrase what the tool said.
5. If a tool returns an error, report the error honestly. Do not pretend the call succeeded.
6. Make the smallest change that satisfies the request. Do not rewrite whole files unless asked.
7. Prefer edit_file with a unique snippet over write_file when changing an existing file.
8. Think step by step: plan briefly, act with tools, verify (re-read the file / re-run the command), then give a concise final answer.
9. Before you give a final answer that depends on tool results, re-check each claim against the actual tool output in the conversation. If a claim is not supported by a tool result, either remove it or label it as your own reasoning.
10. If you are unsure about intent, ask the user one clear question instead of guessing.

Current working folder: %s
`

func NewHistory(cfg *Config) []ChatMessage {
	return []ChatMessage{
		{Role: "system", Content: strings.Replace(systemPrompt, "%s", cfg.Workspace, 1)},
	}
}

type EventFn func(kind, text string, extra map[string]interface{})

// Run drives the tool loop. If streaming events are sent, deltas are forwarded.
func Run(cfg *Config, history *[]ChatMessage, userText string, onEvent EventFn) string {
	if onEvent == nil {
		onEvent = func(string, string, map[string]interface{}) {}
	}
	*history = append(*history, ChatMessage{Role: "user", Content: userText})

	badToolAttempts := 0

	for turn := 0; turn < cfg.MaxTurns; turn++ {
		trimHistory(history)

		// Streaming: forward text deltas so the UI shows the reply as it forms.
		var streamBuf strings.Builder
		streamFn := func(delta string) {
			streamBuf.WriteString(delta)
			onEvent("stream", delta, nil)
		}

		msg, route, err := ChatEx(cfg, *history, ToolSchemas(),
			func(s string) { onEvent("notice", s, nil) },
			streamFn)
		if err != nil {
			onEvent("error", err.Error(), nil)
			return "Error: " + err.Error()
		}
		onEvent("route", route.Provider+" · "+route.Model, map[string]interface{}{
			"provider": route.Provider, "model": route.Model, "pool": route.Pool,
		})

		*history = append(*history, msg)

		// No tool calls → this is a final answer.
		if len(msg.ToolCalls) == 0 {
			return msg.Content
		}

		if msg.Content != "" {
			onEvent("assistant", msg.Content, nil)
		}

		toolLoopOk := true
		for _, call := range msg.ToolCalls {
			// Validate tool exists and arguments are JSON, before calling.
			if _, ok := Tools[call.Function.Name]; !ok {
				badToolAttempts++
				result := fmt.Sprintf("ERROR: no such tool %q. Available tools: %s",
					call.Function.Name, strings.Join(toolNames(), ", "))
				*history = append(*history, ChatMessage{
					Role: "tool", ToolCallID: call.ID, Content: result,
				})
				onEvent("tool_result", result, map[string]interface{}{
					"id": call.ID, "ok": false, "name": call.Function.Name,
				})
				toolLoopOk = false
				continue
			}
			if call.Function.Arguments != "" {
				var probe map[string]interface{}
				if err := json.Unmarshal([]byte(call.Function.Arguments), &probe); err != nil {
					badToolAttempts++
					result := fmt.Sprintf("ERROR: your arguments were not valid JSON (%v). Send valid JSON only.", err)
					*history = append(*history, ChatMessage{
						Role: "tool", ToolCallID: call.ID, Content: result,
					})
					onEvent("tool_result", result, map[string]interface{}{
						"id": call.ID, "ok": false, "name": call.Function.Name,
					})
					toolLoopOk = false
					continue
				}
			}

			onEvent("tool", call.Function.Arguments, map[string]interface{}{
				"name": call.Function.Name, "id": call.ID,
			})
			result := CallTool(cfg, call.Function.Name, call.Function.Arguments)
			*history = append(*history, ChatMessage{
				Role: "tool", ToolCallID: call.ID, Content: result,
			})
			onEvent("tool_result", truncate(result, 1500), map[string]interface{}{
				"id": call.ID, "ok": !strings.HasPrefix(result, "ERROR"),
				"name": call.Function.Name,
			})
		}

		// If the model is repeatedly calling bad tools, correct it and retry once.
		if !toolLoopOk && badToolAttempts >= 3 {
			*history = append(*history, ChatMessage{
				Role: "system",
				Content: "You have made several invalid tool calls. " +
					"Pick a tool from the list and send strictly valid JSON arguments. " +
					"Or if you can answer without tools, just answer in text.",
			})
			badToolAttempts = 0
		}
	}
	return "Reached maximum turns."
}

func toolNames() []string {
	out := make([]string, 0, len(Tools))
	for k := range Tools {
		out = append(out, k)
	}
	return out
}

func trimHistory(h *[]ChatMessage) {
	const limit = 180_000
	total := 0
	for _, m := range *h {
		total += len(m.Content)
	}
	if total <= limit {
		return
	}
	for i := range *h {
		m := &(*h)[i]
		if m.Role == "tool" && len(m.Content) > 400 {
			m.Content = m.Content[:300] + "\n...[trimmed]"
		}
	}
}