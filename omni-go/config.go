package main

import (
	"bufio"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

type Config struct {
	BaseURL       string
	APIKey        string
	Model         string
	Workspace     string
	AllowOutside  bool
	AutoApprove   bool
	MaxTurns      int
	Temperature   float64
	Timeout       int
	DataDir       string
	BackupDir     string
	MaxWait       int
	Strategy      string
	Providers     *Manager
}

func homeDir() string {
	h, _ := os.UserHomeDir()
	return h
}

func loadEnvFile(path string) map[string]string {
	out := map[string]string{}
	f, err := os.Open(path)
	if err != nil {
		return out
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if i := strings.Index(line, " #"); i >= 0 {
			line = strings.TrimSpace(line[:i])
		}
		if i := strings.Index(line, "="); i > 0 {
			k := strings.TrimSpace(line[:i])
			v := strings.Trim(strings.TrimSpace(line[i+1:]), `"'`)
			out[k] = v
		}
	}
	return out
}

func getenv(m map[string]string, k, def string) string {
	if v, ok := m[k]; ok && v != "" {
		return v
	}
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func LoadConfig() *Config {
	root, _ := os.Executable()
	rootDir := filepath.Dir(root)
	env := loadEnvFile(filepath.Join(rootDir, ".env"))
	env2 := loadEnvFile(filepath.Join(".", ".env"))
	for k, v := range env2 {
		env[k] = v
	}

	dataDir := filepath.Join(homeDir(), ".omni")
	_ = os.MkdirAll(dataDir, 0o755)
	backupDir := filepath.Join(dataDir, "backups")
	_ = os.MkdirAll(backupDir, 0o755)

	ws := getenv(env, "AGENT_WORKSPACE", ".")
	wsAbs, _ := filepath.Abs(ws)
	_ = os.MkdirAll(wsAbs, 0o755)

	maxTurns, _ := strconv.Atoi(getenv(env, "AGENT_MAX_TURNS", "40"))
	temp, _ := strconv.ParseFloat(getenv(env, "AGENT_TEMPERATURE", "0.2"), 64)
	timeout, _ := strconv.Atoi(getenv(env, "AGENT_TIMEOUT", "180"))
	maxWait, _ := strconv.Atoi(getenv(env, "AGENT_MAX_WAIT", "90"))

	cfg := &Config{
		BaseURL:      strings.TrimRight(getenv(env, "AGENT_BASE_URL", "http://localhost:20128/v1"), "/"),
		APIKey:       getenv(env, "AGENT_API_KEY", ""),
		Model:        getenv(env, "AGENT_MODEL", "auto"),
		Workspace:    wsAbs,
		AllowOutside: getenv(env, "AGENT_ALLOW_OUTSIDE", "1") == "1",
		AutoApprove:  getenv(env, "AGENT_AUTO_APPROVE", "0") == "1",
		MaxTurns:     maxTurns,
		Temperature:  temp,
		Timeout:      timeout,
		DataDir:      dataDir,
		BackupDir:    backupDir,
		MaxWait:      maxWait,
		Strategy:     getenv(env, "AGENT_STRATEGY", "round_robin"),
	}
	cfg.Providers = NewManager(cfg)
	return cfg
}