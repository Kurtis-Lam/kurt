# Kurt

A terminal coding assistant powered by **Qwythos** (running locally through [Ollama](https://ollama.com)).
Type `kurt` in any terminal, in any project folder, chat in plain English, and Kurt can read your
code and propose edits, new files and shell commands. **Nothing changes until you confirm it.**

- 100% local — your code never leaves your computer
- Works on macOS (Intel and Apple Silicon) and Windows
- Detects your hardware and tunes itself for speed (see [Speed](#speed))
- Shows a colored diff before every edit, with `/undo` to revert

---

## Requirements

| Thing | Where to get it |
|---|---|
| **Ollama** | https://ollama.com/download |
| **Python 3.9 or newer** | https://www.python.org/downloads/ (Windows: tick **"Add python.exe to PATH"**) |
| Several GB of free disk space and RAM for the model | — |

---

## Setup (one time)

### 1. Install Ollama and download the model

Install Ollama, then run this once in a terminal (you've probably already done it):

```
ollama run hf.co/empero-ai/Qwythos-9B-Claude-Mythos-5-1M-GGUF:Q4_K_M
```

When you see the `>>>` prompt, type `/bye` to exit. The model is now downloaded.
(If you skip this step, Kurt will offer to download it the first time you run it.)

### 2. Download Kurt from GitHub

1. Open the repository page on GitHub.
2. Click the green **Code** button, then **Download ZIP**.
3. Unzip it. You'll get a folder such as `kurt-main`. Remember where it is.

### 3. Install

**macOS** — open Terminal and run:

```bash
cd ~/Downloads/kurt-main      # use the folder you unzipped
bash install.sh
```

**Windows** — open the unzipped folder and **double-click `install.bat`**
(or run it from PowerShell: `.\install.bat`).

The installer creates a private Python environment in `~/.kurt` (Windows: `%USERPROFILE%\.kurt`)
and adds the `kurt` command to your PATH. It doesn't touch your system Python.

### 4. Open a NEW terminal window

The PATH change only applies to terminals opened after installing. Check it worked:

```
kurt --version
```

---

## Using Kurt

Go to any project folder and start it:

```bash
cd ~/projects/my-app
kurt
```

On start, Kurt will:

1. Start Ollama if it isn't running
2. Load Qwythos into memory (the first load can take a little while)
3. Show a green K mascot and a bottom-pinned `kurt` input box

Now just talk to it. The green K mascot appears at startup. The input box stays pinned to the bottom of the terminal, with `/help`, `/models`, `/btw <note>`, and `@file` hints inside the box. While Kurt works, thinking text and responses appear above it; type `/btw <note>` in the same box to add context without interrupting generation.

Examples:

```
kurt ❯ add input validation to the signup function in @auth.py
kurt ❯ write a unit test for utils/dates.py
kurt ❯ why does main.js throw "undefined is not a function"?
```

### How edits are confirmed

When Kurt wants to change something you'll see a diff and a question:

```
╭──────────────╮
│ Edit auth.py │
╰──────────────╯
- def signup(email):
+ def signup(email: str):
+     if "@" not in email:
+         raise ValueError("invalid email")
Apply this edit? (y=yes, n=no, a=yes to all) [y/n/a] (y):
```

- `y` — apply this change
- `n` — reject it (Kurt is told, and you can explain what you want instead)
- `a` — apply this and all further **file edits** this session
- Shell commands (`RUN`) **always** ask, defaulting to *no*, even in auto mode

Kurt can only read and write inside the folder you launched it from.

### Commands

| Command | What it does |
|---|---|
| `/help` | Show help |
| `/speed` | Show the detected hardware and the speed settings in use (`/speed refresh` re-detects) |
| `/think [on\|off\|auto]` | Turn thinking on or off. Off is much faster on computers without a GPU |
| `/files` | List the project's files |
| `/add <path>` | Load a file into the conversation (or write `@path` inside any message) |
| `/auto` | Toggle auto-accept for file edits |
| `/undo` | Revert the last applied change (can be repeated) |
| `/clear` | Forget the conversation and start fresh |
| `/exit` | Quit (Ctrl+D also works) |

Press **Ctrl+C** while Kurt is typing to stop its answer.

### Tips for good results with a 9B model

- Be specific, and point at files: `fix the bug in @src/api.py line ~40`.
- Ask for **one change at a time**. Small models do much better on focused tasks.
- If an edit fails to match, Kurt automatically re-reads the file and retries. If it keeps
  failing, say "rewrite the whole file" and it'll use a full-file write instead.
- Use `/clear` when switching to a different task so the context doesn't fill up.
- Commit to git before big changes so you have a second safety net next to `/undo`.

---

## Speed

Kurt looks at your computer the first time it runs (result cached in `~/.kurt/hardware.json`)
and picks the fastest safe settings for it. It prints one line at startup, for example
`⚡ Intel Mac · CPU only, 6 cores, 16 GB RAM → ctx 8192 · 6 threads · thinking off`.

| Your computer | What Kurt does |
|---|---|
| **Intel Mac** (no GPU support in Ollama) | CPU inference with one thread per physical core, a shorter context (4096-8192 depending on RAM), thinking off |
| **Apple Silicon Mac** | Metal GPU, context sized to your unified memory, larger prompt batches on 16 GB or more, thinking on, Flash Attention |
| **Windows with an NVIDIA GPU** | Context sized to your VRAM so the model never spills onto the CPU, larger batches on 8 GB or more, thinking on, Flash Attention |
| **Windows without a supported GPU** | Same as an Intel Mac: CPU threads, short context, thinking off |

Other things that make it faster on every computer: the model loads in the background while you
type, replies are drawn in batches instead of once per token, Kurt stops generating as soon as it
has asked to read a file or run a command, old messages are trimmed in one go so Ollama can reuse
its cached prompt, and large files are cut down to fit smaller contexts.

Type `/speed` to see what was detected and applied. If the Ollama app was already running when you
started Kurt, `/speed` also shows two one-time settings (`OLLAMA_FLASH_ATTENTION`,
`OLLAMA_NUM_PARALLEL`) to give it; Kurt sets them itself whenever it starts Ollama.

---

## Configuration (optional)

Set these environment variables before running `kurt`:

| Variable | Default | Meaning |
|---|---|---|
| `KURT_MODEL` | the Qwythos model above | Any other Ollama model name |
| `KURT_CTX` | auto (4096-16384) | Context window in tokens (bigger = more memory use) |
| `KURT_THREADS` | auto | CPU threads used for inference |
| `KURT_BATCH` | auto | Prompt batch size |
| `KURT_THINK` | auto | `on` or `off`: whether the model thinks before answering |
| `KURT_TUNE` | on | `off` disables all hardware auto-tuning |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | Use a remote or custom Ollama server |

Examples:

```bash
# macOS
KURT_CTX=32768 kurt
```

```powershell
# Windows PowerShell
$env:KURT_CTX = "32768"; kurt
```

---

## Troubleshooting

**`kurt: command not found` / `'kurt' is not recognized`**
Open a brand-new terminal window. On macOS, you can also run `source ~/.zshrc`. If it still fails, re-run the installer.

**`Ollama isn't installed or isn't on PATH`**
Install Ollama from https://ollama.com/download, then open a new terminal.

**Windows: "Python not found"**
Install Python from python.org and tick **Add python.exe to PATH**. If Windows opens the Microsoft Store
instead, turn off the "python.exe" app-execution alias in *Settings → Apps → Advanced app settings → App execution aliases*.

**It's slow or the Mac gets hot**
A 9B model is heavy. Close other big apps and run `/speed` to see the settings Kurt picked for your
computer. You can lower `KURT_CTX` further (for example `4096`) or pick a smaller model with `/models`.
Intel Macs have no GPU acceleration for Ollama, so expect slower responses than on Apple Silicon.

**Kurt's edits don't apply ("SEARCH not found")**
The model sometimes misquotes your code. It retries automatically; keep requests small and mention the file with `@file`.

---

## Updating

Download the new ZIP from GitHub, unzip it, and run the installer again (`bash install.sh` or double-click `install.bat`).

## Uninstalling

- macOS: `bash uninstall.sh`
- Windows: double-click `uninstall.bat`

This removes Kurt only. Remove the model with `ollama rm hf.co/empero-ai/Qwythos-9B-Claude-Mythos-5-1M-GGUF:Q4_K_M`.

---

## What's in this repo

| File | Purpose |
|---|---|
| `kurt.py` | The CLI, green K mascot, pinned prompt, and coding-agent logic |
| `pyproject.toml` | Package definition (creates the `kurt` command) |
| `install.sh` / `install.bat` | One-step installers for macOS / Windows |
| `uninstall.sh` / `uninstall.bat` | Removal scripts |
| `LICENSE` | MIT |

## Safety notes

Kurt is a small local model and can be wrong. Read every diff before pressing `y`, and read every
shell command before running it. Don't use `a` (accept all) on code you can't easily restore.