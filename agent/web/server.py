"""Local web UI for Omni (standard library only).

Security model: it can run shell commands for you, so it
  * binds to 127.0.0.1 by default,
  * needs a random per-start token (carried in the URL once, then in an HttpOnly SameSite=Strict cookie),
  * checks the Host header (blocks DNS-rebinding), and
  * never sends API keys to the browser (only masked tails).
"""
import hmac
import json
import mimetypes
import os
import re
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote

from .. import loop, llm, safety, commands
from ..providers import PRESETS, STRATEGIES

INDEX = Path(__file__).with_name("index.html")
SID_RE = re.compile(r"^[a-f0-9]{8,32}$")
MAX_JSON = 5 * 1024 * 1024
MAX_UPLOAD = 50 * 1024 * 1024


# ---------------------------------------------------------------- chat sessions
class Session:
    def __init__(self, cfg, sid, history=None):
        self.cfg, self.id = cfg, sid
        self.history = history or loop.new_history(cfg)
        self.events = []
        self.cond = threading.Condition()
        self.busy = False
        self.cancel = threading.Event()
        self.auto = False
        self.approvals = {}
        self.touched = time.time()

    @property
    def file(self):
        d = self.cfg.data_dir / "sessions"
        d.mkdir(exist_ok=True)
        return d / f"{self.id}.json"

    def emit(self, kind, text="", **extra):
        with self.cond:
            self.events.append({"type": kind, "text": text, **extra})
            self.cond.notify_all()

    def poll(self, after, wait):
        with self.cond:
            if len(self.events) <= after and wait > 0:
                self.cond.wait(wait)
            return self.events[after:], len(self.events)

    def save(self):
        try:
            self.file.write_text(json.dumps(self.history, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def title(self):
        for m in self.history:
            if m.get("role") == "user":
                return str(m.get("content", ""))[:60]
        return "New chat"

    def render(self):
        """History as UI events."""
        out = []
        for m in self.history[1:]:
            r = m.get("role")
            if r == "user":
                out.append({"type": "user", "text": m.get("content", "")})
            elif r == "assistant":
                calls = m.get("tool_calls") or []
                if m.get("content"):
                    out.append({"type": "assistant" if calls else "final", "text": m["content"]})
                for c in calls:
                    fn = c.get("function", {})
                    out.append({"type": "tool", "name": fn.get("name"), "text": fn.get("arguments", ""), "id": c.get("id")})
            elif r == "tool":
                t = str(m.get("content", ""))
                out.append({"type": "tool_result", "text": t[:1500], "id": m.get("tool_call_id"), "ok": not t.startswith("ERROR")})
        return out

    def stop(self):
        self.cancel.set()
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
        def ask(action):
            if sess.auto:
                return True
            aid = secrets.token_hex(4)
            rec = {"action": action, "event": threading.Event(), "allow": False}
            sess.approvals[aid] = rec
            sess.emit("approval", action, id=aid)
            rec["event"].wait(600)
            sess.approvals.pop(aid, None)
            sess.emit("approval_done", "", id=aid, allow=bool(rec["allow"]))
            return bool(rec["allow"])

        def work():
            safety.set_approver(ask)
            try:
                final = loop.run(self.cfg, sess.history, text, on_event=sess.emit, cancel=sess.cancel)
                sess.emit("final", final)
            except llm.LLMError as e:
                sess.emit("error", str(e))
            except Exception as e:  # never let a tool crash kill the server
                sess.emit("error", f"{type(e).__name__}: {e}")
            finally:
                sess.busy = False
                sess.save()
                sess.emit("done")

        sess.cancel.clear()
        sess.busy = True
        threading.Thread(target=work, daemon=True).start()

    # ---- helpers for the keys panel
    def state(self):
        pm = self.cfg.providers
        return {"workspace": str(self.cfg.workspace), "strategy": pm.strategy, "strategies": list(STRATEGIES),
                "max_wait": pm.max_wait, "pools": pm.summary(), "load_error": pm.load_error,
                "presets": [{"name": k, "base_url": v[0], "model": v[1]} for k, v in PRESETS.items()],
                "auto_approve_env": self.cfg.auto_approve}

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
        server_version = "OmniUI/2"
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        # -- plumbing
        def _send(self, code, body=b"", ctype="application/json", extra=()):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            if code >= 400:
                self.close_connection = True     # body may be unread; don't reuse the socket
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

        # -- routing
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
                        return self._send(401, "Open the exact link printed in the terminal when you ran:  omni ui\n", "text/plain; charset=utf-8")
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

        # -- read endpoints
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
            pending = [{"id": k, "action": v["action"]} for k, v in s.approvals.items()]
            self._send(200, {"items": s.render(), "cursor": len(s.events), "busy": s.busy,
                             "pending": pending, "auto": s.auto})
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
                            out.append({"id": f.stem, "title": t})
                    except Exception:
                        continue
            self._send(200, out)
        api_sessions.method = "GET"

        def api_files(self, q):
            ws, out = app.cfg.workspace, []
            for p in sorted(ws.rglob("*")):
                rel = p.relative_to(ws)
                if p.is_file() and not any(x.startswith(".") or x in ("__pycache__", "node_modules") for x in rel.parts):
                    st = p.stat()
                    out.append({"path": rel.as_posix(), "size": st.st_size, "mtime": int(st.st_mtime)})
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

        # -- chat endpoints
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
            s.emit("user", text)
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
            app.sessions.pop(sid, None)
            try:
                s.file.unlink()
            except FileNotFoundError:
                pass
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

        # -- keys / models endpoints
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
            keys = [k for k in re.split(r"[\s,;]+", b.get("keys", "")) if k and not k.startswith("#")]
            if not keys:
                raise ValueError("paste at least one key")
            pool = (b.get("pool") or "").strip()
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]*", pool):
                raise ValueError("pool name: letters/digits only, e.g. gemini, openrouter, mygateway")
            added, dup = app.cfg.providers.add_keys(pool, keys, (b.get("base_url") or None), (b.get("model") or None))
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

    return H


# ---------------------------------------------------------------- entry
def serve(cfg, host="127.0.0.1", port=8765, open_browser=True):
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
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        srv.server_close()
