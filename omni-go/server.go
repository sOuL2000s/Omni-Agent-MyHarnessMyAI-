package main

import (
	_ "embed"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

//go:embed index.html
var indexHTML []byte

// chatMu serialises chat runs so the package-level Approver hook cannot be
// clobbered by a second session while the first is still waiting for approval.
var chatMu sync.Mutex

type Session struct {
	ID        string
	History   []ChatMessage
	Events    []map[string]interface{}
	mu        sync.Mutex
	busy      bool
	auto      bool
	approvals map[string]chan bool
}

type WebApp struct {
	cfg      *Config
	token    string
	sessions map[string]*Session
	mu       sync.Mutex
}

// DirEntry represents a file or directory entry for the UI
type DirEntry struct {
	Name    string `json:"name"`
	Path    string `json:"path"` // Relative path from workspace
	IsDir   bool   `json:"is_dir"`
	Size    int64  `json:"size,omitempty"`
	Mtime   int64  `json:"mtime,omitempty"`
}

func newToken() string {
	b := make([]byte, 18)
	rand.Read(b)
	return hex.EncodeToString(b)
}

func Serve(cfg *Config, host string, port int) {
	app := &WebApp{cfg: cfg, token: newToken(), sessions: map[string]*Session{}}
	mux := http.NewServeMux()
	mux.HandleFunc("/", app.handleIndex)
	mux.HandleFunc("/api/", app.handleAPI)

	addr := fmt.Sprintf("%s:%d", host, port)
	url := fmt.Sprintf("http://%s:%d/?token=%s", host, port, app.token)
	fmt.Printf("Omni UI running:\n  %s\n(keep this link private — Ctrl+C to stop)\n", url)
	openBrowser(url)
	_ = http.ListenAndServe(addr, mux)
}

func openBrowser(url string) {
	var c *exec.Cmd
	if isWindows() {
		c = exec.Command("rundll32", "url.dll,FileProtocolHandler", url)
	} else if _, err := exec.LookPath("xdg-open"); err == nil {
		c = exec.Command("xdg-open", url)
	} else {
		c = exec.Command("open", url)
	}
	_ = c.Start()
}

func (a *WebApp) handleIndex(w http.ResponseWriter, r *http.Request) {
	tok := r.URL.Query().Get("token")
	if tok == a.token {
		http.SetCookie(w, &http.Cookie{
			Name: "omni_token", Value: a.token, Path: "/",
			HttpOnly: true, SameSite: http.SameSiteStrictMode,
		})
		http.Redirect(w, r, "/", http.StatusFound)
		return
	}
	c, err := r.Cookie("omni_token")
	if err != nil || c.Value != a.token {
		http.Error(w, "Open the exact link printed in the terminal", http.StatusUnauthorized)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Write(indexHTML)
}

func (a *WebApp) auth(w http.ResponseWriter, r *http.Request) bool {
	c, err := r.Cookie("omni_token")
	if err != nil || c.Value != a.token {
		http.Error(w, `{"error":"not authorised"}`, 401)
		return false
	}
	return true
}

func (a *WebApp) handleAPI(w http.ResponseWriter, r *http.Request) {
	if !a.auth(w, r) {
		return
	}
	w.Header().Set("Content-Type", "application/json")
	path := strings.TrimPrefix(r.URL.Path, "/api/")
	switch path {
	case "state":
		a.sendJSON(w, a.stateJSON())

	case "models":
		pool := r.URL.Query().Get("pool")
		refresh := r.URL.Query().Get("refresh") == "1"
		if refresh {
			a.cfg.Providers.RefreshModels(pool)
		}
		a.sendJSON(w, map[string]interface{}{
			"models":  a.cfg.Providers.ModelsFor(pool),
			"per_key": a.cfg.Providers.ModelsPerKey(pool),
		})

	case "keys/add":
		a.handleKeysAdd(w, r)
	case "keys/remove":
		var b struct {
			Name string `json:"name"`
		}
		json.NewDecoder(r.Body).Decode(&b)
		a.cfg.Providers.RemoveKey(b.Name)
		a.sendJSON(w, map[string]bool{"ok": true})
	case "keys/test":
		a.handleKeysTest(w, r)

	case "pool/model":
		var b struct {
			Pool  string `json:"pool"`
			Model string `json:"model"`
		}
		json.NewDecoder(r.Body).Decode(&b)
		a.cfg.Providers.SetModel(b.Pool, b.Model)
		a.sendJSON(w, map[string]bool{"ok": true})
	case "pool/fallbacks":
		var b struct {
			Pool   string `json:"pool"`
			Models string `json:"models"`
		}
		json.NewDecoder(r.Body).Decode(&b)
		parts := strings.FieldsFunc(b.Models, func(r rune) bool {
			return r == ',' || r == ' ' || r == '\n'
		})
		a.cfg.Providers.SetFallbacks(b.Pool, parts)
		a.sendJSON(w, map[string]bool{"ok": true})
	case "pool/remove":
		var b struct {
			Pool string `json:"pool"`
		}
		json.NewDecoder(r.Body).Decode(&b)
		a.cfg.Providers.RemovePool(b.Pool)
		a.sendJSON(w, map[string]bool{"ok": true})
	case "pool/order":
		var b struct {
			Names []string `json:"names"`
		}
		json.NewDecoder(r.Body).Decode(&b)
		a.cfg.Providers.OrderPools(b.Names)
		a.sendJSON(w, map[string]bool{"ok": true})
	case "strategy":
		var b struct {
			Strategy string `json:"strategy"`
		}
		json.NewDecoder(r.Body).Decode(&b)
		a.cfg.Providers.SetStrategy(b.Strategy)
		a.sendJSON(w, map[string]bool{"ok": true})

	case "chat":
		a.handleChat(w, r)
	case "poll":
		a.handlePoll(w, r)
	case "history":
		a.handleHistory(w, r)
	case "approve":
		a.handleApprove(w, r)
	case "stop":
		var b struct {
			Session string `json:"session"`
		}
		json.NewDecoder(r.Body).Decode(&b)
		s := a.session(b.Session)
		s.mu.Lock()
		s.busy = false
		s.mu.Unlock()
		a.sendJSON(w, map[string]bool{"ok": true})
	case "auto":
		var b struct {
			Session string `json:"session"`
			On      bool   `json:"on"`
		}
		json.NewDecoder(r.Body).Decode(&b)
		s := a.session(b.Session)
		s.mu.Lock()
		s.auto = b.On
		s.mu.Unlock()
		a.sendJSON(w, map[string]bool{"auto": b.On})
	case "sessions":
		a.sendJSON(w, a.listSessions())
	case "session/delete":
		var b struct {
			Session string `json:"session"`
		}
		json.NewDecoder(r.Body).Decode(&b)
		a.mu.Lock()
		delete(a.sessions, b.Session)
		a.mu.Unlock()
		a.sendJSON(w, map[string]bool{"ok": true})
	case "files":
		a.handleFiles(w, r)
	case "file":
		a.handleFileGet(w, r)
	case "upload":
		a.handleUpload(w, r)

	default:
		http.NotFound(w, r)
	}
}

func (a *WebApp) handleListDir(w http.ResponseWriter, r *http.Request) {
	reqPath := r.URL.Query().Get("path")
	if reqPath == "" {
		reqPath = "." // Default to current directory
	}

	fullPath := filepath.Join(a.cfg.Workspace, filepath.Clean("/"+reqPath))

	// Security check: ensure the path is within the workspace
	rel, err := filepath.Rel(a.cfg.Workspace, full)
	if err != nil || strings.HasPrefix(rel, "..") || strings.Contains(rel, ".."+string(filepath.Separator)) {
		http.Error(w, `{"error":"forbidden"}`, 403)
		return
	}

	entries, err := readDirSafe(fullPath)
	if err != nil {
		http.Error(w, fmt.Sprintf(`{"error":"%s"}`, err.Error()), 500)
		return
	}

	var responseEntries []DirEntry
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".") || e.Name() == "node_modules" || e.Name() == "__pycache__" {
			continue
		}

		entryPath := filepath.Join(reqPath, e.Name())
		
		de := DirEntry{
			Name:  e.Name(),
			Path:  entryPath,
			IsDir: e.IsDir(),
		}

		if !e.IsDir() {
			info, _ := e.Info()
			de.Size = info.Size()
			de.Mtime = info.ModTime().Unix()
		}
		responseEntries = append(responseEntries, de)
	}
	a.sendJSON(w, map[string]interface{}{"items": responseEntries})
}

func (a *WebApp) handleReadFileContent(w http.ResponseWriter, r *http.Request) {
	name := r.URL.Query().Get("path")
	if name == "" {
		http.Error(w, `{"error":"path required"}`, 400)
		return
	}

	full := filepath.Join(a.cfg.Workspace, filepath.Clean("/"+name))

	// Security check: ensure the path is within the workspace
	rel, err := filepath.Rel(a.cfg.Workspace, full)
	if err != nil || strings.HasPrefix(rel, "..") || strings.Contains(rel, ".."+string(filepath.Separator)) {
		http.Error(w, `{"error":"forbidden"}`, 403)
		return
	}

	content, err := os.ReadFile(full)
	if err != nil {
		http.Error(w, fmt.Sprintf(`{"error":"%s"}`, err.Error()), 500)
		return
	}
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.Write(content)
}

func (a *WebApp) stateJSON() map[string]interface{} {
	pm := a.cfg.Providers
	pools := []map[string]interface{}{}
	for _, pool := range pm.Pools() {
		providers := pm.ProvidersIn(pool)
		keys := []map[string]interface{}{}
		for _, p := range providers {
			keys = append(keys, map[string]interface{}{
				"name":   p.Name,
				"masked": p.Masked(),
				"status": "ready",
				"models": p.Models,
			})
		}
		if len(providers) == 0 {
			continue
		}
		pools = append(pools, map[string]interface{}{
			"pool":      pool,
			"model":     pm.ModelOf(pool),
			"fallbacks": pm.Fallbacks[pool],
			"base_url":  providers[0].BaseURL,
			"keys":      keys,
			"models":    pm.ModelsFor(pool),
		})
	}
	return map[string]interface{}{
		"workspace":    a.cfg.Workspace,
		"strategy":     pm.Strategy,
		"pools":        pools,
		"presets":      presetList(),
		"auto_approve": a.cfg.AutoApprove,
	}
}

func presetList() []map[string]string {
	out := []map[string]string{}
	for name, p := range Presets {
		out = append(out, map[string]string{
			"name": name, "base_url": p.BaseURL, "model": p.Model,
		})
	}
	return out
}

func (a *WebApp) handleKeysAdd(w http.ResponseWriter, r *http.Request) {
	var b struct {
		Pool      string `json:"pool"`
		Keys      string `json:"keys"`
		BaseURL   string `json:"base_url"`
		Model     string `json:"model"`
		AuthStyle string `json:"auth_style"`
	}
	json.NewDecoder(r.Body).Decode(&b)
	added := 0
	for _, k := range strings.FieldsFunc(b.Keys, func(r rune) bool {
		return r == '\n' || r == ',' || r == ';' || r == ' '
	}) {
		k = strings.TrimSpace(k)
		if k == "" || strings.HasPrefix(k, "#") {
			continue
		}
		if _, err := a.cfg.Providers.AddKey(b.Pool, k, b.BaseURL, b.Model, b.AuthStyle); err == nil {
			added++
		}
	}
	a.sendJSON(w, map[string]int{"added": added})
}

func (a *WebApp) handleKeysTest(w http.ResponseWriter, r *http.Request) {
	var b struct {
		Pool string `json:"pool"`
		Name string `json:"name"`
	}
	json.NewDecoder(r.Body).Decode(&b)
	results := []map[string]interface{}{}
	var targets []*Provider
	if b.Name != "" {
		for _, p := range a.cfg.Providers.Items {
			if p.Name == b.Name {
				targets = append(targets, p)
			}
		}
	} else {
		targets = a.cfg.Providers.ProvidersIn(b.Pool)
	}
	for _, p := range targets {
		txt, err := Ping(a.cfg, p)
		ok := err == nil
		if err != nil {
			txt = err.Error()
		}
		results = append(results, map[string]interface{}{"name": p.Name, "ok": ok, "text": txt})
	}
	a.sendJSON(w, map[string]interface{}{"results": results})
}

func (a *WebApp) session(id string) *Session {
	a.mu.Lock()
	defer a.mu.Unlock()
	s, ok := a.sessions[id]
	if !ok {
		s = &Session{ID: id, History: NewHistory(a.cfg), approvals: map[string]chan bool{}}
		a.sessions[id] = s
	}
	return s
}

func (a *WebApp) listSessions() []map[string]string {
	a.mu.Lock()
	defer a.mu.Unlock()
	out := []map[string]string{}
	for id, s := range a.sessions {
		title := "New chat"
		for _, m := range s.History {
			if m.Role == "user" {
				t := m.Content
				if len(t) > 60 {
					t = t[:60]
				}
				title = t
				break
			}
		}
		out = append(out, map[string]string{"id": id, "title": title})
	}
	return out
}

func (a *WebApp) handleChat(w http.ResponseWriter, r *http.Request) {
	var b struct {
		Session string `json:"session"`
		Text    string `json:"text"`
	}
	json.NewDecoder(r.Body).Decode(&b)
	s := a.session(b.Session)
	s.mu.Lock()
	if s.busy {
		s.mu.Unlock()
		http.Error(w, `{"error":"still working"}`, 409)
		return
	}
	s.busy = true
	s.mu.Unlock()

	s.emit(map[string]interface{}{"type": "user", "text": b.Text})
	go a.runChat(s, b.Text)
	a.sendJSON(w, map[string]bool{"ok": true})
}

func (a *WebApp) runChat(s *Session, text string) {
	// Serialise chats so the global Approver cannot be clobbered by a
	// second session while the first is still waiting on a dialog.
	chatMu.Lock()
	defer chatMu.Unlock()

	s.mu.Lock()
	auto := s.auto
	s.mu.Unlock()

	if !auto {
		Approver = func(action string) bool {
			s.mu.Lock()
			if s.auto {
				s.mu.Unlock()
				return true
			}
			ch := make(chan bool, 1)
			id := fmt.Sprintf("a%d", time.Now().UnixNano())
			s.approvals[id] = ch
			s.mu.Unlock()
			s.emit(map[string]interface{}{"type": "approval", "id": id, "text": action})
			select {
			case ok := <-ch:
				s.mu.Lock()
				delete(s.approvals, id)
				s.mu.Unlock()
				return ok
			case <-time.After(10 * time.Minute):
				s.mu.Lock()
				delete(s.approvals, id)
				s.mu.Unlock()
				return false
			}
		}
	} else {
		Approver = nil
	}
	defer func() {
		Approver = nil
		s.mu.Lock()
		s.busy = false
		s.mu.Unlock()
		s.emit(map[string]interface{}{"type": "done"})
	}()

	// Streaming coalescer: batch deltas so the UI doesn't get one event per token.
	var buf strings.Builder
	last := time.Now()
	flush := func() {
		if buf.Len() == 0 {
			return
		}
		s.emit(map[string]interface{}{"type": "stream", "text": buf.String()})
		buf.Reset()
		last = time.Now()
	}

	final := Run(a.cfg, &s.History, text, func(kind, txt string, extra map[string]interface{}) {
		if kind == "stream" {
			buf.WriteString(txt)
			if time.Since(last) > 60*time.Millisecond {
				flush()
			}
			return
		}
		flush()
		ev := map[string]interface{}{"type": kind, "text": txt}
		for k, v := range extra {
			ev[k] = v
		}
		s.emit(ev)
	})
	flush()
	s.emit(map[string]interface{}{"type": "final", "text": final})
}

func (s *Session) emit(ev map[string]interface{}) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.Events = append(s.Events, ev)
}

func (a *WebApp) handlePoll(w http.ResponseWriter, r *http.Request) {
	sid := r.URL.Query().Get("session")
	after := 0
	fmt.Sscanf(r.URL.Query().Get("after"), "%d", &after)
	s := a.session(sid)
	deadline := time.Now().Add(15 * time.Second)
	for {
		s.mu.Lock()
		if len(s.Events) > after || time.Now().After(deadline) {
			evs := append([]map[string]interface{}{}, s.Events[after:]...)
			busy := s.busy
			n := len(s.Events)
			s.mu.Unlock()
			a.sendJSON(w, map[string]interface{}{"events": evs, "next": n, "busy": busy})
			return
		}
		s.mu.Unlock()
		time.Sleep(120 * time.Millisecond)
	}
}

func (a *WebApp) handleHistory(w http.ResponseWriter, r *http.Request) {
	s := a.session(r.URL.Query().Get("session"))
	s.mu.Lock()
	defer s.mu.Unlock()
	items := []map[string]interface{}{}
	for _, m := range s.History {
		switch m.Role {
		case "user":
			items = append(items, map[string]interface{}{"type": "user", "text": m.Content})
		case "assistant":
			t := "final"
			if len(m.ToolCalls) > 0 {
				t = "assistant"
			}
			if m.Content != "" {
				items = append(items, map[string]interface{}{"type": t, "text": m.Content})
			}
			for _, c := range m.ToolCalls {
				items = append(items, map[string]interface{}{
					"type": "tool", "name": c.Function.Name,
					"text": c.Function.Arguments, "id": c.ID,
				})
			}
		case "tool":
			items = append(items, map[string]interface{}{
				"type": "tool_result", "text": m.Content,
				"id": m.ToolCallID, "ok": !strings.HasPrefix(m.Content, "ERROR"),
			})
		}
	}
	a.sendJSON(w, map[string]interface{}{
		"items": items, "busy": s.busy, "cursor": len(s.Events),
	})
}

func (a *WebApp) handleApprove(w http.ResponseWriter, r *http.Request) {
	var b struct {
		Session string `json:"session"`
		ID      string `json:"id"`
		Allow   bool   `json:"allow"`
	}
	json.NewDecoder(r.Body).Decode(&b)
	s := a.session(b.Session)
	s.mu.Lock()
	ch := s.approvals[b.ID]
	s.mu.Unlock()
	if ch != nil {
		ch <- b.Allow
	}
	a.sendJSON(w, map[string]bool{"ok": ch != nil})
}

// -------------------------------------------------------------------- files

func (a *WebApp) handleUploadFiles(w http.ResponseWriter, r *http.Request) {
	root := a.cfg.Workspace
	entries := []map[string]interface{}{}
	walkFiles(root, root, &entries, 500)
	a.sendJSON(w, entries)
}

func walkFiles(root, dir string, out *[]map[string]interface{}, limit int) {
	entries, err := readDirSafe(dir)
	if err != nil {
		return
	}
	for _, e := range entries {
		if len(*out) >= limit {
			return
		}
		if strings.HasPrefix(e.Name(), ".") || e.Name() == "node_modules" || e.Name() == "__pycache__" {
			continue
		}
		full := filepath.Join(dir, e.Name())
		if e.IsDir() {
			walkFiles(root, full, out, limit)
		} else {
			info, _ := e.Info()
			*out = append(*out, map[string]interface{}{
				"path":  relative(root, full),
				"size":  info.Size(),
				"mtime": info.ModTime().Unix(),
			})
		}
	}
}

func (a *WebApp) handleFileGet(w http.ResponseWriter, r *http.Request) {
	name := r.URL.Query().Get("path")
	full := filepath.Join(a.cfg.Workspace, filepath.Clean("/"+name))
	// make sure it's still inside the workspace
	rel, err := filepath.Rel(a.cfg.Workspace, full)
	if err != nil || strings.HasPrefix(rel, "..") || strings.Contains(rel, ".."+string(filepath.Separator)) {
		http.Error(w, `{"error":"forbidden"}`, 403)
		return
	}
	http.ServeFile(w, r, full)
}

func (a *WebApp) handleUpload(w http.ResponseWriter, r *http.Request) {
	name := r.URL.Query().Get("name")
	if name == "" {
		http.Error(w, `{"error":"name required"}`, 400)
		return
	}
	// strip path separators to avoid traversal
	name = strings.ReplaceAll(name, "\\", "_")
	name = strings.ReplaceAll(name, "/", "_")
	dest := filepath.Join(a.cfg.Workspace, name)
	data := make([]byte, 0)
	buf := make([]byte, 32*1024)
	for {
		n, err := r.Body.Read(buf)
		if n > 0 {
			data = append(data, buf[:n]...)
		}
		if err != nil {
			break
		}
	}
	if err := writeFile(dest, data); err != nil {
		http.Error(w, `{"error":"write failed"}`, 500)
		return
	}
	a.sendJSON(w, map[string]interface{}{"path": name, "size": len(data)})
}

func (a *WebApp) sendJSON(w http.ResponseWriter, v interface{}) {
	json.NewEncoder(w).Encode(v)
}