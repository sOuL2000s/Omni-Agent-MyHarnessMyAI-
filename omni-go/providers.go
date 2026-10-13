package main

import (
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
)

type Provider struct {
	Name    string `json:"name"`
	BaseURL string `json:"base_url"`
	APIKey  string `json:"api_key"`
	Model   string `json:"model"`
	// AuthStyle is "bearer" (default), "x-api-key" (Anthropic), "query" (Gemini native), or "none".
	AuthStyle string `json:"auth_style,omitempty"`
	// Models holds the last discovered model list for this specific key.
	Models []string `json:"models,omitempty"`
}

func (p *Provider) Pool() string {
	re := regexp.MustCompile(`^(.*?)[-_ ]?\d+$`)
	if m := re.FindStringSubmatch(p.Name); m != nil && m[1] != "" {
		return m[1]
	}
	return p.Name
}

func (p *Provider) Masked() string {
	k := p.APIKey
	if len(k) > 10 {
		return k[:4] + "..." + k[len(k)-3:]
	}
	if k == "" {
		return "(none)"
	}
	return "***"
}

func (p *Provider) EffectiveAuthStyle() string {
	if p.AuthStyle != "" {
		return p.AuthStyle
	}
	return guessAuthStyle(p.BaseURL)
}

func guessAuthStyle(baseURL string) string {
	l := strings.ToLower(baseURL)
	switch {
	case strings.Contains(l, "anthropic.com"):
		return "x-api-key"
	case strings.Contains(l, "generativelanguage.googleapis.com") && !strings.Contains(l, "/openai"):
		return "query"
	case strings.Contains(l, "openrouter.ai"), strings.Contains(l, "openai.com"),
		strings.Contains(l, "groq.com"), strings.Contains(l, "deepseek.com"),
		strings.Contains(l, "mistral.ai"), strings.Contains(l, "localhost"):
		return "bearer"
	}
	return "bearer"
}

// -------------------------------------------------------------------- Manager

type Manager struct {
	mu        sync.RWMutex
	Items     []*Provider
	Strategy  string
	Fallbacks map[string][]string
	MaxWait   int
	path      string
	settings  string
	cfg       *Config
	rr        map[string]string
}

func NewManager(cfg *Config) *Manager {
	m := &Manager{
		Fallbacks: map[string][]string{},
		Strategy:  cfg.Strategy,
		MaxWait:   cfg.MaxWait,
		path:      filepath.Join(cfg.DataDir, "providers.json"),
		settings:  filepath.Join(cfg.DataDir, "settings.json"),
		cfg:       cfg,
		rr:        map[string]string{},
	}
	m.load()
	return m
}

func (m *Manager) load() {
	if raw, err := os.ReadFile(m.path); err == nil {
		_ = json.Unmarshal(raw, &m.Items)
	} else if m.cfg.APIKey != "" || m.cfg.BaseURL != "" {
		m.Items = append(m.Items, &Provider{
			Name: "default", BaseURL: m.cfg.BaseURL,
			APIKey: m.cfg.APIKey, Model: m.cfg.Model,
		})
	}
	if raw, err := os.ReadFile(m.settings); err == nil {
		var s struct {
			Strategy  string              `json:"strategy"`
			Fallbacks map[string][]string `json:"fallbacks"`
			MaxWait   int                 `json:"max_wait"`
		}
		if json.Unmarshal(raw, &s) == nil {
			if s.Strategy != "" {
				m.Strategy = s.Strategy
			}
			if s.Fallbacks != nil {
				m.Fallbacks = s.Fallbacks
			}
			if s.MaxWait > 0 {
				m.MaxWait = s.MaxWait
			}
		}
	}
}

func (m *Manager) Save() error {
	m.mu.Lock()
	defer m.mu.Unlock()
	raw, _ := json.MarshalIndent(m.Items, "", " ")
	return os.WriteFile(m.path, raw, 0o600)
}

func (m *Manager) saveSettings() {
	m.mu.Lock()
	defer m.mu.Unlock()
	raw, _ := json.MarshalIndent(map[string]interface{}{
		"strategy":  m.Strategy,
		"fallbacks": m.Fallbacks,
		"max_wait":  m.MaxWait,
	}, "", " ")
	_ = os.WriteFile(m.settings, raw, 0o644)
}

func (m *Manager) Pools() []string {
	m.mu.RLock()
	defer m.mu.RUnlock()
	seen := map[string]bool{}
	var out []string
	for _, p := range m.Items {
		if !seen[p.Pool()] {
			seen[p.Pool()] = true
			out = append(out, p.Pool())
		}
	}
	return out
}

func (m *Manager) ProvidersIn(pool string) []*Provider {
	m.mu.RLock()
	defer m.mu.RUnlock()
	var out []*Provider
	for _, p := range m.Items {
		if p.Pool() == pool {
			out = append(out, p)
		}
	}
	return out
}

func (m *Manager) ModelOf(pool string) string {
	ps := m.ProvidersIn(pool)
	if len(ps) == 0 {
		return ""
	}
	return ps[0].Model
}

func (m *Manager) SetModel(pool, model string) {
	m.mu.Lock()
	for _, p := range m.Items {
		if p.Pool() == pool {
			p.Model = model
		}
	}
	m.mu.Unlock()
	_ = m.Save()
}

func (m *Manager) SetStrategy(s string) {
	m.mu.Lock()
	m.Strategy = s
	m.mu.Unlock()
	m.saveSettings()
}

func (m *Manager) SetFallbacks(pool string, models []string) {
	m.mu.Lock()
	m.Fallbacks[pool] = models
	m.mu.Unlock()
	m.saveSettings()
}

// AddKey registers a new key and synchronously fetches its model list.
func (m *Manager) AddKey(pool, apiKey, baseURL, model, authStyle string) (*Provider, error) {
	if baseURL == "" {
		if pre, ok := Presets[pool]; ok {
			baseURL = pre.BaseURL
			if model == "" {
				model = pre.Model
			}
			if authStyle == "" {
				authStyle = pre.AuthStyle
			}
		}
	}
	if model == "" {
		model = "auto"
	}
	m.mu.Lock()
	n := len(m.Items) + 1
	// Count only this pool for the number.
	poolCount := 0
	for _, p := range m.Items {
		if p.Pool() == pool {
			poolCount++
		}
	}
	_ = n
	name := pool + "-" + itoa(poolCount+1)
	p := &Provider{
		Name: name, BaseURL: baseURL, APIKey: apiKey,
		Model: model, AuthStyle: authStyle,
	}
	m.Items = append(m.Items, p)
	m.mu.Unlock()
	_ = m.Save()

	// Synchronous discovery so the UI gets models on the same request.
	if ids, err := ListModelsForProvider(p); err == nil {
		m.mu.Lock()
		p.Models = ids
		m.mu.Unlock()
		_ = m.Save()
	}
	return p, nil
}

// ModelsFor returns the merged set of models across all keys of a pool.
func (m *Manager) ModelsFor(pool string) []string {
	m.mu.RLock()
	defer m.mu.RUnlock()
	merged := map[string]bool{}
	for _, p := range m.Items {
		if p.Pool() != pool {
			continue
		}
		for _, id := range p.Models {
			merged[id] = true
		}
	}
	out := make([]string, 0, len(merged))
	for k := range merged {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// ModelsPerKey returns the model list for each individual key of a pool.
func (m *Manager) ModelsPerKey(pool string) map[string][]string {
	m.mu.RLock()
	defer m.mu.RUnlock()
	out := map[string][]string{}
	for _, p := range m.Items {
		if p.Pool() == pool {
			out[p.Name] = append([]string(nil), p.Models...)
		}
	}
	return out
}

// RefreshModels re-fetches the model list for every key in a pool.
func (m *Manager) RefreshModels(pool string) map[string]string {
	status := map[string]string{}
	for _, p := range m.ProvidersIn(pool) {
		ids, err := ListModelsForProvider(p)
		if err != nil {
			status[p.Name] = "error: " + err.Error()
			continue
		}
		m.mu.Lock()
		p.Models = ids
		m.mu.Unlock()
		status[p.Name] = "ok"
	}
	_ = m.Save()
	return status
}

func (m *Manager) RemoveKey(name string) {
	m.mu.Lock()
	var out []*Provider
	for _, p := range m.Items {
		if p.Name != name {
			out = append(out, p)
		}
	}
	m.Items = out
	m.mu.Unlock()
	_ = m.Save()
}

func (m *Manager) RemovePool(pool string) {
	m.mu.Lock()
	var out []*Provider
	for _, p := range m.Items {
		if p.Pool() != pool {
			out = append(out, p)
		}
	}
	m.Items = out
	delete(m.Fallbacks, pool)
	m.mu.Unlock()
	_ = m.Save()
	m.saveSettings()
}

func (m *Manager) PickOrder(pool string) []*Provider {
	m.mu.RLock()
	defer m.mu.RUnlock()
	var ps []*Provider
	for _, p := range m.Items {
		if p.Pool() == pool {
			ps = append(ps, p)
		}
	}
	if m.Strategy == "round_robin" && len(ps) > 1 {
		last := m.rr[pool]
		for i, p := range ps {
			if p.Name == last {
				ps = append(ps[i+1:], ps[:i+1]...)
				break
			}
		}
	}
	return ps
}

func (m *Manager) NoteUsed(pool, name string) {
	m.mu.Lock()
	m.rr[pool] = name
	m.mu.Unlock()
}

func (m *Manager) OrderPools(names []string) {
	m.mu.Lock()
	ordered := []*Provider{}
	seen := map[string]bool{}
	for _, n := range names {
		for _, p := range m.Items {
			if p.Pool() == n {
				ordered = append(ordered, p)
				seen[p.Name] = true
			}
		}
	}
	for _, p := range m.Items {
		if !seen[p.Name] {
			ordered = append(ordered, p)
		}
	}
	m.Items = ordered
	m.mu.Unlock()
	_ = m.Save()
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b [20]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	return string(b[i:])
}

type Preset struct {
	BaseURL   string
	Model     string
	AuthStyle string
}

var Presets = map[string]Preset{
	"openrouter": {"https://openrouter.ai/api/v1", "anthropic/claude-3.5-sonnet", "bearer"},
	"gemini":     {"https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.0-flash", "bearer"},
	"omniroute":  {"http://localhost:20128/v1", "auto", "bearer"},
	"openai":     {"https://api.openai.com/v1", "gpt-4o", "bearer"},
	"groq":       {"https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", "bearer"},
	"deepseek":   {"https://api.deepseek.com/v1", "deepseek-chat", "bearer"},
	"ollama":     {"http://localhost:11434/v1", "qwen2.5-coder:7b", "bearer"},
	"mistral":    {"https://api.mistral.ai/v1", "mistral-large-latest", "bearer"},
	"anthropic":  {"https://api.anthropic.com/v1", "claude-3-5-sonnet-20241022", "x-api-key"},
}

// -------------------------------------------------------------------- /models

var modelSkipRe = regexp.MustCompile(`(?i)embed|whisper|tts|moderation|dall-e|image|imagen|veo|aqa`)

func ListModelsForProvider(p *Provider) ([]string, error) {
	base := strings.TrimRight(p.BaseURL, "/")
	url := base + "/models"
	req, _ := http.NewRequest("GET", url, nil)
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

	c := &http.Client{Timeout: 25 * time.Second}
	resp, err := c.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 4*1024*1024))
	if resp.StatusCode >= 400 {
		return nil, &FailError{Kind: "http", Msg: truncate(string(raw), 300)}
	}

	// Shape 1: OpenAI / Anthropic / Groq  {"data": [{"id": "..."}]}
	var openai struct {
		Data []struct {
			ID string `json:"id"`
		} `json:"data"`
	}
	if json.Unmarshal(raw, &openai) == nil && len(openai.Data) > 0 {
		ids := make([]string, 0, len(openai.Data))
		for _, m := range openai.Data {
			ids = append(ids, m.ID)
		}
		return filterModelIDs(ids), nil
	}

	// Shape 2: Gemini native  {"models": [{"name": "models/gemini-..."}]}
	var alt struct {
		Models []struct {
			Name string `json:"name"`
			ID   string `json:"id"`
		} `json:"models"`
	}
	if json.Unmarshal(raw, &alt) == nil && len(alt.Models) > 0 {
		ids := make([]string, 0, len(alt.Models))
		for _, m := range alt.Models {
			id := m.ID
			if id == "" {
				id = m.Name
			}
			ids = append(ids, id)
		}
		return filterModelIDs(ids), nil
	}
	return nil, &FailError{Kind: "parse", Msg: "unrecognized /models response"}
}

func filterModelIDs(ids []string) []string {
	seen := map[string]bool{}
	out := []string{}
	for _, id := range ids {
		id = strings.TrimPrefix(id, "models/")
		if id == "" || seen[id] || modelSkipRe.MatchString(id) {
			continue
		}
		seen[id] = true
		out = append(out, id)
	}
	sort.Strings(out)
	return out
}