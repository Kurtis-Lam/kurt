#!/usr/bin/env python3
"""kurt - a terminal coding assistant powered by local models (via Ollama).

Run `kurt` in any project folder. Kurt can read your files and propose
edits / new files / shell commands. Nothing is changed without your OK.
"""
import contextlib
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
from pathlib import Path

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


__version__ = "0.3.0"

DEFAULT_MODEL = "hf.co/empero-ai/Qwythos-9B-Claude-Mythos-5-1M-GGUF:Q4_K_M"
CONFIG_DIR = Path.home() / ".kurt"
CONFIG_FILE = CONFIG_DIR / "config.json"
HW_CACHE_FILE = CONFIG_DIR / "hardware.json"
FAST_MODEL = "qwen2.5-coder:3b"  # ~2 GB: several times faster than a 9B model on a CPU


def load_config():
    try:
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
    HINTS = "/help · /models · @file"
    HINTS_BUSY = "Esc stops Kurt"
    PLACEHOLDER = 'Try "explain this project" or "fix the bug in @file"'
    PLACEHOLDER_BUSY = "Kurt is working · Esc to stop"
    HEIGHT = 5  # status row, box top, input row, box bottom, hints row

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

    # ---- lifecycle
    def start(self):
        if not self.enabled:
            return False
        with TERMINAL_LOCK:
            # make room at the bottom without clearing what is already on screen
            sys.stdout.write(
                "\n" * self.HEIGHT + f"\x1b[{self.HEIGHT}A"
                + "\x1b7" + f"\x1b[1;{self.scroll_bottom}r" + "\x1b8"
            )
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

    def _status_row(self, w):
        m = self.meter
        if m is None:
            return " " * w
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
            lines = self._lines()
            frame = "\x1b[?7l" + "".join(
                f"\x1b[{self.footer_top + i};1H{line}\x1b[K" for i, line in enumerate(lines)
            ) + "\x1b[?7h"
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

You can act on the project by emitting ACTION BLOCKS. The user is asked to confirm
every change, and results are sent back to you. Formats (exactly):

Read a file:
@@@ READ relative/path.py
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

Run a shell command (user confirms first):
@@@ RUN python -m pytest -q
@@@ END

Rules:
- READ a file before editing it. Never guess file contents.
- Use paths relative to the project directory.
- Make minimal, focused edits. Prefer EDIT over WRITE for existing files.
- Briefly explain what you are doing, then emit the blocks. Do not wrap blocks in code fences.
- Do not claim a change is applied until the tool result says so.
- If a tool result reports an error, fix your block and try again.
- A "[Helper agent reports ...]" section holds findings from helper agents that ran in
  parallel. Use them, double-check against the files, and make any changes yourself.
"""

BLOCK_RE = re.compile(r"@@@ (EDIT|WRITE|READ|RUN) ?([^\n]*)\n(.*?)\n?@@@ END", re.S)
PAIR_RE = re.compile(
    r"<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)\n?>>>>>>> REPLACE", re.S
)


# ----------------------------------------------------------------- Config ---
def save_config():
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(
            json.dumps(
                {
                    "model": SAVED_MODEL, "agents": AGENTS, "workers": WORKERS,
                    "think": THINK, "tip_shown": TIP_SHOWN,
                    "model_sort": MODEL_SORT, "model_sort_desc": MODEL_SORT_DESC,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def short_model(name, limit=44):
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
    if model_installed():
        return True
    console.print(f"[yellow]Model not found locally:[/] {escape(MODEL)}")
    if Prompt.ask("Download it now?", choices=["y", "n"], default="y") != "y":
        return False
    return pull_model(MODEL)


def warm_model(quiet=False):
    """Load the model into memory with the exact runner options later requests will use."""
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
        if path.exists() and (not size or path.stat().st_size == size):
            done_before += size
            continue
        part = path.with_name(path.name + ".part")
        url = f"{HF_BASE}/{repo}/resolve/main/{urllib.parse.quote(name)}"
        for attempt in range(6):
            pos = part.stat().st_size if part.exists() else 0
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
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                if attempt == 5:
                    _progress_end()
                    console.print(f"[red]Download failed:[/] {escape(str(e))} (run again to resume)")
                    return False
                time.sleep(2 + attempt * 2)
        done_before += size
    _progress_end()
    return True


def _run(cmd, **kw):
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
        if _run([sys.executable, conv, str(dest), "--outfile", str(out), "--outtype", outtype]):
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
    if not _run(cmd):
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
    MODEL = name
    SAVED_MODEL = name
    save_config()
    retune()  # a different model needs a different context size
    warm_model()
    console.print(f"[green]✔ main model:[/] {escape(name)}")


def add_model_flow():
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


def models_menu():
    global AGENTS, WORKERS, MODEL_SORT, MODEL_SORT_DESC
    buf = ""
    note = ""
    skip_clear = False
    while True:
        models = sort_models(installed_models(), MODEL_SORT, MODEL_SORT_DESC)
        arrow = " ▼" if MODEL_SORT_DESC else " ▲"

        def head(key, label):
            return label + (arrow if MODEL_SORT == key else "")

        table = Table(box=None, header_style="bold", padding=(0, 2))
        table.add_column("#", justify="right", no_wrap=True)
        table.add_column(head("name", "Model"), overflow="fold")
        table.add_column(head("params", "Params"), justify="right", no_wrap=True)
        table.add_column(head("size", "Size"), justify="right", no_wrap=True)
        table.add_column("Role", no_wrap=True)
        for i, m in enumerate(models, 1):
            role = []
            if same_model(m["name"], MODEL):
                role.append("● main")
            if any(same_model(m["name"], w) for w in WORKERS):
                role.append("agent worker")
            table.add_row(
                str(i), Text(m["name"]), m["params"] or "-", fmt_size(m["size"]) or "-", ", ".join(role),
            )

        keys = Table.grid(padding=(0, 2))
        keys.add_column(style="bold", no_wrap=True)
        keys.add_column()
        keys.add_row("number + Enter", "use as main model")
        keys.add_row("+", "add a model")
        keys.add_row("g", f"toggle agents (now {'ON' if AGENTS else 'OFF'})")
        keys.add_row("w + number + Enter", "toggle a model as agent worker")
        keys.add_row("s  or  ← →", f"sort by Model / Size / Params (now {SORT_KEYS[MODEL_SORT][0]})")
        keys.add_row("r  or  ↑ ↓", "reverse the sort order")
        keys.add_row("Enter / q", "close")

        interactive = True
        try:
            interactive = sys.stdin.isatty()
        except Exception:
            interactive = False
        if interactive and not skip_clear:
            console.clear()
        skip_clear = False
        console.print(Panel(table, title="Models", border_style="magenta", expand=False))
        console.print()
        console.print(keys)
        if note:
            console.print(f"\n[dim]{note}[/]")
            note = ""
        if not interactive:
            return
        console.print(f"\n[bold]›[/] {escape(buf)}", end="")
        key = read_menu_key()
        console.print()
        if key is None or key == "ctrl-c":
            return
        if key == "enter":
            if not buf:
                return
            cmd, buf = buf, ""
            mw = re.fullmatch(r"w\s*(\d+)", cmd)
            if mw:
                idx = int(mw.group(1))
                if 1 <= idx <= len(models):
                    name = models[idx - 1]["name"]
                    if any(same_model(w, name) for w in WORKERS):
                        WORKERS = [w for w in WORKERS if not same_model(w, name)]
                        note = f"removed worker {escape(name)}"
                    else:
                        WORKERS.append(name)
                        note = f"added worker {escape(name)}"
                    save_config()
                else:
                    note = "[red]no such model number[/]"
            elif cmd.isdigit():
                idx = int(cmd)
                if 1 <= idx <= len(models):
                    set_model(models[idx - 1]["name"])
                else:
                    note = "[red]no such model number[/]"
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
        if len(key) != 1:
            k = key
            if buf:
                continue
        else:
            k = key.lower()
        if k.isdigit():
            buf += k
        elif k == "w" and not buf:
            buf = "w"
        elif buf:
            continue
        elif k == "q":
            return
        elif k in ("s", "right"):
            i = MENU_SORT_ORDER.index(MODEL_SORT) if MODEL_SORT in MENU_SORT_ORDER else 0
            MODEL_SORT = MENU_SORT_ORDER[(i + 1) % len(MENU_SORT_ORDER)]
            MODEL_SORT_DESC = SORT_KEYS[MODEL_SORT][1]
            save_config()
        elif k == "left":
            i = MENU_SORT_ORDER.index(MODEL_SORT) if MODEL_SORT in MENU_SORT_ORDER else 0
            MODEL_SORT = MENU_SORT_ORDER[(i - 1) % len(MENU_SORT_ORDER)]
            MODEL_SORT_DESC = SORT_KEYS[MODEL_SORT][1]
            save_config()
        elif k in ("r", "up", "down"):
            MODEL_SORT_DESC = not MODEL_SORT_DESC
            save_config()
        elif k in ("+", "a"):
            add_model_flow()
            skip_clear = True
        elif k == "g":
            AGENTS = not AGENTS
            save_config()
            note = f"agents: {'ON' if AGENTS else 'OFF'}"
            if AGENTS and not WORKERS:
                note += " · no worker models chosen, so helpers will use the main model (press w then a number)"


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
    names = [m["name"] for m in installed_models()]
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


def show_diff(old, new, name):
    import difflib
    from rich.syntax import Syntax

    diff = "".join(
        difflib.unified_diff(
            old.splitlines(True), new.splitlines(True), f"a/{name}", f"b/{name}", n=3
        )
    )
    lines = diff.splitlines()
    if len(lines) > 120:
        diff = "\n".join(lines[:120]) + f"\n... ({len(lines) - 120} more lines)"
    console.print(Syntax(diff or "(no changes)", "diff", theme="ansi_dark"))


def show_new_file(name, text):
    from rich.syntax import Syntax

    lines = text.splitlines()
    shown = "\n".join(lines[:80]) + (f"\n... ({len(lines) - 80} more lines)" if len(lines) > 80 else "")
    try:
        lexer = Syntax.guess_lexer(name, shown)
    except Exception:
        lexer = "text"
    console.print(Syntax(shown, lexer, theme="ansi_dark", line_numbers=True))


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
    p = safe_path(arg)
    if not p or not p.is_file():
        return f"READ {arg}: ERROR file not found (or outside project)."
    text, _ = read_file(p)
    console.print(f"[dim]📖 read {arg} ({len(text)} chars)[/]")
    cap = file_cap()
    if len(text) > cap:
        text = text[:cap] + "\n... [truncated]"
    return f"READ {arg}:\n{text}"


def do_edit(arg, body):
    p = safe_path(arg)
    if not p or not p.is_file():
        return f"EDIT {arg}: ERROR file not found (or outside project). Use WRITE to create files."
    pairs = PAIR_RE.findall(body)
    if not pairs:
        return f"EDIT {arg}: ERROR no valid SEARCH/REPLACE pairs found."
    old, crlf = read_file(p)
    new = old
    for i, (s, r) in enumerate(pairs, 1):
        n = new.count(s)
        if n == 0:
            return f"EDIT {arg}: ERROR SEARCH #{i} not found. It must match the file exactly. Re-READ the file."
        if n > 1:
            return f"EDIT {arg}: ERROR SEARCH #{i} matches {n} places. Include more context to make it unique."
        new = new.replace(s, r, 1)
    console.print(Panel(f"[bold]Edit[/] {arg}", border_style="yellow", expand=False))
    show_diff(old, new, arg)
    if not confirm("Apply this edit? (y=yes, n=no, a=yes to all)"):
        return f"EDIT {arg}: REJECTED by user."
    UNDO.append((p, old, crlf))
    write_file(p, new, crlf)
    console.print(f"[green]✔ edited {arg}[/]")
    return f"EDIT {arg}: applied."


def do_write(arg, body):
    p = safe_path(arg)
    if not p:
        return f"WRITE {arg}: ERROR path outside project."
    exists = p.is_file()
    old, crlf = read_file(p) if exists else ("", False)
    new = body if body.endswith("\n") else body + "\n"
    verb = "Overwrite" if exists else "Create"
    console.print(Panel(f"[bold]{verb}[/] {arg}", border_style="yellow", expand=False))
    if exists:
        show_diff(old, new, arg)
    else:
        show_new_file(arg, new)
    if not confirm(f"{verb} this file? (y=yes, n=no, a=yes to all)"):
        return f"WRITE {arg}: REJECTED by user."
    UNDO.append((p, old if exists else None, crlf))
    write_file(p, new, crlf)
    console.print(f"[green]✔ wrote {arg}[/]")
    return f"WRITE {arg}: applied."


def do_run(cmd):
    cmd = cmd.strip()
    console.print(Panel(f"[bold]Run command[/]\n{cmd}", border_style="red", expand=False))
    if Prompt.ask("Run it?", choices=["y", "n"], default="n", console=console) != "y":
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
    console.print(out[:3000] or "[dim](no output)[/]", markup=False)
    if len(out) > 4000:
        out = out[:4000] + "\n... [truncated]"
    return f"RUN {cmd}: exit code {code}\n{out}"


def handle_actions(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    results = []
    for kind, arg, body in BLOCK_RE.findall(text):
        arg = arg.strip()
        if kind == "READ":
            results.append(do_read(arg))
        elif kind == "EDIT":
            r = do_edit(arg, body)
            if "ERROR" in r:
                console.print(f"[red]{r}[/]")
                results.append(r)
        elif kind == "WRITE":
            r = do_write(arg, body)
            if "ERROR" in r:
                console.print(f"[red]{r}[/]")
                results.append(r)
        elif kind == "RUN":
            results.append(do_run(arg or body))
    if not results:
        return ""
    return "[tool results]\n" + "\n\n".join(results)


# ------------------------------------------------------------------- Chat ---
def trim(messages):
    """Keep the conversation inside the context window.

    When over budget, drop old messages down to 70% of it in ONE go. Ollama reuses its cached
    prompt as long as the start of the conversation is unchanged, so trimming rarely (instead of
    shaving one message every turn) avoids re-reading the whole prompt on every request."""
    budget = int(CTX * 3)  # ~3 chars per token, leave headroom
    total = sum(len(m["content"]) for m in messages)
    if total <= budget:
        return
    target = int(budget * 0.7)
    cut = 1
    while total > target and len(messages) - (cut - 1) > 3:
        total -= len(messages[cut]["content"])
        cut += 1
    del messages[1:cut]


def attach_mentions(text):
    extra = []
    cap = file_cap()
    for m in set(re.findall(r"@([\w./\\-]+)", text)):
        p = safe_path(m)
        if p and p.is_file():
            content, _ = read_file(p)
            extra.append(f"\n\n[Attached file {m}]\n{content[:cap]}")
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
                        meter.label = "Writing"
                        if not answer_started:
                            printer.flush()
                            if thinking_started:
                                console.print()
                            console.print(Text("K › ", style="bold bright_green"), end="")
                            answer_started = True
                        full += chunk
                        printer.write(chunk)
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
            console.print(f"\n[red]Error talking to Ollama:[/] {escape(str(e))}")
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
  /models         list models, switch the main model, add a model from Hugging Face,
                  and turn agents on or off
  /speed          show the detected hardware and the speed settings in use
                  (/speed refresh re-detects it)
  /think [on|off] turn thinking on or off (off is much faster on computers without a GPU)
  /fast [model]   switch to a small, much faster model (default qwen2.5-coder:3b)
  /files          list project files
  /add <path>     put a file's contents into the conversation (or use @path in a message)
  /auto           toggle auto-accept for file edits (commands always ask)
  /undo           revert the last applied change
  /clear          forget the conversation
  /exit           quit  (Ctrl+C while Kurt is typing stops generation)"""


def new_messages():
    return [{"role": "system", "content": SYSTEM.replace("{cwd}", str(Path.cwd()))}]


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
    elif cmd == "/clear":
        messages[:] = new_messages()
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
            messages.append({"role": "user", "content": f"[File {arg}]\n{text[:file_cap()]}"})
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
    return console.input("[bold bright_green]kurt >[/] [dim]/help · /models · @file[/] ")


def _cfg_is_first_scan():
    return not HW_CACHE_FILE.exists()


def main():
    global TERMINAL_UI, TIP_SHOWN
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
    if _cfg_is_first_scan():
        with console.status("Detecting your hardware (first run only)..."):
            get_hardware()
    else:
        get_hardware()
    if not ensure_ollama() or not ensure_model():
        sys.exit(1)
    retune()
    console.print(Text(tune_summary(), style="dim green"))
    if get_hardware()["accel"] == "cpu" and TUNE.get("model_gb", 0) > 3.5:
        console.print("[dim]Slow on this computer? Type /fast for a small model that is several times faster.[/]")
    # load the model in the background so you can start typing right away
    threading.Thread(target=warm_model, kwargs={"quiet": True}, daemon=True).start()
    if SERVER_EXTERNAL and not TIP_SHOWN:
        console.print("[dim]Tip: /speed shows Ollama settings that can make it even faster.[/]")
        TIP_SHOWN = True
        save_config()

    messages = new_messages()
    messages.append(
        {"role": "user", "content": f"Files in this project:\n{project_tree()}"}
    )
    messages.append(
        {"role": "assistant", "content": "Understood. I can READ, EDIT, WRITE and RUN as needed. What should we work on?"}
    )

    ui = TerminalUI()
    if ui.enabled:
        console.print(
            "[dim]Ask in plain English. Use @file to attach a file. "
            "Esc stops Kurt while it works.[/]"
        )
    else:
        console.print(
            "[dim]Ask for changes in plain English. /help for commands, /models to switch models "
            "or turn agents on. Use @file to attach a file.[/]\n"
        )
    if ui.start():
        TERMINAL_UI = ui
        ui.draw_footer(preserve_cursor=True)

    try:
        while True:
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
            messages.append({"role": "user", "content": attach_mentions(line)})
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
            run_turn(messages)
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