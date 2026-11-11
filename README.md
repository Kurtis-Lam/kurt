# Kurt

Kurt is a coding assistant that runs on local models (through [Ollama](https://ollama.com)) or hosted models (Claude, OpenAI, Grok, Gemini, OpenRouter and any OpenAI-compatible endpoint). It has two front ends over the same state:

| Command | What it does |
| --- | --- |
| `kurt` | Opens the **browser interface** on `http://127.0.0.1:1277` |
| `kurt --cli` | Runs the same assistant **in your terminal** |

Nothing runs and nothing is written without your approval, in either interface.

## Setup

1. Keep `kurt.py`, `kurt_web.py`, `kurt_sandbox.py` and `providers.py` together in one folder.
2. Install the one dependency into the Python you use to run Kurt: `pip install rich`
3. Make `kurt` a command, for example:
   ```sh
   chmod +x /path/to/kurt.py
   ln -s /path/to/kurt.py /usr/local/bin/kurt
   ```
   or add `alias kurt='python3 /path/to/kurt.py'` to your shell profile.
4. For local models, install Ollama. Kurt starts it for you and offers to download the default model on first use. Hosted models only need an API key (`/keys` in the terminal, or Settings in the browser).

## Using the browser interface

```sh
kurt                  # opens http://127.0.0.1:1277
kurt --port 8080      # use another port
```

Press `Ctrl+C` in the terminal to stop it. If the port is taken, Kurt falls back to `KURT_WEB_FALLBACK_PORT` (default `10220`). `kurt --localhost` still works as an alias.

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
| System prompt, API groups, defaults | `~/.kurt/web_settings.json` | CLI and browser |
| Conversations | `~/.kurt/chats.json` | CLI and browser |
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
- The old parallel "helper agents" setting was removed. Agent mode subagents replace it.
- The `/compact` command was removed because conversation history is now shared and persistent. For local models Kurt leaves out the oldest messages when the context window fills up, and tells you when it does.