"""Kurt's loopback-only local web interface.

The server uses only the Python standard library and reuses Kurt's provider
registry, model configuration, and Ollama client.
"""

import base64
import hashlib
import hmac
import html
import io
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import kurt as K
from kurt_sandbox import is_code_file, validate_content, run_workspace_command

CONFIG_DIR = K.CONFIG_DIR
WEB_SETTINGS_FILE = CONFIG_DIR / "web_settings.json"
CHATS_FILE = CONFIG_DIR / "chats.json"
WEB_PORT = int(os.environ.get("KURT_WEB_PORT", "1277"))
MAX_BODY = 32 * 1024 * 1024
MAX_IMPORT_BYTES = 20 * 1024 * 1024
MAX_WORKSPACE_CONTEXT = 1_400_000
MAX_CHATS = 100
MAX_MESSAGES_PER_CHAT = 300

WEB_INSTRUCTIONS = r"""
KURT WEB WORKSPACE RULES:
- You are Kurt, the local coding assistant in this application. Speak as Kurt and do not
  confuse yourself with ChatGPT or another assistant. The user owns this local instance.
- The System prompt field in Kurt Settings is user-editable configuration. You may explain,
  quote, review, and revise the Kurt-specific prompt supplied in this conversation. Do not
  refuse a request merely because it concerns Kurt's own prompt. Never include API keys,
  credentials, or unrelated host-platform instructions.
- Before every file edit or SYSTEM_PROMPT action, write a clear "Change summary" explaining
  the exact file or prompt sections, what will change, and why. Put it before the action.
- Each conversation has an isolated temporary workspace. Treat it as the only writable
  workspace and use the attached file contents and directory listing supplied in context.
- The original project directory is read-only unless the user explicitly enables project
  writes in Settings and then confirms an individual Apply to project action.
- When creating or modifying files, emit complete files with this exact action format:
  @@@ WRITE relative/path.ext
  complete file contents
  @@@ END
- For an existing file in the temporary workspace, use the supplied contents and emit a
  complete replacement with WRITE. Never invent unseen file contents.
- You may request a web search when current or external information is needed:
  @@@ SEARCH your focused search query
  @@@ END
- You may propose an update to Kurt's editable system prompt using:
  @@@ SYSTEM_PROMPT
  complete replacement Kurt system prompt
  @@@ END
  This creates a review card. It is not saved until the user explicitly applies it.
- Do not emit provider tool-call markers or invent tool results. Do not claim a command or
  test ran unless the interface supplied its actual result.
- Use the workspace tree and readable file contents already supplied in the system
  messages. They are the source of truth for what is available. If the workspace is empty
  or lacks the files needed, ask the user to add the project through Workspace files.
- Do not claim to have inspected files that were not attached or whose contents were not
  supplied. Keep paths relative to this conversation's temporary workspace. Never target
  paths outside it or claim that the original project was changed.
"""

KURT_IDENTITY_INSTRUCTIONS = r"""
KURT IDENTITY AND SELF-CONFIGURATION:
You are Kurt, the assistant configured by this local application. The user owns this local
instance and may inspect, quote, review, and revise the Kurt-controlled prompt. If asked for
your system prompt, provide the exact Kurt-controlled prompt supplied by the application;
the /system-prompt command returns the complete active prompt directly. Do not claim that
Kurt's own prompt is confidential. Clearly distinguish it from provider-owned model or
platform instructions, which are not available to this application. Never disclose API keys,
credentials, or other secrets. Summarize decisions, assumptions, alternatives, and verification
in user-facing rationale; do not reveal private chain-of-thought or hidden scratchpad tokens.
"""

EFFORT_INSTRUCTIONS = {
    "flash": "FLASH EFFORT: prioritize speed and concise answers; request no reasoning where the provider supports disabling it. Some reasoning models enforce internal reasoning regardless of client settings.",
    "thinking": "THINKING EFFORT: request medium reasoning where the provider supports effort controls. Include a useful user-facing rationale summary covering key observations, decisions, assumptions, and checks. Never expose private chain-of-thought or hidden scratchpad tokens.",
    "expert": "EXPERT EFFORT: request high/deep reasoning where the provider supports effort controls and perform careful verification. Include a detailed user-facing rationale summary covering key observations, decisions, assumptions, alternatives, verification evidence, and remaining uncertainty. Never expose private chain-of-thought or hidden scratchpad tokens.",
}

NORMAL_MODE_INSTRUCTIONS = r"""
NORMAL MODE: Do not execute terminal commands. You may still request web research with SEARCH.
Return file changes using WRITE or EDIT actions; the application runs syntax validation.
"""

CODE_MODE_INSTRUCTIONS = r"""
CODE MODE: You may run developer commands using action blocks:
@@@ RUN python -m pytest -q
@@@ END
Commands run in this conversation's working directory and per-chat Python virtual environment.
Before finishing, run relevant existing tests/checks when practical; state when no test harness
exists. Syntax validation alone is not proof of end-to-end correctness. Use one command per RUN block. The runner does not invoke a shell and permits only python,
pytest, ruff, mypy, node --check, and bash -n. A virtual environment is package isolation,
not a full operating-system sandbox; never attempt to access files outside the workspace.
Run targeted tests or checks when useful. The application also validates generated file syntax
before returning artifacts; repair failed checks before presenting final files.
"""

RUN_ACTION_RE = re.compile(r"@@@\s*RUN\s*([^\n]*)\n(.*?)\n?@@@\s*END", re.I | re.S)
SEARCH_ACTION_RE = re.compile(r"@@@\s*SEARCH\s*([^\n]*)\n?(.*?)\n?@@@\s*END", re.I | re.S)
SYSTEM_PROMPT_ACTION_RE = re.compile(r"@@@\s*SYSTEM_PROMPT\s*\n(.*?)\n?@@@\s*END", re.I | re.S)
WEB_ACTION_RE = re.compile(
    r"@@@\s*(?:RUN|SEARCH|SYSTEM_PROMPT)\b[^\n]*\n[\s\S]*?@@@\s*END", re.I
)
SYSTEM_PROMPT_REQUEST_RE = re.compile(
    r"^\s*(?:/system-prompt|(?:what\s+is|what's)\s+(?:your\s+|its\s+|kurt(?:'s)?\s+|the\s+)?system\s+prompt|(?:show|share|reveal|print|display|quote|give)\s+(?:me\s+)?(?:your\s+|its\s+|kurt(?:'s)?\s+|the\s+)?system\s+prompt)\s*[?!.\s]*$",
    re.I,
)


HTML_PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kurt</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='-5 -6 30 28'%3E%3Crect x='-5' y='-6' width='30' height='28' rx='6' fill='%230b120e'/%3E%3Cpath fill='%233dd66e' d='M0 0h6v4h-6zM14 0h6v2h-6zM11 2h6v2h-6zM0 4h1v12h-1zM2 4h2v12h-2zM5 4h1v12h-1zM8 4h6v2h-6zM6 6h5v4h-5zM1 8h1v8h-1zM4 8h1v8h-1zM8 10h6v2h-6zM11 12h6v2h-6zM14 14h6v2h-6z'/%3E%3C/svg%3E">
<script>try{document.documentElement.dataset.theme=localStorage.getItem('kurt-theme')||(matchMedia('(prefers-color-scheme: light)').matches?'light':'dark')}catch(e){document.documentElement.dataset.theme='dark'}</script>
<style>
:root{color-scheme:dark;--bg:#0e1511;--side:#0a100d;--panel:#141e19;--raise:#1c2922;--line:#22302a;--line2:#33463c;--text:#e6efe9;--muted:#8d9e94;--green:#3dd66e;--green-ink:#05200e;--green-soft:rgba(61,214,110,.12);--danger:#ff8d8d;--code:#0a110d;--shadow:0 18px 50px rgba(0,0,0,.4);--sans:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;--serif:"Iowan Old Style","Palatino Linotype",Palatino,Charter,Georgia,"Times New Roman",serif;--mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace}
:root[data-theme=light]{color-scheme:light;--bg:#f6f9f6;--side:#ebf1ec;--panel:#fff;--raise:#e4ece5;--line:#d8e2da;--line2:#bfcec3;--text:#16231b;--muted:#5b6c61;--green:#157f3c;--green-ink:#fff;--green-soft:rgba(21,127,60,.1);--danger:#b3261e;--code:#eff4ef;--shadow:0 16px 40px rgba(22,35,27,.14)}
*{box-sizing:border-box;scrollbar-color:var(--line2) transparent}
html{min-height:100%}
body{margin:0;background:var(--bg);color:var(--text);font:14.5px/1.55 var(--sans);overflow-x:hidden;overflow-y:auto;-webkit-font-smoothing:antialiased}
button,input,textarea,select{font:inherit;color:inherit}
button{cursor:pointer;border:1px solid var(--line2);background:var(--raise);border-radius:8px;padding:7px 12px;transition:background .15s,border-color .15s,color .15s,filter .15s}
button:hover{border-color:var(--green)}
button.primary{background:var(--green);color:var(--green-ink);border-color:var(--green);font-weight:650}
button.primary:hover{filter:brightness(1.08)}
button.ghost{background:transparent;border-color:transparent;color:var(--muted)}
button.ghost:hover{color:var(--text);background:var(--raise)}
button.danger{color:var(--danger)}
button:disabled{opacity:.45;cursor:not-allowed}
:focus-visible{outline:2px solid var(--green);outline-offset:2px}
.ic{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;flex:none;display:block}
.icon-btn{display:inline-grid;place-items:center;width:34px;height:34px;padding:0;border-radius:9px;background:transparent;border-color:transparent;color:var(--muted)}
.icon-btn:hover{color:var(--text);background:var(--raise);border-color:transparent}
.pill{background:transparent;border:1px solid var(--line2);color:var(--muted);border-radius:999px;padding:5px 12px;font-size:12.5px;white-space:nowrap}
.pill:hover{color:var(--text);border-color:var(--green)}
.logo{display:inline-grid;place-items:center;width:30px;height:30px;border-radius:8px;background:#0b120e;border:1px solid var(--line2);color:#3dd66e;flex:none}
.logo svg{width:18px;height:14.4px;display:block}
.mark{width:18px;height:14.4px;color:var(--green);display:block;flex:none}
.mark.xl{width:48px;height:38.4px;margin:0 auto 16px}
.app{min-height:100vh;min-height:100dvh;display:grid;grid-template-columns:268px minmax(0,1fr)}
.sidebar{position:sticky;top:0;height:100vh;height:100dvh;align-self:start;background:var(--side);border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0;padding:12px 10px;gap:2px}
.brand{display:flex;align-items:center;gap:10px;padding:4px 8px 14px}
.brand b{font-size:17px;letter-spacing:-.01em;font-weight:650}
.nav-btn{display:flex;align-items:center;gap:10px;width:100%;text-align:left;background:transparent;border:0;border-radius:9px;padding:8px 10px;color:var(--text)}
.nav-btn:hover{background:var(--raise)}
.nav-btn .ico{width:22px;height:22px;display:grid;place-items:center;color:var(--muted);flex:none}
.nav-btn.new .ico{background:var(--green);color:var(--green-ink);border-radius:50%}
.nav-btn.new .ic{width:15px;height:15px;stroke-width:2.4}
.side-title{color:var(--muted);font-weight:600;font-size:12px;padding:16px 10px 6px}
.side-heading{display:flex;align-items:center;justify-content:space-between;gap:6px;padding:14px 8px 5px}
.side-heading .side-title{padding:0}
.chat-sort{max-width:112px;background:transparent;border:1px solid transparent;border-radius:7px;color:var(--muted);padding:3px 5px;font-size:11px}
.chat-sort:hover{border-color:var(--line2);color:var(--text)}
.chat-list{overflow:auto;min-height:80px;flex:1}
.chat-row{display:flex;align-items:center;gap:3px;margin:2px 0;padding:2px;border-radius:9px}
.chat-row:hover,.chat-row.active{background:var(--raise)}
.chat-item{flex:1;min-width:0;padding:6px 7px;border:0;background:transparent;text-align:left;cursor:pointer;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:13px}
.chat-item:hover,.chat-row.active .chat-item{color:var(--text);background:transparent}
.chat-item .pin-indicator{color:var(--green);margin-right:5px}
.chat-actions{display:flex;gap:1px;flex:none;opacity:.55}
.chat-row:hover .chat-actions,.chat-row:focus-within .chat-actions{opacity:1}
.chat-action{width:25px;height:25px;padding:0;border:0;background:transparent;color:var(--muted);font-size:13px;border-radius:6px}
.chat-action:hover{color:var(--text);background:var(--panel)}
.chat-action.danger:hover{color:var(--danger)}
.side-bottom{border-top:1px solid var(--line);padding-top:8px;display:grid;gap:2px}
.local-note{font-size:12px;color:var(--muted);padding:6px 10px 0}
.status-dot{width:7px;height:7px;border-radius:50%;display:inline-block;background:var(--green);margin-right:7px}
.stage{display:grid;grid-template-rows:auto 2px auto;align-content:start;min-width:0;min-height:100vh;min-height:100dvh}
.topbar{display:flex;align-items:center;gap:6px;padding:8px 16px;min-height:52px;background:var(--bg)}
.titles{flex:1;min-width:0;padding-left:4px}
.chat-title{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chat-meta{color:var(--muted);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.3}
.chat-meta:empty{display:none}
.workspace-label{font:12px var(--mono);color:var(--muted);max-width:220px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;direction:rtl;margin-right:6px}
.menu-btn{display:none}
.download-strip{position:relative;height:2px;background:var(--line)}
.download-fill{height:100%;width:0;background:var(--green);transition:width .25s}
.download-caption{position:absolute;right:20px;top:9px;z-index:6;font-size:12px;background:var(--panel);border:1px solid var(--line2);border-radius:999px;padding:3px 11px;box-shadow:var(--shadow)}
.download-caption:empty{display:none}
.main{min-width:0;min-height:calc(100vh - 54px);min-height:calc(100dvh - 54px);display:flex;flex-direction:column}
.messages{flex:0 0 auto;overflow:visible;overscroll-behavior:contain;padding:24px max(20px,calc((100% - 760px)/2)) 12px}
.main.empty{justify-content:center;padding-bottom:7vh}
.main.empty .messages{flex:0 0 auto;overflow:visible;padding:0 20px}
.welcome{text-align:center;padding-bottom:22px}
.welcome h1{font:400 34px/1.2 var(--serif);letter-spacing:-.01em;margin:0}
.message{display:flex;width:100%;max-width:760px;margin:0 auto 26px}
.message.user{justify-content:flex-end}
.message.assistant{justify-content:flex-start}
.message.user .bubble{display:inline-block;max-width:min(78%,680px);margin-left:auto;background:var(--raise);border-radius:16px;padding:9px 15px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:15px;line-height:1.6}
.message.assistant .bubble{width:100%;min-width:0}
.bubble{min-width:0}
.content{overflow-wrap:anywhere;font-family:var(--serif);font-size:16.5px;line-height:1.7}
.content:empty{display:none}
.content.err{font-family:var(--sans);font-size:14.5px;color:var(--danger)}
.content p{margin:0 0 12px}
.content h1,.content h2,.content h3{line-height:1.3;margin:22px 0 10px}
.content h1{font-size:24px}.content h2{font-size:20px}.content h3{font-size:17.5px}
.content ul,.content ol{padding-left:24px;margin:0 0 12px}.content li{margin:4px 0}
.content hr{border:0;border-top:1px solid var(--line2);margin:20px 0}
.content blockquote{border-left:3px solid var(--green);padding:2px 14px;margin:12px 0;color:var(--muted)}
.content a{color:var(--green)}
.content code:not(pre code){font:.86em var(--mono);background:var(--raise);border:1px solid var(--line);border-radius:5px;padding:1px 5px}
.assistant-heading{display:flex;align-items:center;gap:8px;margin:0 0 10px;color:var(--muted);font:600 13px var(--sans)}
.assistant-heading .mark{width:24px;height:19.2px;padding:2px;border-radius:6px;background:#0b120e;color:#3dd66e;box-sizing:content-box}
.assistant-status{font-weight:400;color:var(--muted);font-size:12px;margin-left:2px}
.message.live .assistant-heading .mark{animation:blink 1.1s steps(2,start) infinite}
.msg-foot{display:flex;align-items:center;gap:4px;margin-top:6px;min-height:30px}
.copy-msg{width:30px;height:30px}
.copy-msg .ic{width:16px;height:16px}
.message.live .mark{animation:blink 1.1s steps(2,start) infinite}
.message.live .copy-msg{visibility:hidden}
@keyframes blink{50%{opacity:.35}}
.think{margin:0 0 8px}
.think[hidden]{display:none}
.think-head{display:inline-flex;align-items:center;gap:6px;background:transparent;border:0;padding:3px 0;color:var(--muted);font-size:13.5px}
.think-head:hover{color:var(--text)}
.think-head .chev{width:15px;height:15px;transition:transform .15s;transform:rotate(-90deg)}
.think.open .chev{transform:rotate(0deg)}
.think-body{display:none;border-left:2px solid var(--line2);margin:6px 0 4px 5px;padding:2px 0 2px 14px;color:var(--muted);font-size:14px;line-height:1.65;white-space:pre-wrap;overflow-wrap:anywhere;max-height:320px;overflow:auto}
.think.open .think-body{display:block}
.think.live .think-label{background:linear-gradient(90deg,var(--muted) 30%,var(--text) 50%,var(--muted) 70%);background-size:200% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:shimmer 1.8s linear infinite}
@keyframes shimmer{from{background-position:100% 0}to{background-position:-100% 0}}
.codebox{background:var(--code);border:1px solid var(--line);border-radius:10px;overflow:hidden;margin:12px 0}
.codehead{display:flex;justify-content:space-between;align-items:center;padding:5px 6px 5px 12px;border-bottom:1px solid var(--line);color:var(--muted);font:12px var(--mono)}
.codebox pre{margin:0;padding:14px;overflow-x:auto;overflow-y:visible;max-height:none;white-space:pre;font:13px/1.6 var(--mono)}
.copy-mini{font-size:12px;padding:3px 8px;background:transparent;border-color:transparent;color:var(--muted)}
.copy-mini:hover{color:var(--text)}
.artifact{margin:14px 0;border:1px solid var(--line2);background:var(--panel);border-radius:12px;overflow:hidden;font-family:var(--sans)}
.artifact.bad{border-color:var(--danger)}
.artifact-top{display:flex;align-items:center;gap:12px;padding:12px 14px;cursor:pointer}
.file-icon{width:32px;height:32px;border-radius:8px;background:var(--raise);display:grid;place-items:center;color:var(--green);flex:none}
.file-icon .ic{width:17px;height:17px}
.file-name{font:600 13px var(--mono);flex:1;min-width:0;overflow-wrap:anywhere}
.file-check{font-size:12px;color:var(--green)}
.artifact.bad .file-check{color:var(--danger)}
.artifact-chev{color:var(--muted);transition:transform .15s;display:grid}
.artifact.open .artifact-chev{transform:rotate(180deg)}
.artifact-body{display:none;border-top:1px solid var(--line)}
.artifact.open .artifact-body{display:block}
.artifact-body .codebox{margin:0;border:0;border-radius:0}
.artifact-actions{display:flex;gap:7px;padding:10px 12px;border-top:1px solid var(--line);flex-wrap:wrap}
.artifact-actions button{font-size:13px}
.composer-wrap{position:relative;padding:6px 20px 14px;flex-shrink:0}
.composer{max-width:760px;margin:0 auto;border:1px solid var(--line2);border-radius:22px;background:var(--panel);padding:12px 10px 8px 18px;display:flex;flex-direction:column;gap:4px;box-shadow:0 2px 14px rgba(0,0,0,.16);transition:border-color .15s,box-shadow .15s}
.composer:focus-within{border-color:var(--green);box-shadow:0 0 0 3px var(--green-soft)}
.composer textarea{display:block;width:100%;resize:none;min-height:30px;max-height:220px;background:transparent;border:0;outline:none;padding:3px 0;font:15px/1.55 var(--sans)}
.composer textarea::placeholder{color:var(--muted)}
.compose-bottom{display:flex;align-items:center;gap:4px;margin-left:-8px}
.spacer{flex:1}
.model-pill{max-width:230px;min-width:0;background:transparent;border:0;color:var(--muted);border-radius:9px;padding:6px 8px;font-size:13px;text-overflow:ellipsis;cursor:pointer;outline-offset:0}
.model-pill:hover{color:var(--text);background:var(--raise)}
.model-pill option{background:var(--panel);color:var(--text)}
.mode-pill{max-width:112px;min-width:84px;background:transparent;border:1px solid var(--line);color:var(--muted);border-radius:9px;padding:5px 7px;font-size:12px}
.mode-pill:hover{color:var(--text);border-color:var(--green)}
.mode-pill option{background:var(--panel);color:var(--text)}
.work-log{margin:0 0 12px;border:1px solid var(--line);border-radius:9px;background:var(--bg);overflow:visible}
.work-log[hidden]{display:none}
.work-log-head{display:flex;align-items:center;gap:7px;width:100%;padding:7px 10px;border:0;border-radius:0;background:transparent;color:var(--muted);text-align:left;font-size:12.5px}
.work-log-head:hover{color:var(--text);background:var(--raise)}
.work-log-head .chev{width:14px;height:14px;transition:transform .15s}
.work-log:not(.open) .work-log-head .chev{transform:rotate(-90deg)}
.work-log-body{display:block;padding:3px 12px 9px 31px;color:var(--muted);font:12.5px/1.65 var(--sans);white-space:pre-wrap;overflow-wrap:anywhere}
.work-log:not(.open) .work-log-body{display:none}
.work-step{padding:6px 0;border-top:1px solid var(--line);color:var(--muted)}
.work-step:first-child{border-top:0}
.work-step summary{cursor:pointer;color:var(--text);font-weight:550}
.work-step summary::marker{color:var(--muted)}
.work-step.ok summary{color:var(--green)}
.work-step.fail summary{color:var(--danger)}
.work-detail{padding:5px 0 2px 17px;color:var(--muted);white-space:pre-wrap;overflow-wrap:anywhere;font-weight:400}
.send{width:34px;height:34px;padding:0;border-radius:50%;display:grid;place-items:center;background:var(--green);color:var(--green-ink);border-color:var(--green);margin-left:2px}
.send:hover{filter:brightness(1.08);border-color:var(--green)}
.send:disabled{background:var(--raise);color:var(--muted);border-color:var(--line2);opacity:.7;filter:none}
.compose-hint{max-width:760px;margin:8px auto 0;text-align:center;color:var(--muted);font-size:12px}
.to-bottom{position:absolute;left:50%;top:-40px;transform:translateX(-50%);width:34px;height:34px;padding:0;border-radius:50%;display:none;place-items:center;background:var(--panel);border:1px solid var(--line2);box-shadow:var(--shadow);color:var(--muted)}
.to-bottom.show{display:grid}
.main.empty .to-bottom{display:none}
.chips{display:none;max-width:760px;margin:6px auto 0;padding:0 20px;flex-wrap:wrap;gap:8px;justify-content:center}
.main.empty .chips{display:flex}
.chip{border-radius:999px;background:transparent;border:1px solid var(--line2);color:var(--muted);padding:7px 15px;font-size:13.5px}
.chip:hover{color:var(--text);border-color:var(--green);background:var(--raise)}
.modal-backdrop{position:fixed;inset:0;background:rgba(4,10,7,.6);z-index:30;display:none;align-items:center;justify-content:center;padding:20px}
.modal-backdrop.show{display:flex}
.modal{width:min(850px,100%);max-height:88vh;overflow:auto;background:var(--panel);border:1px solid var(--line2);border-radius:14px;box-shadow:var(--shadow)}
.modal-head{position:sticky;top:0;z-index:2;background:var(--panel);padding:14px 20px;display:flex;align-items:center;border-bottom:1px solid var(--line)}
.modal-head h2{font-size:17px;margin:0;flex:1}
.modal-body{padding:18px 20px 24px}
.modal.settings-modal{width:min(1020px,100%);height:min(760px,88vh);max-height:88vh;display:flex;flex-direction:column;overflow:hidden}
.settings-modal .modal-head{flex:none}
.settings-body{display:grid;grid-template-columns:205px minmax(0,1fr);flex:1;min-height:0;overflow:hidden;padding:0}
.settings-nav{display:flex;flex-direction:column;gap:5px;padding:16px 12px;background:var(--side);border-right:1px solid var(--line);min-height:0}
.settings-nav-caption{padding:2px 9px 8px;color:var(--muted);font-size:11px;font-weight:700;letter-spacing:.09em;text-transform:uppercase}
.settings-nav-btn{width:100%;display:flex;align-items:center;text-align:left;background:transparent;border:1px solid transparent;border-radius:9px;padding:9px 10px;color:var(--muted);font-size:13.5px}
.settings-nav-btn:hover{background:var(--raise);color:var(--text)}
.settings-nav-btn.active{background:var(--green-soft);border-color:var(--line2);color:var(--text);font-weight:650}
.settings-panel-area{min-width:0;min-height:0;overflow-y:auto;padding:22px 25px 28px}
.settings-panel{display:none;max-width:760px}
.settings-panel.active{display:block}
.settings-panel>h3{font-size:18px;letter-spacing:-.01em;margin:0 0 5px}
.settings-panel>.inline-note{margin:0 0 18px;line-height:1.55}
.model-download-card{padding:14px;border:1px solid var(--line2);border-radius:11px;background:var(--bg);margin-top:16px}
.model-download-card h4{font-size:13px;margin:0 0 5px}
.model-download-card p{color:var(--muted);font-size:12px;margin:0 0 12px}
.model-download-card .row{align-items:stretch}
.model-download-card .row input{flex:1 1 240px;min-width:0;background:var(--panel);border:1px solid var(--line2);border-radius:8px;padding:8px 10px;outline:none}
.model-download-card .row input:focus{border-color:var(--green)}
.model-download-card .row button{flex:0 0 auto}
.settings-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.field{display:grid;gap:5px;margin-bottom:13px}
.field label{font-size:13px;color:var(--muted)}
.field input,.field select,.field textarea{background:var(--bg);border:1px solid var(--line2);border-radius:8px;padding:8px 10px;outline:none;width:100%}
.field textarea{min-height:145px;resize:vertical;font:12.5px/1.55 var(--mono)}
.field input:focus,.field select:focus,.field textarea:focus{border-color:var(--green)}
.section{border-top:1px solid var(--line);margin-top:18px;padding-top:16px}
.section h3{font-size:14px;margin:0 0 12px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.row>*{flex:1;min-width:100px}
.key-row,.model-row{display:flex;gap:10px;align-items:center;padding:9px 10px;border:1px solid var(--line);border-radius:10px;margin:6px 0}
.key-row .grow,.model-row .grow{flex:1;min-width:0}
.key-row small,.model-row small{display:block;color:var(--muted)}
.pool-card{border:1px solid var(--line2);border-radius:11px;padding:10px 12px;margin:9px 0;background:var(--bg)}
.pool-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.pool-head .grow{flex:1;min-width:150px}
.pool-head small{display:block;color:var(--muted);font-size:12px}
.pool-actions{display:flex;gap:6px;align-items:center}
.local-model-row{display:flex;align-items:center;gap:9px;padding:9px 10px;border:1px solid var(--line);border-radius:9px;margin:6px 0}
.local-model-row .grow{flex:1;min-width:0}
.local-model-row small{display:block;color:var(--muted)}
.file-actions{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}
.file-source{padding:12px;border:1px solid var(--line);border-radius:10px;margin:12px 0}
.file-source h3{font-size:13px;margin:0 0 8px}
.file-source p{font-size:12px;color:var(--muted);margin:0 0 10px}
.file-row .file-open{flex:none}
.file-row .file-add{flex:none}
.upload-list{font-size:12px;color:var(--muted);padding:6px 0;overflow-wrap:anywhere}
.inline-note{color:var(--muted);font-size:12px;margin:5px 0 10px}
.check-row{display:flex;align-items:center;gap:9px;color:var(--muted);font-size:13px;margin:8px 0 13px}
.check-row input{width:auto}
.file-picker{border:1px solid var(--line);border-radius:10px;max-height:260px;overflow-y:auto}
.file-row{padding:7px 10px;border-bottom:1px solid var(--line);display:flex;gap:9px;align-items:center}
.file-row.folder{cursor:pointer}
.file-row.folder:hover{background:var(--raise)}
.file-row:last-child{border:0}
.file-row label{flex:1;overflow-wrap:anywhere}
.toast{position:fixed;bottom:24px;left:50%;transform:translate(-50%,16px);background:var(--panel);border:1px solid var(--line2);padding:9px 15px;border-radius:10px;box-shadow:var(--shadow);opacity:0;transition:.2s;pointer-events:none;z-index:40}
.toast.show{opacity:1;transform:translate(-50%,0)}
.modal-body .empty{color:var(--muted);font-size:13px;padding:12px}
.scrim{display:none}
@media(max-width:900px){.workspace-label{display:none}}
@media(max-width:700px){
.app{grid-template-columns:1fr}
.sidebar{position:fixed;inset:0 auto 0 0;width:min(300px,86vw);z-index:20;transform:translateX(-102%);transition:transform .2s;box-shadow:var(--shadow)}
body.nav-open .sidebar{transform:none}
body.nav-open .scrim{display:block;position:fixed;inset:0;background:rgba(4,10,7,.55);z-index:15}
.menu-btn{display:inline-grid}
.topbar{padding:8px 10px}
.model-pill{max-width:42vw}
.mode-pill{max-width:19vw;min-width:0;padding:5px 3px;font-size:10.5px}
.model-pill{max-width:28vw}
.messages{padding:18px 14px 8px}
.composer-wrap{padding:6px 10px 10px}
.welcome h1{font-size:28px}
.settings-grid{grid-template-columns:1fr}
.modal.settings-modal{width:100%;height:92vh;max-height:92vh}
.settings-body{grid-template-columns:1fr;grid-template-rows:auto minmax(0,1fr)}
.settings-nav{flex-direction:row;overflow-x:auto;overflow-y:hidden;gap:5px;padding:8px 10px;border-right:0;border-bottom:1px solid var(--line)}
.settings-nav-caption{display:none}
.settings-nav-btn{width:auto;flex:0 0 auto;white-space:nowrap;padding:8px 11px}
.settings-panel-area{padding:17px 15px 22px}
}
@media(prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important;scroll-behavior:auto!important}}

/* Chat + artifact preview layout */
.main{display:flex;flex:1 0 auto;flex-direction:row;align-items:stretch;justify-content:flex-start;width:100%;min-width:0;min-height:calc(100vh - 54px);min-height:calc(100dvh - 54px);padding-bottom:0}
.chat-area{display:flex;flex:1 1 0%;flex-direction:column;width:100%;max-width:100%;min-width:0;min-height:calc(100vh - 54px);min-height:calc(100dvh - 54px)}
.main.has-artifacts .chat-area{flex:0 0 50%;width:50%;max-width:50%}
.artifact-pane{display:none;flex:0 0 50%;width:50%;min-width:0;max-width:50%;min-height:calc(100vh - 54px);min-height:calc(100dvh - 54px);overflow-y:auto;overscroll-behavior:contain;padding:14px 18px 24px;background:var(--side);border-left:1px solid var(--line)}
.main.has-artifacts .artifact-pane{display:flex;flex-direction:column}
.artifact-pane-head{position:sticky;top:-14px;z-index:3;display:flex;align-items:center;justify-content:space-between;gap:10px;margin:-14px -18px 14px;padding:15px 18px 12px;background:var(--side);border-bottom:1px solid var(--line)}
.artifact-pane-head>div{display:flex;flex:1;flex-direction:column;gap:2px;min-width:0}
.artifact-pane-head strong{overflow:hidden;font-size:13px;font-weight:650;text-overflow:ellipsis;white-space:nowrap}
.artifact-pane-count{color:var(--muted);font-size:11.5px}
.artifact-pane-close{flex:none}
.artifact-pane-content{display:grid;align-content:start;gap:13px;min-width:0}
.artifact-pane .artifact{margin:0;min-width:0}
.artifact-pane .artifact-top{width:100%;text-align:left;color:var(--text);background:transparent;border:0;border-radius:0}
.artifact-pane .artifact-body .codebox pre{max-height:48vh;overflow:auto}
.main.has-artifacts .messages{padding:24px 20px 12px}
.main.has-artifacts .message{max-width:100%}
.main.has-artifacts .composer-wrap{padding-left:18px;padding-right:18px}
.main.has-artifacts .composer,.main.has-artifacts .compose-hint,.main.has-artifacts .chips{max-width:100%}
.message-artifacts{display:grid;gap:8px;margin:13px 0 4px}
.message-artifacts:empty{display:none}
.artifact-trigger{display:flex;align-items:center;gap:11px;width:min(100%,440px);max-width:100%;padding:10px 12px;text-align:left;background:var(--panel);border:1px solid var(--line2);border-radius:11px;color:var(--text);transition:background .15s,border-color .15s,transform .15s}
.artifact-trigger:hover,.artifact-trigger.selected{background:var(--raise);border-color:var(--green)}
.artifact-trigger:active{transform:translateY(1px)}
.artifact-trigger-icon{display:grid;place-items:center;flex:none;width:34px;height:34px;border-radius:8px;background:var(--raise);color:var(--green)}
.artifact-trigger-icon .ic{width:17px;height:17px}
.artifact-trigger-text{display:block;flex:1;min-width:0}
.artifact-trigger-name{display:block;overflow-wrap:anywhere;font:600 12px/1.45 var(--mono)}
.artifact-trigger-status{display:block;margin-top:3px;color:var(--muted);font-size:11.5px;line-height:1.4}
.artifact-trigger-open{flex:none;color:var(--muted);font-size:12px}
.main.empty{justify-content:flex-start;padding-bottom:0}
.main.empty .chat-area{justify-content:flex-start}
.main.empty .messages{display:flex;flex:1 0 auto;min-height:0;align-items:center;justify-content:center;overflow:visible;padding:0 20px}
.main.empty .welcome{padding-bottom:0}
.main.empty .chips{order:2;flex:0 0 auto;margin-top:8px;margin-bottom:4px}
.main.empty .composer-wrap{order:3;flex:0 0 auto;margin-top:0}
.main.empty .to-bottom{display:none}

/* In normal mode the conversation grows with its content, while short chats keep
   the composer anchored at the bottom of the available viewport. */
.messages{flex:1 0 auto;min-width:0;overflow:visible;overscroll-behavior:contain}

/* Compact mode keeps the whole application inside the viewport and scrolls
   the message and preview columns independently. */
body.compact-scroll{height:100vh;height:100dvh;overflow:hidden}
body.compact-scroll .app{height:100vh;height:100dvh;min-height:0}
body.compact-scroll .sidebar{height:100vh;height:100dvh}
body.compact-scroll .stage{height:100vh;height:100dvh;min-height:0;flex:1 1 auto}
body.compact-scroll .main{min-height:0;flex:1 1 auto;overflow:hidden}
body.compact-scroll .chat-area{height:100%;min-height:0;overflow:hidden}
body.compact-scroll .messages{min-height:0;flex:1 1 auto;overflow-y:auto;overscroll-behavior:contain}
body.compact-scroll .main.empty{min-height:0}
body.compact-scroll .main.empty .messages{flex:1 1 auto;overflow-y:auto}
body.compact-scroll .composer-wrap{flex-shrink:0}
body.compact-scroll .artifact-pane{height:100%;min-height:0;overflow-y:auto}
@media(max-width:900px){.main.has-artifacts .messages{padding-left:14px;padding-right:14px}.artifact-pane{padding-left:12px;padding-right:12px}.artifact-pane-head{margin-left:-12px;margin-right:-12px;padding-left:12px;padding-right:12px}}
@media(max-width:700px){
.main.has-artifacts{flex-direction:column}
.main.has-artifacts .chat-area{flex:1 1 auto;width:100%;max-width:none}
.main.has-artifacts .artifact-pane{flex:0 0 auto;width:100%;max-width:none;min-height:45vh;max-height:70vh;border-left:0;border-top:1px solid var(--line)}
.artifact-pane .artifact-body .codebox pre{max-height:42vh}
body.compact-scroll .main.has-artifacts .chat-area{flex:1 1 60%;height:auto;min-height:0}
body.compact-scroll .main.has-artifacts .artifact-pane{flex:0 0 40%;height:auto;min-height:0;max-height:none}
}
</style></head>
<body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
<symbol id="kurt-k" viewBox="0 0 20 16"><path fill="currentColor" shape-rendering="crispEdges" d="M0 0h6v4h-6zM14 0h6v2h-6zM11 2h6v2h-6zM0 4h1v12h-1zM2 4h2v12h-2zM5 4h1v12h-1zM8 4h6v2h-6zM6 6h5v4h-5zM1 8h1v8h-1zM4 8h1v8h-1zM8 10h6v2h-6zM11 12h6v2h-6zM14 14h6v2h-6z"/></symbol>
<symbol id="i-plus" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></symbol>
<symbol id="i-up" viewBox="0 0 24 24"><path d="M12 19V5M5 12l7-7 7 7"/></symbol>
<symbol id="i-down" viewBox="0 0 24 24"><path d="M12 5v14M5 12l7 7 7-7"/></symbol>
<symbol id="i-stop" viewBox="0 0 24 24"><rect x="6.5" y="6.5" width="11" height="11" rx="2.2" fill="currentColor" stroke="none"/></symbol>
<symbol id="i-chev" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></symbol>
<symbol id="i-copy" viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/></symbol>
<symbol id="i-menu" viewBox="0 0 24 24"><path d="M4 7h16M4 12h16M4 17h16"/></symbol>
<symbol id="i-file" viewBox="0 0 24 24"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></symbol>
<symbol id="i-sliders" viewBox="0 0 24 24"><path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/></symbol>
<symbol id="i-download" viewBox="0 0 24 24"><path d="M12 4v11M7 11l5 5 5-5M5 20h14"/></symbol>
<symbol id="i-trash" viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></symbol>
<symbol id="i-theme" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M12 4a8 8 0 0 1 0 16z" fill="currentColor"/></symbol>
<symbol id="i-scroll" viewBox="0 0 24 24"><path d="M8 4v16M5 7l3-3 3 3M5 17l3 3 3-3M16 6h4M16 12h4M16 18h4"/></symbol>
</svg>
<div class="scrim" id="scrim"></div>
<div class="app">
<aside class="sidebar">
<div class="brand"><span class="logo"><svg viewBox="0 0 20 16" aria-hidden="true"><use href="#kurt-k"/></svg></span><b>Kurt</b></div>
<button class="nav-btn new" id="newChat"><span class="ico"><svg class="ic"><use href="#i-plus"/></svg></span>New chat</button>
<button class="nav-btn" id="openFiles"><span class="ico"><svg class="ic"><use href="#i-file"/></svg></span>Workspace files</button>
<div class="side-heading"><div class="side-title">Chats</div><select id="chatSort" class="chat-sort" aria-label="Sort chats"><option value="newest">Newest</option><option value="oldest">Oldest</option></select></div>
<div id="chatList" class="chat-list"></div>
<div class="side-bottom">
<button class="nav-btn" id="sidebarSettings"><span class="ico"><svg class="ic"><use href="#i-sliders"/></svg></span>Settings</button>
<div class="local-note"><span class="status-dot"></span>Running on localhost only</div>
</div>
</aside>
<div class="stage">
<header class="topbar">
<button class="icon-btn menu-btn" id="menuBtn" aria-label="Open menu"><svg class="ic"><use href="#i-menu"/></svg></button>
<div class="titles"><div id="chatTitle" class="chat-title">New chat</div><div id="chatMeta" class="chat-meta"></div></div>
<button class="icon-btn" id="scrollModeBtn" title="Enable compact internal scrolling" aria-label="Enable compact internal scrolling" aria-pressed="false"><svg class="ic"><use href="#i-scroll"/></svg></button>
<button class="icon-btn" id="clearChat" title="Clear conversation messages" aria-label="Clear conversation messages"><svg class="ic"><use href="#i-trash"/></svg></button>
<button class="icon-btn" id="themeBtn" title="Switch colour theme" aria-label="Switch colour theme"><svg class="ic"><use href="#i-theme"/></svg></button>
</header>
<div class="download-strip" title="Background model download"><div id="downloadFill" class="download-fill"></div><div id="downloadCaption" class="download-caption"></div></div>
<main class="main empty" id="main">
<section class="chat-area" id="chatArea">
<div id="messages" class="messages"></div>
<div class="chips" id="chips"></div>
<div class="composer-wrap">
<button class="to-bottom" id="toBottom" aria-label="Scroll to latest message" title="Scroll to latest"><svg class="ic"><use href="#i-down"/></svg></button>
<div class="composer">
<textarea id="prompt" rows="1" placeholder="Message Kurt" aria-label="Message Kurt"></textarea>
<div class="compose-bottom">
<button class="icon-btn" id="contextBtn" title="Attach workspace files" aria-label="Attach workspace files"><svg class="ic"><use href="#i-plus"/></svg></button>
<span class="spacer"></span>
<select id="effortSelect" class="mode-pill" title="Response effort" aria-label="Response effort"><option value="flash">Flash</option><option value="thinking" selected>Thinking</option><option value="expert">Expert</option></select>
<select id="operationMode" class="mode-pill" title="Operating mode" aria-label="Operating mode"><option value="normal" selected>Normal</option><option value="code">Code</option></select>
<select id="topModel" class="model-pill" title="Active model" aria-label="Active model"></select>
<button class="send" id="sendBtn" title="Send message" aria-label="Send message" disabled><svg class="ic"><use href="#i-up"/></svg></button>
</div>
</div>
<div class="compose-hint" id="composeHint">Kurt can make mistakes. Files are staged in this chat’s isolated workspace. Project files stay unchanged unless you explicitly enable and apply changes.</div>
</div>
</section>
<aside class="artifact-pane" id="artifactPane" aria-label="Selected file preview">
<div class="artifact-pane-head"><div><strong id="artifactPaneTitle">File preview</strong><span class="artifact-pane-count" id="artifactCount">Select a file card to preview</span></div><button class="icon-btn artifact-pane-close" id="closeArtifactPane" type="button" title="Close file preview" aria-label="Close file preview">×</button></div>
<div class="artifact-pane-content" id="artifactPaneContent"></div>
</aside>
</main>
</div></div>
<div class="modal-backdrop" id="settingsModal"><div class="modal settings-modal"><div class="modal-head"><h2>Kurt settings</h2><button class="ghost" data-close="settingsModal">Close</button></div><div class="modal-body settings-body">
<nav class="settings-nav" aria-label="Settings sections">
<div class="settings-nav-caption">Preferences</div>
<button class="settings-nav-btn active" type="button" data-settings-tab="general">General</button>
<button class="settings-nav-btn" type="button" data-settings-tab="local-models">Local models</button>
<button class="settings-nav-btn" type="button" data-settings-tab="api-keys">API key groups</button>
<button class="settings-nav-btn" type="button" data-settings-tab="remote-models">Remote API models</button>
</nav>
<div class="settings-panel-area">
<section class="settings-panel active" data-settings-panel="general">
<h3>General</h3><div class="inline-note">Choose the model Kurt uses by default and configure the project workspace.</div>
<div class="settings-grid"><div class="field"><label>Active model</label><select id="settingsModel"></select></div><div class="field"><label>Project directory (read-only by default)</label><input id="projectRoot" placeholder="/Users/you/project"></div></div>
<label class="check-row"><input id="allowProjectWrites" type="checkbox"> Allow applying generated files to the project (each file still requires confirmation)</label>
<div class="field"><label>System prompt</label><textarea id="systemPrompt" style="min-height:180px"></textarea><div class="inline-note">Kurt can review and propose edits to this prompt in chat. Proposed prompt changes appear as a review card and require your approval before saving. Type /system-prompt in chat to display the exact active Kurt prompt for the selected mode.</div></div>
<div class="row"><button class="primary" id="saveSettings">Save settings</button><button id="reloadSettings">Reload from disk</button></div>
</section>
<section class="settings-panel" id="localModelsSection" data-settings-panel="local-models">
<h3>Local models</h3><div class="inline-note">Manage models installed through Ollama. Choose an installed model to make it active, or download another model to this computer.</div>
<div id="localModelList"></div>
<div class="model-download-card"><h4>Download a model</h4><p>Enter an Ollama model ID, for example <code>qwen2.5-coder:7b</code>. Downloads run in the background and progress appears at the top of Kurt.</p><div class="row"><input id="downloadModel" aria-label="Ollama model name" placeholder="Model ID, e.g. qwen2.5-coder:7b"><button id="startDownload" class="primary">Download model</button></div><div class="inline-note" id="localDownloadStatus">No model download started.</div></div>
</section>
<section class="settings-panel" data-settings-panel="api-keys">
<h3>API key groups</h3><div class="inline-note">Keys in the same provider and group form a rotation pool. Kurt retries another key for quota, rate-limit, authentication, server, and connection errors before response content starts.</div>
<div id="keysList"></div>
<div class="section"><h3>Add an API key</h3><div class="settings-grid"><div class="field"><label>Provider</label><select id="keyProvider"></select></div><div class="field"><label>Key group</label><input id="keyGroup" list="keyPoolOptions" value="default" placeholder="e.g. personal"></div><div class="field"><label>Account label</label><input id="keyLabel" placeholder="Personal account"></div><div class="field"><label>API key</label><input id="keyValue" type="password" placeholder="Paste key (stored locally)"></div></div><button id="addKey" class="primary">Add API key</button><datalist id="keyPoolOptions"></datalist></div>
</section>
<section class="settings-panel" data-settings-panel="remote-models">
<h3>Remote API models</h3><div class="inline-note">Register models from Claude, OpenAI, Grok, OpenRouter, and other configured providers. Each model uses the provider and key group selected below.</div>
<div class="section" style="border-top:0;margin-top:0;padding-top:0"><div class="settings-grid"><div class="field"><label>Provider</label><select id="remoteProvider"></select></div><div class="field"><label>API model ID</label><input id="remoteModelId" placeholder="e.g. openrouter/free"></div><div class="field"><label>Display name</label><input id="remoteLabel" placeholder="Model name"></div><div class="field"><label>Key group</label><input id="remoteGroup" list="keyPoolOptions" value="default"></div><div class="field" style="grid-column:1/-1"><label>API base URL (optional for standard providers)</label><input id="remoteBase" placeholder="https://api.example.com/v1"></div></div><button id="addRemote" class="primary">Add remote model</button></div>
<div id="remoteList" style="margin-top:18px"></div>
</section>
</div></div></div>
<div class="modal-backdrop" id="filesModal"><div class="modal"><div class="modal-head"><h2>Add files</h2><button class="ghost" data-close="filesModal">Close</button></div><div class="modal-body"><p style="color:var(--muted);margin-top:0">Choose files or a folder from your computer. This opens the standard macOS Finder or Windows File Explorer picker. Images and other binary files are kept in this chat workspace; readable source files are added to Kurt’s context. Your original files are not changed.</p><div class="file-source"><h3>From this computer</h3><div class="file-actions"><button id="chooseLocalFiles" class="primary">Choose files</button><button id="chooseLocalFolder">Choose folder</button><input id="localFiles" type="file" multiple hidden><input id="localFolder" type="file" webkitdirectory directory multiple hidden></div><div class="upload-list" id="uploadList">No files selected.</div></div><div class="file-source"><h3>From the configured project</h3><div class="row"><input id="fileFolder" value="" placeholder="Project folder path"><button id="refreshFiles">Open</button><button id="selectCurrentFolder">Add this folder</button></div><div class="file-picker" id="filePicker"></div></div><div class="row" style="margin-top:12px"><button class="primary" id="attachFiles">Add to chat workspace</button></div></div></div></div>
<div class="toast" id="toast"></div>
<script>
const TOKEN='__TOKEN__';const HINT='Kurt can make mistakes. Attachments are copied into an isolated chat workspace; the project stays unchanged unless you explicitly apply a file.';let state={models:[],providers:[],keys:[],remoteModels:[],chats:[],chatId:null,projectRoot:'',selectedFiles:[],selectedFolders:[],pendingUploads:[],contextCount:0,busy:false,abort:null,run:0,settings:{},sort:'newest',effort:'thinking',mode:'normal'};
const $=id=>document.getElementById(id);const api=async(path,body)=>{const res=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json','X-Kurt-Token':TOKEN},body:body?JSON.stringify(body):undefined});const data=await res.json().catch(()=>({error:'Invalid server response'}));if(!res.ok)throw new Error(data.error||res.statusText);return data};
function toast(msg){const el=$('toast');el.textContent=msg;el.classList.add('show');setTimeout(()=>el.classList.remove('show'),2600)}
function setScrollMode(enabled){const compact=!!enabled;document.body.classList.toggle('compact-scroll',compact);const b=$('scrollModeBtn');if(b){b.setAttribute('aria-pressed',String(compact));b.title=compact?'Use normal page scrolling':'Enable compact internal scrolling';b.setAttribute('aria-label',b.title)}try{localStorage.setItem('kurt-compact-scroll',compact?'1':'0')}catch(e){}}
function modelLabel(m){return `${m.label||m.name||m.model} · ${m.provider}${m.current?' · active':''}`}
function fillModels(){const html=state.models.map(m=>`<option value="${esc(m.ref)}">${esc(modelLabel(m))}</option>`).join('');$('topModel').innerHTML=html||'<option>No models configured</option>';$('settingsModel').innerHTML=html||'<option>No models configured</option>';if(state.settings.model){$('topModel').value=state.settings.model;$('settingsModel').value=state.settings.model}renderLocalModels()}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function stripUnsupportedActions(src){return String(src||'')
 .replace(/@@@\s*(?:RUN|SEARCH|SYSTEM_PROMPT|READ|EXEC|BASH|SHELL)\b[\s\S]*?(?:@@@\s*END\b|$)/gi,'')
 .replace(/<\|tool_call_start\|>[\s\S]*?(?:<\|tool_call_end\|>|$)/gi,'')
 .replace(/<\|tool_call_(?:start|end)\|>/gi,'')
 .trim()}
function inline(s){const spans=[];s=s.replace(/`([^`\n]+)`/g,(_,c)=>{spans.push('<code>'+c+'</code>');return '\u0001'+(spans.length-1)+'\u0001'});s=s.replace(/\*\*([^*\n]+?)\*\*/g,'<strong>$1</strong>').replace(/(^|[^*\w])\*([^*\n]+?)\*(?![*\w])/g,'$1<em>$2</em>').replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,'<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');return s.replace(/\u0001(\d+)\u0001/g,(_,i)=>spans[+i])}
function mdBlocks(text){const lines=esc(text).split('\n');let html='',para=[],list=null,quote=[];const flushP=()=>{if(para.length){html+='<p>'+inline(para.join('\n')).replace(/\n/g,'<br>')+'</p>';para=[]}};const flushL=()=>{if(list){html+='<'+list.t+'>'+list.items.map(i=>'<li>'+inline(i)+'</li>').join('')+'</'+list.t+'>';list=null}};const flushQ=()=>{if(quote.length){html+='<blockquote>'+inline(quote.join('<br>'))+'</blockquote>';quote=[]}};for(const line of lines){let m;if(!line.trim()){flushP();flushL();flushQ();continue}if(m=/^(#{1,6})\s+(.*)$/.exec(line)){flushP();flushL();flushQ();const n=Math.min(m[1].length,3);html+='<h'+n+'>'+inline(m[2])+'</h'+n+'>';continue}if(/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)){flushP();flushL();flushQ();html+='<hr>';continue}if(m=/^&gt;\s?(.*)$/.exec(line)){flushP();flushL();quote.push(m[1]);continue}if(m=/^\s*[-*+]\s+(.*)$/.exec(line)){flushP();flushQ();if(!list||list.t!=='ul'){flushL();list={t:'ul',items:[]}}list.items.push(m[1]);continue}if(m=/^\s*\d+[.)]\s+(.*)$/.exec(line)){flushP();flushQ();if(!list||list.t!=='ol'){flushL();list={t:'ol',items:[]}}list.items.push(m[1]);continue}flushL();flushQ();para.push(line)}flushP();flushL();flushQ();return html}
function renderMarkdown(src){src=String(src||'');return src.split(/(```[^\n]*\n[\s\S]*?```|```[^\n]*\n[\s\S]*$|```[^\n]*$)/).map((seg,i)=>{if(i%2===0)return mdBlocks(seg);const nl=seg.indexOf('\n');const lang=(nl<0?seg.slice(3):seg.slice(3,nl)).trim().split(/\s+/)[0]||'';let code=nl<0?'':seg.slice(nl+1);if(nl>=0&&seg.endsWith('```'))code=code.replace(/\n?```$/,'');return '<div class="codebox"><div class="codehead"><span>'+esc(lang||'code')+'</span><button class="copy-mini" data-copy-code>Copy code</button></div><pre><code>'+esc(code.replace(/\n$/,''))+'</code></pre></div>'}).join('')}
function splitThink(raw,streaming){let think='',answer='',open=false;const closer=/<\/think(?:ing)?>/i.exec(raw),opener=/<think(?:ing)?>/i.exec(raw);if(closer&&(!opener||closer.index<opener.index)){think+=raw.slice(0,closer.index)+'\n';raw=raw.slice(closer.index+closer[0].length)}while(raw.length){const m=/<think(?:ing)?>/i.exec(raw);if(!m){answer+=raw;break}answer+=raw.slice(0,m.index);const after=raw.slice(m.index+m[0].length);const c=/<\/think(?:ing)?>/i.exec(after);if(!c){think+=after;open=true;break}think+=after.slice(0,c.index)+'\n';raw=after.slice(c.index+c[0].length)}answer=answer.replace(/^\s+/,'');if(streaming)answer=answer.replace(/<\/?(?:t(?:h(?:i(?:n(?:k(?:i(?:n(?:g)?)?)?)?)?)?)?)?$/i,'');return{think:think.trim(),answer,open}}
function fmtSecs(s){s=Math.max(1,Math.round(s));return s<60?s+'s':Math.floor(s/60)+'m '+(s%60)+'s'}
function bindCopy(root){root.querySelectorAll('[data-copy-code]').forEach(b=>b.onclick=()=>{const code=b.closest('.codebox').querySelector('code');navigator.clipboard.writeText(code?code.textContent:'').then(()=>toast('Code copied'))})}
function thinkBlock(){const d=document.createElement('div');d.className='think work-log open';d.hidden=true;d.innerHTML='<button class="work-log-head" type="button" aria-expanded="true"><svg class="ic chev"><use href="#i-chev"/></svg><span class="think-label">Reasoning summaries & execution trace</span></button><div class="work-log-body"></div>';d.querySelector('.work-log-head').onclick=()=>{const open=d.classList.toggle('open');d.querySelector('.work-log-head').setAttribute('aria-expanded',String(open))};return d}
function setThink(node,entries){const t=node.querySelector('.work-log');if(!t)return;const items=Array.isArray(entries)?entries:[];if(!items.length){t.hidden=true;return}t.hidden=false;const head=t.querySelector('.work-log-head');const body=t.querySelector('.work-log-body');const initialized=t.dataset.initialized==='1';const parentOpen=initialized?t.classList.contains('open'):true;const childStates=new Map(Array.from(body.querySelectorAll('details.work-step')).map(row=>[row.querySelector('summary')?.textContent,row.open]));if(!initialized)t.dataset.initialized='1';t.classList.toggle('open',parentOpen);head.setAttribute('aria-expanded',String(parentOpen));body.innerHTML='';items.forEach(item=>{const data=typeof item==='string'?{text:item}:item||{};const summary=String(data.text||'Progress update');const detail=String(data.detail||'');if(detail&&detail!==summary){const row=document.createElement('details');row.className='work-step'+(data.ok===true?' ok':data.ok===false?' fail':'');row.open=childStates.has(summary)?childStates.get(summary):true;const label=document.createElement('summary');label.textContent=summary;const contents=document.createElement('div');contents.className='work-detail';contents.textContent=detail;row.append(label,contents);body.appendChild(row)}else{const row=document.createElement('div');row.className='work-step'+(data.ok===true?' ok':data.ok===false?' fail':'');row.textContent=summary;body.appendChild(row)}});t.querySelector('.think-label').textContent='Reasoning summaries & execution trace · '+items.length+' step(s)'}
function pushWorkStep(node,text,ok,detail){const entries=node.__worklog||(node.__worklog=[]);const last=entries[entries.length-1];if(last&&last.text===text&&last.ok===ok&&last.detail===detail)return;entries.push({text,ok,detail:detail||''});if(entries.length>60)entries.shift();setThink(node,entries)}
function endThink(node){const status=node.querySelector('.assistant-status');if(status)status.textContent='';const t=node.querySelector('.work-log');if(t)t.classList.remove('live')}
let follow=true;
function scrollEnd(force){if(!force&&!follow)return;if(document.body.classList.contains('compact-scroll')){const m=$('messages');m.scrollTop=m.scrollHeight}else{window.scrollTo({top:document.documentElement.scrollHeight,behavior:'auto'})}}
function setEmpty(on){$('main').classList.toggle('empty',on);if(on)$('toBottom').classList.remove('show')}
function setTitle(title,meta){$('chatTitle').textContent=title||'New chat';$('chatMeta').textContent=meta||''}
function autosize(){const t=$('prompt');t.style.height='auto';t.style.height=Math.min(t.scrollHeight,220)+'px'}
function updateSend(){$('sendBtn').disabled=!state.busy&&!$('prompt').value.trim()}
function hint(){const base=state.contextCount?state.contextCount+' item(s) in this chat’s isolated workspace':HINT;$('composeHint').textContent=state.mode==='code'?base+' Code mode can run approved developer checks in a per-chat Python venv; a venv is not a full operating-system sandbox.':base}
function setBusy(b){state.busy=b;const btn=$('sendBtn');btn.querySelector('use').setAttribute('href',b?'#i-stop':'#i-up');btn.title=b?'Stop response':'Send message';btn.setAttribute('aria-label',btn.title);updateSend()}
function cancelRun(){if(state.abort)state.abort.abort();state.abort=null;state.run++;setBusy(false)}
function renderMessage(role,content,artifacts,extra){const wrap=document.createElement('div');wrap.className='message '+role;let safeContent=role==='assistant'?stripUnsupportedActions(content):String(content||'');if(role==='assistant'&&!safeContent&&/(?:@@@\s*(?:RUN|READ|EXEC)|tool_call|<think)/i.test(String(content||''))){safeContent='This earlier response contained an unsupported tool action. Kurt did not execute it.'}wrap.dataset.raw=safeContent;const bubble=document.createElement('div');bubble.className='bubble';wrap.appendChild(bubble);if(role==='user'){bubble.textContent=content}else{bubble.insertAdjacentHTML('beforeend','<div class="assistant-heading"><svg class="mark" viewBox="0 0 20 16" aria-hidden="true"><use href="#kurt-k"/></svg><span>Kurt</span><span class="assistant-status"></span></div>');bubble.appendChild(thinkBlock());bubble.insertAdjacentHTML('beforeend','<div class="content"></div><div class="message-artifacts"></div><div class="msg-foot"><button class="icon-btn copy-msg" title="Copy" aria-label="Copy message"><svg class="ic"><use href="#i-copy"/></svg></button></div>');bubble.querySelector('.content').innerHTML=renderMarkdown(safeContent);bindCopy(wrap);setThink(wrap,extra?.worklog||[]);wrap.__worklog=Array.isArray(extra?.worklog)?extra.worklog.slice():[];renderArtifactTriggers(bubble.querySelector('.message-artifacts'),artifacts||[]);bubble.querySelector('.copy-msg').onclick=()=>navigator.clipboard.writeText(wrap.dataset.raw||'').then(()=>toast('Message copied'))}$('messages').appendChild(wrap);return wrap}
function clearArtifactPane(){const pane=$('artifactPaneContent');if(pane)pane.innerHTML='';const main=$('main');if(main)main.classList.remove('has-artifacts');document.querySelectorAll('.artifact-trigger').forEach(button=>{button.classList.remove('selected');button.setAttribute('aria-expanded','false')});const title=$('artifactPaneTitle');if(title)title.textContent='File preview';const count=$('artifactCount');if(count)count.textContent='Select a file card to preview'}
function closeArtifactPane(){const main=$('main');if(main)main.classList.remove('has-artifacts');const pane=$('artifactPaneContent');if(pane)pane.innerHTML='';document.querySelectorAll('.artifact-trigger.selected').forEach(button=>{button.classList.remove('selected');button.setAttribute('aria-expanded','false')});const title=$('artifactPaneTitle');if(title)title.textContent='File preview';const count=$('artifactCount');if(count)count.textContent='Select a file card to preview'}
function renderArtifactTriggers(container,artifacts){if(!container)return;container.innerHTML='';(artifacts||[]).forEach(a=>{const promptCard=a.kind==='system_prompt';const button=document.createElement('button');button.type='button';button.className='artifact-trigger';button.setAttribute('aria-controls','artifactPane');button.setAttribute('aria-expanded','false');button.innerHTML='<span class="artifact-trigger-icon"><svg class="ic" aria-hidden="true"><use href="#i-file"/></svg></span><span class="artifact-trigger-text"><span class="artifact-trigger-name"></span><span class="artifact-trigger-status"></span></span><span class="artifact-trigger-open">Preview →</span>';button.querySelector('.artifact-trigger-name').textContent=promptCard?'System prompt proposal':String(a.path||'Generated file');button.querySelector('.artifact-trigger-status').textContent=a.validation?.message||'Saved in the temporary workspace';button.setAttribute('aria-label','Preview '+(a.path||'generated file'));button.onclick=()=>openArtifactPreview(a,button);container.appendChild(button)})}
function openArtifactPreview(a,trigger){const pane=$('artifactPaneContent');if(!pane)return;pane.replaceChildren(artifactCard(a));$('main').classList.add('has-artifacts');document.querySelectorAll('.artifact-trigger').forEach(button=>{const selected=button===trigger;button.classList.toggle('selected',selected);button.setAttribute('aria-expanded',String(selected))});const title=$('artifactPaneTitle');if(title)title.textContent=a.kind==='system_prompt'?'System prompt proposal':String(a.path||'File preview');const count=$('artifactCount');if(count)count.textContent='Selected file preview';const side=$('artifactPane');side.scrollTop=0}
function artifactCard(a){const promptCard=a.kind==='system_prompt';const card=document.createElement('div');card.className='artifact open'+(a.validation?.ok?'':' bad');card.innerHTML=`<div class="artifact-top"><div class="file-icon"><svg class="ic"><use href="#i-file"/></svg></div><div class="file-name">${esc(a.path)}</div><div class="file-check">${esc(a.validation?.message||'Saved in temporary workspace')}</div><span class="artifact-chev"><svg class="ic"><use href="#i-chev"/></svg></span></div><div class="artifact-body"><div class="codebox"><div class="codehead"><span>${promptCard?'SYSTEM PROMPT':esc((a.path.split('.').pop()||'FILE').toUpperCase())}</span><button class="copy-mini copy-artifact">Copy</button></div><pre><code></code></pre></div><div class="artifact-actions"><button class="copy-artifact">Copy</button>${promptCard?(a.preview_only?'':'<button class="primary apply-prompt">Apply prompt to Kurt</button>'):'<button class="download-artifact">Download file</button><button class="primary apply-artifact">Apply to project</button>'}</div></div>`;card.querySelector('code').textContent=a.content;card.querySelector('.artifact-top').onclick=()=>card.classList.toggle('open');card.querySelectorAll('.copy-artifact').forEach(b=>b.onclick=()=>navigator.clipboard.writeText(a.content).then(()=>toast('Content copied')));if(promptCard&&!a.preview_only){card.querySelector('.apply-prompt').onclick=async()=>{if(!confirm('Replace Kurt’s editable system prompt with this proposed version?'))return;try{await api('/api/settings',{system_prompt:a.content});await refreshBootstrap();toast('Kurt system prompt updated')}catch(e){toast(e.message)}}}else if(!promptCard){card.querySelector('.download-artifact').onclick=()=>{const blob=new Blob([a.content],{type:'application/octet-stream'}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=a.path.split('/').pop();link.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};card.querySelector('.apply-artifact').onclick=async()=>{if(!a.validation?.ok){toast('Fix validation errors before applying');return}if(!state.settings.allow_project_writes){toast('Enable project writes in Settings first');return}const overwrite=confirm(`Apply ${a.path} to ${state.projectRoot}?${a.exists?'\nThis will overwrite the existing project file.':''}\n\nThe isolated workspace copy will remain unchanged.`);if(!overwrite)return;try{const r=await api('/api/file/write',{path:a.path,content:a.content,overwrite,authorized:true});toast(r.message||'Applied to project')}catch(e){toast(e.message)}}}return card}
const SUGGESTIONS=[['Understand my project','Inspect the current project structure and explain its architecture.'],['Build a feature','Help me plan and implement a new feature. Ask for any missing requirements first.'],['Generate a file','Write a small utility script and return it as a file card.'],['Debug code','Help me debug this issue. Start by asking for the relevant error and files.']];
function showWelcome(){clearArtifactPane();setEmpty(true);follow=true;$('messages').innerHTML='<div class="welcome"><svg class="mark xl" viewBox="0 0 20 16" aria-hidden="true"><use href="#kurt-k"/></svg><h1>What are we building?</h1></div>';$('chips').innerHTML=SUGGESTIONS.map(s=>'<button class="chip" data-prompt="'+esc(s[1])+'">'+esc(s[0])+'</button>').join('');document.querySelectorAll('#chips [data-prompt]').forEach(b=>b.onclick=()=>{$('prompt').value=b.dataset.prompt;autosize();updateSend();$('prompt').focus()})}
function renderChats(){const el=$('chatList');el.innerHTML='';const chats=state.chats.slice().sort((a,b)=>{if(!!a.pinned!==!!b.pinned)return a.pinned?-1:1;return state.sort==='oldest'?(a.updated||0)-(b.updated||0):(b.updated||0)-(a.updated||0)});chats.forEach(c=>{const row=document.createElement('div');row.className='chat-row'+(c.id===state.chatId?' active':'');const title=document.createElement('button');title.type='button';title.className='chat-item';title.title=c.title||'New conversation';title.innerHTML=(c.pinned?'<span class="pin-indicator">◆</span>':'')+esc(c.title||'New conversation');title.onclick=()=>loadChat(c.id);const actions=document.createElement('div');actions.className='chat-actions';const pin=document.createElement('button');pin.className='chat-action';pin.title=c.pinned?'Unpin chat':'Pin chat';pin.setAttribute('aria-label',pin.title);pin.textContent=c.pinned?'◆':'◇';pin.onclick=e=>{e.stopPropagation();togglePinChat(c.id,!c.pinned)};const rename=document.createElement('button');rename.className='chat-action';rename.title='Rename chat';rename.setAttribute('aria-label','Rename chat');rename.textContent='✎';rename.onclick=e=>{e.stopPropagation();renameChat(c.id,c.title||'')};const del=document.createElement('button');del.className='chat-action danger';del.title='Delete chat';del.setAttribute('aria-label','Delete chat');del.textContent='×';del.onclick=e=>{e.stopPropagation();deleteChat(c.id)};actions.append(pin,rename,del);row.append(title,actions);el.appendChild(row)})}
async function renameChat(id,current){const next=prompt('Rename chat:',current);if(next===null)return;const title=next.trim();if(!title){toast('Chat title cannot be empty');return}try{await api('/api/chats/'+encodeURIComponent(id),{rename:title});await refreshChats();if(id===state.chatId)setTitle(title,$('chatMeta').textContent)}catch(e){toast(e.message)}}
async function togglePinChat(id,pinned){try{await api('/api/chats/'+encodeURIComponent(id),{pin:!!pinned});await refreshChats()}catch(e){toast(e.message)}}
async function deleteChat(id){const chat=state.chats.find(c=>c.id===id);if(!confirm('Delete “'+(chat?.title||'this chat')+'” and its temporary workspace? This cannot be undone.'))return;if(state.chatId===id)cancelRun();try{await api('/api/chats/'+encodeURIComponent(id),{delete:true});if(state.chatId===id){state.chatId=null;resetComposer();showWelcome();setTitle('New chat','')}await refreshChats();toast('Chat deleted')}catch(e){toast(e.message)}}
async function refreshBootstrap(){const d=await api('/api/bootstrap');state.models=d.models||[];state.providers=d.providers||[];state.keys=d.keys||[];state.remoteModels=d.remote_models||[];state.settings=d.settings||{};state.projectRoot=d.project_root||'';$('projectRoot').value=state.projectRoot;$('systemPrompt').value=state.settings.system_prompt||'';$('allowProjectWrites').checked=!!state.settings.allow_project_writes;fillModels();renderKeys();renderRemoteModels();fillProviders()}
function fillProviders(){const opts=state.providers.map(p=>`<option value="${esc(p.id)}">${esc(p.label)}</option>`).join('');$('keyProvider').innerHTML=opts;$('remoteProvider').innerHTML=opts;const groups=[...new Set(state.keys.map(k=>k.group||'default'))].sort();$('keyPoolOptions').innerHTML=groups.map(g=>`<option value="${esc(g)}"></option>`).join('')}
function renderLocalModels(){const el=$('localModelList');if(!el)return;el.innerHTML='';const local=state.models.filter(m=>m.type==='local');if(!local.length){el.innerHTML='<div class="empty">No Ollama models are installed yet.</div>';return}local.forEach(m=>{const row=document.createElement('div');row.className='local-model-row';const grow=document.createElement('div');grow.className='grow';grow.innerHTML=`<b>${esc(m.label||m.name)}</b><small>${esc(m.size?String(m.size):'Ollama local model')}${m.current?' · active':''}</small>`;const set=document.createElement('button');set.textContent=m.current?'Current':'Set active';set.disabled=!!m.current;set.onclick=async()=>{try{await api('/api/settings',{model:m.ref});await refreshBootstrap();toast('Active model changed')}catch(e){toast(e.message)}};row.append(grow,set);el.appendChild(row)})}
function renderKeys(){const el=$('keysList');el.innerHTML='';if(!state.keys.length){el.innerHTML='<div class="empty">No API keys configured yet.</div>';fillProviders();return}const groups=new Map();state.keys.forEach(k=>{const provider=k.provider||'unknown',group=k.group||'default',key=provider+'\u0000'+group;if(!groups.has(key))groups.set(key,{provider,group,keys:[]});groups.get(key).keys.push(k)});[...groups.values()].sort((a,b)=>(a.provider+a.group).localeCompare(b.provider+b.group)).forEach(pool=>{const card=document.createElement('div');card.className='pool-card';const head=document.createElement('div');head.className='pool-head';const grow=document.createElement('div');grow.className='grow';grow.innerHTML=`<b>${esc(pool.provider)} · ${esc(pool.group)}</b><small>${pool.keys.length} key(s) · automatic failover enabled for this pool</small>`;const actions=document.createElement('div');actions.className='pool-actions';const rename=document.createElement('button');rename.textContent='Rename group';rename.onclick=async()=>{const next=prompt('New group name (all keys and linked models in this pool will move):',pool.group);if(next===null||!next.trim()||next.trim()===pool.group)return;try{await api('/api/settings',{key_action:'rename_group',provider:pool.provider,old_group:pool.group,new_group:next.trim()});await refreshBootstrap();toast('Key group renamed')}catch(e){toast(e.message)}};actions.append(rename);head.append(grow,actions);card.append(head);pool.keys.forEach(k=>{const row=document.createElement('div');row.className='key-row';row.innerHTML=`<div class="grow"><b>${esc(k.label)}</b><small>${esc(k.masked)}</small></div><button class="edit-key">Edit</button><button class="danger">Remove</button>`;row.querySelector('.edit-key').onclick=async()=>{const label=prompt('Account label:',k.label);if(label===null)return;const group=prompt('Move this key to a different pool (use Rename group to move the whole pool):',k.group);if(group===null)return;const value=prompt('New API key (leave blank to keep current):','');if(value===null)return;try{await api('/api/settings',{key_action:'edit',id:k.id,label,group,value});await refreshBootstrap();toast('API key updated')}catch(e){toast(e.message)}};row.querySelector('.danger').onclick=async()=>{if(!confirm('Remove this API key?'))return;try{await api('/api/settings',{key_action:'remove',id:k.id});await refreshBootstrap()}catch(e){toast(e.message)}};card.append(row)});el.append(card)});fillProviders()}

function renderRemoteModels(){const el=$('remoteList');el.innerHTML='';state.remoteModels.forEach(m=>{const d=document.createElement('div');d.className='model-row';d.innerHTML=`<div class="grow"><b>${esc(m.label||m.model)}</b><small>${esc(m.provider)} · ${esc(m.model)} · pool ${esc(m.key_group||'default')}</small></div><button class="edit-model">Edit</button><button class="danger">Remove</button>`;d.querySelector('.edit-model').onclick=async()=>{const model=prompt('API model ID:',m.model);if(model===null)return;const label=prompt('Display name:',m.label||m.model);if(label===null)return;const group=prompt('Key pool:',m.key_group||'default');if(group===null)return;const base=prompt('API base URL:',m.base_url||'');if(base===null)return;try{await api('/api/settings',{model_action:'edit',id:m.id,model,label,key_group:group,base_url:base});await refreshBootstrap();toast('Model settings updated')}catch(e){toast(e.message)}};d.querySelector('.danger').onclick=async()=>{if(!confirm('Remove this remote model?'))return;await api('/api/settings',{model_action:'remove',id:m.id});await refreshBootstrap()};el.appendChild(d)})}
async function refreshChats(){const d=await api('/api/chats');state.chats=d.chats||[];renderChats()}
function resetComposer(){$('prompt').value='';autosize();state.selectedFiles=[];state.selectedFolders=[];state.pendingUploads=[];state.contextCount=0;renderPendingUploads();hint();updateSend()}
async function ensureChat(){if(!state.chatId){const d=await api('/api/chats',{create:true});state.chatId=d.id}return state.chatId}
async function newChat(){cancelRun();state.chatId=null;resetComposer();showWelcome();setTitle('New chat','');renderChats();follow=true;window.scrollTo({top:0,behavior:'auto'});document.body.classList.remove('nav-open');$('prompt').focus()}
async function loadChat(id){cancelRun();clearArtifactPane();const d=await api('/api/chats/'+encodeURIComponent(id));state.chatId=id;state.contextCount=0;state.effort=d.effort||'thinking';state.mode=d.mode||'normal';$('effortSelect').value=state.effort;$('operationMode').value=state.mode;try{const c=await api('/api/context/status?chat_id='+encodeURIComponent(id));state.contextCount=c.files||0}catch(e){}hint();$('messages').innerHTML='';const list=d.messages||[];if(list.length){setEmpty(false);list.forEach(m=>renderMessage(m.role,m.content,m.artifacts||[],{worklog:m.worklog||[]}));follow=true;scrollEnd(true)}else showWelcome();setTitle(d.title||'Conversation',[d.model||'',state.effort+' · '+state.mode].filter(Boolean).join(' · '));renderChats();document.body.classList.remove('nav-open')}
async function send(){const text=$('prompt').value.trim();if(!text||state.busy)return;const run=++state.run;state.effort=$('effortSelect').value;state.mode=$('operationMode').value;setBusy(true);try{await ensureChat()}catch(e){if(run===state.run)setBusy(false);toast(e.message);return}if(run!==state.run)return;$('prompt').value='';autosize();hint();if($('main').classList.contains('empty')){setEmpty(false);$('messages').innerHTML=''}follow=true;renderMessage('user',text);const node=renderMessage('assistant','');node.classList.add('live');node.querySelector('.assistant-status').textContent='Preparing…';scrollEnd(true);$('chatMeta').textContent=state.effort+' · '+state.mode+' · Generating…';const contentEl=node.querySelector('.content');let raw='',pending=false;const controller=new AbortController();state.abort=controller;
const paint=()=>{pending=false;if(run!==state.run)return;const safeAnswer=stripUnsupportedActions(raw);contentEl.innerHTML=safeAnswer?renderMarkdown(safeAnswer):'';if(safeAnswer)bindCopy(node);node.dataset.raw=safeAnswer;scrollEnd()};
const queue=()=>{if(!pending){pending=true;requestAnimationFrame(paint)}};
try{const res=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json','X-Kurt-Token':TOKEN},body:JSON.stringify({chat_id:state.chatId,text,model:$('topModel').value,effort:state.effort,mode:state.mode}),signal:controller.signal});if(!res.ok){const x=await res.json().catch(()=>({}));throw new Error(x.error||res.statusText)}const reader=res.body.getReader(),decoder=new TextDecoder();let buffer='';while(true){const {value,done}=await reader.read();if(done)break;if(run!==state.run)return;buffer+=decoder.decode(value,{stream:true});let sep;while((sep=buffer.indexOf('\n\n'))>=0){const frame=buffer.slice(0,sep);buffer=buffer.slice(sep+2);const dataLine=frame.split('\n').find(x=>x.startsWith('data:'));if(!dataLine)continue;const event=JSON.parse(dataLine.slice(5).trim());if(event.type==='phase'){pushWorkStep(node,event.text,event.ok,event.detail);node.querySelector('.assistant-status').textContent=event.status||'Working…';queue()}else if(event.type==='token'){raw+=event.text;node.querySelector('.assistant-status').textContent='Writing…';queue()}else if(event.type==='done'){pending=false;raw=event.content||'';const safeFinal=stripUnsupportedActions(raw);contentEl.innerHTML=safeFinal?renderMarkdown(safeFinal):'';bindCopy(node);node.dataset.raw=safeFinal;setThink(node,event.worklog||node.__worklog||[]);renderArtifactTriggers(node.querySelector('.message-artifacts'),event.artifacts||[]);setTitle(event.title||'Conversation',[event.model||'',event.effort||state.effort,event.mode||state.mode].filter(Boolean).join(' · '));scrollEnd();await refreshChats()}else if(event.type==='error')throw new Error(event.message)}}}catch(e){if(run===state.run){if(e.name==='AbortError'){paint();refreshChats().catch(()=>{})}else{contentEl.classList.add('err');contentEl.textContent='Error: '+e.message;toast(e.message)}}}finally{node.classList.remove('live');endThink(node);if(run===state.run){setBusy(false);state.abort=null;const meta=$('chatMeta');if(meta.textContent.includes('Generating…'))meta.textContent=[state.effort,state.mode,'Ready'].join(' · ')}}}
$('messages').addEventListener('scroll',()=>{if(!document.body.classList.contains('compact-scroll'))return;const m=$('messages');follow=m.scrollHeight-m.scrollTop-m.clientHeight<60;$('toBottom').classList.toggle('show',!follow&&!$('main').classList.contains('empty'))});
window.addEventListener('scroll',()=>{if(document.body.classList.contains('compact-scroll'))return;follow=window.innerHeight+window.scrollY>=document.documentElement.scrollHeight-60;$('toBottom').classList.toggle('show',!follow&&!$('main').classList.contains('empty'))},{passive:true});
$('toBottom').onclick=()=>{follow=true;scrollEnd(true);$('toBottom').classList.remove('show')};
$('closeArtifactPane').onclick=closeArtifactPane;
$('scrollModeBtn').onclick=()=>{setScrollMode(!document.body.classList.contains('compact-scroll'));follow=true;scrollEnd(true)};
$('sendBtn').onclick=()=>{if(state.busy){if(state.abort)state.abort.abort()}else send()};
$('prompt').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&e.keyCode!==229){e.preventDefault();if(!state.busy)send()}});
$('prompt').addEventListener('input',()=>{autosize();updateSend()});
$('newChat').onclick=newChat;$('chatSort').onchange=()=>{state.sort=$('chatSort').value;renderChats()};$('effortSelect').onchange=()=>{state.effort=$('effortSelect').value};$('operationMode').onchange=()=>{state.mode=$('operationMode').value;hint()};
function activateSettingsTab(tab){document.querySelectorAll('[data-settings-tab]').forEach(b=>b.classList.toggle('active',b.dataset.settingsTab===tab));document.querySelectorAll('[data-settings-panel]').forEach(p=>p.classList.toggle('active',p.dataset.settingsPanel===tab));const area=document.querySelector('.settings-panel-area');if(area)area.scrollTop=0}
document.querySelectorAll('[data-settings-tab]').forEach(b=>b.onclick=()=>activateSettingsTab(b.dataset.settingsTab));
async function openSettings(){await refreshBootstrap();activateSettingsTab('general');$('settingsModal').classList.add('show')}
async function saveSettings(){const payload={project_root:$('projectRoot').value,system_prompt:$('systemPrompt').value,allow_project_writes:$('allowProjectWrites').checked};if($('settingsModel').value)payload.model=$('settingsModel').value;const d=await api('/api/settings',payload);state.settings=d.settings||{};state.settings.allow_project_writes=$('allowProjectWrites').checked;state.projectRoot=d.project_root;if(d.model){$('topModel').value=d.model;$('settingsModel').value=d.model}toast('Settings saved')}
async function browseFiles(){const path=$('fileFolder').value.trim();const d=await api('/api/files?path='+encodeURIComponent(path));$('fileFolder').value=d.path;const el=$('filePicker');el.innerHTML='';if(d.parent!==null){const b=document.createElement('div');b.className='file-row folder';b.innerHTML='<span>↰</span><label>Parent folder</label>';b.onclick=()=>{$('fileFolder').value=d.parent;browseFiles()};el.appendChild(b)}d.items.forEach(item=>{const row=document.createElement('div');row.className='file-row'+(item.directory?' folder':'');if(item.directory){row.innerHTML=`<span>📁</span><label>${esc(item.name)}/</label><span class="inline-note">Open</span>`;row.onclick=()=>{$('fileFolder').value=item.path;browseFiles()}}else{row.innerHTML=`<input type="checkbox" value="${esc(item.path)}" ${state.selectedFiles.includes(item.path)?'checked':''}><label>${esc(item.name)}</label><small>${esc(item.size)}</small>`;const check=row.querySelector('input');check.onchange=()=>{if(check.checked&&!state.selectedFiles.includes(item.path))state.selectedFiles.push(item.path);else if(!check.checked)state.selectedFiles=state.selectedFiles.filter(x=>x!==item.path);renderPendingUploads()}}el.appendChild(row)})}
function renderPendingUploads(){const labels=[];if(state.selectedFiles.length)labels.push(state.selectedFiles.length+' project file(s)');if(state.selectedFolders.length)labels.push(state.selectedFolders.length+' project folder(s)');if(state.pendingUploads.length)labels.push(state.pendingUploads.length+' computer file(s), including images');$('uploadList').textContent=labels.length?labels.join(' · '):'No files selected.'}
function stageLocalFiles(input){const files=Array.from(input.files||[]);for(const file of files){const path=(file.webkitRelativePath||file.name).replace(/\\/g,'/');if(!state.pendingUploads.some(x=>x.path===path))state.pendingUploads.push({file,path})}input.value='';renderPendingUploads()}
function bytesToBase64(buffer){const bytes=new Uint8Array(buffer);let binary='';const chunk=0x8000;for(let i=0;i<bytes.length;i+=chunk)binary+=String.fromCharCode(...bytes.subarray(i,i+chunk));return btoa(binary)}
async function attachFiles(){try{await ensureChat();const selectedFiles=state.selectedFiles.slice();const uploads=[];let total=0;for(const item of state.pendingUploads){total+=item.file.size;if(total>20*1024*1024)throw new Error('Local file selection exceeds the 20 MB import limit.');uploads.push({path:item.path,content:bytesToBase64(await item.file.arrayBuffer())})}const result=await api('/api/context/import',{chat_id:state.chatId,selected_files:state.selectedFiles,selected_folders:state.selectedFolders,uploads});state.contextCount=(state.contextCount||0)+Number(result.files||0);state.selectedFiles=[];state.selectedFolders=[];state.pendingUploads=[];renderPendingUploads();$('filesModal').classList.remove('show');hint();toast(result.message||'Context added to isolated workspace')}catch(e){toast(e.message)}}
async function startDownload(){const model=$('downloadModel').value.trim();if(!model){toast('Enter an Ollama model ID');return}try{const d=await api('/api/download',{model});$('downloadCaption').textContent='Queued: '+model;$('localDownloadStatus').textContent='Download queued: '+model;$('downloadModel').value='';toast('Download started in background');pollDownload()}catch(e){toast(e.message)}}
async function pollDownload(){try{const d=await api('/api/download/status');const job=d.job;if(!job)return;const percent=job.total?job.completed/job.total*100:0;$('downloadFill').style.width=(job.state==='done'?100:percent)+'%';$('downloadCaption').textContent=job.state==='running'?`${job.model} · ${percent.toFixed(1)}% · ${job.human}`:`${job.model} · ${job.state}${job.error?' · '+job.error:''}`;if(job.state==='done'){await refreshBootstrap();$('localDownloadStatus').textContent=job.model+' installed successfully.';toast('Model download complete')}else if(job.state==='error'){$('localDownloadStatus').textContent=job.error||'Download failed';toast(job.error||'Download failed')}else{$('localDownloadStatus').textContent=job.model+' · '+percent.toFixed(1)+'% · '+job.human;setTimeout(pollDownload,1000)}}catch(e){console.error(e)}}
$('sidebarSettings').onclick=openSettings;$('openFiles').onclick=$('contextBtn').onclick=()=>{$('filesModal').classList.add('show');renderPendingUploads();browseFiles()};$('topModel').onchange=async()=>{try{await api('/api/settings',{model:$('topModel').value});$('settingsModel').value=$('topModel').value;await refreshBootstrap();toast('Active model changed')}catch(e){toast(e.message)}};$('settingsModel').onchange=async()=>{const model=$('settingsModel').value;$('topModel').value=model;try{await api('/api/settings',{model});await refreshBootstrap();toast('Active model changed')}catch(e){toast(e.message)}};$('saveSettings').onclick=saveSettings;$('reloadSettings').onclick=refreshBootstrap;$('addKey').onclick=async()=>{const value=$('keyValue').value.trim();if(!value){toast('Enter an API key');return}await api('/api/settings',{key_action:'add',provider:$('keyProvider').value,group:$('keyGroup').value,label:$('keyLabel').value,value});$('keyValue').value='';await refreshBootstrap();toast('API key saved locally')};$('addRemote').onclick=async()=>{const d=await api('/api/settings',{model_action:'add',provider:$('remoteProvider').value,model:$('remoteModelId').value,label:$('remoteLabel').value,key_group:$('remoteGroup').value,base_url:$('remoteBase').value});await refreshBootstrap();toast(d.message||'Model added')};$('refreshFiles').onclick=browseFiles;$('selectCurrentFolder').onclick=()=>{const path=$('fileFolder').value.trim();if(!state.selectedFolders.includes(path))state.selectedFolders.push(path);renderPendingUploads();toast('Current folder staged for this chat')};$('chooseLocalFiles').onclick=()=>$('localFiles').click();$('chooseLocalFolder').onclick=()=>$('localFolder').click();$('localFiles').onchange=()=>stageLocalFiles($('localFiles'));$('localFolder').onchange=()=>stageLocalFiles($('localFolder'));$('attachFiles').onclick=attachFiles;$('startDownload').onclick=startDownload;$('clearChat').onclick=async()=>{if(!state.chatId)return;if(!confirm('Clear this conversation?'))return;cancelRun();await api('/api/chats/'+encodeURIComponent(state.chatId),{clear:true});state.contextCount=0;hint();showWelcome();$('chatTitle').textContent='New conversation';await refreshChats()};document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>$(b.dataset.close).classList.remove('show'));document.querySelectorAll('.modal-backdrop').forEach(m=>m.onclick=e=>{if(e.target===m)m.classList.remove('show')});
$('menuBtn').onclick=()=>document.body.classList.toggle('nav-open');$('scrim').onclick=()=>document.body.classList.remove('nav-open');document.querySelector('.sidebar').addEventListener('click',e=>{if(e.target.closest('.chat-item,#newChat,#openFiles,#sidebarSettings'))document.body.classList.remove('nav-open')});$('themeBtn').onclick=()=>{const t=document.documentElement.dataset.theme==='light'?'dark':'light';document.documentElement.dataset.theme=t;try{localStorage.setItem('kurt-theme',t)}catch(e){}};
try{setScrollMode(localStorage.getItem('kurt-compact-scroll')==='1')}catch(e){setScrollMode(false)}
(async()=>{try{await refreshBootstrap();await refreshChats();showWelcome();pollDownload()}catch(e){toast(e.message)}})();
</script></body></html>'''


THINKING_KINDS = ("thinking", "reasoning", "thought")
THINK_OPEN_RE = re.compile(r"<think(?:ing)?>", re.I)
THINK_CLOSE_RE = re.compile(r"</think(?:ing)?>", re.I)
THINK_TAG_RE = re.compile(r"<(/)?think(?:ing)?>", re.I)
THINK_RE = re.compile(r"<think(?:ing)?>([\s\S]*?)(?:</think(?:ing)?>|\Z)", re.I)
ACTION_TAIL_RE = re.compile(r"@@@\s*(?:WRITE|EDIT)\b[\s\S]*\Z")
UNSUPPORTED_ACTION_BLOCK_RE = re.compile(
    r"@@@\s*(?:RUN|SEARCH|SYSTEM_PROMPT|READ|EXEC|BASH|SHELL)\b[\s\S]*?(?:@@@\s*END\b|\Z)", re.I
)
TOOL_CALL_BLOCK_RE = re.compile(
    r"<\|tool_call_start\|>[\s\S]*?(?:<\|tool_call_end\|>|\Z)", re.I
)


def strip_unsupported_actions(value):
    """Remove unsupported terminal/tool-call protocol text from model output."""
    text = UNSUPPORTED_ACTION_BLOCK_RE.sub("", str(value or ""))
    text = TOOL_CALL_BLOCK_RE.sub("", text)
    text = re.sub(r"<\|tool_call_(?:start|end)\|>", "", text, flags=re.I)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def split_thinking(raw):
    thoughts = []
    closer = THINK_CLOSE_RE.search(raw)
    opener = THINK_OPEN_RE.search(raw)
    if closer and (not opener or closer.start() < opener.start()):
        thoughts.append(raw[:closer.start()])
        raw = raw[closer.end():]
    thoughts.extend(THINK_RE.findall(raw))
    reply = THINK_RE.sub("", raw)
    return "\n\n".join(part.strip() for part in thoughts if part.strip()), reply.strip()


class _SearchHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self._capture = None
        self._text = []
        self._href = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set(str(attrs.get("class", "")).split())
        if tag == "a" and "result__a" in classes:
            self._capture = "title"
            self._href = str(attrs.get("href") or "")
            self._text = []
        elif "result__snippet" in classes:
            self._capture = "snippet"
            self._text = []

    def handle_data(self, data):
        if self._capture:
            self._text.append(data)

    def handle_endtag(self, tag):
        if self._capture == "title" and tag == "a":
            title = re.sub(r"\s+", " ", "".join(self._text)).strip()
            href = self._href
            parsed = urlparse(href)
            if "duckduckgo.com" in (parsed.hostname or ""):
                target = parse_qs(parsed.query).get("uddg", [""])[0]
                href = target or href
            if title and href.startswith(("http://", "https://")):
                self.results.append({"title": title, "url": href, "snippet": ""})
            self._capture = None
            self._text = []
        elif self._capture == "snippet" and tag in ("div", "a"):
            snippet = re.sub(r"\s+", " ", "".join(self._text)).strip()
            if snippet and self.results and not self.results[-1].get("snippet"):
                self.results[-1]["snippet"] = snippet
            self._capture = None
            self._text = []


def _web_search(query, limit=6):
    query = re.sub(r"\s+", " ", str(query or "")).strip()
    if not query or len(query) > 500:
        return {"ok": False, "results": [], "message": "Enter a focused web-search query (500 characters or fewer)."}
    url = "https://html.duckduckgo.com/html/?" + urlencode({"q": query})
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; KurtLocal/1.0)", "Accept": "text/html"},
    )
    try:
        with urllib.request.urlopen(request, timeout=18) as response:
            body = response.read(2_000_000).decode("utf-8", "replace")
        parser = _SearchHTMLParser()
        parser.feed(body)
        results = parser.results[:max(1, min(int(limit), 10))]
        if not results:
            return {"ok": False, "results": [], "message": "The search provider returned no parseable results."}
        return {"ok": True, "results": results, "message": "Found {} web result(s).".format(len(results))}
    except Exception as exc:
        return {"ok": False, "results": [], "message": "Web search failed: {}".format(exc)}


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _load_json(path, fallback):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, type(fallback)) else fallback
    except Exception:
        return fallback


class KurtWebApp:
    def __init__(self):
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        self.settings = _load_json(WEB_SETTINGS_FILE, {})
        self.settings.setdefault("system_prompt", "")
        self.settings.setdefault("project_root", str(Path.cwd().resolve()))
        self.settings.setdefault("model", K.MODEL)
        self.settings.setdefault("allow_project_writes", False)
        self.settings.setdefault("default_effort", "thinking")
        self.settings.setdefault("default_mode", "normal")
        configured_root = Path(self.settings.get("project_root") or Path.cwd()).expanduser()
        if not configured_root.exists() or not configured_root.is_dir():
            self.settings["project_root"] = str(Path.cwd().resolve())
            self.save_settings()
        self.chats = _load_json(CHATS_FILE, [])
        self.download_job = None
        self.download_thread = None

    def save_settings(self):
        _atomic_json(WEB_SETTINGS_FILE, self.settings)

    def save_chats(self):
        self.chats = sorted(
            self.chats,
            key=lambda item: (bool(item.get("pinned")), item.get("updated", 0)),
            reverse=True,
        )[:MAX_CHATS]
        _atomic_json(CHATS_FILE, self.chats)

    def workspace(self):
        root = Path(self.settings.get("project_root") or Path.cwd()).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise ValueError("The configured workspace directory does not exist.")
        return root

    def resolve_workspace_path(self, relative, allow_missing=True):
        root = self.workspace()
        raw = str(relative or "").strip()
        if not raw or Path(raw).is_absolute():
            raise ValueError("Use a non-empty path relative to the workspace.")
        candidate = (root / raw).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            raise ValueError("That path is outside the workspace.")
        if not allow_missing and not candidate.is_file():
            raise ValueError("File not found in the workspace.")
        return candidate

    def list_models(self):
        models = []
        try:
            for item in K.installed_models() if K.ollama_up() else []:
                models.append({
                    "ref": item["name"], "name": item["name"], "label": item["name"],
                    "provider": "Ollama", "type": "local", "size": item.get("size"),
                    "current": item["name"] == K.MODEL,
                })
        except Exception:
            pass
        for item in K.REMOTE_MODELS:
            ref = K.remote_model_ref(item)
            models.append({
                "ref": ref, "name": item.get("model"), "label": item.get("label") or item.get("model"),
                "provider": K.provider_label(item.get("provider")), "type": "remote",
                "current": ref == K.MODEL,
            })
        models.sort(key=lambda item: (not item["current"], item["provider"].lower(), item["label"].lower()))
        if K.MODEL and not any(item["ref"] == K.MODEL for item in models):
            remote = K.get_remote_model(K.MODEL)
            if remote:
                models.insert(0, {"ref": K.MODEL, "name": remote.get("model"), "label": remote.get("label") or remote.get("model"), "provider": K.provider_label(remote.get("provider")), "type": "remote", "current": True})
        return models

    def set_model(self, model):
        known = any(item["ref"] == model for item in self.list_models())
        if not known:
            # Allow a model name already installed by Ollama even if the tags refresh raced.
            known = bool(K.get_remote_model(model)) or any(K.same_model(item["name"], model) for item in K.installed_models())
        if not known:
            raise ValueError("Select a configured remote model or an installed local model first.")
        K.MODEL = K.SAVED_MODEL = model
        K.save_config()
        self.settings["model"] = model
        self.save_settings()

    def sanitized_keys(self):
        return [{
            "id": item.get("id"), "provider": item.get("provider"),
            "group": item.get("group") or "default", "label": item.get("label") or "Account",
            "masked": K.mask_key(item.get("value")),
        } for item in K.PROVIDER_KEYS]

    def editable_system_prompt(self):
        root = self.workspace()
        custom = str(self.settings.get("system_prompt") or "").strip()
        return custom or (K.SYSTEM.replace("{cwd}", str(root)) + WEB_INSTRUCTIONS)

    def chat_workspace(self, chat_id, create=True):
        chat_id = str(chat_id or '')
        if not re.fullmatch(r'[0-9a-fA-F]{32}', chat_id):
            raise ValueError('Invalid conversation workspace ID.')
        base = (CONFIG_DIR / 'web_workspaces').resolve()
        root = (base / chat_id).resolve()
        root.relative_to(base)
        if create:
            root.mkdir(parents=True, exist_ok=True)
            try:
                root.chmod(0o700)
            except OSError:
                pass
        return root

    @staticmethod
    def _safe_context_relative(value, allow_root=False):
        raw = str(value or '').replace('\\', '/').strip()
        if not raw and allow_root:
            return ''
        if not raw or raw.startswith('/') or re.match(r'^[A-Za-z]:', raw):
            raise ValueError('Context paths must be relative.')
        parts = [part for part in raw.split('/') if part not in ('', '.')]
        if not parts or any(part == '..' for part in parts):
            raise ValueError('Context path contains an invalid directory traversal.')
        if any(part.lower() in {'.git', '.kurt', '.kurt_inputs', '__pycache__', 'node_modules', '.venv', 'venv', '.ssh', '.aws', '.azure', '.gnupg', '.kube'} for part in parts):
            raise ValueError('This system or dependency directory is not imported into the chat workspace.')
        return '/'.join(parts)

    @staticmethod
    def _skip_context_name(name):
        low = str(name).lower()
        protected = {
            '.npmrc', '.pypirc', 'id_rsa', 'id_ed25519', 'credentials.json',
            'service-account.json', 'serviceaccount.json', 'secrets.json',
            'auth.json', 'token.json',
        }
        return low == '.env' or low.startswith('.env.') or low in protected or low.endswith(('.pem', '.p12', '.pfx', '.key')) or 'credentials' in low

    @staticmethod
    def _skip_context_dir(name):
        ignored = {str(item).lower() for item in K.IGNORE_DIRS} | {
            '.git', '.venv', 'venv', '__pycache__', 'node_modules', '.kurt',
            '.kurt_inputs', '.kurt_imports', '.ssh', '.aws', '.azure',
            '.gnupg', '.kube', 'dist', 'build',
        }
        return str(name).lower() in ignored

    def resolve_chat_path(self, root, relative):
        rel = self._safe_context_relative(relative)
        base = Path(root).resolve()
        candidate = (base / rel).resolve()
        candidate.relative_to(base)
        return candidate

    def _extract_zip_bytes(self, payload, destination):
        destination = Path(destination).resolve()
        total = 0
        count = 0
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for info in archive.infolist():
                raw = info.filename.replace('\\', '/')
                if not raw or raw.endswith('/'):
                    continue
                rel = self._safe_context_relative(raw)
                if self._skip_context_name(Path(rel).name):
                    continue
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    continue
                if info.file_size > 10 * 1024 * 1024:
                    raise ValueError('An archive contains a file larger than 10 MB.')
                total += info.file_size
                count += 1
                if total > 60 * 1024 * 1024 or count > 3000:
                    raise ValueError('The extracted archive is too large or contains too many files.')
                target = (destination / rel).resolve()
                target.relative_to(destination)
                target.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                try:
                    with archive.open(info) as source, target.open('wb') as output:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > 10 * 1024 * 1024 or total - info.file_size + written > 60 * 1024 * 1024:
                                raise ValueError('The extracted archive expands beyond the allowed size limits.')
                            output.write(chunk)
                    if written != info.file_size:
                        raise ValueError('An archive entry size did not match its ZIP metadata.')
                except Exception:
                    try:
                        target.unlink()
                    except OSError:
                        pass
                    raise
        return count, total

    def _zip_directory_into_workspace(self, source, relative_prefix, workspace):
        source = Path(source).resolve()
        workspace = Path(workspace).resolve()
        archive_dir = workspace / '.kurt_imports'
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / (re.sub(r'[^A-Za-z0-9._-]+', '_', Path(relative_prefix).name or source.name)[:60] + '-' + uuid.uuid4().hex[:8] + '.zip')
        buffer = io.BytesIO()
        file_count = 0
        total = 0
        with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for current, dirs, names in os.walk(source, followlinks=False):
                dirs[:] = [d for d in dirs if not self._skip_context_dir(d) and not (Path(current) / d).is_symlink()]
                for name in sorted(names):
                    path = Path(current) / name
                    if path.is_symlink() or self._skip_context_name(name):
                        continue
                    size = path.stat().st_size
                    if size > 10 * 1024 * 1024:
                        continue
                    total += size
                    if total > MAX_IMPORT_BYTES:
                        raise ValueError('A selected folder exceeds the 20 MB import limit.')
                    local = path.relative_to(source).as_posix()
                    arcname = '/'.join(part for part in (relative_prefix.strip('/'), local) if part)
                    archive.write(path, arcname)
                    file_count += 1
                    if file_count > 3000:
                        raise ValueError('A selected folder contains too many files (maximum 3,000).')
        payload = buffer.getvalue()
        archive_path.write_bytes(payload)
        self._extract_zip_bytes(payload, workspace)
        return file_count, total, archive_path

    def import_context(self, chat_id, selected_files=None, selected_folders=None, uploads=None):
        workspace = self.chat_workspace(chat_id)
        source_root = self.workspace()
        imported = 0
        total_bytes = 0
        archives = 0
        selected_folders = selected_folders if isinstance(selected_folders, list) else []
        selected_files = selected_files if isinstance(selected_files, list) else []
        folders = []
        for value in selected_folders:
            rel = self._safe_context_relative(value, allow_root=True)
            folder = source_root if not rel else (source_root / rel).resolve()
            folder.relative_to(source_root)
            if not folder.is_dir():
                raise ValueError('A selected project folder no longer exists: ' + (rel or source_root.name))
            folders.append((rel, folder))
        folders.sort(key=lambda pair: len(pair[0].split('/')) if pair[0] else 0)
        filtered_folders = []
        for rel, folder in folders:
            if any(not rel or rel == parent or rel.startswith(parent + '/') for parent, _ in filtered_folders):
                continue
            filtered_folders.append((rel, folder))
        for rel, folder in filtered_folders:
            prefix = rel or source_root.name
            count, size, _archive = self._zip_directory_into_workspace(folder, prefix, workspace)
            imported += count
            total_bytes += size
            if total_bytes > MAX_IMPORT_BYTES:
                raise ValueError('The total context import exceeds 20 MB.')
            archives += 1
        for value in selected_files:
            rel = self._safe_context_relative(value)
            if any(not parent or rel == parent or rel.startswith(parent + '/') for parent, _ in filtered_folders):
                continue
            source = (source_root / rel).resolve()
            source.relative_to(source_root)
            if not source.is_file() or source.is_symlink() or self._skip_context_name(source.name):
                continue
            size = source.stat().st_size
            if size > 10 * 1024 * 1024:
                raise ValueError('A selected file exceeds the 10 MB per-file import limit: ' + rel)
            total_bytes += size
            if total_bytes > MAX_IMPORT_BYTES:
                raise ValueError('The total context import exceeds 20 MB.')
            target = self.resolve_chat_path(workspace, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            imported += 1
            if target.suffix.lower() == '.zip':
                self._extract_zip_bytes(target.read_bytes(), workspace)
                archives += 1
        if total_bytes > MAX_IMPORT_BYTES:
            raise ValueError('The total context import exceeds 20 MB.')
        uploads = uploads if isinstance(uploads, list) else []
        upload_groups = {}
        direct_uploads = []
        for item in uploads:
            if not isinstance(item, dict):
                continue
            rel = self._safe_context_relative(item.get('path'))
            if self._skip_context_name(Path(rel).name):
                continue
            try:
                content = base64.b64decode(str(item.get('content') or ''), validate=True)
            except Exception:
                raise ValueError('A local upload is not valid base64 data.')
            if len(content) > 10 * 1024 * 1024:
                raise ValueError('A local upload exceeds the 10 MB per-file limit: ' + rel)
            total_bytes += len(content)
            if total_bytes > MAX_IMPORT_BYTES:
                raise ValueError('The total context import exceeds 20 MB.')
            if Path(rel).suffix.lower() == '.zip':
                archive_dir = workspace / '.kurt_imports'
                archive_dir.mkdir(parents=True, exist_ok=True)
                archive_path = archive_dir / (Path(rel).stem[:48] + '-' + uuid.uuid4().hex[:8] + '.zip')
                archive_path.write_bytes(content)
                self._extract_zip_bytes(content, workspace)
                archives += 1
                imported += 1
            elif '/' in rel:
                top = rel.split('/', 1)[0]
                upload_groups.setdefault(top, []).append((rel, content))
            else:
                direct_uploads.append((rel, content))
        for rel, content in direct_uploads:
            target = self.resolve_chat_path(workspace, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            imported += 1
        for folder_name, entries in upload_groups.items():
            archive_dir = workspace / '.kurt_imports'
            archive_dir.mkdir(parents=True, exist_ok=True)
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for rel, content in entries:
                    archive.writestr(rel, content)
            payload = buffer.getvalue()
            archive_name = re.sub(r'[^A-Za-z0-9._-]+', '_', folder_name)[:48] or 'folder'
            (archive_dir / (archive_name + '-' + uuid.uuid4().hex[:8] + '.zip')).write_bytes(payload)
            count, _size = self._extract_zip_bytes(payload, workspace)
            imported += count
            archives += 1
        return {'ok': True, 'files': imported, 'bytes': total_bytes, 'archives': archives,
                'workspace': str(workspace), 'message': f'Added {imported} item(s) to this chat workspace; {archives} folder/archive ZIP(s) created and unpacked.'}

    def context_contents(self, chat_id):
        root = self.chat_workspace(chat_id)
        chunks = []
        used = 0
        included = 0
        skipped = 0
        for current, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if not self._skip_context_dir(d))
            for name in sorted(files):
                path = Path(current) / name
                if self._skip_context_name(name) or path.is_symlink() or '.kurt_imports' in path.parts:
                    continue
                try:
                    size = path.stat().st_size
                    if size > 250_000:
                        skipped += 1
                        continue
                    raw = path.read_bytes()
                    if b'\x00' in raw[:4096]:
                        skipped += 1
                        continue
                    content = raw.decode('utf-8')
                except (OSError, UnicodeError):
                    skipped += 1
                    continue
                rel = path.relative_to(root).as_posix()
                block = f'\n--- FILE: {rel} ---\n{content}\n'
                if used + len(block) > MAX_WORKSPACE_CONTEXT:
                    skipped += 1
                    continue
                chunks.append(block)
                used += len(block)
                included += 1
        intro = 'Text file contents from this conversation’s isolated temporary workspace.'
        if skipped:
            intro += f'\n{skipped} file(s) were omitted from inline context because they were binary, unreadable, or over the per-file/context limit; they remain in the workspace ZIP.'
        return intro + ('\n' + ''.join(chunks) if chunks else '\n(No readable text files attached yet.)')

    def chat_system_prompt(self, chat_id, mode="normal", effort="thinking"):
        root = self.chat_workspace(chat_id)
        project = self.workspace()
        custom = str(self.settings.get("system_prompt") or "").strip()
        base = custom or K.SYSTEM.replace("{cwd}", str(root))
        if "{cwd}" in base:
            base = base.replace("{cwd}", str(root))
        if "WEB INTERFACE FILE OUTPUT RULES:" not in base and "KURT WEB WORKSPACE RULES:" not in base:
            base += WEB_INSTRUCTIONS
        if "KURT WEB WORKSPACE RULES:" not in base:
            base += "\n" + WEB_INSTRUCTIONS
        mode_rules = CODE_MODE_INSTRUCTIONS if mode == "code" else NORMAL_MODE_INSTRUCTIONS
        effort_rules = EFFORT_INSTRUCTIONS.get(effort, EFFORT_INSTRUCTIONS["thinking"])
        return (
            base + "\n" + KURT_IDENTITY_INSTRUCTIONS + "\n" + mode_rules + "\n" + effort_rules
            + "\nIsolated temporary workspace directory: " + str(root)
            + "\nOriginal project directory (read-only unless the user explicitly authorizes apply actions): " + str(project)
            + "\nFiles in isolated workspace:\n" + self.tree(root)
        )

    def run_command(self, chat_id, command):
        workspace = self.chat_workspace(chat_id)
        env_root = (CONFIG_DIR / "web_envs" / str(chat_id)).resolve()
        env_root.relative_to((CONFIG_DIR / "web_envs").resolve())
        return run_workspace_command(workspace, env_root, command)

    def search_web(self, query):
        return _web_search(query)

    @staticmethod
    def tree(root, limit=220):
        rows = []
        for current, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if not KurtWebApp._skip_context_dir(d))
            for name in sorted(files):
                if KurtWebApp._skip_context_name(name) or name == '.DS_Store':
                    continue
                rows.append(str((Path(current) / name).relative_to(root)).replace('\\', '/'))
                if len(rows) >= limit:
                    rows.append('... (truncated)')
                    return '\n'.join(rows)
        return '\n'.join(rows) or '(empty workspace)'

    def artifacts_from_answer(self, answer, chat_id):
        artifacts = []
        errors = []
        root = self.chat_workspace(chat_id)
        project_root = self.workspace()
        for kind, arg, body in K.BLOCK_RE.findall(answer):
            if kind not in ("WRITE", "EDIT"):
                continue
            filename = arg.strip()
            try:
                path = self.resolve_chat_path(root, filename)
                if path.is_dir():
                    errors.append("{}: target is a directory.".format(filename))
                    continue
                existed_in_temp = path.is_file()
                content = body
                if kind == "EDIT":
                    if not existed_in_temp:
                        errors.append("{}: EDIT target is not present in the temporary workspace; read or import it first.".format(filename))
                        continue
                    old, _crlf = K.read_file(path)
                    pairs = K.PAIR_RE.findall(body)
                    if not pairs:
                        errors.append("{}: EDIT block contains no valid SEARCH/REPLACE pair.".format(filename))
                        continue
                    content = old
                    valid = True
                    for search, replacement in pairs:
                        start, finish, how, fix = K.find_span(content, search)
                        if start is None:
                            valid = False
                            break
                        if how not in ("exact", "trimmed"):
                            replacement = K.reindent(replacement, fix)
                        content = content[:start] + replacement + content[finish:]
                    if not valid:
                        errors.append("{}: the exact SEARCH text could not be found.".format(filename))
                        continue
                content = content if content.endswith("\n") else content + "\n"
                validation = validate_content(filename, content)
                if not validation.get("ok", False):
                    errors.append("{}: {}".format(filename, validation.get("message") or "syntax validation failed"))
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                project_path = (project_root / filename).resolve()
                project_path.relative_to(project_root)
                artifacts.append({
                    "path": filename.replace("\\", "/"), "content": content,
                    "validation": validation, "exists": project_path.exists(),
                    "temp_exists": existed_in_temp, "source": kind,
                })
            except Exception as exc:
                errors.append("{}: {}".format(filename or "(unnamed file)", exc))

        for match in SYSTEM_PROMPT_ACTION_RE.finditer(answer):
            proposed = match.group(1).strip()
            if not proposed:
                errors.append("Kurt system prompt: proposed prompt is empty.")
                continue
            if len(proposed) > 40000:
                errors.append("Kurt system prompt: replacement exceeds 40,000 characters.")
                continue
            artifacts.append({
                "kind": "system_prompt", "path": "Kurt system prompt", "content": proposed,
                "validation": {"ok": True, "checked": True, "message": "Ready to review · not saved yet"},
            })
        return artifacts, errors

    def store_chat_input(self, chat_id, content, turn_number):
        root = self.chat_workspace(chat_id)
        folder = root / '.kurt_inputs'
        folder.mkdir(parents=True, exist_ok=True)
        try:
            folder.chmod(0o700)
        except OSError:
            pass
        filename = 'request-{:04d}.txt'.format(max(1, int(turn_number)))
        path = folder / filename
        path.write_text(str(content or ''), encoding='utf-8')
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def context_file_count(self, chat_id):
        root = self.chat_workspace(chat_id)
        count = 0
        for current, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not self._skip_context_dir(d)]
            count += sum(1 for name in files if not self._skip_context_name(name) and name != '.DS_Store')
        return count

    def reset_chat_workspace(self, chat_id):
        root = self.chat_workspace(chat_id)
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)

    def workspace_archive(self, chat_id):
        root = self.chat_workspace(chat_id)
        buffer = io.BytesIO()
        total = 0
        with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for current, dirs, names in os.walk(root, followlinks=False):
                dirs[:] = [d for d in dirs if not (Path(current) / d).is_symlink()]
                for name in sorted(names):
                    path = Path(current) / name
                    if path.is_symlink():
                        continue
                    total += path.stat().st_size
                    if total > 100 * 1024 * 1024:
                        raise ValueError('The temporary workspace is too large to export (100 MB maximum).')
                    archive.write(path, path.relative_to(root).as_posix())
        return buffer.getvalue()

    def create_chat(self):
        now = time.time()
        chat = {
            "id": uuid.uuid4().hex, "title": "New conversation", "model": K.MODEL,
            "effort": self.settings.get("default_effort", "thinking"),
            "mode": self.settings.get("default_mode", "normal"),
            "pinned": False, "created": now, "updated": now, "messages": [],
        }
        with self.lock:
            self.chats.insert(0, chat)
            self.save_chats()
        return chat

    def get_chat(self, chat_id):
        with self.lock:
            return next((chat for chat in self.chats if chat.get("id") == chat_id), None)

    def start_download(self, model):
        model = str(model or "").strip()
        if not model or len(model) > 200 or any(ch.isspace() for ch in model):
            raise ValueError("Enter an Ollama model ID, for example qwen2.5-coder:7b.")
        with self.lock:
            if self.download_job and self.download_job.get("state") in ("queued", "running"):
                raise ValueError("A model download is already running.")
            job = {"id": uuid.uuid4().hex, "model": model, "state": "queued", "completed": 0,
                   "total": 0, "status": "Queued", "human": "Starting", "error": "", "started": time.time()}
            self.download_job = job
        self.download_thread = threading.Thread(target=self._download_worker, args=(job,), daemon=True)
        self.download_thread.start()
        return dict(job)

    def _download_worker(self, job):
        try:
            if not K.ollama_up() and not K.ensure_ollama():
                raise RuntimeError("Ollama is not available. Install Ollama and start it before downloading models.")
            with self.lock:
                job["state"] = "running"
            request = urllib.request.Request(
                K.HOST + "/api/pull",
                data=json.dumps({"model": job["model"], "stream": True}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=1800) as response:
                for raw in response:
                    if not raw.strip():
                        continue
                    event = json.loads(raw.decode("utf-8", "replace"))
                    if event.get("error"):
                        raise RuntimeError(str(event["error"]))
                    total = int(event.get("total") or 0)
                    completed = int(event.get("completed") or 0)
                    with self.lock:
                        job["status"] = str(event.get("status") or "Downloading")
                        job["total"] = total
                        job["completed"] = completed
                        job["human"] = K.fmt_bytes(completed) + (" / " + K.fmt_bytes(total) if total else "")
            with self.lock:
                job["state"] = "done"
                job["status"] = "Complete"
                job["completed"] = job.get("total") or job.get("completed")
                job["human"] = "Model installed"
                job["finished"] = time.time()
        except Exception as exc:
            with self.lock:
                job["state"] = "error"
                job["error"] = str(exc)
                job["finished"] = time.time()


class Handler(BaseHTTPRequestHandler):
    server_version = "KurtLocal/0.5"

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        return

    def _host_ok(self):
        host = self.headers.get("Host", "").split(":", 1)[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1")

    def _authorized(self):
        if not self._host_ok():
            return False
        origin = self.headers.get("Origin")
        if origin:
            try:
                origin_host = urlparse(origin).hostname
            except Exception:
                return False
            if origin_host not in ("127.0.0.1", "localhost", "::1"):
                return False
        supplied = self.headers.get("X-Kurt-Token", "")
        return hmac.compare_digest(supplied, self.app.token)

    def _json(self, value, status=200):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid request size.")
        if length < 0 or length > MAX_BODY:
            raise ValueError("Request is too large (maximum 32 MB).")
        data = self.rfile.read(length) if length else b"{}"
        value = json.loads(data.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON request body must be an object.")
        return value

    def _need_auth(self):
        if not self._authorized():
            self._json({"error": "Local session authorization failed. Reload the Kurt page."}, 403)
            return False
        return True

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            if not self._host_ok():
                self.send_error(403)
                return
            data = HTML_PAGE.replace("__TOKEN__", self.app.token).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
            return
        if not self._need_auth():
            return
        try:
            if parsed.path == "/api/bootstrap":
                providers = [{"id": key, "label": value.get("label", key)} for key, value in K.PROVIDER_SPECS.items()]
                self._json({
                    "models": self.app.list_models(), "providers": providers,
                    "keys": self.app.sanitized_keys(), "remote_models": K.REMOTE_MODELS,
                    "settings": {"model": K.MODEL, "system_prompt": self.app.editable_system_prompt(), "allow_project_writes": bool(self.app.settings.get("allow_project_writes", False))},
                    "project_root": str(self.app.workspace()),
                })
            elif parsed.path == "/api/chats":
                with self.app.lock:
                    chats = [{
                        "id": c["id"], "title": c.get("title", "New conversation"),
                        "updated": c.get("updated", 0), "pinned": bool(c.get("pinned", False)),
                    } for c in self.app.chats]
                self._json({"chats": chats})
            elif parsed.path.startswith("/api/chats/"):
                chat_id = unquote(parsed.path.rsplit("/", 1)[-1])
                chat = self.app.get_chat(chat_id)
                if not chat:
                    self._json({"error": "Conversation not found."}, 404)
                else:
                    self._json({
                        "id": chat["id"], "title": chat.get("title"), "model": chat.get("model"),
                        "effort": chat.get("effort", "thinking"), "mode": chat.get("mode", "normal"),
                        "pinned": bool(chat.get("pinned", False)), "messages": chat.get("messages", []),
                    })
            elif parsed.path == "/api/download/status":
                with self.app.lock:
                    self._json({"job": dict(self.app.download_job) if self.app.download_job else None})
            elif parsed.path == "/api/context/status":
                args = parse_qs(parsed.query)
                chat_id = str(args.get("chat_id", [""])[0])
                if not self.app.get_chat(chat_id):
                    self._json({"error": "Conversation not found."}, 404)
                    return
                self._json({"files": self.app.context_file_count(chat_id)})
            elif parsed.path == "/api/workspace/export":
                args = parse_qs(parsed.query)
                chat_id = str(args.get("chat_id", [""])[0])
                if not self.app.get_chat(chat_id):
                    self._json({"error": "Conversation not found."}, 404)
                    return
                payload = self.app.workspace_archive(chat_id)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", 'attachment; filename="kurt-workspace-' + chat_id[:8] + '.zip"')
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
            elif parsed.path == "/api/files":
                args = parse_qs(parsed.query)
                relative = args.get("path", [""])[0]
                root = self.app.workspace()
                folder = (root / relative).resolve()
                folder.relative_to(root)
                if not folder.is_dir():
                    raise ValueError("Folder not found in the workspace.")
                items = []
                for item in sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                    if item.name.startswith(".") or item.name in K.IGNORE_DIRS:
                        continue
                    resolved = item.resolve()
                    try:
                        rel = str(resolved.relative_to(root)).replace("\\", "/")
                    except ValueError:
                        continue
                    items.append({"name": item.name, "path": rel, "directory": item.is_dir(), "size": K.fmt_bytes(item.stat().st_size) if item.is_file() else ""})
                parent = None if folder == root else str(folder.parent.relative_to(root)).replace("\\", "/")
                self._json({"path": str(folder.relative_to(root)).replace("\\", "/") if folder != root else "", "parent": parent, "items": items})
            else:
                self._json({"error": "Not found."}, 404)
        except Exception as exc:
            self._json({"error": str(exc)}, 400)

    def do_POST(self):
        if not self._need_auth():
            return
        parsed = urlparse(self.path)
        try:
            data = self._body()
            if parsed.path == "/api/settings":
                self._settings(data)
            elif parsed.path == "/api/chats":
                chat = self.app.create_chat()
                self._json({"id": chat["id"]})
            elif parsed.path.startswith("/api/chats/"):
                chat_id = unquote(parsed.path.rsplit("/", 1)[-1])
                chat = self.app.get_chat(chat_id)
                if not chat:
                    self._json({"error": "Conversation not found."}, 404)
                    return
                if data.get("delete"):
                    with self.app.lock:
                        self.app.chats = [item for item in self.app.chats if item.get("id") != chat_id]
                        self.app.save_chats()
                    workspace = self.app.chat_workspace(chat_id, create=False)
                    shutil.rmtree(workspace, ignore_errors=True)
                    env_dir = CONFIG_DIR / "web_envs" / chat_id
                    shutil.rmtree(env_dir, ignore_errors=True)
                    self._json({"ok": True})
                elif "rename" in data:
                    title = re.sub(r"\s+", " ", str(data.get("rename") or "")).strip()
                    if not title or len(title) > 100:
                        raise ValueError("Chat titles must contain 1–100 characters.")
                    chat["title"] = title
                    chat["updated"] = time.time()
                    self.app.save_chats()
                    self._json({"ok": True, "title": title})
                elif "pin" in data:
                    chat["pinned"] = bool(data.get("pin"))
                    chat["updated"] = time.time()
                    self.app.save_chats()
                    self._json({"ok": True, "pinned": chat["pinned"]})
                elif data.get("clear"):
                    chat["messages"] = []
                    chat["title"] = "New conversation"
                    chat["updated"] = time.time()
                    self.app.reset_chat_workspace(chat_id)
                    self.app.save_chats()
                    self._json({"ok": True})
                else:
                    self._json({"error": "Unsupported operation."}, 400)
            elif parsed.path == "/api/context/import":
                result = self.app.import_context(data.get("chat_id"), data.get("selected_files"), data.get("selected_folders"), data.get("uploads"))
                self._json(result)
            elif parsed.path == "/api/file/read":
                path = self.app.resolve_workspace_path(data.get("path"), allow_missing=False)
                if path.stat().st_size > 1_000_000:
                    raise ValueError("File is too large to attach (maximum 1 MB).")
                content = path.read_text(encoding="utf-8", errors="replace")
                self._json({"path": str(path.relative_to(self.app.workspace())), "content": content})
            elif parsed.path == "/api/file/write":
                self._write_file(data)
            elif parsed.path == "/api/download":
                job = self.app.start_download(data.get("model"))
                self._json({"job": job}, 202)
            elif parsed.path == "/api/chat":
                self._chat(data)
            else:
                self._json({"error": "Not found."}, 404)
        except Exception as exc:
            try:
                self._json({"error": str(exc)}, 400)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _settings(self, data):
        if "model" in data and not data.get("model_action"):
            self.app.set_model(str(data.get("model") or ""))
        if "project_root" in data:
            root = Path(str(data.get("project_root") or "")).expanduser().resolve()
            if not root.exists() or not root.is_dir():
                raise ValueError("Choose an existing directory for the workspace.")
            self.app.settings["project_root"] = str(root)
        if "system_prompt" in data:
            prompt = str(data.get("system_prompt") or "")
            if len(prompt) > 40000:
                raise ValueError("System prompt must be 40,000 characters or fewer.")
            self.app.settings["system_prompt"] = prompt
        if "allow_project_writes" in data:
            self.app.settings["allow_project_writes"] = bool(data.get("allow_project_writes"))
        if "default_effort" in data:
            value = str(data.get("default_effort") or "thinking")
            if value not in EFFORT_INSTRUCTIONS:
                raise ValueError("Choose Flash, Thinking, or Expert effort.")
            self.app.settings["default_effort"] = value
        if "default_mode" in data:
            value = str(data.get("default_mode") or "normal")
            if value not in ("normal", "code"):
                raise ValueError("Choose Normal or Code mode.")
            self.app.settings["default_mode"] = value
        key_action = data.get("key_action")
        if key_action == "add":
            provider = str(data.get("provider", "")).lower()
            group = str(data.get("group") or "default").strip()
            value = str(data.get("value") or "").strip()
            if provider not in K.PROVIDER_SPECS:
                raise ValueError("Unknown provider.")
            if not group or len(group) > 50 or len(value) < 10 or any(ch.isspace() for ch in value):
                raise ValueError("Enter a valid key, and a pool name of 1–50 characters.")
            if any(item.get("value") == value for item in K.PROVIDER_KEYS):
                raise ValueError("That API key is already saved.")
            K.PROVIDER_KEYS.append({"id": uuid.uuid4().hex[:12], "provider": provider, "group": group,
                                    "label": str(data.get("label") or "Account")[:80], "value": value})
        elif key_action == "edit":
            key_id = data.get("id")
            item = next((entry for entry in K.PROVIDER_KEYS if entry.get("id") == key_id), None)
            if item is None:
                raise ValueError("API key not found.")
            group = str(data.get("group") or item.get("group") or "default").strip()
            value = str(data.get("value") or "").strip()
            if not group or len(group) > 50:
                raise ValueError("Key pool must be 1–50 characters.")
            if value and (len(value) < 10 or any(ch.isspace() for ch in value)):
                raise ValueError("That API key does not look valid.")
            if value and any(entry.get("value") == value and entry.get("id") != key_id for entry in K.PROVIDER_KEYS):
                raise ValueError("That API key is already saved.")
            item["label"] = str(data.get("label") or item.get("label") or "Account")[:80]
            item["group"] = group
            if value:
                item["value"] = value
        elif key_action == "remove":
            key_id = data.get("id")
            K.PROVIDER_KEYS[:] = [item for item in K.PROVIDER_KEYS if item.get("id") != key_id]
        elif key_action == "rename_group":
            provider = str(data.get("provider") or '').lower()
            old_group = str(data.get("old_group") or 'default').strip()
            new_group = str(data.get("new_group") or '').strip()
            if provider not in K.PROVIDER_SPECS or not old_group or not new_group or len(new_group) > 50:
                raise ValueError('Enter a valid provider and group name (1–50 characters).')
            matched = [item for item in K.PROVIDER_KEYS if str(item.get('provider', '')).lower() == provider and str(item.get('group') or 'default') == old_group]
            if not matched:
                raise ValueError('No API keys exist in that group.')
            for item in matched:
                item['group'] = new_group
            for model in K.REMOTE_MODELS:
                if str(model.get('provider', '')).lower() == provider and str(model.get('key_group') or 'default') == old_group:
                    model['key_group'] = new_group
        model_action = data.get("model_action")
        if model_action == "add":
            provider = str(data.get("provider", "")).lower()
            model_id = str(data.get("model") or "").strip()
            if provider not in K.PROVIDER_SPECS or not model_id:
                raise ValueError("Choose a provider and enter its exact API model ID.")
            group = str(data.get("key_group") or "default").strip() or "default"
            base_url = str(data.get("base_url") or K.default_base_url(provider)).strip().rstrip("/")
            if not base_url.startswith(("https://", "http://")):
                raise ValueError("The API base URL must start with http:// or https://.")
            K.REMOTE_MODELS.append({"id": uuid.uuid4().hex[:12], "provider": provider, "model": model_id,
                                    "label": str(data.get("label") or model_id)[:100],
                                    "key_group": group, "base_url": base_url})
        elif model_action == "edit":
            model_id = data.get("id")
            item = next((entry for entry in K.REMOTE_MODELS if entry.get("id") == model_id), None)
            if item is None:
                raise ValueError("Remote model not found.")
            model_name = str(data.get("model") or item.get("model") or "").strip()
            label = str(data.get("label") or model_name).strip()
            group = str(data.get("key_group") or item.get("key_group") or "default").strip()
            base_url = str(data.get("base_url") or K.default_base_url(item.get("provider"))).strip().rstrip("/")
            if not model_name or not group or not base_url.startswith(("https://", "http://")):
                raise ValueError("Enter a model ID, key pool, and valid HTTP(S) endpoint.")
            item.update({"model": model_name, "label": label[:100], "key_group": group, "base_url": base_url})
        elif model_action == "remove":
            model_id = data.get("id")
            K.REMOTE_MODELS[:] = [item for item in K.REMOTE_MODELS if item.get("id") != model_id]
            if K.MODEL.startswith("remote:") and not K.get_remote_model(K.MODEL):
                K.MODEL = K.SAVED_MODEL = K.DEFAULT_MODEL
                self.app.settings["model"] = K.MODEL
        if key_action or model_action:
            K.save_config()
        self.app.settings["model"] = K.MODEL
        self.app.save_settings()
        self._json({"ok": True, "model": K.MODEL, "project_root": str(self.app.workspace()),
                    "settings": {"model": K.MODEL, "system_prompt": self.app.editable_system_prompt(), "allow_project_writes": bool(self.app.settings.get("allow_project_writes", False))}})

    def _write_file(self, data):
        if not self.app.settings.get('allow_project_writes') or data.get('authorized') is not True:
            raise ValueError('Project writes are disabled. Enable them in Settings and confirm the individual Apply to project action.')
        relative = str(data.get("path") or "")
        content = str(data.get("content") or "")
        path = self.app.resolve_workspace_path(relative)
        if path.exists() and not data.get("overwrite"):
            raise ValueError("The file already exists. Confirm overwrite in the browser first.")
        if not validate_content(relative, content).get("ok", False):
            raise ValueError("The file did not pass Kurt's validation checks.")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Re-resolve after creating parent directories to prevent symlink escapes.
        path = self.app.resolve_workspace_path(relative)
        fd, tmp_name = tempfile.mkstemp(prefix=".kurt-", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
            if path.exists():
                shutil.copymode(path, tmp_name)
            os.replace(tmp_name, path)
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        self._json({"ok": True, "message": "Wrote " + relative})

    def _chat(self, data):
        chat_id = str(data.get("chat_id") or "")
        user_text = str(data.get("text") or "").strip()
        model = str(data.get("model") or K.MODEL)
        effort = str(data.get("effort") or "thinking").lower()
        mode = str(data.get("mode") or "normal").lower()
        if not user_text:
            self._json({"error": "Message is empty."}, 400)
            return
        if len(user_text) > 100000:
            self._json({"error": "Message is too long."}, 400)
            return
        if effort not in EFFORT_INSTRUCTIONS:
            self._json({"error": "Choose Flash, Thinking, or Expert effort."}, 400)
            return
        if mode not in ("normal", "code"):
            self._json({"error": "Choose Normal or Code mode."}, 400)
            return
        chat = self.app.get_chat(chat_id)
        if not chat:
            chat = self.app.create_chat()
            chat_id = chat["id"]
        try:
            self.app.set_model(model)
        except Exception as exc:
            self._json({"error": str(exc)}, 400)
            return

        previous = chat.get("messages", [])[-MAX_MESSAGES_PER_CHAT:]
        self.app.chat_workspace(chat_id)
        self.app.store_chat_input(chat_id, user_text, len(chat.get("messages", [])) + 1)
        messages = [
            {"role": "system", "content": self.app.chat_system_prompt(chat_id, mode=mode, effort=effort)},
            {"role": "system", "content": self.app.context_contents(chat_id)},
        ]
        messages.extend(
            {"role": item["role"], "content": item["content"]}
            for item in previous if item.get("role") in ("user", "assistant")
        )
        messages.append({"role": "user", "content": user_text})

        chat["messages"].append({"role": "user", "content": user_text})
        chat["updated"] = time.time()
        if chat.get("title") in (None, "New conversation"):
            chat["title"] = re.sub(r"\s+", " ", user_text)[:58] or "New conversation"
        chat["model"] = model
        chat["effort"] = effort
        chat["mode"] = mode
        self.app.save_chats()

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def emit(event):
            payload = json.dumps(event, ensure_ascii=False).encode("utf-8")
            self.wfile.write(b"data: " + payload + b"\n\n")
            self.wfile.flush()

        worklog = []
        artifacts_by_path = {}
        finished = False
        disconnected = False
        temperature = {"flash": 0.25, "thinking": 0.2, "expert": 0.15}[effort]
        max_tokens = {"flash": 4096, "thinking": 8192, "expert": 16384}[effort]
        use_thinking = effort in ("thinking", "expert")
        reasoning_effort = {"flash": "none", "thinking": "medium", "expert": "high"}[effort]

        def log_step(text, ok=None, status="Working…", detail=None):
            item = {"text": str(text), "ok": ok, "detail": str(detail or "")[:12000]}
            worklog.append(item)
            if len(worklog) > 60:
                del worklog[:-60]
            emit({"type": "phase", "text": item["text"], "ok": ok, "detail": item["detail"], "status": status})

        def generate_response():
            chunks = []
            stream = K.stream_chat(
                messages,
                model=model,
                think=use_thinking,
                temperature=temperature,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
            )
            try:
                for kind, chunk in stream:
                    if not chunk:
                        continue
                    if kind in THINKING_KINDS:
                        continue
                    if kind == "content":
                        chunks.append(chunk)
            finally:
                close = getattr(stream, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
            return "".join(chunks).strip()

        def search_result_text(query, result):
            if not result.get("ok"):
                return "Web search for {!r} did not complete: {}".format(query, result.get("message", "unknown error"))
            lines = ["Web search results for: " + query]
            for index, item in enumerate(result.get("results", []), 1):
                lines.append("{}. {}\nURL: {}\nSnippet: {}".format(
                    index, item.get("title", "Untitled"), item.get("url", ""), item.get("snippet", "")
                ))
            return "\n\n".join(lines)

        try:
            log_step("Starting {} mode with {} effort.".format(mode.title(), effort.title()), status="Preparing…", detail="Operating mode: {}. Effort: {}. The selected mode determines whether developer commands are permitted; the effort setting controls supported provider reasoning parameters. No file has been modified at this stage.".format(mode.title(), effort.title()))
            final_raw = ""
            validation_errors = []
            max_rounds = 4
            for round_index in range(max_rounds):
                if round_index == 0 and SYSTEM_PROMPT_REQUEST_RE.fullmatch(user_text):
                    active_prompt = self.app.chat_system_prompt(chat_id, mode=mode, effort=effort)
                    for key_entry in K.PROVIDER_KEYS:
                        secret = str(key_entry.get("value") or "")
                        if len(secret) >= 8:
                            active_prompt = active_prompt.replace(secret, "[REDACTED API KEY]")
                    raw = (
                        "The exact Kurt-controlled system prompt for this chat is displayed in the expanded, read-only prompt card below. "
                        "Provider-owned internal instructions are not available to Kurt."
                    )
                    artifacts_by_path["Active Kurt system prompt"] = {
                        "path": "Active Kurt system prompt",
                        "kind": "system_prompt",
                        "content": active_prompt,
                        "preview_only": True,
                        "validation": {"ok": True, "checked": True, "message": "Active prompt · read-only preview"},
                    }
                    log_step(
                        "Retrieved the exact active Kurt system prompt.", True,
                        status="Prompt ready…",
                        detail="The prompt below is the first system message assembled by Kurt for this chat, including the selected mode and effort rules. API-key values are redacted. Conversation history and attached-file context are separate messages and are not included.",
                    )
                    final_raw = raw
                    break
                log_step(
                    "Reviewing the request and available workspace files." if round_index == 0 else "Continuing with tool results and verification feedback.",
                    status="Thinking…" if use_thinking else "Working…",
                    detail=("Kurt is matching the request against the selected conversation history and readable workspace files. No edits or terminal commands have been accepted yet." if round_index == 0 else "Kurt is incorporating the actual results from the previous action round and deciding whether another targeted action or a final response is needed."),
                )
                raw = generate_response()
                if not raw:
                    final_raw = "Kurt did not return any response text. Check the selected model and provider configuration."
                    break

                tool_actions = []
                for match in RUN_ACTION_RE.finditer(raw):
                    tool_actions.append((match.start(), "RUN", match.group(1).strip() or match.group(2).strip()))
                for match in SEARCH_ACTION_RE.finditer(raw):
                    tool_actions.append((match.start(), "SEARCH", match.group(1).strip() or match.group(2).strip()))
                tool_actions.sort(key=lambda item: item[0])

                if tool_actions:
                    if round_index >= max_rounds - 1:
                        validation_errors.append("Kurt reached the maximum of {} tool rounds.".format(max_rounds))
                    else:
                        results = []
                        for _position, action, argument in tool_actions:
                            if action == "RUN":
                                if mode != "code":
                                    result = {"ok": False, "returncode": -1, "output": "Terminal commands are disabled in Normal mode. Switch to Code mode to run permitted developer checks."}
                                elif not argument:
                                    result = {"ok": False, "returncode": -1, "output": "RUN action did not include a command."}
                                else:
                                    log_step("Running command: " + argument, status="Running checks…", detail="The command is being run in this chat’s isolated workspace using its dedicated Python virtual environment. It is not run against the original project directory.")
                                    result = self.app.run_command(chat_id, argument)
                                label = "PASSED" if result.get("ok") else "FAILED"
                                command_output = str(result.get("output", ""))
                                log_step("{}: {}".format(label, argument or "(empty command)"), result.get("ok"), status="Reviewing results…", detail="Exit code: {}\n\n{}".format(result.get("returncode"), command_output[:9000] or "(The command produced no output.)"))
                                results.append("[Terminal result]\nCommand: {}\nExit code: {}\n{}".format(
                                    argument, result.get("returncode"), command_output
                                ))
                            else:
                                log_step("Searching the web: " + argument, status="Searching the web…", detail="Search query: " + argument)
                                result = self.app.search_web(argument)
                                search_details = search_result_text(argument, result)
                                log_step(result.get("message", "Web search completed."), result.get("ok"), status="Reviewing sources…", detail=search_details[:10000])
                                results.append("[Web search result]\n" + search_details)
                        messages.append({"role": "assistant", "content": raw})
                        messages.append({
                            "role": "user",
                            "content": "\n\n".join(results) + "\n\nUse these actual results. Continue the task; do not repeat completed actions.",
                        })
                        continue

                first_edit = re.search(r"@@@\s*(?:WRITE|EDIT|SYSTEM_PROMPT)\b", raw, re.I)
                if first_edit:
                    preamble = raw[:first_edit.start()]
                    if preamble.strip():
                        log_step("Reviewed Kurt’s proposed Change summary.", True, status="Reviewing changes…", detail=preamble.strip()[:10000])
                    has_summary = re.search(r"(?i)change\s+summary|planned\s+changes|changes\s+to\s+make", preamble)
                    if not has_summary and round_index < max_rounds - 1:
                        log_step("Requesting the required file-by-file change summary before accepting edits.", False, status="Preparing change summary…", detail="Kurt proposed file actions without first explaining each file and its intended changes. The application has not accepted or written those changes; it is asking Kurt to provide the required summary and repeat the complete action blocks.")
                        messages.append({"role": "assistant", "content": raw})
                        messages.append({
                            "role": "user",
                            "content": "Your response contains WRITE/EDIT actions but no clear file-by-file Change summary before them. Provide a detailed Change summary describing exactly what each file will change and why, then reproduce the complete action blocks. Do not omit or shorten file contents.",
                        })
                        continue
                    if not has_summary:
                        validation_errors.append("File changes were withheld because Kurt did not provide the required change summary.")
                        final_raw = "Kurt proposed file changes but did not provide the required file-by-file Change summary. No files from that response were accepted."
                        break

                log_step("Checking generated file syntax before returning artifacts.", status="Validating files…", detail="Each proposed file is checked with the validator appropriate to its file type where an available parser or compiler exists. Failed checks are sent back to Kurt for repair before the artifact is returned.")
                new_artifacts, validation_errors = self.app.artifacts_from_answer(raw, chat_id)
                for artifact in new_artifacts:
                    artifacts_by_path[artifact["path"]] = artifact
                    validation = artifact.get("validation") or {}
                    if artifact.get("kind") == "system_prompt":
                        log_step("Prepared a Kurt system-prompt proposal for review; it has not been saved.", True, status="Ready for review…", detail="The full replacement prompt is displayed in a review card. It will only become active after the user explicitly selects Apply prompt to Kurt.")
                    elif validation.get("checked"):
                        log_step("Syntax validation passed: " + artifact["path"], True, status="Preparing response…", detail=validation.get("message", "The file passed the configured syntax checks."))
                    else:
                        log_step("Basic file validation passed: " + artifact["path"], True, status="Preparing response…", detail=validation.get("message", "No language-specific compiler was available; basic parsing/structure checks completed where supported."))
                if validation_errors:
                    for error in validation_errors:
                        target = error.split(":", 1)[0].strip()
                        artifacts_by_path.pop(target, None)
                if validation_errors and round_index < max_rounds - 1:
                    details = "\n".join("- " + item for item in validation_errors)
                    log_step("A generated file did not pass validation. Asking Kurt to repair it.", False, status="Repairing files…", detail=details[:10000])
                    messages.append({"role": "assistant", "content": raw})
                    messages.append({
                        "role": "user",
                        "content": "The application rejected these file changes during syntax/structure validation:\n" + details + "\nFix the reported problems, provide a concise Change summary, and return complete corrected file actions. Do not claim checks passed unless they do.",
                    })
                    continue
                final_raw = raw
                break

            if validation_errors:
                for error in validation_errors:
                    log_step("Validation warning: " + error, False, status="Finishing with warnings…", detail="This artifact did not pass the configured check and is withheld from the accepted artifact list.")

            artifacts = list(artifacts_by_path.values())
            _inline_thinking, reply = split_thinking(final_raw)
            reply = strip_unsupported_actions(reply)
            reply = re.sub(r"\n{3,}", "\n\n", reply).strip()
            if validation_errors:
                warning = "\n\n**Validation warning:** " + "; ".join(validation_errors)
                reply = (reply + warning).strip()
            visible = K.BLOCK_RE.sub("", reply).strip()
            if not visible and not artifacts:
                visible = "Kurt completed the request without a text response or validated file artifact. Check the work log for tool and validation results."
            if visible or artifacts:
                record = {
                    "role": "assistant", "content": visible, "artifacts": artifacts,
                    "worklog": worklog, "effort": effort, "mode": mode,
                }
                chat["messages"].append(record)
                chat["updated"] = time.time()
                chat["model"] = model
                chat["effort"] = effort
                chat["mode"] = mode
                self.app.save_chats()

            emit({"type": "token", "text": visible})
            finished = True
            emit({
                "type": "done", "content": visible, "artifacts": artifacts,
                "worklog": worklog, "chat_id": chat_id, "title": chat["title"],
                "model": model, "effort": effort, "mode": mode,
            })
        except (BrokenPipeError, ConnectionResetError):
            disconnected = True
            return
        except Exception as exc:
            try:
                emit({"type": "error", "message": str(exc)})
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        finally:
            if not disconnected:
                try:
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass


def launch_web():
    app = KurtWebApp()
    try:
        if not K.get_remote_model(K.MODEL) and shutil.which("ollama"):
            K.ensure_ollama()
        K.get_hardware()
        K.retune()
    except Exception:
        pass
    try:
        server = ThreadingHTTPServer(("127.0.0.1", WEB_PORT), Handler)
    except OSError as exc:
        if WEB_PORT >= 1024:
            raise RuntimeError("Kurt could not bind to localhost:{} (the port may already be in use): {}".format(WEB_PORT, exc)) from exc
        fallback_port = int(os.environ.get("KURT_WEB_FALLBACK_PORT", "10220"))
        if not 1024 <= fallback_port <= 65535 or fallback_port == WEB_PORT:
            raise RuntimeError("KURT_WEB_FALLBACK_PORT must be an unused port from 1024 to 65535.") from exc
        try:
            server = ThreadingHTTPServer(("127.0.0.1", fallback_port), Handler)
        except OSError as fallback_error:
            raise RuntimeError(
                "Requested port {} is unavailable ({}), and fallback port {} is unavailable: {}".format(
                    WEB_PORT, exc, fallback_port, fallback_error
                )
            ) from fallback_error
        print(
            "Note: could not use localhost:{} ({}); using localhost:{} instead. "
            "Set KURT_WEB_PORT to another available port (1024+) to choose a different address.".format(
                WEB_PORT, exc, fallback_port
            )
        )
    server.daemon_threads = True
    server.app = app
    url = "http://127.0.0.1:{}".format(server.server_address[1])
    print("\nKurt Local Workspace")
    print("Open: {}".format(url))
    print("Workspace: {}".format(app.workspace()))
    print("Press Ctrl+C here to stop Kurt.\n")
    threading.Thread(target=lambda: (time.sleep(0.4), webbrowser.open(url)), daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        print("\nStopping Kurt localhost server…")
    finally:
        server.shutdown()
        server.server_close()


__all__ = ["launch_web"]