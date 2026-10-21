#!/usr/bin/env python3
"""kurt - a terminal coding assistant powered by local models (via Ollama).

Run `kurt` in any project folder. Kurt can read your files and propose
edits / new files / shell commands. Nothing is changed without your OK.
"""
import contextlib
import difflib
import json
import os
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
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

TERMINAL_LOCK = threading.RLock()


class KurtConsole(Console):
    def print(self, *objects, **kwargs):
        with TERMINAL_LOCK:
            return super().print(*objects, **kwargs)


__version__ = "0.2.0"

DEFAULT_MODEL = "hf.co/empero-ai/Qwythos-9B-Claude-Mythos-5-1M-GGUF:Q4_K_M"
CONFIG_DIR = Path.home() / ".kurt"
CONFIG_FILE = CONFIG_DIR / "config.json"


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
MAX_AGENTS = max(1, int(os.environ.get("KURT_MAX_AGENTS", "4")))

_host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
if not _host.startswith("http"):
    _host = "http://" + _host
HOST = _host.replace("0.0.0.0", "127.0.0.1").rstrip("/")
CTX = int(os.environ.get("KURT_CTX", "16384"))  # context window sent to Ollama
MAX_ROUNDS = 6  # max automatic read/run follow-ups per user message
IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".idea", ".vscode", "target", ".next", ".cache", ".pytest_cache",
}

console = KurtConsole(highlight=False)
TERMINAL_UI = None
AUTO = False  # "accept all edits" for this session
UNDO = []  # stack of (path, old_text_or_None, crlf)
PENDING_BTW = []  # notes added with /btw, delivered to the model at the next step




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
    HINTS = "/help · /models · /btw <note> · @file"
    HINTS_BUSY = "/btw <note> adds context without interrupting"
    PLACEHOLDER = 'Try "explain this project" or "fix the bug in @file"'
    PLACEHOLDER_BUSY = "Type /btw <note> to add context while Kurt works"
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
        if meter.elapsed >= 1:
            parts.append(f"{meter.tokens / meter.elapsed:.0f} tok/s")
    console.print(Text("✻ " + " · ".join(parts), style="dim green"))


SYSTEM = """You are Kurt, an expert coding assistant running inside the user's terminal.
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
- A "[btw ...]" section in a user message is extra information the user added while you
  were working. Take it into account without starting over unless it changes the task.
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
                {"model": SAVED_MODEL, "agents": AGENTS, "workers": WORKERS}, indent=2
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
    if ollama_up():
        return True
    exe = shutil.which("ollama")
    if not exe:
        console.print(
            "[red]Ollama isn't installed or isn't on PATH.[/] Get it at https://ollama.com/download"
        )
        return False
    with console.status("Starting Ollama..."):
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        subprocess.Popen(
            [exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs
        )
        for _ in range(40):
            if ollama_up():
                return True
            time.sleep(0.5)
    console.print("[red]Could not start Ollama.[/]")
    return False


def installed_models():
    try:
        data = http_json("/api/tags")
    except Exception:
        return []
    return [
        {"name": m.get("name", ""), "size": m.get("size", 0)}
        for m in data.get("models", [])
        if m.get("name")
    ]


def model_installed():
    return any(same_model(m["name"], MODEL) for m in installed_models())


def pull_model(name):
    exe = shutil.which("ollama") or "ollama"
    try:
        return subprocess.run([exe, "pull", name]).returncode == 0
    except OSError as e:
        console.print(f"[red]Could not run ollama:[/] {escape(str(e))}")
        return False


def ensure_model():
    if model_installed():
        return True
    console.print(f"[yellow]Model not found locally:[/] {escape(MODEL)}")
    if Prompt.ask("Download it now?", choices=["y", "n"], default="y") != "y":
        return False
    return pull_model(MODEL)


def warm_model():
    with console.status(f"Loading {escape(short_model(MODEL))} into memory..."):
        try:
            http_json(
                "/api/generate",
                {"model": MODEL, "keep_alive": "30m", "options": {"num_ctx": CTX}},
                timeout=600,
            )
        except Exception as e:
            console.print(f"[yellow]Warm-up skipped:[/] {escape(str(e))}")


def stream_chat(messages):
    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": True,
        "keep_alive": "30m",
        "options": {"num_ctx": CTX, "temperature": 0.2},
    }
    req = urllib.request.Request(
        HOST + "/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=900) as r:
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
                }
                break


def chat_once(model, messages, temperature=0.2, timeout=900):
    d = http_json(
        "/api/chat",
        {
            "model": model,
            "messages": messages,
            "stream": False,
            "keep_alive": "30m",
            "options": {"num_ctx": CTX, "temperature": temperature},
        },
        timeout=timeout,
    )
    if "error" in d:
        raise RuntimeError(d["error"])
    out = d.get("message", {}).get("content", "")
    return re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()


# ------------------------------------------------------------ Hugging Face ---
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


def hf_search(query):
    url = "https://huggingface.co/api/models?" + urllib.parse.urlencode(
        {
            "search": query,
            "filter": "gguf",
            "sort": "downloads",
            "direction": "-1",
            "limit": "10",
        }
    )
    req = urllib.request.Request(url, headers={"User-Agent": f"kurt/{__version__}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode())
    out = []
    for m in data:
        rid = m.get("id") or m.get("modelId")
        if rid:
            out.append((rid, m.get("downloads", 0)))
    return out


def set_model(name):
    global MODEL, SAVED_MODEL
    MODEL = name
    SAVED_MODEL = name
    save_config()
    warm_model()
    console.print(f"[green]✔ main model:[/] {escape(name)}")


def add_model_flow():
    console.print(
        Panel(
            "Paste a Hugging Face repo (user/name), a huggingface.co link,\n"
            "or type some words to search GGUF models.\n"
            "Optional quantization: user/name:Q4_K_M",
            title="Add a model",
            border_style="green",
            expand=False,
        )
    )
    q = Prompt.ask("Model", console=console, default="", show_default=False).strip()
    if not q:
        return
    ref = parse_hf_ref(q) if ("/" in q and " " not in q) else None
    if ref is None:
        try:
            results = hf_search(q)
        except Exception as e:
            console.print(f"[red]Search failed:[/] {escape(str(e))}")
            return
        if not results:
            console.print("[yellow]No GGUF models found.[/]")
            return
        for i, (rid, dl) in enumerate(results, 1):
            console.print(f"  {i}. {escape(rid)}  [dim]{dl:,} downloads[/]")
        pick = Prompt.ask(
            "Pick a number (Enter to cancel)", console=console, default="", show_default=False
        ).strip()
        if not pick.isdigit() or not 1 <= int(pick) <= len(results):
            return
        ref = (results[int(pick) - 1][0], "")
    repo, quant = ref
    if not quant:
        quant = Prompt.ask(
            "Quantization, e.g. Q4_K_M (Enter = Ollama's default)",
            console=console, default="", show_default=False,
        ).strip()
    name = f"hf.co/{repo}" + (f":{quant}" if quant else "")
    console.print(f"[bold]Downloading[/] {escape(name)} ...")
    if not pull_model(name):
        console.print("[red]Download failed.[/] Check the name and your connection.")
        return
    console.print("[green]✔ downloaded[/]")
    if Prompt.ask("Use it as the main model now?", choices=["y", "n"], default="y", console=console) == "y":
        set_model(name)


def models_menu():
    global AGENTS, WORKERS
    while True:
        models = installed_models()
        table = Table(box=None, header_style="bold", padding=(0, 2))
        table.add_column("#", justify="right")
        table.add_column("Model")
        table.add_column("Size")
        table.add_column("Role")
        for i, m in enumerate(models, 1):
            role = []
            if same_model(m["name"], MODEL):
                role.append("● main")
            if any(same_model(m["name"], w) for w in WORKERS):
                role.append("agent worker")
            table.add_row(str(i), Text(m["name"]), fmt_size(m["size"]), ", ".join(role))
        table.add_row("+", "[green]Add a new model (download from Hugging Face)[/]", "", "")
        table.add_row("g", f"Agents: [bold]{'ON' if AGENTS else 'OFF'}[/]  (toggle)", "", "")
        console.print(Panel(table, title="Models", border_style="magenta", expand=False))
        console.print(
            "[dim]number = use as main model · + = add a model · g = toggle agents · "
            "w <number> = toggle a model as agent worker · Enter = close[/]"
        )
        choice = Prompt.ask("›", console=console, default="", show_default=False).strip().lower()
        if not choice:
            return
        if choice in ("+", "a", "add"):
            add_model_flow()
            continue
        if choice in ("g", "agents"):
            AGENTS = not AGENTS
            save_config()
            console.print(f"agents: [bold]{'ON' if AGENTS else 'OFF'}[/]")
            if AGENTS and not WORKERS:
                console.print(
                    "[dim]No worker models chosen, so helpers will use the main model. "
                    "Add more with: w <number>[/]"
                )
            continue
        mw = re.fullmatch(r"w\s*(\d+)", choice)
        if mw:
            idx = int(mw.group(1))
            if 1 <= idx <= len(models):
                name = models[idx - 1]["name"]
                hit = [w for w in WORKERS if same_model(w, name)]
                if hit:
                    WORKERS = [w for w in WORKERS if not same_model(w, name)]
                    console.print(f"[dim]removed worker {escape(name)}[/]")
                else:
                    WORKERS.append(name)
                    console.print(f"[dim]added worker {escape(name)}[/]")
                save_config()
            else:
                console.print("[red]no such model number[/]")
            continue
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(models):
                set_model(models[idx - 1]["name"])
            else:
                console.print("[red]no such model number[/]")
            continue
        console.print("[red]not an option[/]")


# ------------------------------------------------------------------ /btw ---
def add_btw(note):
    PENDING_BTW.append(note)


def drain_btw(messages):
    """Fold pending /btw notes into the last user message before the next model call."""
    if not PENDING_BTW or not messages or messages[-1]["role"] != "user":
        return
    notes = PENDING_BTW[:]
    del PENDING_BTW[: len(notes)]
    messages[-1]["content"] += (
        "\n\n[btw - extra info from the user, sent while you were working. "
        "Take it into account; do not start over unless it changes the task]\n"
        + "\n".join(f"- {n}" for n in notes)
    )
    console.print(f"[dim]📝 passing {len(notes)} btw note(s) to Kurt[/]")



class BtwListener:
    """While Kurt works, keep keyboard input in the bottom box and accept `/btw <note>`."""

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
        line = line.strip()
        if not line:
            return
        cmd, _, arg = line.partition(" ")
        if cmd.lower() == "/btw" and arg.strip():
            add_btw(arg.strip())
            if TERMINAL_UI and TERMINAL_UI.enabled:
                TERMINAL_UI.set_notice(f"btw queued ({len(PENDING_BTW)})")
            else:
                console.print("[dim]📝 btw noted - Kurt will see it at the next step[/]")
        elif TERMINAL_UI and TERMINAL_UI.enabled:
            TERMINAL_UI.set_notice("K is busy · use /btw <note>")
        else:
            console.print("[dim]Kurt is busy - only '/btw <note>' works right now (Ctrl+C stops it).[/]")

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
        out = chat_once(MODEL, [{"role": "user", "content": prompt}], temperature=0, timeout=300)
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
                attached.append(f"[File {f}]\n{text[:20000]}")
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


def show_diff(old, new, name):
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
    if len(text) > 30000:
        text = text[:30000] + "\n... [truncated]"
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
    budget = int(CTX * 3)  # ~3 chars per token, leave headroom
    while sum(len(m["content"]) for m in messages) > budget and len(messages) > 3:
        del messages[1]


def attach_mentions(text):
    extra = []
    for m in set(re.findall(r"@([\w./\\-]+)", text)):
        p = safe_path(m)
        if p and p.is_file():
            content, _ = read_file(p)
            extra.append(f"\n\n[Attached file {m}]\n{content[:30000]}")
            console.print(f"[dim]📎 attached {m}[/]")
    return text + "".join(extra)




def run_turn(messages):
    for _ in range(MAX_ROUNDS):
        drain_btw(messages)
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
        try:
            with BtwListener():
                for kind, chunk in stream_chat(messages):
                    if kind == "stats":
                        meter.tokens = chunk.get("eval_count") or meter.tokens
                        continue
                    meter.tokens += 1  # live estimate: one streamed chunk is about one token
                    if kind == "thinking":
                        meter.label = "Thinking"
                        if not thinking_started:
                            console.print(Text("Thinking", style="bold green"))
                            thinking_started = True
                        console.print(chunk, style="dim green", end="", markup=False, soft_wrap=True)
                    else:
                        meter.label = "Writing"
                        if not answer_started:
                            if thinking_started:
                                console.print()
                            console.print(Text("K › ", style="bold bright_green"), end="")
                            answer_started = True
                        full += chunk
                        console.print(chunk, end="", markup=False, soft_wrap=True)
        except KeyboardInterrupt:
            interrupted = True
            console.print("\n[yellow]⏹ interrupted[/]")
        except Exception as e:
            failed = True
            console.print(f"\n[red]Error talking to Ollama:[/] {escape(str(e))}")
        finally:
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
            if PENDING_BTW:
                console.print("[dim]📝 btw notes saved - Kurt will get them with your next message[/]")
            return
        feedback = handle_actions(full)
        if not feedback:
            if not PENDING_BTW:
                return
            feedback = "[tool results]\nNo pending actions."
        messages.append({"role": "user", "content": feedback})
    console.print("[yellow]Stopped after several automatic steps. Say 'continue' to go on.[/]")


HELP = """[bold]Commands[/]
  /help           show this help
  /models         list models, switch the main model, add a model from Hugging Face,
                  and turn agents on or off
  /btw <note>     add info for Kurt while it is working, without interrupting it
  /files          list project files
  /add <path>     put a file's contents into the conversation (or use @path in a message)
  /auto           toggle auto-accept for file edits (commands always ask)
  /undo           revert the last applied change
  /clear          forget the conversation
  /exit           quit  (Ctrl+C while Kurt is typing stops generation)"""


def new_messages():
    return [{"role": "system", "content": SYSTEM.replace("{cwd}", str(Path.cwd()))}]


def handle_command(line, messages):
    global AUTO
    cmd, _, arg = line.partition(" ")
    cmd = cmd.lower()
    if cmd in ("/exit", "/quit", "/q"):
        return "exit"
    if cmd == "/help":
        console.print(HELP)
    elif cmd == "/models":
        models_menu()
    elif cmd == "/btw":
        if not arg.strip():
            console.print("usage: /btw <note>", markup=False)
        else:
            add_btw(arg.strip())
            console.print("[dim]📝 btw noted - Kurt will get it with your next message[/]")
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
            messages.append({"role": "user", "content": f"[File {arg}]\n{text[:30000]}"})
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
    return console.input("[bold bright_green]kurt >[/] [dim]/help · /models · /btw <note> · @file[/] ")


def main():
    global TERMINAL_UI
    if len(sys.argv) > 1 and sys.argv[1] in ("-v", "--version"):
        print(f"kurt {__version__}")
        return
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        print("Env: KURT_MODEL, KURT_CTX, KURT_AGENTS, KURT_MAX_AGENTS, OLLAMA_HOST")
        return
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if os.name == "nt":
        os.system("")

    print_brand()
    if not ensure_ollama() or not ensure_model():
        sys.exit(1)
    warm_model()

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
            "[dim]Ask in plain English. Use @file to attach a file. While Kurt works, "
            "type /btw <note> in the bottom box to add context without interrupting "
            "(Esc stops it).[/]"
        )
    else:
        console.print(
            "[dim]Ask for changes in plain English. /help for commands, /models to switch models "
            "or turn agents on, /btw to add info while Kurt works. Use @file to attach a file.[/]\n"
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
                if handle_command(line, messages) == "exit":
                    break
                continue
            messages.append({"role": "user", "content": attach_mentions(line)})
            if AGENTS:
                try:
                    if TERMINAL_UI and TERMINAL_UI.enabled:
                        TERMINAL_UI.set_busy(True)
                    with BtwListener():
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
    main()