#!/usr/bin/env python3
"""kurt - a coding assistant powered by local models (Ollama) and remote provider APIs.

`kurt` opens the browser interface on localhost. `kurt --cli` runs the same assistant in the
terminal. Both share conversations, models, API keys, per-chat workspaces and per-chat
Python environments under ~/.kurt. Nothing is changed or run without your approval.
"""
import concurrent.futures as concurrent_futures
import contextlib
import http.client
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from providers import (
    PROVIDER_SPECS, ProviderClient, ProviderError, default_base_url,
    mask_key, provider_label,
)

from rich.cells import cell_len
from rich.console import Console, Group
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Prompt
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

TERMINAL_LOCK = threading.RLock()


class KurtConsole(Console):
    def print(self, *objects, **kwargs):
        with TERMINAL_LOCK:
            return super().print(*objects, **kwargs)


__version__ = "0.6.0"

DEFAULT_MODEL = "hf.co/empero-ai/Qwythos-9B-Claude-Mythos-5-1M-GGUF:Q4_K_M"
CONFIG_DIR = Path.home() / ".kurt"
CONFIG_FILE = CONFIG_DIR / "config.json"
HW_CACHE_FILE = CONFIG_DIR / "hardware.json"
FAST_MODEL = "qwen2.5-coder:3b"  # ~2 GB: several times faster than a 9B model on a CPU


def load_config():
    try:
        if CONFIG_DIR.exists():
            try:
                CONFIG_DIR.chmod(0o700)
                if CONFIG_FILE.exists():
                    CONFIG_FILE.chmod(0o600)
            except OSError:
                pass
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


_cfg = load_config()
SAVED_MODEL = _cfg.get("model") or None
MODEL = os.environ.get("KURT_MODEL") or SAVED_MODEL or DEFAULT_MODEL
_env_agents = os.environ.get("KURT_AGENTS")
if _env_agents is not None:
    AGENTS = _env_agents.strip().lower() in ("1", "true", "on", "yes")
else:
    AGENTS = bool(_cfg.get("agents", False))
WORKERS = [w for w in _cfg.get("workers", []) if isinstance(w, str)]
PROVIDER_KEYS = [
    dict(item) for item in _cfg.get("provider_keys", [])
    if isinstance(item, dict) and item.get("provider") and item.get("value")
]
REMOTE_MODELS = [
    dict(item) for item in _cfg.get("remote_models", [])
    if isinstance(item, dict) and item.get("id") and item.get("provider") and item.get("model")
]
PROVIDER_CLIENT = ProviderClient()
MODEL_SORT = _cfg.get("model_sort") if _cfg.get("model_sort") in ("name", "size", "params") else "size"
MODEL_SORT_DESC = bool(_cfg.get("model_sort_desc", True))
MAX_AGENTS = max(1, int(os.environ.get("KURT_MAX_AGENTS", "4")))


def _env_flag(name):
    v = os.environ.get(name)
    if v is None or not v.strip():
        return None
    v = v.strip().lower()
    if v in ("1", "true", "on", "yes"):
        return True
    if v in ("0", "false", "off", "no"):
        return False
    return None


# Thinking mode: True / False, or None = decide from the detected hardware.
THINK = _env_flag("KURT_THINK")
if THINK is None and isinstance(_cfg.get("think"), bool):
    THINK = _cfg["think"]
TIP_SHOWN = bool(_cfg.get("tip_shown", False))

_host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
if not _host.startswith("http"):
    _host = "http://" + _host
HOST = _host.replace("0.0.0.0", "127.0.0.1").rstrip("/")
CONTEXT_PCT = 0  # how full the context window is, shown in the footer
CTX = int(os.environ.get("KURT_CTX", "16384"))  # context window sent to Ollama (auto-tuned at startup)
NUM_THREAD = None  # CPU threads for inference (auto-tuned)
NUM_BATCH = None  # prompt batch size (auto-tuned)
KEEP_ALIVE = "60m"
STOP_SEQUENCES = ["[tool results]"]  # the model must never invent tool results itself
SERVER_EXTERNAL = False  # True when Ollama was already running before Kurt started
MAX_ROUNDS = 6  # max automatic read/run follow-ups per user message
IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".idea", ".vscode", "target", ".next", ".cache", ".pytest_cache",
}

console = KurtConsole(highlight=False)
TERMINAL_UI = None
AUTO = False  # "accept all edits" for this session
UNDO = []  # stack of (path, old_text_or_None, crlf)




SPINNER = ["·", "✢", "✳", "✶", "✻", "✽", "✻", "✶", "✳", "✢"]


def term_size():
    try:
        s = os.get_terminal_size(sys.__stdout__.fileno())
        return s.columns, s.lines
    except Exception:
        s = shutil.get_terminal_size(fallback=(100, 30))
        return s.columns, s.lines


def fit_cells(text, width):
    """Cut or pad text to exactly `width` terminal cells."""
    total = cell_len(text)
    if total <= width:
        return text + " " * (width - total)
    out, used = "", 0
    for ch in text:
        cw = cell_len(ch)
        if used + cw > width - 1:
            break
        out += ch
        used += cw
    return out + "…" + " " * max(0, width - used - 1)


def fmt_tokens(n):
    return str(n) if n < 1000 else f"{n / 1000:.1f}k"


def fmt_elapsed(seconds):
    s = int(seconds)
    return f"{s}s" if s < 60 else f"{s // 60}m {s % 60:02d}s"


class Meter:
    """Live clock and token counter for one model call."""

    def __init__(self, label="Thinking"):
        self.start = time.time()
        self.end = None
        self.tokens = 0
        self.label = label
        self.gen_secs = 0.0  # time spent actually generating (from Ollama)
        self.wait_secs = 0.0  # model load + prompt reading time

    def stop(self):
        if self.end is None:
            self.end = time.time()

    @property
    def elapsed(self):
        return (self.end or time.time()) - self.start


def _window(text, pos, avail):
    """Pick the slice of `text` that fits in `avail` cells and keeps the cursor visible.

    Returns (visible_text, cursor_index_inside_visible_text)."""
    if cell_len(text) <= avail:
        return text, pos
    start = 0
    while start < pos and cell_len(text[start:pos]) > avail - 1:
        start += 1
    lead = "…" if start > 0 else ""
    room = avail - cell_len(lead)
    out, used = "", 0
    for ch in text[start:]:
        cw = cell_len(ch)
        if used + cw > room:
            break
        out += ch
        used += cw
    return lead + out, len(lead) + (pos - start)


class KeyReader:
    """Turns raw terminal input into events: ('char', 'a'), ('key', 'left'), ('paste', text)."""

    CSI = {
        "A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end",
        "1~": "home", "4~": "end", "7~": "home", "8~": "end", "3~": "delete",
        "1;5C": "word-right", "1;5D": "word-left", "1;3C": "word-right", "1;3D": "word-left",
    }
    CTRL = {
        "\r": "enter", "\n": "enter", "\x7f": "backspace", "\x08": "backspace",
        "\x03": "ctrl-c", "\x04": "ctrl-d", "\x01": "home", "\x05": "end",
        "\x02": "left", "\x06": "right", "\x0b": "kill-end", "\x15": "kill-start",
        "\x17": "kill-word", "\x10": "up", "\x0e": "down", "\x1b": "escape",
    }

    def __init__(self):
        self.reset()

    def reset(self):
        self._pushback = b""
        self._decoder = None
        if os.name != "nt":
            import codecs
            self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def read(self, timeout=None):
        """Next event, or None when `timeout` seconds pass with no input."""
        if os.name == "nt":
            return self._read_windows(timeout)
        return self._read_posix(timeout)

    def _classify(self, ch):
        if ch in self.CTRL:
            return ("key", self.CTRL[ch])
        if ch == "\t":
            return ("char", "    ")
        if ch < " ":
            return ("key", "noop")
        return ("char", ch)

    # -- POSIX
    def _next_byte(self, timeout):
        if self._pushback:
            b, self._pushback = self._pushback[:1], self._pushback[1:]
            return b
        import select
        fd = sys.stdin.fileno()
        if timeout is not None:
            ready, _, _ = select.select([fd], [], [], timeout)
            if not ready:
                return None
        return os.read(fd, 1)

    def _read_posix(self, timeout):
        b = self._next_byte(timeout)
        if b is None:
            return None
        if b == b"":
            return ("eof", "")
        if b == b"\x1b":
            return self._read_escape()
        ch = self._decoder.decode(b)
        if not ch:
            return ("key", "noop")
        return self._classify(ch)

    def _read_escape(self):
        b = self._next_byte(0.03)
        if b is None:
            return ("key", "escape")
        if b in (b"[", b"O"):
            seq = ""
            while True:
                c = self._next_byte(0.05)
                if not c:
                    break
                seq += c.decode("latin-1")
                if "@" <= seq[-1] <= "~":
                    break
            if seq == "200~":
                return ("paste", self._read_paste())
            return ("key", self.CSI.get(seq, "noop"))
        if b in (b"\x7f", b"\x08"):
            return ("key", "kill-word")
        if b == b"b":
            return ("key", "word-left")
        if b == b"f":
            return ("key", "word-right")
        return ("key", "noop")

    def _read_paste(self):
        import select
        fd = sys.stdin.fileno()
        end = b"\x1b[201~"
        data, self._pushback = self._pushback, b""
        while end not in data:
            ready, _, _ = select.select([fd], [], [], 1.0)
            if not ready:
                break
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            data += chunk
        head, _, tail = data.partition(end)
        self._pushback = tail
        return head.decode("utf-8", "replace")

    # -- Windows
    def _read_windows(self, timeout):
        import msvcrt
        if timeout is not None:
            deadline = time.time() + timeout
            while not msvcrt.kbhit():
                if time.time() >= deadline:
                    return None
                time.sleep(0.01)
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            code = msvcrt.getwch()
            names = {"H": "up", "P": "down", "K": "left", "M": "right",
                     "G": "home", "O": "end", "S": "delete"}
            return ("key", names.get(code, "noop"))
        gathered = ch
        while True:  # characters arriving together are a paste
            deadline = time.time() + 0.02
            while not msvcrt.kbhit() and time.time() < deadline:
                time.sleep(0.002)
            if not msvcrt.kbhit():
                break
            nxt = msvcrt.getwch()
            if nxt in ("\x00", "\xe0"):
                msvcrt.getwch()
                continue
            gathered += nxt
        if len(gathered) > 1:
            return ("paste", gathered)
        return self._classify(ch)


class LineEditor:
    """Single-line editor with a cursor, history and compact paste placeholders."""

    TOKEN_RE = re.compile(r"\[Pasted text #(\d+) \+\d+ lines?\]")

    def __init__(self):
        self.history = []
        self.hist_idx = None
        self.draft = None
        self.reset()

    def reset(self):
        self.buf = ""
        self.pos = 0
        self.pastes = {}
        self.paste_count = 0
        self.hist_idx = None

    # -- content
    def display(self):
        return self.buf.replace("\n", " ")

    def value(self):
        return self.TOKEN_RE.sub(
            lambda m: self.pastes.get(int(m.group(1)), m.group(0)), self.buf
        )

    def remember(self):
        if not self.buf.strip():
            return
        snap = (self.buf, dict(self.pastes), self.paste_count)
        if not self.history or self.history[-1] != snap:
            self.history.append(snap)

    def insert(self, text):
        self.buf = self.buf[: self.pos] + text + self.buf[self.pos:]
        self.pos += len(text)

    def paste(self, text):
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ").rstrip("\n")
        if "\n" in text or len(text) > 300:
            self.paste_count += 1
            lines = text.count("\n") + 1
            self.pastes[self.paste_count] = text
            self.insert(
                f"[Pasted text #{self.paste_count} +{lines} line{'s' if lines != 1 else ''}]"
            )
        else:
            self.insert("".join(c for c in text if c >= " " and c != "\x7f"))

    # -- helpers
    def _token_before(self, pos):
        for m in self.TOKEN_RE.finditer(self.buf):
            if m.end() == pos:
                return m
        return None

    def _token_after(self, pos):
        for m in self.TOKEN_RE.finditer(self.buf):
            if m.start() == pos:
                return m
        return None

    def _word_left(self, pos):
        while pos > 0 and self.buf[pos - 1].isspace():
            pos -= 1
        m = self._token_before(pos)
        if m:
            return m.start()
        while pos > 0 and not self.buf[pos - 1].isspace():
            pos -= 1
        return pos

    def _word_right(self, pos):
        n = len(self.buf)
        while pos < n and self.buf[pos].isspace():
            pos += 1
        m = self._token_after(pos)
        if m:
            return m.end()
        while pos < n and not self.buf[pos].isspace():
            pos += 1
        return pos

    def _load(self, snap):
        self.buf, pastes, self.paste_count = snap
        self.pastes = dict(pastes)
        self.pos = len(self.buf)

    # -- events
    def feed(self, ev):
        """Apply one key event. Returns 'submit', 'interrupt', 'eof', 'escape' or None."""
        kind, val = ev
        if kind == "char":
            self.hist_idx = None
            self.insert(val)
        elif kind == "paste":
            self.hist_idx = None
            self.paste(val)
        elif kind == "key":
            return self._key(val)
        return None

    def _key(self, k):
        if k == "enter":
            return "submit"
        if k == "ctrl-c":
            return "interrupt"
        if k == "escape":
            return "escape"
        if k == "ctrl-d":
            if not self.buf:
                return "eof"
            k = "delete"
        buf, pos = self.buf, self.pos
        if k == "backspace" and pos > 0:
            m = self._token_before(pos)
            n = pos - m.start() if m else 1
            self.buf, self.pos = buf[: pos - n] + buf[pos:], pos - n
            self.hist_idx = None
        elif k == "delete" and pos < len(buf):
            m = self._token_after(pos)
            n = m.end() - pos if m else 1
            self.buf = buf[:pos] + buf[pos + n:]
            self.hist_idx = None
        elif k == "left" and pos > 0:
            m = self._token_before(pos)
            self.pos = m.start() if m else pos - 1
        elif k == "right" and pos < len(buf):
            m = self._token_after(pos)
            self.pos = m.end() if m else pos + 1
        elif k == "home":
            self.pos = 0
        elif k == "end":
            self.pos = len(buf)
        elif k == "word-left":
            self.pos = self._word_left(pos)
        elif k == "word-right":
            self.pos = self._word_right(pos)
        elif k == "kill-word":
            start = self._word_left(pos)
            self.buf, self.pos = buf[:start] + buf[pos:], start
            self.hist_idx = None
        elif k == "kill-start":
            self.buf, self.pos = buf[pos:], 0
            self.hist_idx = None
        elif k == "kill-end":
            self.buf = buf[:pos]
            self.hist_idx = None
        elif k == "up" and self.history:
            if self.hist_idx is None:
                self.draft = (self.buf, dict(self.pastes), self.paste_count)
                self.hist_idx = len(self.history) - 1
            elif self.hist_idx > 0:
                self.hist_idx -= 1
            self._load(self.history[self.hist_idx])
        elif k == "down" and self.hist_idx is not None:
            if self.hist_idx < len(self.history) - 1:
                self.hist_idx += 1
                self._load(self.history[self.hist_idx])
            else:
                self.hist_idx = None
                self._load(self.draft or ("", {}, 0))
        return None


def w_safe_width(width):
    return max(12, min(42, width - 8))


class TerminalUI:
    """Fixed bottom area (live status line + rounded input box + hints) under a scrolling output region."""

    GREEN = "\x1b[38;5;78m"
    BRIGHT_GREEN = "\x1b[1;38;5;83m"
    BORDER = "\x1b[38;5;244m"
    BORDER_BUSY = "\x1b[38;5;239m"
    DIM = "\x1b[38;5;245m"
    CYAN = "\x1b[38;5;80m"
    REV = "\x1b[7m"
    NOREV = "\x1b[27m"
    RESET = "\x1b[0m"
    HINTS = "/help · /mode · /models · /link · @ files"
    HINTS_BUSY = "Esc to interrupt"
    PLACEHOLDER = "Message Kurt…"
    PLACEHOLDER_BUSY = "Working… press Esc to interrupt"
    HEIGHT = 5  # context row, input frame, and compact command hints
    PICKER_VISIBLE = 5
    tag = ""

    def __init__(self):
        self.width, self.rows = term_size()
        self.enabled = (
            sys.stdin.isatty()
            and sys.stdout.isatty()
            and self.width >= 56
            and self.rows >= 14
        )
        self.scroll_bottom = max(1, self.rows - self.HEIGHT)
        self.footer_top = self.scroll_bottom + 1
        self.keys = KeyReader()
        self.main_editor = LineEditor()
        self.editor = self.main_editor
        self.busy = False
        self.notice = ""
        self.meter = None
        self.last_display = ""
        self._ticker = None
        self._tick_stop = None
        self._last_frame = None
        self._cursor_col = 5
        self._stale = False
        self._file_cache = []
        self._file_cache_at = 0.0
        self._picker_context = None
        self._picker_query = None
        self._picker_matches = []
        self._picker_selected = 0
        self._picker_dismissed = None
        self._picker_rendered_rows = 0
        self._last_picker_frame = None

    # ---- lifecycle
    def start(self):
        if not self.enabled:
            return False
        with TERMINAL_LOCK:
            # Anchor the footer without printing placeholder blank lines into the conversation.
            sys.stdout.write("\x1b7" + f"\x1b[1;{self.scroll_bottom}r" + "\x1b8")
            sys.stdout.flush()
        return True

    def stop(self):
        if not self.enabled:
            return
        self.end_work()
        with TERMINAL_LOCK:
            sys.stdout.write("\x1b[?7h\x1b[?2004l\x1b[r")
            if self._stale:
                sys.stdout.write(f"\x1b[{self.scroll_bottom};1H\r\n")
            sys.stdout.write("\r\x1b[J\x1b[?25h")
            sys.stdout.flush()
        self.enabled = False

    @contextlib.contextmanager
    def raw(self):
        """Raw keyboard input (output processing stays on) with bracketed paste enabled."""
        fd = old = None
        if os.name != "nt":
            import termios
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            new = termios.tcgetattr(fd)
            new[0] &= ~(termios.BRKINT | termios.ICRNL | termios.INPCK
                        | termios.ISTRIP | termios.IXON)
            new[3] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG)
            new[6][termios.VMIN] = 1
            new[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSADRAIN, new)
            self.keys.reset()
            with TERMINAL_LOCK:
                sys.stdout.write("\x1b[?2004h")
                sys.stdout.flush()
        try:
            yield
        finally:
            if old is not None:
                import termios
                with TERMINAL_LOCK:
                    sys.stdout.write("\x1b[?2004l")
                    sys.stdout.flush()
                termios.tcsetattr(fd, termios.TCSADRAIN, old)

    # ---- layout
    def _sync_size(self):
        cols, rows = term_size()
        if (cols, rows) == (self.width, self.rows):
            return
        old_top = self.footer_top
        self.width, self.rows = cols, rows
        self.scroll_bottom = max(1, rows - self.HEIGHT)
        self.footer_top = self.scroll_bottom + 1
        out = "\x1b7" + f"\x1b[1;{self.scroll_bottom}r"
        for r in range(max(1, min(old_top, self.footer_top)), rows + 1):
            out += f"\x1b[{r};1H\x1b[2K"
        sys.stdout.write(out + "\x1b8")
        self._stale = True
        self._last_frame = None

    def _context_status(self):
        ed = self.main_editor
        text = ed.buf
        files = []
        for match in re.finditer(r"(?<!\S)@([\w./\\-]+)", text):
            candidate = safe_path(match.group(1))
            if candidate and candidate.is_file():
                rel = str(candidate.relative_to(Path.cwd())).replace("\\", "/")
                if rel not in files:
                    files.append(rel)

        picker_ctx = self._picker_context
        if picker_ctx and self._picker_matches:
            selected = self._picker_matches[min(self._picker_selected, len(self._picker_matches) - 1)]
            target = selected[0] + ("/" if selected[1] else "")
            activity = "selecting file"
        elif files:
            target = files[0]
            action = self._activity_for(text)
            selected = f"{len(files)} file{'s' if len(files) != 1 else ''} selected"
            activity = selected if action == "ready" else f"{action}  ·  {selected}"
        else:
            target = self._default_context_file() or Path.cwd().name or "project"
            activity = self._activity_for(text)

        meter = f"  ·  ctx {CONTEXT_PCT}%" if CONTEXT_PCT else ""
        lead = f"{self.tag}  ·  " if self.tag else ""
        return f"{lead}in {short_path(target, max(12, min(42, w_safe_width(self.width))))}  ·  {activity}{meter}"

    @staticmethod
    def _activity_for(text):
        value = text.lower()
        if re.search(r"\b(debug|traceback|exception|error|bug)\b", value):
            return "debugging"
        if re.search(r"\b(test|pytest|benchmark|spec)\b", value):
            return "running tests"
        if re.search(r"\b(fix|edit|modify|change|update|implement|create|write|refactor|remove|delete)\b", value):
            return "editing"
        if re.search(r"\b(find|search|locate|where|grep)\b", value):
            return "searching"
        if re.search(r"\b(explain|read|review|inspect|understand|describe|summari[sz]e)\b", value):
            return "reading"
        if re.search(r"\b(run|execute|install|deploy)\b", value):
            return "running command"
        return "ready"

    @staticmethod
    def _default_context_file():
        root = Path.cwd()
        likely = (
            "main.py", "app.py", "index.py", "manage.py", "src/main.py", "src/app.py",
            "index.html", "src/index.html", "index.js", "index.ts", "src/index.js",
            "src/main.js", "src/main.ts", "src/App.jsx", "src/App.tsx", "main.go",
            "Cargo.toml", "pyproject.toml", "package.json",
        )
        for name in likely:
            if (root / name).is_file():
                return name
        py_files = sorted(x.name for x in root.glob("*.py") if x.is_file())
        if len(py_files) == 1:
            return py_files[0]
        return None

    def _status_row(self, w):
        m = self.meter
        if m is None:
            label = self._context_status()
            return f"{self.DIM} {fit_cells(label, w - 1)}{self.RESET}" + " " * max(0, w - 1 - cell_len(label))
        t = m.elapsed
        frame = SPINNER[int(t * 8) % len(SPINNER)]
        parts = [fmt_elapsed(t)]
        if m.tokens:
            parts.append(f"↓ {fmt_tokens(m.tokens)} tokens")
        parts.append("esc to interrupt")
        head = f" {frame} {m.label}…"
        meta = " (" + " · ".join(parts) + ")"
        if cell_len(head + meta) > w:
            meta = fit_cells(meta, max(0, w - cell_len(head))).rstrip()
        pad = " " * max(0, w - cell_len(head) - cell_len(meta))
        return f"{self.BRIGHT_GREEN}{head}{self.RESET}{self.DIM}{meta}{self.RESET}{pad}"

    def _file_candidates(self, refresh=False):
        now = time.monotonic()
        if not refresh and self._file_cache and now - self._file_cache_at < 2.0:
            return self._file_cache
        root = Path.cwd()
        found = []
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS)
            rel_dir = Path(dirpath).relative_to(root)
            for name in sorted(dirs):
                path = (rel_dir / name).as_posix()
                found.append((path, True))
            for name in sorted(files):
                path = (rel_dir / name).as_posix()
                found.append((path, False))
        self._file_cache = sorted(found, key=lambda item: (item[0].lower(), not item[1]))
        self._file_cache_at = now
        return self._file_cache

    @staticmethod
    def _mention_context(editor):
        prefix = editor.buf[:editor.pos]
        match = re.search(r"(?<!\S)@([^\s]*)$", prefix)
        if not match:
            return None
        return (match.start(), editor.pos, match.group(1))

    def _matches_for_query(self, query):
        query = query.replace("\\", "/")
        if query.startswith("./"):
            query = query[2:]
        candidates = self._file_candidates()
        if "/" in query:
            parent, leaf = query.rsplit("/", 1)
            parent = parent.strip("/")
            results = [
                item for item in candidates
                if item[0].rpartition("/")[0] == parent
                and item[0].rsplit("/", 1)[-1].lower().startswith(leaf.lower())
            ]
        elif not query:
            results = candidates
        else:
            q = query.lower()
            results = [item for item in candidates if q in item[0].lower()]
            results.sort(key=lambda item: (
                0 if item[0].lower().startswith(q) else
                1 if item[0].rsplit("/", 1)[-1].lower().startswith(q) else 2,
                item[0].lower(),
            ))
        return results

    def _refresh_picker_state(self):
        context = self._mention_context(self.editor)
        if context is None:
            self._picker_context = None
            self._picker_query = None
            self._picker_matches = []
            self._picker_selected = 0
            self._picker_dismissed = None
            return
        signature = (self.editor.buf, self.editor.pos)
        if signature == self._picker_dismissed:
            self._picker_context = None
            self._picker_matches = []
            return
        query = context[2]
        if query != self._picker_query:
            self._picker_selected = 0
        self._picker_context = context
        self._picker_query = query
        self._picker_matches = self._matches_for_query(query)
        if self._picker_matches:
            self._picker_selected %= len(self._picker_matches)
        else:
            self._picker_selected = 0

    def _picker_lines(self, w):
        self._refresh_picker_state()
        if not self._picker_context or not self._picker_matches:
            return []
        matches = self._picker_matches
        selected = self._picker_selected
        visible = min(self.PICKER_VISIBLE, len(matches))
        start = max(0, min(selected - visible // 2, len(matches) - visible))
        header = f" Files  ·  {selected + 1}/{len(matches)}  ·  ↑↓ move  Enter select  Esc close"
        lines = [f"{self.DIM}{fit_cells(header, w)}{self.RESET}"]
        for i in range(start, start + visible):
            path, is_dir = matches[i]
            label = path + ("/" if is_dir else "")
            prefix = " › " if i == selected else "   "
            fitted = fit_cells(prefix + label, w)
            if i == selected:
                lines.append(f"{self.REV}{self.CYAN}{fitted}{self.RESET}")
            else:
                lines.append(f"{self.DIM}{fitted}{self.RESET}")
        return lines

    def _select_picker_item(self):
        if not self._picker_context or not self._picker_matches:
            return False
        start, end, _ = self._picker_context
        path, is_dir = self._picker_matches[self._picker_selected]
        insertion = "@" + path + ("/" if is_dir else "")
        ed = self.editor
        ed.buf = ed.buf[:start] + insertion + ed.buf[end:]
        ed.pos = start + len(insertion)
        ed.hist_idx = None
        self._picker_dismissed = None if is_dir else (ed.buf, ed.pos)
        self._picker_context = None
        self._picker_matches = []
        self._picker_query = None
        self._picker_selected = 0
        return is_dir

    def _hint_row(self, w):
        hint = " " + (self.HINTS_BUSY if self.busy else self.HINTS)
        if self.notice and cell_len(hint) + 3 + cell_len(self.notice) <= w:
            used = cell_len(hint) + 3 + cell_len(self.notice)
            return (f"{self.DIM}{hint}   {self.RESET}{self.GREEN}{self.notice}{self.RESET}"
                    + " " * (w - used))
        return f"{self.DIM}{fit_cells(hint, w)}{self.RESET}"

    def _input_row(self, inner, border):
        R = self.RESET
        avail = inner - 4  # " > " prefix plus one spare cell for the cursor
        ed = self.editor
        if not ed.buf:
            ph = self.PLACEHOLDER_BUSY if self.busy else self.PLACEHOLDER
            if self.busy:
                body = f"{self.REV} {self.NOREV}{self.DIM}{fit_cells(ph, avail - 1)}{R}"
            else:
                body = f"{self.DIM}{fit_cells(ph, avail)}{R}"
            used = avail
            self._cursor_col = 5
        else:
            visible, ci = _window(ed.display(), ed.pos, avail)
            before, after = visible[:ci], visible[ci:]

            def style(s):
                return ed.TOKEN_RE.sub(lambda m: f"{self.CYAN}{m.group(0)}{R}", s)

            self._cursor_col = 5 + cell_len(before)
            if self.busy:
                cur, rest = (after[:1] or " "), after[1:]
                body = f"{style(before)}{self.REV}{cur}{self.NOREV}{style(rest)}"
                used = cell_len(before) + cell_len(cur) + cell_len(rest)
            else:
                body = style(before + after)
                used = cell_len(visible)
        pad = " " * max(0, inner - 3 - used)
        return f"{border}│{R} {self.BRIGHT_GREEN}>{R} {body}{R}{pad}{border}│{R}"

    def _lines(self):
        w = max(20, self.width - 1)  # keep the last column free
        inner = w - 2
        border = self.BORDER_BUSY if self.busy else self.BORDER
        return [
            self._status_row(w),
            f"{border}╭{'─' * inner}╮{self.RESET}",
            self._input_row(inner, border),
            f"{border}╰{'─' * inner}╯{self.RESET}",
            self._hint_row(w),
        ]

    def draw_footer(self, preserve_cursor=True, active=False):
        if not self.enabled:
            return
        with TERMINAL_LOCK:
            self._sync_size()
            self._refresh_picker_state()
            lines = self._lines()
            popup_lines = self._picker_lines(max(20, self.width - 1)) if not self.busy else []
            footer_frame = "\x1b[?7l" + "".join(
                f"\x1b[{self.footer_top + i};1H{line}\x1b[K" for i, line in enumerate(lines)
            )
            popup_top = max(1, self.footer_top - len(popup_lines))
            popup_frame = ""
            if self._picker_rendered_rows > len(popup_lines):
                old_top = max(1, self.footer_top - self._picker_rendered_rows)
                for row in range(old_top, min(popup_top, old_top + self._picker_rendered_rows)):
                    popup_frame += f"\x1b[{row};1H\x1b[K"
            popup_frame += "".join(
                f"\x1b[{popup_top + i};1H{line}\x1b[K" for i, line in enumerate(popup_lines)
            )
            self._picker_rendered_rows = len(popup_lines)
            self._last_picker_frame = popup_frame
            frame = footer_frame + popup_frame + "\x1b[?7h"
            if preserve_cursor:
                if frame == self._last_frame:
                    return
                self._last_frame = frame
                out = "\x1b7" + frame + "\x1b8"
            else:
                self._last_frame = None
                col = max(1, min(self._cursor_col, self.width - 1))
                out = frame + f"\x1b[{self.footer_top + 2};{col}H" + ("\x1b[?25h" if active else "")
            sys.stdout.write(out)
            sys.stdout.flush()

    def redraw(self):
        self.draw_footer(preserve_cursor=True)

    # ---- state
    def set_busy(self, busy):
        if not self.enabled:
            return
        self.busy = busy
        self.editor = LineEditor() if busy else self.main_editor
        with TERMINAL_LOCK:
            sys.stdout.write("\x1b[?25l" if busy else "\x1b[?25h")
            sys.stdout.flush()
        self.draw_footer(preserve_cursor=True)

    def set_notice(self, notice):
        if not self.enabled:
            return
        self.notice = notice
        self.draw_footer(preserve_cursor=True)

    def begin_work(self, meter):
        if not self.enabled:
            return
        self.meter = meter
        self._tick_stop = threading.Event()
        self._ticker = threading.Thread(target=self._tick, args=(self._tick_stop,), daemon=True)
        self.draw_footer(preserve_cursor=True)
        self._ticker.start()

    def _tick(self, stop):
        while not stop.wait(0.12):
            try:
                self.draw_footer(preserve_cursor=True)
            except Exception:
                pass

    def end_work(self):
        if self._tick_stop is not None:
            self._tick_stop.set()
            if self._ticker is not None:
                self._ticker.join(timeout=1)
        self._tick_stop = self._ticker = None
        if self.meter is not None:
            self.meter.stop()
            self.meter = None
            self.draw_footer(preserve_cursor=True)

    # ---- input
    def _restore_content_cursor(self):
        with TERMINAL_LOCK:
            if self._stale:
                sys.stdout.write(f"\x1b[{self.scroll_bottom};1H\r\n")
                self._stale = False
            else:
                sys.stdout.write("\x1b8")
            sys.stdout.flush()

    def read_line(self):
        ed = self.main_editor
        ed.reset()
        self.editor = ed
        self.notice = ""
        self.last_display = ""
        with TERMINAL_LOCK:
            sys.stdout.write("\x1b7")  # remember where output continues
            sys.stdout.flush()
        self._stale = False
        try:
            with self.raw():
                self.draw_footer(preserve_cursor=False, active=True)
                while True:
                    ev = self.keys.read(timeout=0.4)
                    if ev is None:
                        if self._size_changed():
                            self.draw_footer(preserve_cursor=False, active=True)
                        continue
                    if ev[0] == "eof":
                        raise EOFError
                    if ev[0] == "key" and self._picker_context and self._picker_matches:
                        if ev[1] == "up":
                            self._picker_selected = (self._picker_selected - 1) % len(self._picker_matches)
                            self.draw_footer(preserve_cursor=False, active=True)
                            continue
                        if ev[1] == "down":
                            self._picker_selected = (self._picker_selected + 1) % len(self._picker_matches)
                            self.draw_footer(preserve_cursor=False, active=True)
                            continue
                        if ev[1] == "enter":
                            is_dir = self._select_picker_item()
                            self.draw_footer(preserve_cursor=False, active=True)
                            if is_dir:
                                continue
                            continue
                        if ev[1] == "escape":
                            self._picker_dismissed = (ed.buf, ed.pos)
                            self._picker_context = None
                            self._picker_matches = []
                            self._picker_selected = 0
                            self.draw_footer(preserve_cursor=False, active=True)
                            continue
                    act = ed.feed(ev)
                    if act == "submit":
                        line, self.last_display = ed.value(), ed.display()
                        ed.remember()
                        ed.reset()
                        self.draw_footer(preserve_cursor=False)
                        return line
                    if act == "interrupt":
                        if ed.buf:
                            ed.reset()
                            self.draw_footer(preserve_cursor=False, active=True)
                            continue
                        raise KeyboardInterrupt
                    if act == "eof":
                        raise EOFError
                    self.draw_footer(preserve_cursor=False, active=True)
        finally:
            self._restore_content_cursor()

    def _size_changed(self):
        return term_size() != (self.width, self.rows)


def short_path(path, limit):
    p = str(path)
    home = str(Path.home())
    if p == home or p.startswith(home + os.sep):
        p = "~" + p[len(home):]
    if len(p) <= limit:
        return p
    tail = p[-(limit - 1):]
    cut = tail.find(os.sep)
    if 0 <= cut < len(tail) - 1:
        tail = tail[cut:]
    return "…" + tail


# A block-letter "K" mascot in the style of Claude Code's, drawn in green.
MASCOT = [
    "███  ▗▄█▛▀",
    "▌█▐▄█▛▀   ",
    "███▀█▙▄   ",
    "███  ▝▀█▙▄",
]
MASCOT_STYLE = "rgb(61,214,110)"


def print_brand(extra=None):
    mascot = Text("\n".join(MASCOT), style=MASCOT_STYLE, no_wrap=True)
    path_limit = max(16, console.width - len(MASCOT[0]) - 14)
    details = Group(
        Text("Kurt", style="bold " + MASCOT_STYLE),
        Text("Your local coding companion", style="bold"),
        Text(short_model(MODEL), style="green"),
        Text(short_path(Path.cwd(), path_limit), style="dim", no_wrap=True),
        *[Text(line, style="dim") for line in (extra or [])],
    )
    grid = Table.grid(padding=(0, 3))
    grid.add_column(no_wrap=True)
    grid.add_column()
    grid.add_row(mascot, details)
    console.print(
        Panel(
            grid,
            title=f"Kurt v{__version__}",
            title_align="left",
            border_style=MASCOT_STYLE,
            padding=(1, 2),
            expand=False,
        )
    )


def print_summary(meter):
    parts = [f"Done in {fmt_elapsed(meter.elapsed)}"]
    if meter.tokens:
        parts.append(f"↓ {meter.tokens:,} tokens")
        if meter.gen_secs >= 0.5:
            parts.append(f"{meter.tokens / meter.gen_secs:.1f} tok/s")
            if meter.wait_secs >= 2:
                parts.append(f"{fmt_elapsed(meter.wait_secs)} loading/reading prompt")
        elif meter.elapsed >= 1:
            parts.append(f"{meter.tokens / meter.elapsed:.1f} tok/s")
    console.print(Text("✻ " + " · ".join(parts), style="dim green"))


SYSTEM = """You are Kurt, an expert coding assistant running inside the user's terminal. You were made by Kurtis, a Hong Kong grade 11 student. 
Project directory: {cwd}

You can act on the project by emitting ACTION BLOCKS. Reading files, creating new files and
view-only commands (ls, cat, grep, git status ...) run automatically. The user is asked to confirm
only edits to existing files and commands that could change or delete things. Results are sent
back to you. Formats (exactly):

Read a file:
@@@ READ relative/path.py
@@@ END

Read only part of a long file (1-based, inclusive line numbers):
@@@ READ relative/path.py:120-220
@@@ END

Edit an existing file (SEARCH must match the file EXACTLY, including whitespace;
keep it small but unique; several SEARCH/REPLACE pairs are allowed):
@@@ EDIT relative/path.py
<<<<<<< SEARCH
old code
=======
new code
>>>>>>> REPLACE
@@@ END

Create a new file or fully overwrite one:
@@@ WRITE relative/path.py
full file contents
@@@ END

Run a shell command (use it to look around, e.g. ls or grep; risky commands need the user's OK):
@@@ RUN python -m pytest -q
@@@ END

Rules:
- READ a file before editing it. Never guess file contents.
- If a READ result says it was truncated, read the missing part with a line range before touching it.
- Use paths relative to the project directory.
- Make minimal, focused edits. Prefer EDIT over WRITE for existing files.
- Briefly explain what you are doing, then emit the blocks. Do not wrap blocks in code fences.
- Do not claim a change is applied until the tool result says so.
- The user may inspect and revise Kurt-controlled system instructions for this local instance. If asked, explain or quote the Kurt prompt; do not claim that Kurt’s own app prompt is confidential. Do not claim access to provider-owned hidden instructions or disclose credentials.
- If a tool result reports an error or a WARNING (for example a syntax error), fix it and try again.
- If the user rejected a change, do not propose the same change again; ask what they prefer.
- A "[Helper agent reports ...]" section holds findings from helper agents that ran in
  parallel. Use them, double-check against the files, and make any changes yourself.
"""

BLOCK_RE = re.compile(r"@@@ (EDIT|WRITE|READ|RUN) ?([^\n]*)\n(.*?)\n?@@@ END", re.S)
PAIR_RE = re.compile(
    r"<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)\n?>>>>>>> REPLACE", re.S
)


# ----------------------------------------------------------------- Config ---
def save_config():
    """Persist user-wide settings and provider credentials outside project folders."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            CONFIG_DIR.chmod(0o700)
        except OSError:
            pass
        data = {
            "model": SAVED_MODEL, "agents": AGENTS, "workers": WORKERS,
            "think": THINK, "tip_shown": TIP_SHOWN,
            "model_sort": MODEL_SORT, "model_sort_desc": MODEL_SORT_DESC,
            "provider_keys": PROVIDER_KEYS, "remote_models": REMOTE_MODELS,
        }
        temporary = CONFIG_FILE.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, CONFIG_FILE)
        try:
            CONFIG_FILE.chmod(0o600)
        except OSError:
            pass
    except Exception as exc:
        console.print(f"[red]Could not save Kurt settings:[/] {escape(str(exc))}")


def get_remote_model(reference):
    if not isinstance(reference, str) or not reference.startswith("remote:"):
        return None
    model_id = reference.split(":", 1)[1]
    return next((item for item in REMOTE_MODELS if str(item.get("id")) == model_id), None)


def remote_model_ref(item):
    return "remote:" + str(item["id"])


def short_model(name, limit=44):
    remote = get_remote_model(name)
    if remote:
        name = f"{provider_label(remote.get('provider'))} / {remote.get('label') or remote.get('model')}"
    else:
        name = name[6:] if name.startswith("hf.co/") else name
    return name if len(name) <= limit else name[: limit - 1] + "…"


def same_model(a, b):
    def norm(n):
        return n if ":" in n.split("/")[-1] else n + ":latest"

    return norm(a) == norm(b)


def fmt_size(n):
    return f"{n / 1e9:.1f} GB" if n else ""


# ------------------------------------------------------- Hardware tuning ---
HW = None  # detected hardware profile (dict), cached on disk
TUNE = {}  # what the tuner decided (for /speed and the startup line)
CTX_TIERS = (4096, 6144, 8192, 12288, 16384)

_PS_PROBE = (
    "$c=@(Get-CimInstance Win32_Processor);"
    "$o=@{name=$c[0].Name;"
    "cores=($c|Measure-Object NumberOfCores -Sum).Sum;"
    "threads=($c|Measure-Object NumberOfLogicalProcessors -Sum).Sum;"
    "gpu=@(Get-CimInstance Win32_VideoController|ForEach-Object{$_.Name})};"
    "$o|ConvertTo-Json -Compress"
)


def _run(cmd, timeout=4):
    """Run a small helper command quietly; return stdout or '' on any failure."""
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, errors="replace", **kwargs
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _int(s, default=0):
    try:
        return int(str(s).strip().split()[0])
    except Exception:
        return default


def _sysctl(name):
    return _run(["sysctl", "-n", name], timeout=2)


def _windows_ram_gb():
    try:
        import ctypes

        class MemStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = MemStatus()
        st.dwLength = ctypes.sizeof(MemStatus)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return st.ullTotalPhys / 2**30
    except Exception:
        return 0.0


def _nvidia_gpus():
    """[(name, vram_gb)] from nvidia-smi (Windows and Linux)."""
    exe = shutil.which("nvidia-smi")
    if not exe and os.name == "nt":
        for p in (
            r"C:\Windows\System32\nvidia-smi.exe",
            r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
        ):
            if os.path.isfile(p):
                exe = p
                break
    if not exe:
        return []
    out = _run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"], timeout=8)
    gpus = []
    for line in out.splitlines():
        name, _, mem = line.rpartition(",")
        mb = _int(mem)
        if name.strip() and mb > 0:
            gpus.append((name.strip(), mb / 1024))
    return gpus


def detect_hardware():
    """Look at this machine once: OS, CPU, RAM and which accelerator Ollama can really use."""
    hw = {
        "os": "linux", "arch": platform.machine().lower(), "cpu": "",
        "logical": os.cpu_count() or 1, "physical": 0, "ram_gb": 0.0,
        "accel": "cpu", "gpu": "", "vram_gb": 0.0, "rosetta": False, "apple_silicon": False,
    }
    gpu_names = []
    if sys.platform == "darwin":
        hw["os"] = "mac"
        # hw.optional.arm64 is 1 on Apple Silicon even when Python itself runs under Rosetta
        hw["apple_silicon"] = _sysctl("hw.optional.arm64") == "1"
        hw["rosetta"] = _sysctl("sysctl.proc_translated") == "1"
        hw["cpu"] = _sysctl("machdep.cpu.brand_string")
        hw["physical"] = _int(_sysctl("hw.physicalcpu"))
        hw["logical"] = _int(_sysctl("hw.logicalcpu"), hw["logical"])
        hw["ram_gb"] = _int(_sysctl("hw.memsize")) / 2**30
        if hw["apple_silicon"]:
            hw["accel"] = "metal"  # Metal GPU, memory shared with the CPU
            hw["gpu"] = hw["cpu"] or "Apple GPU"
            hw["vram_gb"] = hw["ram_gb"]
        # Intel Macs: Ollama has no GPU support there, so inference runs on the CPU
    elif os.name == "nt":
        hw["os"] = "windows"
        hw["ram_gb"] = _windows_ram_gb()
        out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_PROBE], timeout=20)
        try:
            d = json.loads(out)
            hw["cpu"] = str(d.get("name") or "").strip()
            hw["physical"] = _int(d.get("cores"))
            hw["logical"] = _int(d.get("threads"), hw["logical"])
            g = d.get("gpu") or []
            gpu_names = [g] if isinstance(g, str) else [str(x) for x in g]
        except Exception:
            pass
    else:
        try:
            info = Path("/proc/cpuinfo").read_text(errors="replace")
            m = re.search(r"model name\s*:\s*(.+)", info)
            hw["cpu"] = m.group(1).strip() if m else ""
            cores = set(re.findall(r"physical id\s*:\s*(\d+)\s*\n(?:.*\n)*?core id\s*:\s*(\d+)", info))
            hw["physical"] = len(cores)
        except Exception:
            pass
        try:
            m = re.search(r"MemTotal:\s*(\d+)\s*kB", Path("/proc/meminfo").read_text())
            hw["ram_gb"] = int(m.group(1)) / 2**20 if m else 0.0
        except Exception:
            pass

    if sys.platform != "darwin":
        gpus = _nvidia_gpus()
        if gpus:
            hw["accel"] = "cuda"
            hw["gpu"] = ", ".join(n for n, _ in gpus)
            hw["vram_gb"] = sum(v for _, v in gpus)
        else:
            amd = [n for n in gpu_names if re.search(r"radeon\s+(rx|pro|vii)|instinct", n, re.I)]
            if amd or shutil.which("rocm-smi"):
                hw["accel"] = "amd"
                hw["gpu"] = ", ".join(amd) or "AMD GPU"
    if not hw["physical"]:
        hw["physical"] = max(1, hw["logical"] // 2) if hw["logical"] > 2 else hw["logical"]
    return hw


def get_hardware(refresh=False):
    """Hardware profile, cached in ~/.kurt/hardware.json (re-detected weekly or on /speed refresh)."""
    global HW
    if HW and not refresh:
        return HW
    fp = f"{sys.platform}|{platform.machine()}|{os.cpu_count()}|{platform.node()}"
    if not refresh:
        try:
            c = json.loads(HW_CACHE_FILE.read_text(encoding="utf-8"))
            if c.get("v") == 1 and c.get("fp") == fp and time.time() - c.get("ts", 0) < 7 * 86400:
                HW = c["hw"]
                return HW
        except Exception:
            pass
    HW = detect_hardware()
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        HW_CACHE_FILE.write_text(
            json.dumps({"v": 1, "fp": fp, "ts": time.time(), "hw": HW}), encoding="utf-8"
        )
    except Exception:
        pass
    return HW


def pick_ctx(hw, model_gb):
    """Largest context that fits fast memory (VRAM / unified memory / RAM) for this model.

    A context that spills out of VRAM, or that is too long to process on a CPU, is the most
    common reason local models feel slow."""
    accel, ram = hw["accel"], hw["ram_gb"] or 8.0
    if accel == "metal":
        budget = ram * (0.66 if ram <= 36 else 0.75)
    elif accel == "cuda":
        budget = hw["vram_gb"]
    elif accel == "amd":
        budget = 8.0
    else:
        budget = ram * 0.6
    cap = 16384
    if accel == "cpu":
        cap = 12288 if hw["physical"] >= 12 else 8192  # prompt processing is slow on CPUs
    elif accel == "amd":
        cap = 8192
    best = CTX_TIERS[0]
    for t in CTX_TIERS:
        if t <= cap and model_gb + 1.0 + 0.12 * t / 1000 <= budget:  # ~0.12 GB of KV cache per 1k tokens
            best = t
    return best


def think_enabled():
    """Thinking makes answers slower; on by default only where a GPU can keep up."""
    if THINK is not None:
        return THINK
    return get_hardware()["accel"] != "cpu"


def device_label(hw):
    if hw["os"] == "mac":
        return "Apple Silicon Mac" if hw["apple_silicon"] else "Intel Mac"
    return {"windows": "Windows", "linux": "Linux"}.get(hw["os"], hw["os"])


def retune():
    """Choose context size, CPU threads and batch size for this device and the current model."""
    global CTX, NUM_THREAD, NUM_BATCH, MAX_AGENTS, TUNE
    hw = get_hardware()
    tuning_off = _env_flag("KURT_TUNE") is False
    model_gb = 5.5  # default 9B Q4_K_M
    for m in installed_models():
        if same_model(m["name"], MODEL) and m["size"]:
            model_gb = m["size"] / 1e9
    accel = hw["accel"]

    if os.environ.get("KURT_CTX"):
        CTX = int(os.environ["KURT_CTX"])
    elif tuning_off:
        CTX = 16384
    else:
        CTX = pick_ctx(hw, model_gb)

    NUM_THREAD = None
    if os.environ.get("KURT_THREADS"):
        NUM_THREAD = max(1, int(os.environ["KURT_THREADS"]))
    elif not tuning_off and accel == "cpu":
        NUM_THREAD = max(1, hw["physical"])  # one thread per physical core beats hyper-threads

    NUM_BATCH = None
    if os.environ.get("KURT_BATCH"):
        NUM_BATCH = max(1, int(os.environ["KURT_BATCH"]))
    elif not tuning_off and (
        (accel == "cuda" and hw["vram_gb"] >= 8) or (accel == "metal" and hw["ram_gb"] >= 16)
    ):
        NUM_BATCH = 1024  # faster prompt processing when there is memory to spare

    if "KURT_MAX_AGENTS" not in os.environ:
        MAX_AGENTS = 2 if accel == "cpu" else 4  # parallel helpers do not speed up a CPU

    where = {
        "metal": f"Metal GPU, {hw['ram_gb']:.0f} GB unified memory",
        "cuda": f"{hw['gpu']}, {hw['vram_gb']:.0f} GB VRAM",
        "amd": hw["gpu"] or "AMD GPU",
    }.get(accel, f"CPU only, {hw['physical']} cores, {hw['ram_gb']:.0f} GB RAM")
    bits = [f"ctx {CTX}"]
    if NUM_THREAD:
        bits.append(f"{NUM_THREAD} threads")
    if NUM_BATCH:
        bits.append(f"batch {NUM_BATCH}")
    bits.append("thinking " + ("on" if think_enabled() else "off"))
    TUNE = {"where": where, "bits": bits, "model_gb": model_gb, "off": tuning_off}
    return TUNE


def tune_summary():
    if not TUNE:
        return ""
    off = " (tuning off)" if TUNE["off"] else ""
    return f"⚡ {device_label(get_hardware())} · {TUNE['where']} → {' · '.join(TUNE['bits'])}{off}"


def runner_options():
    """Options that change how the model is loaded. They must be identical in every request,
    otherwise Ollama reloads the model (a multi-second stall)."""
    o = {"num_ctx": CTX}
    if NUM_THREAD:
        o["num_thread"] = NUM_THREAD
    if NUM_BATCH:
        o["num_batch"] = NUM_BATCH
    return o


def server_env():
    """Ollama server settings Kurt applies when it starts Ollama itself."""
    hw = get_hardware()
    env = {"OLLAMA_NUM_PARALLEL": "2" if (AGENTS and hw["accel"] in ("cuda", "metal")) else "1"}
    if hw["accel"] in ("cuda", "metal"):
        env["OLLAMA_FLASH_ATTENTION"] = "1"
    return env


def server_env_help():
    hw = get_hardware()
    env = server_env()
    if hw["os"] == "mac":
        lines = [f"launchctl setenv {k} {v}" for k, v in env.items()]
        after = "Then quit the Ollama app from the menu bar and open it again."
    elif hw["os"] == "windows":
        lines = [f"setx {k} {v}" for k, v in env.items()]
        after = "Then quit Ollama from the system tray and open it again."
    else:
        lines = [f"Environment={k}={v}" for k, v in env.items()]
        after = "Put these in `sudo systemctl edit ollama` under [Service], then restart ollama."
    return lines, after


def speed_report(refresh=False):
    hw = get_hardware(refresh=refresh)
    retune()
    table = Table(box=None, show_header=False, padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Device", device_label(hw))
    table.add_row("CPU", f"{hw['cpu'] or hw['arch']} ({hw['physical']} cores / {hw['logical']} threads)")
    table.add_row("Memory", f"{hw['ram_gb']:.1f} GB")
    table.add_row("Accelerator", TUNE["where"])
    table.add_row("Model", f"{short_model(MODEL, 50)} ({TUNE['model_gb']:.1f} GB)")
    table.add_row("Applied", ", ".join(TUNE["bits"]))
    console.print(Panel(table, title="Speed", border_style="green", expand=False))
    notes = []
    if hw["accel"] == "cpu":
        notes.append("No usable GPU: thinking is off and the context is kept short because CPUs "
                     "are slow at reading long prompts. /think on turns thinking back on.")
        if hw["os"] == "mac":
            notes.append("Intel Macs cannot use the GPU in Ollama.")
        notes.append(f"Each token reads the whole model from RAM, so speed is set by model size. "
                     f"Type /fast to switch to {FAST_MODEL} (roughly 3x faster than a 9B model).")
    if hw["rosetta"]:
        notes.append("Python is running under Rosetta. Installing a native arm64 Python makes Kurt itself faster.")
    for n in notes:
        console.print(f"[dim]• {escape(n)}[/]")
    lines, after = server_env_help()
    console.print("[dim]Kurt sets these when it starts Ollama itself. If the Ollama app was already "
                  "running, set them once:[/]")
    for ln in lines:
        console.print(f"  {escape(ln)}", markup=True)
    console.print(f"[dim]{escape(after)}[/]")
    console.print("[dim]Override anything with KURT_CTX, KURT_THREADS, KURT_BATCH, KURT_THINK, "
                  "KURT_TUNE=off. `/speed refresh` re-detects the hardware.[/]")


# ----------------------------------------------------------------- Ollama ---
def http_json(path, payload=None, timeout=5):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        HOST + path, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def ollama_up():
    try:
        http_json("/api/tags", timeout=2)
        return True
    except Exception:
        return False


def ensure_ollama():
    global SERVER_EXTERNAL
    if ollama_up():
        SERVER_EXTERNAL = True
        return True
    exe = shutil.which("ollama")
    if not exe:
        console.print(
            "[red]Ollama isn't installed or isn't on PATH.[/] Get it at https://ollama.com/download"
        )
        return False
    env = os.environ.copy()
    for k, v in server_env().items():
        env.setdefault(k, v)  # anything the user already set wins
    with console.status("Starting Ollama..."):
        kwargs = {"env": env}
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000 | 0x00008000  # no window, above-normal priority
        else:
            kwargs["start_new_session"] = True  # stays warm for the next launch, survives Ctrl+C
        subprocess.Popen(
            [exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs
        )
        for _ in range(100):
            if ollama_up():
                return True
            time.sleep(0.2)
    console.print("[red]Could not start Ollama.[/]")
    return False


def installed_models():
    try:
        data = http_json("/api/tags")
    except Exception:
        return []
    out = []
    for m in data.get("models", []):
        if not m.get("name"):
            continue
        d = m.get("details") or {}
        out.append({
            "name": m.get("name", ""),
            "size": m.get("size", 0),
            "params": d.get("parameter_size") or "",
            "quant": d.get("quantization_level") or "",
            "family": d.get("family") or "",
            "modified": m.get("modified_at") or "",
        })
    return out


def params_to_num(text):
    """'7.6B' -> 7.6e9, '270M' -> 2.7e8, '' -> None."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([KMBT]?)\s*", str(text or ""), re.I)
    if not m:
        return None
    try:
        return float(m.group(1)) * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[m.group(2).upper()]
    except (ValueError, KeyError):
        return None


SORT_KEYS = {
    "name": ("Name", False),
    "size": ("Size", True),
    "params": ("Parameters", True),
    "quant": ("Quant", False),
    "family": ("Family", False),
    "date": ("Modified", True),
}
SORT_ALIASES = {
    "n": "name", "name": "name",
    "s": "size", "size": "size", "disk": "size",
    "p": "params", "param": "params", "params": "params", "parameters": "params", "parameter": "params",
    "q": "quant", "quant": "quant", "quantization": "quant",
    "f": "family", "family": "family", "arch": "family",
    "d": "date", "date": "date", "modified": "date", "recent": "date", "time": "date",
}


def sort_models(models, key, desc):
    """Sort by key; models with no value for that key always go last."""
    def val(m):
        if key == "name":
            return m["name"].lower()
        if key == "size":
            return m["size"] or None
        if key == "params":
            return params_to_num(m["params"])
        if key == "quant":
            return m["quant"].lower() or None
        if key == "family":
            return m["family"].lower() or None
        return m["modified"] or None
    have = [m for m in models if val(m) is not None]
    missing = [m for m in models if val(m) is None]
    have.sort(key=lambda m: (val(m), m["name"].lower()), reverse=desc)
    return have + missing


def model_installed():
    if get_remote_model(MODEL):
        return True
    return any(same_model(m["name"], MODEL) for m in installed_models())


LAST_PULL_ERROR = ""


def _progress(text):
    with TERMINAL_LOCK:
        sys.stdout.write("\r\x1b[2K" + text)
        sys.stdout.flush()


def _progress_end():
    with TERMINAL_LOCK:
        sys.stdout.write("\n")
        sys.stdout.flush()


def fmt_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000


def pull_model(name, retries=3):
    """Pull a model, retrying transient network errors (Ollama resumes partial layers)."""
    for attempt in range(retries + 1):
        if _pull_once(name):
            return True
        err = LAST_PULL_ERROR.lower()
        transient = any(t in err for t in ("eof", "max retries", "connection reset", "timed out", "timeout", "unexpected"))
        if not transient or attempt == retries:
            return False
        console.print(f"[yellow]Network hiccup, resuming ({attempt + 1}/{retries}) ...[/]")
        time.sleep(2 + attempt * 2)
    return False


def _pull_once(name):
    global LAST_PULL_ERROR
    LAST_PULL_ERROR = ""
    req = urllib.request.Request(
        HOST + "/api/pull",
        data=json.dumps({"model": name, "stream": True}).encode(),
        headers={"Content-Type": "application/json"},
    )
    shown = False
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            for raw in r:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if ev.get("error"):
                    LAST_PULL_ERROR = str(ev["error"])
                    break
                status = ev.get("status", "")
                total, done = ev.get("total") or 0, ev.get("completed") or 0
                if total:
                    pct = 100 * done / total
                    _progress(f"  {status[:28]:<28} {pct:5.1f}%  {fmt_bytes(done)} / {fmt_bytes(total)}")
                else:
                    _progress(f"  {status[:70]}")
                shown = True
    except urllib.error.HTTPError as e:
        try:
            LAST_PULL_ERROR = json.loads(e.read().decode()).get("error", "") or str(e)
        except Exception:
            LAST_PULL_ERROR = str(e)
    except Exception as e:
        LAST_PULL_ERROR = str(e)
    if shown:
        _progress_end()
    if LAST_PULL_ERROR:
        console.print(f"[red]Ollama:[/] {escape(LAST_PULL_ERROR)}")
        return False
    return True


def ensure_model():
    if get_remote_model(MODEL):
        return True
    if model_installed():
        return True
    console.print(f"[yellow]Model not found locally:[/] {escape(MODEL)}")
    if Prompt.ask("Download it now?", choices=["y", "n"], default="y") != "y":
        return False
    return pull_model(MODEL)


def warm_model(quiet=False):
    """Load a local Ollama model with the exact runner options later requests will use."""
    if get_remote_model(MODEL):
        return
    payload = {"model": MODEL, "keep_alive": KEEP_ALIVE, "options": runner_options()}
    if quiet:
        try:
            http_json("/api/generate", payload, timeout=600)
        except Exception:
            pass
        return
    with console.status(f"Loading {escape(short_model(MODEL))} into memory..."):
        try:
            http_json("/api/generate", payload, timeout=600)
        except Exception as e:
            console.print(f"[yellow]Warm-up skipped:[/] {escape(str(e))}")


_THINK_BROKEN = False  # set when the server rejects the `think` field


def _open_chat(payload, timeout):
    """POST /api/chat. Retries once without `think` if this Ollama/model rejects it."""
    global _THINK_BROKEN
    if _THINK_BROKEN:
        payload.pop("think", None)
    for attempt in (0, 1):
        req = urllib.request.Request(
            HOST + "/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if attempt == 0 and "think" in payload and "think" in body.lower():
                _THINK_BROKEN = True
                payload.pop("think", None)
                continue
            try:
                body = json.loads(body).get("error", body)
            except Exception:
                pass
            raise RuntimeError(body or str(e))


def _chat_payload(model, messages, stream, temperature, think, stop=None):
    options = dict(runner_options(), temperature=temperature)
    if stop:
        options["stop"] = stop
    payload = {
        "model": model, "messages": messages, "stream": stream,
        "keep_alive": KEEP_ALIVE, "options": options,
    }
    if think is not None:
        payload["think"] = think
    return payload


def stream_chat(messages, model=None, think=None, temperature=0.2, max_tokens=None, reasoning_effort=None):
    """Stream a response with explicit per-request reasoning and output controls."""
    selected_model = model or MODEL
    if think is None:
        think = think_enabled()
    remote = get_remote_model(selected_model)
    if remote:
        yield from PROVIDER_CLIENT.stream_chat(
            remote, messages, PROVIDER_KEYS,
            temperature=temperature, max_tokens=max_tokens or 8192,
            reasoning_effort=reasoning_effort,
        )
        return
    payload = _chat_payload(
        selected_model, messages, True, temperature, think, stop=STOP_SEQUENCES
    )
    if max_tokens is not None:
        payload.setdefault("options", {})["num_predict"] = max(256, min(int(max_tokens), 32768))
    with _open_chat(payload, 900) as r:
        for line in r:
            if not line.strip():
                continue
            d = json.loads(line)
            if "error" in d:
                raise RuntimeError(d["error"])
            m = d.get("message", {})
            if m.get("thinking"):
                yield "thinking", m["thinking"]
            if m.get("content"):
                yield "content", m["content"]
            if d.get("done"):
                yield "stats", {
                    "eval_count": d.get("eval_count"),
                    "prompt_eval_count": d.get("prompt_eval_count"),
                    "eval_duration": d.get("eval_duration"),
                    "load_duration": d.get("load_duration"),
                    "prompt_eval_duration": d.get("prompt_eval_duration"),
                }
                break


def chat_once(model, messages, temperature=0.2, timeout=900, think=None):
    remote = get_remote_model(model)
    if remote:
        return PROVIDER_CLIENT.chat_once(
            remote, messages, PROVIDER_KEYS, timeout=timeout, temperature=temperature
        )
    if think is None:
        think = think_enabled()
    payload = _chat_payload(model, messages, False, temperature, think)
    with _open_chat(payload, timeout) as r:
        d = json.loads(r.read().decode() or "{}")
    if "error" in d:
        raise RuntimeError(d["error"])
    out = d.get("message", {}).get("content", "")
    return re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()


# ------------------------------------------------------------ Hugging Face ---
# Ollama itself only runs GGUF. To support every kind of model, Kurt tries, in order:
#   1. the repo is GGUF                      -> `ollama pull hf.co/<repo>[:quant]`
#   2. a ready-made GGUF conversion exists   -> pull that one instead
#   3. otherwise download the original weights (safetensors / pytorch) from Hugging
#      Face and import them: `ollama create` (supported architectures), or convert
#      with llama.cpp's convert_hf_to_gguf.py when it is available (FP8/AWQ/GPTQ etc.).
MODELS_DIR = CONFIG_DIR / "models"
HF_BASE = "https://huggingface.co"
UNSUPPORTED_QUANT_RE = re.compile(r"[-_.](fp8|fp4|nvfp4|awq|gptq|bnb|int4|int8|w4a16|w8a8|mxfp4)\b", re.I)
IMPORT_NAMES = {
    "config.json", "generation_config.json", "tokenizer.json", "tokenizer_config.json",
    "tokenizer.model", "special_tokens_map.json", "added_tokens.json", "vocab.json",
    "merges.txt", "chat_template.jinja", "chat_template.json", "preprocessor_config.json",
}


def parse_hf_ref(text):
    """'user/repo[:quant]' or a huggingface.co link -> (repo, quant) or None."""
    t = text.strip()
    t = re.sub(r"^https?://(www\.)?(huggingface\.co|hf\.co)/", "", t)
    t = re.sub(r"^(huggingface\.co|hf\.co)/", "", t)
    quant = ""
    if ":" in t:
        t, quant = t.split(":", 1)
    parts = [p for p in t.split("/") if p]
    if len(parts) < 2:
        return None
    return "/".join(parts[:2]), quant.strip()


def _hf_headers(extra=None):
    h = {"User-Agent": f"kurt/{__version__}"}
    tok = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if tok:
        h["Authorization"] = "Bearer " + tok
    h.update(extra or {})
    return h


def hf_json(url):
    req = urllib.request.Request(url, headers=_hf_headers())
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def hf_search(query, gguf_only=False, filt=None, limit=10):
    params = {"sort": "downloads", "direction": "-1", "limit": str(limit)}
    if query:
        params["search"] = query
    if filt:
        params["filter"] = filt
    elif gguf_only:
        params["filter"] = "gguf"
    data = hf_json(f"{HF_BASE}/api/models?" + urllib.parse.urlencode(params))
    out = []
    for m in data:
        rid = m.get("id") or m.get("modelId")
        if rid:
            out.append((rid, m.get("downloads", 0), m.get("tags") or []))
    return out


def hf_repo_files(repo):
    """-> [(filename, size_bytes)] for every file in the repo (raises on failure)."""
    info = hf_json(f"{HF_BASE}/api/models/{repo}?blobs=true")
    return [(s["rfilename"], s.get("size") or 0) for s in info.get("siblings", [])]


def hf_config(repo):
    try:
        req = urllib.request.Request(f"{HF_BASE}/{repo}/resolve/main/config.json", headers=_hf_headers())
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except Exception:
        return {}


def repo_formats(files):
    low = [n.lower().rsplit("/", 1)[-1] for n, _ in files]
    return {
        "gguf": any(n.endswith(".gguf") for n in low),
        "safetensors": any(n.endswith(".safetensors") for n in low),
        "bin": any(re.match(r"pytorch_model.*\.bin$", n) for n in low),
    }


def gguf_quants(files):
    found = []
    rx = re.compile(r"(?:^|[-_.])((?:IQ|Q)\d(?:_[A-Z0-9]+)*|BF16|F16|F32)(?=[-_.]|$)", re.I)
    for n, _ in files:
        base = n.rsplit("/", 1)[-1]
        if not base.lower().endswith(".gguf"):
            continue
        m = rx.search(base[:-5])
        if m and m.group(1).upper() not in found:
            found.append(m.group(1).upper())
    return found


def local_model_name(repo, quant=""):
    slug = re.sub(r"[^a-z0-9._-]+", "-", repo.lower().replace("/", "-")).strip("-")
    return f"{slug}:{quant.lower()}" if quant else slug


OLLAMA_NATIVE_QUANTS = ("int4", "int8", "nvfp4", "mxfp4", "mxfp8")


def native_quant(q):
    """GGUF-style name (q4_K_M, q8_0 ...) -> what `ollama create --quantize` accepts for safetensors."""
    low = (q or "").strip().lower()
    if not low or low in OLLAMA_NATIVE_QUANTS:
        return low
    if low.startswith("q8") or low in ("f8", "fp8"):
        return "int8"
    return "int4"


def find_llama_converter():
    """Look for llama.cpp's convert_hf_to_gguf.py (optional)."""
    dirs = [os.environ.get("KURT_LLAMA_CPP")]
    home = Path.home()
    dirs += [home / "llama.cpp", home / "src" / "llama.cpp", Path.cwd() / "llama.cpp", CONFIG_DIR / "llama.cpp"]
    for d in dirs:
        if d and (Path(d) / "convert_hf_to_gguf.py").is_file():
            return str(Path(d) / "convert_hf_to_gguf.py")
    return shutil.which("convert_hf_to_gguf.py") or shutil.which("convert_hf_to_gguf")


def hf_download_repo(repo, files, dest):
    """Download files with resume support. Returns True when everything is present."""
    total = sum(sz for _, sz in files)
    done_before = 0
    t0 = time.time()
    got_this_run = 0
    for idx, (name, size) in enumerate(files, 1):
        path = dest / name
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        if path.exists() and size and path.stat().st_size < size:
            path.replace(part)
        if path.exists() and (not size or path.stat().st_size == size):
            done_before += size
            continue
        url = f"{HF_BASE}/{repo}/resolve/main/{urllib.parse.quote(name)}"
        fails = 0
        while True:
            pos = part.stat().st_size if part.exists() else 0
            start_pos = pos
            hdrs = {"Range": f"bytes={pos}-"} if pos else {}
            try:
                req = urllib.request.Request(url, headers=_hf_headers(hdrs))
                with urllib.request.urlopen(req, timeout=60) as r:
                    if pos and r.status != 206:
                        pos = 0
                    with open(part, "ab" if pos else "wb") as f:
                        while True:
                            chunk = r.read(1 << 20)
                            if not chunk:
                                break
                            f.write(chunk)
                            pos += len(chunk)
                            got_this_run += len(chunk)
                            rate = got_this_run / max(time.time() - t0, 0.1)
                            have = done_before + pos
                            pct = 100 * have / total if total else 0
                            _progress(
                                f"  [{idx}/{len(files)}] {name[:34]:<34} {pct:5.1f}%  "
                                f"{fmt_bytes(have)} / {fmt_bytes(total)}  {fmt_bytes(rate)}/s"
                            )
                if size and part.stat().st_size < size:
                    raise OSError("connection closed before the download finished")
                part.replace(path)
                break
            except urllib.error.HTTPError as e:
                if e.code == 416 and part.exists():  # already complete
                    part.replace(path)
                    break
                _progress_end()
                if e.code in (401, 403):
                    console.print(
                        f"[red]Access denied ({e.code}).[/] This repo may be gated: accept its terms on "
                        "huggingface.co, then set HF_TOKEN to a read token and try again."
                    )
                else:
                    console.print(f"[red]Download error:[/] HTTP {e.code} on {escape(name)}")
                return False
            except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException) as e:
                grew = part.exists() and part.stat().st_size > start_pos
                fails = 0 if grew else fails + 1
                if fails >= 6:
                    _progress_end()
                    console.print(f"[red]Download failed:[/] {escape(str(e))} (run again to resume)")
                    return False
                time.sleep(2 + min(fails, 5) * 2)
        done_before += size
    _progress_end()
    return True


def _run_visible(cmd, **kw):
    """Run a long helper command with its output shown; True when it exits 0."""
    try:
        return subprocess.run(cmd, **kw).returncode == 0
    except OSError as e:
        console.print(f"[red]Could not run {escape(str(cmd[0]))}:[/] {escape(str(e))}")
        return False


def import_weights(repo, files, depth=0):
    """Download original weights and turn them into an Ollama model. -> model name or None."""
    fm = repo_formats(files)
    if not (fm["safetensors"] or fm["bin"]):
        console.print("[red]No safetensors / pytorch weights in this repo, so there is nothing to import.[/]")
        return None
    cfg = hf_config(repo)
    qcfg = cfg.get("quantization_config") or (cfg.get("text_config") or {}).get("quantization_config")
    quantized_src = bool(qcfg) or bool(UNSUPPORTED_QUANT_RE.search(repo))
    conv = find_llama_converter()
    if (quantized_src or not fm["safetensors"]) and not conv:
        why = "pre-quantized weights (FP8/AWQ/GPTQ...)" if quantized_src else "pytorch .bin weights"
        console.print(
            f"[yellow]This repo has {why}. Ollama can't import those directly, and llama.cpp's "
            "convert_hf_to_gguf.py was not found (set KURT_LLAMA_CPP to your llama.cpp folder to use it).[/]"
        )
        base = UNSUPPORTED_QUANT_RE.sub("", repo)
        if depth == 0 and base != repo:
            console.print(f"The full-precision original is probably [bold]{escape(base)}[/].")
            if Prompt.ask("Download that instead?", choices=["y", "n"], default="y", console=console) == "y":
                try:
                    return install_hf_model(base, "", depth=1)
                except Exception as e:
                    console.print(f"[red]Could not use {escape(base)}:[/] {escape(str(e))}")
        return None

    keep = []
    for n, sz in files:
        if "/" in n:
            continue
        low = n.lower()
        if (
            low.endswith(".safetensors") or low.endswith(".safetensors.index.json")
            or low in IMPORT_NAMES
            or (not fm["safetensors"] and re.match(r"pytorch_model.*\.bin(\.index\.json)?$", low))
        ):
            keep.append((n, sz))
    total = sum(sz for _, sz in keep)
    dest = MODELS_DIR / re.sub(r"[^A-Za-z0-9._-]+", "__", repo)
    dest.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(dest).free
    console.print(
        f"Will download [bold]{fmt_bytes(total)}[/] of original weights to {escape(str(dest))} "
        f"([dim]{fmt_bytes(free)} free[/])"
    )
    if total * 1.1 > free:
        console.print("[yellow]Warning: that looks like more than your free disk space.[/]")
    if Prompt.ask("Continue?", choices=["y", "n"], default="y", console=console) != "y":
        return None
    quant = Prompt.ask(
        "Quantize to (Enter = q4_K_M, q8_0, or 'none' for full precision)",
        console=console, default="", show_default=False,
    ).strip() or "q4_K_M"
    if quant.lower() == "none":
        quant = ""
    if not hf_download_repo(repo, keep, dest):
        return None

    source = str(dest)
    quantize_arg = native_quant(quant)  # safetensors import only knows int4/int8/...
    gguf_style = bool(quant) and quant.lower() not in OLLAMA_NATIVE_QUANTS
    if conv and (quantized_src or not fm["safetensors"] or gguf_style):
        out = dest / "model.gguf"
        outtype = "q8_0" if quant.lower() == "q8_0" else "f16"
        console.print(f"[bold]Converting to GGUF with llama.cpp ({outtype}) ...[/]")
        if _run_visible([sys.executable, conv, str(dest), "--outfile", str(out), "--outtype", outtype]):
            source = str(out)
            quantize_arg = "" if outtype == "q8_0" else quant  # GGUF source: q4_K_M etc. work
        elif quantized_src or not fm["safetensors"]:
            console.print("[red]Conversion failed.[/] The converter needs: pip install torch transformers gguf")
            return None
        else:
            console.print("[yellow]Conversion failed; importing the safetensors directly instead.[/]")
    if quantize_arg and source == str(dest) and quantize_arg != quant.lower():
        console.print(f"[dim]Ollama quantizes safetensors as {quantize_arg} ({quant} is a GGUF-only name).[/]")
    name = local_model_name(repo, quant)
    modelfile = dest / "Modelfile"
    modelfile.write_text(f"FROM {source}\n", encoding="utf-8")
    exe = shutil.which("ollama") or "ollama"
    cmd = [exe, "create", name, "-f", str(modelfile)]
    if quantize_arg:
        cmd += ["--quantize", quantize_arg]
    console.print(f"[bold]Importing into Ollama as[/] {escape(name)} ...")
    if not _run_visible(cmd):
        console.print(
            "[red]ollama create failed.[/] The architecture may not be supported by your Ollama version "
            "(update Ollama, or use a GGUF conversion)."
        )
        return None
    if Prompt.ask("Delete the downloaded source files to free disk space?", choices=["y", "n"], default="y", console=console) == "y":
        shutil.rmtree(dest, ignore_errors=True)
    return name


def community_gguf(repo):
    """Offer ready-made GGUF conversions of a non-GGUF repo. -> model name or None."""
    found, seen = [], {repo.lower()}
    base = repo.split("/", 1)[1]
    for kw in (
        {"filt": f"base_model:quantized:{repo}"},
        {"query": UNSUPPORTED_QUANT_RE.sub("", base) + " gguf", "gguf_only": True},
    ):
        try:
            for rid, dl, tags in hf_search(kw.get("query", ""), gguf_only=kw.get("gguf_only", False), filt=kw.get("filt"), limit=8):
                if rid.lower() not in seen and "gguf" in [t.lower() for t in tags]:
                    seen.add(rid.lower())
                    found.append((rid, dl))
        except Exception:
            pass
    if not found:
        return None
    console.print("[green]Ready-made GGUF versions of this model exist:[/]")
    for i, (rid, dl) in enumerate(found[:8], 1):
        console.print(f"  {i}. {escape(rid)}  [dim]{dl:,} downloads[/]")
    pick = Prompt.ask(
        "Pick a number, or Enter to import the original weights instead",
        console=console, default="", show_default=False,
    ).strip()
    if not pick.isdigit() or not 1 <= int(pick) <= len(found[:8]):
        return None
    return install_hf_model(found[int(pick) - 1][0], "", depth=1, allow_import=False)


GGUF_QUANT_RE = re.compile(r"(?:^|[-_.])((?:IQ|Q)\d(?:_[A-Z0-9]+)*|BF16|F16|F32)(?=[-_.]|$)", re.I)
SHARD_RE = re.compile(r"-\d{5}-of-\d{5}\.gguf$", re.I)


def short_local_name(repo, quant=""):
    base = repo.split("/", 1)[-1].lower()
    slug = re.sub(r"[^a-z0-9._-]+", "-", base).strip("-._")[:60].rstrip("-._") or "model"
    return f"{slug}:{quant.lower()}" if quant else slug


def pick_gguf_files(files, quant=""):
    """Choose the GGUF file(s) for a quantization (default Q4_K_M, else the first one). -> (files, quant label)"""
    cands = []
    for n, sz in files:
        base = n.rsplit("/", 1)[-1]
        if not base.lower().endswith(".gguf") or "mmproj" in base.lower():
            continue
        m = GGUF_QUANT_RE.search(base[:-5])
        if not m and "/" in n:
            m = GGUF_QUANT_RE.search(n.rsplit("/", 2)[-2])
        cands.append((n, sz, m.group(1).upper() if m else ""))
    if quant:
        label = quant.upper()
    elif any(c[2] == "Q4_K_M" for c in cands):
        label = "Q4_K_M"
    else:
        label = next((c[2] for c in cands if c[2]), "")
    chosen = [c for c in cands if c[2] == label] if label else cands[:1]
    return [(n, sz) for n, sz, _ in chosen], label


def import_gguf_direct(repo, files, quant=""):
    """Download one GGUF file from the repo and import it under a short local name. -> model name or None."""
    if files is None:
        try:
            files = hf_repo_files(repo)
        except Exception as e:
            console.print(f"[red]Could not read the repo:[/] {escape(str(e))}")
            return None
    chosen, label = pick_gguf_files(files, quant)
    if not chosen:
        avail = gguf_quants(files)
        console.print(
            f"[red]No GGUF file matches {escape(quant or 'the default quantization')}.[/]"
            + (f" Available: {', '.join(avail)}" if avail else "")
        )
        return None
    if len(chosen) > 1 or SHARD_RE.search(chosen[0][0]):
        console.print(
            "[red]This quantization is split into several files,[/] which Ollama can't import directly. "
            "Merge them with llama.cpp's llama-gguf-split --merge, or pick another quantization."
        )
        return None
    total = chosen[0][1]
    dest = MODELS_DIR / re.sub(r"[^A-Za-z0-9._-]+", "__", repo)
    dest.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(dest).free
    console.print(
        f"[yellow]Ollama rejects this repo's long name,[/] so Kurt will download "
        f"{escape(chosen[0][0])} ([bold]{fmt_bytes(total)}[/]) to {escape(str(dest))} "
        f"and import it under a short name ([dim]{fmt_bytes(free)} free[/])."
    )
    if total * 1.1 > free:
        console.print("[yellow]Warning: that looks like more than your free disk space.[/]")
    if Prompt.ask("Continue?", choices=["y", "n"], default="y", console=console) != "y":
        return None
    if not hf_download_repo(repo, chosen, dest):
        return None
    got = (dest / chosen[0][0]).stat().st_size
    if total and got != total:
        console.print(
            f"[red]Downloaded file is incomplete[/] ({fmt_bytes(got)} of {fmt_bytes(total)}). "
            "Run it again to resume."
        )
        return None
    name = short_local_name(repo, label)
    modelfile = dest / "Modelfile"
    modelfile.write_text(f"FROM {dest / chosen[0][0]}\n", encoding="utf-8")
    exe = shutil.which("ollama") or "ollama"
    console.print(f"[bold]Importing into Ollama as[/] {escape(name)} ...")
    if not _run_visible([exe, "create", name, "-f", str(modelfile)]):
        console.print(
            "[red]ollama create failed.[/] The architecture may not be supported by your Ollama version "
            "(update Ollama, or try another quantization)."
        )
        return None
    if Prompt.ask("Delete the downloaded file to free disk space?", choices=["y", "n"], default="y", console=console) == "y":
        shutil.rmtree(dest, ignore_errors=True)
    return name


def install_hf_model(repo, quant="", depth=0, allow_import=True):
    """Get any Hugging Face model into Ollama. Returns the Ollama model name, or None."""
    try:
        files = hf_repo_files(repo)
    except Exception as e:
        files = None
        console.print(f"[yellow]Could not read the repo's file list ({escape(str(e))}); trying a direct pull.[/]")
    fm = repo_formats(files) if files is not None else None

    if fm is None or fm["gguf"]:
        if files is not None and not quant:
            quants = gguf_quants(files)
            if quants:
                console.print(f"[dim]Available quantizations: {', '.join(quants)}[/]")
            quant = Prompt.ask(
                "Quantization, e.g. Q4_K_M (Enter = Ollama's default)",
                console=console, default="", show_default=False,
            ).strip()
        name = f"hf.co/{repo}" + (f":{quant}" if quant else "")
        console.print(f"[bold]Downloading[/] {escape(name)} ...")
        if pull_model(name):
            return name
        if "invalid model name" in LAST_PULL_ERROR.lower():
            return import_gguf_direct(repo, files, quant)
        if fm is not None:
            return None
        if "gguf" not in LAST_PULL_ERROR.lower() and "llama.cpp" not in LAST_PULL_ERROR.lower():
            return None
        try:  # the direct pull said "not GGUF": now we can inspect the repo properly
            files = hf_repo_files(repo)
        except Exception as e:
            console.print(f"[red]Could not read the repo:[/] {escape(str(e))}")
            return None

    console.print(f"[yellow]{escape(repo)} is not a GGUF repo[/] - Ollama can't pull it directly.")
    if allow_import:
        name = community_gguf(repo)
        if name:
            return name
        return import_weights(repo, files, depth)
    return None


def hf_label(tags):
    low = [t.lower() for t in tags]
    kinds = [k for k in ("gguf", "safetensors") if k in low]
    return "/".join(k.upper() if k == "gguf" else k for k in kinds)


def set_model(name):
    global MODEL, SAVED_MODEL
    if get_remote_model(name):
        MODEL = SAVED_MODEL = name
        save_config()
        console.print(f"[green]✔ main model:[/] {escape(short_model(name, 70))}")
        return
    if not ensure_ollama():
        return
    MODEL = SAVED_MODEL = name
    save_config()
    retune()
    warm_model()
    console.print(f"[green]✔ main model:[/] {escape(name)}")


def add_model_flow():
    if not ensure_ollama():
        return
    console.print(
        Panel(
            "Paste a Hugging Face repo (user/name), a huggingface.co link,\n"
            "an Ollama name (llama3.2:3b), or type some words to search.\n"
            "Any format works: GGUF, safetensors, FP8 ... Kurt finds or builds a runnable model.\n"
            "Optional GGUF quantization: user/name:Q4_K_M",
            title="Add a model",
            border_style="green",
            expand=False,
        )
    )
    q = Prompt.ask("Model", console=console, default="", show_default=False).strip()
    if not q:
        return
    if re.fullmatch(r"[A-Za-z0-9._-]+(:[A-Za-z0-9._-]+)?", q) and ":" in q:
        console.print(f"[bold]Downloading[/] {escape(q)} from the Ollama library ...")
        name = q if pull_model(q) else None
    else:
        ref = parse_hf_ref(q) if ("/" in q and " " not in q) else None
        if ref is None:
            try:
                results = hf_search(q)
            except Exception as e:
                console.print(f"[red]Search failed:[/] {escape(str(e))}")
                return
            if not results:
                console.print("[yellow]No models found.[/]")
                return
            for i, (rid, dl, tags) in enumerate(results, 1):
                label = hf_label(tags)
                console.print(
                    f"  {i}. {escape(rid)}  [dim]{dl:,} downloads"
                    + (f" · {label}" if label else "") + "[/]"
                )
            pick = Prompt.ask(
                "Pick a number (Enter to cancel)", console=console, default="", show_default=False
            ).strip()
            if not pick.isdigit() or not 1 <= int(pick) <= len(results):
                return
            ref = (results[int(pick) - 1][0], "")
        repo, quant = ref
        try:
            name = install_hf_model(repo, quant)
        except KeyboardInterrupt:
            console.print("\n[yellow]cancelled (partial downloads can be resumed by running it again)[/]")
            return
    if not name:
        console.print("[red]Could not add that model.[/] Check the name, your connection, and the messages above.")
        return
    console.print("[green]✔ ready[/]")
    if Prompt.ask("Use it as the main model now?", choices=["y", "n"], default="y", console=console) == "y":
        set_model(name)


MENU_SORT_ORDER = ("name", "size", "params")
SORT_LABELS = {"name": "Model", "size": "Size", "params": "Params"}


def read_menu_key():
    """Read one keypress. Returns a character, or 'up'/'down'/'left'/'right'/'esc'/'enter'/'backspace'/'ctrl-c'. None if not a terminal."""
    try:
        if not sys.stdin.isatty():
            return None
    except Exception:
        return None
    if os.name == "nt":
        import msvcrt
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return {"H": "up", "P": "down", "K": "left", "M": "right"}.get(msvcrt.getwch(), "")
        return {"\r": "enter", "\n": "enter", "\x1b": "esc", "\x08": "backspace", "\x03": "ctrl-c"}.get(ch, ch)
    import select
    import termios
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    new = termios.tcgetattr(fd)
    new[3] &= ~(termios.ECHO | termios.ICANON)
    new[6][termios.VMIN] = 1
    new[6][termios.VTIME] = 0
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, new)
        ch = os.read(fd, 1).decode("utf-8", "ignore")
        if ch == "\x1b":
            if not select.select([fd], [], [], 0.05)[0]:
                return "esc"
            seq = os.read(fd, 2).decode("utf-8", "ignore")
            return {"[A": "up", "[B": "down", "[C": "right", "[D": "left"}.get(seq, "")
        return {"\r": "enter", "\n": "enter", "\x7f": "backspace", "\x08": "backspace", "\x03": "ctrl-c"}.get(ch, ch)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _provider_choice():
    providers = list(PROVIDER_SPECS)
    console.print("\n[bold]Providers[/]")
    for i, provider in enumerate(providers, 1):
        console.print(f"  {i}. {provider_label(provider)}  [dim]({provider})[/]")
    raw = Prompt.ask("Provider number or name", console=console, default="").strip().lower()
    aliases = {"claude": "anthropic", "grok": "xai", "gemini": "google", "open-ai": "openai"}
    raw = aliases.get(raw, raw)
    if raw.isdigit() and 1 <= int(raw) <= len(providers):
        return providers[int(raw) - 1]
    if raw in PROVIDER_SPECS:
        return raw
    console.print("[red]Unknown provider.[/]")
    return None


def _provider_groups(provider):
    return sorted({
        str(item.get("group") or "default")
        for item in PROVIDER_KEYS
        if str(item.get("provider", "")).lower() == provider
    })


def add_provider_key_flow():
    provider = _provider_choice()
    if not provider:
        return
    group = Prompt.ask("Key group / pool", console=console, default="default").strip() or "default"
    if len(group) > 50:
        console.print("[red]Group names must be 50 characters or fewer.[/]")
        return
    label = Prompt.ask("Account label", console=console, default=f"{provider_label(provider)} account").strip()
    value = Prompt.ask("API key (input hidden)", console=console, password=True).strip()
    if len(value) < 10 or any(ch.isspace() for ch in value):
        console.print("[red]That doesn't look like a valid API key.[/]")
        return
    if any(item.get("value") == value for item in PROVIDER_KEYS):
        console.print("[yellow]That API key is already saved.[/]")
        return
    PROVIDER_KEYS.append({
        "id": uuid.uuid4().hex[:12], "provider": provider,
        "group": group, "label": label or "Account", "value": value,
    })
    save_config()
    pool_count = sum(
        1 for item in PROVIDER_KEYS
        if item.get("provider") == provider and (item.get("group") or "default") == group
    )
    console.print(
        f"[green]✔ saved[/] {provider_label(provider)} key in pool [bold]{escape(group)}[/]. "
        f"Keys in this pool: {pool_count}"
    )


def edit_provider_key_flow(index):
    if not 1 <= index <= len(PROVIDER_KEYS):
        console.print("[red]No such key number.[/]")
        return
    item = PROVIDER_KEYS[index - 1]
    provider = str(item.get("provider", "")).lower()
    old_group = str(item.get("group") or "default")
    old_label = str(item.get("label") or "Account")
    console.print(
        f"Editing {provider_label(provider)} · {escape(old_label)} · pool {escape(old_group)} · "
        f"{mask_key(item.get('value'))}"
    )
    label = Prompt.ask("Account label (Enter keeps current)", console=console, default=old_label).strip() or old_label
    group = Prompt.ask("Key group / pool (Enter keeps current)", console=console, default=old_group).strip() or old_group
    if len(group) > 50:
        console.print("[red]Group names must be 50 characters or fewer.[/]")
        return
    value = Prompt.ask("New API key (leave blank to keep current; input hidden)", console=console, password=True).strip()
    if value and (len(value) < 10 or any(ch.isspace() for ch in value)):
        console.print("[red]That doesn't look like a valid API key.[/]")
        return
    if value and any(k.get("value") == value and k.get("id") != item.get("id") for k in PROVIDER_KEYS):
        console.print("[yellow]That API key is already saved.[/]")
        return
    item["label"] = label
    item["group"] = group
    if value:
        item["value"] = value
    if group != old_group:
        affected = sum(
            1 for model in REMOTE_MODELS
            if model.get("provider") == provider and (model.get("key_group") or "default") == old_group
        )
        if affected:
            console.print(
                f"[yellow]Note:[/] {affected} model(s) still use pool '{old_group}'. "
                "Edit those models or keep another key in that pool."
            )
    save_config()
    console.print("[green]✔ API key settings updated.[/]")


def delete_provider_key_flow(index):
    if not 1 <= index <= len(PROVIDER_KEYS):
        console.print("[red]No such key number.[/]")
        return
    item = PROVIDER_KEYS[index - 1]
    provider = str(item.get("provider", "")).lower()
    group = str(item.get("group") or "default")
    label = str(item.get("label") or "Account")
    if Prompt.ask(
        f"Remove {provider_label(provider)} key '{label}' from pool '{group}'?",
        choices=["y", "n"], default="n", console=console,
    ) != "y":
        return
    del PROVIDER_KEYS[index - 1]
    remaining = sum(
        1 for key in PROVIDER_KEYS
        if key.get("provider") == provider and (key.get("group") or "default") == group
    )
    save_config()
    console.print(f"[green]✔ key removed[/] · {remaining} key(s) remain in this pool")
    if remaining == 0:
        console.print("[yellow]Models assigned to this pool can no longer make API requests until a key is added.[/]")


def select_menu(title, options, selected=0, subtitle="Use ↑ / ↓ to navigate · Enter to select · Esc to go back"):
    """Small, consistent arrow-key menu. Returns a zero-based option index."""
    if not options:
        return None
    try:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        interactive = False
    if not interactive:
        console.print(Panel("\n".join("{}  {}".format(i + 1, item["label"] if isinstance(item, dict) else item)
                                      for i, item in enumerate(options)), title=title, expand=False))
        return None

    selected = max(0, min(int(selected), len(options) - 1))
    previous_lines = 0
    while True:
        table = Table.grid(padding=(0, 1))
        table.add_column(width=3, justify="center")
        table.add_column(ratio=1)
        for index, option in enumerate(options):
            if isinstance(option, dict):
                label = str(option.get("label", ""))
                description = str(option.get("description", ""))
            else:
                label, description = str(option), ""
            marker = "▶" if index == selected else " "
            style = "bold bright_green" if index == selected else ""
            line = Text(label, style=style)
            if description:
                line.append("  " + description, style="dim")
            table.add_row(Text(marker, style="bright_green"), line)
        with console.capture() as cap:
            console.print(Panel(table, title=title, subtitle=subtitle, border_style="green", expand=False))
        frame = cap.get()
        if previous_lines:
            sys.stdout.write("\x1b[{}A\r\x1b[J".format(previous_lines))
        console.file.write(frame)
        console.file.flush()
        previous_lines = frame.count("\n") + 1
        key = read_menu_key()
        if key in ("ctrl-c", "esc", None):
            return None
        if key == "up":
            selected = (selected - 1) % len(options)
        elif key == "down":
            selected = (selected + 1) % len(options)
        elif key == "enter":
            return selected


def provider_keys_menu():
    while True:
        options = [{"label": "Add API key", "description": "Save another provider key in a pool"}]
        for item in PROVIDER_KEYS:
            options.append({
                "label": "{} · {}".format(provider_label(item.get("provider")), item.get("label") or "Account"),
                "description": "pool {} · {}".format(item.get("group") or "default", mask_key(item.get("value"))),
            })
        options.append({"label": "Back", "description": "Return to model selection"})
        choice = select_menu("API keys · stored locally", options)
        if choice is None or choice == len(options) - 1:
            return
        if choice == 0:
            add_provider_key_flow()
            continue
        index = choice - 1
        if not 0 <= index < len(PROVIDER_KEYS):
            continue
        item = PROVIDER_KEYS[index]
        action = select_menu(
            "{} · {}".format(provider_label(item.get("provider")), item.get("label") or "Account"),
            [
                {"label": "Edit key settings", "description": "Change label, pool, or API key"},
                {"label": "Remove API key", "description": "Delete this key from local settings"},
                {"label": "Back", "description": "Return to API keys"},
            ],
        )
        if action == 0:
            edit_provider_key_flow(index + 1)
        elif action == 1:
            delete_provider_key_flow(index + 1)



def add_remote_model_flow():
    provider = _provider_choice()
    if not provider:
        return
    model_id = Prompt.ask("API model ID (exact provider model name)", console=console, default="").strip()
    if not model_id:
        return
    label = Prompt.ask("Display name", console=console, default=model_id).strip() or model_id
    groups = _provider_groups(provider)
    default_group = groups[0] if groups else "default"
    if groups:
        console.print("Available key pools: " + ", ".join(groups))
    group = Prompt.ask("Key pool for this model", console=console, default=default_group).strip() or "default"
    base_url = default_base_url(provider)
    if provider == "custom":
        base_url = Prompt.ask("OpenAI-compatible API base URL", console=console, default="").strip().rstrip("/")
        if not base_url.startswith(("https://", "http://")):
            console.print("[red]Enter a valid http:// or https:// API base URL.[/]")
            return
    if not any(k.get("provider") == provider and (k.get("group") or "default") == group for k in PROVIDER_KEYS):
        console.print(
            f"[yellow]No key is currently saved for {provider_label(provider)} pool '{group}'. "
            "Add one with /keys before making a request.[/]"
        )
    item = {
        "id": uuid.uuid4().hex[:12], "provider": provider, "model": model_id,
        "label": label, "key_group": group, "base_url": base_url,
    }
    REMOTE_MODELS.append(item)
    save_config()
    console.print(f"[green]✔ added[/] {provider_label(provider)} model {escape(label)}")
    if Prompt.ask("Use it as the main model now?", choices=["y", "n"], default="y", console=console) == "y":
        set_model(remote_model_ref(item))


def edit_remote_model_flow(entry):
    if not entry.get("remote"):
        return "Only API models have editable provider settings."
    model = next((item for item in REMOTE_MODELS if item.get("id") == entry["item"].get("id")), None)
    if model is None:
        return "That API model no longer exists."
    old_id = str(model.get("model", ""))
    old_label = str(model.get("label") or old_id)
    old_group = str(model.get("key_group") or "default")
    new_id = Prompt.ask("API model ID (Enter keeps current)", console=console, default=old_id).strip() or old_id
    new_label = Prompt.ask("Display name (Enter keeps current)", console=console, default=old_label).strip() or old_label
    groups = _provider_groups(str(model.get("provider", "")).lower())
    if groups:
        console.print("Available key pools: " + ", ".join(groups))
    new_group = Prompt.ask("Key pool (Enter keeps current)", console=console, default=old_group).strip() or old_group
    old_base = str(model.get("base_url") or default_base_url(model.get("provider")) or "")
    new_base = Prompt.ask("API base URL (Enter keeps current)", console=console, default=old_base).strip().rstrip("/") or old_base
    if not new_base.startswith(("https://", "http://")):
        return "Enter a valid http:// or https:// API base URL."
    model.update({"model": new_id, "label": new_label, "key_group": new_group, "base_url": new_base})
    save_config()
    if not any(
        key.get("provider") == model.get("provider")
        and (key.get("group") or "default") == new_group
        for key in PROVIDER_KEYS
    ):
        return f"Updated {new_label}, but no API key is saved in pool '{new_group}'. Add one with /keys."
    return f"Updated API model: {provider_label(model.get('provider'))} · {new_label}."


def remove_model_entry(entry):
    global MODEL, SAVED_MODEL
    reference = entry["ref"]
    if entry["remote"]:
        model = entry["item"]
        label = model.get("label") or model.get("model")
        if Prompt.ask(f"Remove API model '{label}' from Kurt?", choices=["y", "n"], default="n", console=console) != "y":
            return "Model removal cancelled."
        REMOTE_MODELS[:] = [item for item in REMOTE_MODELS if item.get("id") != model.get("id")]
    else:
        name = entry["name"]
        if Prompt.ask(
            f"Uninstall local Ollama model '{name}'? This deletes its downloaded weights.",
            choices=["y", "n"], default="n", console=console,
        ) != "y":
            return "Model removal cancelled."
        exe = shutil.which("ollama")
        if not exe:
            return "Ollama is not installed or not on PATH."
        if not ensure_ollama():
            return "Could not start Ollama to uninstall that model."
        result = subprocess.run([exe, "rm", name], capture_output=True, text=True)
        if result.returncode:
            return (result.stderr or result.stdout or "Ollama could not remove that model.").strip()
    WORKERS[:] = [worker for worker in WORKERS if worker != reference]
    save_config()
    if MODEL == reference:
        alternatives = [item["ref"] for item in entry.get("alternatives", [])]
        if alternatives:
            set_model(alternatives[0])
        else:
            MODEL = SAVED_MODEL = DEFAULT_MODEL
            save_config()
            return "Model removed. No alternative model is installed; add one before chatting."
    return "Model removed."


def models_menu():
    global MODEL_SORT, MODEL_SORT_DESC
    while True:
        local_models = installed_models() if ollama_up() else []
        local_models = sort_models(local_models, MODEL_SORT, MODEL_SORT_DESC)
        entries = [
            {"ref": item["name"], "name": item["name"], "provider": "Ollama",
             "remote": False, "item": item}
            for item in local_models
        ]
        for item in sorted(REMOTE_MODELS, key=lambda m: (m.get("provider", ""), m.get("label") or m.get("model", ""))):
            entries.append({
                "ref": remote_model_ref(item), "name": item.get("label") or item.get("model"),
                "provider": provider_label(item.get("provider")), "remote": True, "item": item,
            })
        options = []
        for entry in entries:
            markers = []
            if entry["ref"] == MODEL:
                markers.append("active")
            suffix = " · " + ", ".join(markers) if markers else ""
            options.append({
                "label": "{}  [{}]{}".format(entry["name"], entry["provider"], suffix),
                "description": "API model" if entry["remote"] else "Local model",
            })
        action_start = len(options)
        options.extend([
            {"label": "Add local model", "description": "Search Hugging Face or enter an Ollama model"},
            {"label": "Add API model", "description": "Configure Claude, OpenAI, Grok, OpenRouter, and more"},
            {"label": "Manage API keys", "description": "Add, edit, or remove provider keys and pools"},
            {"label": "Change model sorting", "description": "Current: {} · {}".format(SORT_LABELS.get(MODEL_SORT, "Size"), "descending" if MODEL_SORT_DESC else "ascending")},
            {"label": "Close", "description": "Return to chat"},
        ])
        choice = select_menu("Models · local and hosted", options)
        if choice is None or choice == action_start + 4:
            return
        if choice < action_start:
            entry = entries[choice]
            actions = [{"label": "Use as main model", "description": "Make this the model used for new messages"}]
            if entry["remote"]:
                actions.append({"label": "Edit model settings", "description": "Change model ID, key pool, or endpoint"})
            actions.extend([
                {"label": "Remove model", "description": "Remove its configuration or uninstall local weights"},
                {"label": "Back", "description": "Return to model list"},
            ])
            action = select_menu(entry["name"], actions)
            if action is None or action == len(actions) - 1:
                continue
            if action == 0:
                set_model(entry["ref"])
            elif entry["remote"] and action == 1:
                console.print(edit_remote_model_flow(entry))
            elif action == (2 if entry["remote"] else 1):
                removal = dict(entry)
                removal["alternatives"] = [item for item in entries if item["ref"] != entry["ref"]]
                console.print(remove_model_entry(removal))
            continue
        action = choice - action_start
        if action == 0:
            add_model_flow()
        elif action == 1:
            add_remote_model_flow()
        elif action == 2:
            provider_keys_menu()
        elif action == 3:
            sort_choice = select_menu("Sort local models", [
                {"label": "Model name", "description": "Alphabetical"},
                {"label": "Model size", "description": "Largest first"},
                {"label": "Parameter count", "description": "Largest first"},
                {"label": "Reverse sort order", "description": "Toggle ascending / descending"},
                {"label": "Back"},
            ])
            if sort_choice in (0, 1, 2):
                MODEL_SORT = ("name", "size", "params")[sort_choice]
                MODEL_SORT_DESC = SORT_KEYS[MODEL_SORT][1]
                save_config()
            elif sort_choice == 3:
                MODEL_SORT_DESC = not MODEL_SORT_DESC
                save_config()


# ------------------------------------------------------- busy-time input ---
class BusyListener:
    """While Kurt works, keep the bottom box alive so Esc / Ctrl+C can stop generation."""

    def __init__(self):
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        try:
            interactive = sys.stdin.isatty()
        except Exception:
            interactive = False
        if interactive:
            if TERMINAL_UI and TERMINAL_UI.enabled:
                target = self._run_ui
            else:
                target = self._run_windows if os.name == "nt" else self._run_posix
            self._thread = threading.Thread(target=target, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        return False

    def _handle(self, line):
        if not line.strip():
            return
        if TERMINAL_UI and TERMINAL_UI.enabled:
            TERMINAL_UI.set_notice("K is busy · Esc stops it")
        else:
            console.print("[dim]Kurt is busy (Ctrl+C stops it).[/]")

    def _run_ui(self):
        import _thread
        ui = TERMINAL_UI
        ed = ui.editor
        try:
            with ui.raw():
                while not self._stop.is_set():
                    ev = ui.keys.read(timeout=0.1)
                    if ev is None:
                        continue
                    if ev[0] == "eof":
                        return
                    act = ed.feed(ev)
                    if act == "submit":
                        self._handle(ed.value())
                        ed.remember()
                        ed.reset()
                    elif act in ("interrupt", "escape"):
                        _thread.interrupt_main()
                    ui.redraw()
        except Exception:
            return

    def _run_posix(self):
        try:
            fd = sys.stdin.fileno()
        except Exception:
            return
        import select
        buf = b""
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([fd], [], [], 0.1)
                if not ready:
                    continue
                data = os.read(fd, 4096)
            except Exception:
                return
            if not data:
                return
            buf += data
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                self._handle(raw.decode("utf-8", "replace"))

    def _run_windows(self):
        import msvcrt
        buf = ""
        while not self._stop.is_set():
            if not msvcrt.kbhit():
                time.sleep(0.03)
                continue
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                msvcrt.getwch()
                continue
            if ch in ("\r", "\n"):
                self._handle(buf)
                buf = ""
            elif ch == "\x08":
                if buf:
                    buf = buf[:-1]
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
            elif ch == "\x03":
                import _thread
                _thread.interrupt_main()
            elif ch >= " ":
                buf += ch
                sys.stdout.write(ch)
                sys.stdout.flush()


# ------------------------------------------------------------- File utils ---
def safe_path(p):
    """Resolve p relative to cwd; return None if it escapes the project dir."""
    try:
        full = (Path.cwd() / p.strip().strip("'\"")).resolve()
        full.relative_to(Path.cwd().resolve())
        return full
    except Exception:
        return None


def read_file(path):
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        raw = f.read()
    crlf = "\r\n" in raw
    return raw.replace("\r\n", "\n"), crlf


def write_file(path, text, crlf=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if crlf:
        text = text.replace("\n", "\r\n")
    tmp = path.with_name(path.name + ".kurt-tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        if path.exists():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)  # a crash mid-write can no longer leave a half-written file
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)


def project_tree(limit=150):
    root = Path.cwd()
    out = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS and not d.startswith("."))
        for fn in sorted(files):
            if fn.startswith(".") and fn not in (".env.example", ".gitignore"):
                continue
            out.append(str(Path(dirpath, fn).relative_to(root)).replace("\\", "/"))
            if len(out) >= limit:
                out.append("... (truncated)")
                return "\n".join(out)
    return "\n".join(out) or "(empty directory)"


def file_cap():
    """Max characters of one file sent to the model; smaller contexts get smaller files."""
    return max(6000, min(30000, int(CTX * 2)))


def clip_text(text, name, cap):
    """Cut text to `cap` characters at a line boundary and say how to read the rest."""
    if len(text) <= cap:
        return text
    cut = text[:cap].rsplit("\n", 1)[0]
    shown = cut.count("\n") + 1
    total = text.count("\n") + 1
    return (cut + f"\n... [truncated: showing lines 1-{shown} of {total}. "
            f"Use READ {name}:{shown + 1}-{min(total, shown + 200)} to read more]")


def looks_binary(text):
    return "\x00" in text[:8000]


def project_notes():
    """Optional KURT.md in the project root: standing instructions for this project."""
    p = Path.cwd() / "KURT.md"
    try:
        if p.is_file():
            text, _ = read_file(p)
            return text.strip()[:4000]
    except OSError:
        pass
    return ""


def context_budget():
    return int(CTX * 3)


def message_chars(message):
    content = message.get("content")
    if isinstance(content, list):
        return sum(len(str(part.get("text", ""))) for part in content if isinstance(part, dict))
    return len(str(content or ""))


def context_chars(messages):
    return sum(message_chars(m) for m in messages)


def context_percent(messages):
    return min(999, int(100 * context_chars(messages) / max(1, context_budget())))


def check_syntax(path, text):
    """Return a short error message when a .py or .json file does not parse, else ''."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".py":
            compile(text, str(path), "exec")
        elif suffix == ".json":
            json.loads(text)
    except SyntaxError as e:
        return f"{e.msg} (line {e.lineno})"
    except ValueError as e:
        return str(e)
    return ""


def _indent_of(line):
    return line[: len(line) - len(line.lstrip())]


def find_span(text, search):
    """Locate `search` in `text`, tolerating the small whitespace slips small models make.

    Returns (start, end, how, indent_fix) or (None, None, 'none'|'many', None). `indent_fix` maps
    each indentation used in `search` to the file's indentation when the match ignored indentation."""
    n = text.count(search)
    if n == 1:
        i = text.index(search)
        return i, i + len(search), "exact", None
    if n > 1:
        return None, None, "many", None
    trimmed = search.strip("\n")
    if trimmed and trimmed != search:
        n = text.count(trimmed)
        if n == 1:
            i = text.index(trimmed)
            return i, i + len(trimmed), "trimmed", None
        if n > 1:
            return None, None, "many", None
    slines = trimmed.split("\n") if trimmed else []
    if not slines:
        return None, None, "none", None
    tlines = text.split("\n")
    starts, pos = [], 0
    for ln in tlines:
        starts.append(pos)
        pos += len(ln) + 1
    k = len(slines)
    for how, key in (("trailing whitespace", str.rstrip), ("indentation", str.strip)):
        want = [key(s) for s in slines]
        hits = [
            i for i in range(len(tlines) - k + 1)
            if all(key(tlines[i + j]) == want[j] for j in range(k))
        ]
        if len(hits) > 1:
            return None, None, "many", None
        if hits:
            i = hits[0]
            fix = None
            if how == "indentation":
                fix = {}
                for j, s in enumerate(slines):
                    if s.strip():
                        fix.setdefault(_indent_of(s), _indent_of(tlines[i + j]))
            return starts[i], starts[i + k - 1] + len(tlines[i + k - 1]), how, fix
    return None, None, "none", None


def reindent(block, fix):
    """Rewrite the indentation of a replacement the way the matched file text is indented."""
    if not fix or all(k == v for k, v in fix.items()):
        return block
    out = []
    for ln in block.split("\n"):
        if not ln.strip():
            out.append(ln)
            continue
        ind = _indent_of(ln)
        keys = [k for k in fix if ind.startswith(k)]
        if not keys:
            out.append(ln)
            continue
        key = max(keys, key=len)
        out.append(fix[key] + ind[len(key):] + ln[len(ind):])
    return "\n".join(out)


def closest_snippet(text, search, limit=14):
    """The part of the file that looks most like a SEARCH block that failed to match."""
    import difflib

    tlines = text.split("\n")
    slines = [s for s in search.split("\n") if s.strip()]
    if not slines or len(tlines) > 6000:
        return ""
    probe = slines[0].strip()
    best, best_i = 0.0, -1
    for i, ln in enumerate(tlines):
        if not ln.strip():
            continue
        r = difflib.SequenceMatcher(None, probe, ln.strip()).ratio()
        if r > best:
            best, best_i = r, i
    if best < 0.6:
        return ""
    end = min(len(tlines), best_i + max(len(slines), 3) + 1)
    end = min(end, best_i + limit)
    snippet = "\n".join(tlines[best_i:end])
    return f"\nClosest text in the file (lines {best_i + 1}-{end}):\n{snippet}"


def diff_panel(old, new, name, title):
    import difflib

    a, b = old.splitlines(), new.splitlines()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    w = len(str(max(len(a), len(b), 1)))
    t = Text()
    added = removed = rows = 0
    limit = 60
    first = True
    for group in sm.get_grouped_opcodes(2):
        if not first:
            t.append(" " * (w + 2) + "⋯\n", style="dim")
        first = False
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for k in range(i1, i2):
                    if rows < limit:
                        t.append(f" {j1 + k - i1 + 1:>{w}}   {a[k]}\n", style="dim")
                    rows += 1
                continue
            for k in range(i1, i2):
                removed += 1
                if rows < limit:
                    t.append(f" {k + 1:>{w}} - {a[k]}\n", style="red")
                rows += 1
            for k in range(j1, j2):
                added += 1
                if rows < limit:
                    t.append(f" {k + 1:>{w}} + {b[k]}\n", style="green")
                rows += 1
    if rows > limit:
        t.append(f"\n … {rows - limit} more lines", style="dim")
    if not t.plain.strip():
        t = Text("(no changes)", style="dim")
    t.rstrip()
    return Panel(
        t, title=f"[bold]{title}[/] {escape(name)}", border_style="yellow",
        subtitle=f"[green]+{added}[/] [red]-{removed}[/]", subtitle_align="right",
    )


def new_file_panel(name, text):
    from rich.syntax import Syntax

    lines = text.splitlines()
    shown = "\n".join(lines[:40]) + (f"\n… {len(lines) - 40} more lines" if len(lines) > 40 else "")
    try:
        lexer = Syntax.guess_lexer(name, shown)
    except Exception:
        lexer = "text"
    return Panel(
        Syntax(shown, lexer, theme="ansi_dark", line_numbers=True, word_wrap=True),
        title=f"[bold]New file[/] {escape(name)}", border_style="green",
        subtitle=f"{len(lines)} lines", subtitle_align="right",
    )


def attach_mentions(text):
    extra, seen = [], set()
    cap = file_cap()
    for m in re.findall(r"(?<!\S)@([\w./\\-]+)", text):
        m = m.rstrip(".,;:")  # "see @auth.py." should still find auth.py
        if m in seen:
            continue
        seen.add(m)
        p = safe_path(m)
        if p and p.is_file():
            content, _ = read_file(p)
            if looks_binary(content):
                console.print(f"[dim]skipped {m} (binary file)[/]")
                continue
            extra.append(f"\n\n[Attached file {m}]\n{clip_text(content, m, cap)}")
            console.print(f"[dim]📎 attached {m}[/]")
    return text + "".join(extra)


class StreamPrinter:
    """Batches streamed text so the terminal is redrawn ~20 times a second, not once per token."""

    def __init__(self, interval=0.05):
        self.interval = interval
        self.parts = []
        self.style = None
        self.last = time.monotonic()

    def write(self, text, style=None):
        if self.parts and style != self.style:
            self.flush()
        self.style = style
        self.parts.append(text)
        if time.monotonic() - self.last >= self.interval:
            self.flush()

    def flush(self):
        if self.parts:
            text, self.parts = "".join(self.parts), []
            console.print(text, style=self.style, end="", markup=False, soft_wrap=True)
        self.last = time.monotonic()


class ActionFilter:
    """Hides @@@ action blocks from the streamed text; they are shown as tidy boxes afterwards."""

    OPEN = "@@@ "
    CLOSE = "@@@ END"

    def __init__(self):
        self.pending = ""
        self.in_block = False

    def feed(self, chunk):
        self.pending += chunk
        out = []
        while True:
            if self.in_block:
                i = self.pending.find(self.CLOSE)
                if i < 0:
                    self.pending = self.pending[-(len(self.CLOSE) - 1):]
                    break
                self.pending = self.pending[i + len(self.CLOSE):].lstrip("\n")
                self.in_block = False
                continue
            i = self.pending.find(self.OPEN)
            if i >= 0:
                out.append(self.pending[:i].rstrip(" "))
                self.pending = self.pending[i:]
                self.in_block = True
                continue
            keep = 0
            for n in range(min(len(self.OPEN) - 1, len(self.pending)), 0, -1):
                if self.OPEN.startswith(self.pending[-n:]):
                    keep = n
                    break
            out.append(self.pending[: len(self.pending) - keep])
            self.pending = self.pending[len(self.pending) - keep:]
            break
        return "".join(out)


# =============================================================== Shared sessions ===
# The terminal interface and the browser interface are two front ends over the same state:
#
#   ~/.kurt/config.json            models, provider keys, hardware tuning
#   ~/.kurt/web_settings.json      system prompt, API groups, defaults
#   ~/.kurt/chats.json             every conversation (CLI chats appear in the browser and back)
#   ~/.kurt/web_workspaces/<id>/   private scratch workspace and uploads for one conversation
#   ~/.kurt/web_envs/<id>/         private Python virtual environment for one conversation
#
# A CLI session is simply a conversation in chats.json. All command execution goes through the
# macOS Seatbelt sandbox in kurt_sandbox.py, inside that conversation's own venv. Kurt itself is
# never run from those venvs and user code is never run from Kurt's own interpreter environment.
CLI_MODES = ("normal", "agent")
DEFAULT_PROMPT_NOTE = "Provider-owned internal instructions are not available to Kurt."
RATIONALE_PANEL_LIMIT = 4000


def _redact_keys(text):
    for entry in PROVIDER_KEYS:
        secret = str(entry.get("value") or "")
        if len(secret) >= 8:
            text = text.replace(secret, "[REDACTED API KEY]")
    return text


def sandbox_available():
    return sys.platform == "darwin" and bool(
        shutil.which("sandbox-exec") or Path("/usr/bin/sandbox-exec").is_file()
    )


def trim_history(messages, model):
    """Keep local models inside their context window. The two system messages always stay."""
    if get_remote_model(model):
        return
    budget = context_budget()
    if len(messages) > 1 and message_chars(messages[1]) > budget // 2:
        messages[1]["content"] = messages[1]["content"][: budget // 2] + "\n[Workspace context clipped for this model's context window.]"
    total = context_chars(messages)
    if total <= budget:
        return
    target = int(budget * 0.7)
    dropped = 0
    while total > target and len(messages) > 3:
        total -= message_chars(messages[2])
        del messages[2]
        dropped += 1
    if dropped:
        console.print(f"[dim]↻ left out {dropped} older message(s) so this model's context window is not exceeded.[/]")


class TurnAborted(Exception):
    pass


class CliSession:
    """One terminal conversation backed by a shared chat record."""

    def __init__(self, app, web, chat):
        self.app = app
        self.web = web
        self.chat_id = chat["id"]
        self.route = str(chat.get("route") or "")
        self.undo = []
        self.origin = Path.cwd().resolve()

    @property
    def chat(self):
        return self.app.get_chat(self.chat_id)

    @property
    def mode(self):
        return self.web.valid_mode_value((self.chat or {}).get("mode"))

    @property
    def linked(self):
        return str((self.chat or {}).get("linked_folder") or "")

    def work_root(self):
        return self.app.chat_work_root(self.chat_id)

    def sync_cwd(self):
        try:
            os.chdir(self.work_root())
        except (OSError, ValueError) as exc:
            console.print(f"[yellow]Could not enter the session folder:[/] {escape(str(exc))}")
        if TERMINAL_UI is not None:
            TERMINAL_UI._file_cache = []
            TERMINAL_UI.tag = self.tag()

    def tag(self):
        return f"{self.mode} · {'linked' if self.linked else 'private'}"

    def save(self):
        self.app.save_chats()


class ChatTurn:
    """One user message handled exactly like the browser's chat endpoint, with terminal prompts
    standing in for the Approve / Decline cards."""

    MAX_ROUNDS = 4

    def __init__(self, session, user_text, model_text=None):
        self.s = session
        self.app = session.app
        self.W = session.web
        self.user_text = user_text
        self.model_text = model_text or user_text
        self.worklog = []
        self.artifacts = {}
        self.messages = []
        self.model = ""
        self.route_candidates = []
        self.active = 0
        self.failed = set()
        self.original_keys = []
        self.gen_seconds = 0.0
        self.gen_calls = 0
        self.usage_calls = 0
        self.usage_tokens = 0
        self.output_chars = 0
        self.thinking_seconds = 0.0
        self.rationale_chunks = []
        self.last_reasoning = ""
        self.last_meter = None
        self.mode = session.mode
        self.effort = "thinking" if self.mode == "normal" else "expert"
        self.use_thinking = think_enabled()

    # ------------------------------------------------------------ helpers
    def log(self, text, ok=None, detail=None):
        self.worklog.append({"text": str(text), "ok": ok, "detail": str(detail or "")[:12000]})
        if len(self.worklog) > 120:
            del self.worklog[:-120]
        mark = {True: "[green]✔[/]", False: "[red]✖[/]"}.get(ok, "[dim]•[/]")
        console.print(f"  {mark} [dim]{escape(str(text))}[/]")

    def apply_candidate(self, candidate):
        self.model = candidate["model"]
        keys = candidate.get("provider_keys")
        PROVIDER_KEYS[:] = self.original_keys
        self.app.set_model(self.model)
        if keys is not None:
            PROVIDER_KEYS[:] = list(keys)
        elif candidate.get("key_id"):
            chosen = next((k for k in self.original_keys if str(k.get("id")) == str(candidate["key_id"])), None)
            if chosen is None:
                raise ValueError("The selected API key is no longer available.")
            PROVIDER_KEYS[:] = [chosen]

    def ask(self, question):
        try:
            return Prompt.ask(question, choices=["y", "n"], default="n", console=console) == "y"
        except (KeyboardInterrupt, EOFError):
            console.print()
            return False

    def approval(self, kind, title, detail="", command="", path=""):
        payload = {"id": uuid.uuid4().hex, "kind": kind, "title": title, "detail": detail,
                   "command": command, "path": path, "artifactPath": path}
        item = {"kind": "approval", "text": title, "ok": None,
                "detail": detail or command or path or "No action will occur until you approve.",
                "approval": payload, "approved": None}
        self.worklog.append(item)
        approved = self.ask(f"{title} — approve?")
        item["approved"] = approved
        item["ok"] = approved
        return approved, item

    # --------------------------------------------------------- generation
    def stream_once(self, call_messages, think):
        meter = Meter()
        self.last_meter = meter
        ui = TERMINAL_UI if TERMINAL_UI and TERMINAL_UI.enabled else None
        console.print()
        if ui:
            ui.set_busy(True)
            ui.begin_work(meter)
        else:
            console.print(Text("✦ K is thinking…", style="bold bright_green"))
        printer = StreamPrinter()
        afilter = ActionFilter()
        answer, rationale = [], []
        state = {"in_think": False, "pend": "", "thinking": False, "answer": False, "ended": None}
        started = time.monotonic()
        exact_tokens = None
        stream = None

        def push_reasoning(text):
            if not text:
                return
            rationale.append(text)
            meter.label = "Thinking"
            if not state["thinking"]:
                printer.flush()
                console.print(Text("Thinking", style="bold green"))
                state["thinking"] = True
            printer.write(text, "dim green")

        def push_answer(text):
            if not text:
                return
            meter.label = "Preparing action" if afilter.in_block else "Writing"
            if not state["answer"]:
                printer.flush()
                if state["thinking"]:
                    console.print()
                console.print(Text("K › ", style="bold bright_green"), end="")
                state["answer"] = True
            answer.append(text)
            visible = afilter.feed(text)
            if visible:
                printer.write(visible)
            if state["ended"] is None and rationale and text.strip():
                state["ended"] = time.monotonic()

        def route_text(text, final=False):
            state["pend"] += text
            while True:
                tag = self.W.THINK_TAG_RE.search(state["pend"])
                if tag:
                    before = state["pend"][:tag.start()]
                    closing = bool(tag.group(1))
                    if closing and not state["in_think"]:
                        earlier = "".join(answer) + before
                        answer.clear()
                        push_reasoning(earlier)
                    elif state["in_think"]:
                        push_reasoning(before)
                    else:
                        push_answer(before)
                    state["in_think"] = not closing
                    state["pend"] = state["pend"][tag.end():]
                    continue
                hold = ""
                if not final:
                    cut = state["pend"].rfind("<")
                    if cut != -1 and ">" not in state["pend"][cut:] and len(state["pend"]) - cut < 12:
                        hold = state["pend"][cut:]
                        state["pend"] = state["pend"][:cut]
                (push_reasoning if state["in_think"] else push_answer)(state["pend"])
                state["pend"] = hold
                break

        self.gen_calls += 1
        try:
            with BusyListener():
                stream = stream_chat(
                    call_messages, model=self.model, think=think,
                    temperature={"thinking": 0.2, "expert": 0.15}[self.effort],
                    max_tokens={"thinking": 8192, "expert": 16384}[self.effort],
                    reasoning_effort={"thinking": "medium", "expert": "high"}[self.effort],
                )
                for kind, chunk in stream:
                    if not chunk:
                        continue
                    name = str(kind or "").strip().lower().replace("-", "_")
                    if name in self.W.MODEL_RATIONALE_KINDS or name in self.W.THINKING_KINDS:
                        if isinstance(chunk, str):
                            meter.tokens += 1
                            push_reasoning(chunk)
                        continue
                    if name in ("usage", "stats", "metadata") and isinstance(chunk, dict):
                        usage = chunk.get("usage") if isinstance(chunk.get("usage"), dict) else chunk
                        value = next((usage.get(k) for k in (
                            "completion_tokens", "output_tokens", "completionTokens",
                            "outputTokens", "eval_count") if usage.get(k) is not None), None)
                        try:
                            if value is not None:
                                exact_tokens = max(int(value), exact_tokens or 0)
                                meter.tokens = exact_tokens
                        except (TypeError, ValueError):
                            pass
                        meter.gen_secs = (usage.get("eval_duration") or 0) / 1e9
                        meter.wait_secs = ((usage.get("load_duration") or 0)
                                           + (usage.get("prompt_eval_duration") or 0)) / 1e9
                        continue
                    if name == "content":
                        meter.tokens += 1
                        route_text(chunk if isinstance(chunk, str) else str(chunk))
                route_text("", final=True)
        except KeyboardInterrupt:
            printer.flush()
            console.print("\n[yellow]⏹ interrupted[/]")
            raise TurnAborted()
        except Exception:
            printer.flush()
            raise
        finally:
            printer.flush()
            meter.stop()
            if stream is not None and callable(getattr(stream, "close", None)):
                try:
                    stream.close()
                except Exception:
                    pass
            self.gen_seconds += max(0.0, time.monotonic() - started)
            if ui:
                ui.end_work()
                ui.set_busy(False)
        raw = "".join(answer).strip()
        if not raw:
            raise RuntimeError("The model returned no response text.")
        console.print()
        print_summary(meter)
        if exact_tokens is not None:
            self.usage_calls += 1
            self.usage_tokens += exact_tokens
        self.output_chars += sum(len(c) for c in answer)
        self.last_reasoning = "".join(rationale).strip()
        if rationale:
            self.thinking_seconds += max(0.0, (state["ended"] or time.monotonic()) - started)
            self.rationale_chunks.extend(rationale)
        return raw

    def generate(self):
        available = [i for i in range(len(self.route_candidates)) if i not in self.failed]
        if self.active in available:
            available.remove(self.active)
            available.insert(0, self.active)
        queue = list(available)
        empty_retries = {}
        last_error = None
        while queue:
            index = queue.pop(0)
            candidate = self.route_candidates[index]
            retry_empty = empty_retries.get(index, 0) > 0
            try:
                self.apply_candidate(candidate)
                call_messages = self.messages
                if retry_empty:
                    call_messages = list(self.messages) + [{
                        "role": "system",
                        "content": "Your previous reply was empty. Reply now with visible text or one complete action block (@@@ RUN ... @@@ END). Do not answer with reasoning only.",
                    }]
                raw = self.stream_once(call_messages, False if retry_empty else self.use_thinking)
                self.active = index
                return raw
            except (TurnAborted, KeyboardInterrupt):
                raise
            except Exception as exc:
                last_error = exc
                if str(exc) == "The model returned no response text." and empty_retries.get(index, 0) < 2:
                    empty_retries[index] = empty_retries.get(index, 0) + 1
                    queue.insert(0, index)
                    self.log("The model returned an empty reply. Retrying…", None)
                    continue
                self.failed.add(index)
                remaining = [i for i in range(len(self.route_candidates)) if i not in self.failed]
                if not remaining:
                    raise RuntimeError("Every model/key entry in this route failed. Last error: " + str(exc)) from exc
                nxt = self.route_candidates[remaining[0]]
                self.log("This model or key did not complete the request. Switching to the next entry…", False,
                         "Failed entry: {}\nReason: {}\nNext entry: {}".format(
                             candidate.get("member_label") or candidate.get("model"), str(exc)[:1200],
                             nxt.get("member_label") or nxt.get("model")))
                queue = [i for i in remaining]
        raise RuntimeError("No working model/key entry was found." + (f" {last_error}" if last_error else ""))

    def stats(self):
        seconds = max(self.gen_seconds, 0.0)
        exact = self.gen_calls > 0 and self.usage_calls == self.gen_calls
        tokens = self.usage_tokens if exact else (max(1, round(self.output_chars / 4)) if self.output_chars else 0)
        rate = tokens / seconds if seconds > 0 and tokens > 0 else 0.0
        return {"thought_seconds": round(seconds, 1), "output_tokens": int(tokens),
                "tokens_per_second": round(rate, 1), "tokens_estimated": not exact}

    # ------------------------------------------------------------ actions
    @staticmethod
    def search_text(query, result):
        if not result.get("ok"):
            return "Web search for {!r} did not complete: {}".format(query, result.get("message", "unknown error"))
        lines = ["Web search results for: " + query]
        for number, item in enumerate(result.get("results", []), 1):
            lines.append("{}. {}\nURL: {}\nSnippet: {}".format(
                number, item.get("title", "Untitled"), item.get("url", ""), item.get("snippet", "")))
        return "\n\n".join(lines)

    def do_command(self, command):
        chat = self.s.chat
        try:
            self.app.prewarm_chat_environment(self.s.chat_id, with_pip=self.app._command_needs_pip(command))
        except Exception:
            pass
        console.print(Panel(
            Syntax(command, "bash", theme="ansi_dark", word_wrap=True, background_color="default"),
            title="[bold]Run command[/]", border_style="red",
            subtitle="sandboxed · needs your OK", subtitle_align="right",
        ))
        detail = "This exact command runs in the session's isolated workspace" + (
            " / linked folder" if chat.get("linked_folder") else "") + "."
        approved, item = self.approval("command", "Run terminal command", detail=detail, command=command)
        if not approved:
            return "[Command declined] The user declined this command. It was not executed: " + command
        try:
            result = self.app.run_command(self.s.chat_id, command)
        except Exception as exc:
            result = {"ok": False, "returncode": -1, "output": str(exc), "elapsed": None}
        code = result.get("returncode", -1)
        ok = bool(result.get("ok"))
        output = str(result.get("output") or "(No terminal output.)")
        elapsed = result.get("elapsed")
        item["output"] = {"text": output, "returncode": code, "ok": ok, "elapsed": elapsed}
        shown = output[:3000] + ("\n… (output truncated)" if len(output) > 3000 else "")
        console.print(Panel(Text(shown), title="[bold]Output[/]", subtitle=f"exit {code}",
                            subtitle_align="right", border_style="dim" if ok else "red"))
        return ("[Actual terminal result]\nCommand: {}\nExit code: {}\nStatus: {}\nDuration: {}\nOutput:\n{}\n"
                "Use this actual result; do not claim success when the exit code is non-zero.").format(
            command, code, "succeeded" if ok else "failed",
            (str(elapsed) + "s") if elapsed is not None else "not reported", output)

    def do_write(self, action, target, body):
        proposal = "@@@ {} {}\n{}\n@@@ END".format(action, target, body)
        proposed, errors = self.app.artifacts_from_answer(proposal, self.s.chat_id)
        if errors or not proposed:
            message = "; ".join(errors) if errors else "No valid file proposal was produced."
            self.log("File proposal rejected: " + target, False, message)
            return "[File proposal rejected] {}: {}".format(target, message)
        artifact = proposed[0]
        if not (artifact.get("validation") or {}).get("ok", False):
            return "[File proposal rejected] {}: validation failed.".format(target)
        self.artifacts[artifact["path"]] = artifact
        path = Path(artifact["target_path"])
        old = None
        if artifact.get("exists"):
            try:
                old, _ = read_file(path)
            except OSError:
                old = None
        display = str(path)
        if old is None:
            console.print(new_file_panel(display, artifact["content"]))
        else:
            console.print(diff_panel(old, artifact["content"], display, "Change"))
        verb = "Create " if not artifact.get("exists") else "Replace "
        approved, _ = self.approval("file", "Save file change", path=str(path), detail=verb + str(path))
        if not approved:
            artifact["validation"]["message"] = "Declined · not saved"
            return "[File write declined] The file was not saved: " + str(path)
        try:
            saved = self.app.apply_approved_file(
                self.s.chat_id, artifact["target_path"], artifact["content"],
                artifact.get("expected_sha256"), artifact.get("linked_root", ""))
        except Exception as exc:
            artifact["validation"] = {"ok": False, "checked": True, "message": "Write failed: " + str(exc)}
            self.log("Approved file write failed: " + str(path), False, str(exc))
            return "[File write failed] {}: {}".format(target, exc)
        self.s.undo.append((path, old))
        artifact["workspace_written"] = True
        artifact["approval_required"] = False
        artifact["validation"]["message"] = "Saved with approval"
        self.log("Saved approved file: " + str(path), True, saved.get("message", ""))
        return "[File write succeeded] {}".format(saved.get("path"))

    def do_read(self, argument):
        if not argument:
            return "[Read rejected] A file path is required."
        try:
            path = self.app.resolve_user_read_path(self.s.chat_id, argument)
            content = self.app.read_agent_file(str(path))
        except Exception as exc:
            self.log("Local file read failed: " + argument, False, str(exc))
            return "[Local file read failed]\nPath: {}\nReason: {}".format(argument, exc)
        cap = file_cap() if not get_remote_model(self.model) else 120000
        note = ""
        if len(content) > cap:
            note = "\n[Truncated: showing the first {:,} of {:,} characters.]".format(cap, len(content))
            content = content[:cap]
        self.log("Read local file: " + str(path), True, "Read-only access; no file was changed.")
        return "[Local file read]\nPath: {}\n\n{}{}".format(path, content, note)

    def do_list(self, argument):
        try:
            path = self.app.resolve_user_read_path(self.s.chat_id, argument or ".")
            path, entries = self.app.list_agent_directory(str(path))
        except Exception as exc:
            self.log("Directory listing failed: " + (argument or "."), False, str(exc))
            return "[Local directory listing failed]\nPath: {}\nReason: {}".format(argument, exc)
        lines = ["[Local directory listing]\nPath: " + str(path)]
        for item in entries:
            label = "[DIR] " if item["directory"] else "[LINK] " if item.get("symlink") else "[FILE] "
            lines.append(label + item["name"] + (" ({})".format(fmt_bytes(item["size"])) if item["size"] else ""))
        self.log("Listed local directory: " + str(path), True, "Read-only listing; links are not followed.")
        return "\n".join(lines)

    def do_search(self, query):
        self.log("Searching the web: " + query, None)
        result = self.app.search_web(query)
        details = self.search_text(query, result)
        self.log(result.get("message", "Web search completed."), result.get("ok"), details[:10000])
        return "[Web search result]\n" + details

    def do_subagents(self, actions):
        if self.mode != "agent":
            return "[Subagents unavailable] Subagents can only be used in Agent mode. Continue without delegating."
        actions = actions[:3]
        records = [{"name": name, "task": task} for _pos, name, task in actions]
        self.log("Lead agent delegated {} task(s) to subagents.".format(len(records)), None,
                 "Children see only recent chat context and their task; they cannot read files, run commands or write.")

        def run_child(task):
            system = ("You are a read-only analysis subagent. Do not access files, request tools, run commands, "
                      "or propose mutations. Return concise findings and uncertainties to the lead agent.")
            history = self.messages[-8:] + [{"role": "system", "content": system},
                                            {"role": "user", "content": "Delegated task: " + task}]
            parts = []
            for kind, chunk in stream_chat(history, model=self.model, think=False, temperature=0.2,
                                           max_tokens=3072, reasoning_effort="medium"):
                if kind == "content" and isinstance(chunk, str):
                    parts.append(chunk)
            return "".join(parts).strip()[:12000] or "No findings returned."

        findings = []
        with console.status(f"[bold]🤖 {len(records)} subagent(s) working…[/]"):
            with concurrent_futures.ThreadPoolExecutor(max_workers=len(records), thread_name_prefix="kurt-sub") as pool:
                futures = {pool.submit(run_child, rec["task"]): (i, rec) for i, rec in enumerate(records)}
                for future in concurrent_futures.as_completed(futures):
                    i, rec = futures[future]
                    try:
                        text, ok = future.result(), True
                    except Exception as exc:
                        text, ok = "Subagent failed: " + str(exc), False
                    findings.append((i, rec["name"], text))
                    self.log("Subagent {} {}.".format(rec["name"], "completed" if ok else "failed"), ok, text)
        findings.sort()
        return "[Subagent findings for the lead agent]\n" + "\n\n".join(
            "### {}\n{}".format(name, text) for _i, name, text in findings)

    def collect_actions(self, raw):
        W = self.W
        found = []
        simple = (("RUN", W.RUN_ACTION_RE), ("SEARCH", W.SEARCH_ACTION_RE), ("READ", W.READ_ACTION_RE),
                  ("LIST", W.LIST_ACTION_RE), ("AGENT_RUN", W.AGENT_RUN_ACTION_RE),
                  ("AGENT_READ", W.AGENT_READ_ACTION_RE), ("AGENT_LIST", W.AGENT_LIST_ACTION_RE))
        for name, regex in simple:
            for match in regex.finditer(raw):
                found.append((match.start(), name, W.action_argument(match)))
        for match in W.SUBAGENT_ACTION_RE.finditer(raw):
            found.append((match.start(), "SUBAGENT", (match.group(1).strip(), match.group(2).strip())))
        for match in BLOCK_RE.finditer(raw):
            if match.group(1) in ("WRITE", "EDIT"):
                found.append((match.start(), match.group(1), (match.group(2).strip(), match.group(3))))
        for match in W.AGENT_WRITE_ACTION_RE.finditer(raw):
            found.append((match.start(), "AGENT_WRITE", (match.group(1).strip(), match.group(2))))
        found.sort(key=lambda item: item[0])
        return found

    def run_actions(self, actions):
        results = []
        subs = [(pos, arg[0].strip()[:48] or "Subagent", arg[1].strip()) for pos, act, arg in actions if act == "SUBAGENT"]
        if subs:
            results.append(self.do_subagents(subs))
        for _pos, action, argument in actions:
            if action == "SUBAGENT":
                continue
            if action in ("RUN", "AGENT_RUN"):
                results.append(self.do_command(argument) if argument
                               else "[Command rejected] The command action did not include a command.")
            elif action in ("WRITE", "EDIT", "AGENT_WRITE"):
                results.append(self.do_write(action, argument[0], argument[1]))
            elif action in ("READ", "AGENT_READ"):
                results.append(self.do_read(argument))
            elif action in ("LIST", "AGENT_LIST"):
                results.append(self.do_list(argument))
            else:
                results.append(self.do_search(argument))
        return results

    # --------------------------------------------------------------- main
    def prepare(self):
        s, app, W = self.s, self.app, self.W
        chat = s.chat
        chat_id = s.chat_id
        route_info = app.resolve_chat_route(s.route, MODEL)
        self.route_info = route_info
        previous = [
            item for item in chat.get("messages", [])[-W.MAX_MESSAGES_PER_CHAT:]
            if item.get("role") in ("user", "assistant")
            and not str(item.get("content") or "").startswith(W.EMPTY_REPLY_PLACEHOLDER_PREFIX)
            and str(item.get("content") or "").strip()
        ]
        app.store_chat_input(chat_id, self.user_text, len(chat.get("messages", [])) + 1)
        context = app.context_contents(chat_id)
        images, image_note = app.context_image_parts(chat_id)
        self.messages = [
            {"role": "system", "content": app.chat_system_prompt(chat_id, mode=self.mode, effort=self.effort)},
            {"role": "system", "content": context + "\n" + image_note + "\nUploaded files are copied into this chat's private workspace. Separately, READ/LIST may inspect local paths that the current OS account can access; uploads do not alter the originals."},
        ]
        notes = project_notes()
        if notes:
            self.messages[0]["content"] += "\nProject notes from KURT.md (the user wrote these; follow them):\n" + notes + "\n"
        self.messages.extend({"role": m["role"], "content": m["content"]} for m in previous)
        payload = ([{"type": "text", "text": self.model_text}] + images) if images else self.model_text
        self.messages.append({"role": "user", "content": payload})
        record = {"role": "user", "content": self.user_text, "message_id": uuid.uuid4().hex}
        attachments = [item["path"] for item in app.context_file_list(chat_id)]
        if attachments:
            record["attachments"] = attachments
        chat["messages"].append(record)
        chat["updated"] = time.time()
        chat["model"] = route_info["model"]
        chat["route"] = route_info["route"]
        chat["effort"] = self.effort
        chat["mode"] = self.mode
        s.save()
        self.route_candidates = route_info.get("candidates") or [route_info]
        self.model = route_info["model"]

    def run(self):
        global CONTEXT_PCT
        app, W, s = self.app, self.W, self.s
        try:
            self.prepare()
        except Exception as exc:
            console.print(f"[red]{escape(str(exc))}[/] [dim]Use /models or /route to pick a model.[/]")
            return
        app.model_lock.acquire()
        self.original_keys = list(PROVIDER_KEYS)
        try:
            self.apply_candidate(self.route_candidates[0])
            final_raw, validation_errors = "", []
            for round_index in range(self.MAX_ROUNDS):
                trim_history(self.messages, self.model)
                CONTEXT_PCT = 0 if get_remote_model(self.model) else context_percent(self.messages)
                if round_index == 0 and W.DESKTOP_VISUAL_REQUEST_RE.search(self.user_text):
                    final_raw = ("I can't capture or view your live desktop, screen, or GUI. Attach a screenshot to show me "
                                 "what is on screen. Both modes can read files the current OS account may access; file "
                                 "changes and commands still require your approval.")
                    console.print(Text("K › ", style="bold bright_green"), end="")
                    console.print(final_raw)
                    break
                if round_index == 0 and self.mode == "normal" and W.NORMAL_MODE_LOCAL_ACCESS_RE.search(self.user_text):
                    self.log("Local access is available in Normal mode too.", True,
                             "Reads and listings use your OS account's permissions; changes and commands need approval.")
                raw = self.generate()
                raw = W.ALT_ACTION_OPENER_RE.sub("\n@@@ ", raw)
                rationale_blocks, _reply = W.split_thinking(raw)
                round_rationale = "\n\n".join(p for p in (self.last_reasoning, rationale_blocks.strip()) if p).strip()
                if round_rationale:
                    self.worklog.append({"kind": "thinking", "text": "Thinking", "ok": None, "detail": round_rationale[:12000]})
                if rationale_blocks.strip():
                    console.print(Panel(Text(rationale_blocks.strip()[:RATIONALE_PANEL_LIMIT], style="dim"),
                                        title="Rationale", border_style="dim", expand=False))
                actions = self.collect_actions(raw)
                if actions:
                    if round_index >= self.MAX_ROUNDS - 1:
                        validation_errors.append("Kurt reached the maximum of {} tool rounds.".format(self.MAX_ROUNDS))
                    else:
                        results = self.run_actions(actions)
                        self.messages.append({"role": "assistant", "content": raw})
                        failed_search = any("did not complete" in str(item) for item in results)
                        self.messages.append({"role": "user", "content": "\n\n".join(results)
                            + "\n\nUse these actual results. Continue the task; do not repeat completed actions."
                            + ("\nWeb search failed. Do not retry it more than once more. If it keeps failing, answer in plain text from your own knowledge and say clearly that it could not be verified online."
                               if failed_search else "")})
                        continue
                edit_marker = re.search(W.ACTION_MARKER_RE + r"\s*(?:WRITE|EDIT|SYSTEM_PROMPT|AGENT_WRITE)\b", raw, re.I)
                if not actions and not edit_marker:
                    _r, probe = W.split_thinking(raw)
                    probe = BLOCK_RE.sub("", W.strip_unsupported_actions(probe)).strip()
                    if not re.search(r"\w", probe) and round_index < self.MAX_ROUNDS - 1:
                        self.log("The model produced no visible answer. Asking it to answer in plain text.", False)
                        self.messages.append({"role": "assistant", "content": raw})
                        self.messages.append({"role": "user", "content": "Your previous reply had no visible answer: it contained only reasoning, an unsupported tool call, or an incomplete action block. Answer the user's last question directly in plain text now. If you need current information, use exactly one complete block: @@@ SEARCH, the query on the next line, then @@@ END. Do not use any other tool-call format."})
                        continue
                if edit_marker:
                    preamble = raw[:edit_marker.start()]
                    has_summary = re.search(r"(?i)change\s+summary|planned\s+changes|changes\s+to\s+make", preamble)
                    if not has_summary and round_index < self.MAX_ROUNDS - 1:
                        self.log("Requesting the required file-by-file change summary before accepting edits.", False)
                        self.messages.append({"role": "assistant", "content": raw})
                        self.messages.append({"role": "user", "content": "Your response contains WRITE/EDIT actions but no clear file-by-file Change summary before them. Provide a detailed Change summary describing exactly what each file will change and why, then reproduce the complete action blocks. Do not omit or shorten file contents."})
                        continue
                    if not has_summary:
                        validation_errors.append("File changes were withheld because Kurt did not provide the required change summary.")
                        final_raw = "Kurt proposed file changes but did not provide the required file-by-file Change summary. No files from that response were accepted."
                        break
                new_artifacts, validation_errors = app.artifacts_from_answer(raw, s.chat_id)
                for artifact in [a for a in new_artifacts if a.get("kind") == "system_prompt"]:
                    self.propose_system_prompt(artifact)
                if validation_errors and round_index < self.MAX_ROUNDS - 1:
                    details = "\n".join("- " + item for item in validation_errors)
                    self.log("A generated file did not pass validation. Asking Kurt to repair it.", False, details)
                    self.messages.append({"role": "assistant", "content": raw})
                    self.messages.append({"role": "user", "content": "The application rejected these file changes during syntax/structure validation:\n" + details + "\nFix the reported problems, provide a concise Change summary, and return complete corrected file actions. Do not claim checks passed unless they do."})
                    continue
                final_raw = raw
                break
            self.finish(final_raw, validation_errors)
        except TurnAborted:
            console.print("[dim]Turn stopped. Your message is saved; ask again or rephrase.[/]")
        except KeyboardInterrupt:
            console.print("\n[yellow]⏹ interrupted[/]")
        except Exception as exc:
            console.print(f"\n[red]Error talking to the model:[/] {escape(str(exc))}")
        finally:
            PROVIDER_KEYS[:] = self.original_keys
            app.model_lock.release()

    def propose_system_prompt(self, artifact):
        content = str(artifact.get("content") or "").strip()
        if not content:
            return
        console.print(Panel(Text(_redact_keys(content)[:6000]), title="Proposed Kurt system prompt", border_style="yellow"))
        if self.ask("Apply this prompt to Kurt?"):
            self.app.settings["system_prompt"] = content
            self.app.save_settings()
            self.log("Applied the proposed system prompt.", True)
        else:
            self.log("Declined the proposed system prompt.", True)

    def finish(self, final_raw, validation_errors):
        W, app, s = self.W, self.app, self.s
        chat = s.chat
        stats = self.stats()
        _unused, reply = W.split_thinking(final_raw)
        reply = re.sub(r"\n{3,}", "\n\n", W.strip_unsupported_actions(reply)).strip()
        if validation_errors:
            reply = (reply + "\n\n**Validation warning:** " + "; ".join(validation_errors)).strip()
            console.print(f"[yellow]Validation warning:[/] {escape('; '.join(validation_errors))}")
        visible = BLOCK_RE.sub("", reply).strip()
        visible, code_artifacts = W.extract_long_code_artifacts(visible)
        for artifact in code_artifacts:
            stem, suffix = os.path.splitext(artifact["path"])
            candidate, number = artifact["path"], 2
            taken = {str(n).casefold() for n in self.artifacts}
            while candidate.casefold() in taken:
                candidate = "{}-{}{}".format(stem or "code-block", number, suffix)
                number += 1
            artifact["path"] = candidate
            self.artifacts[candidate] = artifact
        artifacts = list(self.artifacts.values())
        if visible and chat.get("title") in (None, "New conversation"):
            chat["title"] = app.generate_chat_title(self.user_text, visible, self.model)
        if (not visible or not re.search(r"\w", visible)) and not artifacts:
            visible = W.EMPTY_REPLY_PLACEHOLDER_PREFIX + " or validated file artifact. Check the work log for tool and validation results."
        rationale = "\n\n".join(str(i.get("detail") or "") for i in self.worklog
                                if isinstance(i, dict) and i.get("kind") == "thinking").strip()
        chat["messages"].append({
            "role": "assistant", "content": visible, "artifacts": artifacts,
            "worklog": self.worklog, "reasoning": rationale,
            "thinking_seconds": round(self.thinking_seconds, 1),
            "effort": self.effort, "mode": self.mode, "route": self.route_info["route"],
            "model": self.model, "stats": stats,
        })
        chat["updated"] = time.time()
        chat["model"] = self.model
        chat["effort"] = self.effort
        chat["mode"] = self.mode
        s.save()


def run_chat_turn(session, user_text, model_text=None):
    ChatTurn(session, user_text, model_text).run()


# ------------------------------------------------------------------ commands ---
HELP = """[bold]Conversations[/]  (shared with the browser interface)
  /new              start a new conversation
  /chats            pick a saved conversation to resume
  /resume \\[n|id]    resume by list number, id prefix, or the latest one
  /rename <title>   rename this conversation
  /delete           delete this conversation with its workspace and environment

[bold]Working[/]
  /mode \\[normal|agent]  Normal: everyday help · Agent: multi-step work with subagents
  /link \\[path]      let Kurt edit a folder (default: the current one); every change still needs approval
  /unlink           go back to this conversation's private workspace
  /workspace        show the working folder, private workspace, environment and sandbox
  /env \\[pip|reset]  show, prepare (with pip) or delete this conversation's private Python environment
  /files            list the files in the working folder
  /add <path>       copy a file into this conversation's workspace (or use @path in a message)
  /undo             restore the last file Kurt saved in this session

[bold]Models[/]
  /models           switch, add or remove local and API models
  /route            choose an API key, failover group or model route for this conversation
  /keys             add, edit or remove provider API keys
  /system-prompt \\[file <path>|reset]  show, replace or reset Kurt's system prompt
  /speed \\[refresh]  hardware report and speed settings
  /think \\[on|off|auto]  thinking for local models
  /fast \\[model]     switch to a small, fast local model
  /context          how full the model's context window is
  /exit             quit  (Esc stops a response)

Every command and every file change asks for approval first.
[bold]Tip[/] put standing instructions for a project in a KURT.md file in its root folder."""


def show_system_prompt(session):
    prompt = session.app.chat_system_prompt(session.chat_id, mode=session.mode,
                                            effort="thinking" if session.mode == "normal" else "expert")
    console.print(Panel(Text(_redact_keys(prompt)), title="Active Kurt system prompt", border_style="green"))
    console.print(f"[dim]{DEFAULT_PROMPT_NOTE} API keys are redacted. Change it with /system-prompt file <path> or reset it with /system-prompt reset.[/]")


def chat_label(chat, index=None):
    title = chat.get("title") or "New conversation"
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(chat.get("updated") or 0))
    head = f"{index}. " if index else ""
    return "{}{}".format(head, title), "{} · {} · {}{}".format(
        chat.get("mode", "normal"), when, (chat.get("id") or "")[:8], " · linked" if chat.get("linked_folder") else "")


def reload_chats(session):
    session.app.chats = session.web._load_json(session.web.CHATS_FILE, session.app.chats)


def discard_empty(app, chat_id):
    """Conversations that never received a message are not kept."""
    chat = app.get_chat(chat_id)
    if chat is None or chat.get("messages"):
        return
    app.chats[:] = [c for c in app.chats if c.get("id") != chat_id]
    app.save_chats()
    for folder in (CONFIG_DIR / "web_workspaces" / chat_id, CONFIG_DIR / "web_envs" / chat_id):
        shutil.rmtree(folder, ignore_errors=True)


def switch_chat(session, chat):
    if session.chat_id != chat["id"]:
        discard_empty(session.app, session.chat_id)
    session.chat_id = chat["id"]
    session.route = str(chat.get("route") or "")
    session.undo = []
    session.sync_cwd()
    linked = chat.get("linked_folder")
    console.print(f"[green]✔ {escape(chat.get('title') or 'New conversation')}[/] [dim]· {session.tag()} · {escape(short_path(Path.cwd(), 60))}[/]")


def offer_link(session):
    here = session.origin
    if here == Path.home().resolve() or here == Path(here.anchor):
        console.print("[dim]Working in this conversation's private workspace. Use /link <folder> to let Kurt edit a project.[/]")
        session.sync_cwd()
        return
    answer = Prompt.ask(
        f"Link {short_path(here, 60)} so Kurt can read and edit it? (each change still needs your approval)",
        choices=["y", "n"], default="y", console=console)
    if answer == "y":
        session.chat["linked_folder"] = str(here)
        session.save()
        console.print("[green]✔ linked[/]")
    session.sync_cwd()


def new_session_chat(session, mode=None):
    chat = session.app.create_chat()
    if mode in CLI_MODES:
        chat["mode"] = mode
        chat["effort"] = "thinking" if mode == "normal" else "expert"
    session.chat_id = chat["id"]
    session.route = str(chat.get("route") or "")
    session.undo = []
    session.save()
    return chat


def env_report(session):
    app = session.app
    env_root = app.chat_environment(session.chat_id)
    bin_dir = env_root / ("Scripts" if os.name == "nt" else "bin")
    python = bin_dir / ("python.exe" if os.name == "nt" else "python")
    pip = bin_dir / ("pip.exe" if os.name == "nt" else "pip")
    table = Table(box=None, show_header=False, padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Conversation", f"{session.chat.get('title')}  [dim]{session.chat_id[:8]}[/]")
    table.add_row("Mode", session.mode)
    table.add_row("Working folder", escape(str(session.work_root())) + ("  [dim](linked)[/]" if session.linked else "  [dim](private)[/]"))
    table.add_row("Private workspace", escape(str(app.chat_workspace(session.chat_id))))
    table.add_row("Python environment", escape(str(env_root)) + ("  [green]ready[/]" if python.is_file() else "  [dim]built on first command[/]"))
    table.add_row("pip", "installed" if pip.exists() else "added on first pip command")
    table.add_row("Command sandbox", "macOS Seatbelt" if sandbox_available() else "[red]unavailable on this platform: commands will not run[/]")
    console.print(Panel(table, title="Workspace", border_style="green", expand=False))


def handle_command(session, line):
    global THINK, MODEL, SAVED_MODEL
    app, W = session.app, session.web
    cmd, _, arg = line.partition(" ")
    cmd, arg = cmd.lower(), arg.strip()
    if cmd in ("/exit", "/quit", "/q"):
        return "exit"
    if cmd == "/help":
        console.print(HELP)
    elif cmd in ("/new", "/clear"):
        discard_empty(app, session.chat_id)
        new_session_chat(session)
        console.print("[green]✔ new conversation[/]")
        offer_link(session)
    elif cmd in ("/chats", "/resume"):
        reload_chats(session)
        chats = list(app.chats)
        if not chats:
            console.print("[dim]no saved conversations[/]")
            return None
        pick = None
        if cmd == "/resume" and arg:
            if arg.isdigit() and 1 <= int(arg) <= len(chats):
                pick = chats[int(arg) - 1]
            else:
                pick = next((c for c in chats if str(c.get("id", "")).startswith(arg)), None)
            if pick is None and arg.lower() == "latest":
                pick = chats[0]
        else:
            options = []
            for number, chat in enumerate(chats[:30], 1):
                label, description = chat_label(chat)
                options.append({"label": label, "description": description})
            choice = select_menu("Conversations · shared with the browser", options)
            if choice is None:
                for number, chat in enumerate(chats[:30], 1):
                    label, description = chat_label(chat, number)
                    console.print(f"  {escape(label)}  [dim]{escape(description)}[/]")
                console.print("[dim]Use /resume <number> to open one.[/]")
                return None
            pick = chats[choice]
        if pick is None:
            console.print("[red]No such conversation.[/]")
        else:
            switch_chat(session, pick)
    elif cmd == "/rename":
        if not arg:
            console.print("usage: /rename <title>", markup=False)
        else:
            session.chat["title"] = arg[:80]
            session.save()
            console.print("[green]✔ renamed[/]")
    elif cmd == "/delete":
        title = session.chat.get("title")
        if Prompt.ask(f"Delete '{title}' with its workspace and environment? This cannot be undone.",
                      choices=["y", "n"], default="n", console=console) != "y":
            return None
        old = session.chat_id
        app.chats[:] = [c for c in app.chats if c.get("id") != old]
        for folder in (CONFIG_DIR / "web_workspaces" / old, CONFIG_DIR / "web_envs" / old):
            shutil.rmtree(folder, ignore_errors=True)
        new_session_chat(session)
        session.sync_cwd()
        console.print("[green]✔ deleted[/] [dim]· started a new conversation (use /link to relink a folder)[/]")
    elif cmd == "/mode":
        wanted = arg.lower()
        if not wanted:
            console.print(f"mode: [bold]{session.mode}[/]  [dim](/mode normal · /mode agent)[/]")
        elif wanted in CLI_MODES:
            session.chat["mode"] = wanted
            session.chat["effort"] = "thinking" if wanted == "normal" else "expert"
            session.save()
            session.sync_cwd()
            console.print(f"[green]✔ {wanted} mode[/]")
        else:
            console.print("usage: /mode [normal|agent]", markup=False)
    elif cmd == "/link":
        target = Path(arg).expanduser() if arg else session.origin
        if not target.is_absolute():
            target = session.origin / target
        if target.is_symlink() or not target.is_dir():
            console.print("[red]That is not a regular folder.[/]")
            return None
        target = target.resolve()
        if target == Path(target.anchor):
            console.print("[red]Refusing to link the filesystem root.[/]")
            return None
        if target == Path.home().resolve() and Prompt.ask(
                "That is your whole home folder. Link it anyway?", choices=["y", "n"], default="n", console=console) != "y":
            return None
        session.chat["linked_folder"] = str(target)
        session.save()
        session.sync_cwd()
        console.print(f"[green]✔ linked[/] {escape(str(target))} [dim]· changes and commands still need approval[/]")
    elif cmd == "/unlink":
        session.chat["linked_folder"] = ""
        session.save()
        session.sync_cwd()
        console.print("[green]✔ unlinked[/] [dim]· working in the private workspace[/]")
    elif cmd in ("/workspace", "/where"):
        env_report(session)
    elif cmd == "/env":
        sub = arg.lower()
        if sub == "pip":
            with console.status("Preparing this conversation's environment with pip..."):
                try:
                    app.ensure_chat_environment(session.chat_id, with_pip=True)
                except Exception as exc:
                    console.print(f"[red]{escape(str(exc))}[/]")
        elif sub == "reset":
            if Prompt.ask("Delete this conversation's Python environment? It is rebuilt on the next command.",
                          choices=["y", "n"], default="n", console=console) == "y":
                shutil.rmtree(app.chat_environment(session.chat_id), ignore_errors=True)
                with app._env_jobs_lock:
                    app._env_jobs.clear()
                console.print("[green]✔ environment removed[/]")
        env_report(session)
    elif cmd == "/models":
        before = MODEL
        models_menu()
        app.invalidate_installed_models()
        if MODEL != before:
            session.route = "model|" + MODEL
            session.chat["route"] = session.route
            session.chat["model"] = MODEL
            app.settings["model"] = MODEL
            app.save_settings()
            session.save()
        retune()
    elif cmd == "/route":
        app.sync_api_groups()
        routes = app.list_chat_routes()
        if not routes:
            console.print("[yellow]No routes yet. Add a model with /models.[/]")
            return None
        choice = select_menu("Route for this conversation", [{"label": r["label"], "description": r.get("kind", "")} for r in routes])
        if choice is not None:
            session.route = routes[choice]["value"]
            session.chat["route"] = session.route
            session.save()
            console.print(f"[green]✔ route:[/] {escape(routes[choice]['label'])}")
    elif cmd == "/keys":
        provider_keys_menu()
    elif cmd == "/system-prompt":
        sub, _, rest = arg.partition(" ")
        if sub.lower() == "reset":
            app.settings["system_prompt"] = ""
            app.save_settings()
            console.print("[green]✔ Kurt's default system prompt restored[/]")
        elif sub.lower() == "file" and rest.strip():
            try:
                text = Path(rest.strip()).expanduser().read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                console.print(f"[red]Could not read that file:[/] {escape(str(exc))}")
                return None
            app.settings["system_prompt"] = text.strip()
            app.save_settings()
            console.print("[green]✔ system prompt replaced[/]")
        elif sub:
            console.print("usage: /system-prompt [file <path>|reset]", markup=False)
        else:
            show_system_prompt(session)
    elif cmd == "/speed":
        speed_report(refresh=arg.lower() == "refresh")
    elif cmd == "/fast":
        name = arg or FAST_MODEL
        if not any(same_model(m["name"], name) for m in installed_models()):
            console.print(f"[bold]Downloading[/] {escape(name)} (small, one time) ...")
            if not pull_model(name):
                console.print("[red]Download failed.[/] Check the name and your connection.")
                return None
        set_model(name)
        app.invalidate_installed_models()
        session.route = "model|" + name
        session.chat["route"] = session.route
        session.save()
        console.print("[dim]Use /models to go back to the bigger model.[/]")
    elif cmd == "/think":
        wanted = arg.lower()
        if wanted in ("on", "off"):
            THINK = wanted == "on"
        elif wanted in ("auto", "default"):
            THINK = None
        elif wanted:
            console.print("usage: /think [on|off|auto]", markup=False)
            return None
        else:
            THINK = not think_enabled()
        save_config()
        retune()
        console.print(f"thinking: [bold]{'ON' if think_enabled() else 'OFF'}[/]" + (" [dim](auto)[/]" if THINK is None else ""))
    elif cmd == "/files":
        console.print(app.tree(session.work_root()), markup=False)
    elif cmd == "/add":
        source = Path(arg).expanduser() if arg else None
        if source is not None and not source.is_absolute():
            source = (session.linked and Path(session.linked) or session.origin) / source
        if source is None or not source.is_file() or source.is_symlink():
            console.print("[red]Give the path of a regular file.[/]")
        elif app._skip_context_name(source.name):
            console.print("[red]That file looks like it holds credentials, so it was not copied.[/]")
        elif source.stat().st_size > 20_000_000:
            console.print("[red]That file is larger than 20 MB.[/]")
        else:
            destination = app.chat_workspace(session.chat_id) / source.name
            number = 2
            while destination.exists():
                destination = destination.with_name(f"{source.stem}-{number}{source.suffix}")
                number += 1
            shutil.copy2(source, destination)
            console.print(f"[dim]added {escape(source.name)} to this conversation's workspace; the original is untouched[/]")
    elif cmd == "/context":
        console.print(f"context: {CONTEXT_PCT}% of the local model's window" if CONTEXT_PCT
                      else "context: not tracked for hosted models or an empty history")
    elif cmd == "/undo":
        if not session.undo:
            console.print("[dim]nothing to undo in this session[/]")
        else:
            path, old = session.undo.pop()
            if old is None:
                path.unlink(missing_ok=True)
            else:
                write_file(path, old, False)
            console.print(f"[green]↩ reverted {escape(path.name)}[/]")
    else:
        console.print("[red]unknown command[/] — try /help")
    return None


# --------------------------------------------------------------- Input box ---
def read_input():
    """Read from Kurt's fixed bottom input box when the terminal supports it."""
    if TERMINAL_UI and TERMINAL_UI.enabled:
        return TERMINAL_UI.read_line()
    return console.input("[bold bright_green]kurt >[/] [dim]/help · /mode · /models · @file[/] ")


def _cfg_is_first_scan():
    return not HW_CACHE_FILE.exists()


def pick_resume(app, resume):
    chats = list(app.chats)
    if not chats:
        return None
    if resume in ("", "latest"):
        return chats[0]
    if resume.isdigit() and 1 <= int(resume) <= len(chats):
        return chats[int(resume) - 1]
    return next((c for c in chats if str(c.get("id", "")).startswith(resume)), None)


def cli_main(resume=None, mode=None):
    global TERMINAL_UI, TIP_SHOWN, CONTEXT_PCT, MODEL, SAVED_MODEL
    sys.modules.setdefault("kurt", sys.modules[__name__])
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if os.name == "nt":
        os.system("")
    import kurt_web as web
    app = web.KurtWebApp()
    app.sync_api_groups()
    if MODEL.startswith("remote:") and not get_remote_model(MODEL):
        console.print("[yellow]The saved API model no longer exists. Choose another model with /models.[/]")
        MODEL = SAVED_MODEL = DEFAULT_MODEL
        save_config()
    chat = pick_resume(app, resume) if resume is not None else None
    if resume is not None and chat is None:
        console.print("[yellow]No matching conversation; starting a new one.[/]")
    fresh = chat is None
    if fresh:
        chat = app.create_chat()
        if mode in CLI_MODES:
            chat["mode"] = mode
            chat["effort"] = "thinking" if mode == "normal" else "expert"
            app.save_chats()
    elif mode in CLI_MODES:
        chat["mode"] = mode
        chat["effort"] = "thinking" if mode == "normal" else "expert"
        app.save_chats()
    session = CliSession(app, web, chat)
    if not fresh and session.linked:
        session.sync_cwd()
    elif not fresh:
        session.sync_cwd()

    print_brand([f"{session.mode} mode · {'linked folder' if session.linked else 'private workspace'}",
                 f"conversation: {(chat.get('title') or 'New conversation')[:50]}"])
    if get_remote_model(MODEL):
        console.print(f"[dim green]☁ {escape(short_model(MODEL, 70))}[/]")
    else:
        if _cfg_is_first_scan():
            with console.status("Detecting your hardware (first run only)..."):
                get_hardware()
        else:
            get_hardware()
        if not ensure_ollama() or not ensure_model():
            sys.exit(1)
        retune()
        console.print(Text(tune_summary(), style="dim green"))
        threading.Thread(target=warm_model, kwargs={"quiet": True}, daemon=True).start()
    if SERVER_EXTERNAL and not TIP_SHOWN:
        console.print("[dim]Tip: /speed shows Ollama settings that can make it even faster.[/]")
        TIP_SHOWN = True
        save_config()
    if not sandbox_available():
        console.print("[yellow]Command execution needs macOS (Seatbelt sandbox). Reading, searching and file edits still work; commands will not run.[/]")
    if fresh:
        offer_link(session)
    console.print(f"[dim]{session.tag()} · /help for commands · Kurt asks before every command and file change[/]")

    ui = TerminalUI()
    ui.tag = session.tag()
    if not ui.enabled:
        console.print("[dim]/help · /mode · /models · @file[/]")
    if ui.start():
        TERMINAL_UI = ui
        ui.draw_footer(preserve_cursor=True)

    try:
        while True:
            CONTEXT_PCT = 0
            try:
                line = read_input().strip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                break
            if not line:
                continue
            if TERMINAL_UI and TERMINAL_UI.enabled:
                console.print()
                console.print(Text.assemble(("> ", "bold bright_green"), (TERMINAL_UI.last_display.strip(), "bold")))
            if line.startswith("/") and not web.SYSTEM_PROMPT_REQUEST_RE.fullmatch(line):
                try:
                    if handle_command(session, line) == "exit":
                        break
                except (KeyboardInterrupt, EOFError):
                    console.print("\n[yellow]cancelled[/]")
                except Exception as exc:
                    console.print(f"[red]{escape(str(exc))}[/]")
                continue
            if web.SYSTEM_PROMPT_REQUEST_RE.fullmatch(line):
                show_system_prompt(session)
                continue
            try:
                run_chat_turn(session, line, attach_mentions(line))
            except KeyboardInterrupt:
                console.print("\n[yellow]⏹ interrupted[/]")
    finally:
        if TERMINAL_UI and TERMINAL_UI.enabled:
            TERMINAL_UI.stop()
        TERMINAL_UI = None
        discard_empty(app, session.chat_id)


# -------------------------------------------------------------- Entry point ---
USAGE = """usage: kurt [--cli] [options]

  kurt                  open the browser interface on localhost (default)
  kurt --cli            use Kurt in this terminal instead

options:
  --port N              browser interface port (default 1277, or KURT_WEB_PORT)
  --resume [id|n]       (--cli) continue the latest or a chosen conversation
  --mode normal|agent   (--cli) mode for the new or resumed conversation
  -v, --version         show the version
  -h, --help            show this help"""


def main():
    argv = sys.argv[1:]
    if any(a in ("-v", "--version") for a in argv):
        print(f"kurt {__version__}")
        return
    if any(a in ("-h", "--help") for a in argv):
        print(__doc__)
        print(USAGE)
        print("\nEnv: KURT_MODEL, KURT_CTX, KURT_THREADS, KURT_BATCH, KURT_THINK, KURT_TUNE,")
        print("     KURT_WEB_PORT, KURT_WEB_FALLBACK_PORT, KURT_MAX_AGENTS, OLLAMA_HOST")
        return
    cli, resume, mode, port = False, None, None, None
    i = 0
    while i < len(argv):
        arg = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        if arg == "--cli":
            cli = True
        elif arg in ("--localhost", "--web"):
            pass
        elif arg == "--resume":
            if nxt is not None and not nxt.startswith("-"):
                resume, i = nxt, i + 1
            else:
                resume = "latest"
        elif arg == "--mode" and nxt in CLI_MODES:
            mode, i = nxt, i + 1
        elif arg == "--port" and nxt and nxt.isdigit():
            port, i = nxt, i + 1
        else:
            print(USAGE)
            sys.exit(2)
        i += 1
    if (resume is not None or mode is not None) and not cli:
        print("--resume and --mode only apply to --cli.\n")
        print(USAGE)
        sys.exit(2)
    sys.modules.setdefault("kurt", sys.modules[__name__])
    if not cli:
        if port:
            os.environ["KURT_WEB_PORT"] = port
        try:
            from kurt_web import launch_web
            launch_web()
        except RuntimeError as exc:
            print(f"kurt: {exc}")
            sys.exit(1)
        return
    cli_main(resume=resume, mode=mode)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        sys.exit(130)