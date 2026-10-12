# Omni Agent — your own AI harness (no Docker)

A working agent you control: it talks to **any OpenAI-compatible gateway** (OmniRoute → OpenRouter, or Ollama, OpenAI, etc.)
and has tools to **read/write/edit code precisely, edit Word/Excel/PowerPoint, run shell commands, browse URLs,
remember things, and run scheduled automations.** Core uses only the Python standard library.

## 1. Install
Linux/macOS/WSL: `./install.sh`   |   Windows: `install.bat`

## 2. Start the gateway (OmniRoute) - or skip it, see "Low-end PC" below
```
npm install -g omniroute
omniroute            # dashboard: http://localhost:20128
```
In the dashboard: add provider **OpenRouter** (paste your OpenRouter key) → create an **API key** → note a model name/alias.

## 3. Configure
Edit `.env` (copied from `.env.example`): `AGENT_BASE_URL`, `AGENT_API_KEY`, `AGENT_MODEL`.
Test: `./omni check` (Windows: `omni.bat check`)

## 4. Use
| Goal | Command |
|---|---|
| Interactive chat | `./omni` |
| One-shot task | `./omni run "In report.docx replace 'Q2' with 'Q3' everywhere"` |
| Task from file | `./omni run -f workflows/review_code.md` |
| Automation loop | `./omni schedule -f workflows/daily_notes_cleanup.md --every 86400` |
| Real cron | `0 9 * * * cd /path/omni-agent && AGENT_AUTO_APPROVE=1 ./omni run -f workflows/daily_notes_cleanup.md` |

Put files in `workspace/` (or change `AGENT_WORKSPACE`). The agent cannot touch anything outside it unless `AGENT_ALLOW_OUTSIDE=1`.
Shell commands and outside-workspace access ask for your approval unless `AGENT_AUTO_APPROVE=1` (needed for unattended cron runs — use only in folders you trust).

## Providers, keys, models and automatic failover
Everything can be changed from inside the chat, no file editing needed:
```
/add openrouter sk-or-...        # presets: openrouter gemini omniroute openai groq deepseek ollama
/add gemini AIza...
/add mine KEY my-model https://host/v1     # any OpenAI-compatible endpoint
/providers                       # list; first = primary, the rest = fallbacks in order
/key openrouter                  # change a key (hidden prompt)
/model gemini-2.5-pro            # change primary's model   (or: /model gemini <model>)
/use gemini   /order gemini,openrouter   /remove name   /test [name]
```
If the primary fails (rate limit 429, bad/expired key 401/403, server error, timeout, unknown model) the agent automatically
tries the next provider and prints a notice. A failed provider is skipped for a while (60s for rate limits,
10 min for bad keys), then the primary is used again. Same works non-interactively: `./omni provider list`, `./omni provider add gemini KEY`.
To rotate several keys of the same service, add them under different names (`/add openrouter-2 KEY2`).
Keys are saved in `data/providers.json` (permissions 600, plain text, git-ignored) - keep that folder private.

## 5. VS Code
`python scripts/setup_continue.py` writes `~/.continue/config.yaml` from your `.env`. Install the **Continue** extension, then Ctrl+L to chat/edit.
(Back up is made automatically if you already have a config.)

## 6. Optional: Hermes
`config/hermes-config.yaml` + `python scripts/setup_hermes.py` point Hermes at the same gateway.
Install Hermes with its official installer; check its docs for the exact env-var name for custom endpoints.

## Tools the agent has
`read_file write_file edit_file list_dir search_files run_shell web_fetch memory_save memory_search
docx_read docx_edit_paragraph docx_replace_text docx_insert_paragraph docx_create xlsx_read xlsx_set_cell pptx_read`

## Extending
Add a function with `@tool(...)` in `agent/tools/` (see `files.py`), import it in `agent/tools/__init__.py`. That's all — the model sees it automatically.

## Tests
`python tests/test_agent.py` runs the full tool loop against a mock server, offline.

## Tips for best results
Pick a strong tool-calling model (Claude Sonnet-class or similar) in OmniRoute; small local models often fail at tool use.

## Low-end PC (no Docker, light on resources)
- Nothing here needs Docker. The models run in the cloud (OpenRouter), not on your machine.
- Lightest setup: skip OmniRoute entirely and set in `.env`:
  `AGENT_BASE_URL=https://openrouter.ai/api/v1`, `AGENT_API_KEY=<your OpenRouter key>`, `AGENT_MODEL=anthropic/claude-sonnet-4.5` (or any model id from openrouter.ai/models).
- Add OmniRoute later only if you want its routing features; it is a plain Node.js process (`npm install -g omniroute`).
