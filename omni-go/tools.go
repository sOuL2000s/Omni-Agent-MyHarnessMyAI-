package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

type ToolDef struct {
	Name        string
	Description string
	Params      map[string]interface{}
	Required    []string
	Handler     func(cfg *Config, args map[string]interface{}) (string, error)
}

var Tools = map[string]*ToolDef{}

func Register(t *ToolDef) { Tools[t.Name] = t }

func init() {
	Register(&ToolDef{
		Name:        "read_file",
		Description: "Read a text file with line numbers. Any absolute path works. Read before editing.",
		Params: map[string]interface{}{
			"path":  map[string]string{"type": "string"},
			"start": map[string]string{"type": "integer"},
			"end":   map[string]string{"type": "integer"},
		},
		Required: []string{"path"},
		Handler:  handleReadFile,
	})
	Register(&ToolDef{
		Name:        "write_file",
		Description: "Create or overwrite a text file anywhere on the PC. Existing files are backed up first.",
		Params: map[string]interface{}{
			"path":    map[string]string{"type": "string"},
			"content": map[string]string{"type": "string"},
		},
		Required: []string{"path", "content"},
		Handler:  handleWriteFile,
	})
	Register(&ToolDef{
		Name:        "edit_file",
		Description: "Replace exact text `old` with `new`. `old` must match exactly once unless replace_all=true. You MUST read_file first.",
		Params: map[string]interface{}{
			"path":        map[string]string{"type": "string"},
			"old":         map[string]string{"type": "string"},
			"new":         map[string]string{"type": "string"},
			"replace_all": map[string]string{"type": "boolean"},
		},
		Required: []string{"path", "old", "new"},
		Handler:  handleEditFile,
	})
	Register(&ToolDef{
		Name:        "list_dir",
		Description: "List files under a directory recursively up to `depth` levels. Defaults to the user's home directory.",
		Params: map[string]interface{}{
			"path":  map[string]string{"type": "string"},
			"depth": map[string]string{"type": "integer"},
		},
		Handler: handleListDir,
	})
	Register(&ToolDef{
		Name:        "search_files",
		Description: "Regex-search across files under a path.",
		Params: map[string]interface{}{
			"pattern": map[string]string{"type": "string"},
			"path":    map[string]string{"type": "string"},
			"glob":    map[string]string{"type": "string"},
		},
		Required: []string{"pattern"},
		Handler:  handleSearchFiles,
	})
	Register(&ToolDef{
		Name:        "run_shell",
		Description: "Run a shell command (cmd on Windows, sh elsewhere). Starts in the user's home folder. Asks for approval unless auto-approve.",
		Params: map[string]interface{}{
			"command": map[string]string{"type": "string"},
			"cwd":     map[string]string{"type": "string"},
		},
		Required: []string{"command"},
		Handler:  handleRunShell,
	})
	Register(&ToolDef{
		Name:        "web_fetch",
		Description: "Fetch a URL and return readable text with HTML tags stripped.",
		Params: map[string]interface{}{
			"url": map[string]string{"type": "string"},
		},
		Required: []string{"url"},
		Handler:  handleWebFetch,
	})
	Register(&ToolDef{
		Name:        "web_search",
		Description: "Search the web via DuckDuckGo and return the top results with titles and URLs.",
		Params: map[string]interface{}{
			"query": map[string]string{"type": "string"},
		},
		Required: []string{"query"},
		Handler:  handleWebSearch,
	})
	Register(&ToolDef{
		Name:        "list_backups",
		Description: "List every backup Omni has made in ~/.omni/backups. Useful before restoring.",
		Params:      map[string]interface{}{},
		Handler:     handleListBackups,
	})
	Register(&ToolDef{
		Name:        "restore_backup",
		Description: "Restore a file from a backup. Give the backup filename from list_backups.",
		Params: map[string]interface{}{
			"backup":      map[string]string{"type": "string"},
			"target_path": map[string]string{"type": "string"},
		},
		Required: []string{"backup", "target_path"},
		Handler:  handleRestoreBackup,
	})
}

var filesRead = map[string]bool{}

func handleReadFile(cfg *Config, a map[string]interface{}) (string, error) {
	p, err := ResolvePath(cfg, str(a["path"]))
	if err != nil {
		return "", err
	}
	data, err := os.ReadFile(p)
	if err != nil {
		return "", err
	}
	filesRead[p] = true
	lines := strings.Split(string(data), "\n")
	start := intArg(a, "start", 1)
	end := intArg(a, "end", len(lines))
	if start < 1 {
		start = 1
	}
	if end > len(lines) {
		end = len(lines)
	}
	var b strings.Builder
	fmt.Fprintf(&b, "%s (%d lines)\n", p, len(lines))
	for i := start - 1; i < end; i++ {
		fmt.Fprintf(&b, "%d\t%s\n", i+1, lines[i])
	}
	return b.String(), nil
}

func handleWriteFile(cfg *Config, a map[string]interface{}) (string, error) {
	p, err := ResolvePath(cfg, str(a["path"]))
	if err != nil {
		return "", err
	}
	if _, err := os.Stat(p); err == nil {
		BackupFile(cfg, p)
	}
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		return "", err
	}
	content := str(a["content"])
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		return "", err
	}
	return fmt.Sprintf("Wrote %d bytes to %s", len(content), p), nil
}

func handleEditFile(cfg *Config, a map[string]interface{}) (string, error) {
	p, err := ResolvePath(cfg, str(a["path"]))
	if err != nil {
		return "", err
	}
	if !filesRead[p] {
		return "", fmt.Errorf("refusing to edit %s: read the file first with read_file so you know its real contents", p)
	}
	data, err := os.ReadFile(p)
	if err != nil {
		return "", err
	}
	old := str(a["old"])
	newText := str(a["new"])
	n := strings.Count(string(data), old)
	if n == 0 {
		return "", fmt.Errorf("`old` text not found — check whitespace")
	}
	if n > 1 && !boolArg(a, "replace_all", false) {
		return "", fmt.Errorf("`old` matches %d places; add context or set replace_all=true", n)
	}
	BackupFile(cfg, p)
	out := strings.Replace(string(data), old, newText, 1)
	if boolArg(a, "replace_all", false) {
		out = strings.ReplaceAll(string(data), old, newText)
	}
	if err := os.WriteFile(p, []byte(out), 0o644); err != nil {
		return "", err
	}
	return fmt.Sprintf("Edited %s (%d replacement(s))", p, n), nil
}

func handleListDir(cfg *Config, a map[string]interface{}) (string, error) {
	path := strOr(a["path"], homeDir())
	p, err := ResolvePath(cfg, path)
	if err != nil {
		return "", err
	}
	depth := intArg(a, "depth", 2)
	var b strings.Builder
	walk(p, 0, depth, &b)
	if b.Len() == 0 {
		return "(empty)", nil
	}
	return b.String(), nil
}

func walk(dir string, level, max int, b *strings.Builder) {
	if level > max {
		return
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return
	}
	for _, e := range entries {
		name := e.Name()
		if strings.HasPrefix(name, ".git") || name == "node_modules" || name == "__pycache__" || name == ".venv" {
			continue
		}
		indent := strings.Repeat("  ", level)
		if e.IsDir() {
			fmt.Fprintf(b, "%s%s/\n", indent, name)
			walk(filepath.Join(dir, name), level+1, max, b)
		} else {
			fmt.Fprintf(b, "%s%s\n", indent, name)
		}
	}
}

func handleSearchFiles(cfg *Config, a map[string]interface{}) (string, error) {
	root, err := ResolvePath(cfg, strOr(a["path"], homeDir()))
	if err != nil {
		return "", err
	}
	re, err := regexp.Compile(str(a["pattern"]))
	if err != nil {
		return "", err
	}
	glob := strOr(a["glob"], "*")
	var hits []string
	filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() {
			return nil
		}
		if ok, _ := filepath.Match(glob, info.Name()); !ok {
			return nil
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return nil
		}
		for i, line := range strings.Split(string(data), "\n") {
			if re.MatchString(line) {
				hits = append(hits, fmt.Sprintf("%s:%d: %s", path, i+1, strings.TrimSpace(line)))
				if len(hits) >= 200 {
					return fmt.Errorf("stop")
				}
			}
		}
		return nil
	})
	if len(hits) == 0 {
		return "No matches.", nil
	}
	return strings.Join(hits, "\n"), nil
}

func handleRunShell(cfg *Config, a map[string]interface{}) (string, error) {
	cmd := str(a["command"])
	if !Approve(cfg, "Run shell: "+cmd) {
		return "", fmt.Errorf("user denied the action")
	}
	cwd := strOr(a["cwd"], homeDir())
	var c *exec.Cmd
	if isWindows() {
		c = exec.Command("cmd", "/C", cmd)
	} else {
		c = exec.Command("sh", "-c", cmd)
	}
	c.Dir = cwd
	out, err := c.CombinedOutput()
	if err != nil {
		return fmt.Sprintf("exit error: %v\n%s", err, string(out)), nil
	}
	return string(out), nil
}

func isWindows() bool { return os.PathSeparator == '\\' }

// -------------------------------------------------------------------- backups

func handleListBackups(cfg *Config, _ map[string]interface{}) (string, error) {
	entries, err := os.ReadDir(cfg.BackupDir)
	if err != nil {
		return "(no backups yet)", nil
	}
	names := []string{}
	for _, e := range entries {
		info, _ := e.Info()
		names = append(names, fmt.Sprintf("%s  (%d bytes)", e.Name(), info.Size()))
	}
	sort.Strings(names)
	if len(names) == 0 {
		return "(no backups yet)", nil
	}
	return strings.Join(names, "\n"), nil
}

func handleRestoreBackup(cfg *Config, a map[string]interface{}) (string, error) {
	name := filepath.Base(str(a["backup"]))
	src := filepath.Join(cfg.BackupDir, name)
	data, err := os.ReadFile(src)
	if err != nil {
		return "", fmt.Errorf("backup not found: %s", name)
	}
	target, err := ResolvePath(cfg, str(a["target_path"]))
	if err != nil {
		return "", err
	}
	// Back up the current version before overwriting it, so restore is reversible.
	if _, err := os.Stat(target); err == nil {
		BackupFile(cfg, target)
	}
	if err := os.WriteFile(target, data, 0o644); err != nil {
		return "", err
	}
	return fmt.Sprintf("Restored %s from %s", target, name), nil
}

// -------------------------------------------------------------------- web

func handleWebFetch(cfg *Config, a map[string]interface{}) (string, error) {
	url := str(a["url"])
	body, err := HTTPGet(url, "", 30*1e9)
	if err != nil {
		return "", err
	}
	return stripHTML(string(body)), nil
}

func handleWebSearch(cfg *Config, a map[string]interface{}) (string, error) {
	q := str(a["query"])
	url := "https://html.duckduckgo.com/html/?q=" + strings.ReplaceAll(q, " ", "+")
	body, err := HTTPGet(url, "", 30*1e9)
	if err != nil {
		return "", err
	}
	re := regexp.MustCompile(`(?s)<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>`)
	matches := re.FindAllStringSubmatch(string(body), 10)
	var b strings.Builder
	for _, m := range matches {
		fmt.Fprintf(&b, "%s\n  %s\n\n", stripHTML(m[2]), m[1])
	}
	if b.Len() == 0 {
		return "No results parsed.", nil
	}
	return b.String(), nil
}

func stripHTML(s string) string {
	re := regexp.MustCompile(`(?s)<script.*?</script>|<style.*?</style>|<[^>]+>`)
	s = re.ReplaceAllString(s, " ")
	s = regexp.MustCompile(`\s+`).ReplaceAllString(s, " ")
	return strings.TrimSpace(s)
}

// -------------------------------------------------------------------- helpers

func ToolSchemas() []Tool {
	var out []Tool
	for _, t := range Tools {
		params := map[string]interface{}{
			"type":       "object",
			"properties": t.Params,
		}
		if len(t.Required) > 0 {
			params["required"] = t.Required
		}
		var tool Tool
		tool.Type = "function"
		tool.Function.Name = t.Name
		tool.Function.Description = t.Description
		tool.Function.Parameters = params
		out = append(out, tool)
	}
	return out
}

func CallTool(cfg *Config, name, argsJSON string) string {
	t, ok := Tools[name]
	if !ok {
		return fmt.Sprintf("ERROR: unknown tool %s", name)
	}
	var args map[string]interface{}
	if argsJSON != "" {
		if err := json.Unmarshal([]byte(argsJSON), &args); err != nil {
			return fmt.Sprintf("ERROR: bad JSON arguments: %v", err)
		}
	}
	out, err := t.Handler(cfg, args)
	if err != nil {
		return "ERROR: " + err.Error()
	}
	return out
}

func str(v interface{}) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

func strOr(v interface{}, def string) string {
	s := str(v)
	if s == "" {
		return def
	}
	return s
}

func intArg(a map[string]interface{}, k string, def int) int {
	v, ok := a[k]
	if !ok {
		return def
	}
	switch n := v.(type) {
	case float64:
		return int(n)
	case int:
		return n
	case string:
		var x int
		fmt.Sscanf(n, "%d", &x)
		return x
	}
	return def
}

func boolArg(a map[string]interface{}, k string, def bool) bool {
	v, ok := a[k]
	if !ok {
		return def
	}
	if b, ok := v.(bool); ok {
		return b
	}
	return def
}