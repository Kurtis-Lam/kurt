"""Kurt agent mode.

A lead agent orchestrates a team of subagents that share one context (a
channel, shared notes, a plan and a live list of the files in the workspace).
Everything runs in background threads, retries failed or rate-limited model
calls after one minute, and stays reachable: the user can message the lead at
any time and receive an answer while the team keeps working.

Runs are finite by default: the lead ends the run with FINISH once the goal is
met and verified. Continuous mode (a switch in the UI, or the user asking for
it in words) keeps the team working until the user stops it.

Guard rails enforced by code, not just by prompts:

* runaway or repeating model output is detected while it streams, aborted,
  discarded and retried with a correction;
* tool results written by the model itself are cut off and never executed;
* DONE needs evidence (real tool use) and may not cite files that do not exist;
* an existing file cannot be overwritten before the agent has read it;
* agents may only read inside the project workspace;
* duplicate, flooding and ping-pong messages are suppressed;
* repeated identical actions are flagged, then the agent is marked BLOCKED;
* FINISH is refused while teammates work or before the final state has been
  verified by someone other than its author.

The module uses only the Python standard library. All Kurt services (model
routing, workspace files, commands, approvals, usage) are reached through the
``host`` object, which is the KurtWebApp instance.
"""

import difflib
import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections import deque, namedtuple
from pathlib import Path

try:
    from kurt_sandbox import parse_tty_keys
except ImportError:
    def parse_tty_keys(text):
        return list(str(text or "").replace(" ", ""))

RETRY_SECONDS = 60
MAX_SUBAGENTS = 8
MAX_EVENTS = 4000
MAX_CHANNEL = 400
MAX_ENTRIES = 80
MAX_ACTIVITY = 800
MAX_HISTORY = 14
MAX_TOOL_ROUNDS = 30
LEAD_TOOL_ROUNDS = 6
LEAD_SOLO_ROUNDS = 24
RETRY_STEPS = (8, 15, 30, 60)
CONTINUE_REASON = "continuing your last turn"
LEAD_HEARTBEAT = 90
GENERATION_TOKENS = 6144
QUICK_REPLY_TOKENS = 1024
APPROVAL_TIMEOUT = 1800
RESULT_LIMIT = 12000
READ_LIMIT = 30000
EVENT_TEXT_LIMIT = 6000
PERSIST_INTERVAL = 3.0

MAX_ACTIONS_PER_TURN = 8
MAX_DEGENERATE_STRIKES = 3
MAX_NUDGES = 2
MAX_REPEAT_ACTIONS = 3
MAX_LOOP_WARNINGS = 2
MAX_DONE_REJECTS = 2
MAX_FINISH_REJECTS = 3
REASONING_CHAR_LIMIT = 24000
POST_WINDOW = 60.0
POST_LIMIT = 8
PINGPONG_LIMIT = 6
DUPLICATE_RATIO = 0.88
STALL_SECONDS = 420
IDLE_REVIEW_LIMIT = 3
MAX_AUTO_RECOVERIES = 3
LEAD_BLOCK_PUSHES = 3
BLOCK_REVIEW_LIMIT = 12
BLOCK_HEARTBEAT = 20
FILE_SCAN_LIMIT = 400
FILE_LIST_LIMIT = 60
SCAN_DEPTH = 5
TTY_TIMEOUT = 10
DEFAULT_TEMPERATURE = 0.2
RETRY_TEMPERATURE = 0.55

EXTRACT_COMMAND_RE = re.compile(r"^\s*(?:cd\s+\S+\s*&&\s*)?(?:unzip|7z[a]?\s+x|7z[a]?\s+e|unrar\s+x|tar\s+-?[a-zA-Z]*x[a-zA-Z]*)\b")


def is_extract_command(command):
    """True for a command that only unpacks an archive the user supplied (not a deliverable the team wrote)."""
    text = str(command or "").strip()
    if not text or "\n" in text:
        return False
    parts = [part for part in re.split(r"\s*(?:&&|;)\s*", text) if part]
    return bool(parts) and all(EXTRACT_COMMAND_RE.match(part) or re.match(r"^\s*(?:ls|cat|head|tail|wc|echo|find)\b", part) for part in parts) \
        and any(EXTRACT_COMMAND_RE.match(part) for part in parts)


THINK_TAG_RE = re.compile(r"<(/)?think(?:ing)?>", re.I)
STRAY_CLOSE_RE = re.compile(r"</think(?:ing)?>", re.I)
RATE_LIMIT_RE = re.compile(r"429|rate.?limit|quota|overload|too many|capacity|throttl", re.I)
BLOCK_START_RE = re.compile(r"^\s*@@@\s*([A-Za-z_]+)\b[ \t]*(.*)$")
BLOCK_END_RE = re.compile(r"^\s*@@@\s*END\b", re.I)
VERBS = {"SPAWN", "ASSIGN", "RETIRE", "PLAN", "WAIT", "SAY", "NOTE", "DONE", "BLOCKED", "FINISH",
         "READ", "LIST", "WRITE", "RUN", "SEARCH"}
LEAD_ONLY = {"SPAWN", "ASSIGN", "RETIRE", "PLAN", "WAIT", "FINISH"}
WORKER_ONLY = {"DONE", "BLOCKED"}
TOOL_VERBS = {"READ", "LIST", "WRITE", "RUN", "SEARCH"}
WORKING_STATES = ("working", "waiting", "retrying")

FABRICATED_RE = re.compile(
    r"^\s*(?:TOOL\s+RESULTS?\s*:|NEW\s+MESSAGES\s+FOR\s+YOU\s*:|SHARED\s+CONTEXT\b|</?(?:tool_response|tool_result|function_results?)>"
    r"|\[(?:READ|LIST)\s+\S"
    r"|\[(?:READ|LIST|WRITE|RUN|SEARCH|SPAWN|ASSIGN|RETIRE|DONE|BLOCKED|FINISH|SAY|NOTE|PLAN|WAIT)\s+"
    r"(?:result|results|ok|succeeded|failed|rejected|declined|held|suppressed|ignored|for:)\b)",
    re.I)
RESULT_RE = re.compile(r"^\W*RESULT\s*:\s*(PASS|FAIL|PARTIAL)\b", re.I | re.M)

CONTINUOUS_ON_RE = re.compile(
    r"\b(?:run|runs|running|work|works|working|operate|keep\s+(?:going|working|running)|go\s+on|stay\s+(?:on|running|active))\b"
    r"[^.\n]{0,25}?\b(?:continuous(?:ly)?|non-?stop|forever|indefinitely|24\s*/\s*7)\b"
    r"|\b(?:continuous(?:ly)?|non-?stop|always[- ]on)\s+(?:mode|agents?|run|running)\b"
    r"|\bnever\s+stop\b|\buntil\s+i\s+(?:tell|ask|say|stop)\b", re.I)
CONTINUOUS_OFF_RE = re.compile(
    r"\b(?:stop\s+when\s+(?:it\s+is\s+|it'?s\s+|you\s+are\s+|you'?re\s+)?(?:done|finished|complete)"
    r"|(?:turn|switch)\s+off\s+continuous|not\s+continuous|stop\s+continuous|no\s+longer\s+continuous"
    r"|finish\s+and\s+stop)\b", re.I)

Action = namedtuple("Action", "verb args body closed")


class StopRequested(Exception):
    pass


def clip(text, limit):
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "\n… [truncated {} characters]".format(len(text) - limit)


def _short(text, limit):
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + "\u2026"


def clip_tail(text, limit):
    text = str(text or "")
    if len(text) <= limit:
        return text
    return "… " + text[-limit:]


def slug(value, fallback="agent"):
    value = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "").strip()).strip("-_")
    return (value or fallback)[:24]


def detect_continuous(text):
    """True or False when the user clearly asks for (or against) continuous running, else None."""
    text = str(text or "")
    on, off = bool(CONTINUOUS_ON_RE.search(text)), bool(CONTINUOUS_OFF_RE.search(text))
    if on == off:
        return None
    return on


# ---------------------------------------------------------------------------
# Parsing model output
# ---------------------------------------------------------------------------

FUNC_CALL_RE = re.compile(r"<function=\s*([A-Za-z_][\w.-]*)\s*>(.*?)(?:</function>|\Z)", re.S | re.I)
FUNC_PARAM_RE = re.compile(r"<parameter=\s*([A-Za-z_][\w.-]*)\s*>\s*(.*?)\s*(?:</parameter>|(?=<parameter=)|\Z)", re.S | re.I)
WRAPPER_TAG_RE = re.compile(
    r"</?\s*(?:[A-Za-z0-9]+_)?(?:tool_calls?|function_calls?|calls|tool_use|tool_code|function|parameter|invoke|arg_key|arg_value)\b[^>]*>", re.I)
ARG_TAG_RE = re.compile(r"</?\s*arg_(?:key|value)\s*>", re.I)
ARG_PAIR_RE = re.compile(r"<arg_key>\s*(.*?)\s*</arg_key>\s*<arg_value>\s*(.*?)\s*(?:</arg_value>|(?=<arg_key>)|\Z)", re.S | re.I)
SPECIAL_TOKEN_RE = re.compile(r"<\|[^|<>\n]{1,48}\|>")
DSML_PREFIX_RE = re.compile(r"<[\u200b\s]*(/?)[\u200b\s]*[\uff5c|]\s*DSML\s*[\uff5c|]\s*(?=[A-Za-z])", re.I)
INVOKE_RE = re.compile(r"<invoke\s+name\s*=\s*[\"']([^\"']+)[\"']\s*>(.*?)(?:</invoke\s*>|(?=<invoke\s)|\Z)", re.S | re.I)
NAMED_PARAM_RE = re.compile(r"<parameter\s+name\s*=\s*[\"']([^\"']+)[\"'][^>]*>\s*(.*?)\s*(?:</parameter\s*>|(?=<parameter\s)|\Z)", re.S | re.I)
# Tool-call dialects other than '@@@' that models fall back on:
#   <|tool_call_begin|> RUN ls <|tool_call_end|>            (Kimi / DeepSeek style special tokens)
#   <dots_function_call> ls -la \n\n description </...>      (a bare command, optionally followed by a description)
TOKEN_CALL_RE = re.compile(
    r"<\|tool_call_(?:begin|start)\|>(.*?)(?:<\|tool_call_end\|>|(?=<\|tool_call_(?:begin|start)\|>)|\Z)", re.S | re.I)
TAG_CALL_RE = re.compile(
    r"<((?:[A-Za-z0-9]+_)?(?:function_call|tool_call|tool_use|tool_code))\s*>(.*?)(?:</\1\s*>|(?=<\1\s*>)|\Z)", re.S | re.I)
FOREIGN_MARKER_RE = re.compile(r"DSML|<invoke\s|<\|tool_call_(?:begin|start)\|>|<(?:[A-Za-z0-9]+_)?(?:function_call|tool_call|tool_use|tool_code)\s*>|<function\b", re.I)
FENCE_RE = re.compile(r"```[A-Za-z0-9_-]*[ \t]*\n(.*?)\n[ \t]*```", re.S)
FENCED_TOOL_LINE_RE = re.compile(r"^\s*(RUN|READ|LIST|SEARCH)[ \t]+(\S.*?)\s*$")
FUNC_ALIASES = {
    "READ_FILE": "READ", "VIEW": "READ", "CAT": "READ", "OPEN": "READ",
    "LIST_DIR": "LIST", "LIST_FILES": "LIST", "LS": "LIST", "LIST_DIRECTORY": "LIST",
    "WRITE_FILE": "WRITE", "CREATE_FILE": "WRITE", "SAVE_FILE": "WRITE",
    "RUN_COMMAND": "RUN", "BASH": "RUN", "SHELL": "RUN", "EXEC": "RUN", "EXECUTE": "RUN",
    "WEB_SEARCH": "SEARCH", "SEND_MESSAGE": "SAY", "MESSAGE": "SAY", "REPORT": "SAY",
}
PARAM_NAMES = {
    "path": ("path", "file", "file_path", "filepath", "filename", "directory", "dir", "target", "name"),
    "query": ("query", "q", "search", "text"),
    "content": ("content", "file_text", "contents", "body", "text", "data"),
    "command": ("command", "cmd", "body", "script"),
    "target": ("target", "to", "recipient", "agent", "name"),
    "message": ("body", "message", "text", "content", "msg"),
    "key": ("key", "name", "title", "topic"),
    "note": ("body", "value", "text", "content", "note"),
    "summary": ("summary", "body", "text", "content", "result"),
    "name": ("name", "agent", "target", "id"),
    "role": ("role", "title", "type"),
    "mission": ("mission", "body", "task", "instructions", "instruction", "text", "content", "prompt"),
    "plan": ("plan", "body", "text", "content"),
    "seconds": ("seconds", "duration", "time", "delay", "value"),
}


def _param(params, kind):
    for key in PARAM_NAMES[kind]:
        if key in params and str(params[key]).strip():
            return str(params[key])
    return ""


def _oneline(value):
    return " ".join(str(value or "").split())


def _guess_verb(params):
    """Which action an unknown tool name most likely meant, judging by its parameters ('' when unclear)."""
    keys = set(params)
    if keys & {"command", "cmd", "script"}:
        return "RUN"
    if keys & {"query", "q"}:
        return "SEARCH"
    path = next((str(params[key]).strip() for key in ("path", "file", "file_path", "filepath", "filename", "directory", "dir") if key in params), "")
    if path and keys & {"content", "contents", "file_text", "text", "data"}:
        return "WRITE"
    if path:
        return "LIST" if path.endswith(("/", "\\")) else "READ"
    return ""


def _function_to_block(name, raw_params):
    verb = re.sub(r"[^A-Za-z_]", "_", name).upper()
    verb = FUNC_ALIASES.get(verb, verb)
    params = {key.lower(): value for key, value in raw_params}
    if verb not in VERBS:
        verb = _guess_verb(params)
        if verb not in VERBS:
            return ""
    if verb in ("READ", "LIST"):
        args, body = _oneline(_param(params, "path")), ""
    elif verb == "SEARCH":
        args, body = _oneline(_param(params, "query")), ""
    elif verb == "WRITE":
        args, body = _oneline(_param(params, "path")), _param(params, "content")
    elif verb == "RUN":
        args, body = "", _param(params, "command")
    elif verb == "SAY":
        args, body = _oneline(_param(params, "target")) or "all", _param(params, "message")
    elif verb == "NOTE":
        args, body = _oneline(_param(params, "key")), _param(params, "note")
    elif verb in ("DONE", "BLOCKED", "FINISH"):
        args, body = "", _param(params, "summary")
    elif verb == "SPAWN":
        name_value = _oneline(_param(params, "name")) or "agent"
        role_value = _oneline(_param(params, "role")) or "Specialist"
        args, body = "{} | {}".format(name_value, role_value), _param(params, "mission")
    elif verb == "ASSIGN":
        args, body = _oneline(_param(params, "name")), _param(params, "mission")
    elif verb == "RETIRE":
        args, body = _oneline(_param(params, "name")), ""
    elif verb == "PLAN":
        args, body = "", _param(params, "plan")
    else:
        args, body = _oneline(_param(params, "seconds")), ""
    return "@@@ {} {}\n{}\n@@@ END".format(verb, args, body).replace("@@@ {} \n".format(verb), "@@@ {}\n".format(verb), 1)


def _block_text(verb, first, rest=""):
    first, rest = str(first or "").strip(), str(rest or "").strip("\n")
    head = "@@@ {} {}".format(verb, first).rstrip()
    return "{}\n{}\n@@@ END".format(head, rest) if rest.strip() else head + "\n@@@ END"


def _shell_block(inner):
    """A bare command, optionally followed by a blank line and a one-line description of it."""
    paragraphs = [part.strip("\n") for part in re.split(r"\n[ \t]*\n", inner.strip()) if part.strip()]
    if not paragraphs:
        return ""
    if len(paragraphs) > 1 and "\n" not in paragraphs[-1].strip() and len(paragraphs[-1]) <= 160:
        paragraphs = paragraphs[:-1]
    return _block_text("RUN", "", "\n\n".join(paragraphs))


def _foreign_call_to_block(inner):
    """Convert the inside of one non-@@@ tool call to an @@@ block ('' when it holds nothing to run)."""
    inner = SPECIAL_TOKEN_RE.sub(lambda m: "\x00" if "argument_begin" in m.group(0) else "", str(inner or "")).strip()
    if not inner:
        return ""
    if re.search(r"^\s*@@@\s*[A-Za-z_]+", inner, re.M):
        return ARG_TAG_RE.sub("", inner)
    pairs = [(item.group(1), item.group(2)) for item in ARG_PAIR_RE.finditer(inner)]
    if pairs:
        name = re.sub(r"^functions?\.|:\d+$", "", re.split(r"<arg_key>", inner, maxsplit=1, flags=re.I)[0].strip())
        block = _function_to_block(name, pairs)
        if block:
            return block
    inner = ARG_TAG_RE.sub("", inner).strip()
    if not inner:
        return ""
    if "\x00" in inner:
        name, _sep, argument = inner.partition("\x00")
        name = re.sub(r"^functions?\.|:\d+$", "", name.strip())
        try:
            data = json.loads(argument.strip())
        except ValueError:
            data = None
        if isinstance(data, dict):
            return _function_to_block(name, list(data.items()))
        inner = (name + " " + argument).strip()
    if inner.startswith("{"):
        try:
            data = json.loads(inner)
        except ValueError:
            data = None
        if isinstance(data, dict):
            name = data.get("name") or data.get("tool") or data.get("function")
            arguments = data.get("arguments") or data.get("parameters") or data.get("args") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = {"command": arguments}
            if isinstance(name, str) and isinstance(arguments, dict):
                return _function_to_block(name, list(arguments.items()))
    match = re.match(r"^([A-Za-z_]+)\b[ \t]*(.*)$", inner, re.S)
    if match and match.group(1).isupper():
        # Only an UPPERCASE first word is a verb: a lowercase 'ls' or 'cat' is a shell command, not LIST or READ.
        verb = FUNC_ALIASES.get(match.group(1), match.group(1))
        if verb in VERBS:
            first, _sep, rest = match.group(2).partition("\n")
            return _block_text(verb, first, rest)
    return _shell_block(inner)


def _fenced_tool_lines(text):
    """A code fence holding only 'RUN cmd' / 'READ path' lines is an action the model forgot to wrap in '@@@'."""
    if re.search(r"^\s*@@@\s*[A-Za-z_]+", text, re.M):
        return text

    def convert(match):
        lines = [line for line in match.group(1).split("\n") if line.strip()]
        found = [FENCED_TOOL_LINE_RE.match(line) for line in lines]
        if not lines or not all(found):
            return match.group(0)
        return "\n" + "\n".join(_block_text(item.group(1), item.group(2)) for item in found) + "\n"

    return FENCE_RE.sub(convert, text)


def normalize_function_calls(text):
    """Rewrite foreign tool-call syntaxes (<function=..>, <|tool_call_begin|>, <dots_function_call>, fenced RUN lines) as @@@ blocks."""
    text = str(text or "")
    if FOREIGN_MARKER_RE.search(text):
        def convert(match):
            params = [(item.group(1), item.group(2)) for item in FUNC_PARAM_RE.finditer(match.group(2))]
            return "\n" + _function_to_block(match.group(1), params) + "\n"

        text = FUNC_CALL_RE.sub(convert, text)
        if "DSML" in text:
            text = DSML_PREFIX_RE.sub(r"<\1", text)
        if "<invoke" in text.lower():
            def invoke(match):
                params = [(item.group(1), item.group(2)) for item in NAMED_PARAM_RE.finditer(match.group(2))]
                return "\n" + _function_to_block(match.group(1), params) + "\n"

            text = INVOKE_RE.sub(invoke, text)
        text = TOKEN_CALL_RE.sub(lambda m: "\n" + _foreign_call_to_block(m.group(1)) + "\n", text)
        text = TAG_CALL_RE.sub(lambda m: "\n" + _foreign_call_to_block(m.group(2)) + "\n", text)
        text = SPECIAL_TOKEN_RE.sub("", text)
    if "arg_" in text:
        text = ARG_TAG_RE.sub("", text)
    if "```" in text:
        text = _fenced_tool_lines(text)
    return text


def strip_markup(text):
    """Remove leftover tool-call tags, empty code fences and stray @@@ lines from text meant for people."""
    text = DSML_PREFIX_RE.sub(r"<\1", str(text or ""))
    text = SPECIAL_TOKEN_RE.sub("", WRAPPER_TAG_RE.sub("", text))
    lines = [line for line in text.split("\n") if not BLOCK_END_RE.match(line)]
    text = "\n".join(lines)
    text = re.sub(r"```[A-Za-z0-9_-]*[ \t]*\n?\s*```", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _bare_opener(lines, index):
    """A line like 'READ game.js' (the model forgot '@@@') that is closed by a later '@@@ END'."""
    match = re.match(r"^\s*([A-Z]{3,7})\b[ \t]*(.*)$", lines[index])
    if not match or match.group(1) not in VERBS:
        return None
    for probe in range(index + 1, len(lines)):
        if BLOCK_END_RE.match(lines[probe]):
            return match.group(1), match.group(2).strip()
        if BLOCK_START_RE.match(lines[probe]):
            return None
    return None


class Turn:
    """One parsed model reply."""

    def __init__(self, prose, actions, fabricated, clean):
        self.prose = prose
        self.actions = actions
        self.fabricated = fabricated
        self.clean = clean


def parse_turn(text):
    """Split a model reply into commentary, action blocks and fabricated-result detection.

    Everything after a line that imitates a tool result is invented by the model: it is cut off,
    so neither the fake result nor any action that depends on it is ever executed.
    """
    lines = normalize_function_calls(text).split("\n")
    prose, actions = [], []
    fabricated = False
    index = 0
    cut = len(lines)
    while index < len(lines):
        match = BLOCK_START_RE.match(lines[index])
        verb = args = None
        if match and match.group(1).upper() in VERBS:
            verb, args = match.group(1).upper(), match.group(2).strip()
        else:
            bare = _bare_opener(lines, index) if not match else None
            if bare:
                verb, args = bare
        if verb:
            body, closed = [], False
            index += 1
            while index < len(lines):
                if BLOCK_END_RE.match(lines[index]):
                    index += 1
                    closed = True
                    break
                nxt = BLOCK_START_RE.match(lines[index])
                if nxt and nxt.group(1).upper() in VERBS:
                    closed = True
                    break
                body.append(lines[index])
                index += 1
            actions.append(Action(verb, args, "\n".join(body), closed))
        elif FABRICATED_RE.match(lines[index]):
            fabricated = True
            cut = index
            break
        else:
            prose.append(lines[index])
            index += 1
    return Turn(strip_markup("\n".join(prose)), actions, fabricated, "\n".join(lines[:cut]).strip())


def parse_actions(text):
    """Split model output into (prose, [(verb, args, body)])."""
    turn = parse_turn(text)
    return turn.prose, [(item.verb, item.args, item.body) for item in turn.actions]


def display_text(raw):
    """What a person should read of a model reply: the commentary, never action blocks or tool tags."""
    return parse_turn(raw).prose


LIVE_LABELS = {
    "WRITE": "Writing {a}", "READ": "Reading {a}", "LIST": "Listing {a}", "RUN": "Preparing a command",
    "SEARCH": "Searching the web", "SAY": "Messaging {a}", "NOTE": "Writing a note", "DONE": "Writing the report",
    "BLOCKED": "Reporting a blocker", "PLAN": "Updating the plan", "SPAWN": "Setting up {a}",
    "ASSIGN": "Briefing {a}", "RETIRE": "Retiring {a}", "WAIT": "Waiting", "FINISH": "Writing the final summary",
}


def live_activity(raw):
    """Describe the action block a model is still streaming, for the 'now' line of the UI."""
    open_block = None
    for line in normalize_function_calls(raw).split("\n"):
        match = BLOCK_START_RE.match(line)
        if match and match.group(1).upper() in VERBS:
            open_block = (match.group(1).upper(), match.group(2).strip())
        elif BLOCK_END_RE.match(line):
            open_block = None
    if not open_block:
        return ""
    verb, args = open_block
    subject = clip(args.split("|")[0].strip() or "…", 60)
    return LIVE_LABELS.get(verb, verb.title()).format(a=subject)


# ---------------------------------------------------------------------------
# Degenerate output detection
# ---------------------------------------------------------------------------

_INLINE_RE = re.compile(r"(.{2,160}?)\1{7,}", re.S)
_RUN_RE = re.compile(r"(\S)\1{199,}")


def _line_loop(text):
    lines = text.split("\n")
    usable = lines[:-1]
    count = len(usable)
    if count < 2:
        return None
    offsets, pos = [], 0
    for line in usable:
        offsets.append(pos)
        pos += len(line) + 1
    for period in (1, 2, 3, 4):
        if count < period * 2:
            continue
        block = usable[count - period:]
        index = count - period
        repeats = 1
        while index - period >= 0 and usable[index - period:index] == block:
            repeats += 1
            index -= period
        size = sum(len(line) + 1 for line in block)
        if not any(line.strip() for line in block):
            hit = repeats * period >= 40
        elif size <= 4:
            hit = repeats >= 100
        else:
            hit = repeats >= 6 and repeats * size >= 240
        if hit:
            return offsets[index]
    return None


def _inline_loop(text):
    window = text[-900:]
    base = len(text) - len(window)
    best = None
    run = _RUN_RE.search(window)
    if run:
        best = run.start()
    if "\n" not in window[-300:]:
        for match in _INLINE_RE.finditer(window):
            if len(match.group(0)) >= 240:
                best = match.start() if best is None else min(best, match.start())
                break
    return None if best is None else base + best


def find_loop(text):
    """Offset in text where a degenerate repeating run begins, or None."""
    found = [value for value in (_line_loop(text), _inline_loop(text)) if value is not None]
    return min(found) if found else None


class LoopGuard:
    """Watches a stream of text and trips once it degenerates into endless repetition."""

    TAIL = 2400
    STEP = 120

    def __init__(self):
        self.tail = ""
        self.total = 0
        self.since = 0
        self.start = None

    def feed(self, chunk):
        if self.start is not None:
            return True
        self.tail = (self.tail + chunk)[-self.TAIL:]
        self.total += len(chunk)
        self.since += len(chunk)
        if self.since < self.STEP:
            return False
        self.since = 0
        offset = find_loop(self.tail)
        if offset is None:
            return False
        self.start = max(0, self.total - len(self.tail) + offset)
        return True


class Generation(tuple):
    """(reasoning, answer) plus a ``degenerate`` flag naming the stream that looped, if any."""

    def __new__(cls, reasoning, answer, degenerate=""):
        obj = super().__new__(cls, (reasoning, answer))
        obj.degenerate = degenerate
        return obj


def split_stray_think(reasoning, answer):
    """Handle models that emit only a closing think tag."""
    match = None
    for match in STRAY_CLOSE_RE.finditer(answer):
        pass
    if match:
        reasoning = (reasoning + "\n" + answer[:match.start()]).strip()
        answer = answer[match.end():]
    return reasoning.strip(), answer.strip()


class ThinkSplitter:
    def __init__(self):
        self.in_think = False
        self.pend = ""

    def feed(self, text, final=False):
        out = []
        self.pend += text
        while True:
            tag = THINK_TAG_RE.search(self.pend)
            if tag:
                before = self.pend[:tag.start()]
                if before:
                    out.append(("reasoning" if self.in_think else "text", before))
                self.in_think = not bool(tag.group(1))
                self.pend = self.pend[tag.end():]
                continue
            hold = ""
            if not final:
                cut = self.pend.rfind("<")
                if cut != -1 and ">" not in self.pend[cut:] and len(self.pend) - cut < 12:
                    hold = self.pend[cut:]
                    self.pend = self.pend[:cut]
            if self.pend:
                out.append(("reasoning" if self.in_think else "text", self.pend))
            self.pend = hold
            break
        return out


# ---------------------------------------------------------------------------
# Claims about files
# ---------------------------------------------------------------------------

CLAIM_FILE_RE = re.compile(
    r"(?<![\w/.:\\-])((?:[\w.-]+/)*[\w-][\w.-]*\.(?:py|js|jsx|mjs|ts|tsx|html|htm|css|scss|json|md|txt|sh|bat|java|c|cc|cpp|h|hpp|rs|go|rb|php|yml|yaml|toml|csv|sql|xml|svg|lua|cs|kt|swift))\b",
    re.I)
DOMAIN_RE = re.compile(r"\.(?:com|org|net|io|dev|app|ai|co|edu|gov|me|xyz)$", re.I)
KNOWN_LIBS = {"node", "three", "p5", "d3", "vue", "react", "next", "express", "chart", "phaser", "pixi", "jquery",
              "angular", "svelte", "nuxt", "backbone", "ember", "alpine", "htmx", "lodash", "moment", "anime", "gsap",
              "tone", "howler", "matter", "babylon", "konva", "fabric", "leaflet", "mocha", "jest", "socket"}


def claimed_files(text):
    names = []
    for match in CLAIM_FILE_RE.finditer(str(text or "")):
        name = match.group(1)
        first = name.split("/")[0]
        if "/" in name and DOMAIN_RE.search(first):
            continue
        if "/" not in name and name.rsplit(".", 1)[0].lower() in KNOWN_LIBS:
            continue
        names.append(name)
    return list(dict.fromkeys(names))


# ---------------------------------------------------------------------------
# Model gate
# ---------------------------------------------------------------------------

class ModelGate:
    """Lets many agent threads generate at once while holding the app's model lock.

    The lock is taken when the first generation starts and released when the
    last one ends, from one dedicated holder thread, so normal chats can run
    whenever every agent is idle, waiting, or backing off.
    """

    def __init__(self, model_lock, on_acquire=None, on_release=None):
        self.model_lock = model_lock
        self.on_acquire = on_acquire
        self.on_release = on_release
        self.cond = threading.Condition()
        self.active = 0
        self.held = False
        self.thread = None

    def _ensure_thread(self):
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._holder, name="kurt-agent-gate", daemon=True)
            self.thread.start()

    def enter(self, should_abort=None):
        with self.cond:
            self.active += 1
            self._ensure_thread()
            self.cond.notify_all()
            while not self.held:
                if should_abort and should_abort():
                    self.active -= 1
                    self.cond.notify_all()
                    raise StopRequested()
                self.cond.wait(0.5)

    def exit(self):
        with self.cond:
            self.active = max(0, self.active - 1)
            self.cond.notify_all()

    def _holder(self):
        while True:
            with self.cond:
                while self.active == 0:
                    if not self.cond.wait(timeout=30) and self.active == 0:
                        self.thread = None
                        return
            self.model_lock.acquire()
            try:
                if self.on_acquire:
                    try:
                        self.on_acquire()
                    except Exception:
                        pass
                with self.cond:
                    self.held = True
                    self.cond.notify_all()
                    while self.active > 0:
                        self.cond.wait()
                    self.held = False
                    if self.on_release:
                        try:
                            self.on_release()
                        except Exception:
                            pass
            finally:
                self.model_lock.release()


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

COMMON_SYNTAX = """
ACTION BLOCKS
You act by writing action blocks. A block looks like this:
@@@ VERB arguments
optional body
@@@ END
Use ONLY this @@@ block format. Never write <function=...>, <parameter=...>, <tool_call>, JSON tool calls or any other tag-based call; they are not executed. Every block starts with @@@ VERB and ends with @@@ END.
Plain text outside blocks is a one-line private note that appears in your own panel and is delivered to no one. To tell anyone anything, use SAY. Keep notes to a sentence or two.

Verbs available to every agent:
- SAY <target>   Body is the message. Targets: lead, all, a teammate's name{user_target}. Every message is visible to the whole team and to the user. Send one only when you need something or have a result worth sharing; no greetings, thanks or acknowledgements.
- NOTE <key>     Body is a fact, decision, interface contract, file map or result to keep in the shared notes that every teammate reads. Reusing a key overwrites it. Keep notes short and current.
- READ <path>    Read a text file. Paths resolve inside the project workspace.
- LIST <path>    List a folder. An empty path lists the project workspace.
- SEARCH <query> Search the web.
- WRITE <path>   Body is the complete new content of the file. Paths are relative to the project workspace. An existing file must be READ by you first.
- RUN            Body is one shell command, run in the project workspace. It runs without a terminal: no keyboard, no screen, and the command must finish by itself.
- RUN tty        For anything that needs a terminal (curses, input(), games, TUIs). Body is the command; optional extra line "KEYS: w a s d ENTER SPACE UP q" types those keys one by one. The program runs in a real 80x24 pseudo terminal and is stopped after about 10 seconds, and you get the final screen back. A game still running when it is stopped is normal: judge it by the screen. Typical test: RUN tty with "python3 game.py" and "KEYS: d d s q".

DELIVERABLE-FIRST RULES
- Identify the exact final result requested (for example a PNG, PDF, spreadsheet, website or source-code change) before doing the work. Optimise for that result, not for files that only help create it.
- Helper scripts, generated source files used only to create another deliverable, debug logs and intermediate assets are progress files, not final results. Put temporary work in a hidden .tmp/ folder and do not feature those files in your final report unless the human explicitly asked for source code.
- If the human asks for an image, create and verify the actual image file (.png, .jpg, etc.). Do not present the Python/JavaScript script that generated it as the result. Run the script, inspect the output, and report the image path. Apply this rule to every requested output format.
- Verify the final result exists, is non-empty, and has the requested format before claiming completion. Prefer opening or inspecting the actual result when the tools support it. If generation fails, say so instead of substituting an intermediate file.
- Keep final file lists focused on deliverables; do not mix progress files with the files the human needs to use.

GROUND RULES (the system checks these and refuses or cuts off violations)
- The WORKSPACE FILES list in the shared context is the live truth about what exists. Only read, edit or mention files that are listed there or that you have just written. Never invent file names, APIs, command output, test results or progress.
- Results of READ, LIST, SEARCH, RUN and WRITE are produced only by the system and arrive in your next message. Never write a result yourself ("TOOL RESULTS", "[RUN result]", "[READ ...]"), never guess what a file contains or what a command printed, and never say an action worked before its result says so. Anything you write after a pretend result is cut off and ignored.
- After your action blocks, stop and wait for the results. Send at most {max_actions} action blocks per reply, and do not repeat an action that already returned a result: use it or change approach.
- Stay inside the project workspace. Do not search the rest of the computer for files.
- If you do not know something, find out with READ, LIST or RUN. If you cannot find out, say so plainly instead of guessing.
- You DO have a terminal: RUN for normal commands, RUN tty for interactive programs. "No terminal", "cannot run interactively" or "needs a real terminal" is never a valid reason to be blocked. Use RUN tty, or test the logic headlessly (import the module and call its functions, simulate input).
"""

MODE_FINITE = """8. FINISHING. This is a normal run: the team stops when the work is done. When the goal is met and verified (every deliverable is listed in WORKSPACE FILES, a teammate other than the author tested or reviewed the final state and reported RESULT: PASS, no open blockers) write FINISH with the final report. If the goal is only a question or an analysis (nothing to build), answer it yourself with read-only READ, LIST, SEARCH and RUN, then SAY user and FINISH: no teammates, no installing or running the project, no new files unless the human asked. Do not invent extra work afterwards, and do not FINISH while any teammate is still working. The system checks these conditions and refuses an early FINISH; the FINISH CHECK line in the shared context shows whether you are ready."""

MODE_CONTINUOUS = """8. CONTINUOUS MODE. The human asked the team to keep running until they stop it. Never use FINISH. When a milestone is complete, define the next useful improvement (more tests, bug hunting, polish, performance, docs) and assign it. If there is truly nothing useful left to do, WAIT."""

LEAD_SYSTEM = """You are LEAD, the orchestrator of Kurt's agent team. You are always reachable: the human can message you at any moment and you answer them first.

The ONLY goal is the one under GOAL in the shared context. Everything the team does must serve it. Reject scope creep: no extra features, rewrites or side projects the human did not ask for.

How you run the team:
1. PLAN: turn the goal into a short ordered plan (3 to 7 steps, each with an owner and a status) and keep it current.
2. SPAWN only the specialists the goal truly needs (none for a question or analysis; usually 2 to 4 for a build; hard limit {max_agents}). Each mission states the goal, the files the agent owns, the interfaces it must respect and its definition of done.
3. Sequence dependent work. Do not start a tester or reviewer before there is something to test: spawn or ASSIGN them after the builder reports DONE, and give them the exact file names.
4. React to reports. DONE reports start with RESULT: PASS, FAIL or PARTIAL and give evidence. Accept PASS only when the evidence is concrete (files written, command output, specific lines checked). Send failures back to the owner with ASSIGN.
   A BLOCKED agent is YOUR problem to solve, and you never leave it stopped. Keep trying until it works:
   a. Diagnose first. Read its blocker and last actions, then reproduce the failing step yourself with RUN or READ and look at the real error (missing package, wrong path, bad command, unsupported API, malformed reply).
   b. Fix the cause (install the package, correct the path, write the helper script, find the right API) and ASSIGN the same agent a rewritten, smaller, concrete task that includes the exact commands and the fix.
   c. If it blocks again, change approach: try a different library or method, split the task in two, or RETIRE it and SPAWN a fresh specialist with a cleaner brief. You may also do the step yourself with RUN and WRITE.
   d. Ask the human only for something no tool can produce (credentials, a missing input file, a decision about what they want), and keep every other part of the work moving while you wait. Never report a technical obstacle to the human as the end result.
5. Verify with your own tools when it matters: LIST the workspace, READ the key files, RUN a check. Do not repeat a teammate's claim as fact until the live state (WORKSPACE FILES, team reports) supports it.
6. Message the human (SAY user) only when they asked something, at a real milestone, or when you are blocked on something only a human can give (a decision about what they want, credentials, files you cannot create). Never ask the human to advise on technical obstacles: pick a way around it yourself (for example have the tester use RUN tty, or test the logic headlessly) and ASSIGN it. Answer with real facts from the live team state, briefly, and never repeat a status you already gave. If their message contains an instruction, act on it and say what you changed. If the human sent several messages while you were busy, answer them together in ONE SAY user message after reading all of them; never send one reply per message.
7. Do the work through the team. Use READ, LIST, SEARCH and RUN to inspect, verify and debug; do a step yourself only when that is the fastest way to unblock someone.
{mode_block}

Lead-only verbs:
- SPAWN <name> | <role>   Body is the agent's mission: goal, files it owns, interfaces it must respect, definition of done, how to report back.
- ASSIGN <name>           Body is a new instruction or task for an existing agent.
- RETIRE <name>           Stop an agent permanently.
- PLAN                    Body replaces the shared plan (a short ordered list with owners and status).
- WAIT <seconds>          Sleep until the next review (5 to 900) unless a teammate reports sooner. Use it whenever the team is busy and nothing needs you.
- FINISH                  Body is the final report for the human: what was built, where the files are, how it was verified, known limits. Ends the run (normal mode only).
{syntax}"""

AGENT_SYSTEM = """You are {name}, a subagent on Kurt's agent team. Your role: {role}.
You work in parallel with teammates and share one context with them: the channel, the shared notes, the plan and the workspace file list are shown below and everyone sees the same thing.

Stay on task. Your task is under YOUR CURRENT TASK in the shared context. Do that and nothing else: no unrelated work, no redesigning the project, no extra requirements, no wandering through other folders. If your task depends on something that does not exist yet, do not guess and do not search elsewhere: report BLOCKED and say what you are waiting for.

How you work:
1. Look first. Check WORKSPACE FILES and READ what you depend on before you change anything.
2. Work in small steps with tools. Own your files; ask the owner (SAY <name>) before changing someone else's.
3. Verify your own work with tools (READ the file back, RUN a check) before you report. A claim without evidence is rejected.
4. Publish contracts, file maps and key decisions with NOTE so teammates stay in sync.
5. Report to lead, never to the human. Messages are short and only when you need something or have a result.

Finishing your task (pick exactly one, in a reply of its own, after you have seen the results of your last action):
- DONE    Body: first line "RESULT: PASS" (everything asked is complete and works), "RESULT: FAIL" (you found problems; list them) or "RESULT: PARTIAL". Then what you did, the files you touched and the evidence (commands run and what they printed, lines you checked). The system rejects DONE without real tool use and DONE that mentions files that do not exist.
- BLOCKED Body: what stops you and exactly what you need. Last resort: first read the error, fix your command and try at least two different approaches (another library, a smaller step, a helper script). The lead will debug the blocker with you, so include the exact error text and what you already tried.
Do not repeat yourself or chat for its own sake. If you have nothing to do, finish with DONE or BLOCKED.
Extra verbs: DONE and BLOCKED, as described above.
{syntax}"""

QUICK_SYSTEM = """You are LEAD. The human wrote while your main orchestration loop was busy. Send only a short acknowledgement in one or two sentences: say you got the message and will answer in full as soon as the current step finishes. Do not answer the question, give a status report or list details; the full answer comes later in one reply. If they gave an instruction, say it will be applied. Do not use action blocks."""

DEGENERATE_NOTICE = ("SYSTEM: your previous reply got stuck repeating the same text and was discarded; nothing in it was executed. "
                     "Reply again with a short, fresh reply: one or two action blocks at most. If you were writing a long file, write a smaller first part.")
FABRICATED_NOTICE = ("[SYSTEM] Your reply contained text that imitated a tool result. Everything from that point on was removed and NOT executed. "
                     "Results only come from the system, in the next message: send your action blocks and then stop.")
NUDGE = ("NO ACTION RECEIVED. Your reply contained no action block the system could run, so nothing happened. Reply with an action block now, "
         "written exactly like this (no <|tool_call|> tokens, no <function> tags, no code fence instead of the block):\n"
         "@@@ RUN\nls -la\n@@@ END\n"
         "Use READ, LIST, WRITE, RUN or SEARCH to keep working on your task, SAY to ask a teammate, DONE (with RESULT and evidence) if your task is complete, "
         "or BLOCKED if you cannot continue.")
RECOVERY_FORMAT = "Write every action as an @@@ block, for example:\n@@@ RUN\npython3 script.py\n@@@ END"
RECOVERY_HINTS = (
    ("without taking any action", "The system could not find a valid action block in your replies, so nothing ran. " + RECOVERY_FORMAT),
    ("repeating the same action", "Do not run the same action again. Read its output, fix the real cause (wrong path, missing module, syntax error, wrong API) "
                                  "or take a different route, such as a smaller step or a helper script."),
    ("repeated text", "Keep replies short: one or two action blocks at a time, and split long files into smaller parts."),
)


class Agent:
    def __init__(self, name, role, task="", lead=False):
        self.name = name
        self.role = role
        self.task = task
        self.lead = lead
        self.status = "idle"
        self.current = ""
        self.cur_emit = 0.0
        self.summary = ""
        self.turns = 0
        self.retry_at = 0.0
        self.retired = False
        self.busy = False
        self.history = []
        self.inbox = []
        self.entries = deque(maxlen=MAX_ENTRIES)
        self.wake = threading.Event()
        self.thread = None
        self.heartbeat = LEAD_HEARTBEAT
        self.files = {}
        self.seen = {}
        self.last_report_ts = 0.0
        self.last_result = ""
        self.blocked_reason = ""
        self.last_activity = time.time()
        self.temperature = None
        self.suppress_think = False
        self.recoveries = 0
        self.restarted = False
        self.last_answer = ""
        self.carry = ""
        self.reset_task()

    def reset_task(self):
        self.evidence = 0
        self.done_rejects = 0
        self.strikes = 0
        self.nudges = 0
        self.loop_warnings = 0
        self.recent = deque(maxlen=8)
        self.blocked_reason = ""
        self.last_result = ""


class AgentSwarm:
    def __init__(self, host, chat_id, route_info, restore=None):
        self.host = host
        self.chat_id = chat_id
        self.route_info = route_info
        self.candidates = route_info.get("candidates") or [route_info]
        self.active_candidate = 0
        self.lock = threading.RLock()
        self.epoch = uuid.uuid4().hex[:12]
        self.stop_event = threading.Event()
        self.events = deque(maxlen=MAX_EVENTS)
        self.seq = 0
        self.channel = []
        self._ack_pending = False
        self.activity = deque(maxlen=MAX_ACTIVITY)
        self.notes = {}
        self.plan = ""
        self.goal = ""
        self.agents = {}
        self.pending = {}
        self.cooldown_until = 0.0
        self.cooldown_reason = ""
        self.failure_streak = 0
        self.lead_trace = []
        self.lead_think_seconds = 0.0
        self.file_lock = threading.Lock()
        self.last_persist = 0.0
        self.started_at = time.time()
        self.user_msg_count = 0
        self.recent_user_posts = []
        self.continuous = False
        self.finished = False
        self.finish_summary = ""
        self.finish_rejects = 0
        self.idle_paused = False
        self.idle_reviews = 0
        self.block_reviews = 0
        self.review_signature = None
        self.user_paths = set()
        self.file_state = {}
        self.terminals = []
        self.title_pending = False
        self.last_change_ts = 0.0
        self.last_writer = ""
        self.sent_log = {}
        self.streaks = {}
        self.scan_lock = threading.Lock()
        self.scan_at = 0.0
        self.scan_cache = ([], False)
        self.lead = Agent("lead", "Lead orchestrator", lead=True)
        self.agents["lead"] = self.lead
        self.restored = bool(restore)
        self.restored_finished = False
        if restore:
            self._restore(restore)

    @property
    def running(self):
        return not self.stop_event.is_set()

    @property
    def model_name(self):
        return self.candidates[min(self.active_candidate, len(self.candidates) - 1)].get("model") or ""

    def _restore(self, data):
        self.goal = str(data.get("goal") or "")
        self.plan = str(data.get("plan") or "")
        self.continuous = bool(data.get("continuous"))
        self.restored_finished = bool(data.get("finished"))
        notes = data.get("notes")
        if isinstance(notes, dict):
            for key, item in list(notes.items())[:200]:
                if isinstance(item, dict):
                    self.notes[str(key)[:60]] = {"value": str(item.get("value") or ""), "by": str(item.get("by") or ""),
                                                 "ts": float(item.get("ts") or time.time())}
        channel = data.get("channel")
        if isinstance(channel, list):
            self.channel = [item for item in channel if isinstance(item, dict)][-MAX_CHANNEL:]
        activity = data.get("activity")
        if isinstance(activity, list):
            for item in activity[-200:]:
                if isinstance(item, dict):
                    self.activity.append(item)
        for item in data.get("agents") or []:
            if not isinstance(item, dict) or item.get("name") == "lead" or item.get("retired"):
                continue
            name = slug(item.get("name"))
            agent = Agent(name, str(item.get("role") or "Agent")[:120], str(item.get("task") or ""))
            agent.summary = str(item.get("summary") or "")
            agent.last_result = str(item.get("result") or "")
            agent.status = "working" if item.get("status") in ("working", "retrying", "waiting") and not self.restored_finished else "idle"
            self.agents[name.lower()] = agent
        for row in data.get("files") or []:
            if isinstance(row, dict) and row.get("path"):
                self.file_state[str(row["path"])] = {"by": str(row.get("by") or ""), "ts": float(row.get("ts") or 0),
                                                     "size": int(row.get("size") or 0), "rel": str(row["path"]),
                                                     "output": bool(row.get("output")), "mime": str(row.get("mime") or ""),
                                                     "area": str(row.get("area") or "work")}
        terminals = data.get("terminals")
        if isinstance(terminals, list):
            self.terminals = [dict(item) for item in terminals if isinstance(item, dict)][-8:]
        self.lead.summary = str((data.get("lead") or {}).get("summary") or "")

    def start(self, first_message, message_id="", continuous=None):
        self._trace("start", model=self.model_name, route=self.route_info.get("route"),
                    candidates=[item.get("member_label") or item.get("model") for item in self.candidates],
                    restored=self.restored, restored_finished=self.restored_finished, continuous=continuous,
                    team=[agent.name for agent in self.agents.values()], first_message=first_message)
        self.lead.thread = threading.Thread(target=self._lead_loop, name="kurt-agent-lead", daemon=True)
        self.lead.thread.start()
        for agent in list(self.agents.values()):
            if not agent.lead:
                self._launch(agent, resumed=True)
        if self.restored and not self.restored_finished:
            self.lead.inbox.append({"from": "system", "text": "Agent mode was restarted. Review the restored plan, notes, channel and team, then continue the work.", "ts": time.time()})
        self._emit("state", running=True)
        self.user_message(first_message, message_id, continuous)

    def stop(self, final_status="stopped", finished=False):
        if finished:
            self.finished = True
        self.stop_event.set()
        for agent in list(self.agents.values()):
            agent.wake.set()
        for approval_id in list(self.pending):
            with self.host.lock:
                pending = self.host.pending_approvals.get(approval_id)
                if pending and pending.get("approved") is None:
                    pending["approved"] = False
                    pending["event"].set()
        for agent in list(self.agents.values()):
            if agent.status != "retired":
                self._set_status(agent, final_status, current="")
        self._emit("state", running=False, finished=self.finished)
        self._persist(force=True)

    # -- events ----------------------------------------------------------

    def _trace(self, event, agent=None, **fields):
        recorder = getattr(self.host, "trace", None)
        if recorder is None:
            return
        try:
            if agent is not None:
                fields["agent"] = getattr(agent, "name", str(agent))
            recorder(self.chat_id, "agent." + event, **fields)
        except Exception:
            pass

    def _emit(self, kind, **payload):
        with self.lock:
            self.seq += 1
            event = {"seq": self.seq, "ts": round(time.time(), 3), "type": kind}
            event.update(payload)
            self.events.append(event)
        if kind not in ("entry", "agent"):
            self._trace("event." + kind, seq=event["seq"], **payload)
        if kind != "entry":
            self._persist()

    def _entry_view(self, entry, limit=EVENT_TEXT_LIMIT):
        return {"id": entry["id"], "kind": entry["kind"], "text": clip_tail(entry["text"], limit),
                "ts": entry["ts"], "done": entry["done"]}

    def _entry_open(self, agent, kind, text=""):
        entry = {"id": uuid.uuid4().hex[:10], "kind": kind, "text": str(text), "ts": time.time(), "done": False, "emitted": 0.0}
        with self.lock:
            agent.entries.append(entry)
        self._entry_emit(agent, entry, force=True)
        return entry

    def _entry_emit(self, agent, entry, force=False, done=False):
        now = time.time()
        agent.last_activity = now
        if done:
            entry["done"] = True
            if not entry.get("traced"):
                entry["traced"] = True
                self._trace("entry", agent, entry_kind=entry["kind"], entry_id=entry["id"], text=entry["text"],
                            seconds=round(now - entry["ts"], 3))
        if not (force or done) and now - entry["emitted"] < 0.4:
            return
        entry["emitted"] = now
        self._emit("entry", agent=agent.name, entry=self._entry_view(entry))

    def _entry_set(self, agent, entry, text, done=True):
        entry["text"] = str(text)
        self._entry_emit(agent, entry, force=True, done=done)

    def _note_entry(self, agent, kind, text):
        entry = self._entry_open(agent, kind, text)
        self._entry_emit(agent, entry, done=True)
        return entry

    def _trace_lead_thinking(self, reasoning, seconds):
        text = str(reasoning or "").strip()
        if not text:
            return
        with self.lock:
            self.lead_trace.append({"kind": "thinking", "text": "Thinking", "ok": None, "detail": clip_tail(text, 8000)})
            del self.lead_trace[:-40]
            self.lead_think_seconds += max(0.0, float(seconds))

    def _take_lead_trace(self):
        with self.lock:
            items, seconds = list(self.lead_trace), self.lead_think_seconds
            self.lead_trace, self.lead_think_seconds = [], 0.0
        return items, seconds

    def _activity(self, agent, kind, text, detail="", ok=None):
        item = {"id": uuid.uuid4().hex[:10], "ts": round(time.time(), 3), "agent": agent.name if agent else "system",
                "kind": kind, "text": clip(text, 260), "detail": clip(detail, 2500), "ok": ok}
        with self.lock:
            self.activity.append(item)
            if agent is not None and agent.lead and kind in ("run", "read", "write", "list", "search"):
                self.lead_trace.append({"text": item["text"], "detail": item["detail"], "ok": ok})
                del self.lead_trace[:-40]
        if agent is not None:
            agent.last_activity = time.time()
        self._emit("activity", item=item)
        return item

    def _set_status(self, agent, status, task=None, current=None):
        previous = agent.status
        changed = previous != status
        if changed:
            self._trace("status", agent, status=status, previous=previous, task=task if task is not None else agent.task,
                        current=current if current is not None else agent.current)
        agent.status = status
        if task is not None:
            agent.task = task
            changed = True
        if current is not None and current != agent.current:
            agent.current = current
            agent.cur_emit = time.time()
            changed = True
        agent.last_activity = time.time()
        if changed:
            self._emit("agent", agent=self._agent_view(agent, entries=0))

    def _set_current(self, agent, text, force=False):
        text = clip(text, 140)
        now = time.time()
        if text == agent.current or not (force or now - agent.cur_emit > 0.5):
            return
        agent.current = text
        agent.cur_emit = now
        self._emit("agent", agent=self._agent_view(agent, entries=0))

    def _agent_view(self, agent, entries=25, text_cap=4000):
        with self.lock:
            items = list(agent.entries)[-entries:] if entries else []
            files = list(agent.files)[-12:]
        return {"name": agent.name, "role": agent.role, "task": clip(agent.task, 600), "status": agent.status,
                "current": agent.current, "summary": clip(agent.summary, 800), "turns": agent.turns,
                "retry_at": agent.retry_at, "lead": agent.lead, "files": files, "result": agent.last_result,
                "blocked": clip(agent.blocked_reason, 400), "report_ts": agent.last_report_ts,
                "strikes": agent.strikes, "recoveries": agent.recoveries, "entries": [self._entry_view(item, text_cap) for item in items]}

    def files_view(self):
        with self.lock:
            rows = [{"path": item.get("rel") or key, "by": item.get("by", ""), "ts": item.get("ts", 0), "size": item.get("size", 0),
                     "output": bool(item.get("output")), "mime": item.get("mime", ""), "area": item.get("area", "work")}
                    for key, item in self.file_state.items()]
        rows.sort(key=lambda row: row["ts"], reverse=True)
        return rows[:200]

    def snapshot(self):
        with self.lock:
            agents = [self._agent_view(agent) for agent in self.agents.values() if agent.status != "retired"]
            channel = list(self.channel)[-150:]
            notes = {key: {"value": clip(item["value"], 3000), "by": item["by"], "ts": item["ts"]} for key, item in self.notes.items()}
            approvals = list(self.pending.values())
            activity = list(self.activity)[-300:]
            return {"running": self.running, "finished": self.finished, "finish_summary": self.finish_summary,
                    "continuous": self.continuous, "idle_paused": self.idle_paused, "goal": self.goal, "plan": self.plan,
                    "notes": notes, "channel": channel, "activity": activity, "files": self.files_view(), "agents": agents,
                    "approvals": approvals, "terminals": list(self.terminals)[-8:],
                    "cooldown_until": self.cooldown_until, "cooldown_reason": self.cooldown_reason,
                    "model": self.model_name, "seq": self.seq, "started_at": self.started_at, "now": time.time(),
                    "chat": chat_history(self.host, self.chat_id)}

    def events_since(self, after, epoch=""):
        with self.lock:
            first = self.events[0]["seq"] if self.events else self.seq + 1
            if epoch != self.epoch or after < 0 or after < first - 1 or after > self.seq:
                return {"seq": self.seq, "epoch": self.epoch, "running": self.running, "snapshot": self.snapshot(), "events": []}
            events = [event for event in self.events if event["seq"] > after][:800]
            seq = events[-1]["seq"] if events else self.seq
            return {"seq": seq, "epoch": self.epoch, "running": self.running, "finished": self.finished,
                    "continuous": self.continuous, "idle_paused": self.idle_paused, "events": events,
                    "now": time.time(), "cooldown_until": self.cooldown_until, "cooldown_reason": self.cooldown_reason}

    def state_for_persistence(self):
        with self.lock:
            agents = []
            for agent in self.agents.values():
                if agent.lead:
                    continue
                agents.append({"name": agent.name, "role": agent.role, "task": clip(agent.task, 1500), "status": agent.status,
                               "summary": clip(agent.summary, 1500), "retired": agent.retired, "result": agent.last_result})
            notes = {key: {"value": clip(item["value"], 4000), "by": item["by"], "ts": item["ts"]} for key, item in self.notes.items()}
            lead_entries = [self._entry_view(item, 1500) for item in list(self.lead.entries)[-8:]]
            return {"goal": self.goal, "plan": self.plan, "notes": notes, "channel": list(self.channel)[-120:],
                    "activity": list(self.activity)[-120:], "agents": agents, "running": self.running,
                    "finished": self.finished, "finish_summary": self.finish_summary, "continuous": self.continuous,
                    "files": self.files_view(), "terminals": list(self.terminals)[-8:], "updated": time.time(),
                    "lead": {"summary": self.lead.summary, "entries": lead_entries}}

    def _persist(self, force=False):
        now = time.time()
        if not force and now - self.last_persist < PERSIST_INTERVAL:
            return
        self.last_persist = now
        try:
            state = self.state_for_persistence()
            chat = self.host.get_chat(self.chat_id)
            if chat is not None:
                with self.host.lock:
                    chat["agent"] = state
                    chat["updated"] = time.time()
                self.host.save_chats()
        except Exception:
            pass

    def _find(self, name):
        key = str(name or "").strip().lower()
        with self.lock:
            agent = self.agents.get(key)
        return agent if agent and agent.status != "retired" else None

    # -- workspace knowledge ----------------------------------------------

    def _work_root(self):
        try:
            return Path(self.host.chat_work_root(self.chat_id)).resolve()
        except Exception:
            return None

    def _scan(self, fresh=False):
        """Bounded walk of the project workspace: ([(relative path, size, mtime_ns)], truncated)."""
        now = time.time()
        with self.scan_lock:
            if not fresh and now - self.scan_at < 1.0:
                return self.scan_cache
            root = self._work_root()
            items, truncated = [], False
            if root is not None and root.is_dir():
                skip = {"node_modules", "__pycache__", "venv", "env", "site-packages", "dist", "build", "target"}
                for dirpath, dirnames, filenames in os.walk(str(root)):
                    rel_dir = os.path.relpath(dirpath, str(root))
                    depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
                    dirnames[:] = [] if depth >= SCAN_DEPTH else sorted(d for d in dirnames if not d.startswith(".") and d not in skip)
                    for name in sorted(filenames):
                        if name.startswith("."):
                            continue
                        full = os.path.join(dirpath, name)
                        try:
                            stat = os.lstat(full)
                        except OSError:
                            continue
                        rel = name if rel_dir == "." else os.path.join(rel_dir, name)
                        items.append((rel.replace(os.sep, "/"), stat.st_size, stat.st_mtime_ns))
                        if len(items) >= FILE_SCAN_LIMIT:
                            truncated = True
                            break
                    if truncated:
                        break
            self.scan_cache = (items, truncated)
            self.scan_at = time.time()
            return self.scan_cache

    def workspace_files_text(self, limit=FILE_LIST_LIMIT):
        items, truncated = self._scan()
        if not items:
            return "(none yet: the project workspace is empty. Nothing has been created, so do not assume any file exists.)"
        written = {}
        with self.lock:
            for key, info in self.file_state.items():
                written[info.get("rel") or key] = info.get("by", "")
        rows = []
        for rel, size, _mtime in items[:limit]:
            by = " by {}".format(written[rel]) if rel in written else ""
            rows.append("- ./{} ({} bytes{})".format(rel, size, by))
        if len(items) > limit or truncated:
            rows.append("- … more files not shown")
        return "\n".join(rows)

    def _file_exists(self, name):
        root = self._work_root()
        if root is None:
            return True
        try:
            if (root / name).exists():
                return True
        except OSError:
            return True
        items, truncated = self._scan(fresh=True)
        base = name.rsplit("/", 1)[-1].lower()
        wanted = name.lower()
        for rel, _size, _mtime in items:
            low = rel.lower()
            if low == wanted or low.endswith("/" + wanted) or low.rsplit("/", 1)[-1] == base:
                return True
        return truncated

    def missing_claimed_files(self, text):
        return [name for name in claimed_files(text) if not self._file_exists(name)]

    def _signature(self):
        items, _truncated = self._scan(fresh=True)
        return tuple(items)

    def _rel(self, path):
        root = self._work_root()
        try:
            return Path(path).resolve().relative_to(root).as_posix() if root is not None else str(path)
        except ValueError:
            return str(path)

    def _within_scope(self, path):
        try:
            path = Path(path).resolve()
        except OSError:
            return False
        roots = []
        root = self._work_root()
        if root is not None:
            roots.append(root)
        try:
            roots.append(Path(self.host.chat_workspace(self.chat_id)).resolve())
        except Exception:
            pass
        for base in roots:
            try:
                path.relative_to(base)
                return True
            except ValueError:
                continue
        text = str(path)
        return any(text == allowed or text.startswith(allowed.rstrip("/\\") + os.sep) for allowed in self.user_paths)

    def _remember_user_paths(self, text):
        for token in re.findall(r"(?:~|/|[A-Za-z]:[\\/])[^\s\"'<>|`]*", str(text or "")):
            token = token.rstrip(".,;:)]}")
            if len(token) < 3:
                continue
            try:
                self.user_paths.add(str(Path(token).expanduser().resolve()))
            except (OSError, RuntimeError):
                continue

    # -- messaging --------------------------------------------------------

    def post(self, sender, target, text, kind="message"):
        message, _reason = self._post(sender, target, text, kind)
        return message

    def _norm(self, text):
        return " ".join(re.sub(r"[^a-z0-9 ]+", " ", str(text).lower()).split())

    def _post(self, sender, target, text, kind="message", checked=False):
        """Deliver a message. With ``checked`` duplicates, floods and ping-pong are suppressed: (message|None, reason)."""
        text = clip(str(text or "").strip(), 4000)
        if not text:
            return None, "empty"
        target = str(target or "all").strip()
        lowered = target.lower()
        if lowered in ("user", "human") and sender != "lead":
            lowered = "lead"
            text = "(for the user) " + text
        recipient = None
        if lowered in ("all", "everyone", "team", "*"):
            lowered = "all"
        elif lowered in ("user", "human"):
            lowered = "user"
        else:
            recipient = self._find(lowered)
            if recipient is None:
                text = "(undeliverable: no active agent named '{}') {}".format(target, text)
                lowered = "lead" if sender != "lead" else "all"
                recipient = self._find(lowered) if lowered != "all" else None
        if lowered == "user" and kind != "quick" and self._is_repeat_for_user(text):
            return None, "That message repeats what the human was already told."
        if checked and sender not in ("user", "system"):
            reason = self._chatter_block(sender, lowered, text)
            if reason:
                return None, reason
        message = {"id": uuid.uuid4().hex[:10], "ts": round(time.time(), 3), "from": sender, "to": lowered, "text": text, "kind": kind}
        with self.lock:
            self.channel.append(message)
            if len(self.channel) > MAX_CHANNEL:
                del self.channel[:-MAX_CHANNEL]
        self._emit("channel", message=message)
        if lowered == "user":
            self._deliver_to_user(text, sender, attach=kind != "quick")
        elif lowered == "all":
            if sender != "lead":
                self.lead.inbox.append(message)
                self.lead.wake.set()
        elif recipient is not None:
            recipient.inbox.append(message)
            recipient.wake.set()
        return message, ""

    def _chatter_block(self, sender, target, text):
        """Reason a team message must be suppressed (duplicate, flood or ping-pong), else ''."""
        now = time.time()
        norm = self._norm(text)
        with self.lock:
            log = [item for item in self.sent_log.get(sender, []) if now - item[0] < 600]
            for _ts, earlier_target, earlier in log[-6:]:
                if earlier_target == target and self._same_message(norm, earlier):
                    self.sent_log[sender] = log
                    return "That duplicates a message you already sent. Do not repeat yourself: act on your task or wait for a reply."
            if len([item for item in log if now - item[0] < POST_WINDOW]) >= POST_LIMIT:
                self.sent_log[sender] = log
                return "You are sending too many messages. Stop messaging and do your task, or report DONE / BLOCKED."
            if target not in ("all", "user", "lead") and target != sender:
                key = frozenset((sender, target))
                if self.streaks.get(key, 0) >= PINGPONG_LIMIT:
                    self.sent_log[sender] = log
                    return ("You and {} have exchanged {} messages without either of you doing any work. Stop chatting: act on your task, "
                            "or report DONE / BLOCKED to lead.").format(target, PINGPONG_LIMIT)
                self.streaks[key] = self.streaks.get(key, 0) + 1
            log.append((now, target, norm))
            self.sent_log[sender] = log[-30:]
        return ""

    @staticmethod
    def _same_message(norm, earlier):
        """Near-identical text. Messages whose numbers differ ("bug 3" / "bug 4") are different messages."""
        if norm == earlier:
            return True
        if re.findall(r"\d+", norm) != re.findall(r"\d+", earlier):
            return False
        return difflib.SequenceMatcher(None, norm, earlier).ratio() >= DUPLICATE_RATIO

    def _note_work(self, agent):
        with self.lock:
            for key in [key for key in self.streaks if agent.name in key]:
                self.streaks[key] = 0

    def _is_repeat_for_user(self, text):
        norm = self._norm(text)
        with self.lock:
            recent = [item for item in self.recent_user_posts if item[0] == self.user_msg_count][-4:]
            for _count, earlier in recent:
                if norm == earlier or difflib.SequenceMatcher(None, norm, earlier).ratio() >= 0.8:
                    return True
            self.recent_user_posts.append((self.user_msg_count, norm))
            del self.recent_user_posts[:-12]
        return False

    def _deliver_to_user(self, text, sender="lead", attach=True):
        chat = self.host.get_chat(self.chat_id)
        record = {"role": "assistant", "content": text, "model": self.model_name, "route": self.route_info.get("route", ""),
                  "agent_mode": True, "agent": sender, "message_id": uuid.uuid4().hex}
        if attach and sender == "lead":
            worklog, seconds = self._take_lead_trace()
            if worklog:
                record["worklog"] = worklog
                record["reasoning"] = "\n\n".join(item["detail"] for item in worklog if item.get("kind") == "thinking")
                record["thinking_seconds"] = round(seconds, 1)
        elif not attach:
            record["quick"] = True
        if chat is not None:
            with self.host.lock:
                chat["messages"].append(record)
                chat["updated"] = time.time()
            self.host.save_chats()
        self._emit("chat", message=record)

    def set_continuous(self, value, by="user"):
        value = bool(value)
        if value == self.continuous:
            return
        self.continuous = value
        self._emit("mode", continuous=value)
        self._activity(None, "mode", "Continuous mode turned {}".format("on: the team keeps working until you stop it" if value else "off: the team stops when the lead finishes the project"))
        self.idle_paused = False
        self.idle_reviews = 0
        notice = ("MODE CHANGED by the human: CONTINUOUS is now ON. Do not FINISH; keep improving the work until the human stops you." if value else
                  "MODE CHANGED by the human: CONTINUOUS is now OFF. Finish the project once it is complete and verified, then use FINISH.")
        self.lead.inbox.append({"from": "system", "text": notice, "ts": time.time()})
        self.lead.wake.set()
        self._persist(force=True)

    def _generate_title_for_prompt(self, text):
        entered = False
        try:
            self.host.agent_gate.enter()
            entered = True
            title = self.host.generate_chat_title(text, "", self.model_name)
        except Exception:
            title = ""
        finally:
            if entered:
                self.host.agent_gate.exit()
        if title:
            chat = self.host.get_chat(self.chat_id)
            if chat is not None:
                with self.host.lock:
                    changed = chat.get("title") in (None, "", "New conversation")
                    if changed:
                        chat["title"] = title
                        chat["updated"] = time.time()
                if changed:
                    self.host.save_chats()
                    self._emit("title", title=title)
        with self.lock:
            self.title_pending = False

    def user_message(self, text, message_id="", continuous=None):
        text = str(text or "").strip()
        if not text:
            return
        chat = self.host.get_chat(self.chat_id)
        record = {"role": "user", "content": text, "agent_mode": True, "message_id": message_id or uuid.uuid4().hex}
        create_title = False
        if chat is not None:
            with self.host.lock:
                chat["messages"].append(record)
                chat["updated"] = time.time()
                if chat.get("title") in (None, "", "New conversation") and not self.title_pending:
                    self.title_pending = True
                    create_title = True
            self.host.save_chats()
        if create_title:
            threading.Thread(target=self._generate_title_for_prompt, args=(text,),
                             name="kurt-agent-title", daemon=True).start()
        if not self.goal:
            self.goal = clip(text, 4000)
        elif self.restored_finished and self.restored:
            self.goal = clip("{}\n\n(Earlier, already completed: {})".format(text, clip(self.goal, 600)), 4000)
            self.restored_finished = False
        self._remember_user_paths(text)
        with self.lock:
            self.user_msg_count += 1
        if continuous is None:
            continuous = detect_continuous(text)
        if continuous is not None:
            self.set_continuous(continuous)
        self.idle_paused = False
        self.idle_reviews = 0
        message = {"id": uuid.uuid4().hex[:10], "ts": round(time.time(), 3), "from": "user", "to": "lead", "text": text, "kind": "user"}
        with self.lock:
            self.channel.append(message)
        self._emit("chat", message=record)
        self._emit("channel", message=message)
        busy = self.lead.busy
        send_ack = False
        if busy:
            with self.lock:
                send_ack = not self._ack_pending
                self._ack_pending = True
            message["acknowledged"] = True
        self.lead.inbox.append(message)
        self.lead.wake.set()
        if send_ack:
            threading.Thread(target=self._quick_reply, args=(message,), name="kurt-agent-quick", daemon=True).start()

    def _wait(self, seconds):
        return not self.stop_event.wait(seconds)

    def _wait_cooldown(self, agent):
        while not self.stop_event.is_set():
            remaining = self.cooldown_until - time.time()
            if remaining <= 0:
                return
            agent.retry_at = self.cooldown_until
            if agent.status != "retrying":
                self._set_status(agent, "retrying", current="Waiting to retry the model request")
            self.stop_event.wait(min(1.0, remaining))
        raise StopRequested()

    def _on_failure(self, agent, exc, entry=None):
        detail = clip(str(exc), 600)
        reason = "rate limit" if RATE_LIMIT_RE.search(detail) else "error"
        with self.lock:
            wait = RETRY_SECONDS if reason == "rate limit" else RETRY_STEPS[min(self.failure_streak, len(RETRY_STEPS) - 1)]
            self.failure_streak += 1
        self._trace("model_failure", agent, reason=reason, retry_in_seconds=wait, error=exc)
        with self.lock:
            fresh = self.cooldown_until < time.time() + 2
            self.cooldown_until = max(self.cooldown_until, time.time() + wait)
            self.cooldown_reason = reason
            agent.retry_at = self.cooldown_until
        self._note_entry(agent, "error", "The model request hit a {}: {}\nRetrying in {} seconds.".format(reason, detail, wait))
        if fresh:
            self._activity(agent, "alert", "The model request hit a {}. All agents wait {} seconds, then retry.".format(reason, wait), detail=detail, ok=False)
        self._set_status(agent, "retrying", current="Model {}: retrying soon".format(reason))
        self._emit("state", running=True, cooldown_until=self.cooldown_until, cooldown_reason=reason)

    # -- generation -------------------------------------------------------

    def _generate(self, agent, messages, max_tokens=GENERATION_TOKENS, entries=True, think=True, temperature=None):
        """Return a Generation (reasoning, answer). Retries forever with a growing pause after each failure."""
        while True:
            if self.stop_event.is_set():
                raise StopRequested()
            self._wait_cooldown(agent)
            try:
                result = self._generate_once(agent, messages, max_tokens, entries, think, temperature)
                self.failure_streak = 0
                return result
            except StopRequested:
                raise
            except Exception as exc:
                self._on_failure(agent, exc)

    def _generate_once(self, agent, messages, max_tokens, entries, think=True, temperature=None):
        host = self.host
        count = len(self.candidates)
        order = [(self.active_candidate + step) % count for step in range(count)]
        last_error = None
        for position, candidate_index in enumerate(order):
            candidate = self.candidates[candidate_index]
            empty_retry = False
            while True:
                reasoning_entry = output_entry = None
                reasoning_parts, answer_parts = [], []
                guards = {"reasoning": LoopGuard(), "answer": LoopGuard()}
                degenerate = ""
                last_view = 0.0
                started = time.monotonic()
                in_chars = sum(len(str(item.get("content") or "")) for item in messages if isinstance(item, dict))
                splitter = ThinkSplitter()
                stream = None
                entered = False
                try:
                    host.agent_gate.enter(self.stop_event.is_set)
                    entered = True
                    if agent.status != "working" and not agent.lead:
                        self._set_status(agent, "working")
                    if entries:
                        self._set_current(agent, "Thinking…", force=True)
                    call_messages = list(messages)
                    if empty_retry:
                        call_messages.append({"role": "system", "content": "Your previous reply was empty. Reply now with visible text or action blocks."})
                    last_content = call_messages[-1].get("content") if call_messages and isinstance(call_messages[-1], dict) else ""
                    self._trace("model_request", agent, model=candidate.get("model"), entry=candidate.get("member_label"),
                                position=position, empty_retry=empty_retry, think=think and not empty_retry,
                                temperature=temperature if temperature is not None else DEFAULT_TEMPERATURE, max_tokens=max_tokens,
                                messages=[{"role": item.get("role"), "chars": len(str(item.get("content") or ""))}
                                          for item in call_messages if isinstance(item, dict)],
                                last_message=str(last_content or ""))
                    stream = host.agent_stream(candidate, call_messages, max_tokens=max_tokens, think=think and not empty_retry,
                                               temperature=temperature if temperature is not None else DEFAULT_TEMPERATURE)
                    for kind, chunk in stream:
                        if self.stop_event.is_set():
                            raise StopRequested()
                        if not chunk or not isinstance(chunk, str):
                            continue
                        if kind == "reasoning":
                            pieces = [("reasoning", chunk)]
                        elif kind == "content":
                            pieces = splitter.feed(chunk)
                        else:
                            continue
                        for piece_kind, piece in pieces:
                            part = "reasoning" if piece_kind == "reasoning" else "answer"
                            (reasoning_parts if part == "reasoning" else answer_parts).append(piece)
                            guard = guards[part]
                            if guard.feed(piece) or (part == "reasoning" and guard.total > REASONING_CHAR_LIMIT):
                                degenerate = part
                                break
                            if not entries:
                                continue
                            if part == "reasoning":
                                if reasoning_entry is None:
                                    reasoning_entry = self._entry_open(agent, "thinking")
                                reasoning_entry["text"] = clip_tail(reasoning_entry["text"] + piece, 20000)
                                self._entry_emit(agent, reasoning_entry)
                            else:
                                now = time.monotonic()
                                if now - last_view >= 0.3:
                                    last_view = now
                                    output_entry = self._view_output(agent, output_entry, "".join(answer_parts))
                        if degenerate:
                            break
                    if not degenerate:
                        for piece_kind, piece in splitter.feed("", final=True):
                            (reasoning_parts if piece_kind == "reasoning" else answer_parts).append(piece)
                    reasoning_text, answer_text = "".join(reasoning_parts), "".join(answer_parts)
                    if degenerate:
                        guard = guards[degenerate]
                        if degenerate == "reasoning":
                            reasoning_text = reasoning_text[:guard.start] if guard.start is not None else reasoning_text
                            answer_text = ""
                        else:
                            answer_text = answer_text[:guard.start] if guard.start is not None else answer_text
                        if reasoning_entry is not None:
                            self._entry_set(agent, reasoning_entry, clip_tail(reasoning_text, 4000) + ("\n… [stopped: the model was looping]" if degenerate == "reasoning" else ""))
                        if output_entry is not None:
                            self._entry_set(agent, output_entry, display_text(answer_text) or "(discarded: the reply was stuck repeating itself)")
                        self._trace("model_degenerate", agent, model=candidate.get("model"), part=degenerate,
                                    seconds=round(time.monotonic() - started, 3), reasoning=reasoning_text, answer=answer_text)
                        self.active_candidate = candidate_index
                        return Generation(reasoning_text.strip(), answer_text.strip(), degenerate)
                    reasoning, answer = split_stray_think(reasoning_text, answer_text)
                    if not answer.strip() and not reasoning.strip():
                        raise RuntimeError("The model returned no response text.")
                    if reasoning_entry is not None:
                        self._entry_set(agent, reasoning_entry, clip_tail(reasoning, 20000))
                    if entries:
                        output_entry = self._view_output(agent, output_entry, answer, final=True)
                    self.active_candidate = candidate_index
                    try:
                        host.record_usage(self.chat_id, candidate.get("model"), self.route_info.get("route"),
                                          round(in_chars / 4), round((len(reasoning) + len(answer)) / 4), True,
                                          time.monotonic() - started)
                    except Exception:
                        pass
                    self._trace("model_response", agent, model=candidate.get("model"), entry=candidate.get("member_label"),
                                seconds=round(time.monotonic() - started, 3), reasoning=reasoning, answer=answer)
                    if agent.lead and entries:
                        self._trace_lead_thinking(reasoning, time.monotonic() - started)
                    return Generation(reasoning, answer)
                except StopRequested:
                    raise
                except Exception as exc:
                    last_error = exc
                    self._trace("model_error", agent, model=candidate.get("model"), entry=candidate.get("member_label"),
                                seconds=round(time.monotonic() - started, 3), partial_reasoning="".join(reasoning_parts),
                                partial_answer="".join(answer_parts), error=exc)
                    for entry in (reasoning_entry, output_entry):
                        if entry is not None:
                            self._entry_emit(agent, entry, done=True)
                    if str(exc) == "The model returned no response text." and not empty_retry:
                        empty_retry = True
                        continue
                    break
                finally:
                    close = getattr(stream, "close", None)
                    if callable(close):
                        try:
                            close()
                        except Exception:
                            pass
                    if entered:
                        host.agent_gate.exit()
            if position < count - 1:
                self._note_entry(agent, "error", "This model or key failed ({}). Switching to the next entry in the group.".format(clip(last_error, 300)))
        raise last_error or RuntimeError("No working model entry was found.")

    def _view_output(self, agent, entry, raw, final=False):
        """Show only the commentary of a streaming reply and say what the agent is doing right now."""
        prose = display_text(raw)
        if prose:
            if entry is None:
                entry = self._entry_open(agent, "output")
            entry["text"] = prose
            if final:
                self._entry_emit(agent, entry, force=True, done=True)
            else:
                self._entry_emit(agent, entry)
        elif entry is not None and final:
            self._entry_emit(agent, entry, force=True, done=True)
        if not final:
            self._set_current(agent, live_activity(raw) or "Thinking…")
        return entry

    # -- shared context -----------------------------------------------------

    def team_table(self, exclude=""):
        rows = []
        with self.lock:
            agents = list(self.agents.values())
        for agent in agents:
            if agent.status == "retired" or agent.name == exclude:
                continue
            extra = " result={}".format(agent.last_result) if agent.last_result else ""
            rows.append("- {} [{}] status={}{} task: {} | last report: {}".format(
                agent.name, agent.role, agent.status, extra, clip(agent.task, 220).replace("\n", " ") or "none",
                clip(agent.summary, 220).replace("\n", " ") or "none"))
        return "\n".join(rows) or "(no subagents yet)"

    def shared_context(self, agent):
        with self.lock:
            notes = sorted(self.notes.items(), key=lambda pair: pair[1]["ts"], reverse=True)
            channel = list(self.channel)[-25:]
            plan = self.plan
            goal = self.goal
        note_lines, budget = [], 8000
        for key, item in notes:
            line = "- {} (by {}): {}".format(key, item["by"], clip(item["value"], 1500))
            if budget - len(line) < 0:
                break
            budget -= len(line)
            note_lines.append(line)
        chat_lines = ["[{}] {} -> {}: {}".format(time.strftime("%H:%M:%S", time.localtime(item["ts"])), item["from"], item["to"],
                                                 clip(item["text"], 500).replace("\n", " ")) for item in channel]
        text = ("SHARED CONTEXT (identical for every agent)\nMODE: {}\nGOAL (the only thing the team works on): {}\n\nPLAN:\n{}\n\nSHARED NOTES:\n{}\n\nTEAM:\n{}\n\n"
                "WORKSPACE FILES (live, checked by the system; this is the truth about what exists):\n{}\n\nRECENT CHANNEL:\n{}\n\n"
                "PROJECT WORKSPACE: {}").format(
            "CONTINUOUS (keep improving until the human stops the team)" if self.continuous else "NORMAL (the lead finishes the run when the goal is met and verified)",
            clip(goal, 2500) or "(none yet)", clip(plan, 3000) or "(no plan yet)", "\n".join(note_lines) or "(no notes yet)",
            self.team_table(), self.workspace_files_text(), "\n".join(chat_lines) or "(quiet)", self._workspace_label())
        if agent is not None and agent.lead:
            text += "\n\nFINISH CHECK: " + self._finish_summary_line()
        elif agent is not None:
            text += "\n\nYOUR CURRENT TASK (stay on this and nothing else):\n" + (clip(agent.task, 1800) or "(none: ask lead)")
        return text

    def _workspace_label(self):
        try:
            return str(self.host.chat_work_root(self.chat_id))
        except Exception:
            return "(unavailable)"

    def _format_inbox(self, items):
        lines = []
        for item in items:
            extra = ""
            if item.get("acknowledged") or item.get("quick_reply"):
                extra = "\n(The human already got a short acknowledgement while you were busy. Do not reply to this message on its own.)"
            lines.append("from {}: {}{}".format(item.get("from"), clip(item.get("text"), 3000), extra))
        return "\n\n".join(lines)

    def _drain(self, agent):
        items, agent.inbox = agent.inbox, []
        if agent.lead:
            with self.lock:
                self._ack_pending = False
        return items

    def _late_note(self, late):
        note = "NEW MESSAGES FOR YOU:\n" + self._format_inbox(late)
        if any(item.get("from") == "user" for item in late):
            note += ("\n\nThe human wrote while you were working and already received a short acknowledgement. Fold these messages into your work "
                     "and, when you are done with this step, send ONE full SAY user reply that covers all of them. Do not send one reply per message.")
        return note

    def _trim(self, agent):
        if len(agent.history) > MAX_HISTORY:
            del agent.history[:-MAX_HISTORY]
        while agent.history and agent.history[0].get("role") != "user":
            del agent.history[0]
        for item in agent.history:
            item["content"] = clip(item["content"], 7000)

    def _system_for(self, agent):
        if agent.lead:
            syntax = COMMON_SYNTAX.replace("{user_target}", ", user").replace("{max_actions}", str(MAX_ACTIONS_PER_TURN))
            mode = MODE_CONTINUOUS if self.continuous else MODE_FINITE
            system = LEAD_SYSTEM.replace("{max_agents}", str(MAX_SUBAGENTS)).replace("{mode_block}", mode).replace("{syntax}", syntax)
        else:
            syntax = COMMON_SYNTAX.replace("{user_target}", "").replace("{max_actions}", str(MAX_ACTIONS_PER_TURN))
            system = AGENT_SYSTEM.replace("{name}", agent.name).replace("{role}", agent.role).replace("{syntax}", syntax)
        return [{"role": "system", "content": system}, {"role": "system", "content": self.shared_context(agent)}]

    # -- loops --------------------------------------------------------------

    def _team_signature(self):
        with self.lock:
            return (tuple((a.name, a.status, round(a.last_report_ts)) for a in self.agents.values() if not a.lead),
                    round(self.last_change_ts), len(self.channel))

    def _scheduled_review(self):
        """Reason for an unprompted lead review, or None when nothing needs the lead (saves model calls)."""
        if self.continuous:
            return "scheduled review"
        with self.lock:
            members = [a for a in self.agents.values() if not a.lead and a.status != "retired"]
        now = time.time()
        working = [a for a in members if a.status in WORKING_STATES]
        blocked = [a for a in members if a.status == "blocked"]
        if blocked and not working:
            self.block_reviews += 1
            if self.block_reviews <= BLOCK_REVIEW_LIMIT:
                self.idle_paused = False
                self.lead.heartbeat = min(self.lead.heartbeat, BLOCK_HEARTBEAT)
                return "blocked agents still need fixing: " + ", ".join(a.name for a in blocked)
        elif not blocked:
            self.block_reviews = 0
        if self.idle_paused:
            return None
        if not members and not self.lead.carry and not self.lead.inbox and self.last_change_ts == 0 and self._answered_current_user_message():
            # Question/analysis run: the lead already answered and wrote nothing, so end quietly instead of inventing more work.
            self._finish_quietly()
            return None
        if working:
            stalled = [a.name for a in working if a.status == "working" and now - a.last_activity > STALL_SECONDS]
            return "stalled agents: " + ", ".join(stalled) if stalled else None
        signature = self._team_signature()
        if signature == self.review_signature:
            self.idle_reviews += 1
            if self.idle_reviews >= IDLE_REVIEW_LIMIT:
                self.idle_paused = True
                self._activity(None, "alert", "The team is idle and the lead has neither finished nor given new work. Message the lead to continue.", ok=False)
                self._emit("state", running=True, idle_paused=True)
                return None
        else:
            self.idle_reviews = 0
        self.review_signature = signature
        return "team idle review"

    def _answered_current_user_message(self):
        with self.lock:
            return self.user_msg_count > 0 and any(count == self.user_msg_count for count, _text in self.recent_user_posts)

    def _finish_quietly(self):
        """Mark the run finished without posting a second copy of the answer the human already has."""
        with self.lock:
            last = next((m for m in reversed(self.channel) if m.get("from") == "lead" and m.get("to") == "user"), None)
        self.finish_summary = (last or {}).get("text", "")
        self._activity(self.lead, "finish", "The lead answered and finished", detail=self.finish_summary, ok=True)
        self._emit("finished", summary=self.finish_summary, files=self.files_view())
        self.stop(final_status="finished", finished=True)

    def _lead_loop(self):
        lead = self.lead
        while not self.stop_event.is_set():
            try:
                triggered = lead.wake.wait(timeout=lead.heartbeat)
                if self.stop_event.is_set():
                    break
                lead.wake.clear()
                if triggered or lead.inbox:
                    reason = CONTINUE_REASON if lead.carry and not lead.inbox else "new messages"
                else:
                    reason = self._scheduled_review()
                    if reason is None:
                        continue
                self._agent_turn(lead, reason)
            except StopRequested:
                break
            except Exception as exc:
                self._trace("loop_error", lead, where="lead loop", error=exc)
                self._note_entry(lead, "error", "Lead loop error: {}".format(clip(exc, 500)))
                self._wait(5)
            self._wait(2)
        if not self.finished:
            self._set_status(lead, "stopped", current="")

    def _agent_loop(self, agent):
        while not self.stop_event.is_set() and not agent.retired:
            try:
                if not agent.inbox:
                    if agent.status in WORKING_STATES:
                        self._set_status(agent, "idle", current="Waiting for work")
                    agent.wake.wait(timeout=3)
                    agent.wake.clear()
                    if not agent.inbox:
                        continue
                self._agent_turn(agent, "new messages")
            except StopRequested:
                break
            except Exception as exc:
                self._trace("loop_error", agent, where="agent loop", error=exc)
                self._note_entry(agent, "error", "Agent loop error: {}".format(clip(exc, 500)))
                self._wait(5)
            self._wait(1)
        if agent.retired:
            self._set_status(agent, "retired", current="")
        elif not self.finished:
            self._set_status(agent, "stopped", current="")

    def _launch(self, agent, resumed=False):
        agent.thread = threading.Thread(target=self._agent_loop, args=(agent,), name="kurt-agent-" + agent.name, daemon=True)
        agent.thread.start()
        if resumed and agent.status == "working" and not agent.inbox:
            agent.inbox.append({"from": "system", "text": "Agent mode restarted. Continue your task: {}".format(clip(agent.task, 1500)),
                                "ts": time.time()})
            agent.wake.set()

    def _worker_prompt(self, agent, inbox):
        return ("NEW MESSAGES FOR YOU:\n{}\n\nYOUR TASK: {}\n\nTake the next small step toward your task. Reply with action blocks "
                "(plus at most one short line of commentary), then stop and wait for the results.").format(
            self._format_inbox(inbox) or "(none)", clip(agent.task, 1500))

    def _on_degenerate(self, agent, gen):
        """Handle a reply that degenerated into repetition. Returns True when the agent should stop trying this turn."""
        agent.strikes += 1
        agent.temperature = RETRY_TEMPERATURE
        if gen.degenerate == "reasoning":
            agent.suppress_think = True
        what = "its thinking" if gen.degenerate == "reasoning" else "its reply"
        self._activity(agent, "alert", "{}'s model got stuck repeating {}. The output was stopped and discarded ({}/{}).".format(
            agent.name, what, agent.strikes, MAX_DEGENERATE_STRIKES), ok=False)
        if agent.strikes < MAX_DEGENERATE_STRIKES:
            return False
        if agent.lead:
            agent.strikes = 0
            self._set_status(agent, "orchestrating", current="Model keeps looping: paused until the next event")
            return True
        self._block(agent, "My model repeatedly got stuck producing endless repeated text, so I stopped instead of guessing. "
                           "Please re-assign this with a smaller, simpler task.", auto=True)
        return True

    def _check_alive(self, agent):
        if self.stop_event.is_set() or agent.retired:
            raise StopRequested()

    def _agent_turn(self, agent, reason):
        agent.busy = True
        agent.restarted = False
        try:
            inbox = self._drain(agent)
            self._trace("turn_start", agent, reason=reason, inbox=[
                {"from": item.get("from"), "to": item.get("to"), "kind": item.get("kind"), "text": item.get("text"), "ts": item.get("ts")}
                for item in inbox if isinstance(item, dict)])
            if agent.lead:
                agent.heartbeat = BLOCK_HEARTBEAT if self._blocked_workers() else LEAD_HEARTBEAT
                self._set_status(agent, "orchestrating", current="Reviewing the team")
                base = self._lead_prompt(reason, inbox)
                if agent.carry:
                    base += "\n\nUNFINISHED WORK FROM YOUR PREVIOUS TURN. Continue from these results:\n" + agent.carry
                    agent.carry = ""
                max_rounds = LEAD_TOOL_ROUNDS if self._has_workers() else LEAD_SOLO_ROUNDS
            else:
                self._set_status(agent, "working", current="Starting")
                base = self._worker_prompt(agent, inbox)
                max_rounds = MAX_TOOL_ROUNDS
            extra = []
            nudges = 0
            pushes = 0
            for _round in range(max_rounds):
                self._check_alive(agent)
                prompt = base + ("\n\n" + "\n".join(extra) if extra else "")
                agent.history.append({"role": "user", "content": prompt})
                self._trim(agent)
                messages = self._system_for(agent) + [dict(item) for item in agent.history]
                gen = self._generate(agent, messages, think=not agent.suppress_think, temperature=agent.temperature)
                reasoning, answer = gen
                agent.turns += 1
                agent.last_answer = answer or ""
                if gen.degenerate:
                    if agent.history and agent.history[-1].get("role") == "user":
                        agent.history.pop()
                    if self._on_degenerate(agent, gen):
                        break
                    extra = [DEGENERATE_NOTICE]
                    continue
                agent.strikes = 0
                agent.temperature = None
                agent.suppress_think = False
                turn = parse_turn(answer)
                self._trace("turn_parsed", agent, prose=turn.prose, fabricated=turn.fabricated,
                            actions=[{"verb": item.verb, "args": item.args, "body": item.body, "closed": item.closed} for item in turn.actions])
                agent.history.append({"role": "assistant", "content": turn.clean or answer or reasoning})
                self._trim(agent)
                extra = []
                results, finished, said_to_user, stop_turn = self._execute(agent, turn)
                if agent.lead:
                    pending_user = [item for item in inbox if item.get("from") == "user"]
                    if pending_user and not said_to_user and turn.prose and not results and not self.finished:
                        self.post("lead", "user", turn.prose)
                        said_to_user = True
                    if turn.prose:
                        agent.summary = clip(turn.prose, 600)
                    if self.finished or self.stop_event.is_set():
                        break
                    if self._blocked_workers() and pushes < LEAD_BLOCK_PUSHES and _round < max_rounds - 1:
                        # A blocked teammate is still stopped: the lead must not end its turn until it has acted on it.
                        pushes += 1
                        late = self._drain(agent)
                        base = ("TOOL RESULTS:\n" + "\n\n".join(results) + "\n\n" if results else "") + self._blocker_section(reminder=True)
                        if late:
                            base += "\n\n" + self._late_note(late)
                            inbox = inbox + late
                        continue
                    used_tools = any(item.verb in TOOL_VERBS for item in turn.actions)
                    failed = any(("rejected" in item or "failed" in item) for item in results)
                    if stop_turn or not results or not (used_tools or failed):
                        break
                else:
                    if finished or stop_turn:
                        break
                    if not results:
                        if turn.actions:
                            self._set_status(agent, "idle", current="Waiting for a reply")
                            break
                        nudges += 1
                        if nudges > MAX_NUDGES:
                            self._block(agent, "I replied several times without taking any action, so I am stopping. Last thing I said: {}".format(
                                clip(turn.prose or answer, 300)), auto=True)
                            break
                        base = NUDGE
                        continue
                late = self._drain(agent)
                base = "TOOL RESULTS:\n" + "\n\n".join(results)
                if late:
                    base += "\n\n" + self._late_note(late)
                    inbox = inbox + late
            else:
                if agent.lead:
                    if base.startswith("TOOL RESULTS") and not self.finished and not self.stop_event.is_set():
                        agent.carry = clip(base, RESULT_LIMIT)
                        agent.wake.set()
                else:
                    agent.summary = "Reached the per-task round limit."
                    self._set_status(agent, "idle", current="Round limit reached")
                    self.post(agent.name, "lead", "[LIMIT] Reached my round limit on the current task. Send follow-up work if more is needed.")
        finally:
            agent.busy = False
            if not self.stop_event.is_set() and not agent.retired:
                if agent.lead:
                    self._set_status(agent, "watching", current="Waiting for the team")
                elif not agent.inbox and agent.status in WORKING_STATES:
                    self._set_status(agent, "idle", current="Waiting for work")

    def _has_workers(self):
        with self.lock:
            return any(not a.lead and a.status != "retired" for a in self.agents.values())

    def _lead_prompt(self, reason, inbox):
        parts = ["TURN REASON: {}".format(reason)]
        users = [item for item in inbox if item.get("from") == "user"]
        others = [item for item in inbox if item.get("from") != "user"]
        if users:
            parts.append("THE HUMAN WROTE {} message(s). Read them together and answer ALL of them in ONE full SAY user reply (a short acknowledgement may already have been sent, so do not repeat it), then act on any instruction:\n".format(len(users)) + self._format_inbox(users))
        if others:
            parts.append("TEAM MESSAGES:\n" + self._format_inbox(others))
        if reason == CONTINUE_REASON and not inbox:
            parts.append("No new messages. You were in the middle of a task: carry on with it now.")
        elif not inbox:
            if self.continuous:
                parts.append("No new messages. Review progress. Use WAIT if the team is busy and nothing needs you; otherwise assign the next useful work.")
            else:
                parts.append("No new messages. Check the team state and the FINISH CHECK. If the goal is met and verified, FINISH. "
                             "If the human only asked a question or for an analysis and you already answered, FINISH now: do not install, run or write anything. "
                             "If something the human asked for is missing, assign exactly that. If the team is busy, WAIT.")
        blockers = self._blocker_section()
        if blockers:
            parts.append(blockers)
        parts.append("Decide your next actions now.")
        return "\n\n".join(parts)

    def _blocker_section(self, reminder=False):
        """The lead's standing orders about stopped teammates ('' when nobody is blocked)."""
        blocked = self._blocked_workers()
        if not blocked:
            return ""
        rows = []
        for member in blocked:
            rows.append("- {} ({}): {} | task: {} | automatic restarts used: {}/{}".format(
                member.name, clip(member.role, 60), clip(member.blocked_reason, 500).replace("\n", " "),
                clip(member.task, 220).replace("\n", " "), member.recoveries, MAX_AUTO_RECOVERIES))
        head = ("STILL BLOCKED: you have not unblocked these yet. Act on them now." if reminder else
                "BLOCKED TEAMMATES: fixing these is your first job this turn.")
        return (head + "\n" + "\n".join(rows) + "\nDiagnose with RUN or READ, fix the cause, then ASSIGN the agent a rewritten concrete task "
                "(exact commands, paths and the fix). If it already failed that way, change approach: RETIRE it and SPAWN a fresh specialist, "
                "or do the step yourself. Do not wait, and do not hand a technical obstacle to the human.")

    # -- finishing ------------------------------------------------------------

    def _finish_gate(self):
        """(hard, soft) reasons that stand in the way of FINISH. Hard ones can never be overridden."""
        hard, soft = [], []
        if self.continuous:
            hard.append("continuous mode is on: the human asked the team to keep running until they stop it")
        with self.lock:
            members = [a for a in self.agents.values() if not a.lead and a.status != "retired"]
        busy = [a.name for a in members if a.status in WORKING_STATES or a.inbox]
        if busy:
            hard.append("still working or has unread messages: " + ", ".join(busy))
        blocked = [a.name for a in members if a.status == "blocked"]
        if blocked:
            soft.append("unresolved blockers: " + ", ".join(blocked))
        if self.last_change_ts:
            stamp = time.strftime("%H:%M:%S", time.localtime(self.last_change_ts))
            if not members:
                # A solo lead has nobody to hand verification to: its own successful check after the last change is enough.
                if self.lead.last_report_ts <= self.last_change_ts:
                    soft.append("files were written at {}: run one quick check (RUN, READ or LIST) on them, then FINISH".format(stamp))
                elif self.lead.last_result in ("FAIL", "PARTIAL"):
                    soft.append("your last check after the file change at {} failed: fix it or explain it in the final report".format(stamp))
            else:
                reporters = [a for a in list(members) + [self.lead] if a.last_report_ts > self.last_change_ts]
                others = reporters if len(members) <= 1 else [a for a in reporters if a.name != self.last_writer]
                if not others:
                    soft.append("nobody other than {} has reported since the last file change at {}: have a tester or reviewer verify the final state".format(
                        self.last_writer or "the author", stamp))
                fails = [a.name for a in reporters if a.last_result in ("FAIL", "PARTIAL")]
                if fails:
                    soft.append("reports since the last change say FAIL or PARTIAL: " + ", ".join(fails))
        return hard, soft

    def _finish_summary_line(self):
        hard, soft = self._finish_gate()
        if not hard and not soft:
            return "ready: the work looks verified. FINISH is allowed now."
        return "not ready: " + "; ".join(hard + soft)

    def _try_finish(self, agent, args, body):
        """Returns None when the run was finished, else the rejection text."""
        hard, soft = self._finish_gate()
        summary = clip(body.strip() or args.strip(), 3500) or "The goal has been completed."
        if hard:
            return "[FINISH rejected] " + "; ".join(hard) + ". Use WAIT now; you will be woken when a teammate reports."
        missing = self.missing_claimed_files(summary)
        if missing:
            soft.append("your summary mentions files that do not exist: " + ", ".join(missing[:6]))
        unverified = []
        if soft:
            self.finish_rejects += 1
            if self.finish_rejects < MAX_FINISH_REJECTS:
                return "[FINISH rejected] Not verified yet: " + "; ".join(soft) + ". Fix that (ASSIGN the right teammate) and FINISH again."
            unverified = soft
        self._finish(agent, summary, unverified)
        return None

    def _finish(self, agent, summary, unverified):
        lines = [summary.strip(), "", "Verified by the system:"]
        files = self.files_view()
        scanned, _truncated = self._scan(fresh=True)
        if scanned:
            lines.append("Files in the workspace: " + ", ".join(rel for rel, _s, _m in scanned[:20]) + (" …" if len(scanned) > 20 else ""))
        else:
            lines.append("Files in the workspace: none")
        reports = []
        with self.lock:
            members = [a for a in self.agents.values() if not a.lead and a.status != "retired"]
        for member in members:
            if member.summary:
                reports.append("- {}{}: {}".format(member.name, " ({})".format(member.last_result) if member.last_result else "",
                                                   _short(member.summary, 220)))
        if reports:
            lines.append("Team reports:")
            lines.extend(reports)
        if unverified:
            lines.append("")
            lines.append("Warning: finished with open verification points: " + "; ".join(unverified))
        text = "\n".join(lines).strip()
        self.finish_summary = text
        self._activity(agent, "finish", "The lead finished the project", detail=text, ok=not unverified)
        message = {"id": uuid.uuid4().hex[:10], "ts": round(time.time(), 3), "from": "lead", "to": "user", "text": text, "kind": "final"}
        with self.lock:
            self.channel.append(message)
        self._emit("channel", message=message)
        self._deliver_to_user(text, "lead")
        self._emit("finished", summary=text, files=files)
        self.stop(final_status="finished", finished=True)

    # -- executing actions ----------------------------------------------------

    def _execute(self, agent, turn):
        results, finished, said_to_user, stop_turn = [], False, False, False
        if turn.fabricated:
            results.append(FABRICATED_NOTICE)
        seen, actions = set(), []
        for item in turn.actions:
            key = (item.verb, item.args.strip(), item.body.strip())
            if key in seen:
                continue
            seen.add(key)
            actions.append(item)
        if len(actions) > MAX_ACTIONS_PER_TURN:
            dropped = len(actions) - MAX_ACTIONS_PER_TURN
            actions = actions[:MAX_ACTIONS_PER_TURN]
            results.append("[LIMIT] Only the first {} action blocks were run; {} were dropped. Send them again in your next reply if still needed.".format(
                MAX_ACTIONS_PER_TURN, dropped))
        has_work = any(item.verb in TOOL_VERBS for item in actions)
        for item in actions:
            verb, args, body = item.verb, item.args, item.body
            if verb in LEAD_ONLY and not agent.lead:
                results.append("[{} rejected] Only lead may use {}.".format(verb, verb))
                continue
            if verb in WORKER_ONLY and agent.lead:
                continue
            self._trace("action", agent, verb=verb, args=args, body=body, closed=item.closed)
            try:
                if verb == "SAY":
                    target, _sep, inline = args.partition(" ")
                    text = body.strip() or inline.strip()
                    message, reason = self._post(agent.name, target or "all", text, checked=True)
                    if message is None:
                        if reason and reason != "empty":
                            results.append("[SAY suppressed] " + reason)
                    elif target.lower() in ("user", "human") and agent.lead:
                        said_to_user = True
                elif verb == "NOTE":
                    key = slug(args or "note", "note")
                    value = body.strip() or args
                    with self.lock:
                        existing = self.notes.get(key)
                        unchanged = bool(existing) and existing["value"] == value
                        if not unchanged and not existing and len(self.notes) >= 60:
                            results.append("[NOTE rejected] The shared notes are full. Reuse an existing key or ask lead to clean up.")
                            continue
                        if not unchanged:
                            self.notes[key] = {"value": value, "by": agent.name, "ts": time.time()}
                    if not unchanged:
                        self._emit("note", key=key, note={"value": clip(value, 3000), "by": agent.name, "ts": self.notes[key]["ts"]})
                        self._activity(agent, "note", "Shared note: {}".format(key), detail=value)
                elif verb == "DONE":
                    outcome = self._accept_done(agent, body.strip() or args.strip(), has_work)
                    if outcome:
                        self._trace("action_result", agent, verb=verb, result=outcome)
                        results.append(outcome)
                    else:
                        finished = True
                elif verb == "BLOCKED":
                    self._block(agent, clip(body.strip() or args.strip(), 1200) or "No reason given.")
                    finished = True
                elif verb == "FINISH":
                    outcome = self._try_finish(agent, args, body)
                    if outcome:
                        self._trace("action_result", agent, verb=verb, result=outcome)
                        results.append(outcome)
                        stop_turn = True
                    else:
                        finished = True
                        stop_turn = True
                elif verb == "SPAWN":
                    results.append(self._logged(agent, verb, args, self._spawn(agent, args, body)))
                elif verb == "ASSIGN":
                    results.append(self._logged(agent, verb, args, self._assign(agent, args, body)))
                elif verb == "RETIRE":
                    results.append(self._logged(agent, verb, args, self._retire(agent, args)))
                elif verb == "PLAN":
                    plan = clip(body.strip(), 6000)
                    if plan != self.plan:
                        with self.lock:
                            self.plan = plan
                        self._emit("plan", plan=self.plan)
                        self._activity(agent, "plan", "Updated the plan", detail=plan)
                elif verb == "WAIT":
                    try:
                        agent.heartbeat = max(5, min(900, int(float(args.split()[0]))))
                    except (ValueError, IndexError):
                        agent.heartbeat = LEAD_HEARTBEAT
                    if self._blocked_workers():
                        agent.heartbeat = min(agent.heartbeat, LEAD_HEARTBEAT)
                    self._set_current(agent, "Waiting for the team", force=True)
                elif verb in TOOL_VERBS:
                    if not item.closed and verb == "WRITE":
                        results.append("[WRITE rejected] Your reply was cut off before '@@@ END', so the file was NOT written (a half-written file would be corrupt). "
                                       "Write a smaller file, or split the content into several files, and finish each block with @@@ END.")
                        self._trace("action_result", agent, verb=verb, args=args, result=results[-1])
                        continue
                    self._note_work(agent)
                    started = time.monotonic()
                    outcome = self._tool(agent, verb, args, body)
                    self._trace("action_result", agent, verb=verb, args=args, seconds=round(time.monotonic() - started, 3), result=outcome)
                    results.append(self._track_repeat(agent, item, outcome))
            except StopRequested:
                raise
            except Exception as exc:
                self._trace("action_error", agent, verb=verb, args=args, body=body, error=exc)
                results.append("[{} failed] {}".format(verb, clip(exc, 800)) + self._missing_hint(verb))
            if self.finished or agent.restarted or (agent.status == "blocked" and not agent.lead):
                break
        results = [item for item in results if item]
        if agent.restarted or (agent.status == "blocked" and not finished and not agent.lead and agent.blocked_reason):
            finished = True
        return results, finished, said_to_user, stop_turn

    def _logged(self, agent, verb, args, outcome):
        self._trace("action_result", agent, verb=verb, args=args, result=outcome)
        return outcome

    def _missing_hint(self, verb):
        if verb in ("READ", "LIST"):
            return "\nFiles that exist in the workspace:\n" + self.workspace_files_text(25)
        return ""

    def _track_repeat(self, agent, item, result):
        """Flag an agent that keeps repeating the same action with the same result."""
        key = (item.verb, _oneline(item.args)[:200], hashlib.md5(item.body.encode("utf-8", "replace")).hexdigest()[:8])
        digest = hashlib.md5(str(result).encode("utf-8", "replace")).hexdigest()[:8]
        agent.recent.append((key, digest))
        repeats = sum(1 for entry in agent.recent if entry == (key, digest))
        if repeats < MAX_REPEAT_ACTIONS:
            return result
        agent.loop_warnings += 1
        agent.recent.clear()
        if agent.lead:
            return result + "\n[LOOP WARNING] You already did this exact action {} times with the same result. Use what you have or change approach.".format(repeats)
        if agent.loop_warnings >= MAX_LOOP_WARNINGS:
            self._block(agent, "I got stuck repeating the same action ({} {}) with the same result, so I stopped instead of looping. "
                               "I need a different approach or more information.".format(item.verb, clip(_oneline(item.args) or _oneline(item.body), 80)), auto=True)
            return result
        return result + "\n[LOOP WARNING] You already did this exact action {} times with the same result. Use the result, change your approach, or report BLOCKED.".format(repeats)

    def _accept_done(self, agent, text, has_work):
        text = clip(text, 1500) or "Task complete."
        if has_work:
            return "[DONE ignored] Do not combine DONE with other actions. Wait for their results, check them, then send DONE alone."
        problems = []
        if agent.evidence == 0:
            problems.append("you have not read, written, run or searched anything for this task, so there is no evidence. Do the work with tools first")
        missing = self.missing_claimed_files(text)
        if missing:
            problems.append("your summary mentions files that do not exist in the workspace: {}. Check WORKSPACE FILES, then correct the summary or create them".format(", ".join(missing[:6])))
        if problems and agent.done_rejects < MAX_DONE_REJECTS:
            agent.done_rejects += 1
            return "[DONE rejected] " + "; ".join(problems) + "."
        if problems:
            text += "\n(UNVERIFIED: {})".format("; ".join(problems))
        result = RESULT_RE.search(text)
        agent.last_result = result.group(1).upper() if result else ("PARTIAL" if problems else "")
        agent.summary = text
        agent.last_report_ts = time.time()
        agent.blocked_reason = ""
        agent.recoveries = 0
        self._note_work(agent)
        self._set_status(agent, "done", current="Finished: waiting for the lead")
        self._activity(agent, "done", "Reported done" + (" ({})".format(agent.last_result) if agent.last_result else ""), detail=text, ok=not problems and agent.last_result != "FAIL")
        self.post(agent.name, "lead", "[DONE] " + text)
        self._deliver_to_user(clip(text, 1200), sender=agent.name)
        return ""

    def _blocked_workers(self):
        with self.lock:
            return [a for a in self.agents.values() if not a.lead and a.status == "blocked"]

    @staticmethod
    def _recovery_hint(reason):
        lowered = str(reason or "").lower()
        for needle, hint in RECOVERY_HINTS:
            if needle in lowered:
                return hint
        return "Read the last results carefully, fix the cause, and try a different approach instead of repeating what failed."

    def _blocker_brief(self, agent, reason):
        """What the lead needs to debug a blocker: the reason, the agent's latest actions and its last raw reply."""
        lines = [reason.strip() or "No reason given.", "", "Restarts so far: {} of {} automatic.".format(agent.recoveries, MAX_AUTO_RECOVERIES)]
        with self.lock:
            recent = [item for item in list(self.activity)[-80:] if item.get("agent") == agent.name][-6:]
        if recent:
            lines.append("Its latest actions:")
            for item in recent:
                lines.append("- {}{}: {}".format(item.get("kind"), " (failed)" if item.get("ok") is False else "", clip(item.get("text"), 160)))
        if agent.last_answer.strip():
            lines.append("Its last raw reply:\n" + clip(agent.last_answer.strip(), 600))
        return "\n".join(lines)

    def _auto_recover(self, agent, reason):
        """Restart a stuck subagent on the same task with a clean history and a concrete fix, without bothering anyone."""
        agent.recoveries += 1
        number = agent.recoveries
        task = agent.task
        note = ("RECOVERY {}/{}: your previous attempt stopped automatically ({}). {}\n"
                "Continue the task from where you were; check WORKSPACE FILES and do not redo what already worked.\nYOUR TASK: {}").format(
            number, MAX_AUTO_RECOVERIES, clip(reason, 240), self._recovery_hint(reason), clip(task, 1500))
        agent.reset_task()
        agent.history = []
        agent.inbox = []
        agent.temperature = RETRY_TEMPERATURE
        agent.restarted = True
        self._note_work(agent)
        self._activity(agent, "recover", "{} hit a problem and was restarted automatically ({}/{})".format(agent.name, number, MAX_AUTO_RECOVERIES),
                       detail=note, ok=None)
        self._set_status(agent, "working", current="Recovering (attempt {} of {})".format(number, MAX_AUTO_RECOVERIES))
        agent.inbox.append({"from": "system", "text": note, "ts": time.time()})
        agent.wake.set()

    def _block(self, agent, reason, auto=False):
        if auto and not agent.lead and agent.recoveries < MAX_AUTO_RECOVERIES:
            self._auto_recover(agent, reason)
            return
        agent.blocked_reason = reason
        agent.summary = "BLOCKED: " + reason
        agent.last_report_ts = time.time()
        agent.last_result = ""
        self._note_work(agent)
        self._set_status(agent, "blocked", current="Blocked: " + clip(reason, 100))
        self._activity(agent, "blocked", "{} is blocked: the lead is working on a fix".format(agent.name) + (" (after automatic restarts)" if auto else ""),
                       detail=reason, ok=False)
        self.post(agent.name, "lead", "[BLOCKED] " + self._blocker_brief(agent, reason))
        self.lead.heartbeat = min(self.lead.heartbeat, BLOCK_HEARTBEAT)
        self.idle_paused = False
        self.idle_reviews = 0

    def _spawn(self, agent, args, body):
        name_part, _sep, role_part = args.partition("|")
        name = slug(name_part, "agent")
        role = clip(role_part.strip() or "Specialist", 120)
        mission = body.strip() or role
        existing = self._find(name)
        if existing is not None:
            return self._assign(agent, name, mission)
        with self.lock:
            live = [item for item in self.agents.values() if not item.lead and item.status != "retired"]
            if len(live) >= MAX_SUBAGENTS:
                return "[SPAWN rejected] The team already has {} subagents. RETIRE one first.".format(MAX_SUBAGENTS)
            member = Agent(name, role, clip(mission, 1500))
            member.status = "working"
            member.current = "Starting"
            self.agents[name.lower()] = member
        self._emit("agent", agent=self._agent_view(member, entries=0))
        self._activity(agent, "spawn", "Added {} ({})".format(name, role), detail=mission)
        self._launch(member)
        self.post("lead", name, mission, kind="assignment")
        return "[SPAWN ok] {} ({}) is running.".format(name, role)

    def _assign(self, agent, args, body):
        name = args.strip().split()[0] if args.strip() else ""
        target = self._find(name)
        if target is None or target.lead:
            return "[ASSIGN rejected] No active subagent named '{}'.".format(name)
        text = body.strip()
        if not text:
            return "[ASSIGN rejected] The instruction is empty."
        if target.status == "blocked":
            target.history = []  # a blocked agent's history holds the very mistakes that stopped it: start it clean
            target.temperature = RETRY_TEMPERATURE
        target.reset_task()
        self._set_status(target, "working", task=clip(text, 1500), current="Starting")
        self._activity(agent, "assign", "Gave {} a task".format(target.name), detail=text)
        self.post("lead", target.name, text, kind="assignment")
        return "[ASSIGN ok] {} received the task.".format(target.name)

    def _retire(self, agent, args):
        name = args.strip().split()[0] if args.strip() else ""
        target = self._find(name)
        if target is None or target.lead:
            return "[RETIRE rejected] No active subagent named '{}'.".format(name)
        target.retired = True
        target.wake.set()
        self._set_status(target, "retired", current="")
        self._activity(agent, "retire", "Retired {}".format(target.name))
        self.post("lead", "all", "{} was retired.".format(target.name), kind="system")
        return "[RETIRE ok] {} stopped.".format(target.name)

    def _approval(self, agent, kind, title, detail="", command="", path=""):
        host = self.host
        if host.settings.get("always_allow"):
            return True
        approval_id = uuid.uuid4().hex
        gate = threading.Event()
        pending = {"event": gate, "approved": None, "chat_id": self.chat_id, "created": time.time()}
        payload = {"id": approval_id, "kind": kind, "title": title, "detail": detail, "command": command, "path": path,
                   "agent": agent.name}
        with host.lock:
            host.pending_approvals[approval_id] = pending
        with self.lock:
            self.pending[approval_id] = payload
        previous = agent.status
        self._set_status(agent, "waiting", current="Waiting for your approval")
        self._emit("approval", approval=payload)
        deadline = time.time() + APPROVAL_TIMEOUT
        while pending["approved"] is None and time.time() < deadline and not self.stop_event.is_set():
            gate.wait(1.0)
        approved = pending["approved"] is True
        with host.lock:
            host.pending_approvals.pop(approval_id, None)
        with self.lock:
            self.pending.pop(approval_id, None)
        self._emit("approval_resolved", id=approval_id, approved=approved)
        if not self.stop_event.is_set() and agent.status == "waiting":
            self._set_status(agent, previous if previous != "waiting" else "working", current="")
        return approved

    @staticmethod
    def _sha(data):
        return hashlib.sha256(data if isinstance(data, bytes) else str(data).encode("utf-8")).hexdigest()

    def _mark_change(self, agent):
        self.last_change_ts = time.time()
        self.last_writer = agent.name

    def _tool(self, agent, verb, args, body):
        host = self.host
        argument = (args or body or "").strip()
        if verb == "READ":
            self._set_current(agent, "Reading " + clip(argument, 60), force=True)
            path = host.resolve_user_read_path(self.chat_id, argument)
            if not self._within_scope(path):
                return ("[READ rejected] {} is outside the project workspace ({}). Stay inside the workspace. "
                        "If you need something from elsewhere, ask lead.").format(path, self._workspace_label())
            if path.is_dir():
                self._activity(agent, "list", "Listed " + (self._rel(path) or "."), ok=True)
                return self._list_dir(path, "READ")
            if path.stat().st_size > 2_000_000:
                return "[READ failed] {} is larger than 2 MB.".format(path)
            raw = path.read_bytes()
            if b"\x00" in raw[:4096]:
                return "[READ failed] {} looks binary.".format(path)
            agent.seen[str(path)] = self._sha(raw)
            agent.evidence += 1
            self._activity(agent, "read", "Read {}".format(self._rel(path)), ok=True)
            return "[READ {}]\n{}".format(path, clip(raw.decode("utf-8", errors="replace"), READ_LIMIT))
        if verb == "LIST":
            self._set_current(agent, "Listing " + clip(argument or ".", 60), force=True)
            path = host.chat_work_root(self.chat_id) if not argument or argument == "." else host.resolve_user_read_path(self.chat_id, argument)
            if not self._within_scope(path):
                return ("[LIST rejected] {} is outside the project workspace ({}). Stay inside the workspace.").format(path, self._workspace_label())
            self._activity(agent, "list", "Listed " + (self._rel(path) or "."), ok=True)
            return self._list_dir(path, "LIST")
        if verb == "SEARCH":
            self._set_current(agent, "Searching: " + clip(argument, 60), force=True)
            result = host.search_web(argument)
            if not result.get("ok"):
                self._activity(agent, "search", "Search failed: " + clip(argument, 80), detail=str(result.get("message", "")), ok=False)
                return "[SEARCH failed] {}".format(result.get("message", "unknown error"))
            agent.evidence += 1
            lines = ["[SEARCH results for: {}]".format(argument)]
            for number, item in enumerate(result.get("results", []), 1):
                lines.append("{}. {}\nURL: {}\nSnippet: {}".format(number, item.get("title", "Untitled"), item.get("url", ""), item.get("snippet", "")))
            self._activity(agent, "search", "Searched: " + clip(argument, 80), ok=True)
            return clip("\n\n".join(lines), RESULT_LIMIT)
        if verb == "WRITE":
            target = args.strip().strip("\"'")
            if not target:
                return "[WRITE rejected] No path was given."
            content = strip_fence(body)
            if not content.endswith("\n"):
                content += "\n"
            resolved = host.resolve_working_path(self.chat_id, target, allow_missing=True)
            rel = self._rel(resolved)
            new_sha = self._sha(content)
            current_sha = self._sha(resolved.read_bytes()) if resolved.is_file() else None
            if current_sha is not None and current_sha != new_sha and agent.seen.get(str(resolved)) != current_sha:
                owner = self.file_state.get(rel, {}).get("by")
                return ("[WRITE held] {} already exists{} and you have not read its current version. "
                        "READ it first, then send the WRITE again, so you do not overwrite work you have not seen.").format(
                            rel, " (last changed by {})".format(owner) if owner and owner != agent.name else "")
            self._set_current(agent, "Writing " + rel, force=True)
            if not self._approval(agent, "write_file", "{} wants to write a file".format(agent.name),
                                  detail="{}\n\n{}".format(resolved, clip(content, 4000)), path=str(resolved)):
                self._trace("declined", agent, verb="WRITE", path=rel)
                self._activity(agent, "write", "Write declined: " + rel, ok=False)
                return "[WRITE declined] The user declined this change. Nothing was written: {}".format(target)
            chat = host.get_chat(self.chat_id) or {}
            with self.file_lock:
                resolved = host.resolve_working_path(self.chat_id, target, allow_missing=True)
                current = hashlib.sha256(resolved.read_bytes()).hexdigest() if resolved.is_file() else None
                outcome = host.apply_approved_file(self.chat_id, target, content, current, str(chat.get("linked_folder") or ""))
            size = len(content.encode("utf-8"))
            info = {"by": agent.name, "ts": time.time(), "sha": new_sha, "size": size, "rel": rel}
            with self.lock:
                self.file_state[rel] = info
                agent.files[rel] = size
            agent.seen[str(resolved)] = new_sha
            agent.evidence += 1
            if current != new_sha:
                self._mark_change(agent)
            self._emit("file", file={"path": rel, "by": agent.name, "ts": info["ts"], "size": size})
            self._activity(agent, "write", "Wrote {} ({})".format(rel, human_size(size)), detail=clip(content, 1800), ok=True)
            return "[WRITE succeeded] {}".format(outcome.get("message") or resolved)
        if verb == "RUN":
            # '@@@ RUN python3 -c "' followed by a body: the first line is part of the command, never discard it.
            command = "\n".join(part for part in (args.strip(), body.strip()) if part)
            tty = None
            first_word = args.strip().split(None, 1)
            if first_word and first_word[0].lower() == "tty":
                inline = first_word[1].strip() if len(first_word) > 1 else ""
                command_lines, key_lines = [], []
                for line in (body.strip() or inline).splitlines():
                    if line.strip().upper().startswith("KEYS:"):
                        key_lines.append(line.split(":", 1)[1])
                    else:
                        command_lines.append(line)
                command = "\n".join(command_lines).strip()
                tty = {"keys": parse_tty_keys(" ".join(key_lines)), "timeout": TTY_TIMEOUT}
            if not command:
                return "[RUN rejected] No command was given."
            self._set_current(agent, "Running: " + clip(command.split("\n")[0], 80), force=True)
            readonly = False
            try:
                readonly = bool(host.is_readonly_command(self.chat_id, command))
            except Exception:
                readonly = False
            if not readonly and not self._approval(agent, "command", "{} wants to run a command".format(agent.name),
                                                   detail="Runs with your account's permissions in the project workspace.", command=command):
                self._trace("declined", agent, verb="RUN", command=command)
                self._activity(agent, "run", "Command declined", detail=command, ok=False)
                return "[RUN declined] The user declined this command. It was not executed: {}".format(command)
            try:
                host.prewarm_chat_environment(self.chat_id, with_pip=host._command_needs_pip(command))
            except Exception:
                pass
            before = self._signature()
            try:
                before_outputs = host._snapshot_outputs(host.chat_work_root(self.chat_id))
            except Exception:
                before_outputs = {}
            terminal_id = uuid.uuid4().hex[:12]
            terminal = {"id": terminal_id, "command": command, "output": "$ " + command + "\n",
                        "ts": time.time(), "live": True}
            with self.lock:
                self.terminals.append(terminal)
                self.terminals = self.terminals[-8:]
            self._emit("terminal", action="start", id=terminal_id, command=command, ts=terminal["ts"])

            def stream_terminal(chunk, reset=False):
                text_chunk = str(chunk or "")
                with self.lock:
                    for item in reversed(self.terminals):
                        if item.get("id") == terminal_id:
                            item["output"] = text_chunk if reset else str(item.get("output") or "") + text_chunk
                            item["output"] = item["output"][-24000:]
                            break
                self._emit("terminal", action="chunk", id=terminal_id, text=text_chunk, reset=bool(reset))

            try:
                if tty is not None:
                    outcome = host.run_command(self.chat_id, command, tty=tty, on_output=stream_terminal)
                else:
                    outcome = host.run_command(self.chat_id, command, on_output=stream_terminal)
            except TypeError as exc:
                if "on_output" not in str(exc):
                    raise
                outcome = host.run_command(self.chat_id, command, **({"tty": tty} if tty is not None else {}))
                if outcome.get("output"):
                    stream_terminal(outcome.get("output"))
            extracting = is_extract_command(command)
            if not extracting and self._signature() != before:
                self._mark_change(agent)
            agent.evidence += 1
            ok = bool(outcome.get("ok"))
            exit_code = outcome.get("returncode", -1)
            elapsed = outcome.get("elapsed")
            with self.lock:
                for item in reversed(self.terminals):
                    if item.get("id") == terminal_id:
                        item.update({"live": False, "ok": ok, "returncode": exit_code, "elapsed": elapsed})
                        item["output"] = (str(item.get("output") or "") + "\n[Exit {} · {}s]".format(exit_code, elapsed if elapsed is not None else "?"))[-24000:]
                        break
            self._emit("terminal", action="done", id=terminal_id, returncode=exit_code,
                       ok=ok, elapsed=elapsed)
            try:
                for generated in ([] if extracting else host.collect_user_outputs(self.chat_id, before_outputs)):
                    row = {"by": agent.name, "ts": time.time(), "size": generated.get("size", 0),
                           "rel": generated.get("path", ""), "output": True, "mime": generated.get("mime", ""),
                           "area": generated.get("area", "work")}
                    with self.lock:
                        self.file_state[generated["path"]] = row
                    self._emit("file", file={"path": generated["path"], **row})
            except Exception:
                pass
            self._trace("command", agent, command=command, readonly=readonly, tty=tty, ok=ok, returncode=outcome.get("returncode", -1),
                        output=outcome.get("output") or "")
            text = "[RUN result]\nCommand: {}\nExit code: {}\nStatus: {}\nOutput:\n{}".format(
                command, outcome.get("returncode", -1), "succeeded" if ok else "failed",
                clip(outcome.get("output") or "(no output)", RESULT_LIMIT))
            if agent.lead:
                agent.last_report_ts = time.time()
                agent.last_result = "PASS" if ok else "FAIL"
            self._activity(agent, "run", "{} `{}`".format("Ran" if ok else "Command failed:", clip(command.split("\n")[0], 90)),
                           detail="exit code {}\n{}".format(outcome.get("returncode", -1), clip(outcome.get("output") or "(no output)", 2000)), ok=ok)
            return text
        return ""

    def _list_dir(self, path, label):
        try:
            rel = self._rel(path)
        except Exception:
            rel = str(path)
        parent = "./" if rel in ("", ".") else "./" + str(rel).lstrip("/")
        try:
            entries = sorted(path.iterdir(), key=lambda entry: (not entry.is_dir(), entry.name.casefold()))
        except OSError as exc:
            return "[{} failed] {}".format(label, exc)
        rows = []
        for item in entries[:80]:
            try:
                shown = "./" + item.name
                if item.is_symlink():
                    rows.append("{} (symlink)".format(shown))
                elif item.is_dir():
                    rows.append("{}/".format(shown))
                else:
                    rows.append("{} ({} bytes)".format(shown, item.stat().st_size))
            except OSError:
                continue
        if len(entries) > len(rows):
            rows.append("… {} more entries not shown".format(len(entries) - len(rows)))
        text = "[{}]\nParent: {}\n{}".format(label, parent, "\n".join(rows) or "(empty)")
        if not rows:
            text += ("\nThis folder is empty: nothing has been created here yet. Do NOT search other folders or invent files. "
                     "If your task depends on a teammate's output, report BLOCKED and say who you are waiting for; otherwise create what your task needs.")
        return text

    def _status_report(self):
        with self.lock:
            members = [item for item in self.agents.values() if not item.lead and item.status != "retired"]
            plan = self.plan.strip()
            recent = [item for item in self.channel if item.get("from") not in ("user", "system")][-3:]
        lines = []
        if self.finished:
            lines.append("The project is finished.")
        if members:
            lines.append("Team status:")
            for member in members:
                report = clip(member.summary, 220).replace("\n", " ") or "no report yet"
                lines.append("- {} ({}): {}. Last report: {}".format(member.name, clip(member.role, 60), member.status, report))
        else:
            lines.append("No subagents are running yet.")
        if plan:
            lines.append("Plan:\n" + clip(plan, 700))
        if recent:
            lines.append("Latest updates:")
            for item in recent:
                lines.append("- {}: {}".format(item.get("from"), clip(item.get("text"), 200).replace("\n", " ")))
        return "\n".join(lines)

    def _quick_reply(self, message):
        lead = self.lead
        try:
            state = "{}\n\nTHE HUMAN ASKED: {}".format(self.shared_context(lead), message["text"])
            messages = [{"role": "system", "content": QUICK_SYSTEM}, {"role": "user", "content": state}]
            _reasoning, answer = self._generate(lead, messages, max_tokens=QUICK_REPLY_TOKENS, entries=False)
            prose, _actions = parse_actions(answer)
            answer = prose.strip()
            if not answer:
                answer = "Got it. I will finish the current step and reply in full."
            if answer:
                message["quick_reply"] = answer
                self.post("lead", "user", answer, kind="quick")
        except StopRequested:
            return
        except Exception as exc:
            self._trace("quick_reply_error", lead, error=exc)
            self._note_entry(lead, "error", "Quick reply failed: {}".format(clip(exc, 300)))


def strip_fence(body):
    stripped = body.strip("\n")
    match = re.match(r"^```[^\n]*\n(.*)\n```\s*$", stripped, re.S)
    return match.group(1) if match else body


def human_size(size):
    size = float(size)
    for unit in ("B", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return "{:.0f} {}".format(size, unit) if unit == "B" else "{:.1f} {}".format(size, unit)
        size /= 1024.0
    return "{} B".format(int(size))


def chat_history(host, chat_id):
    chat = host.get_chat(chat_id) or {}
    return [dict(item) for item in chat.get("messages", []) if isinstance(item, dict) and item.get("agent_mode")][-40:]


class AgentManager:
    def __init__(self, host):
        self.host = host
        self.lock = threading.Lock()
        self.swarms = {}

    def flush(self, chat_id):
        with self.lock:
            swarm = self.swarms.get(chat_id)
        if swarm is not None:
            try:
                swarm._persist(force=True)
            except Exception:
                pass

    def running_chat(self):
        with self.lock:
            for chat_id, swarm in self.swarms.items():
                if swarm.running:
                    return chat_id
        return None

    def send(self, chat_id, text, route_info, message_id="", continuous=None):
        with self.lock:
            swarm = self.swarms.get(chat_id)
            if swarm is not None and swarm.running:
                swarm.user_message(text, message_id, continuous)
                return {"ok": True, "running": True, "started": False, "continuous": swarm.continuous}
            for other_id, other in self.swarms.items():
                if other_id != chat_id and other.running:
                    raise ValueError("Agent mode is already running in another chat. Stop it there before starting here.")
            chat = self.host.get_chat(chat_id)
            if chat is None:
                raise ValueError("Conversation not found.")
            swarm = AgentSwarm(self.host, chat_id, route_info, restore=chat.get("agent") if isinstance(chat.get("agent"), dict) else None)
            self.swarms[chat_id] = swarm
            swarm.start(text, message_id, continuous)
        return {"ok": True, "running": True, "started": True, "continuous": swarm.continuous}

    def set_continuous(self, chat_id, value):
        value = bool(value)
        with self.lock:
            swarm = self.swarms.get(chat_id)
        if swarm is not None and swarm.running:
            swarm.set_continuous(value)
            return {"ok": True, "continuous": swarm.continuous, "running": True}
        chat = self.host.get_chat(chat_id)
        if chat is None:
            raise ValueError("Conversation not found.")
        saved = chat.get("agent")
        if isinstance(saved, dict):
            with self.host.lock:
                saved["continuous"] = value
            self.host.save_chats()
        return {"ok": True, "continuous": value, "running": False}

    def stop(self, chat_id):
        with self.lock:
            swarm = self.swarms.get(chat_id)
        if swarm is None or not swarm.running:
            return {"ok": True, "running": False}
        swarm.stop()
        return {"ok": True, "running": False}

    def poll(self, chat_id, after, epoch=""):
        with self.lock:
            swarm = self.swarms.get(chat_id)
        if swarm is not None:
            return swarm.events_since(after, epoch)
        chat = self.host.get_chat(chat_id) or {}
        saved = chat.get("agent")
        if not isinstance(saved, dict):
            return {"seq": 0, "epoch": "", "running": False, "snapshot": None, "events": []}
        if epoch == "" and after == 0:
            return {"seq": 0, "epoch": "", "running": False, "events": []}
        status = "finished" if saved.get("finished") else "stopped"
        agents = [{"name": "lead", "role": "Lead orchestrator", "task": "", "status": status, "current": "", "summary": str((saved.get("lead") or {}).get("summary") or ""),
                   "turns": 0, "retry_at": 0, "lead": True, "files": [], "result": "", "blocked": "", "report_ts": 0, "strikes": 0,
                   "entries": (saved.get("lead") or {}).get("entries") or []}]
        for item in saved.get("agents") or []:
            if isinstance(item, dict) and not item.get("retired"):
                agents.append({"name": item.get("name"), "role": item.get("role"), "task": item.get("task"), "status": status, "current": "",
                               "summary": item.get("summary"), "turns": 0, "retry_at": 0, "lead": False, "files": [],
                               "result": item.get("result") or "", "blocked": "", "report_ts": 0, "strikes": 0, "entries": []})
        snapshot = {"running": False, "finished": bool(saved.get("finished")), "finish_summary": saved.get("finish_summary", ""),
                    "continuous": bool(saved.get("continuous")), "idle_paused": False, "goal": saved.get("goal", ""), "plan": saved.get("plan", ""),
                    "notes": saved.get("notes") or {}, "channel": (saved.get("channel") or [])[-150:], "activity": (saved.get("activity") or [])[-300:],
                    "files": saved.get("files") or [], "agents": agents, "approvals": [], "cooldown_until": 0, "cooldown_reason": "",
                    "model": "", "seq": 0, "started_at": saved.get("updated") or 0, "now": time.time(), "chat": chat_history(self.host, chat_id)}
        return {"seq": 0, "epoch": "", "running": False, "snapshot": snapshot, "events": []}