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

Additional endpoints added for the 2.2 UX pass:
  GET    /api/state?lite=1     -> pools + keys without per-key model lists
  GET    /api/sessions?q=...   -> server-side search across all session files
  GET    /api/tool_result?session=&id= -> full tool output from history
  POST   /api/workspace/mkdir  -> create a folder, returns its path
  GET    /api/file?path=&inline=1 -> inline (preview) instead of attachment
  GET    /api/browse?path=&search=&timeout= -> deep search with wall-clock cap
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
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote

from .. import loop, llm, safety, commands
from ..providers import PRESETS, STRATEGIES
from ..tools import shell as _shell
from .. import stt as _stt


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
# With a monotonic _seq counter, events can be evicted from the front of the
# deque safely (the client's cursor is a sequence number, not an array index).
MAX_EVENTS = 2000
MAX_POLL_EVENTS = 200
STREAM_COALESCE_SECONDS = 0.1

# Cap how much text a single message can render into the browser.
MAX_RENDER_CHARS = 20000

# How long a single approval dialog may block the agent.
APPROVAL_TIMEOUT = 120

# Wall-clock cap for /api/browse?search= deep scans. After this many
# seconds the walk returns whatever it found so far. Prevents a `C:\`
# search from blocking the whole HTTP handler thread.
DEEP_SEARCH_TIMEOUT = 5.0


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
        # Ring buffer + monotonic sequence. `after` from the client is a
        # sequence number, not an array index, so eviction from the front of
        # the deque never causes silent event skips.
        self.events = deque(maxlen=MAX_EVENTS)
        self._seq = 0
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
                    # Merge in place if the previous event is a stream for
                    # the same id and arrived <STREAM_COALESCE_SECONDS ago.
                    # That keeps bursts cheap for the browser, while the
                    # FIRST chunk of a burst still goes out instantly.
                    if self.events:
                        last = self.events[-1]
                        if (last.get("type") == "tool_stream"
                                and last.get("id") == sid
                                and time.time() - last.get("_t", 0) < STREAM_COALESCE_SECONDS):
                            last["text"] += text
                            last["_t"] = time.time()
                            self.cond.notify_all()
                            return
                    extra["_t"] = time.time()

            # Flush any pending stream buffer when the tool finishes so the
            # last <100ms of a shell command isn't lost from the live view.
            if kind == "tool_result":
                sid = extra.get("id")
                if sid in self._stream_buf and self._stream_buf[sid]:
                    pending = "".join(self._stream_buf.pop(sid))
                    self._seq += 1
                    self.events.append({"type": "tool_stream", "text": pending,
                                        "id": sid, "_seq": self._seq, "_t": time.time()})
                    self._stream_last.pop(sid, None)

            self._seq += 1
            payload = {"type": kind, "text": text, "_seq": self._seq, **extra}
            if kind in ("assistant", "final") and "idx" not in extra:
                payload["idx"] = max(0, len(self.history) - 1)
            self.events.append(payload)
            self.cond.notify_all()

    def _cap_events(self):
        # Ring buffer via deque(maxlen=...) handles eviction; nothing to do.
        # Kept as a no-op so any external callers don't break.
        pass

    def poll(self, after, wait):
        """Return events with _seq > after.

        `after` is a monotonic sequence number from the client. Events can be
        evicted from the front of the deque without invalidating the cursor.
        """
        with self.cond:
            if self._seq <= after and wait > 0:
                self.cond.wait(wait)
            if after < 0:
                after = 0
            chunk = [e for e in self.events if e["_seq"] > after][:MAX_POLL_EVENTS]
            # If the client fell behind the ring buffer (its cursor is older
            # than the oldest retained event), tell it to reload history by
            # returning an empty chunk with next == self._seq. The JS side
            # treats that as "resync" and calls /api/history.
            if chunk and chunk[-1]["_seq"] < self._seq:
                chunk = [e for e in chunk if e.get("type") != "tool_stream"]
            nxt = chunk[-1]["_seq"] if chunk else after
            return (
                [{k: v for k, v in e.items() if k != "_t"} for e in chunk],
                nxt,
            )

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

    def full_tool_result(self, call_id: str):
        """Return the full tool output for `call_id` from history, uncut.

        Returns None if the id isn't found. Used by /api/tool_result so
        the browser can download the untruncated version of a tool output
        that was clipped to 1500 chars in the live view.
        """
        for m in self.history:
            if m.get("role") == "tool" and m.get("tool_call_id") == call_id:
                return str(m.get("content", ""))
        return None

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
        # STT: single-session lock. Only one live capture at a time.
        self.stt_lock = threading.Lock()
        self.stt_session = None

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
                try:
                    sess.emit("final", final, idx=final_idx)
                except Exception:
                    pass
            except llm.LLMError as e:
                try:
                    sess.emit("error", str(e))
                except Exception:
                    pass
            except Exception as e:
                try:
                    sess.emit("error", f"{type(e).__name__}: {e}")
                except Exception:
                    pass
            finally:
                sess.busy = False
                sess.save()
                try:
                    sess.emit("done")
                except Exception:
                    pass

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
        from .. import mcp_admin
        return mcp_admin.add_server(
            self.cfg, getattr(self.cfg, "mcp", None), spec, lock=self.mcp_lock,
        )

    def mcp_update(self, old_name: str, spec: dict) -> dict:
        from .. import mcp_admin
        return mcp_admin.update_server(
            self.cfg, getattr(self.cfg, "mcp", None), old_name, spec,
            lock=self.mcp_lock,
        )

    def mcp_delete(self, name: str) -> None:
        from .. import mcp_admin
        mcp_admin.delete_server(
            self.cfg, getattr(self.cfg, "mcp", None), name, lock=self.mcp_lock,
        )

    def mcp_start(self, name: str) -> dict:
        from .. import mcp_admin
        return mcp_admin.start_server(
            self.cfg, getattr(self.cfg, "mcp", None), name,
        )

    def mcp_stop(self, name: str) -> None:
        from .. import mcp_admin
        mcp_admin.stop_server(
            self.cfg, getattr(self.cfg, "mcp", None), name,
        )

    def state(self, lite=False):
        """Return the UI state.

        `lite=True` skips the per-key `models` lists (which can be 100+
        model ids × 30 keys = tens of KB of JSON on every poll). The UI
        uses `lite` for background refreshes and the full state only on
        initial load / explicit "Refresh all".
        """
        pm = self.cfg.providers
        pools = pm.summary()
        if lite:
            pools = [{
                **{k: v for k, v in p.items() if k != "keys"},
                "keys": [{k: v for k, v in key.items() if k != "models"} for key in p["keys"]],
            } for p in pools]
        return {
            "workspace": str(self.cfg.workspace),
            "home": str(self.cfg.home) if self.cfg.home else "",
            "strategy": pm.strategy,
            "strategies": list(STRATEGIES),
            "max_wait": pm.max_wait,
            "pools": pools,
            "load_error": pm.load_error,
            "presets": [{"name": k, "base_url": v[0], "model": v[1]} for k, v in PRESETS.items()],
            "auto_approve_env": self.cfg.auto_approve,
            "strict": self.cfg.strict,
            "shell_bg_default": _shell.default_background(),
            "mcp": self.mcp_state(),
            "mcp_load_errors": list(getattr(self.cfg.mcp, "load_errors", [])) if self.cfg.mcp else [],
        }

    def search_sessions(self, query: str, limit: int = 40) -> list:
        """Server-side search across every session file.

        The old UI filtered the 40 newest sessions client-side, which
        meant anything older than the 40th was invisible. This walks
        every `sessions/*.json`, reads the title (from the meta file if
        present, else the first user message), and returns the matching
        rows sorted by mtime descending.
        """
        d = self.cfg.data_dir / "sessions"
        out = []
        if not d.exists():
            return out
        q = (query or "").strip().lower()
        # Gather candidate files first so we only sort what we'll keep.
        cands = []
        for f in d.glob("*.json"):
            if f.name.endswith(".meta.json"):
                continue
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            cands.append((mtime, f))
        cands.sort(reverse=True)
        for mtime, f in cands:
            sid = f.stem
            s = self.sessions.get(sid)
            if s:
                title = s.title()
                busy = s.busy
            else:
                # Try the meta file first (cheap), then the session body.
                title = ""
                meta = f.with_suffix(".meta.json")
                if meta.exists():
                    try:
                        title = str(json.loads(meta.read_text(encoding="utf-8")).get("title") or "")
                    except Exception:
                        title = ""
                if not title:
                    try:
                        hist = json.loads(f.read_text(encoding="utf-8"))
                        title = next((str(m.get("content", ""))[:60]
                                      for m in hist if m.get("role") == "user"), "")
                    except Exception:
                        title = ""
                busy = False
            if not title:
                continue
            if q and q not in title.lower():
                continue
            out.append({"id": sid, "title": title, "mtime": int(mtime), "busy": bool(busy)})
            if len(out) >= limit:
                break
        return out

    def probe(self, names):
        pm, out = self.cfg.providers, []
        for n in names:
            p = pm.get(n)
            ok, kind, text = llm.probe(self.cfg, p)
            out.append({"name": n, "ok": ok, "kind": kind, "text": text})
        return out

    # ------------------------------------------------------------------ STT

    def stt_status(self):
        ok, reason = _stt.is_available()
        return {
            "ok": ok,
            "reason": reason,
            "active": bool(self.stt_session and self.stt_session.is_alive()),
        }

    def stt_start(self):
        ok, reason = _stt.is_available()
        if not ok:
            raise ValueError(reason)
        with self.stt_lock:
            if self.stt_session and self.stt_session.is_alive():
                return {"ok": True, "already": True}
            self.stt_session = _stt.LiveSession(_stt._model_dir())
            try:
                self.stt_session.start()
            except Exception as e:
                self.stt_session = None
                raise ValueError(f"could not start STT: {e}")
        return {"ok": True}

    def stt_poll(self, after: float) -> dict:
        if not self.stt_session:
            return {"events": [], "next": after, "active": False}
        events = self.stt_session.drain()
        # Filter by `after` timestamp so a slow client doesn't re-see events.
        out = [e for e in events if e["t"] > after]
        return {
            "events": out,
            "next": max((e["t"] for e in out), default=after),
            "active": self.stt_session.is_alive(),
            "error": getattr(self.stt_session, "_error", None),
        }

    def stt_stop(self):
        with self.stt_lock:
            if self.stt_session:
                self.stt_session.stop()
                self.stt_session = None
        return {"ok": True}

def make_handler(app):
    class H(BaseHTTPRequestHandler):
        server_version = "OmniUI/2.2"
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
            lite = q.get("lite") == "1"
            self._send(200, app.state(lite=lite))
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
            self._send(200, {"items": s.render(), "cursor": s._seq,
                             "busy": s.busy, "pending": pending,
                             "auto": s.auto, "strict": app.cfg.strict,
                             "allowed_tools": sorted(s.allowed_tools),
                             "title": s.title()})
        api_history.method = "GET"

        def api_tool_result(self, q):
            """Return the FULL (uncut) output of a tool call from history.

            The live view clips tool results to 1500 chars to keep the
            UI responsive. This endpoint lets the browser download the
            full output for a specific call id.
            """
            s = app.session(q.get("session", ""))
            cid = q.get("id", "")
            if not cid:
                return self._err(400, "id is required")
            text = s.full_tool_result(cid)
            if text is None:
                return self._err(404, "no such tool call in this session")
            self._send(200, text, "text/plain; charset=utf-8")
        api_tool_result.method = "GET"

        def api_sessions(self, q):
            """List sessions, optionally filtered by `q` server-side.

            The old handler returned the 40 newest with no query support.
            The new one delegates to App.search_sessions() when `q` is
            present, so old chats are findable.
            """
            query = q.get("q", "")
            if query:
                out = app.search_sessions(query, limit=200)
            else:
                out = app.search_sessions("", limit=40)
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
            inline = q.get("inline") == "1" and (
                ctype.startswith("text/") or ctype.startswith("image/")
                or ctype in ("application/json", "application/xml",
                             "application/javascript"))
            disp = "inline" if inline else "attachment"
            self._send(200, p.read_bytes(), ctype,
                       [("Content-Disposition",
                         f"{disp}; filename*=UTF-8''{quote(p.name)}")])
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
            self._send(200, {"ok": True, "cursor": s._seq, "user_idx": user_idx})

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
            self._send(200, {"ok": True, "cursor": s._seq})

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
            self._send(200, {"ok": True, "cursor": s._seq})

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
            role = s.history[idx].get("role")
            if role not in ("user", "assistant"):
                return self._err(400, "can only delete user/assistant messages")
            # If we're deleting an assistant message with tool_calls, also
            # drop the contiguous `tool` messages that follow it, so we
            # don't leave orphan tool_call_ids that strict providers reject.
            drop_to = idx + 1
            if role == "assistant" and s.history[idx].get("tool_calls"):
                while drop_to < len(s.history) and s.history[drop_to].get("role") == "tool":
                    drop_to += 1
            s.history = s.history[:idx] + s.history[drop_to:]
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
            kept, dropped = app.cfg.providers.set_fallbacks(b["pool"], models)
            self._send(200, {"ok": True, "kept": kept, "dropped": dropped})

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

        # ---- STT
        def api_stt_status(self, q):
            self._send(200, app.stt_status())
        api_stt_status.method = "GET"

        def api_stt_start(self, q):
            try:
                self._send(200, app.stt_start())
            except ValueError as e:
                self._send(200, {"ok": False, "error": str(e)})

        def api_stt_poll(self, q):
            try:
                after = float(q.get("after", 0))
            except ValueError:
                after = 0.0
            self._send(200, app.stt_poll(after))
        api_stt_poll.method = "GET"

        def api_stt_stop(self, q):
            self._send(200, app.stt_stop())

        def api_browse(self, q):
            """List a directory, or deep-search a tree with a wall-clock cap.

            The old handler walked the whole tree with no time limit,
            which blocked the HTTP thread for minutes on `C:\\`. The new
            one checks elapsed time every directory and returns partial
            results once `DEEP_SEARCH_TIMEOUT` is exceeded.
            """
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
            timed_out = False
            try:
                if search:
                    pat = f"*{search}*"
                    count = 0
                    start = time.time()
                    for root, dirs, files in os.walk(p):
                        if time.time() - start > DEEP_SEARCH_TIMEOUT:
                            timed_out = True
                            break
                        dirs[:] = [d for d in dirs
                                   if d not in ("node_modules", "__pycache__", ".git", ".venv",
                                                ".next", "dist", "build", "target", "Pods",
                                                "DerivedData", "AppData", "Library", ".cache")]
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
                        if count >= 200 or timed_out:
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
                "timed_out": timed_out,
            })
        api_browse.method = "GET"

        def api_workspace_set(self, q):
            b = self._json_body()
            new_ws = app.set_workspace(b.get("path", ""))
            self._send(200, {"workspace": str(new_ws)})

        def api_workspace_mkdir(self, q):
            """Create a folder (inside the current workspace or any writable dir).

            Body: {"parent": "/path/to/dir", "name": "new-folder"}.
            `parent` defaults to the current workspace. Returns the full
            path. Refuses `..` and absolute `name` values so the caller
            can't escape `parent`.
            """
            b = self._json_body()
            parent = Path(b.get("parent") or str(app.cfg.workspace)).expanduser().resolve()
            name = (b.get("name") or "").strip()
            if not name:
                raise ValueError("name is required")
            if "/" in name or "\\" in name or name in (".", ".."):
                raise ValueError("invalid folder name")
            if not parent.is_dir():
                raise ValueError(f"parent is not a directory: {parent}")
            target = parent / name
            try:
                target.mkdir(parents=False, exist_ok=False)
            except FileExistsError:
                raise ValueError(f"already exists: {target}")
            self._send(200, {"path": str(target)})

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
        # Flush pending health state (debounced save) before exit.
        try:
            if getattr(app, "stt_session", None):
                app.stt_session.stop()
        except Exception:
            pass
        try:
            cfg.providers.health.flush()
        except Exception:
            pass
        if getattr(cfg, "mcp", None):
            cfg.mcp.stop_all()