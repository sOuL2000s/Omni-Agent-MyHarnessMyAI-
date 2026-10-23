# agent/mcp_registry.py
"""Fetches the official MCP Registry and converts entries into Omni's
`mcp.json` templates.

Registry docs: https://registry.modelcontextprotocol.io
API is in preview — cache aggressively, never block the UI on it.

The `search()` output is shaped for the UI:
  {name, title, description, version, author, homepage, repository, icon,
   categories, tags, runtime, installed, template, remote_kind}

`template` is one of:
  {command, args}            local stdio (npx / uvx / docker)
  {transport:"http", url}    remote streamable-http
  {unsupported: True, reason}
  None                       nothing we can prefill
"""
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

REGISTRY_BASE = "https://registry.modelcontextprotocol.io/v0.1"
CACHE_TTL = 3600
HTTP_TIMEOUT = 20
MAX_PAGES = 20
MAX_RESULTS = 500


def _get_json(url: str):
    req = urllib.request.Request(url, headers={
        "User-Agent": "omni-agent/2.1 (+mcp-registry)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def fetch_all_servers(cache_path: Path, max_age: int = CACHE_TTL):
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
            batch = data.get("servers") or data.get("data") or []
            for entry in batch:
                if isinstance(entry, dict):
                    servers.append(entry.get("server", entry))
            cursor = data.get("nextCursor") or data.get("next_cursor")
            if not cursor:
                break
        if servers:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(servers, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        return servers
    except Exception:
        if cache_path.exists():
            try:
                return json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        raise


# ---------------------------------------------------------------- helpers
def _pick_package(entry):
    for pkg in entry.get("packages") or []:
        t = (pkg.get("transport") or {}).get("type")
        if t in (None, "stdio"):
            return pkg
    pkgs = entry.get("packages") or []
    return pkgs[0] if pkgs else None


def _package_args(pkg):
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


def _package_env(pkg):
    """Extract env vars a package needs (name + description + required)."""
    out = []
    for e in pkg.get("environmentVariables") or []:
        if not isinstance(e, dict):
            continue
        out.append({
            "name": e.get("name") or "",
            "description": e.get("description") or "",
            "required": bool(e.get("required") or e.get("isRequired")),
            "secret": bool(e.get("secret") or e.get("isSecret")),
        })
    return out


def _package_runtime(pkg):
    rtype = (pkg.get("registryType") or "").lower()
    return {"npm": "npx", "pypi": "uvx", "oci": "docker"}.get(rtype, rtype or "custom")


def _join_category(tags):
    cats = []
    for t in tags or []:
        if isinstance(t, str) and t not in cats:
            cats.append(t)
    return cats


def to_omni_config(entry):
    if not isinstance(entry, dict):
        return None
    remotes = entry.get("remotes") or []
    packages = entry.get("packages") or []

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
    env = {e["name"]: "" for e in _package_env(pkg) if e.get("name")}

    if rtype == "npm":
        spec = f"{ident}@{ver}" if ver and ver != "latest" else ident
        return {"command": "npx", "args": ["-y", spec, *tmpl_args],
                "env": env, "runtime": "npx"}
    if rtype == "pypi":
        spec = f"{ident}=={ver}" if ver and ver != "latest" else ident
        return {"command": "uvx", "args": [spec, *tmpl_args],
                "env": env, "runtime": "uvx"}
    if rtype == "oci":
        image = f"{ident}:{ver}" if ver else ident
        return {"command": "docker", "args": ["run", "--rm", "-i", image, *tmpl_args],
                "env": env, "runtime": "docker"}
    return None


def _row(entry, installed):
    name = entry.get("name") or ""
    pkg = _pick_package(entry) or {}
    tmpl = to_omni_config(entry)
    env_specs = _package_env(pkg)
    runtime = _package_runtime(pkg) if pkg else ("http" if (tmpl and tmpl.get("transport") == "http") else "")
    icon = ""
    for k in ("icon", "logo", "iconUrl"):
        if entry.get(k):
            icon = entry[k]
            break
    homepage = ""
    repo = entry.get("repository") or {}
    if isinstance(repo, dict):
        homepage = repo.get("url") or ""
    elif isinstance(repo, str):
        homepage = repo
    if not homepage:
        homepage = entry.get("homepage") or entry.get("website") or ""
    author = ""
    if entry.get("author"):
        if isinstance(entry["author"], dict):
            author = entry["author"].get("name") or entry["author"].get("url") or ""
        else:
            author = str(entry["author"])
    tags = _join_category(entry.get("tags") or entry.get("categories") or [])
    title = entry.get("title") or entry.get("displayName") or name.split("/")[-1]
    return {
        "name": name,
        "title": title,
        "description": (entry.get("description") or "")[:400],
        "version": entry.get("version") or "",
        "author": author,
        "homepage": homepage,
        "repository": (repo.get("url") if isinstance(repo, dict) else "") or "",
        "icon": icon,
        "categories": tags,
        "runtime": runtime,
        "installed": name in installed,
        "template": tmpl,
        "env_required": [e for e in env_specs if e.get("required")],
        "env_optional": [e for e in env_specs if not e.get("required")],
    }


def search(cache_path: Path, query: str = "", limit: int = 200, installed=()):
    servers = fetch_all_servers(cache_path)
    q = (query or "").strip().lower()
    installed = set(installed or ())
    scored = []
    for entry in servers:
        try:
            row = _row(entry, installed)
        except Exception:
            continue
        if q:
            hay = " ".join([
                row["name"], row["title"], row["description"],
                " ".join(row["categories"]), row["runtime"] or "",
            ]).lower()
            if q not in hay:
                continue
            # lightweight ranking: name-prefix > name contains > description
            if row["name"].lower().startswith(q) or row["title"].lower().startswith(q):
                score = 0
            elif q in row["name"].lower() or q in row["title"].lower():
                score = 1
            elif q in row["description"].lower():
                score = 2
            else:
                score = 3
        else:
            score = 0
        scored.append((score, row))
    scored.sort(key=lambda t: (t[0], t[1]["name"]))
    return [r for _, r in scored[:max(1, min(limit, MAX_RESULTS))]]