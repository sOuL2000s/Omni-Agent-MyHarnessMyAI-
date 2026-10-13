package main

import (
	"bufio"
	"flag"
	"fmt"
	"os"
	"strings"
)

func main() {
	cfg := LoadConfig()
	if len(os.Args) < 2 {
		chatREPL(cfg)
		return
	}
	switch os.Args[1] {
	case "ui":
		fs := flag.NewFlagSet("ui", flag.ExitOnError)
		host := fs.String("host", "127.0.0.1", "bind address")
		port := fs.Int("port", 8765, "port")
		fs.Parse(os.Args[2:])
		Serve(cfg, *host, *port)
	case "chat":
		chatREPL(cfg)
	case "run":
		if len(os.Args) < 3 {
			fmt.Println(`usage: omni run "prompt"`)
			os.Exit(1)
		}
		task := strings.Join(os.Args[2:], " ")
		hist := NewHistory(cfg)
		out := Run(cfg, &hist, task, cliEvent)
		fmt.Println("\n" + out)
	case "models":
		if len(os.Args) < 3 {
			fmt.Println("usage: omni models <pool>")
			os.Exit(1)
		}
		status := cfg.Providers.RefreshModels(os.Args[2])
		for name, st := range status {
			fmt.Printf("  %s: %s\n", name, st)
		}
	default:
		fmt.Printf("Unknown command %q. Try: ui | chat | run | models\n", os.Args[1])
	}
}

func chatREPL(cfg *Config) {
	pools := cfg.Providers.Pools()
	fmt.Printf("Omni agent | pools=%v | keys=%d\n", pools, len(cfg.Providers.Items))
	fmt.Println("Type /exit to quit. Prefer a browser? run: omni ui")
	hist := NewHistory(cfg)
	sc := bufio.NewScanner(os.Stdin)
	for {
		fmt.Print("you> ")
		if !sc.Scan() {
			break
		}
		q := strings.TrimSpace(sc.Text())
		if q == "" {
			continue
		}
		if q == "/exit" || q == "/quit" {
			return
		}
		if q == "/reset" {
			hist = NewHistory(cfg)
			fmt.Println("history cleared")
			continue
		}
		out := Run(cfg, &hist, q, cliEvent)
		fmt.Println("\n" + out + "\n")
	}
}

func cliEvent(kind, text string, extra map[string]interface{}) {
	switch kind {
	case "stream":
		fmt.Print(text)
	case "tool":
		fmt.Printf("\n  → %v(%s)\n", extra["name"], text)
	case "notice":
		fmt.Printf("  ! %s\n", text)
	case "assistant":
		fmt.Printf("  · %s\n", text)
	case "route":
		fmt.Printf("  [%s]\n", text)
	case "error":
		fmt.Printf("  ✗ %s\n", text)
	}
}