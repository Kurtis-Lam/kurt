# Kurt

Kurt is a coding assistant that runs on local models (through [Ollama](https://ollama.com)) or hosted models (Claude, OpenAI, Grok, Gemini, OpenRouter and any OpenAI-compatible endpoint). It has two front ends over the same state:

| Command | What it does |
| --- | --- |
| `kurt` | Opens the **browser interface** on `http://127.0.0.1:1277` |
| `kurt --cli` | Runs the same assistant **in your terminal** |

Workspace commands and file changes require your approval. MCP tools run only when you explicitly choose a connected tool with a slash command; their effects depend on the server you connected.

## Setup

1. Download / Clone this repo and unzip it. 
2. Install kurt by:
   ```sh
   bash install.sh   # macOS
   install.bat       # Windows
   ```
3. For local models, install Ollama. Kurt starts it for you and offers to download the default model on first use. Hosted models only need an API key (`/keys` in the terminal, or Settings in the browser).

## Using the browser interface

```sh
kurt                  # opens http://127.0.0.1:1277
kurt --port 8080      # use another port
```

Press `Ctrl+C` in the terminal to stop it. If the port is taken, Kurt falls back to `KURT_WEB_FALLBACK_PORT` (default `10220`). `kurt --localhost` still works as an alias.

## MCP servers and skills (browser interface)

Open **Settings → MCP servers** to connect tools. Kurt supports:

- **Local stdio MCP**: Kurt launches a trusted local MCP executable such as `comfy-mcp`.
- **Remote Streamable HTTP MCP**: connect to an HTTPS endpoint and supply custom headers, for example an API key. OAuth sign-in is not implemented yet.

After connecting, Kurt discovers the server's tools. Type `/` in the chat composer to search the combined list of MCP tools and skills. Choose a tool, add its input, and press Enter. A tool that needs multiple inputs accepts a JSON object after its slash command. Returned image and video files are displayed in the conversation when the MCP server returns local file paths or embedded images.

### Recommended image and video setup: Comfy MCP

[Comfy MCP](https://docs.comfy.org/agent-tools/mcp) is Comfy's first-party MCP connection for [ComfyUI](https://www.comfy.org/). It can drive image, video, audio and 3D workflows. The local server is open source and runs workflows on your own ComfyUI installation and hardware; model downloads and hardware requirements still apply.

For a local setup:

1. Install ComfyUI and its CLI requirements from the [official Comfy MCP setup guide](https://docs.comfy.org/agent-tools/mcp#local-comfy-mcp-connection). The local MCP server requires Python 3.10+, `comfy-cli`, a ComfyUI workspace, and a running ComfyUI instance for generation.
2. Install the MCP package with `pip install comfy-mcp`.
3. Start ComfyUI with `comfy launch`.
4. In Kurt, open Settings → MCP servers and enter:

   | Field | Value |
   | --- | --- |
   | Display name | `ComfyUI` |
   | Connection type | `Local stdio` |
   | Executable command | `comfy-mcp` |
   | Arguments | `[]` |
   | Environment variables | `{"COMFY_BIN":"/absolute/path/to/comfy"}` only if `comfy` is not already on Kurt's PATH |

   The environment-variable field may be left blank when `comfy` is already available on PATH.

For a hosted option, choose **Remote Streamable HTTP**, use `https://cloud.comfy.org/mcp`, and add the API key as an HTTP header: `{"X-API-Key":"your-key"}`. Comfy Cloud currently gives new accounts five free generation runs; further generation requires an active subscription. Kurt supports API-key headers for this connection, but not the Comfy Cloud OAuth flow.

### Skills

Create, edit, and remove your own skills under **Settings → Skills**. Custom skills are saved as `SKILL.md` files in `~/.kurt/web_skills/<skill-id>/SKILL.md`, and are applied to one request when selected from the `/` menu.

MCP servers are powerful local/remote programs. Only connect servers you trust. Local MCP commands run with the permissions of your operating-system account. API keys and custom headers are stored locally in `~/.kurt/web_settings.json` with owner-only file permissions.

## Using the terminal interface

```sh
cd my-project
kurt --cli                    # new conversation
kurt --cli --resume           # continue the latest conversation
kurt --cli --resume 3         # continue conversation 3 from /chats (or an id prefix)
kurt --cli --mode agent       # start in Agent mode
```

On a new conversation Kurt offers to **link** the current folder. Linking lets Kurt read and edit that folder; every change still needs your approval. Say no and Kurt works only in the conversation's private workspace.

### Modes

- **Normal** is for everyday questions and straightforward file work.
- **Agent** is for multi-step jobs. Kurt may delegate analysis to up to three read-only subagents, and uses higher reasoning effort.

Both modes read and list files your OS account can read. Both pause for approval before every command and every file change. Switch with `/mode normal` or `/mode agent`.

### Approvals

When Kurt wants to run a command or save a file you see the exact command or a diff, then `Approve? [y/n]`. The default is `n`. A declined action is reported back to the model, which will not assume it happened.

### Commands

| Command | Purpose |
| --- | --- |
| `/new`, `/chats`, `/resume [n\|id]`, `/rename`, `/delete` | Manage conversations (shared with the browser) |
| `/mode [normal\|agent]` | Switch mode |
| `/link [path]`, `/unlink` | Let Kurt edit a folder, or go back to the private workspace |
| `/workspace` | Show the working folder, private workspace, environment and sandbox |
| `/env [pip\|reset]` | Show, prepare with pip, or delete the conversation's Python environment |
| `/files`, `/add <path>`, `@path` | Show files, copy a file into the workspace, or attach a file to one message |
| `/undo` | Restore the last file Kurt saved in this session |
| `/models`, `/route`, `/keys` | Models, routes (API key, failover group, local model), provider keys |
| `/system-prompt [file <path>\|reset]` | Show, replace or reset Kurt's system prompt |
| `/speed`, `/think`, `/fast`, `/context` | Local-model speed and context tools |
| `/help`, `/exit` | Help and quit. `Esc` stops a response |

Put standing instructions for a project in a `KURT.md` file in its root folder; the terminal interface includes it automatically.

## How everything fits together

Both interfaces use the same files under `~/.kurt`, so a conversation started in the terminal can be continued in the browser and the other way round.

| What | Where | Shared by |
| --- | --- | --- |
| Models, API keys, hardware tuning | `~/.kurt/config.json`, `hardware.json` | CLI and browser |
| System prompt, API groups, MCP server configuration and defaults | `~/.kurt/web_settings.json` | CLI and browser |
| Conversations | `~/.kurt/chats.json` | CLI and browser |
| Custom browser skills | `~/.kurt/web_skills/` | browser |
| Private workspace (uploads, scratch, temp, home) | `~/.kurt/web_workspaces/<chat id>/` | one conversation |
| Python virtual environment | `~/.kurt/web_envs/<chat id>/` | one conversation |
| Linked project folder (optional) | your folder | one conversation |

### Virtual environments

There are exactly two kinds, and they never mix:

1. **Kurt's own environment** is whichever Python runs `kurt.py` (it needs `rich`). It is never used to run your code.
2. **One private venv per conversation** at `~/.kurt/web_envs/<chat id>`. It is created the first time a command needs it, and `pip` is added only when a command uses it. Commands run inside it and inside the sandbox, so `pip install` in one conversation never affects another conversation or your system Python. Nothing is created inside your project, so no `.venv` clutters a linked folder.

Deleting a conversation with `/delete` removes its workspace and venv. `/env reset` removes only the venv; it is rebuilt on demand.

### Command sandbox

Commands run under the macOS Seatbelt sandbox. They can read what your OS account can read, but can write only to the conversation's private workspace and venv, plus the linked folder if you linked one. Network access is granted only for package and repository commands such as `pip install`, `npm install`, `git clone` and `curl`. `sudo`, `su` and similar are refused. On Linux and Windows there is no unrestricted fallback: commands are refused, while reading, searching and approved file edits still work.

## Configuration

| Variable | Meaning |
| --- | --- |
| `KURT_MODEL` | Model to use instead of the saved one |
| `KURT_WEB_PORT`, `KURT_WEB_FALLBACK_PORT` | Browser interface ports (default `1277`, `10220`) |
| `KURT_CTX`, `KURT_THREADS`, `KURT_BATCH` | Override auto-tuned context size, CPU threads, batch size |
| `KURT_THINK` | Force thinking on or off for local models |
| `KURT_TUNE=off` | Turn hardware auto-tuning off |
| `OLLAMA_HOST` | Ollama server address |
| `HF_TOKEN` | Hugging Face token for gated models |

## Notes

- Do not use the same conversation in the terminal and the browser at the same moment. Both write `chats.json`, and the last writer wins.
- Empty conversations (no message sent) are discarded when you leave the terminal interface.