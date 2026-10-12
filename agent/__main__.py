import argparse
import sys
import time
from . import config, loop, llm, commands


def chat_repl(cfg):
    p0 = cfg.providers.items[0].name if cfg.providers.items else "none"
    print(f"Omni agent  |  primary={p0}  |  providers={len(cfg.providers.items)}\nworkspace={cfg.workspace}\n"
          "Type /help for provider/key/model commands (failover is automatic).\n")
    hist = loop.new_history(cfg)
    while True:
        try:
            q = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not q:
            continue
        if q in ("/exit", "/quit"):
            return
        if q == "/reset":
            hist = loop.new_history(cfg)
            print("history cleared")
            continue
        if q.startswith("/"):
            print(commands.handle(cfg, q))
            continue
        try:
            print("\n" + loop.run(cfg, hist, q) + "\n")
        except llm.LLMError as e:
            print(f"[gateway error] {e}")


def main():
    ap = argparse.ArgumentParser(prog="omni-agent")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("chat", help="interactive session (default)")
    r = sub.add_parser("run", help="one-shot task, then exit")
    r.add_argument("prompt", nargs="?")
    r.add_argument("-f", "--file", help="read the task from a file (e.g. workflows/daily.md)")
    s = sub.add_parser("schedule", help="repeat a task on an interval")
    s.add_argument("-f", "--file", required=True)
    s.add_argument("--every", type=int, required=True, help="seconds between runs")
    sub.add_parser("check", help="test the primary provider")
    pv = sub.add_parser("provider", help="manage providers: list | add | key | model | use | order | remove | test")
    pv.add_argument("rest", nargs="*")
    a = ap.parse_args()
    cfg = config.load()

    if a.cmd in (None, "chat"):
        return chat_repl(cfg)
    if a.cmd == "check":
        try:
            print("OK:", llm.chat(cfg, [{"role": "user", "content": "Reply with the word pong."}]).get("content"))
        except llm.LLMError as e:
            print("FAILED:", e)
            sys.exit(1)
        return
    if a.cmd == "provider":
        import shlex
        r = a.rest or ["list"]
        print(commands.handle(cfg, "/" + " ".join(shlex.quote(x) for x in r)))
        return
    if a.cmd == "run":
        task = open(a.file, encoding="utf-8").read() if a.file else a.prompt
        if not task:
            ap.error("give a prompt or -f file")
        print(loop.run(cfg, loop.new_history(cfg), task))
        return
    if a.cmd == "schedule":
        task = open(a.file, encoding="utf-8").read()
        while True:
            print(f"\n[{time.strftime('%F %T')}] running {a.file}")
            try:
                print(loop.run(cfg, loop.new_history(cfg), task))
            except Exception as e:
                print("run failed:", e)
            time.sleep(a.every)


if __name__ == "__main__":
    main()
