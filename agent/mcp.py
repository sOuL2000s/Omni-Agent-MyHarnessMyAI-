# agent/mcp.py
"""Minimal MCP (Model Context Protocol) client over stdio.

Loads `data/mcp.json`:

    {
      "servers": {
        "filesystem": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/you"],
          "enabled": true
        },
        "github": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-github"],
          "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"}
        }
      }
    }

Each server's tools are exposed to the agent as
    mcp__<server>__<tool>
so names never collide with built-ins.
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request

MCP_PROTOCOL_VERSION = "2025-06-18"   # header sent on every POST


class MCPError(Exception):
    pass


class MCPServer:
    def __init__(self, name, command=None, args=None, env=None, cwd=None,
                 url=None, headers=None):
        self.name = name
        self.transport = "http" if url else "stdio"
        self.url = url
        self.http_headers = dict(headers or {})
        self.session_id = None
        self.cmd = ([command] + (args or [])) if command else []
        self.env = {**os.environ, **(env or {})}
        self.cwd = cwd
        self.proc = None
        self.seq = 0
        self.pending = {}
        self.lock = threading.RLock()
        self.tools = []
        self.last_error = None
        self.ready = False

    # -- lifecycle
    def start(self):
        if self.transport == "http":
            self._start_http()
        else:
            self._start_stdio()
        self._initialize()
        self.ready = True

    def _start_stdio(self):
        self.proc = subprocess.Popen(
            self.cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=self.env, cwd=self.cwd,
        )
        threading.Thread(target=self._reader, daemon=True,
                         name=f"mcp-{self.name}-out").start()
        threading.Thread(target=self._stderr, daemon=True,
                         name=f"mcp-{self.name}-err").start()

    def _start_http(self):
        """No persistent connection for Streamable HTTP: every JSON-RPC
        message is a fresh POST. We only verify the endpoint answers a
        tools/list during _initialize()."""
        if not self.url:
            raise MCPError(f"MCP {self.name}: http transport requires a url")

    def _stderr(self):
        for line in self.proc.stderr:
            sys.stderr.write(f"[mcp:{self.name}] {line}")

    def _reader(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                rid = msg.get("id")
                if rid is not None and rid in self.pending:
                    self.pending.pop(rid).put(msg)
        except Exception as e:
            self.last_error = f"reader died: {e}"

    def _rpc(self, method, params=None, timeout=60):
        if self.transport == "http":
            return self._rpc_http(method, params, timeout)
        with self.lock:
            self.seq += 1
            rid = self.seq
            q = queue.Queue()
            self.pending[rid] = q
            payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
            try:
                self.proc.stdin.write(json.dumps(payload) + "\n")
                self.proc.stdin.flush()
            except Exception as e:
                self.pending.pop(rid, None)
                raise MCPError(f"MCP {self.name}: could not write to server: {e}")
        try:
            resp = q.get(timeout=timeout)
        except queue.Empty:
            raise MCPError(f"MCP {self.name}: {method} timed out after {timeout}s")
        if "error" in resp:
            raise MCPError(f"MCP {self.name}: {resp['error']}")
        return resp.get("result") or {}

    def _rpc_http(self, method, params=None, timeout=60):
        """One JSON-RPC request = one HTTP POST to the MCP endpoint."""
        with self.lock:
            self.seq += 1
            rid = self.seq
        payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            "Mcp-Method": method,
            **self.http_headers,
        }
        if method in ("tools/call", "resources/read", "prompts/get"):
            name = (params or {}).get("name") or (params or {}).get("uri")
            if name:
                headers["Mcp-Name"] = name
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                sid = r.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                ctype = r.headers.get("Content-Type", "")
                body = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            txt = ""
            try:
                txt = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            raise MCPError(f"MCP {self.name}: HTTP {e.code}: {txt[:300]}")
        except Exception as e:
            raise MCPError(f"MCP {self.name}: request failed: {e}")
        data = self._parse_http_body(body, ctype, rid)
        if "error" in data:
            raise MCPError(f"MCP {self.name}: {data['error']}")
        return data.get("result") or {}

    @staticmethod
    def _parse_http_body(body, ctype, rid):
        """The server may reply with a single JSON object or an SSE stream."""
        if "text/event-stream" in ctype:
            for line in body.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    if msg.get("id") == rid:
                        return msg
            raise MCPError("SSE stream ended without a matching response")
        try:
            data = json.loads(body)
        except ValueError:
            raise MCPError(f"invalid JSON reply: {body[:200]}")
        if isinstance(data, list):
            for msg in data:
                if isinstance(msg, dict) and msg.get("id") == rid:
                    return msg
            raise MCPError("batch reply had no matching id")
        return data

    def _notify(self, method, params=None):
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if self.transport == "http":
            headers = {
                "Content-Type": "application/json",
                "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
                "Mcp-Method": method,
                **self.http_headers,
            }
            if self.session_id:
                headers["Mcp-Session-Id"] = self.session_id
            try:
                req = urllib.request.Request(self.url, data=json.dumps(payload).encode(),
                                             headers=headers, method="POST")
                urllib.request.urlopen(req, timeout=10).close()
            except Exception:
                pass
            return
        try:
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()
        except Exception:
            pass

    def _initialize(self):
        self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "omni", "version": "2.0"},
        }, timeout=30)
        self._notify("notifications/initialized", {})
        result = self._rpc("tools/list", {}, timeout=30)
        self.tools = result.get("tools") or []

    def call(self, tool_name, arguments, timeout=180):
        result = self._rpc("tools/call",
                           {"name": tool_name, "arguments": arguments or {}},
                           timeout=timeout)
        parts = []
        for c in result.get("content") or []:
            t = c.get("type")
            if t == "text":
                parts.append(c.get("text", ""))
            elif t == "resource":
                parts.append(json.dumps(c.get("resource", c)))
            elif t == "image":
                parts.append(f"[image: {c.get('mimeType', 'unknown')}]")
            else:
                parts.append(json.dumps(c))
        if result.get("isError"):
            return "ERROR: " + "\n".join(parts)
        return "\n".join(parts) if parts else "(no output)"

    def stop(self):
        if self.transport == "stdio":
            try:
                if self.proc and self.proc.poll() is None:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                        self.proc.wait(timeout=3)
            except Exception:
                pass
            for stream in ("stdin", "stdout", "stderr"):
                try:
                    getattr(self.proc, stream).close()
                except Exception:
                    pass
        self.ready = False
        self.tools = []
        self.session_id = None


def _expand_env(value):
    if isinstance(value, str):
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
                      lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


def sanitize_name(name: str) -> str:
    """MCP tool names can contain '/' and ':' - sanitize for OpenAI tool-name rules."""
    return re.sub(r"[^A-Za-z0-9_\-]", "_", name)[:64]


class MCPRegistry:
    def __init__(self):
        self.servers = {}
        self.load_errors = []

    def load(self, config_path):
        if not config_path.exists():
            return
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except Exception as e:
            self.load_errors.append(f"{config_path.name}: {e}")
            return
        for name, spec in (raw.get("servers") or {}).items():
            if not isinstance(spec, dict):
                continue
            if spec.get("enabled") is False:
                continue
            url = spec.get("url")
            cmd = spec.get("command")
            if not url and not cmd:
                self.load_errors.append(f"{name}: needs either 'command' or 'url'")
                continue
            srv = MCPServer(
                name=name,
                command=cmd,
                args=_expand_env(spec.get("args") or []),
                env=_expand_env(spec.get("env") or {}),
                cwd=spec.get("cwd"),
                url=url,
                headers=_expand_env(spec.get("headers") or {}),
            )
            try:
                srv.start()
                self.servers[name] = srv
            except Exception as e:
                srv.last_error = str(e)
                self.servers[name] = srv
                self.load_errors.append(f"{name}: {e}")

    def restart(self, name):
        srv = self.servers.get(name)
        if not srv:
            return "no such server"
        srv.stop()
        try:
            srv.start()
            return f"{name}: {len(srv.tools)} tools"
        except Exception as e:
            srv.last_error = str(e)
            return f"{name}: ERROR {e}"

    def stop_all(self):
        for s in self.servers.values():
            s.stop()

    def register_tools(self):
        """Register every MCP tool into the global registry as mcp__<server>__<tool>."""
        from .tools.registry import register_external
        count = 0
        for sname, srv in self.servers.items():
            if not srv.ready:
                continue
            for t in srv.tools:
                raw_name = t.get("name", "")
                exposed = f"mcp__{sanitize_name(sname)}__{sanitize_name(raw_name)}"

                def _make(srv=srv, raw_name=raw_name):
                    def _fn(cfg, **kwargs):
                        return srv.call(raw_name, kwargs)
                    return _fn

                desc = t.get("description") or f"MCP tool {raw_name} from {sname}"
                schema = t.get("inputSchema") or {"type": "object", "properties": {}}
                register_external(exposed, desc, schema, _make())
                count += 1
        return count