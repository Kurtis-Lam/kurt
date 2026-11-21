"""Kurt agent mode.

An always-on lead agent orchestrates a team of subagents that share one
context (a channel, shared notes, and a plan). Everything runs in background
threads, keeps going until stopped, retries failed or rate-limited model calls
after one minute, and stays reachable: the user can message the lead at any
time and receive an answer while the team keeps working.

The module uses only the Python standard library. All Kurt services (model
routing, workspace files, commands, approvals, usage) are reached through the
``host`` object, which is the KurtWebApp instance.
"""

import difflib
import hashlib
import re
import threading
import time
import uuid
from collections import deque

RETRY_SECONDS = 60
MAX_SUBAGENTS = 8
MAX_EVENTS = 4000
MAX_CHANNEL = 400
MAX_ENTRIES = 80
MAX_HISTORY = 14
MAX_TOOL_ROUNDS = 30
LEAD_TOOL_ROUNDS = 6
LEAD_HEARTBEAT = 90
GENERATION_TOKENS = 6144
QUICK_REPLY_TOKENS = 1024
APPROVAL_TIMEOUT = 1800
RESULT_LIMIT = 12000
READ_LIMIT = 30000
EVENT_TEXT_LIMIT = 6000
PERSIST_INTERVAL = 3.0

THINK_TAG_RE = re.compile(r"<(/)?think(?:ing)?>", re.I)
STRAY_CLOSE_RE = re.compile(r"</think(?:ing)?>", re.I)
RATE_LIMIT_RE = re.compile(r"429|rate.?limit|quota|overload|too many|capacity|throttl", re.I)
BLOCK_START_RE = re.compile(r"^\s*@@@\s*([A-Za-z_]+)\b[ \t]*(.*)$")
BLOCK_END_RE = re.compile(r"^\s*@@@\s*END\b", re.I)
VERBS = {"SPAWN", "ASSIGN", "RETIRE", "PLAN", "WAIT", "SAY", "NOTE", "DONE",
         "READ", "LIST", "WRITE", "RUN", "SEARCH"}
LEAD_ONLY = {"SPAWN", "ASSIGN", "RETIRE", "PLAN", "WAIT"}
TOOL_VERBS = {"READ", "LIST", "WRITE", "RUN", "SEARCH"}


class StopRequested(Exception):
    pass


def clip(text, limit):
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "\n… [truncated {} characters]".format(len(text) - limit)


def clip_tail(text, limit):
    text = str(text or "")
    if len(text) <= limit:
        return text
    return "… " + text[-limit:]


def slug(value, fallback="agent"):
    value = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "").strip()).strip("-_")
    return (value or fallback)[:24]


FUNC_CALL_RE = re.compile(r"<function=\s*([A-Za-z_][\w.-]*)\s*>(.*?)(?:</function>|\Z)", re.S | re.I)
FUNC_PARAM_RE = re.compile(r"<parameter=\s*([A-Za-z_][\w.-]*)\s*>\s*(.*?)\s*(?:</parameter>|(?=<parameter=)|\Z)", re.S | re.I)
WRAPPER_TAG_RE = re.compile(r"</?\s*(?:tool_call|tool_calls|function_calls|function|parameter|invoke)\b[^>]*>", re.I)
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


def _function_to_block(name, raw_params):
    verb = re.sub(r"[^A-Za-z_]", "_", name).upper()
    verb = FUNC_ALIASES.get(verb, verb)
    if verb not in VERBS:
        return ""
    params = {key.lower(): value for key, value in raw_params}
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
    elif verb == "DONE":
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


def normalize_function_calls(text):
    """Rewrite <function=NAME><parameter=key>value</parameter></function> calls as @@@ blocks."""
    text = str(text or "")
    if "<function" not in text.lower():
        return text

    def convert(match):
        params = [(item.group(1), item.group(2)) for item in FUNC_PARAM_RE.finditer(match.group(2))]
        return "\n" + _function_to_block(match.group(1), params) + "\n"

    return FUNC_CALL_RE.sub(convert, text)


def strip_markup(text):
    """Remove leftover tool-call tags and stray @@@ lines from text meant for people."""
    text = WRAPPER_TAG_RE.sub("", str(text or ""))
    lines = [line for line in text.split("\n") if not BLOCK_END_RE.match(line)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


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


def parse_actions(text):
    """Split model output into (prose, [(verb, args, body)])."""
    lines = normalize_function_calls(text).split("\n")
    prose, actions = [], []
    index = 0
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
            body = []
            index += 1
            while index < len(lines):
                if BLOCK_END_RE.match(lines[index]):
                    index += 1
                    break
                nxt = BLOCK_START_RE.match(lines[index])
                if nxt and nxt.group(1).upper() in VERBS:
                    break
                body.append(lines[index])
                index += 1
            actions.append((verb, args, "\n".join(body)))
        else:
            prose.append(lines[index])
            index += 1
    return strip_markup("\n".join(prose)), actions


def strip_fence(body):
    stripped = body.strip("\n")
    match = re.match(r"^```[^\n]*\n(.*)\n```\s*$", stripped, re.S)
    return match.group(1) if match else body


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


COMMON_SYNTAX = """
ACTION BLOCKS
You act by writing action blocks. A block looks like this:
@@@ VERB arguments
optional body
@@@ END
Use ONLY this @@@ block format. Never write <function=...>, <parameter=...>, <tool_call>, JSON tool calls or any other tag-based call; they are not executed. Every block starts with @@@ VERB and ends with @@@ END.
Plain text outside blocks is your private running commentary; it appears in your own panel and is delivered to no one. To tell anyone anything, use SAY.

Verbs available to every agent:
- SAY <target>   Body is the message. Targets: lead, all, a teammate's name{user_target}. Every message is visible to the whole team and to the user.
- NOTE <key>     Body is a fact, decision, interface contract, file map or result to keep in the shared notes that every teammate reads. Reusing a key overwrites it. Keep notes short and current.
- READ <path>    Read a text file (or list a folder). Relative paths resolve inside the shared project workspace.
- LIST <path>    List a folder. An empty path lists the project workspace.
- SEARCH <query> Search the web.
- WRITE <path>   Body is the complete new content of the file. Paths are relative to the shared project workspace.
- RUN            Body is one shell command, run in the shared project workspace.
Tool results come back in the next message. Never claim a file was saved or a command succeeded until its result says so. File changes and commands that change state may wait for the human's approval; keep working on other things or wait for the result.
All agents work in the same project workspace, so coordinate who owns which file before writing and read a file again before editing it.
"""

LEAD_SYSTEM = """You are LEAD, the orchestrator of Kurt's autonomous agent team. You run continuously until the human stops you. You are always reachable: the human can message you at any moment, and you answer them first.

Your job:
1. Turn the human's goal into a plan (PLAN) and keep it current.
2. Split the work across specialised subagents (SPAWN) with a clear role, mission and file ownership: for example logic, UI, tester, debugger, reviewer. Spawn only what the work needs, at most {max_agents} at once.
3. Coordinate: answer their questions, resolve conflicts, hand out new work (ASSIGN), retire agents that are no longer useful (RETIRE), and record shared decisions with NOTE.
4. Never declare the project finished for good. When a milestone is complete, define the next improvement: more tests, bug hunting, polish, performance, docs. If the team has nothing useful left to do, WAIT.
5. Do not SAY user on every team message. Message the human only when they asked something, when a real milestone is reached or when you are blocked, and never repeat a status you already gave. When the human messages you, reply with SAY user using real facts from the live team state (who is doing what, what finished, what is blocked, what comes next). Be concrete and brief. If the message contains an instruction, act on it and tell them what you changed.
6. Do the work through the team, not alone. Use READ, LIST, SEARCH and RUN only to inspect and verify.

Lead-only verbs:
- SPAWN <name> | <role>   Body is the agent's mission: goal, files it owns, interfaces it must respect, and how to report back.
- ASSIGN <name>           Body is a new instruction or task for an existing agent.
- RETIRE <name>           Stop an agent permanently.
- PLAN                    Body replaces the shared plan (a short ordered list with owners and status).
- WAIT <seconds>          Sleep until the next review (5 to 900) unless something happens sooner. Use it when the team is busy and nothing needs you.
{syntax}"""

AGENT_SYSTEM = """You are {name}, a subagent on Kurt's autonomous agent team. Your role: {role}.
You work in parallel with teammates and share one context with them: the channel, the shared notes and the plan are shown below and everyone sees the same thing.

How you work:
- Do your assigned task fully with tools. Own your files; ask the owner (SAY <name>) before changing someone else's, and read the current file first.
- Communicate with teammates directly and briefly when you need an interface, a decision, a review or a bug fixed. Report blockers to lead.
- Publish contracts, file maps and key decisions with NOTE so teammates stay in sync.
- When your task is complete, or you are blocked and cannot continue, finish with DONE and a short summary of what you did, which files you touched and what remains. The lead will give you more work.
- Never talk to the human directly; report to lead.
- Do not repeat yourself or chat for its own sake. If you have nothing to do, finish with DONE.
Extra verb:
- DONE   Body is your summary. Ends your current task.
{syntax}"""

QUICK_SYSTEM = """You are LEAD answering the human while your main orchestration loop is busy. Answer using only the live team state below. Be direct and concrete in 2 to 6 sentences: what is done, who is doing what, what is blocked, and what is next. If the human gave an instruction, say it has been passed to your orchestration loop. Do not use action blocks."""


class Agent:
    def __init__(self, name, role, task="", lead=False):
        self.name = name
        self.role = role
        self.task = task
        self.lead = lead
        self.status = "idle"
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
        self.notes = {}
        self.plan = ""
        self.goal = ""
        self.agents = {}
        self.pending = {}
        self.cooldown_until = 0.0
        self.cooldown_reason = ""
        self.file_lock = threading.Lock()
        self.last_persist = 0.0
        self.started_at = time.time()
        self.user_msg_count = 0
        self.recent_user_posts = []
        self.lead = Agent("lead", "Lead orchestrator", lead=True)
        self.agents["lead"] = self.lead
        self.restored = bool(restore)
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
        notes = data.get("notes")
        if isinstance(notes, dict):
            for key, item in list(notes.items())[:200]:
                if isinstance(item, dict):
                    self.notes[str(key)[:60]] = {"value": str(item.get("value") or ""), "by": str(item.get("by") or ""),
                                                 "ts": float(item.get("ts") or time.time())}
        channel = data.get("channel")
        if isinstance(channel, list):
            self.channel = [item for item in channel if isinstance(item, dict)][-MAX_CHANNEL:]
        for item in data.get("agents") or []:
            if not isinstance(item, dict) or item.get("name") == "lead" or item.get("retired"):
                continue
            name = slug(item.get("name"))
            agent = Agent(name, str(item.get("role") or "Agent")[:120], str(item.get("task") or ""))
            agent.summary = str(item.get("summary") or "")
            agent.status = "working" if item.get("status") in ("working", "retrying", "waiting") else "idle"
            self.agents[name.lower()] = agent
        self.lead.summary = str((data.get("lead") or {}).get("summary") or "")

    def start(self, first_message):
        self.lead.thread = threading.Thread(target=self._lead_loop, name="kurt-agent-lead", daemon=True)
        self.lead.thread.start()
        for agent in list(self.agents.values()):
            if not agent.lead:
                self._launch(agent, resumed=True)
        if self.restored:
            self.lead.inbox.append({"from": "system", "text": "Agent mode was restarted. Review the restored plan, notes, channel and team, then continue the work.", "ts": time.time()})
        self._emit("state", running=True)
        self.user_message(first_message)

    def stop(self):
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
                self._set_status(agent, "stopped")
        self._emit("state", running=False)
        self._persist(force=True)

    def _emit(self, kind, **payload):
        with self.lock:
            self.seq += 1
            event = {"seq": self.seq, "ts": round(time.time(), 3), "type": kind}
            event.update(payload)
            self.events.append(event)
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
        if done:
            entry["done"] = True
        if not (force or done) and now - entry["emitted"] < 0.4:
            return
        entry["emitted"] = now
        self._emit("entry", agent=agent.name, entry=self._entry_view(entry))

    def _entry_append(self, agent, entry, text):
        entry["text"] = clip_tail(entry["text"] + text, 20000) if len(entry["text"]) > 20000 else entry["text"] + text
        self._entry_emit(agent, entry)

    def _entry_set(self, agent, entry, text, done=True):
        entry["text"] = str(text)
        self._entry_emit(agent, entry, force=True, done=done)

    def _note_entry(self, agent, kind, text):
        entry = self._entry_open(agent, kind, text)
        self._entry_emit(agent, entry, done=True)
        return entry

    def _set_status(self, agent, status, task=None):
        changed = agent.status != status
        agent.status = status
        if task is not None:
            agent.task = task
            changed = True
        if changed:
            self._emit("agent", agent=self._agent_view(agent, entries=0))

    def _agent_view(self, agent, entries=25, text_cap=4000):
        with self.lock:
            items = list(agent.entries)[-entries:] if entries else []
        return {"name": agent.name, "role": agent.role, "task": clip(agent.task, 600), "status": agent.status,
                "summary": clip(agent.summary, 800), "turns": agent.turns, "retry_at": agent.retry_at,
                "lead": agent.lead, "entries": [self._entry_view(item, text_cap) for item in items]}

    def snapshot(self):
        with self.lock:
            agents = [self._agent_view(agent) for agent in self.agents.values() if agent.status != "retired"]
            channel = list(self.channel)[-150:]
            notes = {key: {"value": clip(item["value"], 3000), "by": item["by"], "ts": item["ts"]} for key, item in self.notes.items()}
            approvals = list(self.pending.values())
            return {"running": self.running, "goal": self.goal, "plan": self.plan, "notes": notes, "channel": channel,
                    "agents": agents, "approvals": approvals, "cooldown_until": self.cooldown_until,
                    "cooldown_reason": self.cooldown_reason, "model": self.model_name, "seq": self.seq,
                    "chat": chat_history(self.host, self.chat_id)}

    def events_since(self, after, epoch=""):
        with self.lock:
            first = self.events[0]["seq"] if self.events else self.seq + 1
            if epoch != self.epoch or after < 0 or after < first - 1 or after > self.seq:
                return {"seq": self.seq, "epoch": self.epoch, "running": self.running, "snapshot": self.snapshot(), "events": []}
            events = [event for event in self.events if event["seq"] > after][:800]
            seq = events[-1]["seq"] if events else self.seq
            return {"seq": seq, "epoch": self.epoch, "running": self.running, "events": events,
                    "cooldown_until": self.cooldown_until, "cooldown_reason": self.cooldown_reason}

    def state_for_persistence(self):
        with self.lock:
            agents = []
            for agent in self.agents.values():
                if agent.lead:
                    continue
                agents.append({"name": agent.name, "role": agent.role, "task": clip(agent.task, 1500), "status": agent.status,
                               "summary": clip(agent.summary, 1500), "retired": agent.retired})
            notes = {key: {"value": clip(item["value"], 4000), "by": item["by"], "ts": item["ts"]} for key, item in self.notes.items()}
            lead_entries = [self._entry_view(item, 1500) for item in list(self.lead.entries)[-8:]]
            return {"goal": self.goal, "plan": self.plan, "notes": notes, "channel": list(self.channel)[-120:],
                    "agents": agents, "running": self.running, "updated": time.time(),
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

    def post(self, sender, target, text, kind="message"):
        text = clip(str(text or "").strip(), 4000)
        if not text:
            return None
        target = str(target or "all").strip()
        lowered = target.lower()
        if lowered in ("user", "human") and sender != "lead":
            lowered = "lead"
            text = "(for the user) " + text
        recipient = None
        if lowered in ("all", "everyone", "team", "*"):
            lowered = "all"
        elif lowered == "user" or lowered == "human":
            lowered = "user"
        else:
            recipient = self._find(lowered)
            if recipient is None:
                text = "(undeliverable: no active agent named '{}') {}".format(target, text)
                lowered = "lead" if sender != "lead" else "all"
                recipient = self._find(lowered) if lowered != "all" else None
        if lowered == "user" and kind != "quick" and self._is_repeat_for_user(text):
            return None
        message = {"id": uuid.uuid4().hex[:10], "ts": round(time.time(), 3), "from": sender, "to": lowered, "text": text, "kind": kind}
        with self.lock:
            self.channel.append(message)
            if len(self.channel) > MAX_CHANNEL:
                del self.channel[:-MAX_CHANNEL]
        self._emit("channel", message=message)
        if lowered == "user":
            self._deliver_to_user(text, sender)
        elif lowered == "all":
            if sender != "lead":
                self.lead.inbox.append(message)
                self.lead.wake.set()
        elif recipient is not None:
            recipient.inbox.append(message)
            recipient.wake.set()
        return message

    def _is_repeat_for_user(self, text):
        norm = " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())
        with self.lock:
            recent = [item for item in self.recent_user_posts if item[0] == self.user_msg_count][-4:]
            for _count, earlier in recent:
                if norm == earlier or difflib.SequenceMatcher(None, norm, earlier).ratio() >= 0.8:
                    return True
            self.recent_user_posts.append((self.user_msg_count, norm))
            del self.recent_user_posts[:-12]
        return False

    def _deliver_to_user(self, text, sender="lead"):
        chat = self.host.get_chat(self.chat_id)
        record = {"role": "assistant", "content": text, "model": self.model_name, "route": self.route_info.get("route", ""),
                  "agent_mode": True, "agent": sender, "message_id": uuid.uuid4().hex}
        if chat is not None:
            with self.host.lock:
                chat["messages"].append(record)
                chat["updated"] = time.time()
            self.host.save_chats()
        self._emit("chat", message=record)

    def user_message(self, text, message_id=""):
        text = str(text or "").strip()
        if not text:
            return
        chat = self.host.get_chat(self.chat_id)
        record = {"role": "user", "content": text, "agent_mode": True, "message_id": message_id or uuid.uuid4().hex}
        if chat is not None:
            with self.host.lock:
                chat["messages"].append(record)
                chat["updated"] = time.time()
                if chat.get("title") in (None, "", "New conversation"):
                    chat["title"] = clip(text.replace("\n", " "), 60)
            self.host.save_chats()
        if not self.goal:
            self.goal = clip(text, 4000)
        with self.lock:
            self.user_msg_count += 1
        message = {"id": uuid.uuid4().hex[:10], "ts": round(time.time(), 3), "from": "user", "to": "lead", "text": text, "kind": "user"}
        with self.lock:
            self.channel.append(message)
        self._emit("chat", message=record)
        self._emit("channel", message=message)
        busy = self.lead.busy
        self.lead.inbox.append(message)
        self.lead.wake.set()
        if busy:
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
                self._set_status(agent, "retrying")
            self.stop_event.wait(min(1.0, remaining))
        raise StopRequested()

    def _on_failure(self, agent, exc, entry=None):
        detail = clip(str(exc), 600)
        reason = "rate limit" if RATE_LIMIT_RE.search(detail) else "error"
        with self.lock:
            self.cooldown_until = max(self.cooldown_until, time.time() + RETRY_SECONDS)
            self.cooldown_reason = reason
            agent.retry_at = self.cooldown_until
        self._note_entry(agent, "error", "The model request hit a {}: {}\nRetrying in {} seconds.".format(reason, detail, RETRY_SECONDS))
        self._set_status(agent, "retrying")
        self._emit("state", running=True, cooldown_until=self.cooldown_until, cooldown_reason=reason)

    def _generate(self, agent, messages, max_tokens=GENERATION_TOKENS, entries=True):
        """Return (reasoning, answer). Retries forever, one minute after each failure."""
        while True:
            if self.stop_event.is_set():
                raise StopRequested()
            self._wait_cooldown(agent)
            try:
                return self._generate_once(agent, messages, max_tokens, entries)
            except StopRequested:
                raise
            except Exception as exc:
                self._on_failure(agent, exc)

    def _generate_once(self, agent, messages, max_tokens, entries):
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
                    call_messages = list(messages)
                    if empty_retry:
                        call_messages.append({"role": "system", "content": "Your previous reply was empty. Reply now with visible text or action blocks."})
                    stream = host.agent_stream(candidate, call_messages, max_tokens=max_tokens, think=not empty_retry)
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
                            if piece_kind == "reasoning":
                                reasoning_parts.append(piece)
                                if entries:
                                    if reasoning_entry is None:
                                        reasoning_entry = self._entry_open(agent, "thinking")
                                    self._entry_append(agent, reasoning_entry, piece)
                            else:
                                answer_parts.append(piece)
                                if entries:
                                    if output_entry is None:
                                        output_entry = self._entry_open(agent, "output")
                                    self._entry_append(agent, output_entry, piece)
                    for piece_kind, piece in splitter.feed("", final=True):
                        (reasoning_parts if piece_kind == "reasoning" else answer_parts).append(piece)
                    reasoning, answer = split_stray_think("".join(reasoning_parts), "".join(answer_parts))
                    if not answer.strip() and not reasoning.strip():
                        raise RuntimeError("The model returned no response text.")
                    if reasoning_entry is not None:
                        self._entry_set(agent, reasoning_entry, reasoning)
                    if output_entry is not None:
                        self._entry_set(agent, output_entry, answer)
                    elif entries and answer:
                        self._note_entry(agent, "output", answer)
                    self.active_candidate = candidate_index
                    try:
                        host.record_usage(self.chat_id, candidate.get("model"), self.route_info.get("route"),
                                          round(in_chars / 4), round((len(reasoning) + len(answer)) / 4), True,
                                          time.monotonic() - started)
                    except Exception:
                        pass
                    return reasoning, answer
                except StopRequested:
                    raise
                except Exception as exc:
                    last_error = exc
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

    def team_table(self, exclude=""):
        rows = []
        with self.lock:
            agents = list(self.agents.values())
        for agent in agents:
            if agent.status == "retired" or agent.name == exclude:
                continue
            rows.append("- {} [{}] status={} task: {} | last report: {}".format(
                agent.name, agent.role, agent.status, clip(agent.task, 220).replace("\n", " ") or "none",
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
        return ("SHARED CONTEXT (identical for every agent)\nGOAL: {}\n\nPLAN:\n{}\n\nSHARED NOTES:\n{}\n\nTEAM:\n{}\n\nRECENT CHANNEL:\n{}\n\n"
                "PROJECT WORKSPACE: {}").format(
            clip(goal, 2500) or "(none yet)", clip(plan, 3000) or "(no plan yet)", "\n".join(note_lines) or "(no notes yet)",
            self.team_table(), "\n".join(chat_lines) or "(quiet)", self._workspace_label())

    def _workspace_label(self):
        try:
            return str(self.host.chat_work_root(self.chat_id))
        except Exception:
            return "(unavailable)"

    def _format_inbox(self, items):
        lines = []
        for item in items:
            extra = ""
            if item.get("quick_reply"):
                extra = "\n(Already answered by a quick status reply: {})".format(clip(item["quick_reply"], 600))
            lines.append("from {}: {}{}".format(item.get("from"), clip(item.get("text"), 3000), extra))
        return "\n\n".join(lines)

    @staticmethod
    def _drain(agent):
        items, agent.inbox = agent.inbox, []
        return items

    def _trim(self, agent):
        if len(agent.history) > MAX_HISTORY:
            del agent.history[:-MAX_HISTORY]
        for item in agent.history:
            item["content"] = clip(item["content"], 7000)

    def _system_for(self, agent):
        if agent.lead:
            syntax = COMMON_SYNTAX.replace("{user_target}", ", user")
            system = LEAD_SYSTEM.replace("{max_agents}", str(MAX_SUBAGENTS)).replace("{syntax}", syntax)
        else:
            syntax = COMMON_SYNTAX.replace("{user_target}", "")
            system = AGENT_SYSTEM.replace("{name}", agent.name).replace("{role}", agent.role).replace("{syntax}", syntax)
        return [{"role": "system", "content": system}, {"role": "system", "content": self.shared_context(agent)}]

    def _lead_loop(self):
        lead = self.lead
        reason = "start"
        while not self.stop_event.is_set():
            try:
                triggered = lead.wake.wait(timeout=lead.heartbeat)
                if self.stop_event.is_set():
                    break
                lead.wake.clear()
                reason = "new messages" if triggered else "scheduled review"
                self._agent_turn(lead, reason)
            except StopRequested:
                break
            except Exception as exc:
                self._note_entry(lead, "error", "Lead loop error: {}".format(clip(exc, 500)))
                self._wait(5)
            self._wait(2)
        self._set_status(lead, "stopped")

    def _agent_loop(self, agent):
        while not self.stop_event.is_set() and not agent.retired:
            try:
                if not agent.inbox:
                    if agent.status not in ("idle", "stopped"):
                        self._set_status(agent, "idle")
                    agent.wake.wait(timeout=3)
                    agent.wake.clear()
                    if not agent.inbox:
                        continue
                self._agent_turn(agent, "new messages")
            except StopRequested:
                break
            except Exception as exc:
                self._note_entry(agent, "error", "Agent loop error: {}".format(clip(exc, 500)))
                self._wait(5)
            self._wait(1)
        if agent.retired:
            self._set_status(agent, "retired")
        else:
            self._set_status(agent, "stopped")

    def _launch(self, agent, resumed=False):
        agent.thread = threading.Thread(target=self._agent_loop, args=(agent,), name="kurt-agent-" + agent.name, daemon=True)
        agent.thread.start()
        if resumed and agent.status == "working" and not agent.inbox:
            agent.inbox.append({"from": "system", "text": "Agent mode restarted. Continue your task: {}".format(clip(agent.task, 1500)),
                                "ts": time.time()})
            agent.wake.set()

    def _agent_turn(self, agent, reason):
        agent.busy = True
        try:
            inbox = self._drain(agent)
            if agent.lead:
                agent.heartbeat = LEAD_HEARTBEAT
                self._set_status(agent, "orchestrating")
                prompt = self._lead_prompt(reason, inbox)
                max_rounds = LEAD_TOOL_ROUNDS
            else:
                prompt = "NEW MESSAGES FOR YOU:\n{}\n\nCurrent task: {}\nContinue working with action blocks.".format(
                    self._format_inbox(inbox) or "(none)", clip(agent.task, 1500))
                max_rounds = MAX_TOOL_ROUNDS
                self._set_status(agent, "working")
            for round_index in range(max_rounds):
                if self.stop_event.is_set() or agent.retired:
                    raise StopRequested()
                agent.history.append({"role": "user", "content": prompt})
                self._trim(agent)
                messages = self._system_for(agent) + [dict(item) for item in agent.history]
                reasoning, answer = self._generate(agent, messages)
                agent.turns += 1
                agent.history.append({"role": "assistant", "content": answer or reasoning})
                self._trim(agent)
                prose, actions = parse_actions(answer)
                results, finished, said_to_user = self._execute(agent, actions)
                if agent.lead:
                    pending_user = [item for item in inbox if item.get("from") == "user" and not item.get("quick_reply")]
                    if pending_user and not said_to_user and prose and not results:
                        self.post("lead", "user", prose)
                        said_to_user = True
                    if prose:
                        agent.summary = clip(prose, 600)
                    used_tools = any(verb in TOOL_VERBS for verb, _a, _b in actions)
                    failed = any(("rejected" in item or "failed" in item) for item in results)
                    if not results or not (used_tools or failed):
                        break
                else:
                    if finished:
                        break
                    if not results:
                        summary = clip(prose, 800) or "Finished my turn without further actions."
                        agent.summary = summary
                        self.post(agent.name, "lead", "[idle] " + summary)
                        break
                late = self._drain(agent)
                prompt = "TOOL RESULTS:\n" + "\n\n".join(results)
                if late:
                    prompt += "\n\nNEW MESSAGES FOR YOU:\n" + self._format_inbox(late)
                    inbox = inbox + late
            else:
                if not agent.lead:
                    agent.summary = "Reached the per-task round limit."
                    self.post(agent.name, "lead", "[idle] Reached my round limit on the current task. Send follow-up work if more is needed.")
        finally:
            agent.busy = False
            if not self.stop_event.is_set() and not agent.retired:
                if agent.lead:
                    self._set_status(agent, "orchestrating")
                elif not agent.inbox:
                    self._set_status(agent, "idle")

    def _lead_prompt(self, reason, inbox):
        parts = ["TURN REASON: {}".format(reason)]
        users = [item for item in inbox if item.get("from") == "user"]
        others = [item for item in inbox if item.get("from") != "user"]
        if users:
            parts.append("THE HUMAN WROTE (reply with SAY user unless a quick reply already covered it, then act on any instruction):\n" + self._format_inbox(users))
        if others:
            parts.append("TEAM MESSAGES:\n" + self._format_inbox(others))
        if not inbox:
            parts.append("No new messages. Review progress. Use WAIT if the team is busy and nothing needs you; otherwise assign the next useful work.")
        parts.append("Decide your next actions now.")
        return "\n\n".join(parts)

    def _execute(self, agent, actions):
        results, finished, said_to_user = [], False, False
        for verb, args, body in actions:
            if verb in LEAD_ONLY and not agent.lead:
                results.append("[{} rejected] Only lead may use {}.".format(verb, verb))
                continue
            if verb == "DONE" and agent.lead:
                continue
            try:
                if verb == "SAY":
                    target, _sep, inline = args.partition(" ")
                    text = body.strip() or inline.strip()
                    if self.post(agent.name, target or "all", text) and target.lower() in ("user", "human") and agent.lead:
                        said_to_user = True
                elif verb == "NOTE":
                    key = slug(args or "note", "note")
                    with self.lock:
                        self.notes[key] = {"value": body.strip() or args, "by": agent.name, "ts": time.time()}
                    self._emit("note", key=key, note={"value": clip(self.notes[key]["value"], 3000), "by": agent.name, "ts": self.notes[key]["ts"]})
                    self._note_entry(agent, "action", "NOTE {}".format(key))
                elif verb == "DONE":
                    summary = clip((body.strip() or args), 1500) or "Task complete."
                    agent.summary = summary
                    self._note_entry(agent, "action", "DONE: " + clip(summary, 300))
                    self.post(agent.name, "lead", "[DONE] " + summary)
                    finished = True
                elif verb == "SPAWN":
                    results.append(self._spawn(agent, args, body))
                elif verb == "ASSIGN":
                    results.append(self._assign(agent, args, body))
                elif verb == "RETIRE":
                    results.append(self._retire(agent, args))
                elif verb == "PLAN":
                    with self.lock:
                        self.plan = clip(body.strip(), 6000)
                    self._emit("plan", plan=self.plan)
                    self._note_entry(agent, "action", "PLAN updated")
                elif verb == "WAIT":
                    try:
                        agent.heartbeat = max(5, min(900, int(float(args.split()[0]))))
                    except (ValueError, IndexError):
                        agent.heartbeat = LEAD_HEARTBEAT
                    self._note_entry(agent, "action", "WAIT {}s".format(agent.heartbeat))
                elif verb in TOOL_VERBS:
                    results.append(self._tool(agent, verb, args, body))
            except StopRequested:
                raise
            except Exception as exc:
                results.append("[{} failed] {}".format(verb, clip(exc, 800)))
        return [item for item in results if item], finished, said_to_user

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
            self.agents[name.lower()] = member
        self._emit("agent", agent=self._agent_view(member, entries=0))
        self._note_entry(agent, "action", "SPAWN {} | {}".format(name, role))
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
        self._set_status(target, "working", task=clip(text, 1500))
        self._note_entry(agent, "action", "ASSIGN {}".format(target.name))
        self.post("lead", target.name, text, kind="assignment")
        return "[ASSIGN ok] {} received the task.".format(target.name)

    def _retire(self, agent, args):
        name = args.strip().split()[0] if args.strip() else ""
        target = self._find(name)
        if target is None or target.lead:
            return "[RETIRE rejected] No active subagent named '{}'.".format(name)
        target.retired = True
        target.wake.set()
        self._set_status(target, "retired")
        self._note_entry(agent, "action", "RETIRE {}".format(target.name))
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
        self._set_status(agent, "waiting")
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
            self._set_status(agent, previous if previous != "waiting" else "working")
        return approved

    def _tool(self, agent, verb, args, body):
        host = self.host
        argument = (args or body or "").strip()
        if verb == "READ":
            self._note_entry(agent, "action", "READ " + clip(argument, 200))
            path = host.resolve_user_read_path(self.chat_id, argument)
            if path.is_dir():
                return self._list_dir(path, "READ")
            if path.stat().st_size > 2_000_000:
                return "[READ failed] {} is larger than 2 MB.".format(path)
            raw = path.read_bytes()
            if b"\x00" in raw[:4096]:
                return "[READ failed] {} looks binary.".format(path)
            return "[READ {}]\n{}".format(path, clip(raw.decode("utf-8", errors="replace"), READ_LIMIT))
        if verb == "LIST":
            self._note_entry(agent, "action", "LIST " + clip(argument or ".", 200))
            path = host.chat_work_root(self.chat_id) if not argument or argument == "." else host.resolve_user_read_path(self.chat_id, argument)
            return self._list_dir(path, "LIST")
        if verb == "SEARCH":
            self._note_entry(agent, "action", "SEARCH " + clip(argument, 200))
            result = host.search_web(argument)
            if not result.get("ok"):
                return "[SEARCH failed] {}".format(result.get("message", "unknown error"))
            lines = ["[SEARCH results for: {}]".format(argument)]
            for number, item in enumerate(result.get("results", []), 1):
                lines.append("{}. {}\nURL: {}\nSnippet: {}".format(number, item.get("title", "Untitled"), item.get("url", ""), item.get("snippet", "")))
            return clip("\n\n".join(lines), RESULT_LIMIT)
        if verb == "WRITE":
            target = args.strip().strip("\"'")
            if not target:
                return "[WRITE rejected] No path was given."
            content = strip_fence(body)
            if not content.endswith("\n"):
                content += "\n"
            resolved = host.resolve_working_path(self.chat_id, target, allow_missing=True)
            self._note_entry(agent, "action", "WRITE {} ({} bytes)".format(target, len(content.encode("utf-8"))))
            if not self._approval(agent, "write_file", "{} wants to write a file".format(agent.name),
                                  detail="{}\n\n{}".format(resolved, clip(content, 4000)), path=str(resolved)):
                return "[WRITE declined] The user declined this change. Nothing was written: {}".format(target)
            chat = host.get_chat(self.chat_id) or {}
            with self.file_lock:
                resolved = host.resolve_working_path(self.chat_id, target, allow_missing=True)
                current = hashlib.sha256(resolved.read_bytes()).hexdigest() if resolved.is_file() else None
                outcome = host.apply_approved_file(self.chat_id, target, content, current, str(chat.get("linked_folder") or ""))
            return "[WRITE succeeded] {}".format(outcome.get("message") or resolved)
        if verb == "RUN":
            command = (body.strip() or args.strip())
            if not command:
                return "[RUN rejected] No command was given."
            self._note_entry(agent, "action", "RUN " + clip(command, 300))
            readonly = False
            try:
                readonly = bool(host.is_readonly_command(self.chat_id, command))
            except Exception:
                readonly = False
            if not readonly and not self._approval(agent, "command", "{} wants to run a command".format(agent.name),
                                                   detail="Runs with your account's permissions in the project workspace.", command=command):
                return "[RUN declined] The user declined this command. It was not executed: {}".format(command)
            try:
                host.prewarm_chat_environment(self.chat_id, with_pip=host._command_needs_pip(command))
            except Exception:
                pass
            outcome = host.run_command(self.chat_id, command)
            text = "[RUN result]\nCommand: {}\nExit code: {}\nStatus: {}\nOutput:\n{}".format(
                command, outcome.get("returncode", -1), "succeeded" if outcome.get("ok") else "failed",
                clip(outcome.get("output") or "(no output)", RESULT_LIMIT))
            self._note_entry(agent, "result", clip(text, 3000))
            return text
        return ""

    @staticmethod
    def _list_dir(path, label):
        rows = []
        for item in sorted(path.iterdir(), key=lambda entry: (not entry.is_dir(), entry.name.casefold())):
            try:
                if item.is_symlink():
                    rows.append("{} (symlink)".format(item.name))
                elif item.is_dir():
                    rows.append("{}/".format(item.name))
                else:
                    rows.append("{} ({} bytes)".format(item.name, item.stat().st_size))
            except OSError:
                continue
            if len(rows) >= 300:
                rows.append("… truncated at 300 entries")
                break
        return "[{} {}]\n{}".format(label, path, "\n".join(rows) or "(empty)")

    def _status_report(self):
        with self.lock:
            members = [item for item in self.agents.values() if not item.lead and item.status != "retired"]
            plan = self.plan.strip()
            recent = [item for item in self.channel if item.get("from") not in ("user", "system")][-3:]
        lines = []
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
                answer = self._status_report()
            if answer:
                message["quick_reply"] = answer
                self.post("lead", "user", answer, kind="quick")
        except StopRequested:
            return
        except Exception as exc:
            self._note_entry(lead, "error", "Quick reply failed: {}".format(clip(exc, 300)))


def chat_history(host, chat_id):
    chat = host.get_chat(chat_id) or {}
    return [dict(item) for item in chat.get("messages", []) if isinstance(item, dict) and item.get("agent_mode")][-40:]


class AgentManager:
    def __init__(self, host):
        self.host = host
        self.lock = threading.Lock()
        self.swarms = {}

    def running_chat(self):
        with self.lock:
            for chat_id, swarm in self.swarms.items():
                if swarm.running:
                    return chat_id
        return None

    def send(self, chat_id, text, route_info, message_id=""):
        with self.lock:
            swarm = self.swarms.get(chat_id)
            if swarm is not None and swarm.running:
                swarm.user_message(text, message_id)
                return {"ok": True, "running": True, "started": False}
            for other_id, other in self.swarms.items():
                if other_id != chat_id and other.running:
                    raise ValueError("Agent mode is already running in another chat. Stop it there before starting here.")
            chat = self.host.get_chat(chat_id)
            if chat is None:
                raise ValueError("Conversation not found.")
            swarm = AgentSwarm(self.host, chat_id, route_info, restore=chat.get("agent") if isinstance(chat.get("agent"), dict) else None)
            self.swarms[chat_id] = swarm
            swarm.start(text)
        return {"ok": True, "running": True, "started": True}

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
        agents = [{"name": "lead", "role": "Lead orchestrator", "task": "", "status": "stopped", "summary": str((saved.get("lead") or {}).get("summary") or ""),
                   "turns": 0, "retry_at": 0, "lead": True, "entries": (saved.get("lead") or {}).get("entries") or []}]
        for item in saved.get("agents") or []:
            if isinstance(item, dict) and not item.get("retired"):
                agents.append({"name": item.get("name"), "role": item.get("role"), "task": item.get("task"), "status": "stopped",
                               "summary": item.get("summary"), "turns": 0, "retry_at": 0, "lead": False, "entries": []})
        snapshot = {"running": False, "goal": saved.get("goal", ""), "plan": saved.get("plan", ""), "notes": saved.get("notes") or {},
                    "channel": (saved.get("channel") or [])[-150:], "agents": agents, "approvals": [], "cooldown_until": 0,
                    "cooldown_reason": "", "model": "", "seq": 0, "chat": chat_history(self.host, chat_id)}
        return {"seq": 0, "epoch": "", "running": False, "snapshot": snapshot, "events": []}