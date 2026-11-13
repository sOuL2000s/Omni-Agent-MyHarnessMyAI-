# agent/tools/memory.py
import json
import sqlite3
import time
import os
from contextlib import contextmanager
from pathlib import Path
from .registry import tool

# Optional dependencies for Vector DB
try:
    import lancedb
    from openai import OpenAI
    HAS_VECTOR = True
except ImportError:
    HAS_VECTOR = False


def _open_db(cfg):
    db_path = cfg.data_dir / "memory.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _get_db(cfg):
    """Yield a connection and always close it."""
    conn = _open_db(cfg)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now', 'localtime')),
            content TEXT NOT NULL,
            category TEXT DEFAULT 'event'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS facts (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    legacy_file = cfg.data_dir / "memory.jsonl"
    if legacy_file.exists():
        try:
            with open(legacy_file, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                        conn.execute(
                            "INSERT INTO memories (timestamp, content) VALUES (?, ?)",
                            (data.get("t"), data.get("text"))
                        )
                    except (json.JSONDecodeError, sqlite3.Error):
                        continue
            conn.commit()
            legacy_file.rename(cfg.data_dir / "memory.jsonl.bak")
        except Exception as e:
            print(f"Memory migration failed: {e}")
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _get_vector_db(cfg):
    if not HAS_VECTOR:
        return None
    db_path = cfg.data_dir / "lancedb"
    db = lancedb.connect(db_path)
    if "memories" not in db.table_names():
        return db
    return db.open_table("memories")


def _get_embeddings(cfg, texts):
    """Get embeddings using OpenAI-compatible API."""
    api_key = os.environ.get("AGENT_API_KEY", cfg.api_key)
    base_url = os.environ.get("AGENT_BASE_URL", cfg.base_url)
    model = os.environ.get("AGENT_EMBEDDING_MODEL", "text-embedding-3-small")

    client = OpenAI(api_key=api_key, base_url=base_url)
    try:
        response = client.embeddings.create(input=texts, model=model)
        return [item.embedding for item in response.data]
    except Exception as e:
        print(f"Embedding error: {e}")
        return None


def _normalise(text):
    """Lowercase + collapse whitespace, for dedupe comparison."""
    return " ".join((text or "").lower().split())


def _recent_dupe(cfg, text, window=20):
    """Return True if `text` (normalised) already exists in the last `window`
    memories. Cheap local dedupe so auto-save doesn't pile up near-identical
    entries."""
    target = _normalise(text)
    if not target:
        return False
    with _get_db(cfg) as conn:
        rows = conn.execute(
            "SELECT content FROM memories ORDER BY id DESC LIMIT ?", (window,)
        ).fetchall()
    return any(_normalise(r["content"]) == target for r in rows)


@tool("memory_save", "Save a durable fact/preference/note for future sessions.",
      {"text": {"type": "string"}}, ["text"])
def memory_save(cfg, text):
    # Local dedupe: skip if an identical (normalised) entry was saved recently.
    if _recent_dupe(cfg, text):
        return "skipped (duplicate of a recent memory)"
    with _get_db(cfg) as conn:
        with conn:
            conn.execute("INSERT INTO memories (content) VALUES (?)", (text,))

    if HAS_VECTOR:
        try:
            embeddings = _get_embeddings(cfg, [text])
            if embeddings:
                db = lancedb.connect(cfg.data_dir / "lancedb")
                table_name = "memories"
                data = [{
                    "vector": embeddings[0],
                    "text": text,
                    "timestamp": time.strftime("%Y-%m-%d %H:%M")
                }]
                if table_name in db.table_names():
                    table = db.open_table(table_name)
                    table.add(data)
                else:
                    db.create_table(table_name, data=data)
        except Exception as e:
            print(f"Vector save failed: {e}")

    return "saved"


@tool("memory_search",
      "Search saved memory by keywords or semantic meaning. Returns up to 20 matches.",
      {"query": {"type": "string"}}, ["query"])
def memory_search(cfg, query=""):
    if not query:
        with _get_db(cfg) as conn:
            rows = conn.execute(
                "SELECT timestamp, content FROM memories ORDER BY id DESC LIMIT 20"
            ).fetchall()
        if not rows:
            return "(no memory yet)"
        return "\n".join(f"[{r['timestamp']}] {r['content']}" for r in reversed(rows))

    if HAS_VECTOR:
        try:
            embedding = _get_embeddings(cfg, [query])
            if embedding:
                db = lancedb.connect(cfg.data_dir / "lancedb")
                if "memories" in db.table_names():
                    table = db.open_table("memories")
                    results = table.search(embedding[0]).limit(20).to_list()
                    if results:
                        lines = [f"[{r['timestamp']}] {r['text']}" for r in results]
                        return "\n".join(lines)
        except Exception as e:
            print(f"Vector search failed: {e}")

    with _get_db(cfg) as conn:
        words = query.lower().split()
        placeholders = " AND content LIKE ?" * len(words)
        params = [f"%{w}%" for w in words]

        rows = conn.execute(
            f"SELECT timestamp, content FROM memories WHERE 1=1 {placeholders} ORDER BY id ASC LIMIT 20",
            params
        ).fetchall()

    if not rows:
        return "no matches"

    lines = [f"[{r['timestamp']}] {r['content']}" for r in rows]
    return "\n".join(lines)


@tool("memory_store_fact", "Store a structured fact or preference (e.g. 'user_name', 'theme').",
      {"key": {"type": "string"}, "value": {"type": "string"}}, ["key", "value"])
def memory_store_fact(cfg, key, value):
    with _get_db(cfg) as conn:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO facts (key, value, updated_at) VALUES (?, ?, datetime('now', 'localtime'))",
                (key, value)
            )
    return f"Fact '{key}' stored."


@tool("memory_get_fact", "Retrieve a specific structured fact by its key.",
      {"key": {"type": "string"}}, ["key"])
def memory_get_fact(cfg, key):
    with _get_db(cfg) as conn:
        row = conn.execute("SELECT value FROM facts WHERE key = ?", (key,)).fetchone()
    if row:
        return row["value"]
    return "Fact not found."


@tool("memory_list_facts", "List all stored structured facts.", {}, [])
def memory_list_facts(cfg):
    with _get_db(cfg) as conn:
        rows = conn.execute("SELECT key, value FROM facts ORDER BY key ASC").fetchall()
    if not rows:
        return "No facts stored."
    return "\n".join(f"{r['key']}: {r['value']}" for r in rows)


@tool("memory_delete",
      "Delete a specific memory by its numeric id, timestamp, or content substring. "
      "If multiple matches are found, it returns the IDs for clarification.",
      {"query": {"type": "string"}}, ["query"])
def memory_delete(cfg, query):
    q = (query or "").strip()
    if not q:
        return "No query provided."

    with _get_db(cfg) as conn:
        # Numeric id: delete that exact row.
        row = None
        if q.isdigit():
            row = conn.execute(
                "SELECT id, timestamp, content FROM memories WHERE id = ?", (int(q),)
            ).fetchone()
        if row is None:
            # Exact content match (what the UI sends), then LIKE fallback.
            rows = conn.execute(
                "SELECT id, timestamp, content FROM memories WHERE content = ?",
                (q,)
            ).fetchall()
            if not rows:
                rows = conn.execute(
                    "SELECT id, timestamp, content FROM memories WHERE content LIKE ? OR timestamp LIKE ?",
                    (f"%{q}%", f"%{q}%")
                ).fetchall()
            if not rows:
                return f"No memories found matching '{q}'."
            if len(rows) > 1:
                lines = [f"ID {r['id']} [{r['timestamp']}]: {r['content']}" for r in rows]
                return "Multiple matches found. Please specify the ID to delete:\n" + "\n".join(lines)
            row = rows[0]

        target_id, target_content = row["id"], row["content"]
        with conn:
            conn.execute("DELETE FROM memories WHERE id = ?", (target_id,))

    if HAS_VECTOR:
        try:
            db = lancedb.connect(cfg.data_dir / "lancedb")
            if "memories" in db.table_names():
                table = db.open_table("memories")
                table.delete(f"text = {json.dumps(target_content)}")
        except Exception as e:
            print(f"Vector delete failed: {e}")

    return f"Deleted memory: [{row['timestamp']}] {target_content}"


@tool("memory_delete_fact", "Delete a specific structured fact by its key.",
      {"key": {"type": "string"}}, ["key"])
def memory_delete_fact(cfg, key):
    with _get_db(cfg) as conn:
        with conn:
            cursor = conn.execute("DELETE FROM facts WHERE key = ?", (key,))
            if cursor.rowcount > 0:
                return f"Fact '{key}' deleted."
    return f"Fact '{key}' not found."


@tool("memory_clear_all", "WIPE ALL MEMORY (both facts and events). Use with extreme caution.", {}, [])
def memory_clear_all(cfg):
    with _get_db(cfg) as conn:
        with conn:
            conn.execute("DELETE FROM memories")
            conn.execute("DELETE FROM facts")

    if HAS_VECTOR:
        try:
            db = lancedb.connect(cfg.data_dir / "lancedb")
            if "memories" in db.table_names():
                db.drop_table("memories")
        except Exception as e:
            print(f"Vector clear failed: {e}")

    return "All memories and facts have been wiped."


def recent(cfg, n=15):
    """Return the latest n entries as bullet points, for the system prompt."""
    try:
        with _get_db(cfg) as conn:
            rows = conn.execute(
                "SELECT content FROM memories ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
        if not rows:
            return ""
        return "\n".join(f"- {r['content']}" for r in reversed(rows))
    except Exception:
        return ""


def get_all_facts_formatted(cfg):
    """Return all facts as a string for the system prompt."""
    try:
        with _get_db(cfg) as conn:
            rows = conn.execute("SELECT key, value FROM facts ORDER BY key ASC").fetchall()
        if not rows:
            return ""
        return "\n".join(f"- {r['key']}: {r['value']}" for r in rows)
    except Exception:
        return ""