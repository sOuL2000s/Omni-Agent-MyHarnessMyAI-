# Omni Agent

**Your own AI harness — on your own machine, with your own keys, on your own terms.**

Omni is a self-hosted AI agent that runs as a single `.exe` (no Python needed for
end users) and talks to **any OpenAI-compatible service** — Google Gemini,
OpenRouter, OpenAI, Anthropic, Groq, DeepSeek, Ollama, LM Studio, OmniRoute, or
your own gateway. It reads and edits code, edits Word/Excel/PowerPoint files,
runs shell commands, browses the web, browses **your entire PC**, and remembers
things across sessions. It rotates API keys and falls back across models by
itself, so one dead key or exhausted free quota never stops your work. It can
also connect to **MCP servers** (Model Context Protocol) so the model gains new
tools without you writing a line of code.

---

## Table of contents

1. [What it is (and what it is not)](#1-what-it-is-and-what-it-is-not)
2. [Feature tour](#2-feature-tour)
3. [Quick start — the 5-minute path](#3-quick-start--the-5-minute-path)
4. [Getting an API key](#4-getting-an-api-key)
5. [Running it — every mode explained](#5-running-it--every-mode-explained)
6. [The web UI, in detail](#6-the-web-ui-in-detail)
7. [The terminal, in detail](#7-the-terminal-in-detail)
8. [How keys, pools, models and failover work](#8-how-keys-pools-models-and-failover-work)
9. [Building the standalone .exe](#9-building-the-standalone-exe)
10. [Configuration reference (`.env`)](#10-configuration-reference-env)
11. [Safety model](#11-safety-model)
12. [Reducing hallucination (strict mode)](#12-reducing-hallucination-strict-mode)
13. [MCP servers](#13-mcp-servers)
14. [Extending Omni — adding your own tools](#14-extending-omni--adding-your-own-tools)
15. [Running the tests](#15-running-the-tests)
16. [Troubleshooting](#16-troubleshooting)
17. [FAQ](#17-faq)
18. [Design notes & limitations (read this)](#18-design-notes--limitations-read-this)

---

## 1. What it is (and what it is not)

### What Omni is

- A **local agent runtime**. The brain is a remote LLM you already have access
  to; Omni is the harness that lets that LLM read files, run commands, and
  produce results on *your* computer.
- **Multi-provider by design.** One config, many services. Free Gemini keys,
  a paid Claude key, a local Ollama model — Omni picks whichever is available
  right now and rotates between them automatically.
- **A GUI-first desktop app** (built on tkinter, bundled into one `.exe`) with
  a **full web UI** for chat, key management, file handling, and MCP servers.
- **Standard-library core.** The agent itself uses only Python's standard
  library. The extra pip packages (`python-docx`, `openpyxl`, `python-pptx`)
  are only for Word/Excel/PowerPoint editing — you can ignore them and still
  use the agent for everything else. `selenium` is opt-in for JS-rendered
  pages.

### What Omni is not

- **Not a hosted service.** Nothing is sent anywhere except the LLM calls you
  make and the MCP servers you explicitly connect. Your keys, files, and
  history live in a `data/` folder next to the exe.
- **Not a wrapper around someone else's agent.** No LangChain, no AutoGen, no
  framework. It's a small, readable, auditable codebase.
- **Not immune to hallucination.** No LLM is. Omni forces the model to call a
  tool before asserting facts about your machine, and strict mode flags
  unverified claims in the answer — but see [§18](#18-design-notes--limitations-read-this).
- **Not a magic replacement for judgment.** If you enable auto-approve and let
  it run shell commands unreviewed, it can delete your files. See [§11](#11-safety-model).

---

## 2. Feature tour

**Every feature that matters, in plain language:**

| Feature | What it means for you |
|---|---|
| **Runs anywhere** | One `.exe` (Windows), one folder (macOS/Linux). No Python, no Docker, no dependencies. |
| **Any LLM** | Gemini, OpenRouter, OpenAI, Anthropic, Groq, DeepSeek, Ollama, LM Studio, OmniRoute, or any OpenAI-compatible URL. |
| **Multi-key rotation** | Add 30 Gemini keys; Omni spreads calls across them by health score and cools down the one that just hit a quota. |
| **Model fallback** | If `gemini-2.5-pro` runs out of quota, it tries `gemini-2.5-flash` on the *same key* automatically. |
| **Provider failover** | If Gemini is entirely down, it moves on to your OpenRouter pool, then your Groq pool. |
| **Auto key detection** | Paste `AIza...` — Omni knows that's Gemini. `sk-or-v1-...` — OpenRouter. `gsk_...` — Groq. |
| **Auto model discovery** | Every key fetches its available model list from the provider. You pick from a typeable dropdown (important for Gemini/OpenRouter, which list 100+ models). |
| **Full filesystem access** | Reads and writes *anywhere on your PC*, with a confirmation prompt outside the workspace. A filesystem root (`/`, `C:\`) is refused as a workspace because it would silently disable this guard. |
| **Word / Excel / PowerPoint** | Edits `.docx`, `.xlsx`, `.pptx` while preserving formatting. |
| **Shell** | Runs commands in a shell, with a blocklist for catastrophic ones (`rm -rf /`, `mkfs`, fork bombs). It is a **speed bump, not a guard** — approval prompts are the real protection. |
| **Background shells** | A header toggle (or `/bgauto on`) makes every `run_shell` call detach by default — dev servers survive Stop. |
| **Web fetch** | Reads any URL, strips HTML, returns plain text. Fast (`urllib`) by default. Set `AGENT_BROWSER=selenium` to render JavaScript pages with a headless Chrome (source installs only — see §9.3). |
| **Long-term memory** | Save facts, preferences, notes; they come back in every future session. |
| **Chat history** | Every session is persisted in `data/sessions/`; the sidebar switches between them. |
| **MCP servers** | Connect any Model Context Protocol server (filesystem, github, sqlite, youtube, browser automation, …) — add, edit, and delete them from the web UI, or pick one from the live registry. |
| **Strict mode** | After a tool-using answer, a second model call verifies factual claims and flags unverified ones. |
| **Approval flow** | Shell commands and writes outside the workspace pop up an Allow/Deny dialog. |
| **Auto-approve** | One toggle (or env var) skips all prompts. For sandboxes and trusted workspaces. |
| **Workspace picker** | Click the workspace chip in the header to browse your entire PC and change the root on the fly. A confirmation explains what "inside" means before applying. |
| **Responsive sidebar** | Session list and shell jobs each scroll independently; on narrow screens the sidebar is an overlay. |
| **Stop button** | Cancels a running task mid-flight, releases pending approvals, kills foreground child processes (detached jobs keep running). |
| **Zero-config start** | Install, add one key, start working. Everything else is optional. |

---

## 3. Quick start — the 5-minute path

### For end users (with the pre-built `.exe`)

If someone gave you a `omni.dist` folder or a single `omni.exe`:

**Windows**
1. Unzip if needed.
2. Double-click `omni.exe`.
3. A small launcher window opens with two buttons: **Terminal chat** and **Web UI**.
4. Click **Web UI**. Your browser opens.
5. Click **Keys & models** (top right) → **Add a service** → pick a preset (e.g. `gemini`) → paste your API key → click **Add**.
6. Click **Test one** to confirm the key works. The dot next to the key turns green.
7. Close the panel. Start chatting.

**macOS / Linux**
1. Unzip if needed.
2. `cd` into the folder and run `./omni` (opens the launcher GUI on systems with a display).
3. Same as above from step 3.

You do **not** need Python, pip, Node.js, Docker, or anything else installed.

### For developers (running from source)

If you have the source folder and want to run/modify it:

```
# Windows
install.bat

# macOS / Linux / WSL
./install.sh
```

Then:

```
# Windows
.venv\Scripts\activate
python -m agent           # launcher GUI
python -m agent ui        # web UI directly
python -m agent chat      # terminal chat directly

# macOS / Linux
source .venv/bin/activate
python -m agent
python -m agent ui
python -m agent chat
```

Add your first key either through the UI, or from the terminal:

```
/add AIzaSy...your-real-gemini-key...
```

That's it. The agent is ready.

### For builders (producing the `.exe`)

See [§9 Building the standalone .exe](#9-building-the-standalone-exe). TL;DR:
`install.bat`, `python -m pip install -r requirements-build.txt`,
`build.bat`, then `dist\omni.dist\omni.exe`.

---

## 4. Getting an API key

You need at least one. All of these have a **free tier** (or free models):

### Google Gemini — recommended for beginners

- **Where:** https://aistudio.google.com/apikey
- **Key looks like:** `AIzaSy...` (39 characters, starts with `AIza`)
- **Free tier:** yes, generous, but **quotas are per Google Cloud project, not
  per key**. If you create all your keys in the same project, they share one
  quota and rotation doesn't add capacity. Create each key in its own project
  (AI Studio → *Create API key in new project*) for real rotation.
- **Privacy note:** free-tier prompts may be used by Google for training.
  Don't send confidential data through free Gemini keys.
- **What Omni does with it:** auto-detects the provider as `gemini`, pulls the
  model list, and defaults to `gemini-2.5-flash`.

### OpenRouter — the "everything" aggregator

- **Where:** https://openrouter.ai/keys
- **Key looks like:** `sk-or-v1-...`
- **Free tier:** a rotating set of free models (models with `:free` suffix).
- **What Omni does:** auto-detects `openrouter`, pulls the (long) model list,
  defaults to `anthropic/claude-sonnet-4.5` if you have credit, or you can
  switch to a free model.

### Groq — the fastest free option

- **Where:** https://console.groq.com/keys
- **Key looks like:** `gsk_...`
- **Free tier:** yes, very fast inference on Llama/Mixtral.
- **What Omni does:** auto-detects `groq`, defaults to `llama-3.3-70b-versatile`.

### OpenAI / Anthropic — paid, strongest models

- **OpenAI:** https://platform.openai.com/api-keys, key `sk-proj-...` or `sk-...`
- **Anthropic:** https://console.anthropic.com/settings/keys, key `sk-ant-...`
- **What Omni does:** auto-detects, defaults to `gpt-4o` / `claude-sonnet-4-5`.

### Ollama / LM Studio — local, private, free

- Install Ollama (https://ollama.com) or LM Studio, start the server.
- Ollama listens on `http://localhost:11434/v1` by default.
- **What Omni does:** no key needed; add a pool named `ollama` with any
  placeholder string as the key, and it works.

### Which one should I pick?

- **Want zero cost, decent quality:** Gemini free tier.
- **Want the best quality, don't mind paying:** Anthropic or OpenAI.
- **Want privacy (nothing leaves your machine):** Ollama or LM Studio.
- **Want everything at once:** OpenRouter (one key, many models).
- **Want speed:** Groq.

You can add **all of them** and Omni will pick whichever is healthiest.
That's the whole point.

---

## 5. Running it — every mode explained

There are three ways to launch Omni, and each supports several subcommands.
Everything below works the same whether you run from source (`python -m agent`)
or from the exe (`omni.exe`).

### 5.1 The launcher GUI (default when you double-click)

```
omni.exe               (no arguments)
```

Shows a small native window:

```
┌──────────────────────────────────────┐
│           Omni Agent                 │
│         Choose how to start          │
│                                      │
│  ┌────────────────┐ ┌──────────────┐ │
│  │  Terminal chat │ │    Web UI    │ │
│  └────────────────┘ └──────────────┘ │
│ ──────────────────────────────────── │
│               [ Quit ]               │
│                                      │
│ Workspace: C:\Users\you              │
└──────────────────────────────────────┘
```

- **Terminal chat** → opens a new terminal window running the CLI chat. The
  launcher stays open so you can also start the web UI.
- **Web UI** → starts the server in the background and opens your browser. The
  status line shows the actual URL (the port may have been bumped to avoid a
  conflict); closing the launcher stops the server.
- **Quit** → closes everything.

To skip the launcher and go straight to the terminal, either run
`omni.exe chat`, or set `OMNI_NO_GUI=1` in your environment.

### 5.2 Subcommands

Every subcommand is available in both the source build and the exe. Replace
`omni.exe` with `python -m agent` if you're running from source.

| Command | What it does |
|---|---|
| `omni.exe` | Launcher GUI (or terminal chat if no GUI available) |
| `omni.exe chat` | Interactive terminal session (the REPL) |
| `omni.exe ui` | Web UI on `http://localhost:8765`, opens browser |
| `omni.exe ui --port 9000` | Web UI on a custom port |
| `omni.exe ui --no-browser` | Web UI without auto-opening the browser |
| `omni.exe ui --host 0.0.0.0` | **⚠ Exposes to your whole network.** Read §11 first. |
| `omni.exe run "task"` | One-shot task: does it, prints the result, exits |
| `omni.exe run -f workflows/review_code.md` | One-shot from a task file |
| `omni.exe schedule -f task.md --every 86400` | Repeats a task on an interval (seconds) |
| `omni.exe check` | Tests one key from the primary pool |
| `omni.exe check --all` | Tests every key |
| `omni.exe models gemini` | Lists the models Gemini offers |
| `omni.exe models gemini flash` | Lists Gemini models matching "flash" |
| `omni.exe provider list` | Shows every pool and key |
| `omni.exe provider add gemini AIza...` | Adds a key from the CLI |
| `omni.exe provider addkeys gemini K1 K2 K3` | Adds many keys at once |
| `omni.exe provider model gemini gemini-2.5-pro` | Changes the pool's model |
| `omni.exe provider fallback gemini gemini-2.5-flash-lite` | Sets fallback models |
| `omni.exe provider strategy round_robin` | Rotation strategy |
| `omni.exe provider use openrouter` | Makes a pool primary |
| `omni.exe provider remove gemini-3` | Removes one key |
| `omni.exe provider revive` | Clears all cooldowns (retry immediately) |

### 5.3 The wrapper scripts (source tree only)

If you installed from source, the short scripts in the project root do the
same thing with less typing:

| Windows | macOS / Linux | Same as |
|---|---|---|
| `omni.bat` | `./omni` | `python -m agent` |
| `omni-ui.bat` | `./omni-ui` | `python -m agent ui` |
| `build.bat` | (use `python build.py`) | `python build.py` |

They activate the virtualenv automatically.

### 5.4 Where does Omni store its stuff?

Everything lives in `data/`, next to the exe (or in the project root for a
source install):

```
data/
├── providers.json   ← your API keys (plain text, chmod 600 on Unix)
├── settings.json    ← strategy, fallbacks, max_wait
├── state.json       ← cooldowns (so a daily quota hit is remembered after restart)
├── memory.jsonl     ← long-term memory (what you told it to remember)
├── mcp.json         ← MCP server definitions (added via UI, never auto-run)
├── mcp_registry.json← cache of the official MCP registry (1h TTL)
├── jobs/            ← logs of detached shell jobs (uuid.log)
└── sessions/
    ├── <id>.json    ← one file per chat
    └── <id>.meta.json  ← optional title override
```

**Treat `data/` as a secret.** `providers.json` contains your keys in plain
text, and `mcp.json` may contain API tokens for MCP servers. If you back up
your PC, exclude this folder or encrypt it. The `.gitignore` already excludes
it from version control.

---

## 6. The web UI, in detail

The web UI is Omni's primary interface. Everything you can do in the terminal
you can do here, plus file uploads, a live keys panel, and full MCP
management.

### 6.1 Layout

```
┌───────────┬──────────────────────────────────────────┬──────────────┐
│           │  workspace chip    answering: gemini ·   │              │
│  + New    │                    gemini-2.5-flash     │  Keys &      │
│  chat     │     strict  auto-approve  bg-shell       │  models      │
│           ├──────────────────────────────────────────┤              │
│  session  │                                          │  Rotation    │
│  list     │  Chat messages and tool cards appear      │  ────────    │
│  (scroll) │  here                                    │  gemini  ★   │
│           │                                          │  [model]     │
│  shell    │                                          │  [fallback]  │
│  jobs     │                                          │  keys...     │
│  (scroll) ├──────────────────────────────────────────┤              │
│           │  📎  [type here…]           Send / Stop   │  Files       │
└───────────┴──────────────────────────────────────────┴──────────────┘
```

- **Left sidebar** — `+ New chat`, a **Search chats…** box, then two
  independently scrollable sections:
  - **Shell jobs** — every detached `run_shell` process. Shows command, a
    ● running / ○ exited dot, and a `stop` button. Live-refreshed every 5
    seconds.
  - **Recent** — chat sessions. `✎` renames, `✕` deletes, `●` marks a chat
    that's still working. Long titles end in `…`.
- **Header** — workspace chip (click to browse your PC and change the root),
  a chip showing which pool/model answered last (click for the full route:
  pool · key · model · reason), and the **strict** / **auto-approve** /
  **bg-shell** toggle group, then the **Keys & models** button.
- **Center** — the conversation. Tool calls show as collapsible cards. Shell
  commands and outside-workspace writes pop up an Allow/Deny dialog.
- **Right panel** — Keys (default), MCP, Files. Opens with the **Keys &
  models** button or the three tabs.

### 6.2 The header toggles

- **strict** (on by default) — after a tool-using answer, a second model call
  checks every factual claim against the tool outputs. Unverified ones are
  flagged. Costs a small amount of extra tokens per answer.
- **auto-approve** (off by default) — skips all Allow/Deny dialogs. **Only
  enable in a workspace you trust.** A red banner reminds you when it's on.
- **bg-shell** (off by default) — makes every `run_shell` call start as a
  **detached background job**. It returns immediately with a job id and the
  process keeps running even after you press Stop. Useful for dev servers
  (`npm run dev`, `uvicorn`, `flask run`, `dotnet watch`). Individual calls
  can override it by passing `background=false`, and the model's system
  prompt still tells it to use `background=true` for servers regardless of
  the global setting. See §6.3 for the escape hatches.

Each toggle is a pill you can click; state is kept per-process (bg-shell) or
per-session (strict, auto-approve) — see §8.7 for why.

### 6.3 Chat

Type in the box, press Enter (Shift+Enter for a newline). Everything else is
automatic:

- The model decides to call a tool (e.g. `read_file`). You see a card with the
  tool name and arguments. Expand it to see the raw arguments.
- The tool runs. The card gets a green dot (success) or red dot (error), and
  the output appears inside it. Long-running shell commands stream their output
  as it happens (coalesced to ~10 updates/sec so the UI stays responsive).
- The model continues with the result. You see its reasoning as a dimmed
  "working" message, then the final answer.
- If the model switched keys or models mid-answer (because of a quota hit),
  you see an amber `↪ using fallback …` notice, and the progress card shows
  which key is being retried and why (humanized: "gemini-3 (gemini-2.5-flash)
  rate-limited").
- If strict mode is on and the answer contains claims the model didn't
  actually verify with a tool, an amber `⚠ Unverified claim(s)…` banner
  appears under the answer.
- If all keys fail, the error note has a **Retry** button so you don't have to
  retype the prompt.

Every message has **Copy** (and **Retry** on assistant/final answers); user
messages also have **Edit** and **Delete**. The user bubble is rendered with
its history index immediately, so Edit/Delete work before the run finishes.

### 6.4 The **Keys & models** panel

**Rotation card** — choose `round_robin` (spread calls over all keys) or
`failover` (always start with the first key, use the rest only when it fails).
See [§8](#8-how-keys-pools-models-and-failover-work).

**Models card** — a single **↻ Refresh all model lists** button. It pings
every provider with every key and re-populates the dropdowns. Only needed if a
provider added new models since you last used it.

**One card per service** (pool):

- **Header** — pool name, badges showing ready / cooling / invalid key counts,
  and ▲▼ buttons to change the pool's priority relative to other pools.
- **Base URL** — the service endpoint.
- **Model row** — the pool's default model (used when a key has no override),
  an **Apply** button (changes the model for all keys of the pool at once),
  and a **List…** button (fetches the live model list). The model field is a
  **typeable** `<input list>` — Gemini and OpenRouter list 100+ models, so a
  native dropdown would be unusable. Type to filter.
- **Fallback row** — comma-separated list of models to try when the chosen
  one hits a quota. **Quotas are per model** — if `gemini-2.5-pro` is used up,
  `gemini-2.5-flash-lite` on the *same key* still works. Set one or two.
- **Key list** — every key with a health dot, its masked form, its stats, a
  **typeable model box per key** (choose a different model for a specific
  key), a ↻ button to refresh that one key's models, a **test** button, and
  ✕ to remove it.
- **Add keys to <pool>** — paste more keys, one per line. A paste-in-progress
  survives re-renders of the panel.
- **Test one / Test all** — real API calls. Shows which keys work.
- **Reset cooldowns** — clears health state for the pool.
- **Remove service** — deletes the pool and all its keys.

**Add a service card** — the fast path. Pick a preset (`gemini`, `openrouter`,
`openai`, `groq`, `deepseek`, `anthropic`, `ollama`, or *Custom*), paste one or
more keys, click **Add**. Omni detects the provider from the key format if you
leave the name blank, fetches the model list, and picks a sensible default.
When there are no pools yet, this card is shown **at the top** of the panel so
a first-time user lands directly on it.

### 6.5 The **Files** panel

- **Upload…** or drag-and-drop files (onto the chat area or the composer) to
  upload them into the workspace. Files larger than 2 MB show upload progress.
- The list shows every file in the workspace with size and a download link.
- Uploaded files appear in the chat box as `[file in workspace: name.docx]`
  so you can immediately ask Omni to work on them.
- **Pasting or dropping more than ~20 KB of text** is intercepted and uploaded
  as a file (`pasted-<timestamp>.txt`) so the chat box stays responsive and
  the model doesn't choke on a 500 KB prompt.

### 6.6 The workspace picker

Click the **workspace chip** in the header. A modal opens showing your whole
PC:

- **⬆ Up** to go to the parent directory; **←** to go back to the previous
  location.
- **📂 Browse…** opens the OS folder picker on the **server's** machine (the
  only reliable way to get a native dialog for a path on the server's
  filesystem from a browser). If the server has no display, this returns 501
  and you type the path instead.
- **Drive buttons** on Windows to jump between `C:\`, `D:\`, etc.
- **Filter…** to search the current folder; tick **deep search** to search
  recursively (prunes `node_modules`, `__pycache__`, `.git`, `.venv` and stops
  after 200 hits so it never hangs on `C:\`).
- Click a folder to enter it, **Use this folder** to make it Omni's new root.
  A **confirmation dialog** warns you that files under the new folder will be
  treated as "inside" and won't prompt for approval. The picker refuses a
  filesystem root (`/`, `C:\`) because that would disable the
  outside-workspace approval guard. `data/` (and its parents) may be chosen,
  but `data/providers.json`, `data/state.json` and `data/memory.jsonl` are
  always refused by the file tools so the agent cannot read its own keys.
- **Recent folders** are remembered per browser so you can jump back quickly.

### 6.7 The **MCP** panel

The MCP tab has three sections:

1. **Installed servers** — one card per configured server. The card shows
   status (● running / ○ stopped), tool count, transport (stdio / http), the
   command or URL, the tool names, and any error from the last start attempt.
   - **Start / Stop** — toggles the subprocess (or the HTTP client).
   - **Restart** — stops and starts again with the same config.
   - **Delete** — removes the entry from `data/mcp.json`, stops the process,
     and unregisters its tools.
   - **▲ Edit** — expands an inline form to rename, change the command, args,
     URL, env vars, or HTTP headers, then **Save & restart**. The update path
     is stop → delete → add, so a broken config can't wedge the running one.

2. **Add an MCP server** — a full form with:
   - **Name**, **Command**, **Args** for a local stdio server.
   - **URL** for a remote Streamable-HTTP server. Leave the command blank
     and only the URL is used; the transport is inferred.
   - **Environment variables** — one `KEY=VALUE` per line. Values can contain
     `=` — the split is on the first `=`.
   - **HTTP headers** — one `Header: value` per line, for remote servers that
     need `Authorization: Bearer …`.

3. **Browse the official registry** — the live catalog from
   `registry.modelcontextprotocol.io`. Each row shows the server's **title,
   canonical name, description, runtime badge** (`npx` / `uvx` / `docker` /
   `http`), its **categories**, and its **required environment variables**
   (badged with `*`). Clicking a row **autofills** the Add form above:
   - For npm/pypi/docker packages: command (`npx` / `uvx` / `docker`), args,
     and env var names as `KEY=` so you know what to fill in.
   - For remote entries: the URL, and the transport switches to HTTP.
   - **↻ Refresh catalog** forces a re-fetch (it's cached for 1 hour).
   - Search matches name, title, description, categories, and runtime.

The registry endpoint never returns a 500 to the UI — a network failure
returns `{servers: [], error: "..."}` so the panel shows a hint and the
"Add an MCP server" form still works.

### 6.8 Strict mode and auto-approve toggles

- **strict** (on by default) — after a tool-using answer, a second model call
  checks every factual claim against the tool outputs. Unverified ones are
  flagged. Costs a small amount of extra tokens per answer. Turn it off for
  speed, on for accuracy. See [§12](#12-reducing-hallucination-strict-mode).
- **auto-approve** (off by default) — skips all Allow/Deny dialogs. **Only
  enable in a workspace you trust.** See [§11](#11-safety-model).
- **bg-shell** (off by default) — detach every shell call. See §6.2.

---

## 7. The terminal, in detail

The terminal chat is a REPL. Type a task, get a result. Type `/` commands to
manage keys, models, workspace, and shell behaviour without leaving the session.

### 7.1 Slash commands

```
/help                              list every command
/providers                         one line per pool (model, fallbacks, key health)
/keys [pool]                       every key with its health
/add [name] <key> [model] [url]    add a key; name is optional (auto-detected)
/addkeys <pool> <key> <key> ...    bulk add (or:  /addkeys gemini @keys.txt)
/key <name> [new_key]              change one key (prompted if omitted)
/model <model>                     set the model for the primary pool
/model <pool> <model>              set it for a specific pool
/models [pool] [filter]            list the models the service offers
/fallback <pool> m1,m2 | clear     set/try-clear fallback models
/strategy round_robin|failover     rotation strategy
/use <pool|key>                    make it primary
/order a,b,c                       pool priority order
/remove <key>                      remove one key
/removepool <pool>                 remove a whole pool
/test [key|pool|all]               real ping(s)
/revive                            clear all cooldowns
/refresh [key|pool|all]            re-fetch model lists
/keymodel <key> <model>            set the model for ONE key
/autodetect <key>                  guess the provider from key format
/workspace [path]                  show or change the agent's workspace
/bgauto on|off                     every run_shell call detaches in the background
/bg <task>                         run the task in the background (agent job)
/jobs                              list background agent + shell jobs
/kill <id>                         stop one background job by id
/reset                             clear chat history
/exit                              quit
```

### 7.2 The `/workspace` commandTwo forms:

```
/workspace                          # prints the current workspace
/workspace C:\projects\my-app       # absolute
/workspace ../sibling               # relative to the current workspace
/workspace "my folder with spaces"  # quoted
```

The guard is the same as the one at startup: a **filesystem root** (`/`,
`C:\`) is refused because that would silently disable the outside-workspace
approval prompt. `data/` and its parents are allowed; the credential files
inside `data/` are always blocked by `agent/safety.py`. A successful change:

- updates `cfg.workspace` for this process,
- refreshes the system prompt on the **next turn** (the loop already does
  this),
- is reflected immediately in the web UI's workspace chip if that process is
  also running (they share `data/`, but not the live `cfg`; you'd change it
  again in the UI if you want them to match).

### 7.3 The `/bgauto` command

```
/bgauto            # shows the current state
/bgauto on         # every run_shell call without explicit background runs detached
/bgauto off        # back to foreground streaming
```

Same effect as the **bg-shell** toggle in the header. Explicit
`background=false` in a tool call still wins; explicit `background=true`
always detaches. See §6.2 for why the default is off.

### 7.4 Example session

```
you> /add AIzaSyABC...your-gemini-key
added 'gemini-1' to pool 'gemini'. 47 models cached, using gemini-2.5-flash. /test gemini-1 to verify.

you> /test gemini-1
gemini-1 OK (gemini-2.5-flash): 'pong'

you> /workspace C:\projects\my-site
workspace -> C:\projects\my-site
(the next turn will use this path; the web UI is updated too)

you> Summarise the Python files in my project and write a summary.md
  → list_dir(path=".", depth=2)
  → search_files(pattern=".*", glob="*.py")
  → read_file(path="main.py")
  → read_file(path="utils.py")
  → write_file(path="summary.md", content="...")
Wrote 1234 chars to C:\projects\my-site\summary.md
Done. I read 4 Python files and wrote summary.md with a short overview of each.

you> /bgauto on
background shell executions = True

you> Start the dev server and tell me the URL
  → run_shell(command="npm run dev", background=true)      (auto-detach)
  → tail_background(job_id="a3f1c8e2")
Dev server is up at http://localhost:5173 — see the log in the Shell jobs panel.

you> /exit
(some background jobs are still running - use /kill N first if you want them gone)
```

### 7.5 One-shot mode

```
omni.exe run "In report.docx replace 'Q2' with 'Q3' and save as report_q3.docx"
```

Runs the task, prints the answer, exits. Useful in scripts, cron jobs, CI.
Combine with `AGENT_AUTO_APPROVE=1` in `.env` for fully unattended runs —
see the warning in [§11](#11-safety-model).

### 7.6 Scheduled mode

```
omni.exe schedule -f workflows/daily_notes_cleanup.md --every 86400
```

Runs the task file every 86400 seconds (24h). Stays alive until you Ctrl+C.
For real scheduling, use your OS's task scheduler (Windows Task Scheduler,
`cron`, `launchd`) and call `omni.exe run -f ...` — that's cleaner than
keeping a long-lived process.

### 7.7 Task files (workflows)

Anything you'd type can live in a Markdown file. Two examples ship in
`workflows/`:

- `review_code.md` — "Review the project, write REVIEW.md, don't edit sources."
- `daily_notes_cleanup.md` — "Fix spelling in `notes/`, append a summary."

Write your own. Reference paths, use imperative verbs, be specific about
outputs.

---

## 8. How keys, pools, models and failover work

This is the heart of Omni. Understanding it turns a fragile single-key setup
into a robust multi-provider one.

### 8.1 Pools

A **pool** is a group of keys for the same service. Keys named `gemini-1`,
`gemini-2`, ..., `gemini-37` all belong to pool `gemini`. A key with no
number (`openrouter`) is its own pool.

Pools have a **priority order** (set with ▲▼ in the UI or `/order`). The first
pool is *primary*; the others are used only when the primary can't serve the
request.

### 8.2 The request chain

For every request, Omni tries, in order:

```
for each pool (primary first):
    for each model in [chosen model, fallback models…]:
        for each usable key in the pool (scored best-first):
            try the request
              success → return
              failure → classify, cooldown the right thing, continue
```

Keys are **scored by health** (a Bayesian success rate with a small recency
penalty for recent failures), not blindly round-robined. In `round_robin`
mode the top three keys rotate to spread quota; in `failover` mode they are
tried strictly best-first.

### 8.3 Strategies

- **`round_robin`** — spread calls evenly across all healthy keys. Best for
  free-tier stacks where you want to use every key's quota equally.
- **`failover`** — always start with the first key; use the next only when the
  first fails. Best when you have one paid key and several backups.

### 8.4 What gets cooled down (and for how long)

| Response | Meaning | Scope | Cooldown |
|---|---|---|---|
| 429 (per minute) | key is too fast | this key + this model | `retryDelay` from the body (min 10 s) |
| 429 (per day) | daily quota used | this key + this model | until midnight Pacific (Google's reset) |
| 401 or `API_KEY_INVALID` | dead key | the key, all models | 6 h, shown as **INVALID KEY** |
| 402 / billing | no credits | the key, all models | 1 h |
| 404 / model not found | wrong model for the service | the pool + this model | 10 min (**whole pool skipped at once**, so 20 keys ≠ 20 wasted calls) |
| 5xx / network | service problem | the pool + this model | 15–20 s |
| Other 400 | bad request (your fault) | nothing | **0** — the error is shown, keys untouched |

If every key is only briefly rate-limited, Omni **waits** (up to
`AGENT_MAX_WAIT`, default 90 s) for the first one to free up instead of
failing. Cooldowns are saved in `data/state.json`, so a daily quota hit is
remembered after a restart. Expired entries are pruned automatically.

### 8.5 Auto-detection of providers

When you paste a key, Omni matches it against known patterns:

| Pattern | Detected as |
|---|---|
| `AIza...` | gemini |
| `sk-or-v1-...` | openrouter |
| `sk-ant-...` | anthropic |
| `sk-proj-...` or `sk-...` | openai |
| `gsk_...` | groq |
| `sk-` + 32 hex | deepseek |

If nothing matches, the key goes into a pool called `custom`. You can always
override by naming the pool explicitly: `/add mypool KEY`.

### 8.6 Model discovery

Every key is asked for its `/models` endpoint when added. The results are
cached on the key and shown in the UI's per-key model box. Click **↻ Refresh
all model lists** if a provider adds models. The pool's default model (top of
the card) applies to any key without an override. **A refresh never silently
replaces a model you chose** — the auto-pick only fires when a key has no model
at all.

### 8.7 Per-session vs per-process state

- **strict** is per-process (`cfg.strict`): flipping it in the header affects
  every chat that isn't already mid-answer.
- **auto-approve** is per-session: each chat has its own toggle so a trusted
  chat can auto-approve while another asks.
- **bg-shell** is per-process (like `strict`): every `run_shell` from every
  chat follows the same default. This is deliberate — it's a "mode" of the
  shell tool, not a property of one conversation. Explicit `background=true`
  or `background=false` in a call still overrides it.

---

## 9. Building the standalone .exe

This section is for the person producing the `.exe` — the end user never
touches it.

### 9.1 Prerequisites (build machine only)

- **Python 3.9+** — https://python.org/downloads (tick "Add to PATH" on Windows).
- **A C compiler:**
  - **Windows:** [Visual Studio 2022 Build Tools](https://visualstudio.microsoft.com/downloads/#build-tools-for-visual-studio-2022), tick **"Desktop development with C++"**. Reboot after.
  - **macOS:** `xcode-select --install`
  - **Linux:** `sudo apt install build-essential`
- **Nuitka and its helpers:**
  ```
  python -m pip install -r requirements-build.txt
  ```

Verify the compiler is reachable:
- Windows: open the **"x64 Native Tools Command Prompt for VS 2022"** from the
  Start menu, run `where cl` — you should see a path.
- macOS/Linux: `which gcc` or `which clang`.

### 9.2 Build

```
# From the project root, in the venv
install.bat               (Windows)   or   ./install.sh   (macOS/Linux)
build.bat                 (Windows)   or   python build.py (all platforms)
```

**Two output modes:**

- **Default (recommended)** — `python build.py` → a **folder** at
  `dist/omni.dist/` containing `omni.exe` and all its dependencies. **Fast
  startup** (<1 s). Ship the whole folder as a zip.
- **Single file** — `python build.py --onefile` → a single `dist/omni.exe`.
  Slower startup (3–8 s, it unpacks to a temp dir on every launch). Ship
  just that file.

Both are fully self-contained — the end user does **not** need Python.

**Clean rebuild** if something gets stuck:

```
python build.py --clean
python build.py
```

The first build takes 5–10 minutes (Nuitka compiles every module to C). Later
builds are 2–3 minutes.

### 9.3 What's inside the build

- All of `agent/` (agent code, tools, web UI).
- `agent/web/index.html` (the web UI).
- `config/`, `workflows/` (data files).
- `tkinter`, `tkinter.ttk`, `_tkinter` (for the launcher GUI).
- `docx`, `openpyxl`, `pptx`, `lxml` (for Office files).
- `tzdata` (for the Pacific-midnight quota reset logic on Windows).

**Not** included by default: `selenium` and `webdriver-manager`. That means
`AGENT_BROWSER=selenium` does **not** work in a shipped `.exe` — `web_fetch`
always uses the fast `urllib` path, and `web_search` returns the "needs a
browser" message. If you need JS rendering in the exe, rebuild with:

```
python build.py --with-browser
```

which adds `--include-package=selenium --include-package=webdriver_manager`
and grows the build by ~30 MB. Most users don't need it.

### 9.4 Testing the build

1. Move `dist/omni.dist/` (or `dist/omni.exe`) to a **machine without Python**.
2. Double-click `omni.exe` → launcher window should appear.
3. Click **Web UI** → browser opens.
4. Add a key, chat.
5. Click **Terminal chat** → a new terminal window opens with the REPL.
6. Close both.

If all six steps work on a clean machine, the build is good.

### 9.5 Windows SmartScreen warning

Unsigned `.exe` files trigger SmartScreen. The user sees "Windows protected
your PC" and has to click **More info → Run anyway**. To avoid this, code-sign
the exe (needs an EV code-signing certificate, ~$300/year). For personal or
team use, just explain the warning to your users.

### 9.6 Linux headless build

On a machine without a display, the launcher GUI cannot start. Omni detects
this and falls back to the terminal REPL automatically. Users can force it
with `OMNI_NO_GUI=1` or `omni.exe chat`.

---

## 10. Configuration reference (`.env`)

All settings are optional. Copy `.env.example` to `.env` and edit. Omni reads
it from the project root (source) or the folder next to the exe (frozen).
Values already exported in your shell take precedence (the loader uses
`os.environ.setdefault`).

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_BASE_URL` | `http://localhost:20128/v1` | **Fallback** gateway URL — only used when `data/providers.json` doesn't exist yet. Add real keys through the UI or `/add` instead. |
| `AGENT_API_KEY` | *(empty)* | Fallback API key (same caveat). |
| `AGENT_MODEL` | `auto` | Fallback model (same caveat). |
| `AGENT_WORKSPACE` | *(your home)* | Folder relative paths resolve against. Empty = home folder. **A filesystem root (`/`, `C:\`) is refused at startup** because it disables the outside-workspace approval guard. |
| `AGENT_ALLOW_OUTSIDE` | `1` | `1` = allow read/write anywhere on the PC (still asks for approval). |
| `AGENT_AUTO_APPROVE` | `0` | `1` = skip all prompts. **Dangerous.** See §11. |
| `AGENT_STRICT` | `1` | `1` = verify factual claims in answers (see §12). |
| `AGENT_SHELL_BG` | `0` | `1` = every `run_shell` call detaches in the background by default, same as the header toggle. Requires the patch in §6.2. |
| `AGENT_MAX_TURNS` | `60` | Max tool-call rounds per task. |
| `AGENT_TEMPERATURE` | `0.2` | Model temperature. Lower = less creative, more factual. |
| `AGENT_TIMEOUT` | `180` | Seconds per HTTP call to the LLM. |
| `AGENT_STRATEGY` | `round_robin` | `round_robin` or `failover`. |
| `AGENT_MAX_WAIT` | `90` | Seconds to wait for a rate-limited key before giving up. |
| `AGENT_UI_TOKEN` | *(random)* | Fixed token for the web UI link. Useful for bookmarks. |
| `AGENT_BROWSER` | *(unset)* | `selenium` = use a headless Chrome for `web_fetch` / `web_search` (opt-in; see §9.3). |
| `OMNI_NO_GUI` | *(unset)* | `1` = skip the launcher and go straight to the terminal. |
| `OMNI_NO_REFRESH` | *(unset)* | `1` = don't ping `/models` on key add (useful in tests). |

Keys live in `data/providers.json`, not in `.env`. Add them via the UI or the
CLI. Treat the whole `data/` folder as a secret.

---

## 11. Safety model

Omni can run shell commands and write files **as your user account**. That is
a real capability with real risks. Here's exactly how the safety layers work.

### 11.1 Layers

1. **Path resolution** (`agent/safety.py`) — relative paths are anchored at
   the workspace (default: your home folder). Absolute paths anywhere on the
   PC are allowed, but trigger a confirmation prompt unless auto-approve is on.
2. **Approval prompts** — any shell command, and any write *outside* the
   workspace, asks for Allow / Deny. In the terminal, it's a `y/N` prompt. In
   the web UI, it's a modal. "Don't ask again for this tool" is per-session.
3. **Shell blocklist** — a small set of catastrophic patterns is refused
   outright regardless of approval: `rm -rf /`, `rm -rf /*`, `rm -rf ~`,
   `--no-preserve-root`, `mkfs`, fork bombs, `dd if=...of=/dev/...`, `chmod -R
   000 /`, `format C:`, etc. It is a **speed bump, not a guard** — it will not
   catch `mv ~/Documents /tmp/x` or base64-encoded payloads.
4. **Web UI hardening** — binds to `127.0.0.1` only, requires a per-start
   token, checks the `Host` header (blocks DNS rebinding), never sends API
   keys to the browser (only masked tails like `sk-...abc`), and **refuses to
   use a filesystem root or a parent of `data/` as the workspace**.
5. **Startup guard** — `config.load()` exits with code 2 and a clear message
   when `AGENT_WORKSPACE` points at a filesystem root. The workspace picker
   applies the same rule. The credential files inside `data/` are refused by
   `agent/safety.py` even when `data/` is the workspace.
6. **MCP servers** — never started implicitly. Every server must be added
   explicitly from the UI (or `data/mcp.json`). The registry browser only
   *prefills a form*; nothing connects until you click **Add & connect**.
   Env vars and headers you enter are stored in `data/mcp.json`, so treat
   that file as a secret too.

### 11.2 Auto-approve — read this before enabling

`AGENT_AUTO_APPROVE=1` (or the UI toggle) **disables every confirmation
prompt**. The agent can then:

- Run any shell command, including `rm -rf ~/Documents`.
- Write to any file on your PC.
- Delete files via shell.

**Only enable it if** you are (a) in a sandbox or VM, (b) in a throwaway
workspace you can delete and recreate, or (c) genuinely comfortable with an
LLM acting autonomously as your user.

A middle ground: leave auto-approve off, but run `omni.exe ui` and click
**Allow** on the prompts. It's one click per dangerous action.

### 11.3 Background shells and Stop

`bg-shell` (the toggle) and `run_shell(background=true)` (explicit) **do not
run shell commands as approvals-free**. They still go through the same
approval flow when auto-approve is off — you'll see the Allow/Deny dialog
before the process starts. What changes is that once approved:

- The command returns immediately with a job id.
- The process keeps running after **Stop** and after the chat is closed.
- Its output goes to `data/jobs/<id>.log`.

Use `tail_background` / `stop_background` (or the Shell jobs panel) to manage
them. If you genuinely want no prompts *and* background execution, set both
toggles — that's the "unattended dev server" mode, and it belongs in a
sandbox.

### 11.4 `--host 0.0.0.0`

By default the UI listens only on `127.0.0.1`. Setting `--host 0.0.0.0` makes
it reachable from your LAN. **Anyone with the link can run commands as you.**
Only do this behind a VPN, or on a network you fully trust, and preferably
with `AGENT_UI_TOKEN` set to a long random string.

### 11.5 Where keys are stored

`data/providers.json`, plain text, `chmod 600` on Unix (`data/` itself is
`chmod 700`). Back it up carefully or not at all. The `.gitignore` excludes it.

---

## 12. Reducing hallucination (strict mode)

No prompt can eliminate hallucination — that's a property of the model, not
the harness. But Omni gives you two concrete levers.

### 12.1 Tool-grounding (always on)

The system prompt forbids the model from asserting facts about your machine
without calling a tool first. If it says "your project has 42 Python files",
it should have called `search_files` to find out. If it can't verify something,
it's told to say so explicitly.

This works well for **file and command questions**. It doesn't help with
general knowledge the model invents ("the capital of Australia is Sydney").

### 12.2 Strict mode (on by default)

After any tool-using answer longer than a sentence, Omni makes a **second**
model call: a fact-checker that sees the whole conversation and the draft
answer, and marks each factual claim about your files/system as verified or
unverified.

- Verified = there's a tool call in the conversation whose output supports it.
- Unverified = the model asserted it without checking.

Unverified claims appear as an amber **⚠ Unverified claim(s)** banner under
the answer. The answer still shows; you just get the warning.

Costs: one extra LLM call per tool-using answer (roughly +20% tokens on
average). Turn it off in the header toggle if you're optimizing cost or speed.

### 12.3 Practical tips

- **Use a strong model.** `claude-sonnet-4.5`, `gemini-2.5-pro`, `gpt-4o`
  hallucinate far less than small free models.
- **Keep temperature low.** Omni defaults to 0.2; don't raise it for factual work.
- **Ask narrow questions.** "How many functions are in `utils.py`?" is more
  verifiable than "explain my codebase".
- **Check the ⚠ banner.** If it fires, the answer probably needs a follow-up.
- **Never trust the model on external facts.** If it answers a knowledge
  question (not a file/tool question), verify it independently.

---

## 13. MCP servers

Omni speaks the **Model Context Protocol** so the model can gain tools
without you writing Python. Any server that implements MCP over stdio or
Streamable HTTP works — the official Node/Python servers, a wrapper you
wrote yourself, a cloud endpoint, anything.

### 13.1 How it works

1. You add a server from the **MCP** tab (form or registry autofill), or the
   server is already in `data/mcp.json` when Omni starts.
2. Omni spawns its command (`npx`, `uvx`, `docker`, or a direct path) with
   `env` you gave it, or POSTs to its URL for remote servers.
3. Omni lists the server's tools and registers each as
   `mcp__<server>__<tool>` (so they never collide with built-ins).
4. From then on the model can call those tools like any other. Approval
   prompts still apply — an MCP tool that runs a shell command goes through
   `run_shell`'s confirm.

### 13.2 The file format

`data/mcp.json`:

```json
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
    },
    "remote-thing": {
      "url": "https://mcp.example.com/mcp",
      "headers": {"Authorization": "Bearer eyJ..."}
    }
  }
}
```

- `enabled: false` skips a server without deleting it (the UI's **Stop**
  button is different — it stops the process but keeps the entry).
- `env` values support `${VAR}` expansion from the current process's
  environment, so you can keep secrets in `.env`.
- `cwd` (optional) runs a stdio server from a specific directory.

### 13.3 Adding from the registry

The registry browser lists every published MCP server with its **title**,
**description**, **runtime**, **required env vars** (badged with `*`), and
**categories** (filesystem, database, browser, productivity, …). Clicking a
row autofills the Add form:

- npm/pypi/docker servers → `command`, `args`, and env var names seeded as
  `KEY=` so you know what to fill in.
- Remote-only servers → the URL, and the transport switches to HTTP.

Review the prefill, fill in required env vars, click **Add & connect**. If
the server fails to start, the error is shown on its card — click **▲ Edit**
to fix and retry.

### 13.4 Editing and deleting

Every installed server has an **▲ Edit** button that opens an inline form
for **name, command, args, URL, env vars, headers**. **Save & restart**
applies the changes atomically (stop old → write config → start new). If the
new config is broken, the old process is already stopped — click **Start** to
retry after fixing, or **Delete** to remove it entirely. **Delete** removes
the entry from `data/mcp.json` and stops the process.

### 13.5 Reliability notes

- **UTF-8 in subprocess pipes** is handled with `errors="replace"` in the
  reader thread. Previous versions crashed the reader on a single invalid
  byte (some `youtube` / `ffmpeg` MCP servers emit raw bytes on stdout) and
  then every call hung. If a server writes log lines to stdout that aren't
  JSON, they're appended to its `stderr_tail` instead of being dropped.
- **Per-server startup lock** — restarting the same server twice in a row
  can't race.
- **Timeouts** — `initialize` gets 90 s (npx servers can be slow the first
  time), tool calls get 180 s. If a tool call hangs, the call returns an
  error and the server is left as-is; press **Restart** to reset it.
- **Registry never blocks the UI** — the endpoint returns
  `{servers: [], error: "..."}` on network failure, so the Add form keeps
  working.

### 13.6 Writing your own

Any executable that speaks MCP over stdio works. A minimal Python server:

```python
import json, sys
def send(m): sys.stdout.write(json.dumps(m) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "serverInfo": {"name": "mine", "version": "1"}}})
    elif msg.get("method") == "tools/list":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [{
            "name": "echo",
            "description": "Echo text back.",
            "inputSchema": {"type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"]}}]}})
    elif msg.get("method") == "tools/call":
        text = msg["params"]["arguments"]["text"]
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "content": [{"type": "text", "text": text}]}})
```

Save as `echo_server.py`, add to `data/mcp.json` with
`"command": "python", "args": ["echo_server.py"]`, restart Omni. The `echo`
tool now appears to the model as `mcp__mine__echo`.

---

## 14. Extending Omni — adding your own tools

Adding a built-in tool takes two steps.

### 14.1 Define it

Create or edit a file under `agent/tools/` (see `files.py` for examples):

```python
from .registry import tool

@tool(
    "word_count",
    "Count words in a text file.",
    {"path": {"type": "string"}},
    ["path"],
)
def word_count(cfg, path):
    from ..safety import resolve
    p = resolve(cfg, path)
    return str(len(p.read_text().split()))
```

The decorator registers the tool. The function receives `cfg` plus the JSON
arguments the model provides. Return a string.

### 14.2 Import it

Add your module to `agent/tools/__init__.py`:

```python
from . import files, shell, web, memory, office, your_module  # noqa: F401
```

That's it. The model sees the new tool in the next request automatically.

### 14.3 Good tool design

- **Name it clearly.** `word_count` not `wc2`. The model picks tools by name.
- **Describe it in one sentence.** What it does, what it returns, when to use it.
- **Use typed arguments.** `{"type": "string"}`, `{"type": "integer"}`,
  `{"type": "boolean"}`.
- **Return plain text.** The model reads it; keep it short unless output is the point.
- **Raise on error.** The registry catches exceptions and returns them as
  `ERROR: ...` — the model sees the error and can fix its call.

---

## 15. Running the tests

Three offline test suites ship with Omni. They spin up mock LLM servers and
never touch your real `data/` folder or make real API calls.

```
python tests/test_failover.py     # key rotation, quotas, model fallback, waiting, persistence
python tests/test_agent.py        # tool loop, workspace guard, approvals
python tests/test_web.py          # web server: auth, chat + approvals, key/model endpoints, workspace guard, MCP CRUD
```

Or all at once:

```
python -m unittest discover tests
```

CI runs these on Ubuntu, Windows, and macOS for Python 3.10 and 3.12 — see
`.github/workflows/test.yml`. The workflow installs only the runtime test
deps (`python-docx openpyxl python-pptx tzdata`), skipping Selenium.

The tests are a good way to see what Omni actually does when a key hits a
quota, when a model doesn't exist, when the user denies approval, etc. Read
them if you want to understand edge cases.

---

## 16. Troubleshooting

### "All keys/providers failed"

Read the message carefully — it says **why per service**. Examples:

```
All keys/providers failed:
  - gemini / gemini-2.5-flash: 37x quota
      quota: daily quota is used up - it resets by itself …
```

Fixes:
1. Open **Keys & models** → **Test all**.
2. If a key shows `INVALID KEY`, replace it.
3. If a pool shows `daily quota used`, add fallback models or more keys from
   other Google projects.
4. If a pool shows `model not available`, click **List…** and pick a model
   that exists.
5. If the note has a **Retry** button, click it after fixing the cause.

### "workspace is a filesystem root"

Omni refuses to start when `AGENT_WORKSPACE` (or `/workspace`, or the
workspace picker) points at `/` or `C:\`, because that would silently disable
the outside-workspace approval guard: every path would become "inside". Set
`AGENT_WORKSPACE` to a normal folder (your home, or a project dir) and
restart. `data/` and its parents are allowed; the credential files inside
`data/` are always blocked by the file tools.

### "Model not available / 404"

The chosen model doesn't exist for that service. Open **Keys & models**,
click **List…** next to the pool's model, pick one from the dropdown, click
**Apply**. A ⚠ appears next to the model box when the current model isn't in
the service's list.

### The launcher GUI doesn't appear

- **On Windows:** tkinter ships with Python and with the Nuitka build. If the
  launcher fails, it falls back to the terminal REPL and prints a message.
- **On headless Linux:** there's no display, so the launcher can't run. Omni
  detects this and goes straight to the terminal. Force it explicitly with
  `OMNI_NO_GUI=1` or `omni.exe chat`.

### "Approval required but no terminal available"

You're running `omni run "..."` from a script or service that has no stdin.
Either:
- Add `AGENT_AUTO_APPROVE=1` to `.env` (dangerous), or
- Restrict the task so it doesn't need approval (stay inside the workspace,
  no shell).

### `providers.json` was edited by hand and now Omni says "no keys"

Old Notepad / PowerShell on Windows write a UTF-8 BOM. Omni reads
BOM-prefixed JSON correctly, and rewrites the file without the BOM. If your
version is older, upgrade.

### No browser opens when you run `omni ui`

Copy the printed URL (`http://localhost:8765/?token=...`) manually. If the
token is stale, restart `omni ui` and use the fresh one.

### The web UI says "not authorised"

The cookie is stale (browser cleared, or the server restarted with a new
token). Reopen the exact URL printed in the terminal. If you want a stable
link across restarts, set `AGENT_UI_TOKEN` in `.env`.

### Agent can't find a file

Path resolution: relative paths are anchored at the workspace (default:
your home folder). If you meant a file elsewhere, use an absolute path, run
`/workspace <path>` in the terminal, or click the **workspace chip** in the
UI and change the root.

### Every answer triggers a ⚠ strict warning

The verifier is conservative. Two options:
1. Turn **strict** off in the header for that session.
2. Use a stronger model — small models often "verify" their own mistakes.

### The sidebar is too small to see all my chats

The **Recent** section is its own scroll area — mousewheel over it. If you
have dozens of chats, type in the **Search chats…** box. On narrow windows,
tap ☰ (top-left) to open the sidebar as an overlay.

### bg-shell is on and now every command returns "background job …"

That's the point. Either:
- Turn it off (header toggle or `/bgauto off`), or
- Ask the model for output with **tail_background(job_id="…")** — every
  background job returns its id.

Some short commands are still fine in the background — the model can call
`tail_background` immediately and read the finished log.

### An MCP server shows "not loaded" or stays grey

Open the MCP tab, look at its card. The error line has the real exception
(e.g. `command not found: npx`, `HTTP 401: Unauthorized`). Common causes:

- **`npx` / `uvx` / `docker` not on PATH** — install Node / uv / Docker.
- **Wrong env var name** — the registry badges required vars with `*`; check
  the exact spelling against the server's README.
- **Remote server needs auth** — add `Authorization: Bearer …` in the
  headers box.
- **`npx` needs the package to exist** — check the identifier in the args.

Click **Restart** after fixing. If it still fails, click **Delete**, add it
again from the registry (autofill gets the args right).

### An MCP tool hangs or times out

Timeouts are 90 s for `initialize` and 180 s for tool calls. If a call hangs
past that, Omni returns an error and leaves the server process running. Click
**Restart** on the card to reset it. If it hangs on every call, the server
itself is broken — check its stderr (the card shows the tail).

### `omni.exe` is slow to start (single-file build)

That's normal for `--onefile`: it unpacks ~60 MB to a temp folder on every
launch. Build without `--onefile` for a folder build with fast startup.

### Windows shows "Windows protected your PC"

The exe is unsigned. Click **More info → Run anyway**. See §9.5.

### `web_search` says "needs a browser" in a built exe

That's expected: the default build doesn't bundle Selenium. See §9.3 for how
to rebuild with `--with-browser` if you need JS rendering.

---

## 17. FAQ

**Do I need Python?**
Only if you're building the exe or running from source. End users of the exe
need nothing.

**Does it work offline?**
Only if you use a local model (Ollama / LM Studio). Otherwise the LLM calls
need internet.

**Where are my files sent?**
Only to the LLM provider you configured, and only when the model calls a tool
whose output it needs to see (`read_file` returns file contents, etc.). Nothing
goes to Omni's authors. If you add MCP servers, their processes may make
network calls of their own — read their docs. Your keys, files, and history
stay on your PC.

**Can I use it for work / proprietary code?**
Yes, but read the provider's privacy policy. Free tiers often train on your
data. Use a paid tier or a local model for confidential work.

**Can it run code?**
Yes, via `run_shell`. With approval prompts by default.

**Can it edit Word / Excel / PowerPoint?**
Yes, with formatting preserved where possible. It edits paragraphs / cells /
slide text, not styles wholesale.

**Can it browse the web?**
Yes, via `web_fetch` (HTML stripped to plain text). It's read-only — it
doesn't drive a browser or click buttons. Set `AGENT_BROWSER=selenium` to
render JS pages (source installs only).

**How many keys can I add?**
No hard limit. People run 30–50 Gemini keys. Health-weighted rotation spreads
the load.

**Which model should I use?**
Best quality per dollar: `claude-sonnet-4.5` (Anthropic), `gpt-4o` (OpenAI),
`gemini-2.5-pro` (Google). Best free: `gemini-2.5-flash` or a Groq Llama.

**Can I run two instances?**
Yes — they'll use different ports (`8765`, `8766`, ...). Each has its own
`data/` if you keep them in separate folders.

**Can I run it on a server?**
Yes, but read §11.4 first. Use `--host 0.0.0.0` only behind a VPN.

**Can I make every shell call run in the background?**
Yes — click **bg-shell** in the header (whole session), run `/bgauto on` in
the terminal, or add `AGENT_SHELL_BG=1` to `.env` for a persistent default
after the small patch in §6.2. See §6.2 for why it's off by default.

**How do I uninstall?**
Delete the folder. Nothing is installed system-wide except, optionally,
Nuitka's C toolchain if you built it.

---

## 18. Design notes & limitations (read this)

I want you to have accurate expectations.

### 18.1 What "self-contained" means here

The exe has **no Python dependency**, but it still talks to remote LLM
services. If your internet is down or your provider is down, Omni can't
think. That's true of every LLM tool that isn't running a local model.

### 18.2 Hallucination is not solvable

Strict mode **reduces** it and **surfaces** it. It cannot eliminate it. If
your workflow is high-stakes (legal, medical, financial), verify every
answer independently. Use strict mode, use a strong model, keep temperature
low, and treat every factual claim as a draft until confirmed.

### 18.3 Shell access is real

An LLM with shell on your PC can do anything you can. The blocklist catches
obvious catastrophes; it does not catch subtle ones (`mv ~/Documents /tmp/x`
is not on the blocklist). Approval prompts are your real safety net.
`bg-shell` does **not** bypass those prompts — it changes *where* output goes,
not whether the command is allowed.

### 18.4 The build is unsigned

Nuitka/PyInstaller exes are unsigned by default and trigger SmartScreen.
Signing costs money. For personal/team use, explain the warning to users.

### 18.5 Onefile startup is slow

Packing 60 MB into one file means unpacking 60 MB to a temp folder every
launch. Folder builds start in under a second. Prefer the folder build unless
you specifically need a single file.

### 18.6 Free-tier privacy

Free Gemini / OpenRouter prompts can be used for training. For anything
confidential: paid tier, or a local model.

### 18.7 Antivirus false positives

Unsigned Nuitka exes sometimes trip heuristics. If it happens, either code-
sign, or add an exclusion. This is a known issue with any Python-to-exe
tool.

### 18.8 The core is small on purpose

Roughly 5,000 lines of Python across the entire agent (the earlier "3,500"
figure predates the health-scoring, MCP, streaming, and web-UI work). No
framework, no abstraction layer, no magic. You can read it in an afternoon
and change anything. That's a feature.

### 18.9 What the MCP integration does *not* do

- It doesn't sandbox MCP servers. An MCP server is a process you asked Omni
  to run — it has the same permissions Omni's own shell does.
- It doesn't proxy the server's network. If a server you add makes HTTP calls,
  they go out on your normal connection.
- It doesn't cache tool *results* — every tool call is a fresh round trip to
  the server. Some servers are slow. That's their problem.

---

## Credits and license

Omni Agent is a personal project. Use it, modify it, ship it. No warranty,
no support contract. If it breaks, read the code — it's short.

Built with the Python standard library, Nuitka, and tkinter.

Have fun. Be careful with auto-approve.