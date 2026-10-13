package main

import (
	"fmt"
	"os"
	"path/filepath"
	"time"
)

func ResolvePath(cfg *Config, path string) (string, error) {
	if path == "" {
		return "", fmt.Errorf("empty path")
	}
	if path[0] == '~' {
		home, _ := os.UserHomeDir()
		path = filepath.Join(home, path[1:])
	}
	return filepath.Abs(path)
}

func BackupFile(cfg *Config, path string) {
	data, err := os.ReadFile(path)
	if err != nil {
		return
	}
	rel := filepath.Base(path)
	dest := filepath.Join(cfg.BackupDir, fmt.Sprintf("%s.%d", rel, time.Now().UnixNano()))
	_ = os.WriteFile(dest, data, 0o644)
}

func Approve(cfg *Config, action string) bool {
	if cfg.AutoApprove {
		return true
	}
	if Approver != nil {
		return Approver(action)
	}
	fmt.Printf("\n[approve?] %s\n  (y/N) > ", action)
	var ans string
	fmt.Scanln(&ans)
	return ans == "y" || ans == "yes"
}

var Approver func(string) bool