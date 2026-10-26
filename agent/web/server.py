# agent/web/server.py
"""Local web UI for Omni (standard library only).

Security model:
  * binds to 127.0.0.1 by default,
  * requires a per-start token (URL once, then HttpOnly SameSite=Strict cookie),
  * checks the Host header (blocks DNS-rebinding),
  * never sends API keys to the browser (only masked tails),
  * refuses a filesystem root as the workspace (it would disable the
    outside-workspace approval guard; secret files are blocked separately
    in agent/safety.py).
  * /api/mcp_registry proxies the official MCP Registry (cached on disk);
    nothing from it is executed until the user clicks "Add & connect".

MCP endpoints (all under /api/mcp):
  GET    /api/mcp           -> {servers: [{name, ready, tools, error, command,
                                            args, url, env_keys, header_keys, enabled}]}
  POST   /api/mcp/add       -> add a new server; start + register tools
  POST   /api/mcp/update    -> edit an existing server; stop + restart
  POST   /api/mcp/delete    -> stop and remove a server (config + memory)
  POST   /api/mcp/start     -> start (already-configured) server
  POST   /api/mcp/stop      -> stop but keep config
  POST   /api/mcp/restart   -> stop + start
"""
import fnmatch
import hmac
import json
import mimetypes
import os
import re
import secrets
import string
import sys
import threading
import time
import webbrowser
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote

from .. import loop, llm, safety, commands
from ..providers import PRESETS, STRATEGIES
from ..tools import shell as _shell


def _find_index() -> Path:
    p = Path(__file__).with_name("index.html")
    if p.exists():
        return p
    base = Path(os.environ.get("OMNI_ROOT", Path.cwd()))
    for cand in (base / "agent" / "web" / "index.html", base / "index.html"):
        if cand.exists():
            return cand
    return p


INDEX = _find_index()
SID_RE = re.compile(r"^[a-f0-9]{8,32}$")
MAX_JSON = 5 * 1024 * 1024
MAX_UPLOAD = 50 * 1024 * 1024

# Don't accumulate thousands of streaming chunks per session.
MAX_EVENTS = 2000
MAX_POLL_EVENTS = 200
STREAM_COALESCE_SECONDS = 0.1

# Cap how much text a single message can render into the browser.
MAX_RENDER_CHARS = 20000

# How long a single approval dialog may block the agent.
APPROVAL_TIMEOUT = 120


def _validate_workspace(new_ws: Path, data_dir: Path) -> None:
    try:
        new_ws = new_ws.resolve()
    except OSError:
        pass
    if new_ws == new_ws.parent:
        raise ValueError(
            "refusing a filesystem root as the workspace "
            "(it would disable the outside-workspace approval guard)")


class Session:
    def __init__(self, cfg, sid, history=None):
        self.cfg, self.id = cfg, sid
        self.history = history or loop.new_history(cfg)
        self.events = []
        self.cond = threading.Condition()
        self.busy = False
        self.cancel = threading.Event()
        self.auto = bool(getattr(cfg, "auto_approve", False))
        self.approvals = {}
        self.allowed_tools = set()
        self.touched = time.time()
        self.deleted = False
        self._title = self._load_title()
        self._stream_buf = {}
        self._stream_last = {}

    @property
    def file(self):
        d = self.cfg.data_dir / "sessions"
        d.mkdir(exist_ok=True)
        return d / f"{self.id}.json"

    @property
    def meta_file(self):
        return self.file.with_suffix(".meta.json")

    def _load_title(self) -> str:
        try:
            if self.meta_file.exists():
                d = json.loads(self.meta_file.read_text(encoding="utf-8"))
                return str(d.get("title") or "")[:80]
        except Exception:
            pass
        return ""

    def set_title(self, text: str):
        self._title = (text or "").strip()[:80]
        try:
            self.meta_file.write_text(json.dumps({"title": self._title}), encoding="utf-8")
        except Exception:
            pass

    def title(self):
        if self._title:
            return self._title
        for m in self.history:
            if m.get("role") == "user":
                return str(m.get("content", ""))[:60]
        return "New chat"

    def emit(self, kind, text="", **extra):
        with self.cond:
            if kind == "tool_stream":
                sid = extra.get("id")
                if sid is not None:
                    buf = self._stream_buf.setdefault(sid, [])
                    buf.append(text)
                    now = time.time()
                    last = self._stream_last.get(sid, 0.0)
                    if now - last < STREAM_COALESCE_SECONDS:
                        return
                    text = "".join(buf)
                    buf.clear()
                    self._stream_last[sid] = now
            payload = {"type": kind, "text": text}
            if kind in ("assistant", "final") and "idx" not in extra:
                payload["idx"] = max(0, len(self.history) - 1)
            payload.update(extra)
            self.events.append(payload)
            self._cap_events()
            self.cond.notify_all()

    def _cap_events(self):
        if len(self.events) <= MAX_EVENTS:
            return
        overflow = len(self.events) - MAX_EVENTS
        drop = []
        for i, e in enumerate(self.events):
            if e.get("type") == "tool_stream":
                drop.append(i)
                if len(drop) >= overflow:
                    break
        if len(drop) < overflow:
            for i in range(overflow - len(drop)):
                if i < len(self.events):
                    drop.append(i)
        for i in sorted(set(drop), reverse=True):
            del self.events[i]

    def poll(self, after, wait):
        with self.cond:
            if len(self.events) <= after and wait > 0:
                self.cond.wait(wait)
            if after < 0:
                after = 0
            chunk = self.events[after:after + MAX_POLL_EVENTS]
            if after + MAX_POLL_EVENTS < len(self.events):
                chunk = [e for e in chunk if e.get("type") != "tool_stream"]
            return chunk, min(after + MAX_POLL_EVENTS, len(self.events))

    def save(self):
        if self.deleted:
            return
        try:
            self.file.write_text(json.dumps(self.history, ensure_ascii=False),
                                 encoding="utf-8")
        except Exception:
            pass

    def render(self):
        out = []
        for i, m in enumerate(self.history):
            if i == 0:
                continue
            r = m.get("role")
            if r == "user":
                out.append({"type": "user", "text": self._clip(m.get("content", "")), "idx": i})
            elif r == "assistant":
                calls = m.get("tool_calls") or []
                if m.get("content"):
                    out.append({"type": "assistant" if calls else "final",
                                "text": self._clip(m["content"]), "idx": i})
                for c in calls:
                    fn = c.get("function", {})
                    out.append({"type": "tool", "name": fn.get("name"),
                                "text": fn.get("arguments", ""),
                                "id": c.get("id"), "idx": i})
            elif r == "tool":
                t = str(m.get("content", ""))
                out.append({"type": "tool_result", "text": t[:1500],
                            "id": m.get("tool_call_id"),
                            "ok": not t.startswith("ERROR"), "idx": i})
        return out

    @staticmethod
    def _clip(text):
        s = str(text)
        if len(s) <= MAX_RENDER_CHARS:
            return s
        return (s[:MAX_RENDER_CHARS]
                + f"\n…[+{len(s) - MAX_RENDER_CHARS} chars — full text is in this chat's history]")

    def stop(self):
        self.cancel.set()
        try:
            _shell.kill_all()
        except Exception:
            pass
        for rec in list(self.approvals.values()):
            rec["allow"] = False
            rec["event"].set()
        with self.cond:
            self.cond.notify_all()


class App:
    def __init__(self, cfg, token, hosts):
        self.cfg, self.token, self.hosts = cfg, token, hosts
        self.sessions = {}
        self.lock = threading.Lock()
        # MCP: single source of truth for the on-disk config file. Guarded
        # by self.mcp_lock so concurrent add/update/delete calls can't
        # clobber each other.
        self.mcp_lock = threading.RLock()

    def session(self, sid, create=True):
        if not SID_RE.match(sid or ""):
            raise ValueError("bad session id")
        with self.lock:
            s = self.sessions.get(sid)
            if s is None:
                f = self.cfg.data_dir / "sessions" / f"{sid}.json"
                hist = None
                if f.exists():
                    try:
                        hist = json.loads(f.read_text(encoding="utf-8"))
                    except Exception:
                        hist = None
                elif not create:
                    raise KeyError(sid)
                s = self.sessions[sid] = Session(self.cfg, sid, hist)
            s.touched = time.time()
            return s

    def set_workspace(self, path: str) -> Path:
        new_ws = Path(path).expanduser().resolve()
        if not new_ws.is_dir():
            raise ValueError(f"not a directory: {new_ws}")
        _validate_workspace(new_ws, self.cfg.data_dir)
        self.cfg.workspace = new_ws
        with self.lock:
            for s in self.sessions.values():
                if s.history and s.history[0].get("role") == "system":
                    s.history[0]["content"] = loop.system_prompt(self.cfg)
                    s.save()
        return new_ws

    def run_chat(self, sess, text):
        def ask(action, tool_name=None):
            if sess.auto:
                return True
            if tool_name and tool_name in sess.allowed_tools:
                return True
            aid = secrets.token_hex(4)
            rec = {"action": action, "event": threading.Event(),
                   "allow": False, "remember": False, "tool": tool_name}
            sess.approvals[aid] = rec
            sess.emit("approval", action, id=aid, tool=tool_name)
            deadline = time.time() + APPROVAL_TIMEOUT
            while time.time() < deadline:
                if rec["event"].wait(0.25):
                    break
                if sess.cancel.is_set():
                    rec["allow"] = False
                    break
            sess.approvals.pop(aid, None)
            if rec["remember"] and tool_name:
                sess.allowed_tools.add(tool_name)
            sess.emit("approval_done", "", id=aid, allow=bool(rec["allow"]))
            return bool(rec["allow"])

        def work():
            try:
                safety.set_approver(lambda action, tool=None: ask(action, tool))
            except TypeError:
                safety.set_approver(ask)
            try:
                final = loop.run(self.cfg, sess.history, text,
                                 on_event=sess.emit, cancel=sess.cancel)
                final_idx = len(sess.history) - 1 if sess.history else None
                sess.emit("final", final, idx=final_idx)
            except llm.LLMError as e:
                sess.emit("error", str(e))
            except Exception as e:
                sess.emit("error", f"{type(e).__name__}: {e}")
            finally:
                sess.busy = False
                sess.save()
                sess.emit("done")

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ MCP

    def _mcp_config_path(self) -> Path:
        return self.cfg.data_dir / "mcp.json"

    def _mcp_read_config(self) -> dict:
        p = self._mcp_config_path()
        if not p.exists():
            return {"servers": {}}
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception:
            return {"servers": {}}
        if not isinstance(data, dict):
            return {"servers": {}}
        data.setdefault("servers", {})
        return data

    def _mcp_write_config(self, data: dict) -> None:
        p = self._mcp_config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        # ensure_ascii=False so env values that contain non-ASCII survive
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)

    def mcp_state(self) -> list:
        """One row per server, for the UI. Includes editable config."""
        mcp = getattr(self.cfg, "mcp", None)
        rows = []
        cfg_servers = self._mcp_read_config().get("servers", {})
        # Live servers first (in registry order), then config-only entries
        # that failed to start (so the user can edit/delete them).
        seen = set()
        if mcp:
            for name, srv in mcp.servers.items():
                seen.add(name)
                spec = cfg_servers.get(name, {}) or {}
                rows.append({
                    "name": name,
                    "ready": bool(srv.ready),
                    "tools": len(srv.tools or []),
                    "tool_names": [t.get("name") for t in (srv.tools or [])
                                   if isinstance(t, dict)],
                    "error": srv.last_error,
                    "command": " ".join(getattr(srv, "cmd", []) or []),
                    "args": list(getattr(srv, "cmd", [])[1:] if getattr(srv, "cmd", None) else []),
                    "url": getattr(srv, "url", "") or "",
                    "env_keys": sorted((spec.get("env") or {}).keys()),
                    "header_keys": sorted((spec.get("headers") or {}).keys()),
                    "enabled": spec.get("enabled", True),
                    "transport": "http" if getattr(srv, "transport", "stdio") == "http" else "stdio",
                })
        for name, spec in cfg_servers.items():
            if name in seen or not isinstance(spec, dict):
                continue
            rows.append({
                "name": name,
                "ready": False,
                "tools": 0,
                "tool_names": [],
                "error": "not loaded (start it to use it)",
                "command": spec.get("command") or "",
                "args": list(spec.get("args") or []),
                "url": spec.get("url") or "",
                "env_keys": sorted((spec.get("env") or {}).keys()),
                "header_keys": sorted((spec.get("headers") or {}).keys()),
                "enabled": spec.get("enabled", True),
                "transport": "http" if spec.get("url") else "stdio",
            })
        return rows

    def _mcp_reload_tools(self):
        from ..tools import mcp_bridge
        mcp_bridge._registered_for = None
        try:
            mcp_bridge.register(self.cfg)
        except Exception:
            pass

    def mcp_add(self, spec: dict) -> dict:
        name = (spec.get("name") or "").strip()
        command = (spec.get("command") or "").strip()
        url = (spec.get("url") or "").strip()
        if not name:
            raise ValueError("name is required")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.\-]*", name):
            raise ValueError("name must start with a letter and contain only letters, digits, _, ., -")
        if not command and not url:
            raise ValueError("either command (local stdio) or url (remote HTTP) is required")

        args = list(spec.get("args") or [])
        env = dict(spec.get("env") or {})
        headers = dict(spec.get("headers") or {})
        cwd = spec.get("cwd") or None

        from ..mcp import MCPServer
        srv = MCPServer(name, command or None, args, env, cwd,
                        url=url or None, headers=headers)
        try:
            srv.start()
        except Exception as e:
            raise ValueError(f"could not start '{name}': {e}")

        with self.mcp_lock:
            data = self._mcp_read_config()
            entry = {
                "command": command or None,
                "url": url or None,
                "args": args,
                "env": env,
                "headers": headers,
                "enabled": True,
            }
            if cwd:
                entry["cwd"] = cwd
            data["servers"][name] = entry
            self._mcp_write_config(data)
            if not getattr(self.cfg, "mcp", None):
                from ..mcp import MCPRegistry
                self.cfg.mcp = MCPRegistry()
            self.cfg.mcp.servers[name] = srv
            self._mcp_reload_tools()
        return {"name": name, "tools": len(srv.tools or [])}

    def mcp_update(self, old_name: str, spec: dict) -> dict:
        old_name = (old_name or "").strip()
        new_name = (spec.get("name") or old_name).strip()
        if not old_name:
            raise ValueError("old_name is required")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.\-]*", new_name):
            raise ValueError("name must start with a letter and contain only letters, digits, _, ., -")
        command = (spec.get("command") or "").strip()
        url = (spec.get("url") or "").strip()
        if not command and not url:
            raise ValueError("either command (local stdio) or url (remote HTTP) is required")
        args = list(spec.get("args") or [])
        env = dict(spec.get("env") or {})
        headers = dict(spec.get("headers") or {})
        cwd = spec.get("cwd") or None

        with self.mcp_lock:
            # stop old if present
            if getattr(self.cfg, "mcp", None):
                old_srv = self.cfg.mcp.servers.pop(old_name, None)
                if old_srv:
                    try:
                        old_srv.stop()
                    except Exception:
                        pass
            data = self._mcp_read_config()
            data["servers"].pop(old_name, None)
            self._mcp_write_config(data)
        # Now add as a fresh server under the (possibly new) name.
        res = self.mcp_add({
            "name": new_name, "command": command, "url": url,
            "args": args, "env": env, "headers": headers, "cwd": cwd,
        })
        return res

    def mcp_delete(self, name: str) -> None:
        name = (name or "").strip()
        if not name:
            raise ValueError("name is required")
        with self.mcp_lock:
            if getattr(self.cfg, "mcp", None):
                srv = self.cfg.mcp.servers.pop(name, None)
                if srv:
                    try:
                        srv.stop()
                    except Exception:
                        pass
            data = self._mcp_read_config()
            data["servers"].pop(name, None)
            self._mcp_write_config(data)
            self._mcp_reload_tools()

    def mcp_start(self, name: str) -> dict:
        name = (name or "").strip()
        mcp = getattr(self.cfg, "mcp", None)
        srv = mcp.servers.get(name) if mcp else None
        if not srv:
            # Build one from config.
            data = self._mcp_read_config()
            spec = data.get("servers", {}).get(name)
            if not spec:
                raise ValueError(f"no such MCP server: {name}")
            from ..mcp import MCPServer
            srv = MCPServer(name, spec.get("command"), list(spec.get("args") or []),
                            dict(spec.get("env") or {}), spec.get("cwd"),
                            url=spec.get("url"), headers=dict(spec.get("headers") or {}))
            if not mcp:
                from ..mcp import MCPRegistry
                mcp = self.cfg.mcp = MCPRegistry()
            mcp.servers[name] = srv
        srv.stop()
        srv.start()
        self._mcp_reload_tools()
        return {"name": name, "tools": len(srv.tools or [])}

    def mcp_stop(self, name: str) -> None:
        mcp = getattr(self.cfg, "mcp", None)
        srv = mcp.servers.get(name) if mcp else None
        if srv:
            srv.stop()

    def state(self):
        pm = self.cfg.providers
        return {
            "workspace": str(self.cfg.workspace),
            "home": str(self.cfg.home) if self.cfg.home else "",
            "strategy": pm.strategy,
            "strategies": list(STRATEGIES),
            "max_wait": pm.max_wait,
            "pools": pm.summary(),
            "load_error": pm.load_error,
            "presets": [{"name": k, "base_url": v[0], "model": v[1]} for k, v in PRESETS.items()],
            "auto_approve_env": self.cfg.auto_approve,
            "strict": self.cfg.strict,
            "shell_bg_default": _shell.default_background(),
            "mcp": self.mcp_state(),
            "mcp_load_errors": list(getattr(self.cfg.mcp, "load_errors", [])) if self.cfg.mcp else [],
        }

    def probe(self, names):
        pm, out = self.cfg.providers, []
        for n in names:
            p = pm.get(n)
            ok, kind, text = llm.probe(self.cfg, p)
            out.append({"name": n, "ok": ok, "kind": kind, "text": text})
        return out


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        server_version = "OmniUI/2.1"
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body=b"", ctype="application/json", extra=()):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode()
            elif isinstance(body, str):
                body = body.encode()
            if code >= 400:
                self.close_connection = True
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _err(self, code, msg):
            self._send(code, {"error": msg})

        def _cookie_ok(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "omni_token" and hmac.compare_digest(v, app.token):
                    return True
            return False

        def _host_ok(self):
            if not app.hosts:
                return True
            return (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower() in app.hosts

        def _body(self, limit=MAX_JSON):
            n = int(self.headers.get("Content-Length") or 0)
            if n > limit:
                raise ValueError("request too large")
            return self.rfile.read(n) if n else b""

        def _json_body(self):
            raw = self._body()
            if not raw:
                return {}
            # utf-8-sig tolerates a BOM, which some clipboard buffers insert.
            return json.loads(raw.decode("utf-8-sig"))

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def _route(self, method):
            try:
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                if not self._host_ok():
                    return self._err(403, "bad host")
                if u.path == "/" and method == "GET":
                    t = q.get("token", "")
                    if t and hmac.compare_digest(t, app.token):
                        return self._send(302, b"", "text/plain", [
                            ("Location", "/"),
                            ("Set-Cookie", f"omni_token={app.token}; Path=/; HttpOnly; SameSite=Strict")])
                    if not self._cookie_ok():
                        return self._send(401, "Open the exact link printed in the terminal when you ran:  omni ui\n",
                                          "text/plain; charset=utf-8")
                    return self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
                if not u.path.startswith("/api/"):
                    return self._err(404, "not found")
                if not self._cookie_ok():
                    return self._err(401, "not authorised - open the link from the terminal again")
                if method == "POST" and self.headers.get("X-Omni") != "1":
                    return self._err(403, "missing header")
                fn = getattr(self, "api_" + u.path[5:].replace("/", "_"), None)
                if not fn or method != getattr(fn, "method", "POST"):
                    return self._err(404, "no such endpoint")
                fn(q)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except KeyError as e:
                self._err(404, f"unknown: {e}")
            except (ValueError, llm.LLMError, json.JSONDecodeError) as e:
                self._err(400, str(e))
            except Exception as e:
                self._err(500, f"{type(e).__name__}: {e}")

        # ---- reads
        def api_state(self, q):
            self._send(200, app.state())
        api_state.method = "GET"

        def api_models(self, q):
            pool = q.get("pool", "")
            pm = app.cfg.providers
            try:
                ids = llm.list_models(app.cfg, pool, force=q.get("refresh") == "1")
                cur = pm.model_of(pool)
                self._send(200, {"models": ids, "listed": cur in ids if cur else None})
            except llm.LLMError as e:
                self._send(200, {"models": [], "listed": None, "note": str(e)})
        api_models.method = "GET"

        def api_poll(self, q):
            s = app.session(q.get("session", ""), create=False)
            after = int(q.get("after", 0))
            evs, nxt = s.poll(after, min(float(q.get("wait", 15)), 25))
            self._send(200, {"events": evs, "next": nxt, "busy": s.busy})
        api_poll.method = "GET"

        def api_history(self, q):
            s = app.session(q.get("session", ""))
            pending = [{"id": k, "action": v["action"], "tool": v.get("tool")}
                       for k, v in s.approvals.items()]
            self._send(200, {"items": s.render(), "cursor": len(s.events),
                             "busy": s.busy, "pending": pending,
                             "auto": s.auto, "strict": app.cfg.strict,
                             "allowed_tools": sorted(s.allowed_tools),
                             "title": s.title()})
        api_history.method = "GET"

        def api_sessions(self, q):
            d = app.cfg.data_dir / "sessions"
            out = []
            if d.exists():
                for f in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:40]:
                    if f.name.endswith(".meta.json"):
                        continue
                    try:
                        hist = json.loads(f.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    sid = f.stem
                    s = app.sessions.get(sid)
                    title = s.title() if s else next(
                        (str(m["content"])[:60] for m in hist if m.get("role") == "user"), "")
                    if not title:
                        continue
                    out.append({
                        "id": sid,
                        "title": title,
                        "mtime": int(f.stat().st_mtime),
                        "busy": bool(s and s.busy),
                    })
            self._send(200, out)
        api_sessions.method = "GET"

        def api_files(self, q):
            ws, out = app.cfg.workspace, []
            for p in sorted(ws.rglob("*")):
                rel = p.relative_to(ws)
                if p.is_file() and not any(x.startswith(".") or x in ("__pycache__", "node_modules")
                                           for x in rel.parts):
                    st = p.stat()
                    out.append({"path": rel.as_posix(), "size": st.st_size,
                                "mtime": int(st.st_mtime)})
                if len(out) >= 500:
                    break
            self._send(200, out)
        api_files.method = "GET"

        def api_file(self, q):
            p = (app.cfg.workspace / q.get("path", "")).resolve()
            if app.cfg.workspace not in p.parents or not p.is_file():
                return self._err(404, "no such file")
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            self._send(200, p.read_bytes(), ctype,
                       [("Content-Disposition", f"attachment; filename*=UTF-8''{quote(p.name)}")])
        api_file.method = "GET"

        # ---- chat
        def api_chat(self, q):
            b = self._json_body()
            s = app.session(b.get("session", ""))
            text = (b.get("text") or "").strip()
            if not text:
                raise ValueError("empty message")
            if s.busy:
                return self._err(409, "this chat is still working")
            if not app.cfg.providers.items:
                raise ValueError("No API keys yet - open the Keys panel and add one.")
            s.cancel.clear()
            s.busy = True
            user_idx = len(s.history)
            s.emit("user", text, idx=user_idx)
            app.run_chat(s, text)
            self._send(200, {"ok": True, "cursor": len(s.events), "user_idx": user_idx})

        def api_chat_retry(self, q):
            b = self._json_body()
            s = app.session(b.get("session", ""))
            if s.busy:
                return self._err(409, "this chat is still working")
            idx = -1
            for i in range(len(s.history) - 1, -1, -1):
                if s.history[i].get("role") == "user":
                    idx = i
                    break
            if idx == -1:
                return self._err(400, "nothing to retry")
            text = s.history[idx]["content"]
            s.history = s.history[:idx]
            s.save()
            s.cancel.clear()
            s.busy = True
            s.emit("user", text, idx=idx)
            app.run_chat(s, text)
            self._send(200, {"ok": True, "cursor": len(s.events)})

        def api_chat_edit(self, q):
            b = self._json_body()
            s = app.session(b.get("session", ""))
            if s.busy:
                return self._err(409, "this chat is still working")
            idx, text = b.get("index"), (b.get("text") or "").strip()
            if idx is None or not (0 <= idx < len(s.history)):
                return self._err(400, "invalid index")
            if s.history[idx].get("role") != "user":
                return self._err(400, "you can only edit user messages")
            if not text:
                return self._err(400, "empty message")
            s.history = s.history[:idx]
            s.save()
            s.cancel.clear()
            s.busy = True
            s.emit("user", text, idx=idx)
            app.run_chat(s, text)
            self._send(200, {"ok": True, "cursor": len(s.events)})

        def api_stop(self, q):
            app.session(self._json_body().get("session", "")).stop()
            self._send(200, {"ok": True})

        def api_approve(self, q):
            b = self._json_body()
            s = app.session(b.get("session", ""))
            rec = s.approvals.get(b.get("id"))
            if rec:
                rec["allow"] = bool(b.get("allow"))
                rec["remember"] = bool(b.get("remember"))
                rec["event"].set()
            self._send(200, {"ok": bool(rec)})

        def api_auto(self, q):
            b = self._json_body()
            s = app.session(b.get("session", ""))
            s.auto = bool(b.get("on"))
            self._send(200, {"auto": s.auto})

        def api_strict(self, q):
            b = self._json_body()
            app.cfg.strict = bool(b.get("on"))
            self._send(200, {"strict": app.cfg.strict})

        def api_shell_bg(self, q):
            b = self._json_body()
            if "on" in b:
                _shell.set_default_background(bool(b["on"]))
            self._send(200, {"on": _shell.default_background()})

        def api_session_delete(self, q):
            sid = self._json_body().get("session", "")
            s = app.session(sid)
            s.stop()
            s.deleted = True
            app.sessions.pop(sid, None)
            for f in (s.file, s.meta_file):
                try:
                    f.unlink()
                except FileNotFoundError:
                    pass
            self._send(200, {"ok": True})

        def api_session_rename(self, q):
            b = self._json_body()
            sid = b.get("session", "")
            title = (b.get("title") or "").strip()
            if not title:
                raise ValueError("title is empty")
            s = app.session(sid)
            s.set_title(title)
            self._send(200, {"ok": True, "title": s.title()})

        def api_message_delete(self, q):
            b = self._json_body()
            s = app.session(b.get("session", ""))
            if s.busy:
                return self._err(409, "this chat is still working")
            idx = b.get("index")
            if idx is None or not (0 <= idx < len(s.history)):
                return self._err(400, "invalid index")
            if idx == 0:
                return self._err(400, "cannot delete the system prompt")
            if s.history[idx].get("role") not in ("user", "assistant"):
                return self._err(400, "can only delete user/assistant messages")
            s.history.pop(idx)
            s.save()
            self._send(200, {"ok": True})

        def api_upload(self, q):
            name = Path(q.get("name", "upload.bin")).name or "upload.bin"
            data = self._body(MAX_UPLOAD)
            dest = app.cfg.workspace / name
            i = 1
            while dest.exists():
                dest = app.cfg.workspace / f"{Path(name).stem} ({i}){Path(name).suffix}"
                i += 1
            dest.write_bytes(data)
            self._send(200, {"path": dest.name, "size": len(data)})

        # ---- shell jobs
        def api_shell_jobs(self, q):
            from ..tools import shell as _sh
            self._send(200, {"jobs": _sh.list_detached(), "bg_default": _sh.default_background()})
        api_shell_jobs.method = "GET"

        def api_shell_jobs_kill(self, q):
            b = self._json_body()
            from ..tools import shell as _sh
            self._send(200, {"ok": True, "result": _sh.kill_detached(b.get("job_id", ""))})

        # ---- keys / models
        def api_pool_model(self, q):
            b = self._json_body()
            pm = app.cfg.providers
            pool = pm.set_model(b["pool"], b["model"])
            self._send(200, {"ok": True, "listed": llm.model_listed(app.cfg, pool, b["model"])})

        def api_pool_fallbacks(self, q):
            b = self._json_body()
            models = b.get("models", [])
            if isinstance(models, str):
                models = re.split(r"[,\s]+", models)
            app.cfg.providers.set_fallbacks(b["pool"], models)
            self._send(200, {"ok": True})

        def api_pool_order(self, q):
            app.cfg.providers.order_pools(self._json_body()["names"])
            self._send(200, {"ok": True})

        def api_pool_remove(self, q):
            app.cfg.providers.remove_pool(self._json_body()["pool"])
            self._send(200, {"ok": True})

        def api_strategy(self, q):
            app.cfg.providers.set_strategy(self._json_body()["strategy"])
            self._send(200, {"ok": True})

        def api_keys_add(self, q):
            b = self._json_body()
            keys = [k.strip("'\" ") for k in re.split(r"[\s,;]+", b.get("keys", ""))
                    if k.strip("'\" ") and not k.strip().startswith("#")]
            if not keys:
                raise ValueError("paste at least one key")
            pool = (b.get("pool") or "").strip()
            if not pool:
                from ..providers import detect_provider
                pool = detect_provider(keys[0]) or ""
            if pool and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.\-]*", pool):
                raise ValueError("pool name: letters/digits only")
            added, dup = app.cfg.providers.add_keys(pool, keys,
                                                    (b.get("base_url") or None),
                                                    (b.get("model") or None))
            self._send(200, {"added": len(added), "duplicates": dup})

        def api_keys_remove(self, q):
            app.cfg.providers.remove(self._json_body()["name"])
            self._send(200, {"ok": True})

        def api_keys_revive(self, q):
            b = self._json_body()
            app.cfg.providers.health.clear(name=b.get("name"), pool=b.get("pool"))
            self._send(200, {"ok": True})

        def api_keys_test(self, q):
            b = self._json_body()
            pm = app.cfg.providers
            if b.get("name"):
                names = [b["name"]]
            elif b.get("pool"):
                names = [p.name for p in pm.providers_in(b["pool"])]
            else:
                raise ValueError("name or pool required")
            self._send(200, {"results": app.probe(names)})

        def api_key_models(self, q):
            name = q.get("name", "")
            pm = app.cfg.providers
            p = pm.get(name)
            fresh = (time.time() - (p.models_fetched or 0)) < 600
            if q.get("refresh") == "1" or not p.models or not fresh:
                try:
                    pm.refresh_models(name, force=True)
                except Exception as e:
                    return self._send(200, {"models": p.models or [], "current": p.model,
                                            "error": str(e), "fetched": p.models_fetched})
            self._send(200, {"models": p.models or [], "current": p.model,
                             "fetched": p.models_fetched})
        api_key_models.method = "GET"

        def api_refresh_all(self, q):
            out = app.cfg.providers.refresh_all_models()
            summary = {k: (len(v) if isinstance(v, list) else str(v)) for k, v in out.items()}
            by_pool = {}
            for pool in app.cfg.providers.pool_names():
                try:
                    by_pool[pool] = llm.list_models(app.cfg, pool)
                except Exception:
                    by_pool[pool] = []
            self._send(200, {"refreshed": summary, "models": by_pool})

        def api_key_model(self, q):
            b = self._json_body()
            app.cfg.providers.set_model_for_key(b["name"], b["model"])
            self._send(200, {"ok": True})

        def api_detect(self, q):
            from ..providers import detect_provider
            key = self._json_body().get("key", "")
            self._send(200, {"provider": detect_provider(key)})

        def api_browse(self, q):
            path = q.get("path") or str(app.cfg.workspace)
            search = q.get("search", "").strip()
            p = Path(path).expanduser()
            try:
                p = p.resolve()
            except OSError:
                p = Path.home()
            if p.is_file():
                p = p.parent
            if not p.is_dir():
                p = Path.home()
            entries = []
            try:
                if search:
                    pat = f"*{search}*"
                    count = 0
                    for root, dirs, files in os.walk(p):
                        dirs[:] = [d for d in dirs
                                   if d not in ("node_modules", "__pycache__", ".git", ".venv")]
                        if q.get("hidden") != "1":
                            dirs[:] = [d for d in dirs if not d.startswith(".")]
                        for name in files:
                            if name.startswith(".") and q.get("hidden") != "1":
                                continue
                            if not fnmatch.fnmatch(name, pat):
                                continue
                            full = Path(root) / name
                            try:
                                st = full.stat()
                            except OSError:
                                continue
                            entries.append({"name": str(full.relative_to(p)),
                                            "path": str(full),
                                            "dir": False, "size": st.st_size,
                                            "mtime": int(st.st_mtime)})
                            count += 1
                            if count >= 200:
                                break
                        if count >= 200:
                            break
                else:
                    for c in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                        if c.name.startswith(".") and q.get("hidden") != "1":
                            continue
                        try:
                            st = c.stat()
                            entries.append({"name": c.name, "path": str(c),
                                            "dir": c.is_dir(), "size": st.st_size,
                                            "mtime": int(st.st_mtime)})
                        except OSError:
                            continue
            except PermissionError:
                return self._err(403, "permission denied")
            drives = []
            if os.name == "nt":
                for d in string.ascii_uppercase:
                    if Path(f"{d}:\\").exists():
                        drives.append(f"{d}:\\")
            else:
                drives = ["/", str(Path.home())]
            self._send(200, {
                "path": str(p),
                "parent": str(p.parent) if p.parent != p else None,
                "entries": entries[:2000],
                "drives": drives,
            })
        api_browse.method = "GET"

        def api_workspace_set(self, q):
            b = self._json_body()
            new_ws = app.set_workspace(b.get("path", ""))
            self._send(200, {"workspace": str(new_ws)})

        def api_workspace_pick(self, q):
            try:
                import tkinter as tk
                from tkinter import filedialog
            except Exception as e:
                return self._err(501, f"no GUI available on the server: {e}")
            try:
                root = tk.Tk()
            except Exception as e:
                return self._err(501, f"no display on the server: {e}")
            root.withdraw()
            try:
                root.attributes("-topmost", True)
            except Exception:
                pass
            try:
                chosen = filedialog.askdirectory(
                    initialdir=str(app.cfg.workspace),
                    title="Choose the Omni workspace folder",
                    mustexist=True,
                )
            finally:
                try:
                    root.destroy()
                except Exception:
                    pass
            if not chosen:
                return self._send(200, {"cancelled": True})
            try:
                new_ws = app.set_workspace(chosen)
            except ValueError as e:
                return self._err(400, str(e))
            self._send(200, {"workspace": str(new_ws)})

        # ---- MCP: CRUD
        def api_mcp(self, q):
            self._send(200, {"servers": app.mcp_state()})
        api_mcp.method = "GET"

        def api_mcp_add(self, q):
            b = self._json_body()
            self._send(200, app.mcp_add(b))

        def api_mcp_update(self, q):
            b = self._json_body()
            old = b.get("old_name") or b.get("name") or ""
            self._send(200, app.mcp_update(old, b))

        def api_mcp_delete(self, q):
            b = self._json_body()
            app.mcp_delete(b.get("name", ""))
            self._send(200, {"ok": True})

        def api_mcp_start(self, q):
            b = self._json_body()
            self._send(200, app.mcp_start(b.get("name", "")))

        def api_mcp_stop(self, q):
            b = self._json_body()
            app.mcp_stop(b.get("name", ""))
            self._send(200, {"ok": True})

        def api_mcp_restart(self, q):
            b = self._json_body()
            name = b.get("name", "")
            app.mcp_stop(name)
            self._send(200, app.mcp_start(name))

        def api_mcp_registry(self, q):
            from .. import mcp_registry
            cache = app.cfg.data_dir / "mcp_registry.json"
            if q.get("refresh") == "1":
                try:
                    if cache.exists():
                        cache.unlink()
                except OSError:
                    pass
            installed = set()
            if getattr(app.cfg, "mcp", None):
                installed = set(app.cfg.mcp.servers.keys())
            try:
                limit = min(max(1, int(q.get("limit", 200))), 500)
            except ValueError:
                limit = 200
            try:
                rows = mcp_registry.search(cache, q.get("q", ""),
                                           limit=limit, installed=installed)
                self._send(200, {"servers": rows, "cached": cache.exists()})
            except Exception as e:
                # Never a 500 for a flaky third party.
                self._send(200, {"servers": [], "error": str(e)})
        api_mcp_registry.method = "GET"

    return H


def serve(cfg, host="127.0.0.1", port=8765, open_browser=True, on_url=None):
    # If the default port is used, try to make it "sticky" to the workspace
    # so different Omni installations don't clash by default.
    if port == 8765:
        try:
            path_hash = int(hashlib.md5(str(cfg.data_dir).encode()).hexdigest(), 16)
            port = 8000 + (path_hash % 1000)
        except Exception:
            pass

    token = os.environ.get("AGENT_UI_TOKEN") or secrets.token_urlsafe(18)
    loopback = host in ("127.0.0.1", "localhost", "::1")
    hosts = {"127.0.0.1", "localhost", "::1"} if loopback else None
    app = App(cfg, token, hosts)
    srv = None
    
    # Try up to 100 ports starting from the preferred one
    for p in range(port, port + 100):
        try:
            srv = ThreadingHTTPServer((host, p), make_handler(app))
            port = p
            break
        except OSError:
            continue
    
    if srv is None:
        raise SystemExit(f"No free port found in range {port} to {port + 100}")
    srv.daemon_threads = True
    url = f"http://{'localhost' if loopback else host}:{port}/?token={token}"
    print(f"Omni UI running.  Open:  {url}\n(keep this link private - it unlocks the agent; Ctrl+C to stop)")
    if not loopback:
        print("WARNING: listening beyond this computer. Anyone with the link can run commands as you.")
    if cfg.providers.load_error:
        print("WARNING:", cfg.providers.load_error)
    if getattr(cfg, "mcp", None) and cfg.mcp.servers:
        names = ", ".join(f"{n}({len(s.tools)})" for n, s in cfg.mcp.servers.items())
        print(f"MCP servers: {names}")
    if on_url is not None:
        try:
            on_url(url)
        except Exception:
            pass
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        srv.server_close()
        if getattr(cfg, "mcp", None):
            cfg.mcp.stop_all()