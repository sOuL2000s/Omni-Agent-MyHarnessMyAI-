# agent/mcp_registry.py
"""Fetches the official MCP Registry and converts entries into Omni's
`mcp.json` {command, args} format.

Registry docs: https://registry.modelcontextprotocol.io
API is in preview — cache aggressively, never block the UI on it.

Stdlib only. Called from the web server (see /api/mcp_registry) and from
a background refresh thread. Nothing here starts a server; it only
produces templates the user reviews before clicking "Add & connect".
"""
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

REGISTRY_BASE = "https://registry.modelcontextprotocol.io/v0.1"
CACHE_TTL = 3600          # seconds; re-fetch the catalog at most hourly
HTTP_TIMEOUT = 20
MAX_PAGES = 20            # safety cap; ~2000 entries at limit=100


def _get_json(url: str):
    req = urllib.request.Request(url, headers={
        "User-Agent": "omni-agent/2.1 (+mcp-registry)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def fetch_all_servers(cache_path: Path, max_age: int = CACHE_TTL):
    """Return the full server list, using the on-disk cache when fresh.

    On a network error with a stale cache present, returns the stale cache
    rather than raising — the UI should still show *something*.
    """
    cache_path = Path(cache_path)
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < max_age:
            try:
                return json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                pass

    servers, cursor, pages = [], None, 0
    try:
        while pages < MAX_PAGES:
            pages += 1
            url = f"{REGISTRY_BASE}/servers?limit=100"
            if cursor:
                url += "&cursor=" + urllib.parse.quote(cursor)
            data = _get_json(url)
            # The registry returns {"servers": [...], "metadata": {...}, "nextCursor": "..."}
            batch = data.get("servers") or data.get("data") or []
            for entry in batch:
                # Newer entries wrap the payload in {"server": {...}, "_meta": {...}}
                servers.append(entry.get("server", entry) if isinstance(entry, dict) else entry)
            cursor = data.get("nextCursor") or data.get("next_cursor")
            if not cursor:
                break
        if servers:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(servers), encoding="utf-8")
            except OSError:
                pass
        return servers
    except Exception:
        # Fall back to whatever we have on disk, even if stale.
        if cache_path.exists():
            try:
                return json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        raise


def _pick_package(entry):
    """Return the first stdio-capable package, or None."""
    for pkg in entry.get("packages") or []:
        t = (pkg.get("transport") or {}).get("type")
        if t in (None, "stdio"):
            return pkg
    pkgs = entry.get("packages") or []
    return pkgs[0] if pkgs else None


def _package_args(pkg):
    """Extract template arguments (positional + named) from a package entry."""
    args = []
    for a in pkg.get("packageArguments") or []:
        kind = a.get("type")
        if kind == "positional":
            v = a.get("value") or a.get("default") or a.get("placeholder")
            if v:
                args.append(str(v))
        elif kind == "named":
            name = a.get("name") or ""
            v = a.get("value") or a.get("default") or a.get("placeholder")
            if name:
                args.append(str(name))
                if v:
                    args.append(str(v))
    return args


def to_omni_config(entry):
    """Convert a registry entry to a pre-fillable {command, args} or {url}
    template.

    Remote-only entries now become {transport: "http", url: ...} so the UI
    can pre-fill them. Only entries with neither a usable package nor a
    streamable-http remote return None.
    """
    if not isinstance(entry, dict):
        return None

    remotes = entry.get("remotes") or []
    packages = entry.get("packages") or []

    # Prefer a streamable-http remote when the entry has no local package.
    if remotes and not packages:
        http_remote = next((r for r in remotes
                            if (r or {}).get("type") == "streamable-http"), None)
        if http_remote:
            return {"transport": "http", "url": http_remote.get("url")}
        sse_remote = next((r for r in remotes if (r or {}).get("type") == "sse"), None)
        if sse_remote:
            return {"transport": "http", "url": sse_remote.get("url"),
                    "note": "legacy SSE endpoint"}
        return {"unsupported": True,
                "reason": "remote entry has no streamable-http URL"}

    pkg = _pick_package(entry)
    if not pkg:
        return None
    rtype = (pkg.get("registryType") or "").lower()
    ident = pkg.get("identifier") or ""
    ver = pkg.get("version") or ""
    tmpl_args = _package_args(pkg)

    if rtype == "npm":
        spec = f"{ident}@{ver}" if ver and ver != "latest" else ident
        return {"command": "npx", "args": ["-y", spec, *tmpl_args]}
    if rtype == "pypi":
        spec = f"{ident}=={ver}" if ver and ver != "latest" else ident
        return {"command": "uvx", "args": [spec, *tmpl_args]}
    if rtype == "oci":
        image = f"{ident}:{ver}" if ver else ident
        return {"command": "docker", "args": ["run", "--rm", "-i", image, *tmpl_args]}
    # Unknown registry type — let the user type it manually.
    return None


def search(cache_path: Path, query: str = "", limit: int = 200, installed=()):
    """Return UI-ready rows: {name, description, template, installed}."""
    servers = fetch_all_servers(cache_path)
    q = (query or "").strip().lower()
    installed = set(installed or ())
    out = []
    for entry in servers:
        name = entry.get("name") or ""
        desc = entry.get("description") or ""
        if q and q not in name.lower() and q not in desc.lower():
            continue
        tmpl = to_omni_config(entry)
        out.append({
            "name": name,
            "description": desc[:280],
            "version": (entry.get("version") or ""),
            "repository": (entry.get("repository") or {}).get("url", ""),
            "template": tmpl,
            "installed": name in installed,
        })
        if len(out) >= limit:
            break
    return out