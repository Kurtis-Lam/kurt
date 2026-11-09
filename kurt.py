#!/usr/bin/env python3
"""kurt - a terminal coding assistant powered by local models (Ollama) and remote provider APIs.

Run `kurt` in any project folder. Kurt can read your files and propose
edits / new files / shell commands. Nothing is changed without your OK.
"""
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
from rich.table import Table
from rich.text import Text

TERMINAL_LOCK = threading.RLock()


class KurtConsole(Console):
    def print(self, *objects, **kwargs):
        with TERMINAL_LOCK:
            return super().print(*objects, **kwargs)


__version__ = "0.4.0"

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
    HINTS = "/help · /models · /keys · @ files"
    HINTS_BUSY = "Esc to interrupt"
    PLACEHOLDER = "Message Kurt…"
    PLACEHOLDER_BUSY = "Working… press Esc to interrupt"
    HEIGHT = 5  # context row, input frame, and compact command hints
    PICKER_VISIBLE = 5

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
        return f"in {short_path(target, max(12, min(42, w_safe_width(self.width))))}  ·  {activity}{meter}"

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


def print_brand():
    mascot = Text("\n".join(MASCOT), style=MASCOT_STYLE, no_wrap=True)
    path_limit = max(16, console.width - len(MASCOT[0]) - 14)
    details = Group(
        Text("Kurt", style="bold " + MASCOT_STYLE),
        Text("Your local coding companion", style="bold"),
        Text(short_model(MODEL), style="green"),
        Text(short_path(Path.cwd(), path_limit), style="dim", no_wrap=True),
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


def stream_chat(messages):
    remote = get_remote_model(MODEL)
    if remote:
        yield from PROVIDER_CLIENT.stream_chat(remote, messages, PROVIDER_KEYS)
        return
    payload = _chat_payload(
        MODEL, messages, True, 0.2, think_enabled(), stop=STOP_SEQUENCES
    )
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


def provider_keys_menu():
    buf = ""
    note = ""
    prev_lines = 0
    while True:
        table = Table(box=None, header_style="bold", padding=(0, 2))
        table.add_column("#", justify="right", no_wrap=True)
        table.add_column("Provider", no_wrap=True)
        table.add_column("Pool", no_wrap=True)
        table.add_column("Account", overflow="fold")
        table.add_column("Saved key", no_wrap=True)
        if not PROVIDER_KEYS:
            table.add_row("—", "No API keys configured", "", "", "")
        for i, item in enumerate(PROVIDER_KEYS, 1):
            table.add_row(
                str(i), provider_label(item.get("provider")),
                str(item.get("group") or "default"),
                str(item.get("label") or "Account"), mask_key(item.get("value")),
            )
        actions = Table.grid(padding=(0, 2))
        actions.add_column(style="bold", no_wrap=True)
        actions.add_column()
        actions.add_row("+", "add a provider API key (hidden input)")
        actions.add_row("e + number", "edit the account label, pool, or key")
        actions.add_row("d + number", "remove a key from its pool")
        actions.add_row("Enter / q", "close")
        try:
            interactive = sys.stdin.isatty()
        except Exception:
            interactive = False
        with console.capture() as cap:
            console.print(Panel(table, title="API keys · local storage", border_style="cyan", expand=False))
            console.print(actions)
            if note:
                console.print(f"[dim]{note}[/]")
        frame = cap.get()
        note = ""
        if interactive and prev_lines:
            sys.stdout.write(f"\x1b[{prev_lines}A\r\x1b[J")
        console.file.write(frame)
        console.file.flush()
        if not interactive:
            return
        console.print(f"[bold]›[/] {escape(buf)}", end="")
        prev_lines = frame.count("\n") + 1
        key = read_menu_key()
        console.print()
        if key is None or key == "ctrl-c":
            return
        if key == "enter":
            cmd, buf = buf.lower(), ""
            match = re.fullmatch(r"([ed])\s*(\d+)", cmd)
            if match:
                idx = int(match.group(2))
                prev_lines = 0
                if match.group(1) == "e":
                    edit_provider_key_flow(idx)
                else:
                    delete_provider_key_flow(idx)
            elif not cmd:
                return
            else:
                note = "Use +, e<number>, or d<number>."
            continue
        if key == "backspace":
            buf = buf[:-1]
        elif key == "esc":
            if buf:
                buf = ""
            else:
                return
        elif key == "+" and not buf:
            prev_lines = 0
            add_provider_key_flow()
        elif isinstance(key, str) and len(key) == 1 and key.isalnum():
            if not buf and key.lower() == "q":
                return
            buf += key.lower()


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
    global AGENTS, WORKERS, MODEL_SORT, MODEL_SORT_DESC
    if shutil.which("ollama"):
        ensure_ollama()
    local_models = installed_models() if ollama_up() else []
    buf = ""
    note = ""
    prev_lines = 0
    while True:
        local = sort_models(local_models, MODEL_SORT, MODEL_SORT_DESC)
        entries = [
            {"ref": item["name"], "name": item["name"], "provider": "Ollama", "params": item["params"],
             "size": item["size"], "remote": False, "item": item}
            for item in local
        ]
        for item in sorted(REMOTE_MODELS, key=lambda m: (m.get("provider", ""), m.get("label") or m.get("model", ""))):
            entries.append({
                "ref": remote_model_ref(item), "name": item.get("label") or item.get("model"),
                "provider": provider_label(item.get("provider")), "params": "API", "size": 0,
                "remote": True, "item": item,
            })
        arrow = " ▼" if MODEL_SORT_DESC else " ▲"

        def head(key, label):
            return label + (arrow if MODEL_SORT == key else "")

        table = Table(box=None, header_style="bold", padding=(0, 2))
        table.add_column("#", justify="right", no_wrap=True)
        table.add_column(head("name", "Model"), overflow="fold")
        table.add_column("Provider", no_wrap=True)
        table.add_column(head("params", "Type"), justify="right", no_wrap=True)
        table.add_column(head("size", "Size"), justify="right", no_wrap=True)
        table.add_column("Role", no_wrap=True)
        for i, item in enumerate(entries, 1):
            role = []
            if item["ref"] == MODEL:
                role.append("● main")
            if item["ref"] in WORKERS:
                role.append("agent worker")
            table.add_row(
                str(i), Text(str(item["name"])), item["provider"], item["params"],
                "API" if item["remote"] else (fmt_size(item["size"]) or "-"), ", ".join(role),
            )

        keys = Table.grid(padding=(0, 2))
        keys.add_column(style="bold", no_wrap=True)
        keys.add_column()
        keys.add_row("number + Enter", "use as main model")
        keys.add_row("+", "add a local model from Hugging Face / Ollama")
        keys.add_row("m", "add a remote API model")
        keys.add_row("e + number", "edit an API model's ID, name, key pool, or endpoint")
        keys.add_row("d + number", "remove an API model or uninstall a local model")
        keys.add_row("k", "manage provider API keys and key pools")
        keys.add_row("g", f"toggle agents (now {'ON' if AGENTS else 'OFF'})")
        keys.add_row("w + number + Enter", "toggle a model as agent worker")
        keys.add_row("s  or  ← →", f"sort local models (now {SORT_LABELS[MODEL_SORT]})")
        keys.add_row("r  or  ↑ ↓", "reverse the local model sort order")
        keys.add_row("Enter / q", "close")
        try:
            interactive = sys.stdin.isatty()
        except Exception:
            interactive = False
        with console.capture() as cap:
            console.print(Panel(table, title="Models · local + API", border_style="magenta", expand=False))
            console.print(keys)
            if note:
                console.print(f"[dim]{note}[/]")
        frame = cap.get()
        note = ""
        if interactive and prev_lines:
            sys.stdout.write(f"\x1b[{prev_lines}A\r\x1b[J")
        console.file.write(frame)
        console.file.flush()
        if not interactive:
            return
        console.print(f"[bold]›[/] {escape(buf)}", end="")
        prev_lines = frame.count("\n") + 1
        key = read_menu_key()
        console.print()
        if key is None or key == "ctrl-c":
            return
        if key == "enter":
            cmd, buf = buf.lower(), ""
            mw = re.fullmatch(r"w\s*(\d+)", cmd)
            md = re.fullmatch(r"d\s*(\d+)", cmd)
            me = re.fullmatch(r"e\s*(\d+)", cmd)
            if mw:
                idx = int(mw.group(1))
                if 1 <= idx <= len(entries):
                    reference = entries[idx - 1]["ref"]
                    if reference in WORKERS:
                        WORKERS = [worker for worker in WORKERS if worker != reference]
                        note = f"removed worker {escape(entries[idx - 1]['name'])}"
                    else:
                        WORKERS.append(reference)
                        note = f"added worker {escape(entries[idx - 1]['name'])}"
                    save_config()
                else:
                    note = "[red]no such model number[/]"
            elif md:
                idx = int(md.group(1))
                if 1 <= idx <= len(entries):
                    entry = dict(entries[idx - 1])
                    entry["alternatives"] = entries[:idx - 1] + entries[idx:]
                    prev_lines = 0
                    note = remove_model_entry(entry)
                    local_models = installed_models() if ollama_up() else []
                else:
                    note = "[red]no such model number[/]"
            elif me:
                idx = int(me.group(1))
                if 1 <= idx <= len(entries):
                    prev_lines = 0
                    note = edit_remote_model_flow(entries[idx - 1])
                else:
                    note = "[red]no such model number[/]"
            elif cmd.isdigit():
                idx = int(cmd)
                if 1 <= idx <= len(entries):
                    prev_lines = 0
                    set_model(entries[idx - 1]["ref"])
                else:
                    note = "[red]no such model number[/]"
            elif not cmd:
                return
            else:
                note = "[red]not an option[/]"
            continue
        if key == "backspace":
            buf = buf[:-1]
            continue
        if key == "esc":
            if buf:
                buf = ""
                continue
            return
        if key in ("right", "left") and not buf:
            i = MENU_SORT_ORDER.index(MODEL_SORT) if MODEL_SORT in MENU_SORT_ORDER else 0
            MODEL_SORT = MENU_SORT_ORDER[(i + (1 if key == "right" else -1)) % len(MENU_SORT_ORDER)]
            MODEL_SORT_DESC = SORT_KEYS[MODEL_SORT][1]
            save_config()
            continue
        if key in ("up", "down") and not buf:
            MODEL_SORT_DESC = not MODEL_SORT_DESC
            save_config()
            continue
        if isinstance(key, str) and len(key) == 1 and key.isalnum():
            if not buf:
                k = key.lower()
                if k == "q":
                    return
                if k == "s":
                    i = MENU_SORT_ORDER.index(MODEL_SORT) if MODEL_SORT in MENU_SORT_ORDER else 0
                    MODEL_SORT = MENU_SORT_ORDER[(i + 1) % len(MENU_SORT_ORDER)]
                    MODEL_SORT_DESC = SORT_KEYS[MODEL_SORT][1]
                    save_config()
                    continue
                if k == "l":
                    prev_lines = 0
                    add_model_flow()
                    local_models = installed_models() if ollama_up() else []
                    continue
                if k == "m":
                    prev_lines = 0
                    add_remote_model_flow()
                    continue
                if k == "k":
                    prev_lines = 0
                    provider_keys_menu()
                    continue
                if k == "g":
                    AGENTS = not AGENTS
                    save_config()
                    note = f"agents: {'ON' if AGENTS else 'OFF'}"
                    if AGENTS and not WORKERS:
                        note += " · no worker models chosen, helpers use the main model"
                    continue
                if k == "r":
                    MODEL_SORT_DESC = not MODEL_SORT_DESC
                    save_config()
                    continue
                if k == "w":
                    buf = "w"
                    continue
                if k == "d":
                    buf = "d"
                    continue
            buf += key.lower()
        elif key == "+" and not buf:
            prev_lines = 0
            add_model_flow()
            local_models = installed_models() if ollama_up() else []


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


# ----------------------------------------------------------------- Agents ---
PLANNER = """You are the orchestrator of a coding assistant. Decide whether the user's request
below is worth splitting between helper agents that run IN PARALLEL (for example: analysing
different files, investigating separate sub-problems, reviewing different parts).

Helpers can only read the files you list for them. They cannot edit files or run commands.
Use 0 agents for simple or small requests. Use at most {max_agents} agents, and only as many
as are truly needed. Each task must be self-contained and independent.

Reply with ONLY a JSON object, no other text:
{{"agents": [{{"task": "what this helper should find out or produce", "files": ["relative/path"]}}]}}
or, when no helpers are needed:
{{"agents": []}}

Project files:
{tree}

User request:
{request}
"""

WORKER_SYSTEM = (
    "You are a helper agent inside a coding assistant. Complete the subtask you are given "
    "thoroughly but concisely. You cannot edit files or run commands. Never use @@@ blocks. "
    "Reply with your findings and, when useful, exact code snippets."
)


def plan_agents(request):
    prompt = PLANNER.format(
        max_agents=MAX_AGENTS, tree=project_tree(80), request=request[:6000]
    )
    try:
        out = chat_once(
            MODEL, [{"role": "user", "content": prompt}], temperature=0, timeout=300, think=False
        )
    except Exception as e:
        console.print(f"[dim]agents: planning failed ({escape(str(e))}), continuing alone[/]")
        return []
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    raw = data.get("agents") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    agents = []
    for a in raw[:MAX_AGENTS]:
        if isinstance(a, dict) and isinstance(a.get("task"), str) and a["task"].strip():
            files = a.get("files") if isinstance(a.get("files"), list) else []
            files = [f for f in files if isinstance(f, str)]
            agents.append({"task": a["task"].strip(), "files": files[:5]})
    return agents


def effective_workers():
    names = [m["name"] for m in installed_models()] if ollama_up() else []
    names.extend(remote_model_ref(item) for item in REMOTE_MODELS)
    ws = [w for w in WORKERS if any(same_model(w, n) for n in names)]
    return ws or [MODEL]


def run_agents(request):
    """Let the main model decide how many helpers to spawn, run them in parallel,
    and return their reports as text to append to the user's message."""
    with console.status("[bold]🤖 Kurt is deciding how many agents are needed...[/]"):
        tasks = plan_agents(request)
    if not tasks:
        console.print("[dim]🤖 agents: Kurt handled this alone[/]")
        return ""

    n = len(tasks)
    workers = effective_workers()
    assigned = [workers[i % len(workers)] for i in range(n)]
    plan = Text()
    for i, (t, w) in enumerate(zip(tasks, assigned), 1):
        plan.append(f"{i}. ", style="bold")
        plan.append(f"[{short_model(w, 30)}] ", style="cyan")
        plan.append(t["task"] + "\n")
    plan.rstrip()
    console.print(
        Panel(plan, title=f"Agents · {n} working in parallel", border_style="blue", expand=False)
    )

    results = [None] * n

    def work(i):
        t = tasks[i]
        attached = []
        for f in t["files"]:
            p = safe_path(f)
            if p and p.is_file():
                text, _ = read_file(p)
                attached.append(f"[File {f}]\n{text[: file_cap() * 2 // 3]}")
        user = (
            f"Overall user request:\n{request}\n\nYour subtask:\n{t['task']}\n\n"
            + "\n\n".join(attached)
        )
        try:
            results[i] = chat_once(
                assigned[i],
                [
                    {"role": "system", "content": WORKER_SYSTEM},
                    {"role": "user", "content": user},
                ],
            ) or "(no answer)"
        except Exception as e:
            results[i] = f"(agent failed: {e})"

    threads = [threading.Thread(target=work, args=(i,), daemon=True) for i in range(n)]
    for th in threads:
        th.start()
    with console.status(f"[bold]🤖 0/{n} agents finished...[/]") as status:
        while any(th.is_alive() for th in threads):
            done = sum(1 for r in results if r is not None)
            status.update(f"[bold]🤖 {done}/{n} agents finished...[/]")
            time.sleep(0.2)

    parts = []
    for i, (t, rep) in enumerate(zip(tasks, results), 1):
        rep = rep.replace("@@@", "@ @ @")
        lines = rep.splitlines()
        preview = "\n".join(lines[:8]) + ("\n…" if len(lines) > 8 else "")
        console.print(
            Panel(
                Text(preview, style="dim"),
                title=f"Agent {i} · {short_model(assigned[i - 1], 30)}",
                border_style="blue",
                expand=False,
            )
        )
        parts.append(f"### Agent {i} - {t['task']}\n{rep[:6000]}")
    return (
        f"\n\n[Helper agent reports - {n} helper agent(s) ran in parallel on parts of this "
        "request. Use their findings, verify against the files, and make any changes "
        "yourself with action blocks.]\n" + "\n\n".join(parts)
    )


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


def context_chars(messages):
    return sum(len(m["content"]) for m in messages)


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


SAFE_VIEW_COMMANDS = {
    "ls", "dir", "cat", "type", "head", "tail", "pwd", "wc", "grep", "egrep", "fgrep", "rg",
    "find", "tree", "stat", "file", "du", "df", "which", "where", "whoami", "date", "echo",
    "printf", "sort", "uniq", "cut", "tr", "diff", "cmp", "realpath", "basename", "dirname",
    "findstr", "mkdir", "touch",
}
SAFE_GIT = {"status", "log", "diff", "show", "ls-files", "blame", "rev-parse", "describe"}
VERSION_FLAGS = {"--version", "-v", "-V", "version"}


def _escapes_project(tok):
    """True when an argument names a path outside the project folder."""
    if tok.startswith("-") or not any(c in tok for c in ("/", "\\", "~")) and ".." not in tok:
        return False
    try:
        p = Path(os.path.expanduser(tok))
        full = (p if p.is_absolute() else Path.cwd() / p).resolve()
        full.relative_to(Path.cwd().resolve())
        return False
    except Exception:
        return True


def is_safe_command(cmd):
    """True for commands that only look at things (or create empty files/folders)."""
    import shlex

    if re.search(r"[;&`><\n]|\$\(|\|\|", cmd):
        return False
    for seg in cmd.split("|"):
        try:
            toks = shlex.split(seg, posix=(os.name != "nt"))
        except ValueError:
            return False
        if not toks:
            return False
        name = Path(toks[0]).name.lower()
        if name.endswith(".exe"):
            name = name[:-4]
        args = toks[1:]
        if name in SAFE_VIEW_COMMANDS or name == "git":
            if any(_escapes_project(x) for x in args):
                return False
        if name in SAFE_VIEW_COMMANDS:
            if name == "find" and any(
                x in ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fls") or x.startswith("-fprint")
                for x in args
            ):
                return False
            if name == "tree" and any(x == "-o" for x in args):
                return False
            if name in ("rg", "grep", "egrep", "fgrep") and any(
                x.startswith(("--pre", "--hostname-bin")) for x in args
            ):
                return False
            if name == "uniq" and len([x for x in args if not x.startswith("-")]) > 1:
                return False
            if name == "date" and any(x == "-s" or x.startswith("--set") for x in args):
                return False
            if name == "sort" and any(x == "-o" or x.startswith("--output") for x in args):
                return False
            if name == "tail" and any(x in ("-f", "-F", "--follow") for x in args):
                return False
            continue
        if name == "git":
            sub = [x for x in args if not x.startswith("-")]
            if any(x.startswith("--output") for x in args):
                return False
            if sub and sub[0] in SAFE_GIT:
                continue
            return False
        if name in ("python", "python3", "py", "node", "npm", "pip", "pip3") and len(args) == 1 and args[0] in VERSION_FLAGS:
            continue
        return False
    return True


def confirm(question):
    global AUTO
    if AUTO:
        return True
    a = Prompt.ask(question, choices=["y", "n", "a"], default="y", console=console)
    if a == "a":
        AUTO = True
        console.print("[dim]Auto-accepting edits for this session (/auto to toggle).[/]")
        return True
    return a == "y"


# ---------------------------------------------------------------- Actions ---
def do_read(arg):
    rng = re.fullmatch(r"(.+?):(\d+)-(\d+)", arg)
    name = rng.group(1) if rng else arg
    p = safe_path(name)
    if not p or not p.is_file():
        return f"READ {arg}: ERROR file not found (or outside project)."
    text, _ = read_file(p)
    if looks_binary(text):
        return f"READ {arg}: ERROR this looks like a binary file, so it was not read."
    cap = file_cap()
    if rng:
        lines = text.split("\n")
        a = max(1, int(rng.group(2)))
        b = min(len(lines), max(a, int(rng.group(3))))
        chunk = "\n".join(lines[a - 1:b])
        if len(chunk) > cap:
            chunk = chunk[:cap].rsplit("\n", 1)[0]
            b = a + chunk.count("\n")
        console.print(f"[dim]📖 read {name} lines {a}-{b} of {len(lines)}[/]")
        return f"READ {name} (lines {a}-{b} of {len(lines)}):\n{chunk}"
    console.print(f"[dim]📖 read {name} ({len(text)} chars)[/]")
    return f"READ {name}:\n{clip_text(text, name, cap)}"


def do_edit(arg, body):
    p = safe_path(arg)
    if not p or not p.is_file():
        return f"EDIT {arg}: ERROR file not found (or outside project). Use WRITE to create files."
    pairs = PAIR_RE.findall(body)
    if not pairs:
        return f"EDIT {arg}: ERROR no valid SEARCH/REPLACE pairs found."
    old, crlf = read_file(p)
    new = old
    loose = set()
    for i, (s, r) in enumerate(pairs, 1):
        start, end, how, fix = find_span(new, s)
        if start is None and how == "many":
            return f"EDIT {arg}: ERROR SEARCH #{i} matches several places. Include more context to make it unique."
        if start is None:
            return (f"EDIT {arg}: ERROR SEARCH #{i} not found. It must match the file exactly. "
                    "Re-READ the file." + closest_snippet(new, s))
        if how not in ("exact", "trimmed"):
            loose.add(how)
            r = reindent(r, fix)
        new = new[:start] + r + new[end:]
    if new == old:
        return f"EDIT {arg}: ERROR the replacement is identical to the existing code, nothing to change."
    if loose:
        console.print(f"[dim]≈ matched ignoring differences in {' and '.join(sorted(loose))}[/]")
    console.print(diff_panel(old, new, arg, "Edit"))
    if not confirm("Apply this change? (y=yes, n=no, a=yes to all)"):
        return f"EDIT {arg}: REJECTED by user. Do not propose this change again; ask what they would prefer."
    broken_before = check_syntax(p, old)
    UNDO.append((p, old, crlf))
    write_file(p, new, crlf)
    console.print(f"[green]✔ edited {arg}[/]")
    err = "" if broken_before else check_syntax(p, new)
    if err:
        console.print(f"[yellow]⚠ {escape(arg)} no longer parses: {escape(err)}[/]")
        return f"EDIT {arg}: applied, but WARNING: the file now has a syntax error: {err}. Fix it."
    return f"EDIT {arg}: applied."


def do_write(arg, body):
    p = safe_path(arg)
    if not p:
        return f"WRITE {arg}: ERROR path outside project."
    if p.is_dir():
        return f"WRITE {arg}: ERROR that path is a folder."
    exists = p.is_file()
    old, crlf = read_file(p) if exists else ("", False)
    new = body if body.endswith("\n") else body + "\n"
    if exists:
        console.print(diff_panel(old, new, arg, "Overwrite"))
        if not confirm("Overwrite this file? (y=yes, n=no, a=yes to all)"):
            return f"WRITE {arg}: REJECTED by user. Do not propose this change again; ask what they would prefer."
    else:
        console.print(new_file_panel(arg, new))
    broken_before = check_syntax(p, old) if exists else ""
    UNDO.append((p, old if exists else None, crlf))
    write_file(p, new, crlf)
    console.print(f"[green]✔ {'overwrote' if exists else 'created'} {escape(arg)}[/]")
    err = "" if broken_before else check_syntax(p, new)
    if err:
        console.print(f"[yellow]⚠ {escape(arg)} does not parse: {escape(err)}[/]")
        return f"WRITE {arg}: applied, but WARNING: the file has a syntax error: {err}. Fix it."
    return f"WRITE {arg}: applied."


def do_run(cmd):
    from rich.syntax import Syntax

    cmd = cmd.strip()
    safe = is_safe_command(cmd)
    console.print(Panel(
        Syntax(cmd, "bash", theme="ansi_dark", word_wrap=True, background_color="default"),
        title="[bold]Run command[/]",
        subtitle="view-only · running automatically" if safe else "can change things · needs your OK",
        subtitle_align="right",
        border_style="green" if safe else "red",
    ))
    if not safe and Prompt.ask("Run it?", choices=["y", "n"], default="n", console=console) != "y":
        return f"RUN {cmd}: REJECTED by user."
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=180,
            cwd=Path.cwd(), errors="replace",
        )
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        code = r.returncode
    except subprocess.TimeoutExpired:
        out, code = "timed out after 180s", -1
    except KeyboardInterrupt:
        out, code = "interrupted by the user", -2
    shown = out[:3000]
    if len(out) > 3000:
        shown += "\n… (output truncated)"
    console.print(Panel(
        Text(shown or "(no output)", style="" if shown else "dim"),
        title="[bold]Output[/]", subtitle=f"exit {code}", subtitle_align="right",
        border_style="dim" if code == 0 else "red",
    ))
    if len(out) > 4000:
        out = out[:4000] + "\n... [truncated]"
    return f"RUN {cmd}: exit code {code}\n{out}"


PENDING_NOTES = []  # rejections the model has not heard about yet; sent with the next message


def _outcome(result):
    """Classify an EDIT/WRITE result: 'error', 'rejected', 'warning' or 'ok'."""
    tail = result.split(": ", 1)[-1]
    if tail.startswith("ERROR"):
        return "error"
    if tail.startswith("REJECTED"):
        return "rejected"
    return "warning" if "WARNING:" in tail else "ok"


def handle_actions(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    results, rejected = [], []
    for kind, arg, body in BLOCK_RE.findall(text):
        arg = arg.strip()
        try:
            if kind == "READ":
                results.append(do_read(arg))
            elif kind in ("EDIT", "WRITE"):
                r = do_edit(arg, body) if kind == "EDIT" else do_write(arg, body)
                state = _outcome(r)
                if state == "rejected":
                    rejected.append(r)
                elif state != "ok":
                    if state == "error":
                        console.print(f"[red]{escape(r)}[/]")
                    results.append(r)
            elif kind == "RUN":
                results.append(do_run(arg or body))
        except OSError as e:
            results.append(f"{kind} {arg}: ERROR {e}")
            console.print(f"[red]{escape(results[-1])}[/]")
    if not results:
        PENDING_NOTES.extend(rejected)  # no follow-up needed now; the model hears it next turn
        return ""
    return "[tool results]\n" + "\n\n".join(results + rejected)


# ------------------------------------------------------------------- Chat ---
def trim(messages):
    """Keep the conversation inside the context window.

    When over budget, drop old messages down to 70% of it in ONE go. Ollama reuses its cached
    prompt as long as the start of the conversation is unchanged, so trimming rarely (instead of
    shaving one message every turn) avoids re-reading the whole prompt on every request."""
    budget = context_budget()  # ~3 chars per token, leave headroom
    total = context_chars(messages)
    if total <= budget:
        return
    target = int(budget * 0.7)
    cut = 1
    while total > target and len(messages) - (cut - 1) > 3:
        total -= len(messages[cut]["content"])
        cut += 1
    dropped = cut - 1
    del messages[1:cut]
    console.print(f"[dim]↻ dropped {dropped} old messages to fit the context window "
                  "(/compact summarises instead of forgetting)[/]")


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


def early_stop_point(full, state):
    """After the model asks to READ a file or RUN a command it must wait for the result.
    If it keeps writing instead (guessing what the file contains), stop generating and return
    the index where the reply should be cut; otherwise None."""
    end = full.rfind("@@@ END")
    if end < 0:
        return None
    cut = end + len("@@@ END")
    tail = full[cut:]
    if len(tail) < 160 or "@@@" in tail:
        return None
    if state.get("cut") != cut:
        blocks = BLOCK_RE.findall(full[:cut])
        state["cut"] = cut
        state["fetch"] = bool(blocks) and blocks[-1][0] in ("READ", "RUN")
    return cut if state["fetch"] else None




def run_turn(messages):
    for _ in range(MAX_ROUNDS):
        trim(messages)
        full, interrupted = "", False
        thinking_started = False
        answer_started = False
        meter = Meter()
        ui = TERMINAL_UI if TERMINAL_UI and TERMINAL_UI.enabled else None
        console.print()
        if ui:
            ui.set_busy(True)
            ui.begin_work(meter)
        else:
            console.print(Text("✦ K is thinking…", style="bold bright_green"))
        failed = False
        printer = StreamPrinter()
        stop_state = {}
        afilter = ActionFilter()
        try:
            with BusyListener(), contextlib.closing(stream_chat(messages)) as stream:
                for kind, chunk in stream:
                    if kind == "stats":
                        meter.tokens = chunk.get("eval_count") or meter.tokens
                        meter.gen_secs = (chunk.get("eval_duration") or 0) / 1e9
                        meter.wait_secs = (
                            (chunk.get("load_duration") or 0)
                            + (chunk.get("prompt_eval_duration") or 0)
                        ) / 1e9
                        continue
                    meter.tokens += 1  # live estimate: one streamed chunk is about one token
                    if kind == "thinking":
                        meter.label = "Thinking"
                        if not thinking_started:
                            printer.flush()
                            console.print(Text("Thinking", style="bold green"))
                            thinking_started = True
                        printer.write(chunk, "dim green")
                    else:
                        meter.label = "Preparing action" if afilter.in_block else "Writing"
                        if not answer_started:
                            printer.flush()
                            if thinking_started:
                                console.print()
                            console.print(Text("K › ", style="bold bright_green"), end="")
                            answer_started = True
                        full += chunk
                        visible = afilter.feed(chunk)
                        if visible:
                            printer.write(visible)
                        cut = early_stop_point(full, stop_state)
                        if cut is not None:
                            full = full[:cut]  # closing the stream makes Ollama stop generating
                            break
        except KeyboardInterrupt:
            interrupted = True
            printer.flush()
            console.print("\n[yellow]⏹ interrupted[/]")
        except Exception as e:
            failed = True
            printer.flush()
            console.print(f"\n[red]Error talking to the model:[/] {escape(str(e))}")
        finally:
            printer.flush()
            meter.stop()
            if ui:
                ui.end_work()
                ui.set_busy(False)
        if failed:
            if messages and messages[-1]["role"] == "user":
                messages.pop()
            return
        console.print()
        print_summary(meter)
        messages.append({"role": "assistant", "content": full})
        if interrupted:
            return
        feedback = handle_actions(full)
        if not feedback:
            return
        messages.append({"role": "user", "content": feedback})
    console.print("[yellow]Stopped after several automatic steps. Say 'continue' to go on.[/]")


HELP = """[bold]Commands[/]
  /help           show this help
  /models         switch, add or remove local and API models; configure agents
  /keys           add, edit, group, or remove provider API keys
  /speed          show the detected hardware and the speed settings in use
                  (/speed refresh re-detects it)
  /think [on|off] turn thinking on or off (off is much faster on computers without a GPU)
  /fast [model]   switch to a small, much faster model (default qwen2.5-coder:3b)
  /files          list project files
  /add <path>     put a file's contents into the conversation (or use @path in a message)
  /auto           toggle auto-accept for edits (new files and view-only commands never ask)
  /undo           revert the last applied change
  /context        show how full the model's context window is
  /compact        summarise the conversation so far to free up context
  /clear          forget the conversation
  /exit           quit  (Ctrl+C while Kurt is typing stops generation)

[bold]Tip[/] put standing instructions for a project in a KURT.md file in its root folder."""


def new_messages(announce=False):
    system = SYSTEM.replace("{cwd}", str(Path.cwd()))
    notes = project_notes()
    if notes:
        system += "\nProject notes from KURT.md (the user wrote these; follow them):\n" + notes + "\n"
        if announce:
            console.print("[dim]📌 loaded project notes from KURT.md[/]")
    return [{"role": "system", "content": system}]


def context_report(messages):
    used, budget = context_chars(messages), context_budget()
    pct = context_percent(messages)
    style = "green" if pct < 70 else "yellow" if pct < 90 else "red"
    console.print(
        f"context: [{style}]{pct}%[/] full · about {used // 3:,} of {budget // 3:,} tokens · "
        f"{len(messages)} messages"
    )
    if pct >= 70:
        console.print("[dim]Running low: /compact summarises the chat, /clear starts fresh.[/]")


def compact_conversation(messages):
    """Replace the middle of the conversation with a model-written summary."""
    tail = 2 if len(messages) >= 4 and messages[-2]["role"] == "user" else 1
    middle = messages[1:len(messages) - tail]
    if len(middle) < 4:
        console.print("[dim]nothing worth compacting yet[/]")
        return
    before = context_chars(messages)
    parts = []
    for m in middle:
        who = "User" if m["role"] == "user" else "Kurt"
        parts.append(f"{who}: {m['content'][:1500]}")
    transcript = "\n\n".join(parts)[-max(6000, CTX * 2):]
    prompt = (
        "Summarise this coding session so the assistant can carry on without the full history. "
        "List: the user's goals, files that were read or changed (and how), decisions made, "
        "problems still open, and any facts worth remembering. Plain text, under 300 words. "
        "Do not use @@@ blocks.\n\n" + transcript
    )
    try:
        with console.status("Summarising the conversation..."):
            summary = chat_once(MODEL, [{"role": "user", "content": prompt}], temperature=0.1,
                                timeout=600, think=False)
    except Exception as e:
        console.print(f"[red]Could not summarise:[/] {escape(str(e))}")
        return
    summary = summary.replace("@@@", "@ @ @")
    if not summary:
        console.print("[red]The model returned an empty summary; nothing was changed.[/]")
        return
    head = messages[0]
    rest = messages[len(messages) - tail:]
    messages[:] = [
        head,
        {"role": "user", "content": f"[Summary of the earlier conversation]\n{summary}\n\n"
                                    f"Files in this project:\n{project_tree()}"},
        {"role": "assistant", "content": "Understood. I'll continue from that summary."},
    ] + rest
    after = context_chars(messages)
    console.print(f"[green]✔ compacted[/] {before // 3:,} → {after // 3:,} tokens "
                  f"({context_percent(messages)}% full)")


def handle_command(line, messages):
    global AUTO, THINK
    cmd, _, arg = line.partition(" ")
    cmd = cmd.lower()
    if cmd in ("/exit", "/quit", "/q"):
        return "exit"
    if cmd == "/help":
        console.print(HELP)
    elif cmd == "/models":
        models_menu()
    elif cmd == "/keys":
        provider_keys_menu()
    elif cmd == "/speed":
        speed_report(refresh=arg.strip().lower() == "refresh")
    elif cmd == "/fast":
        name = arg.strip() or FAST_MODEL
        if not any(same_model(m["name"], name) for m in installed_models()):
            console.print(f"[bold]Downloading[/] {escape(name)} (small, one time) ...")
            if not pull_model(name):
                console.print("[red]Download failed.[/] Check the name and your connection.")
                return None
        set_model(name)
        console.print("[dim]Use /models to go back to the bigger model.[/]")
    elif cmd == "/think":
        a = arg.strip().lower()
        if a in ("on", "off"):
            THINK = a == "on"
        elif a in ("auto", "default"):
            THINK = None
        elif a:
            console.print("usage: /think [on|off|auto]", markup=False)
            return None
        else:
            THINK = not think_enabled()
        save_config()
        retune()
        console.print(f"thinking: [bold]{'ON' if think_enabled() else 'OFF'}[/]"
                      + (" [dim](auto)[/]" if THINK is None else ""))
    elif cmd == "/files":
        console.print(project_tree(), markup=False)
    elif cmd == "/context":
        context_report(messages)
    elif cmd == "/compact":
        compact_conversation(messages)
    elif cmd == "/clear":
        messages[:] = new_messages(announce=True)
        PENDING_NOTES.clear()
        console.print("[dim]conversation cleared[/]")
    elif cmd == "/auto":
        AUTO = not AUTO
        console.print(f"auto-accept edits: [bold]{'ON' if AUTO else 'OFF'}[/]")
    elif cmd == "/add":
        p = safe_path(arg)
        if not p or not p.is_file():
            console.print("[red]file not found[/]")
        else:
            text, _ = read_file(p)
            if looks_binary(text):
                console.print("[red]that looks like a binary file[/]")
                return None
            messages.append({"role": "user", "content": f"[File {arg}]\n{clip_text(text, arg.strip(), file_cap())}"})
            messages.append({"role": "assistant", "content": f"Got it, I have {arg}."})
            console.print(f"[dim]added {arg}[/]")
    elif cmd == "/undo":
        if not UNDO:
            console.print("[dim]nothing to undo[/]")
        else:
            p, old, crlf = UNDO.pop()
            if old is None:
                p.unlink(missing_ok=True)
            else:
                write_file(p, old, crlf)
            console.print(f"[green]↩ reverted {p.name}[/]")
    else:
        console.print("[red]unknown command[/] — try /help")
    return None



# --------------------------------------------------------------- Input box ---
def read_input():
    """Read from Kurt's fixed bottom input box when the terminal supports it."""
    if TERMINAL_UI and TERMINAL_UI.enabled:
        return TERMINAL_UI.read_line()
    return console.input("[bold bright_green]kurt >[/] [dim]/help · /models · /keys · @file[/] ")


def _cfg_is_first_scan():
    return not HW_CACHE_FILE.exists()


def main():
    global TERMINAL_UI, TIP_SHOWN, CONTEXT_PCT, MODEL, SAVED_MODEL
    if len(sys.argv) > 1 and sys.argv[1] in ("-v", "--version"):
        print(f"kurt {__version__}")
        return
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        print("Env: KURT_MODEL, KURT_CTX, KURT_THREADS, KURT_BATCH, KURT_THINK, KURT_TUNE,")
        print("     KURT_AGENTS, KURT_MAX_AGENTS, OLLAMA_HOST")
        return
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if os.name == "nt":
        os.system("")

    print_brand()
    if MODEL.startswith("remote:") and not get_remote_model(MODEL):
        console.print("[yellow]The saved API model no longer exists. Choose another model with /models.[/]")
        MODEL = SAVED_MODEL = DEFAULT_MODEL
        save_config()
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

    messages = new_messages(announce=True)
    messages.append(
        {"role": "user", "content": f"Files in this project:\n{project_tree()}"}
    )
    messages.append(
        {"role": "assistant", "content": "Understood. I can READ, EDIT, WRITE and RUN as needed. What should we work on?"}
    )

    ui = TerminalUI()
    if not ui.enabled:
        console.print("[dim]/help · /models · /keys · @file[/]")
    if ui.start():
        TERMINAL_UI = ui
        ui.draw_footer(preserve_cursor=True)

    try:
        while True:
            CONTEXT_PCT = context_percent(messages)
            try:
                line = read_input().strip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                break
            if not line:
                continue
            if TERMINAL_UI and TERMINAL_UI.enabled:
                console.print()
                console.print(
                    Text.assemble(("> ", "bold bright_green"), (TERMINAL_UI.last_display.strip(), "bold"))
                )
            if line.startswith("/"):
                try:
                    if handle_command(line, messages) == "exit":
                        break
                except (KeyboardInterrupt, EOFError):
                    console.print("\n[yellow]cancelled[/]")
                continue
            content = attach_mentions(line)
            if PENDING_NOTES:
                content = ("[Earlier changes the user rejected]\n" + "\n".join(PENDING_NOTES)
                           + "\n\n" + content)
                PENDING_NOTES.clear()
            messages.append({"role": "user", "content": content})
            if AGENTS:
                try:
                    if TERMINAL_UI and TERMINAL_UI.enabled:
                        TERMINAL_UI.set_busy(True)
                    with BusyListener():
                        extra = run_agents(line)
                except KeyboardInterrupt:
                    console.print("\n[yellow]⏹ interrupted[/]")
                    messages.pop()
                    continue
                finally:
                    if TERMINAL_UI and TERMINAL_UI.enabled:
                        TERMINAL_UI.set_busy(False)
                if extra:
                    messages[-1]["content"] += extra
            try:
                run_turn(messages)
            except KeyboardInterrupt:
                console.print("\n[yellow]⏹ interrupted[/]")
    finally:
        if TERMINAL_UI and TERMINAL_UI.enabled:
            TERMINAL_UI.stop()
        TERMINAL_UI = None


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        sys.exit(130)