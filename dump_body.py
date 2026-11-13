# dump_body.py — dumps the exact JSON the agent sends to the provider,
# with the API key redacted. Reproduces the same request the failing
# chat sent, so we can see which tool index is tools[3].
import json
import os
from agent import config, llm
from agent.tools import schemas

cfg = config.load()
pool = cfg.providers.pool_names()[0]
p = cfg.providers.providers_in(pool)[0]

body = {
    "model": p.model,
    "messages": [
        {"role": "system", "content": "test"},
        {"role": "user", "content": "hi"},
    ],
    "tools": schemas(),
    "tool_choice": "auto",
    "temperature": 0.2,
}

# Print the tools array with indexes so we can see which one is tools[3].
print("=== tools array (index : name) ===")
for i, t in enumerate(body["tools"]):
    print(f"[{i}] {t['function']['name']}")

print()
print("=== full JSON body (redacted) ===")
text = json.dumps(body, indent=2, ensure_ascii=False)
# crude redaction, just in case
text = text.replace(p.api_key or "___nokey___", "***REDACTED***")
print(text)