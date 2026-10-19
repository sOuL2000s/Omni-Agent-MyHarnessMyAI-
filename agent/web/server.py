# agent/web/server.py
"""Local web UI for Omni (standard library only).

Security model:
  * binds to 127.0.0.1 by default,
  * requires a per-start token (URL once, then HttpOnly SameSite=Strict cookie),
  * checks the Host header (blocks DNS-rebinding),
  * never sends API keys to the browser (only masked tails),
  * refuses to use a filesystem root or a parent of data/ as the workspace.
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


# ---------------------------------------------------------------- workspace guard
def _validate_workspace(new_ws: Path, data_dir: Path) -> None:
    """Refuse filesystem roots and ancestors of data/ as the workspace.

    A filesystem root would make every path "inside" and silently disable the
    outside-workspace approval guard. An ancestor of data/ would let the
    agent's own file tools read data/providers.json and exfiltrate the keys.
    """
    try:
        new_ws = new_ws.resolve()
    except OSError:
        pass
    try:
        data = data_dir.resolve()
    except OSError:
        data = data_dir
    if new_ws == new_ws.parent:
        raise ValueError(
            "refusing a filesystem root as the workspace "
            "(it would disable the outside-workspace approval guard)")
    if new_ws == data or new_ws in data.parents:
        raise ValueError(
            "refusing a parent of data/ as the workspace "
            "(the agent could read its own API keys)")


# ---------------------------------------------------------------- chat sessions
class Session:
    def __init__(self, cfg, sid, history=None):
        self.cfg, self.id = cfg, sid
        self.history = history or loop.new_history(cfg)
        self.events = []
        self.cond = threading.Condition()
        self.busy = False
        self.cancel = threading.Event()
        # Inherit the process-level auto-approve setting. The per-session
        # toggle in the UI can still turn it off/on without touching the env.
        self.auto = bool(getattr(cfg, "auto_approve", False))
        self.approvals = {}
        self.allowed_tools = set()
        self.touched = time.time()
        self.deleted = False
        # tool_stream coalescing: one buffer per stream id
        self._stream_buf = {}
        self._stream_last = {}

    @property
    def file(self):
        d = self.cfg.data_dir / "sessions"
        d.mkdir(exist_ok=True)
        return d / f"{self.id}.json"

    def emit(self, kind, text="", **extra):
        """Emit an event. For messages that will persist in history, attach
        the current history index as `idx` so the client can offer Edit /
        Delete even before the run finishes and history is re-fetched."""
        with self.cond:
            # Coalesce streaming tool output so the poller isn't woken up
            # hundreds of times per second on a chatty shell command.
            if kind == "tool_stream":
                sid = extra.get("id")
                if sid is None:
                    pass
                else:
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
        """Keep the events list bounded, dropping the oldest tool_stream chunks."""
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
            # Cap per-response payload; skip stale streaming chunks if the
            # client fell far behind.
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

    def title(self):
        for m in self.history:
            if m.get("role") == "user":
                return str(m.get("content", ""))[:60]
        return "New chat"

    def render(self):
        out = []
        for i, m in enumerate(self.history):
            if i == 0:
                continue
            r = m.get("role")
            if r == "user":
                out.append({"type": "user", "text": m.get("content", ""), "idx": i})
            elif r == "assistant":
                calls = m.get("tool_calls") or []
                if m.get("content"):
                    out.append({"type": "assistant" if calls else "final",
                                "text": m["content"], "idx": i})
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

    def stop(self):
        self.cancel.set()
        try:
            _shell.kill_all()
        except Exception:
            pass
        for rec in list(self.approvals.values()):
            rec["allow"] = False
            rec["event"].set()


class App:
    def __init__(self, cfg, token, hosts):
        self.cfg, self.token, self.hosts = cfg, token, hosts
        self.sessions = {}
        self.lock = threading.Lock()

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
            rec["event"].wait(600)
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

        # The caller is expected to have already set sess.busy = True and
        # cleared sess.cancel (so the "is this session busy?" check is atomic).
        threading.Thread(target=work, daemon=True).start()

    def state(self):
        pm = self.cfg.providers
        mcp_state = []
        if getattr(self.cfg, "mcp", None):
            for name, srv in self.cfg.mcp.servers.items():
                mcp_state.append({
                    "name": name,
                    "ready": srv.ready,
                    "tools": len(srv.tools),
                    "error": srv.last_error,
                    "command": " ".join(srv.cmd),
                })
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
            "mcp": mcp_state,
            "mcp_load_errors": list(getattr(self.cfg.mcp, "load_errors", [])) if self.cfg.mcp else [],
        }

    def probe(self, names):
        pm, out = self.cfg.providers, []
        for n in names:
            p = pm.get(n)
            ok, kind, text = llm.probe(self.cfg, p)
            out.append({"name": n, "ok": ok, "kind": kind, "text": text})
        return out


# ---------------------------------------------------------------- HTTP
def make_handler(app):
    class H(BaseHTTPRequestHandler):
        server_version = "OmniUI/2.1"
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body=b"", ctype="application/json", extra=()):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
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
            return json.loads(raw.decode("utf-8")) if raw else {}

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
                # /api/keys/add -> api_keys_add. Hyphens and nested slashes
                # won't match; if you ever need them, add an explicit route
                # table here.
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
                             "allowed_tools": sorted(s.allowed_tools)})
        api_history.method = "GET"

        def api_sessions(self, q):
            d = app.cfg.data_dir / "sessions"
            out = []
            if d.exists():
                for f in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:40]:
                    try:
                        hist = json.loads(f.read_text(encoding="utf-8"))
                        t = next((str(m["content"])[:60] for m in hist if m.get("role") == "user"), "")
                        if t:
                            out.append({"id": f.stem, "title": t,
                                        "mtime": int(f.stat().st_mtime)})
                    except Exception:
                        continue
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
            # Claim the session BEFORE emitting or spawning, so a second
            # concurrent request cannot slip past the busy check above.
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

        def api_session_delete(self, q):
            sid = self._json_body().get("session", "")
            s = app.session(sid)
            s.stop()
            s.deleted = True
            app.sessions.pop(sid, None)
            try:
                s.file.unlink()
            except FileNotFoundError:
                pass
            self._send(200, {"ok": True})

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
            self._send(200, {"refreshed": summary})

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
                    # os.walk lets us prune noise and stop early; rglob would
                    # descend the whole tree before the filter is even applied.
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
            new_ws = Path(b.get("path", "")).expanduser().resolve()
            if not new_ws.is_dir():
                raise ValueError(f"not a directory: {new_ws}")
            _validate_workspace(new_ws, app.cfg.data_dir)
            app.cfg.workspace = new_ws
            self._send(200, {"workspace": str(new_ws)})

        def api_workspace_pick(self, q):
            """Open the OS folder picker on the machine running the server.

            This is the only way to get a native 'Browse…' dialog for a path
            on the *server's* machine. Browsers can't return an absolute path
            from <input webkitdirectory> or showDirectoryPicker(). On a
            headless Linux box or when tkinter is missing, returns 501 so the
            UI can tell the user to type the path instead.
            """
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
            new_ws = Path(chosen).expanduser().resolve()
            try:
                _validate_workspace(new_ws, app.cfg.data_dir)
            except ValueError as e:
                return self._err(400, str(e))
            app.cfg.workspace = new_ws
            self._send(200, {"workspace": str(new_ws)})

        def api_strict(self, q):
            b = self._json_body()
            app.cfg.strict = bool(b.get("on"))
            self._send(200, {"strict": app.cfg.strict})

        # ---- MCP
        def api_mcp_restart(self, q):
            name = self._json_body().get("name", "")
            if not getattr(app.cfg, "mcp", None):
                return self._err(400, "no MCP configured")
            msg = app.cfg.mcp.restart(name)
            from ..tools import mcp_bridge
            mcp_bridge._registered_for = None
            mcp_bridge.register(app.cfg)
            self._send(200, {"ok": True, "result": msg})

        def api_mcp_stop(self, q):
            name = self._json_body().get("name", "")
            srv = getattr(app.cfg, "mcp", None) and app.cfg.mcp.servers.get(name)
            if srv:
                srv.stop()
            self._send(200, {"ok": True})

        def api_mcp_start(self, q):
            name = self._json_body().get("name", "")
            srv = getattr(app.cfg, "mcp", None) and app.cfg.mcp.servers.get(name)
            if not srv:
                return self._err(404, "no such MCP server")
            try:
                srv.start()
                from ..tools import mcp_bridge
                mcp_bridge._registered_for = None
                mcp_bridge.register(app.cfg)
                self._send(200, {"ok": True, "tools": len(srv.tools)})
            except Exception as e:
                self._err(500, str(e))

        def api_mcp_add(self, q):
            b = self._json_body()
            name = (b.get("name") or "").strip()
            command = (b.get("command") or "").strip()
            if not name or not command:
                raise ValueError("name and command are required")
            cfg_path = app.cfg.data_dir / "mcp.json"
            try:
                data = json.loads(cfg_path.read_text(encoding="utf-8-sig")) if cfg_path.exists() else {"servers": {}}
            except Exception:
                data = {"servers": {}}
            data.setdefault("servers", {})[name] = {
                "command": command,
                "args": b.get("args") or [],
                "env": b.get("env") or {},
                "enabled": True,
            }
            cfg_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            from ..mcp import MCPServer
            srv = MCPServer(name, command, b.get("args") or [], b.get("env") or {}, b.get("cwd"))
            try:
                srv.start()
                app.cfg.mcp.servers[name] = srv
                from ..tools import mcp_bridge
                mcp_bridge._registered_for = None
                mcp_bridge.register(app.cfg)
                self._send(200, {"ok": True, "tools": len(srv.tools)})
            except Exception as e:
                self._err(500, str(e))

    return H


# ---------------------------------------------------------------- entry
def serve(cfg, host="127.0.0.1", port=8765, open_browser=True, on_url=None):
    token = os.environ.get("AGENT_UI_TOKEN") or secrets.token_urlsafe(18)
    loopback = host in ("127.0.0.1", "localhost", "::1")
    hosts = {"127.0.0.1", "localhost", "::1"} if loopback else None
    app = App(cfg, token, hosts)
    srv = None
    for p in range(port, port + 20):
        try:
            srv = ThreadingHTTPServer((host, p), make_handler(app))
            port = p
            break
        except OSError:
            continue
    if srv is None:
        raise SystemExit(f"No free port near {port}")
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