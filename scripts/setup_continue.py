"""Writes ~/.continue/config.yaml using the *primary pool* from
data/providers.json (not the env fallback — the fallback is usually empty
now that keys live in the pool manager).

Usage:
    python scripts/setup_continue.py

The generated file makes VS Code's Continue extension talk to the same
provider and model Omni's primary pool is configured with.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent import config  # noqa: E402


def main() -> int:
    cfg = config.load()
    pm = cfg.providers
    pools = pm.summary()
    if not pools:
        print("No keys configured yet.", file=sys.stderr)
        print("Add one in the web UI (Keys & models) or from the terminal:  /add AIza...",
              file=sys.stderr)
        return 1

    primary = pools[0]
    pool_name = primary["pool"]
    # Pick the first key in the pool to mirror what Omni would use first.
    keys = pm.providers_in(pool_name)
    if not keys:
        print(f"Pool '{pool_name}' has no keys.", file=sys.stderr)
        return 1
    key = keys[0]

    tpl_path = config.ROOT / "config" / "continue-config.yaml"
    if not tpl_path.exists():
        print(f"missing template: {tpl_path}", file=sys.stderr)
        return 1

    out = (tpl_path.read_text(encoding="utf-8")
           .replace("__MODEL__", primary["model"])
           .replace("__BASE_URL__", primary["base_url"])
           .replace("__API_KEY__", key.api_key))

    dest = Path.home() / ".continue" / "config.yaml"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        backup = dest.with_suffix(".yaml.bak")
        backup.write_text(dest.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"existing config backed up to {backup.name}")
    dest.write_text(out, encoding="utf-8")
    print(f"wrote {dest}")
    print(f"  using pool '{pool_name}' -> {primary['model']} @ {primary['base_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())