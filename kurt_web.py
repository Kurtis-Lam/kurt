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
import mimetypes
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
KURT WEB INTERFACE RULES:
- You are Kurt, the local assistant in this application. Speak as Kurt.
- Normal mode has no access to local files, attachments, directories, terminal commands, or
  other computer resources. In Normal mode, do not request or emit local-access actions.
- Code mode can read and list files only beneath the configured project directory, subject to
  the server's protected-path exclusions. Uploaded attachments are available only in Code mode.
- Before proposing a file or prompt change, provide a concise Change summary that names the
  target and explains what will change and why.
- WRITE and EDIT blocks create review cards only. They never save a file by themselves. A
  user-confirmed Download saves a copy; a separately confirmed Apply to project action writes
  to the configured project directory when project writes are enabled in Settings.
- RUN blocks create command-review cards only. They never execute until the user approves
  the specific command in the interface. The command runner is restricted to the isolated
  conversation workspace and its allowlisted developer checks; it is not an unrestricted shell.
- READ and LIST are read-only and may address only paths inside the configured project root.
  Never attempt path traversal, symbolic-link access, or access to protected/system directories.
- SEARCH may request external web research when needed. Do not invent search results.
- SYSTEM_PROMPT creates a review card. It is not saved until the user confirms applying it.
- Uploaded ZIP archives and folders are unpacked into the conversation workspace in Code mode.
  Treat extracted paths as their uploaded relative paths. Do not claim to inspect binary images
  unless the selected model has vision input or an available image-reading capability.
- Never claim a file was read, a command ran, or a change was saved unless the application
  returned the actual result. Never imply access beyond the configured project directory.
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

RESPONSE_FORMAT_INSTRUCTIONS = r"""
RESPONSE FORMAT:
- Begin every substantive reply with one detailed, user-facing rationale block, then the answer:
  @@@ SUMMARY
  Explain the request interpretation, relevant constraints and assumptions, the approach,
  important decisions or trade-offs, checks actually performed, and remaining limitations.
  Use clearly labelled lines or bullets; give enough detail that the user can understand and
  audit the response without having to infer why a choice was made. Include all relevant
  decision factors and verification evidence, but do not pad the explanation with irrelevant
  steps. Simple greetings and trivial answers may use a shorter block.
  This is a public explanation, not private chain-of-thought. Never include hidden analysis,
  internal scratchpad content, speculative internal monologue, or token-by-token reasoning.
  @@@ END
- Keep the actual answer after the rationale block. Never output <think> tags.
- Whenever you provide source code, commands, configuration, or a multi-line code example,
  put it in a fenced Markdown code block with an appropriate language label (for example,
  ```python). Do not put multi-line code in ordinary paragraphs or inline code. The interface
  renders fenced code as a copyable code card with a Copy code button.
- Keep normal prose, the rationale, and code clearly separated. Never claim an action happened
  unless the application reports that it actually happened.
"""

EFFORT_INSTRUCTIONS = {
    "thinking": "THINKING EFFORT: request medium reasoning where the provider supports effort controls. In the public SUMMARY block, give a substantive explanation of the request, relevant constraints, approach, key decisions, actual checks, and limitations. Do not reduce a substantive task to a generic one-line placeholder. Never reveal hidden chain-of-thought or private scratchpad content.",
    "expert": "EXPERT EFFORT: request high/deep reasoning where the provider supports effort controls and perform careful verification. In the public SUMMARY block, give a thorough, auditable explanation of the request, assumptions, constraints, alternatives and trade-offs, chosen approach, actual actions, verification evidence, and remaining uncertainty. Never reveal hidden chain-of-thought or private scratchpad content.",
}

NORMAL_MODE_INSTRUCTIONS = r"""
NORMAL MODE — NO LOCAL FILE OR COMPUTER ACCESS:
- You cannot inspect, list, search, read, create, edit, delete, move, upload, download, or otherwise access local files or folders.
- You cannot access the configured project directory, chat workspace, attachments, local models' files, or the user's computer through tools or action blocks.
- Do not request, emit, or simulate READ, LIST, RUN, WRITE, EDIT, or SYSTEM_PROMPT actions. The server blocks these actions as well.
- You may answer general questions and request external web research with SEARCH.
- When the user asks you to inspect or change local files, run commands, or work on their project, explicitly tell them to switch this conversation to Code mode. Explain that Code mode can read and list files under the configured project directory and chat workspace, while changes and commands require separate approval. Do not pretend to have inspected anything.
- If the user asks to see their live desktop, screen, or GUI, explain that switching to Code mode enables project-file access but does not provide live screen capture or remote GUI control; ask them to attach a screenshot for visual inspection.
"""

CODE_MODE_INSTRUCTIONS = r"""
CODE MODE — PROJECT FILE ACCESS WITH USER APPROVAL:
- You may read and list files inside the configured Code mode project directory using:
  @@@ LIST relative/path
  @@@ END
  @@@ READ relative/path.ext
  @@@ END
  Use LIST with an empty path to inspect the configured project directory root.
- The read/list tools are read-only. The server restricts them to the configured project directory and excludes credential, secret, system, and dependency locations. Never imply that you can access paths outside that configured root.
- You may inspect attached files and the isolated conversation workspace. Uploaded attachments are copied into that workspace by the user's explicit upload action.
- Propose complete file replacements using WRITE or EDIT. These create review cards only; they do not write to the project or workspace. The user must explicitly download a proposal or approve the confirmed Apply to project action before a project file changes.
- You may propose a developer command using one RUN action block. It is never executed automatically. The user must review and approve each command in the interface first. Approved commands run only through the restricted per-chat developer runner in the isolated chat workspace, not with unrestricted shell access to the computer.
- Never claim a read, command, or write succeeded unless the application returned its actual result. Run targeted checks only after the user approves the command.
- Syntax validation is not proof of end-to-end correctness. State when no test harness exists and describe any remaining uncertainty.
"""

RUN_ACTION_RE = re.compile(r"@@@\s*RUN\s*([^\n]*)\n(.*?)\n?@@@\s*END", re.I | re.S)
SEARCH_ACTION_RE = re.compile(r"@@@\s*SEARCH\s*([^\n]*)\n?(.*?)\n?@@@\s*END", re.I | re.S)
READ_ACTION_RE = re.compile(r"@@@\s*READ\s*([^\n]*)\n?(.*?)\n?@@@\s*END", re.I | re.S)
LIST_ACTION_RE = re.compile(r"@@@\s*LIST\s*([^\n]*)\n?(.*?)\n?@@@\s*END", re.I | re.S)
SUMMARY_ACTION_RE = re.compile(r"@@@\s*SUMMARY\s*\n(.*?)\n?@@@\s*END", re.I | re.S)
SYSTEM_PROMPT_ACTION_RE = re.compile(r"@@@\s*SYSTEM_PROMPT\s*\n(.*?)\n?@@@\s*END", re.I | re.S)
WEB_ACTION_RE = re.compile(
    r"@@@\s*(?:RUN|SEARCH|READ|LIST|SYSTEM_PROMPT)\b[^\n]*\n[\s\S]*?@@@\s*END", re.I
)
SYSTEM_PROMPT_REQUEST_RE = re.compile(
    r"^\s*(?:/system-prompt|(?:what\s+is|what's)\s+(?:your\s+|its\s+|kurt(?:'s)?\s+|the\s+)?system\s+prompt|(?:show|share|reveal|print|display|quote|give)\s+(?:me\s+)?(?:your\s+|its\s+|kurt(?:'s)?\s+|the\s+)?system\s+prompt)\s*[?!.\s]*$",
    re.I,
)

NORMAL_MODE_LOCAL_ACCESS_RE = re.compile(
    r"(?:\b(?:see|view|look\s+at|access|inspect|read|browse|list|open|find|search|check|modify|edit|write|delete|create|run|execute)\b.{0,100}\b(?:my\s+)?(?:mac(?:book)?|computer|desktop|screen|files?|folders?|directories|project|codebase|repository|repo|terminal|drive|disk|local\s+(?:files?|folders?|machine))\b"
    r"|\b(?:my\s+)?(?:mac(?:book)?|computer|desktop|screen|local\s+(?:files?|folders?|machine)|project\s+(?:files?|folder|directory)|codebase|repository|repo)\b.{0,100}\b(?:access|inspect|read|browse|list|open|find|search|check|modify|edit|write|delete|create|run|execute)\b)",
    re.I,
)
DESKTOP_VISUAL_REQUEST_RE = re.compile(
    r"\b(?:see|view|look\s+at|show|watch|inspect)\b.{0,90}\b(?:desktop|screen|display|monitor|gui)\b|\b(?:desktop|screen|display|monitor|gui)\b.{0,90}\b(?:see|view|look\s+at|show|watch|inspect)\b",
    re.I,
)


HTML_PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kurt</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='-5 -6 30 28'%3E%3Crect x='-5' y='-6' width='30' height='28' rx='6' fill='%230b120e'/%3E%3Cpath fill='%233dd66e' d='M0 0h6v4h-6zM14 0h6v2h-6zM11 2h6v2h-6zM0 4h1v12h-1zM2 4h2v12h-2zM5 4h1v12h-1zM8 4h6v2h-6zM6 6h5v4h-5zM1 8h1v8h-1zM4 8h1v8h-1zM8 10h6v2h-6zM11 12h6v2h-6zM14 14h6v2h-6z'/%3E%3C/svg%3E">
<script>try{if(localStorage.getItem('kurt-side-collapsed')==='1')document.documentElement.classList.add('side-collapsed')}catch(e){}</script>
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
.chat-sort{max-width:112px}
.custom-select.chat-sort .custom-select-trigger{padding:3px 5px;border-color:transparent;background:transparent;color:var(--muted);font-size:11px}
.custom-select.chat-sort .custom-select-trigger:hover{border-color:var(--line2);color:var(--text)}
.chat-list{overflow:auto;min-height:80px;flex:1}
.chat-row{display:flex;align-items:center;gap:3px;margin:2px 0;padding:2px;border-radius:9px}
.chat-row:hover,.chat-row.active{background:var(--raise)}
.chat-item{flex:1;min-width:0;padding:6px 7px;border:0;background:transparent;text-align:left;cursor:pointer;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:13px}
.chat-item:hover,.chat-row.active .chat-item{color:var(--text);background:transparent}
.chat-item .pin-indicator{color:var(--green);margin-right:5px}
.chat-actions{display:flex;flex:none;opacity:0;transition:opacity .16s ease}
.chat-row:hover .chat-actions,.chat-row:focus-within .chat-actions,.chat-row.active .chat-actions{opacity:1}
.chat-more{width:30px;height:30px;padding:0;border:0;background:transparent;color:var(--muted);font-size:20px;line-height:1;border-radius:7px}
.chat-more:hover{color:var(--text);background:var(--panel)}
.chat-context-menu{position:fixed;z-index:45;display:none;min-width:174px;padding:5px;background:var(--panel);border:1px solid var(--line2);border-radius:10px;box-shadow:var(--shadow);transform-origin:top right}
.chat-context-menu.open{display:grid;animation:menuIn .14s ease both}
.chat-context-menu button{width:100%;display:flex;align-items:center;gap:9px;padding:8px 10px;border:0;background:transparent;text-align:left;color:var(--text);font-size:13px}
.chat-context-menu button:hover{background:var(--raise)}
.chat-context-menu button.danger{color:var(--danger)}
.chat-context-menu .menu-divider{height:1px;background:var(--line);margin:4px 2px}
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
.content .math-display{display:block;width:100%;margin:18px auto;text-align:center;font-weight:750;font-size:1.06em;line-height:1.8;overflow-x:auto;overflow-y:hidden;white-space:nowrap}
.content .math-display mjx-container[display="true"]{display:block!important;margin:0 auto!important;font-weight:750}
.content code:not(pre code){font:.86em var(--mono);background:var(--raise);border:1px solid var(--line);border-radius:5px;padding:1px 5px}
.assistant-heading{display:flex;align-items:center;gap:8px;margin:0 0 10px;color:var(--muted);font:600 13px var(--sans)}
.assistant-heading .mark{width:24px;height:19.2px;padding:2px;border-radius:6px;background:#0b120e;color:#3dd66e;box-sizing:content-box}
.assistant-status{font-weight:400;color:var(--muted);font-size:12px;margin-left:2px}
.message.live .assistant-heading .mark{animation:blink 1.1s steps(2,start) infinite}
.msg-foot{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin-top:6px;min-height:30px}
.copy-msg{width:30px;height:30px}
.copy-msg .ic{width:16px;height:16px}
.response-metrics{min-width:0;color:var(--muted);font-size:11.5px;line-height:1.5;overflow-wrap:anywhere}
.message.live .mark{animation:blink 1.1s steps(2,start) infinite}
.message.live .copy-msg{visibility:hidden}
@keyframes blink{50%{opacity:.35}}
.think{margin:0 0 8px;max-width:100%}
.think[hidden]{display:none}
.think-head{display:inline-flex;align-items:center;gap:7px;background:transparent;border:0;padding:4px 0;color:var(--muted);font-size:13px}
.think-head:hover{color:var(--text)}
.think-head .chev{width:15px;height:15px;transition:transform .24s cubic-bezier(.2,.75,.25,1);transform:rotate(-90deg)}
.think.open .chev{transform:rotate(0deg)}
.think-body{display:block;max-height:0;opacity:0;overflow:hidden;border-left:2px solid var(--line2);margin:0 0 0 5px;padding:0 0 0 14px;color:var(--muted);font-size:14px;line-height:1.65;white-space:pre-wrap;overflow-wrap:anywhere;transition:max-height .28s cubic-bezier(.2,.75,.25,1),opacity .2s ease,margin .2s ease,padding .2s ease}
.think.open .think-body{max-height:5000px;opacity:1;margin:6px 0 4px 5px;padding:2px 0 2px 14px;overflow:auto}
.think.live .think-label{background:linear-gradient(90deg,var(--muted) 30%,var(--text) 50%,var(--muted) 70%);background-size:200% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:shimmer 1.8s linear infinite}
.reasoning-panel .think-body{border-left-color:var(--green);color:var(--text);max-height:0}
.reasoning-panel.open .think-body{max-height:5000px}
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
.artifact-body{display:block;max-height:0;opacity:0;overflow:hidden;border-top:1px solid var(--line);transition:max-height .3s cubic-bezier(.2,.75,.25,1),opacity .2s ease}
.artifact.open .artifact-body{max-height:12000px;opacity:1}
.artifact-body .codebox{margin:0;border:0;border-radius:0}
.artifact-actions{display:flex;gap:7px;padding:10px 12px;border-top:1px solid var(--line);flex-wrap:wrap}
.artifact-actions button{font-size:13px}
.composer-wrap{position:relative;padding:6px 20px 14px;flex-shrink:0}
.composer{max-width:760px;margin:0 auto;border:1px solid var(--line2);border-radius:22px;background:var(--panel);padding:12px 10px 8px 18px;display:flex;flex-direction:column;gap:4px;box-shadow:0 2px 14px rgba(0,0,0,.16);transition:border-color .15s,box-shadow .15s}
.composer:focus-within{border-color:var(--green);box-shadow:0 0 0 3px var(--green-soft)}
.composer textarea{display:block;width:100%;resize:none;min-height:30px;max-height:220px;background:transparent;border:0;outline:none;padding:3px 0;font:15px/1.55 var(--sans)}
.composer textarea::placeholder{color:var(--muted)}
.compose-bottom{display:flex;align-items:center;gap:4px;margin-left:-8px;min-width:0}
.spacer{flex:1;min-width:8px}
.route-picker{position:relative;display:flex;align-items:center;gap:2px;flex:none;min-width:0}
.route-category{padding:6px 9px;border:1px solid transparent;background:transparent;color:var(--muted);font-size:12px;border-radius:8px;white-space:nowrap}
.route-category:hover,.route-category.active{background:var(--raise);border-color:var(--line2);color:var(--text)}
.route-flyout{position:fixed;z-index:70;display:grid;gap:3px;max-width:calc(100vw - 20px);max-height:min(360px,calc(100vh - 24px));overflow:auto;padding:6px;background:var(--panel);border:1px solid var(--line2);border-radius:11px;box-shadow:var(--shadow)}
.route-flyout[hidden]{display:none}
.route-flyout-item{width:100%;min-width:0;padding:8px 10px;border:0;border-radius:7px;background:transparent;color:var(--text);text-align:left;font-size:12.5px;white-space:normal;overflow-wrap:anywhere}
.route-flyout-item:hover,.route-flyout-item[aria-current=true]{background:var(--raise);color:var(--green)}
.route-flyout-empty{padding:10px;color:var(--muted);font-size:12px}
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
.modal-backdrop{position:fixed;inset:0;background:rgba(4,10,7,.6);z-index:30;display:flex;align-items:center;justify-content:center;padding:20px;opacity:0;visibility:hidden;pointer-events:none;transition:opacity .2s ease,visibility .2s ease}
.modal-backdrop.show{opacity:1;visibility:visible;pointer-events:auto}
.modal{width:min(850px,100%);max-height:88vh;overflow:auto;background:var(--panel);border:1px solid var(--line2);border-radius:14px;box-shadow:var(--shadow);transform:translateY(12px) scale(.985);transition:transform .24s cubic-bezier(.2,.75,.25,1)}
.modal-backdrop.show .modal{transform:translateY(0) scale(1)}
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
.settings-panel{display:block;max-width:760px;max-height:0;opacity:0;overflow:hidden;pointer-events:none;transform:translateY(5px);transition:max-height .32s cubic-bezier(.2,.75,.25,1),opacity .2s ease,transform .24s ease}
.settings-panel.active{max-height:12000px;opacity:1;pointer-events:auto;transform:translateY(0)}
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
.field input,.field select,.field textarea{background:var(--bg);border:1px solid var(--line2);border-radius:8px;padding:8px 10px;outline:none;width:100%;min-width:0;max-width:100%}
.field input{overflow:hidden;text-overflow:ellipsis}
.field textarea{min-height:145px;resize:vertical;font:12.5px/1.55 var(--mono)}
.field input:focus,.field select:focus,.field textarea:focus{border-color:var(--green)}
.section{border-top:1px solid var(--line);margin-top:18px;padding-top:16px}
.section h3{font-size:14px;margin:0 0 12px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.row>*{flex:1;min-width:100px}
.key-row,.model-row{display:flex;gap:10px;align-items:center;padding:9px 10px;border:1px solid var(--line);border-radius:10px;margin:6px 0}
.failover-member-row{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr) auto;gap:8px;align-items:center;padding:9px 10px;border:1px solid var(--line);border-radius:10px;margin:7px 0;background:var(--bg)}
.failover-member-row select{width:100%;min-width:0;max-width:100%;padding:8px 9px;border:1px solid var(--line2);border-radius:8px;background:var(--panel);color:var(--text);font-size:12.5px;text-overflow:ellipsis}
.failover-member-row .remove-member{width:32px;height:32px;padding:0}
@media(max-width:700px){.failover-member-row{grid-template-columns:minmax(0,1fr) minmax(0,1fr) auto}.route-picker{gap:0}.route-category{padding:4px 3px;font-size:10px}.custom-select.mode-pill{max-width:20vw;min-width:0}}
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
.artifact-pane{display:none;position:fixed;top:54px;right:0;bottom:0;z-index:18;width:calc((100vw - 268px)/2);min-width:0;max-width:none;min-height:0;height:auto;overflow-y:auto;overscroll-behavior:contain;padding:14px 18px 24px;background:var(--side);border-left:1px solid var(--line);box-shadow:-12px 0 32px rgba(0,0,0,.12)}
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
body.compact-scroll .artifact-pane{top:54px;bottom:0;height:auto;min-height:0;overflow-y:auto}
@media(max-width:900px){.main.has-artifacts .messages{padding-left:14px;padding-right:14px}.artifact-pane{padding-left:12px;padding-right:12px}.artifact-pane-head{margin-left:-12px;margin-right:-12px;padding-left:12px;padding-right:12px}}
@media(max-width:700px){
.main.has-artifacts{flex-direction:row}
.main.has-artifacts .chat-area{flex:1 1 100%;width:100%;max-width:100%}
.main.has-artifacts .artifact-pane{top:54px;right:0;bottom:0;left:0;width:100vw;max-width:none;min-height:0;max-height:none;border-left:0;border-top:0;padding:12px 14px 20px}
.artifact-pane .artifact-body .codebox pre{max-height:calc(100dvh - 220px)}
}

/* Fully styled dropdowns and in-app dialogs */
.custom-select{position:relative;display:inline-flex;min-width:0;vertical-align:middle}
.custom-select-trigger{display:flex;align-items:center;justify-content:space-between;gap:12px;width:100%;min-width:0;overflow:hidden;text-overflow:ellipsis;padding:7px 10px;border:1px solid var(--line2);border-radius:9px;background:var(--raise);color:var(--text);font-size:13px;text-align:left;white-space:nowrap}
.custom-select-trigger:after{content:"";width:7px;height:7px;flex:none;border-right:1.7px solid currentColor;border-bottom:1.7px solid currentColor;transform:rotate(45deg) translateY(-2px);transition:transform .18s ease}
.custom-select.open .custom-select-trigger:after{transform:rotate(225deg) translate(-1px,-1px)}
.custom-select-menu{position:absolute;top:calc(100% + 6px);left:0;right:0;z-index:42;display:none;max-height:min(340px,55vh);overflow:auto;padding:5px;background:var(--panel);border:1px solid var(--line2);border-radius:10px;box-shadow:var(--shadow);transform-origin:top center}
.custom-select.open .custom-select-menu{display:block;animation:menuIn .15s ease both}
.custom-select-option{display:block;width:100%;padding:8px 9px;border:0;border-radius:7px;background:transparent;color:var(--text);text-align:left;font-size:13px;white-space:normal}
.custom-select-option:hover,.custom-select-option[aria-selected=true]{background:var(--raise);color:var(--green)}
.custom-select-option:disabled{opacity:.4;cursor:not-allowed}
.custom-select-group{padding:7px 9px 3px;color:var(--muted);font-size:10.5px;font-weight:700;letter-spacing:.08em;text-transform:uppercase}
.custom-select-native{display:none!important}
.custom-select.model-pill{max-width:260px;min-width:0}
.custom-select.model-pill .custom-select-trigger{border:0;background:transparent;color:var(--muted);padding:6px 8px}
.custom-select.model-pill .custom-select-trigger:hover{background:var(--raise);color:var(--text)}
.custom-select.mode-pill{max-width:140px;min-width:86px;border:0!important;background:transparent!important;box-shadow:none!important}
.custom-select.mode-pill .custom-select-trigger{min-width:0;padding:5px 8px;border:1px solid var(--line)!important;background:transparent;color:var(--muted);font-size:12px;box-shadow:none!important}
.custom-select.chat-sort{max-width:112px;min-width:0}
.custom-select.chat-sort .custom-select-menu{min-width:120px;left:auto;right:0}
.field>.custom-select{width:100%}
.field>.custom-select .custom-select-trigger{background:var(--bg)}
@keyframes menuIn{from{opacity:0;transform:translateY(-4px) scale(.985)}to{opacity:1;transform:translateY(0) scale(1)}}
.dialog-backdrop{position:fixed;inset:0;z-index:60;display:flex;align-items:center;justify-content:center;padding:20px;background:rgba(4,10,7,.62);opacity:0;visibility:hidden;pointer-events:none;transition:opacity .18s ease,visibility .18s ease}
.dialog-backdrop.show{opacity:1;visibility:visible;pointer-events:auto}
.app-dialog{width:min(470px,100%);padding:20px;background:var(--panel);border:1px solid var(--line2);border-radius:14px;box-shadow:var(--shadow);transform:translateY(10px) scale(.985);transition:transform .22s cubic-bezier(.2,.75,.25,1)}
.dialog-backdrop.show .app-dialog{transform:translateY(0) scale(1)}
.app-dialog h2{margin:0 0 7px;font-size:17px}
.dialog-message{margin:0 0 15px;color:var(--muted);font-size:13px;white-space:pre-wrap}
.dialog-fields{display:grid;gap:11px}
.dialog-field{display:grid;gap:5px;color:var(--muted);font-size:12px}
.dialog-field input,.dialog-field textarea{width:100%;padding:9px 10px;border:1px solid var(--line2);border-radius:8px;background:var(--bg);color:var(--text);outline:none}
.dialog-field input:focus,.dialog-field textarea:focus{border-color:var(--green)}
.dialog-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:18px}
.dialog-actions .danger{background:var(--danger);border-color:var(--danger);color:#fff}
@media(max-width:700px){.custom-select.model-pill{max-width:42vw}.custom-select.mode-pill{max-width:22vw;min-width:0}.custom-select-trigger{font-size:12px}.chat-more{opacity:1}}
@media(prefers-reduced-motion:reduce){.custom-select-menu,.app-dialog,.modal,.modal-backdrop,.artifact-body,.think-body,.settings-panel{transition:none!important;animation:none!important}}


.modal-backdrop{display:flex;opacity:0;visibility:hidden;pointer-events:none;transition:opacity .2s ease,visibility .2s ease}
.modal-backdrop.show{display:flex;opacity:1;visibility:visible;pointer-events:auto}
.modal{transform:translateY(9px) scale(.99);transition:transform .22s cubic-bezier(.2,.75,.25,1)}
.modal-backdrop.show .modal{transform:translateY(0) scale(1)}
/* Smoothly animate custom menus in both directions. */
.custom-select-menu{display:block;visibility:hidden;opacity:0;pointer-events:none;max-height:0;overflow:hidden;transform:translateY(-4px) scale(.985);transition:max-height .18s ease,opacity .15s ease,transform .15s ease,visibility .15s}
.custom-select.open .custom-select-menu{display:block;visibility:visible;opacity:1;pointer-events:auto;max-height:min(340px,55vh);transform:translateY(0) scale(1);animation:none;overflow:auto}
.chat-context-menu{display:grid;visibility:hidden;opacity:0;pointer-events:none;transform:translateY(-4px);transition:opacity .14s ease,transform .14s ease,visibility .14s}
.chat-context-menu.open{display:grid;visibility:visible;opacity:1;pointer-events:auto;transform:translateY(0);animation:none}

/* Stable viewport: the conversation scrolls, while the composer stays at the bottom. */
html,body{height:100%;min-height:100%;overflow:hidden}
.app{height:100vh;height:100dvh;min-height:0}
.stage{display:flex;flex-direction:column;height:100vh;height:100dvh;min-height:0;overflow:hidden}
.topbar{flex:0 0 54px;min-height:54px}
.download-strip{flex:0 0 2px}
.main{flex:1 1 auto;height:calc(100dvh - 56px);min-height:0;overflow:hidden}
.chat-area{height:100%;min-height:0;overflow:hidden;flex:1 1 0%;display:flex;flex-direction:column}
.main .messages{flex:1 1 auto;min-height:0;overflow-x:hidden;overflow-y:auto;overscroll-behavior:contain}
.main.empty .messages{flex:1 1 auto;min-height:0;overflow-y:auto}
.composer-wrap{flex:0 0 auto;margin-top:auto;padding-bottom:max(10px,env(safe-area-inset-bottom))}
.main.has-artifacts .chat-area{height:100%;min-height:0}
.main.has-artifacts .messages{min-height:0;flex:1 1 auto;overflow-y:auto}
.artifact-pane{top:54px;bottom:0;height:auto}
.custom-select-menu{transform-origin:top center}
.compose-bottom .custom-select-menu{top:auto;bottom:calc(100% + 7px);transform-origin:bottom center}
.compose-bottom .custom-select{z-index:1}
.compose-bottom .custom-select.open{z-index:50}
.think-body,.work-log-body{display:block;max-height:0;overflow:hidden;opacity:0;transition:max-height .24s ease,opacity .2s ease,margin .2s ease}
.think.open .think-body,.work-log.open .work-log-body{max-height:420px;overflow:auto;opacity:1}
.work-log-body{padding-top:0;padding-bottom:0}
.work-log.open .work-log-body{padding-top:3px;padding-bottom:9px}
.work-log:not(.open) .work-log-body{display:block}
.reasoning-panel{padding:0 0 3px}
.reasoning-panel .think-body{max-height:0}
.reasoning-panel.open .think-body{max-height:min(72vh,1100px);overflow:auto;overscroll-behavior:contain}
.artifact-body{display:block;max-height:0;overflow:hidden;opacity:0;transition:max-height .28s ease,opacity .2s ease}
.artifact.open .artifact-body{max-height:3000px;opacity:1}
.artifact .artifact-body .codebox pre{max-height:48vh;overflow:auto}
.work-detail{max-height:0;overflow:hidden;transition:max-height .22s ease,opacity .18s ease;opacity:0}
.work-step[open] .work-detail{max-height:1200px;opacity:1}
.settings-panel{display:block;max-width:760px;max-height:0;overflow:hidden;opacity:0;transition:max-height .3s ease,opacity .2s ease}
.settings-panel.active{display:block;max-height:5000px;opacity:1}
.key-sort-row{margin:12px 0 6px;justify-content:flex-end}
.key-sort-row>span{flex:1}
.chat-actions{opacity:0}
.chat-row:hover .chat-actions,.chat-row:focus-within .chat-actions,.chat-row.active .chat-actions{opacity:1}
@media(max-width:700px){.main.has-artifacts .chat-area{height:100%;min-height:0}.artifact-pane{top:54px;bottom:0}.chat-actions{opacity:1}}
@media(prefers-reduced-motion:reduce){.think-body,.work-log-body,.artifact-body,.work-detail,.settings-panel{transition:none!important}}

/* Compact chat list and bulk selection. */
.side-heading{padding:8px 6px 3px;gap:5px}
.chat-heading-actions{display:flex;align-items:center;gap:3px;min-width:0}
.chat-sort{max-width:94px}
.chat-select-toggle{padding:3px 6px;border:0;background:transparent;color:var(--muted);font-size:11px;white-space:nowrap}
.chat-select-toggle:hover,.chat-select-toggle[aria-pressed=true]{background:var(--raise);color:var(--text);border-color:var(--line2)}
.chat-heading-actions{gap:1px}
.chat-context-menu{min-width:154px;padding:3px}
.chat-context-menu button{gap:7px;padding:6px 8px;font-size:12px}
.chat-context-menu .menu-divider{margin:2px 1px}
.chat-select-bar{display:flex;align-items:center;gap:5px;padding:3px 6px 5px;color:var(--muted);font-size:11px}
.chat-select-bar[hidden]{display:none}
.chat-select-bar label{display:flex;align-items:center;gap:4px;white-space:nowrap}
.chat-select-bar input,.chat-row-select{width:13px;height:13px;margin:0;accent-color:var(--green);flex:none}
.chat-select-bar span{flex:1;min-width:0;white-space:nowrap}
.chat-select-bar button{padding:3px 7px;font-size:11px}
.chat-list{min-height:60px}
.chat-row{margin:0;padding:1px 2px;gap:2px;border-radius:7px}
.chat-item{padding:4px 6px;font-size:12.5px;line-height:1.35}
.chat-more{width:26px;height:26px;font-size:18px}
.chat-row-select{margin:0 3px 0 4px}
.chat-row.selected{background:var(--green-soft);outline:1px solid var(--line2)}
.chat-row .chat-actions{gap:1px}

/* The composer shows the chosen model; categories and choices open as adjacent panes. */
.route-picker{min-width:0;max-width:min(260px,32vw)}
.route-current{display:flex;align-items:center;gap:8px;max-width:100%;min-width:0;padding:6px 9px;border:1px solid transparent;background:transparent;color:var(--muted);font-size:12px;border-radius:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.route-current:after{content:"";width:6px;height:6px;flex:none;border-right:1.5px solid currentColor;border-bottom:1.5px solid currentColor;transform:rotate(45deg) translateY(-2px)}
.route-current:hover,.route-current[aria-expanded=true]{background:var(--raise);border-color:var(--line2);color:var(--text)}
.route-flyout{display:grid;grid-template-columns:minmax(112px,.8fr) minmax(0,1.35fr);gap:5px;width:min(430px,calc(100vw - 20px));max-height:min(390px,calc(100vh - 24px));overflow:hidden;padding:6px}
.route-flyout-column{min-width:0;max-height:min(376px,calc(100vh - 38px));overflow-y:auto;overscroll-behavior:contain}
.route-flyout-categories{padding-right:4px;border-right:1px solid var(--line)}
.route-flyout-category{display:flex;align-items:center;justify-content:space-between;width:100%;padding:8px 9px;border:0;border-radius:7px;background:transparent;color:var(--muted);text-align:left;font-size:12.5px}
.route-flyout-category:after{content:"›";font-size:17px;line-height:1;color:var(--muted)}
.route-flyout-category:hover,.route-flyout-category.active{background:var(--raise);color:var(--text)}
.route-flyout-options{display:grid;align-content:start;gap:2px}
.route-flyout-item{padding:8px 9px;font-size:12px}
.route-flyout-empty{padding:9px;color:var(--muted);font-size:12px}

/* Let the settings content, including long group forms, scroll inside its own pane. */
.settings-modal{overscroll-behavior:contain}
.settings-panel-area{overflow-x:hidden;overflow-y:auto;overscroll-behavior:contain;scrollbar-gutter:stable;min-height:0}
.settings-panel.active{max-height:none;overflow:visible}
.settings-panel:not(.active){overflow:hidden}
#failoverMembers{display:grid;gap:7px;min-width:0}
.failover-member-row{margin:0;min-width:0;grid-template-columns:minmax(0,1.2fr) minmax(0,.95fr) auto}
.failover-member-row select{min-width:0;max-width:100%}
.local-model-row .model-actions{display:flex;gap:6px;flex:none}
.local-model-row .model-actions button{padding:6px 9px;font-size:12px}
.key-row{min-width:0}
.key-row .key-provider{color:var(--muted);font-size:11px}
@media(max-width:700px){
 .route-picker{max-width:30vw}
 .route-current{max-width:30vw;padding:5px 5px;font-size:10.5px}
 .route-flyout{width:min(360px,calc(100vw - 20px));grid-template-columns:minmax(100px,.8fr) minmax(0,1.3fr)}
 .chat-select-bar{gap:4px}
 .settings-panel-area{overflow-y:auto;min-height:0}
 .failover-member-row{grid-template-columns:minmax(0,1fr) minmax(0,.9fr) auto}
}
/* A little more breathing room between conversation turns and within long answers. */
.message{margin:0 auto 36px}
.message.assistant .assistant-heading{margin-bottom:13px}
.message.user .bubble{padding:11px 16px}
.content p{margin:0 0 17px}
.content li{margin:7px 0}
.content h1,.content h2,.content h3{margin-top:26px;margin-bottom:13px}
.content .math-display{display:block;width:100%;margin:20px auto 22px;text-align:center;white-space:normal;overflow-x:auto;overflow-y:hidden;font-weight:750;line-height:1.7}
.content .math-display strong{font-weight:750}
.content mjx-container[display="true"]{display:block!important;margin:0 auto!important;text-align:center!important;font-weight:700}
.user-content{font-family:var(--sans);font-size:14px;line-height:1.6;white-space:normal}
.message.user .bubble:has(.user-content){white-space:normal}

/* Keep the two fallback selectors inside their own grid tracks. */
#failoverMembers{width:100%;min-width:0}
.failover-member-row{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr) 36px;gap:9px;width:100%;min-width:0;overflow:visible}
.failover-member-row>.custom-select{display:block;width:100%;min-width:0;max-width:100%;overflow:visible}
.failover-member-row>.custom-select .custom-select-trigger{width:100%;min-width:0;max-width:100%;gap:6px;white-space:nowrap;text-overflow:ellipsis;overflow:hidden}
.failover-member-row>.custom-select .custom-select-menu{min-width:100%;max-width:min(340px,calc(100vw - 48px))}
.failover-member-row>.remove-member{width:36px;min-width:36px;padding:0;justify-self:end}
.failover-member-row select{width:100%;min-width:0;max-width:100%}

/* Uploaded files remain visible in the composer, with image previews where available. */
.composer-attachments{display:flex;flex-wrap:wrap;gap:8px;max-width:100%;padding:0 0 8px}
.composer-attachments[hidden]{display:none}
.attachment-chip{display:flex;align-items:center;gap:9px;min-width:0;max-width:min(100%,300px);padding:6px 9px;border:1px solid var(--line);border-radius:11px;background:var(--bg)}
.attachment-thumb,.attachment-file-icon{width:38px;height:38px;flex:none;border-radius:7px;object-fit:cover;background:var(--raise)}
.attachment-file-icon{display:grid;place-items:center;color:var(--green)}
.attachment-file-icon .ic{width:18px;height:18px}
.attachment-info{display:flex;flex:1;flex-direction:column;min-width:0;gap:2px}
.attachment-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;font-weight:650}
.attachment-meta{color:var(--muted);font-size:11px;line-height:1.35}
.attachment-status{color:var(--green);font-size:10.5px;line-height:1.3}
.attachment-remove{width:25px;height:25px;flex:none;padding:0;border:0;background:transparent;color:var(--muted);font-size:17px}
.attachment-remove:hover{color:var(--danger);background:var(--raise)}
.upload-list{display:grid;gap:7px;padding:8px 0;color:var(--muted);font-size:12px}
.upload-item{display:flex;align-items:center;gap:9px;min-width:0;padding:8px 9px;border:1px solid var(--line);border-radius:9px;background:var(--bg)}
.upload-item .attachment-thumb,.upload-item .attachment-file-icon{width:34px;height:34px}
.upload-item .attachment-name{font-size:12px}
.upload-item .attachment-meta{font-size:10.5px}
.upload-section-label{padding:4px 2px 0;color:var(--muted);font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase}
.upload-empty{padding:8px 2px;color:var(--muted)}
.route-flyout{box-sizing:border-box;min-width:0}
.route-flyout-column{min-width:0}
.route-flyout-category,.route-flyout-item{min-width:0;overflow-wrap:anywhere}
@media(max-width:700px){
 .message{margin-bottom:29px}
 .failover-member-row{grid-template-columns:minmax(0,1fr) minmax(0,1fr) 34px;gap:6px}
 .failover-member-row>.remove-member{width:34px;min-width:34px}
 .attachment-chip{max-width:100%}
}


.chat-list{padding:2px 0 14px;scrollbar-gutter:stable}
.chat-date-heading{padding:16px 10px 6px;color:var(--muted);font-size:11.5px;font-weight:600;line-height:1.2;user-select:none}
.chat-date-heading:first-child{padding-top:8px}
.chat-row{margin:1px 0;padding:0 2px 0 0;gap:2px;border-radius:8px;min-height:34px}
.chat-item{padding:8px 10px;font-size:13.5px;line-height:1.35;border-radius:8px}
.chat-more{width:28px;height:28px;font-size:18px}
.chat-row-select{margin:0 2px 0 8px}
.route-flyout{height:min(320px,calc(100vh - 24px));max-height:none;grid-template-rows:minmax(0,1fr);align-items:stretch;overflow:hidden}
.route-flyout-column{height:100%;max-height:none;min-height:0;overflow-y:auto;scrollbar-gutter:stable}
.route-flyout-categories{align-content:start}
.route-flyout-options{align-content:start}
.failover-member-row{grid-template-columns:minmax(0,1fr) 36px}
.reasoning-panel .think-body{border-left-color:var(--line2);color:var(--muted)}
.reasoning-panel .think-label{font-size:13px}
.custom-select.open .custom-select-menu{overflow-y:auto;max-height:var(--menu-max,min(300px,50vh));overscroll-behavior:contain;-webkit-overflow-scrolling:touch;scrollbar-width:thin}
.custom-select.drop-up .custom-select-menu{top:auto;bottom:calc(100% + 6px);transform-origin:bottom center}
.route-flyout-column,.key-checks,.group-members{overscroll-behavior:contain}
.assistant-heading .think-head{padding:0 0 0 2px;font-size:12.5px;font-weight:400;white-space:nowrap}
.assistant-heading .think-head[hidden]{display:none}
.think-head.open .chev{transform:rotate(0deg)}
.think-head.live .think-label{background:linear-gradient(90deg,var(--muted) 30%,var(--text) 50%,var(--muted) 70%);background-size:200% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:shimmer 1.8s linear infinite}
.assistant-heading .think-head.live~.assistant-status{display:none}
.reasoning-panel{margin:0}
.key-checks{display:grid;gap:6px;max-height:190px;overflow-y:auto;padding:6px;border:1px solid var(--line2);border-radius:9px;background:var(--panel)}
.key-check{display:flex;align-items:center;gap:9px;padding:7px 8px;border-radius:7px;cursor:pointer;font-size:13px}
.key-check:hover{background:var(--raise)}
.key-check.disabled{opacity:.45;cursor:not-allowed}
.key-check small{color:var(--muted)}
.drag-handle{display:inline-flex;align-items:center;justify-content:center;width:20px;color:var(--muted);cursor:grab;user-select:none;font-size:15px;letter-spacing:-3px}
.priority-badge{display:inline-flex;align-items:center;justify-content:center;padding:3px 8px;border-radius:999px;border:1px solid var(--line2);color:var(--muted);font-size:11px;font-weight:600;white-space:nowrap}
.priority-badge.first{color:var(--green);border-color:var(--green)}
#failoverMembers .failover-member-row{display:grid;grid-template-columns:20px 92px minmax(0,1fr) auto 36px;gap:8px;align-items:center}
.member-move{display:flex;flex-direction:column;gap:2px}
.member-move button{width:24px;height:16px;padding:0;font-size:9px;line-height:1}
.group-members{display:grid;gap:6px;margin-top:10px}
.group-member{display:flex;align-items:center;gap:8px;padding:7px 9px;border:1px solid var(--line);border-radius:9px;background:var(--panel);cursor:grab;font-size:12.5px;min-width:0}
.group-member .grow{flex:1;min-width:0;overflow-wrap:anywhere}
.failover-member-row.dragging,.group-member.dragging{opacity:.45}
@media(max-width:700px){#failoverMembers .failover-member-row{grid-template-columns:18px 78px minmax(0,1fr) auto 32px;gap:6px}}

/* UI refresh: motion system, Claude-style chat panel, polish. */
:root{--ease:cubic-bezier(.22,.8,.26,1);--ease-soft:cubic-bezier(.4,0,.2,1);--side-w:268px}
html.side-collapsed{--side-w:0px}
*{scrollbar-width:thin}
::selection{background:color-mix(in srgb,var(--green) 34%,transparent);color:var(--text)}
html.theme-fade *,html.theme-fade *::before,html.theme-fade *::after{transition:background-color .35s var(--ease-soft),border-color .35s var(--ease-soft),color .35s var(--ease-soft),fill .35s,stroke .35s!important}
button{transition:background-color .18s var(--ease-soft),border-color .18s var(--ease-soft),color .18s var(--ease-soft),filter .18s,transform .2s var(--ease),box-shadow .22s var(--ease-soft),opacity .18s}
button:not(:disabled):not(.to-bottom):active{transform:scale(.97)}
button.primary:not(:disabled):hover{box-shadow:0 6px 18px color-mix(in srgb,var(--green) 30%,transparent)}
.send{transition:background-color .18s,filter .18s,transform .22s var(--ease),box-shadow .22s}
.send:not(:disabled):hover{transform:translateY(-1px) scale(1.06);box-shadow:0 6px 16px color-mix(in srgb,var(--green) 32%,transparent)}
.icon-btn{transition:background-color .18s,color .18s,transform .2s var(--ease)}
.icon-btn:hover{transform:translateY(-1px)}
.settings-nav-btn,.custom-select-option,.route-flyout-item,.route-flyout-category,.key-check,.chat-context-menu button,.artifact-top,.nav-btn,.chip,.pill,.work-log-head,.think-head,.group-member,.chat-more{transition:background-color .18s var(--ease-soft),color .18s var(--ease-soft),border-color .18s var(--ease-soft),transform .22s var(--ease),box-shadow .2s}
.chat-context-menu button:hover{transform:translateX(2px)}
.route-flyout-item:hover,.route-flyout-category:hover{transform:translateX(2px)}
.menu-btn{transition:background-color .18s,color .18s,transform .3s var(--ease)}
.topbar{position:sticky;top:0;z-index:12;background:color-mix(in srgb,var(--bg) 84%,transparent);-webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px)}
.modal-backdrop,.dialog-backdrop{-webkit-backdrop-filter:blur(4px);backdrop-filter:blur(4px)}
.brand{transition:opacity .25s var(--ease-soft)}
.logo{transition:transform .35s var(--ease),border-color .2s}
.brand:hover .logo{transform:rotate(-6deg) scale(1.06);border-color:var(--green)}
.nav-btn .ico{transition:transform .35s var(--ease),background-color .2s,color .2s}
.nav-btn.new:hover .ico{transform:rotate(90deg) scale(1.08)}
.nav-btn:hover .ico{color:var(--text)}
.nav-btn.new:hover .ico{color:var(--green-ink)}
.nav-btn:not(.new):hover .ico{transform:rotate(18deg)}
.status-dot{animation:pulseDot 2.4s ease-in-out infinite}
@keyframes pulseDot{0%,100%{box-shadow:0 0 0 0 color-mix(in srgb,var(--green) 45%,transparent)}50%{box-shadow:0 0 0 5px transparent}}
@keyframes rise{from{opacity:0;transform:translateY(10px)}}
@keyframes rowIn{from{opacity:0;transform:translateX(-10px)}}
@keyframes activeIn{from{background-color:transparent}}
@keyframes floaty{0%,100%{transform:translateY(0)}50%{transform:translateY(-5px)}}
.message{animation:rise .45s var(--ease) backwards}
.message.user .bubble{border:1px solid var(--line);border-radius:18px 18px 6px 18px;padding:10px 16px;transition:border-color .2s,transform .2s var(--ease)}
.message.user .bubble:hover{border-color:var(--line2)}
.welcome{animation:rise .65s var(--ease) backwards}
.welcome .mark.xl{animation:floaty 4.5s ease-in-out infinite}
.welcome h1{letter-spacing:-.02em}
.main.empty .chip{animation:rise .55s var(--ease) backwards}
.main.empty .chip:nth-child(1){animation-delay:.12s}.main.empty .chip:nth-child(2){animation-delay:.18s}.main.empty .chip:nth-child(3){animation-delay:.24s}.main.empty .chip:nth-child(4){animation-delay:.30s}.main.empty .chip:nth-child(5){animation-delay:.36s}.main.empty .chip:nth-child(6){animation-delay:.42s}
.chip:hover{transform:translateY(-2px);box-shadow:0 6px 16px rgba(0,0,0,.16)}
.composer{border-radius:24px;box-shadow:0 4px 24px rgba(0,0,0,.18);transition:border-color .2s var(--ease-soft),box-shadow .28s var(--ease-soft),transform .28s var(--ease)}
.composer:focus-within{box-shadow:0 0 0 3px var(--green-soft),0 10px 32px rgba(0,0,0,.24)}
.artifact{transition:border-color .2s,box-shadow .25s var(--ease-soft),transform .25s var(--ease)}
.artifact:hover{border-color:var(--green);box-shadow:0 8px 24px rgba(0,0,0,.16)}
.artifact-top:hover{background:var(--raise)}
.codebox{transition:border-color .2s}
.codebox:hover{border-color:var(--line2)}
.toast{transition:opacity .25s var(--ease-soft),transform .32s var(--ease)}
.to-bottom{display:grid;opacity:0;visibility:hidden;pointer-events:none;transform:translateX(-50%) translateY(10px);transition:opacity .22s var(--ease-soft),transform .3s var(--ease),visibility .3s,background-color .18s,color .18s}
.to-bottom.show{display:grid;opacity:1;visibility:visible;pointer-events:auto;transform:translateX(-50%)}
.to-bottom.show:hover{transform:translateX(-50%) translateY(-2px)}
.main.empty .to-bottom{display:none}
.route-flyout{transform-origin:bottom left;transition:opacity .16s var(--ease-soft),transform .22s var(--ease),visibility .22s}
.route-flyout[hidden]{display:grid;visibility:hidden;opacity:0;pointer-events:none;transform:translateY(6px) scale(.98)}
.artifact-pane{display:flex;flex-direction:column;opacity:0;visibility:hidden;pointer-events:none;transform:translateX(28px);transition:opacity .25s var(--ease-soft),transform .36s var(--ease),visibility .36s,width .34s var(--ease)}
.main.has-artifacts .artifact-pane{opacity:1;visibility:visible;pointer-events:auto;transform:none}
.chat-area{transition:flex-basis .36s var(--ease),width .36s var(--ease),max-width .36s var(--ease)}
.chat-list{padding:4px 0 14px;transition:opacity .15s var(--ease-soft),transform .18s var(--ease)}
.chat-list.swapping{opacity:0;transform:translateY(6px)}
.chat-group{display:block}
.chat-date-heading,.chat-date-heading:first-child{display:flex;align-items:center;gap:4px;padding:14px 4px 3px 2px;color:var(--text);font-size:13px;font-weight:600;line-height:1.2}
.chat-group:first-child .chat-date-heading{padding-top:4px}
.heading-toggle{display:inline-flex;align-items:center;gap:5px;padding:4px 7px;border:0;background:transparent;color:var(--text);font-size:13px;font-weight:600;border-radius:8px}
.heading-toggle:hover{background:var(--raise)}
.heading-toggle .chev{width:14px;height:14px;color:var(--muted);transition:transform .3s var(--ease),color .2s}
.heading-toggle:hover .chev{color:var(--text)}
.chat-group.collapsed .heading-toggle .chev{transform:rotate(-90deg)}
.heading-actions{margin-left:auto;display:flex;align-items:center;gap:1px}
.heading-btn{width:28px;height:28px;padding:0;display:grid;place-items:center;border:0;background:transparent;color:var(--muted);border-radius:8px}
.heading-btn .ic{width:16px;height:16px}
.heading-btn:hover,.heading-btn[aria-pressed=true]{color:var(--text);background:var(--raise)}
.heading-btn[aria-pressed=true]{color:var(--green)}
.sort-btn.flipped .ic{transform:scaleY(-1)}
.chat-group-body{display:grid;grid-template-rows:1fr;opacity:1;transition:grid-template-rows .32s var(--ease),opacity .26s var(--ease-soft)}
.chat-group-inner{min-height:0;overflow:hidden;padding:0 1px}
.chat-group.collapsed .chat-group-body{grid-template-rows:0fr;opacity:0}
.chat-group.collapsed .chat-group-inner{visibility:hidden;transition:visibility 0s linear .32s}
.chat-row{position:relative;margin:1px 0;padding:0 2px 0 0;gap:2px;min-height:36px;border-radius:9px;transition:background-color .2s var(--ease-soft),outline-color .2s}
.chat-row.active{animation:activeIn .38s var(--ease-soft)}
.chat-row.enter{animation:rowIn .45s var(--ease) backwards}
.chat-item{position:relative;padding:8px 8px 8px 31px;font-size:14px;line-height:1.35;color:color-mix(in srgb,var(--text) 80%,transparent);transition:color .2s var(--ease-soft),transform .25s var(--ease)}
.chat-item::before{content:"";position:absolute;left:13px;top:50%;width:7px;height:7px;margin-top:-4px;border:1.5px solid var(--muted);border-radius:50%;transition:background-color .25s var(--ease-soft),border-color .25s var(--ease-soft),transform .3s var(--ease)}
.chat-row:hover .chat-item::before{border-color:var(--text);transform:scale(1.15)}
.chat-row.active .chat-item::before{background:var(--green);border-color:var(--green);transform:scale(1.15)}
.chat-row.active .chat-item{font-weight:550}
.chat-row.selected{outline:1px solid var(--green);outline-offset:-1px}
.chat-select-bar{max-height:48px;opacity:1;overflow:hidden;transform:none;transition:max-height .3s var(--ease),opacity .22s var(--ease-soft),padding .3s var(--ease),transform .3s var(--ease),visibility .3s}
.chat-select-bar[hidden]{display:flex;max-height:0;opacity:0;padding-top:0;padding-bottom:0;transform:translateY(-6px);visibility:hidden;pointer-events:none}
.chat-context-menu{display:grid;visibility:hidden;opacity:0;pointer-events:none;transform:scale(.95) translateY(-4px);transition:opacity .16s var(--ease-soft),transform .2s var(--ease),visibility .2s}
.chat-context-menu.open{visibility:visible;opacity:1;pointer-events:auto;transform:none;animation:none}
.side-bottom{transition:border-color .2s}
@media(min-width:701px){
.menu-btn{display:inline-grid}
.app{grid-template-columns:var(--side-w) minmax(0,1fr);transition:grid-template-columns .36s var(--ease)}
.sidebar{overflow:hidden;transition:opacity .28s var(--ease-soft),border-color .3s,padding .36s var(--ease)}
.sidebar>*{min-width:244px}
html.side-collapsed .sidebar{opacity:0;pointer-events:none;border-right-color:transparent;padding-left:0;padding-right:0}
.artifact-pane{width:calc((100vw - var(--side-w))/2)}
}
@media(max-width:700px){
.sidebar{transition:transform .34s var(--ease),box-shadow .34s var(--ease-soft);box-shadow:none}
body.nav-open .sidebar{box-shadow:var(--shadow)}
.scrim,body.nav-open .scrim{display:block;position:fixed;inset:0;z-index:15;background:rgba(4,10,7,.55);-webkit-backdrop-filter:blur(2px);backdrop-filter:blur(2px)}
.scrim{opacity:0;visibility:hidden;pointer-events:none;transition:opacity .3s var(--ease-soft),visibility .3s}
body.nav-open .scrim{opacity:1;visibility:visible;pointer-events:auto}
}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important;scroll-behavior:auto!important}}
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
</svg>
<div class="scrim" id="scrim"></div>
<div class="app">
<aside class="sidebar">
<div class="brand"><span class="logo"><svg viewBox="0 0 20 16" aria-hidden="true"><use href="#kurt-k"/></svg></span><b>Kurt</b></div>
<button class="nav-btn new" id="newChat"><span class="ico"><svg class="ic"><use href="#i-plus"/></svg></span>New chat</button>
<div class="chat-select-bar" id="chatSelectBar" hidden><label><input id="selectAllChats" type="checkbox"> All</label><span id="selectedChatCount">0 selected</span><button id="deleteSelectedChats" type="button" class="danger" disabled>Delete</button><button id="cancelChatSelect" type="button" class="ghost">Done</button></div>
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
<div class="composer-attachments" id="composerAttachments" aria-live="polite" hidden></div>
<textarea id="prompt" rows="1" placeholder="Message Kurt" aria-label="Message Kurt"></textarea>
<div class="compose-bottom">
<button class="icon-btn" id="contextBtn" title="Add files from this computer" aria-label="Add files from this computer"><svg class="ic"><use href="#i-plus"/></svg></button>
<span class="spacer"></span>
<select id="effortSelect" class="mode-pill" title="Response effort" aria-label="Response effort"><option value="thinking" selected>Thinking</option><option value="expert">Expert</option></select>
<select id="operationMode" class="mode-pill" title="Operating mode" aria-label="Operating mode"><option value="normal" selected>Normal</option><option value="code">Code</option></select>
<div class="route-picker" id="routePicker" aria-label="Choose model route"><button class="route-current" id="routeCurrent" type="button" aria-haspopup="true" aria-expanded="false">Choose model</button></div><select id="topModel" hidden aria-label="Selected model route"></select><input id="composerFiles" type="file" multiple hidden>
<button class="send" id="sendBtn" title="Send message" aria-label="Send message" disabled><svg class="ic"><use href="#i-up"/></svg></button>
</div>
</div>
<div class="compose-hint" id="composeHint">Normal mode has no local file access. Code mode can inspect the configured project folder; changes require your approval.</div>
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
<div class="settings-nav-caption">Settings</div>
<button class="settings-nav-btn active" type="button" data-settings-tab="general">General</button>
<button class="settings-nav-btn" type="button" data-settings-tab="local-models">Local models</button>
<button class="settings-nav-btn" type="button" data-settings-tab="keys">API keys</button>
<button class="settings-nav-btn" type="button" data-settings-tab="api-routes">API routes</button>
<button class="settings-nav-btn" type="button" data-settings-tab="groups">Groups</button>
</nav>
<div class="settings-panel-area">
<section class="settings-panel active" data-settings-panel="general">
<h3>General</h3><div class="inline-note">Choose the default route and configure which local folder Code mode may inspect.</div>
<div class="settings-grid"><div class="field"><label>Default chat route</label><select id="settingsModel"></select></div><div class="field"><label>Code mode project access root</label><input id="projectRoot" placeholder="/Users/you/project"><div class="inline-note">Choose one project folder, not your home folder or system root. Code mode can read files beneath this folder, except protected credential and system locations.</div></div></div>
<label class="check-row"><input id="allowProjectWrites" type="checkbox"> Allow applying generated files to the project (each file still requires confirmation)</label>
<div class="field"><label>System prompt</label><textarea id="systemPrompt" style="min-height:180px"></textarea><div class="inline-note">Kurt can review and propose edits to this prompt in chat. Proposed prompt changes appear as a review card and require your approval before saving. Type /system-prompt in chat to display the exact active Kurt prompt for the selected mode.</div></div>
<div class="row"><button class="primary" id="saveSettings">Save settings</button><button id="reloadSettings">Reload from disk</button></div>
</section>
<section class="settings-panel" data-settings-panel="local-models">
<h3>Local models</h3><div class="inline-note">Models installed through Ollama. Removing one uninstalls its downloaded weights.</div>
<div id="localModelList"></div>
<div class="model-download-card"><h4>Download a model</h4><p>Choose where the model comes from. For Hugging Face, enter a GGUF repository such as <code>bartowski/Llama-3.2-3B-Instruct-GGUF</code> (add <code>:Q4_K_M</code> to pick a quantization) or paste its URL. For the Ollama library or another registry, enter an ID such as <code>qwen2.5-coder:7b</code>. Downloads run in the background and progress appears at the top of Kurt.</p><div class="field"><label>Source</label><select id="downloadSource" aria-label="Download source"><option value="ollama" selected>Ollama library / registry</option><option value="huggingface">Hugging Face (GGUF)</option></select></div><div class="row"><input id="downloadModel" aria-label="Model ID or URL" placeholder="Model ID, repository or URL"><button id="startDownload" class="primary">Download model</button></div><div class="inline-note" id="localDownloadStatus">No model download started.</div></div>
</section>
<section class="settings-panel" data-settings-panel="keys">
<h3>API keys</h3><div class="inline-note">Add and manage provider API keys. Each key is listed independently; attach keys to models under API routes.</div>
<div class="settings-grid"><div class="field"><label>Provider</label><select id="keyProvider"></select></div><div class="field"><label>Account label</label><input id="keyLabel" placeholder="Personal account"></div><div class="field"><label>API key</label><input id="keyValue" type="password" autocomplete="off" placeholder="Paste API key (stored locally)"></div></div><button id="addKey" class="primary">Create new key</button>
<div class="row key-sort-row"><span class="inline-note">Sort keys</span><select id="keySort" aria-label="Sort API keys"><option value="name">Name</option><option value="date">Date added</option></select></div><div id="keysList"></div>
</section>
<section class="settings-panel" data-settings-panel="api-routes">
<h3>API routes</h3><div class="inline-note">Models reached through a provider API. Each route links a model name or ID to the saved keys it may use.</div>
<div class="row"><button id="showApiModelForm" class="primary" type="button">Add model</button></div>
<div class="model-download-card" id="apiModelCard" hidden><h4 id="apiModelFormTitle">Add model</h4><p>Select the keys this model may use, then enter the model name or ID exactly as the provider lists it. Keys must belong to one provider.</p>
<div class="field"><label>Keys</label><div id="apiModelKeys" class="key-checks"></div></div>
<div class="settings-grid"><div class="field"><label>Model name / ID</label><input id="apiModelId" aria-label="API model name or ID" placeholder="Exact model ID from the provider"></div><div class="field"><label>Display name (optional)</label><input id="apiModelLabel" placeholder="e.g. My free model"></div></div>
<div class="row"><button id="addApiModel" class="primary" type="button">Save model</button><button id="cancelApiModel" type="button">Cancel</button></div></div>
<div id="apiModelList"></div>
</section>
<section class="settings-panel" data-settings-panel="groups">
<h3>Groups</h3><div class="inline-note">Add local models or API models. The first is the priority; the rest are fallbacks used in order (2, 3, 4…) only when a request fails before a response is produced. Drag entries by their handle or use the arrows to change the order.</div>
<div class="field"><label>Group name</label><input id="failoverGroupName" placeholder="e.g. Reliable, Coding fallback"></div>
<div id="failoverMembers"></div>
<div class="row"><button id="addFailoverMember" type="button">Add model</button><button id="saveFailoverGroup" class="primary" type="button">Create group</button><button id="cancelFailoverGroup" type="button" hidden>Cancel edit</button></div>
<div class="section"><h3>Saved groups</h3><div class="inline-note">Drag a saved entry to change its priority, or press Edit to rearrange and add models.</div><div id="apiGroupsList"></div></div>
</section>
</div></div></div></div>
<div class="modal-backdrop" id="filesModal"><div class="modal"><div class="modal-head"><h2>Add files</h2><button class="ghost" data-close="filesModal">Close</button></div><div class="modal-body"><p style="color:var(--muted);margin-top:0">Choose individual files or an entire folder from Finder or File Explorer. Images are retained as image files in this chat. Only a folder selection is packaged internally as a ZIP, then unpacked automatically. Large pasted text is attached as a .txt file. Your original files are never changed.</p><div class="file-source"><h3>From this computer</h3><div class="file-actions"><button id="chooseLocalFiles" class="primary">Choose files</button><button id="chooseLocalFolder">Choose folder</button><input id="localFiles" type="file" multiple hidden><input id="localFolder" type="file" webkitdirectory directory multiple hidden></div><div class="upload-list" id="uploadList" aria-live="polite">No files selected.</div></div><div class="file-source"><h3>From the configured project</h3><div class="row"><input id="fileFolder" value="" placeholder="Project folder path"><button id="refreshFiles">Open</button><button id="selectCurrentFolder">Add this folder</button></div><div class="file-picker" id="filePicker"></div></div><div class="row" style="margin-top:12px"><button class="primary" id="attachFiles">Add to chat workspace</button></div></div></div></div>
<div class="toast" id="toast"></div>
<div class="route-flyout" id="routeFlyout" hidden><div class="route-flyout-column route-flyout-categories"></div><div class="route-flyout-column route-flyout-options"></div></div>
<div class="chat-context-menu" id="chatContextMenu" role="menu"></div>
<div class="dialog-backdrop" id="appDialogBackdrop"><form class="app-dialog" id="appDialogForm"><h2 id="appDialogTitle"></h2><p class="dialog-message" id="appDialogMessage"></p><div class="dialog-fields" id="appDialogFields"></div><div class="dialog-actions"><button type="button" id="appDialogCancel">Cancel</button><button type="submit" class="primary" id="appDialogConfirm">Save</button></div></form></div>
<script>
const TOKEN='__TOKEN__';const HINT='Normal mode has no local file or computer access. Switch to Code mode to inspect project files and propose changes for your approval.';let state={models:[],routes:[],providers:[],keys:[],apiGroups:[],chats:[],chatId:null,projectRoot:'',selectedFiles:[],selectedFolders:[],pendingUploads:[],uploadedFiles:[],contextCount:0,busy:false,abort:null,run:0,settings:{},sort:'newest',keySort:'name',effort:'thinking',mode:'normal',editingGroup:null,selectingChats:false,selectedChatIds:[]};
const $=id=>document.getElementById(id);const api=async(path,body)=>{const res=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json','X-Kurt-Token':TOKEN},body:body?JSON.stringify(body):undefined});const data=await res.json().catch(()=>({error:'Invalid server response'}));if(!res.ok)throw new Error(data.error||res.statusText);return data};
function toast(msg){const el=$('toast');el.textContent=msg;el.classList.add('show');setTimeout(()=>el.classList.remove('show'),2600)}

function inAppDialog(options={}){
 return new Promise(resolve=>{
  const backdrop=$('appDialogBackdrop'),form=$('appDialogForm'),fieldsRoot=$('appDialogFields');
  $('appDialogTitle').textContent=options.title||'Confirm';$('appDialogMessage').textContent=options.message||'';fieldsRoot.replaceChildren();
  $('appDialogConfirm').textContent=options.confirmText||'Save';$('appDialogConfirm').classList.toggle('danger',!!options.danger);
  const controls=[];
  (options.fields||[]).forEach(field=>{const label=document.createElement('label');label.className='dialog-field';label.textContent=field.label||field.name;const control=document.createElement(field.multiline?'textarea':'input');if(!field.multiline)control.type=field.type||'text';control.value=field.value??'';control.placeholder=field.placeholder||'';control.autocomplete=field.autocomplete||'off';control.required=!!field.required;if(field.maxLength)control.maxLength=field.maxLength;label.appendChild(control);fieldsRoot.appendChild(label);controls.push({name:field.name,control})});
  let settled=false;const finish=value=>{if(settled)return;settled=true;backdrop.classList.remove('show');form.onsubmit=null;$('appDialogCancel').onclick=null;backdrop.onclick=null;document.removeEventListener('keydown',onKey);resolve(value)};
  const onKey=e=>{if(e.key==='Escape'){e.preventDefault();finish(null)}};
  form.onsubmit=e=>{e.preventDefault();if(!form.reportValidity())return;finish(controls.length?Object.fromEntries(controls.map(x=>[x.name,x.control.value])):true)};
  $('appDialogCancel').onclick=()=>finish(null);backdrop.onclick=e=>{if(e.target===backdrop)finish(null)};document.addEventListener('keydown',onKey);backdrop.classList.add('show');setTimeout(()=>{(controls[0]?.control||$('appDialogConfirm')).focus()},30);
 });
}
async function confirmInApp(title,message,confirmText='Confirm',danger=false){return (await inAppDialog({title,message,confirmText,danger}))!==null}
function closeCustomSelects(except){document.querySelectorAll('.custom-select.open').forEach(w=>{if(w!==except){w.classList.remove('open');w.querySelector('.custom-select-trigger')?.setAttribute('aria-expanded','false')}})}
function syncSelect(select){const wrap=select?._customWrap;if(!wrap)return;const option=select.options[select.selectedIndex];wrap.querySelector('.custom-select-trigger').textContent=option?.textContent||'Choose…';wrap.querySelectorAll('.custom-select-option').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.value===select.value)))}
function enhanceSelect(select){
 if(!select||select.id==='topModel'||select._customWrap)return;const wrap=document.createElement('div');wrap.className='custom-select '+Array.from(select.classList).join(' ');const trigger=document.createElement('button');trigger.type='button';trigger.className='custom-select-trigger';trigger.setAttribute('aria-haspopup','listbox');trigger.setAttribute('aria-expanded','false');trigger.setAttribute('aria-label',select.getAttribute('aria-label')||select.title||'Choose an option');const menu=document.createElement('div');menu.className='custom-select-menu';menu.setAttribute('role','listbox');select.parentNode.insertBefore(wrap,select);wrap.append(select,trigger,menu);select.classList.add('custom-select-native');select._customWrap=wrap;
 const build=()=>{menu.replaceChildren();let group='';Array.from(select.options).forEach(option=>{const groupName=option.parentElement?.tagName==='OPTGROUP'?option.parentElement.label:'';if(groupName&&groupName!==group){group=groupName;const heading=document.createElement('div');heading.className='custom-select-group';heading.textContent=groupName;menu.appendChild(heading)}const button=document.createElement('button');button.type='button';button.className='custom-select-option';button.textContent=option.textContent;button.dataset.value=option.value;button.disabled=option.disabled;button.setAttribute('role','option');button.setAttribute('aria-selected',String(option.value===select.value));button.onclick=()=>{select.value=option.value;select.dispatchEvent(new Event('change',{bubbles:true}));wrap.classList.remove('open');trigger.setAttribute('aria-expanded','false');trigger.focus()};menu.appendChild(button)});syncSelect(select)};
 trigger.onclick=()=>{const open=!wrap.classList.contains('open');closeCustomSelects(wrap);if(open){const r=trigger.getBoundingClientRect();const pane=wrap.closest('.settings-panel-area');const limitBottom=Math.min(window.innerHeight,pane?pane.getBoundingClientRect().bottom:window.innerHeight);const limitTop=Math.max(0,pane?pane.getBoundingClientRect().top:0);const below=limitBottom-r.bottom-14,above=r.top-limitTop-14;const up=!!wrap.closest('.compose-bottom')||(below<180&&above>below);wrap.classList.toggle('drop-up',up);menu.style.setProperty('--menu-max',Math.max(120,Math.min(320,up?above:below))+'px')}wrap.classList.toggle('open',open);trigger.setAttribute('aria-expanded',String(open))};trigger.onkeydown=e=>{if(['ArrowDown','Enter',' '].includes(e.key)){e.preventDefault();wrap.classList.add('open');trigger.setAttribute('aria-expanded','true');menu.querySelector('.custom-select-option:not(:disabled)')?.focus()}else if(e.key==='Escape'){wrap.classList.remove('open');trigger.setAttribute('aria-expanded','false')}};select.addEventListener('change',build);const observer=new MutationObserver(build);observer.observe(select,{childList:true,subtree:true,attributes:true,characterData:true});select._customObserver=observer;build();
}
function enhanceAllSelects(root=document){root.querySelectorAll('select').forEach(enhanceSelect)}
function setSelectOptions(select,items,placeholder=''){if(!select)return;const current=select.value;select.innerHTML='';if(placeholder){const option=document.createElement('option');option.value='';option.textContent=placeholder;option.disabled=true;select.appendChild(option)}items.forEach(item=>{const option=document.createElement('option');option.value=item.value;option.textContent=item.label;select.appendChild(option)});if(items.some(item=>item.value===current))select.value=current;else if(items.length)select.value=items[0].value;syncSelect(select)}

function modelLabel(m){return `${m.label||m.name||m.model} · ${m.provider}${m.current?' · active':''}`}
function fillModels(){
 const routes=(state.routes||[]).map(route=>({value:route.value,label:route.label}));
 const fallback=state.models.filter(model=>model.type==='local').map(model=>({value:'model|'+model.ref,label:'Local · '+modelLabel(model)}));
 const options=routes.length?routes:fallback;
 setSelectOptions($('topModel'),options,'No routes configured');
 setSelectOptions($('settingsModel'),options,'No routes configured');
 const preferred=state.settings.default_route||'';
 let selected=options.find(item=>item.value===preferred)?.value||'';
 if(!selected&&state.settings.model){
  const saved=String(state.settings.model);
  selected=options.find(item=>item.value==='model|'+saved)?.value||
   options.find(item=>item.value.startsWith('keymodel|'+saved+'|'))?.value||'';
 }
 if(!selected)selected=options[0]?.value||'';
 if(selected){$('topModel').value=selected;$('settingsModel').value=selected}
 syncSelect($('settingsModel'));renderRoutePicker();renderLocalModels();renderApiModels();
}
function routeCategoryForValue(value){
 const route=(state.routes||[]).find(item=>item.value===value);
 if(route?.kind)return route.kind;
 if(String(value||'').startsWith('model|'))return 'local';
 if(String(value||'').startsWith('key'))return 'key';
 return 'group';
}
function routeChoiceLabel(value){
 const route=(state.routes||[]).find(item=>item.value===value);
 if(route)return String(route.label||'').replace(/^(?:Local|Key|API|Group)\s*·\s*/,'')||route.label;
 return routeLabel(value);
}
function routeLabel(value){
 const route=(state.routes||[]).find(item=>item.value===value);
 const parts=String(value||'').split('|');
 if(parts[0]==='model'||parts[0]==='keymodel'){
  const model=state.models.find(item=>item.ref===parts[1]);
  if(model)return model.label||model.name||'Choose model';
 }
 if(route)return String(route.label||'').replace(/^(?:Local|Key|API|Group)\s*·\s*/,'').replace(/\s*·\s*key pool$/i,'')||route.label;
 return 'Choose model';
}
function renderRoutePicker(){
 const trigger=$('routeCurrent');if(!trigger)return;
 let selected=String($('topModel').value||'');
 if(selected&&!state.routes.some(route=>route.value===selected))selected='';
 if(!selected){const preferred=state.settings.default_route||'';selected=state.routes.find(route=>route.value===preferred)?.value||state.routes[0]?.value||''}
 if(selected!==$('topModel').value)$('topModel').value=selected;
 const route=(state.routes||[]).find(item=>item.value===selected);
 trigger.textContent=routeLabel(selected);
 trigger.title=route?.label||routeLabel(selected);
 trigger.setAttribute('aria-label','Current model: '+trigger.title+'. Choose another model');
 trigger.onclick=event=>{event.stopPropagation();openRouteFlyout(trigger)};
 trigger.onmouseenter=()=>{if($('routeFlyout').hidden)openRouteFlyout(trigger)};
}
function positionRouteFlyout(){
 const menu=$('routeFlyout'),trigger=$('routeCurrent');if(!menu||menu.hidden||!trigger)return;
 const width=Math.max(0,Math.min(430,window.innerWidth-20));
 const height=Math.max(160,Math.min(320,window.innerHeight-24));
 menu.style.width=width+'px';menu.style.height=height+'px';menu.style.maxHeight=height+'px';
 const rect=trigger.getBoundingClientRect();
 const left=Math.max(10,Math.min(window.innerWidth-width-10,rect.right-width));
 let top=rect.top-height-8;
 if(top<12)top=Math.min(rect.bottom+8,window.innerHeight-height-12);
 menu.style.left=left+'px';menu.style.top=Math.max(12,top)+'px';
}
function closeRouteFlyout(){const menu=$('routeFlyout');if(menu){menu.hidden=true;menu.setAttribute('aria-hidden','true');$('routeCurrent')?.setAttribute('aria-expanded','false')}}
function showRouteOptions(category){
 const menu=$('routeFlyout');if(!menu)return;
 const categories=menu.querySelector('.route-flyout-categories');const options=menu.querySelector('.route-flyout-options');
 categories.querySelectorAll('[data-route-kind]').forEach(button=>button.classList.toggle('active',button.dataset.routeKind===category));
 options.replaceChildren();options.scrollTop=0;
 const routes=(state.routes||[]).filter(route=>route.kind===category);
 if(!routes.length){const empty=document.createElement('div');empty.className='route-flyout-empty';empty.textContent=category==='local'?'No local models are installed.':category==='key'?'No API model has a key yet. Add a model in Settings › API routes and it will appear here with your key.':'No groups have been created.';options.appendChild(empty)}
 else routes.forEach(route=>{
  const item=document.createElement('button');item.type='button';item.className='route-flyout-item';item.textContent=routeChoiceLabel(route.value);item.title=route.label;
  if(route.value===$('topModel').value)item.setAttribute('aria-current','true');
  item.onclick=event=>{event.stopPropagation();$('topModel').value=route.value;$('topModel').dispatchEvent(new Event('change',{bubbles:true}));closeRouteFlyout()};
  options.appendChild(item);
 });
 requestAnimationFrame(positionRouteFlyout);
}
function openRouteFlyout(trigger){
 const menu=$('routeFlyout');if(!menu||!trigger)return;
 const categories=menu.querySelector('.route-flyout-categories');categories.replaceChildren();
 menu.hidden=false;menu.setAttribute('aria-hidden','false');$('routeCurrent').setAttribute('aria-expanded','true');
 const categoryLabels=[['local','Local'],['key','API'],['group','Group']];
 const first=routeCategoryForValue($('topModel').value);
 categoryLabels.forEach(([kind,label])=>{
  const button=document.createElement('button');button.type='button';button.className='route-flyout-category';button.dataset.routeKind=kind;button.textContent=label;
  button.onmouseenter=()=>showRouteOptions(kind);
  button.onfocus=()=>showRouteOptions(kind);
  button.onclick=event=>{event.stopPropagation();showRouteOptions(kind)};
  categories.appendChild(button);
 });
 showRouteOptions(first);
 requestAnimationFrame(positionRouteFlyout);
}
window.addEventListener('resize',positionRouteFlyout);
window.addEventListener('scroll',positionRouteFlyout,true);
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function stripUnsupportedActions(src){let text=String(src||'');const opener=/<think(?:ing)?>/i.exec(text),closer=/<\/think(?:ing)?>/i.exec(text);if(closer&&(!opener||closer.index<opener.index))text=text.slice(closer.index+closer[0].length);return text
 .replace(/@@@\s*(?:RUN|SEARCH|READ|LIST|WRITE|EDIT|SYSTEM_PROMPT|EXEC|BASH|SHELL)\b[\s\S]*?(?:@@@\s*END\b|$)/gi,'')
 .replace(/<think(?:ing)?>[\s\S]*?(?:<\/think(?:ing)?>|$)/gi,'')
 .replace(/<\/think(?:ing)?>/gi,'')
 .replace(/<\|tool_call_start\|>[\s\S]*?(?:<\|tool_call_end\|>|$)/gi,'')
 .replace(/<\|tool_call_(?:start|end)\|>/gi,'')
 .trim()}
function inline(s){const spans=[];s=s.replace(/`([^`\n]+)`/g,(_,c)=>{spans.push('<code>'+c+'</code>');return '\u0001'+(spans.length-1)+'\u0001'});s=s.replace(/\*\*([^*\n]+?)\*\*/g,'<strong>$1</strong>').replace(/(^|[^*\w])\*([^*\n]+?)\*(?![*\w])/g,'$1<em>$2</em>').replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,'<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');return s.replace(/\u0001(\d+)\u0001/g,(_,i)=>spans[+i])}
function mdBlocks(text){
 const math=[];
 const mathToken=tex=>{const index=math.push(String(tex||'').trim())-1;return '\n\u0002KURTMATH'+index+'\u0002\n'};
 text=String(text||'')
  .replace(/\\begin\{(equation\*?|align\*?|gather\*?|multline\*?|displaymath)\}([\s\S]*?)\\end\{\1\}/g,(_,environment,tex)=>mathToken('\\begin{'+environment+'}'+tex+'\\end{'+environment+'}'))
  .replace(/\\\[([\s\S]*?)\\\]/g,(_,tex)=>mathToken(tex))
  .replace(/\$\$([\s\S]*?)\$\$/g,(_,tex)=>mathToken(tex))
  .replace(/\\\(([\s\S]*?)\\\)/g,(_,tex)=>mathToken(tex))
  .replace(/(^|[^\$])\$([^\$\n]+)\$(?!\$)/g,(_,prefix,tex)=>prefix+mathToken(tex));
 const lines=esc(text).split('\n');let html='',para=[],list=null,quote=[];
 const flushP=()=>{if(para.length){html+='<p>'+inline(para.join('\n')).replace(/\n/g,'<br>')+'</p>';para=[]}};
 const flushL=()=>{if(list){html+='<'+list.t+'>'+list.items.map(i=>'<li>'+inline(i)+'</li>').join('')+'</'+list.t+'>';list=null}};
 const flushQ=()=>{if(quote.length){html+='<blockquote>'+inline(quote.join('<br>'))+'</blockquote>';quote=[]}};
 for(const line of lines){let m;
  if(m=/^\u0002KURTMATH(\d+)\u0002$/.exec(line)){flushP();flushL();flushQ();html+='<div class="math-display"><strong>\\['+esc(math[Number(m[1])])+'\\]</strong></div>';continue}
  if(!line.trim()){flushP();flushL();flushQ();continue}
  if(m=/^(#{1,6})\s+(.*)$/.exec(line)){flushP();flushL();flushQ();const n=Math.min(m[1].length,3);html+='<h'+n+'>'+inline(m[2])+'</h'+n+'>';continue}
  if(/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)){flushP();flushL();flushQ();html+='<hr>';continue}
  if(m=/^&gt;\s?(.*)$/.exec(line)){flushP();flushL();quote.push(m[1]);continue}
  if(m=/^\s*[-*+]\s+(.*)$/.exec(line)){flushP();flushQ();if(!list||list.t!=='ul'){flushL();list={t:'ul',items:[]}}list.items.push(m[1]);continue}
  if(m=/^\s*\d+[.)]\s+(.*)$/.exec(line)){flushP();flushQ();if(!list||list.t!=='ol'){flushL();list={t:'ol',items:[]}}list.items.push(m[1]);continue}
  flushL();flushQ();para.push(line)
 }
 flushP();flushL();flushQ();return html
}
function hasMathMarkup(value){return /\\begin\{(?:equation\*?|align\*?|gather\*?|multline\*?|displaymath)\}|\\\[[\s\S]*?\\\]|\$\$[\s\S]*?\$\$|\\\([\s\S]*?\\\)|(^|[^\$])\$[^\$\n]+\$(?!\$)/.test(String(value||''))}
window.MathJax={tex:{inlineMath:[],displayMath:[['\\[','\\]']],processEscapes:true,packages:{'[+]':['ams']}},options:{skipHtmlTags:['script','noscript','style','textarea','pre','code']},startup:{typeset:false}};
function typesetMath(root){if(!root||!window.MathJax?.typesetPromise)return;try{window.MathJax.typesetClear?.([root]);window.MathJax.typesetPromise([root]).catch(error=>console.warn('Math rendering failed:',error))}catch(error){console.warn('Math rendering failed:',error)}}
const mathJaxScript=document.createElement('script');mathJaxScript.src='https://cdn.jsdelivr.net/npm/mathjax@3.2.2/es5/tex-chtml.js';mathJaxScript.async=true;mathJaxScript.onload=()=>document.querySelectorAll('.content').forEach(typesetMath);mathJaxScript.onerror=()=>console.warn('MathJax could not be loaded; equations will remain centered as TeX text.');document.head.appendChild(mathJaxScript);
function splitResponseSummary(src,allowOpen=false){src=String(src||'');const marker=/@@@\s*SUMMARY\s*\r?\n/i.exec(src);if(!marker)return{summary:'',answer:src};const start=marker.index,bodyStart=start+marker[0].length,tail=src.slice(bodyStart),end=/\r?\n?@@@\s*END\b/i.exec(tail);if(!end)return allowOpen?{summary:tail.trim(),answer:src.slice(0,start).trim()}:{summary:'',answer:src};const summary=tail.slice(0,end.index).trim(),after=tail.slice(end.index+end[0].length);return{summary,answer:(src.slice(0,start)+'\n'+after).replace(/\n{3,}/g,'\n\n').trim()}}
function renderMarkdown(src){src=String(src||'');return src.split(/((?:```|~~~)[^\n]*\n[\s\S]*?(?:```|~~~)|(?:```|~~~)[^\n]*\n[\s\S]*$|(?:```|~~~)[^\n]*$)/).map((seg,i)=>{if(i%2===0)return mdBlocks(seg);const fence=seg.slice(0,3),nl=seg.indexOf('\n');const lang=(nl<0?seg.slice(3):seg.slice(3,nl)).trim().split(/\s+/)[0]||'';let code=nl<0?'':seg.slice(nl+1);if(nl>=0&&seg.endsWith(fence))code=code.slice(0,-fence.length).replace(/\n$/,'');return '<div class="codebox"><div class="codehead"><span>'+esc(lang||'code')+'</span><button class="copy-mini" data-copy-code>Copy code</button></div><pre><code>'+esc(code.replace(/\n$/,''))+'</code></pre></div>'}).join('')}
function splitThink(raw,streaming){return{think:'',answer:stripUnsupportedActions(raw),open:false}}
function fmtSecs(s){s=Math.max(1,Math.round(s));return s<60?s+'s':Math.floor(s/60)+'m '+(s%60)+'s'}
function bindCopy(root){root.querySelectorAll('[data-copy-code]').forEach(b=>b.onclick=()=>{const code=b.closest('.codebox').querySelector('code');navigator.clipboard.writeText(code?code.textContent:'').then(()=>toast('Code copied'))})}
function formatThought(seconds){const s=Number(seconds);if(!Number.isFinite(s)||s<=0)return '';if(s<60)return Math.max(1,Math.round(s))+'s';const m=Math.floor(s/60),r=Math.round(s%60);return r?m+'m '+r+'s':m+'m'}
function setReasoningOpen(panel,open){panel.classList.toggle('open',open);if(panel._head){panel._head.classList.toggle('open',open);panel._head.setAttribute('aria-expanded',String(open))}}
function reasoningBlock(){const d=document.createElement('div');d.className='think reasoning-panel';d.hidden=true;d.innerHTML='<div class="think-body"></div>';const head=document.createElement('button');head.type='button';head.className='think-head';head.hidden=true;head.setAttribute('aria-expanded','false');head.innerHTML='<svg class="ic chev"><use href="#i-chev"/></svg><span class="think-label">Approach &amp; checks</span>';d._head=head;d._label=head.querySelector('.think-label');head.onclick=()=>{const open=!d.classList.contains('open');setReasoningOpen(d,open);d.dataset.userCollapsed=open?'':'1';d.dataset.userOpen=open?'1':''};return d}
function thinkBlock(){const d=document.createElement('div');d.className='think work-log';d.hidden=true;d.innerHTML='<button class="work-log-head" type="button" aria-expanded="false"><svg class="ic chev"><use href="#i-chev"/></svg><span class="think-label">Workspace activity</span></button><div class="work-log-body"></div>';d.querySelector('.work-log-head').onclick=()=>{const open=d.classList.toggle('open');d.querySelector('.work-log-head').setAttribute('aria-expanded',String(open))};return d}
function setReasoning(node,text,live=false,seconds=0){
 const panel=node.querySelector('.reasoning-panel');if(!panel)return;
 const head=panel._head,label=panel._label;
 const value=String(text||'').trim();
 if(!value){panel.hidden=true;if(head)head.hidden=true;return}
 panel.hidden=false;if(head)head.hidden=false;
 const body=panel.querySelector('.think-body');
 body.textContent=value;panel.classList.toggle('live',!!live);if(head)head.classList.toggle('live',!!live);
 if(live){label.textContent='Preparing rationale…';if(panel.dataset.userCollapsed!=='1')setReasoningOpen(panel,true);body.scrollTop=body.scrollHeight}
 else{const t=formatThought(seconds);label.textContent='Approach & checks'+(t?' · '+t:'');if(panel.dataset.userCollapsed!=='1')setReasoningOpen(panel,true)}
}
function setThink(node,entries){const panel=node.querySelector('.work-log');if(!panel)return;const items=Array.isArray(entries)?entries.slice(-120):[];if(!items.length){panel.hidden=true;return}panel.hidden=false;const body=panel.querySelector('.work-log-body');const open=panel.dataset.initialized!=='1'||panel.classList.contains('open');panel.dataset.initialized='1';panel.classList.toggle('open',open);panel.querySelector('.work-log-head').setAttribute('aria-expanded',String(open));body.replaceChildren();items.forEach(item=>{const data=typeof item==='string'?{text:item}:item||{};const label=String(data.text||'Activity');const detail=String(data.detail||'');const row=document.createElement(detail&&detail!==label?'details':'div');row.className='work-step'+(data.ok===true?' ok':data.ok===false?' fail':'');if(row.tagName==='DETAILS'){row.open=true;const summary=document.createElement('summary');summary.textContent=label;const content=document.createElement('div');content.className='work-detail';content.textContent=detail;row.append(summary,content)}else row.textContent=label;body.appendChild(row)});panel.querySelector('.think-label').textContent='Full activity log · '+items.length+' event(s)'}
function pushWorkStep(node,text,ok,detail){const entries=node.__worklog||(node.__worklog=[]);const last=entries[entries.length-1];if(last&&last.text===text&&last.ok===ok&&last.detail===detail)return;entries.push({text,ok,detail:detail||''});if(entries.length>120)entries.shift();setThink(node,entries)}
function endThink(node){const status=node.querySelector('.assistant-status');if(status)status.textContent='';const panel=node.querySelector('.reasoning-panel');if(panel){panel.classList.remove('live');if(panel._head)panel._head.classList.remove('live');const label=panel._label;if(label&&label.textContent.indexOf('Preparing')===0)label.textContent='Approach & checks'}}
let follow=true;
function scrollEnd(force){if(!force&&!follow)return;const m=$('messages');m.scrollTop=m.scrollHeight}
function setEmpty(on){$('main').classList.toggle('empty',on);if(on)$('toBottom').classList.remove('show')}
function setTitle(title,meta){$('chatTitle').textContent=title||'New chat';$('chatMeta').textContent=meta||''}
function autosize(){const t=$('prompt');t.style.height='auto';t.style.height=Math.min(t.scrollHeight,220)+'px'}
function updateSend(){$('sendBtn').disabled=!state.busy&&!$('prompt').value.trim()}
function hint(){const base=state.contextCount?state.contextCount+' item(s) in this chat’s isolated workspace':HINT;$('composeHint').textContent=state.mode==='code'?'Code mode can read files only under the configured project directory, not the whole computer. File changes are previews until you approve Apply to project; every terminal command also requires approval and runs in the isolated chat workspace.':HINT}
function setBusy(b){state.busy=b;const btn=$('sendBtn');btn.querySelector('use').setAttribute('href',b?'#i-stop':'#i-up');btn.title=b?'Stop response':'Send message';btn.setAttribute('aria-label',btn.title);updateSend()}
function cancelRun(){if(state.abort)state.abort.abort();state.abort=null;state.run++;setBusy(false)}
function responseModelLabel(ref){if(!ref)return'';const item=state.models.find(m=>m.ref===ref);return item?`${item.label||item.name||item.model} · ${item.provider}`:String(ref)}
function setResponseMeta(node,stats,model){const el=node?.querySelector('.response-metrics');if(!el)return;const parts=[];const seconds=Number(stats?.thought_seconds);const speed=Number(stats?.tokens_per_second);if(Number.isFinite(seconds)&&seconds>0)parts.push(`Generated in ${seconds.toFixed(seconds<10?1:0)}s`);if(Number.isFinite(speed)&&speed>0)parts.push(`${stats?.tokens_estimated?'~':''}${speed.toFixed(speed<10?1:0)} tok/s`);const label=responseModelLabel(model);if(label)parts.push(label);el.textContent=parts.join(' · ');if(stats?.tokens_estimated)el.title='Token rate is estimated from generated text length because the provider did not report output-token usage.'}
function renderMessage(role,content,artifacts,extra){const wrap=document.createElement('div');wrap.className='message '+role;let safeContent=role==='assistant'?stripUnsupportedActions(content):String(content||'');if(role==='assistant'&&!safeContent&&/(?:@@@\s*(?:RUN|READ|EXEC)|tool_call|<\/?think)/i.test(String(content||'')))safeContent='This earlier response contained an unsupported tool action. Kurt did not execute it.';wrap.dataset.raw=safeContent;const bubble=document.createElement('div');bubble.className='bubble';wrap.appendChild(bubble);if(role==='user'){if(hasMathMarkup(safeContent)){const userContent=document.createElement('div');userContent.className='content user-content';userContent.innerHTML=renderMarkdown(safeContent);bubble.appendChild(userContent);typesetMath(userContent)}else bubble.textContent=content}else{bubble.insertAdjacentHTML('beforeend','<div class="assistant-heading"><svg class="mark" viewBox="0 0 20 16" aria-hidden="true"><use href="#kurt-k"/></svg><span>Kurt</span><span class="assistant-status"></span></div>');const reasoning=reasoningBlock();const headingRow=bubble.querySelector('.assistant-heading');headingRow.insertBefore(reasoning._head,headingRow.querySelector('.assistant-status'));bubble.appendChild(reasoning);bubble.appendChild(thinkBlock());bubble.insertAdjacentHTML('beforeend','<div class="content"></div><div class="message-artifacts"></div><div class="msg-foot"><button class="icon-btn copy-msg" title="Copy" aria-label="Copy message"><svg class="ic"><use href="#i-copy"/></svg></button><span class="response-metrics"></span></div>');const contentNode=bubble.querySelector('.content');contentNode.innerHTML=renderMarkdown(safeContent);typesetMath(contentNode);bindCopy(wrap);wrap.__worklog=Array.isArray(extra?.worklog)?extra.worklog.slice():[];setReasoning(wrap,extra?.reasoning||'',false,extra?.thinkingSeconds||0);setThink(wrap,extra?.worklog||[]);setResponseMeta(wrap,extra?.stats,extra?.model);renderArtifactTriggers(bubble.querySelector('.message-artifacts'),artifacts||[]);bubble.querySelector('.copy-msg').onclick=()=>navigator.clipboard.writeText(wrap.dataset.raw||'').then(()=>toast('Message copied'))}$('messages').appendChild(wrap);return wrap}
function clearArtifactPane(){const pane=$('artifactPaneContent');if(pane)pane.innerHTML='';const main=$('main');if(main)main.classList.remove('has-artifacts');document.querySelectorAll('.artifact-trigger').forEach(button=>{button.classList.remove('selected');button.setAttribute('aria-expanded','false')});const title=$('artifactPaneTitle');if(title)title.textContent='File preview';const count=$('artifactCount');if(count)count.textContent='Select a file card to preview'}
function closeArtifactPane(){const main=$('main');if(main)main.classList.remove('has-artifacts');const pane=$('artifactPaneContent');if(pane)pane.innerHTML='';document.querySelectorAll('.artifact-trigger.selected').forEach(button=>{button.classList.remove('selected');button.setAttribute('aria-expanded','false')});const title=$('artifactPaneTitle');if(title)title.textContent='File preview';const count=$('artifactCount');if(count)count.textContent='Select a file card to preview'}
function renderArtifactTriggers(container,artifacts){
 if(!container)return;
 container.innerHTML='';
 (artifacts||[]).forEach(a=>{
  const promptCard=a.kind==='system_prompt',commandCard=a.kind==='command';
  const button=document.createElement('button');button.type='button';button.className='artifact-trigger';
  button.setAttribute('aria-controls','artifactPane');button.setAttribute('aria-expanded','false');
  button.innerHTML='<span class="artifact-trigger-icon"><svg class="ic" aria-hidden="true"><use href="#i-file"/></svg></span><span class="artifact-trigger-text"><span class="artifact-trigger-name"></span><span class="artifact-trigger-status"></span></span><span class="artifact-trigger-open">Review →</span>';
  button.querySelector('.artifact-trigger-name').textContent=promptCard?'System prompt proposal':commandCard?'Command approval':String(a.path||'Generated file');
  button.querySelector('.artifact-trigger-status').textContent=a.validation?.message||(a.approval_required?'Ready for approval · not saved':'Preview');
  button.setAttribute('aria-label','Review '+(a.path||'generated file'));button.onclick=()=>openArtifactPreview(a,button);container.appendChild(button);
 });
}
function openArtifactPreview(a,trigger){
 const pane=$('artifactPaneContent');if(!pane)return;pane.replaceChildren(artifactCard(a));$('main').classList.add('has-artifacts');
 document.querySelectorAll('.artifact-trigger').forEach(button=>{const selected=button===trigger;button.classList.toggle('selected',selected);button.setAttribute('aria-expanded',String(selected))});
 const title=$('artifactPaneTitle');if(title)title.textContent=a.kind==='system_prompt'?'System prompt proposal':a.kind==='command'?'Command approval':String(a.path||'File preview');
 const count=$('artifactCount');if(count)count.textContent=a.kind==='command'?'Not executed · approval required':a.approval_required?'Preview only · not saved':'Selected file preview';
 $('artifactPane').scrollTop=0;
}
function artifactCard(a){
 const promptCard=a.kind==='system_prompt',commandCard=a.kind==='command';
 const card=document.createElement('div');card.className='artifact open'+(a.validation?.ok?'':' bad');
 const actions=commandCard?'<button class="primary approve-command">Approve &amp; run</button>':promptCard?(a.preview_only?'':'<button class="primary apply-prompt">Apply prompt to Kurt</button>'):'<button class="download-artifact">Download file</button><button class="primary apply-artifact">Apply to project</button>';
 const heading=commandCard?'COMMAND REVIEW':promptCard?'SYSTEM PROMPT':esc((String(a.path||'file').split('.').pop()||'FILE').toUpperCase());
 const status=a.validation?.message||(a.approval_required?'Preview only · not saved':'Ready for review');
 card.innerHTML=`<div class="artifact-top"><div class="file-icon"><svg class="ic"><use href="#i-file"/></svg></div><div class="file-name">${esc(a.path)}</div><div class="file-check">${esc(status)}</div><span class="artifact-chev"><svg class="ic"><use href="#i-chev"/></svg></span></div><div class="artifact-body"><div class="codebox"><div class="codehead"><span>${heading}</span><button class="copy-mini copy-artifact">Copy</button></div><pre><code></code></pre></div><div class="artifact-actions">${actions}</div></div>`;
 card.querySelector('code').textContent=a.content;card.querySelector('.artifact-top').onclick=()=>card.classList.toggle('open');
 card.querySelector('.copy-artifact').onclick=()=>navigator.clipboard.writeText(a.content).then(()=>toast('Content copied'));
 if(commandCard){
  const button=card.querySelector('.approve-command');
  button.onclick=async()=>{
   if(state.mode!=='code'){toast('Switch this conversation to Code mode before running a command.');return}
   const approved=await confirmInApp('Approve command execution',`Run this command in the isolated workspace for this conversation?\n\n${a.content}\n\nIt has not run yet. The command runner only permits approved developer checks.`, 'Approve & run',true);
   if(!approved)return;
   button.disabled=true;button.textContent='Running…';
   try{
    const result=await api('/api/command/run',{chat_id:state.chatId,command:a.content,approved:true});
    const output=`Exit code: ${result.returncode}\nStatus: ${result.ok?'Succeeded':'Failed'}\n\n${String(result.output||'(No output.)').slice(0,12000)}`;
    await inAppDialog({title:result.ok?'Command completed':'Command failed',message:output,confirmText:'Close'});
    button.textContent=result.ok?'Completed':'Failed';
    const fileCheck=card.querySelector('.file-check');if(fileCheck)fileCheck.textContent=result.ok?'Executed with approval':'Execution failed';
   }catch(error){button.disabled=false;button.textContent='Approve & run';toast(error.message)}
  };
 }else if(promptCard&&!a.preview_only){
  card.querySelector('.apply-prompt').onclick=async()=>{
   if(state.mode!=='code'){toast('Switch this conversation to Code mode before changing the system prompt.');return}
   if(!await confirmInApp('Apply system prompt','Replace Kurt’s editable system prompt with this proposed version?','Apply',true))return;
   try{await api('/api/settings',{system_prompt:a.content,prompt_proposal:true,chat_id:state.chatId,approved:true});await refreshBootstrap();toast('Kurt system prompt updated')}catch(error){toast(error.message)}
  };
 }else if(!promptCard){
  card.querySelector('.download-artifact').onclick=async()=>{
   if(!await confirmInApp('Download approved file',`Save ${a.path} to your computer’s Downloads folder? This only downloads the proposed file; it does not modify the project.`, 'Download',false))return;
   const blob=new Blob([a.content],{type:'application/octet-stream'}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=a.path.split('/').pop();link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  };
  card.querySelector('.apply-artifact').onclick=async()=>{
   if(state.mode!=='code'){toast('Switch this conversation to Code mode before applying a file.');return}
   if(!a.validation?.ok){toast('Fix validation errors before applying');return}
   if(!state.settings.allow_project_writes){toast('Enable project writes in Settings first');return}
   const message=`Apply ${a.path} to ${state.projectRoot}?${a.exists?'\nThis will overwrite the existing project file.':''}\n\nThe proposed file has not been saved yet.`;
   if(!await confirmInApp('Apply file to project',message,'Apply file',true))return;
   try{const result=await api('/api/file/write',{path:a.path,content:a.content,overwrite:!!a.exists,chat_id:state.chatId,approved:true,authorized:true});toast(result.message||'Applied to project');const status=card.querySelector('.file-check');if(status)status.textContent='Applied to project with approval'}catch(error){toast(error.message)}
  };
 }
 return card;
}
const SUGGESTIONS=[['Understand my project','Inspect the current project structure and explain its architecture.'],['Build a feature','Help me plan and implement a new feature. Ask for any missing requirements first.'],['Generate a file','Write a small utility script and return it as a file card.'],['Debug code','Help me debug this issue. Start by asking for the relevant error and files.']];
function showWelcome(){clearArtifactPane();setEmpty(true);follow=true;$('messages').innerHTML='<div class="welcome"><svg class="mark xl" viewBox="0 0 20 16" aria-hidden="true"><use href="#kurt-k"/></svg><h1>What are we building?</h1></div>';$('chips').innerHTML=SUGGESTIONS.map(s=>'<button class="chip" data-prompt="'+esc(s[1])+'">'+esc(s[0])+'</button>').join('');document.querySelectorAll('#chips [data-prompt]').forEach(b=>b.onclick=()=>{$('prompt').value=b.dataset.prompt;autosize();updateSend();$('prompt').focus()})}
function closeChatMenu(){const menu=$('chatContextMenu');if(!menu.classList.contains('open')&&!menu.childElementCount)return;menu.classList.remove('open');clearTimeout(menu._t);menu._t=setTimeout(()=>{if(!menu.classList.contains('open')){menu.replaceChildren();menu.removeAttribute('data-chat-id')}},220)}
function openChatMenu(chatId,x,y){const chat=state.chats.find(item=>item.id===chatId);if(!chat)return;const menu=$('chatContextMenu');menu.replaceChildren();menu.dataset.chatId=chatId;const add=(label,action,cls='')=>{const button=document.createElement('button');button.type='button';button.className=cls;button.textContent=label;button.onclick=()=>{closeChatMenu();action()};menu.appendChild(button)};add(chat.pinned?'Unpin chat':'Pin chat',()=>togglePinChat(chatId,!chat.pinned));add('Rename chat',()=>renameChat(chatId,chat.title||''));const divider=document.createElement('div');divider.className='menu-divider';menu.appendChild(divider);add('Delete chat',()=>deleteChat(chatId),'danger');clearTimeout(menu._t);menu.classList.add('open');const rect={width:menu.offsetWidth,height:menu.offsetHeight};menu.style.left=Math.max(8,Math.min(x,innerWidth-rect.width-8))+'px';menu.style.top=Math.max(8,Math.min(y,innerHeight-rect.height-8))+'px'}
function updateChatSelectionToolbar(){
 const bar=$('chatSelectBar');if(!bar)return;
 bar.hidden=!state.selectingChats;
 const selected=state.selectedChatIds.length;
 $('selectedChatCount').textContent=selected+' selected';
 $('deleteSelectedChats').disabled=!selected;
 const all=state.chats.length>0&&selected===state.chats.length;
 $('selectAllChats').checked=all;
 $('selectAllChats').indeterminate=selected>0&&!all;
 const selBtn=$('selectChats');
 if(selBtn){selBtn.setAttribute('aria-pressed',String(state.selectingChats));selBtn.title=state.selectingChats?'Cancel selection':'Select chats'}
}
function setChatSelectionMode(enabled){
 state.selectingChats=!!enabled;
 if(!state.selectingChats)state.selectedChatIds=[];
 updateChatSelectionToolbar();renderChats();
}
function toggleChatSelection(id,checked){
 const selected=new Set(state.selectedChatIds);
 if(checked)selected.add(id);else selected.delete(id);
 state.selectedChatIds=state.chats.map(chat=>chat.id).filter(chatId=>selected.has(chatId));
 updateChatSelectionToolbar();renderChats();
}
function selectAllChats(checked){
 state.selectedChatIds=checked?state.chats.map(chat=>chat.id):[];
 updateChatSelectionToolbar();renderChats();
}
async function deleteSelectedChats(){
 const ids=state.selectedChatIds.slice();if(!ids.length)return;
 if(!await confirmInApp('Delete selected chats',`Delete ${ids.length} selected conversation${ids.length===1?'':'s'} and their temporary workspaces? This cannot be undone.`,'Delete chats',true))return;
 if(ids.includes(state.chatId))cancelRun();
 try{
  await api('/api/chats/bulk-delete',{ids});
  if(ids.includes(state.chatId)){state.chatId=null;resetComposer();showWelcome();setTitle('New chat','')}
  state.selectedChatIds=[];state.selectingChats=false;await refreshChats();updateChatSelectionToolbar();toast('Selected chats deleted');
 }catch(error){toast(error.message)}
}

function chatDateBucket(ts){
 const t=Number(ts)||0;if(!t)return {key:'older',label:'Older',order:9999};
 const d=new Date(t*1000),now=new Date(),day=86400000;
 const startToday=new Date(now.getFullYear(),now.getMonth(),now.getDate()).getTime(),ms=d.getTime();
 if(ms>=startToday)return {key:'today',label:'Today',order:0};
 if(ms>=startToday-day)return {key:'yesterday',label:'Yesterday',order:1};
 if(ms>=startToday-7*day)return {key:'7d',label:'Previous 7 days',order:2};
 if(ms>=startToday-30*day)return {key:'30d',label:'Previous 30 days',order:3};
 const sameYear=d.getFullYear()===now.getFullYear();
 const label=d.toLocaleDateString(undefined,sameYear?{month:'long'}:{month:'long',year:'numeric'});
 return {key:'m'+d.getFullYear()+'-'+d.getMonth(),label,order:4+(now.getFullYear()-d.getFullYear())*12+(now.getMonth()-d.getMonth())};
}
function buildChatRow(chat){
 const row=document.createElement('div');row.className='chat-row'+(chat.id===state.chatId?' active':'')+(state.selectedChatIds.includes(chat.id)?' selected':'');row.dataset.chatId=chat.id;
 if(state.selectingChats){const check=document.createElement('input');check.type='checkbox';check.className='chat-row-select';check.checked=state.selectedChatIds.includes(chat.id);check.setAttribute('aria-label','Select '+(chat.title||'conversation'));check.onchange=()=>toggleChatSelection(chat.id,check.checked);row.append(check)}
 const title=document.createElement('button');title.type='button';title.className='chat-item';title.title=chat.title||'New conversation';title.textContent=chat.title||'New conversation';title.onclick=()=>state.selectingChats?toggleChatSelection(chat.id,!state.selectedChatIds.includes(chat.id)):loadChat(chat.id);
 row.append(title);
 if(!state.selectingChats){const actions=document.createElement('div');actions.className='chat-actions';const more=document.createElement('button');more.type='button';more.className='chat-more';more.title='Chat options';more.setAttribute('aria-label','Chat options');more.textContent='···';more.onclick=event=>{event.stopPropagation();const rect=more.getBoundingClientRect();openChatMenu(chat.id,rect.right-176,rect.bottom+4)};actions.append(more);row.append(actions)}
 row.addEventListener('contextmenu',event=>{event.preventDefault();openChatMenu(chat.id,event.clientX,event.clientY)});
 return row;
}
let collapsedGroups=new Set();try{collapsedGroups=new Set(JSON.parse(localStorage.getItem('kurt-collapsed-groups')||'[]'))}catch(error){}
let chatListPainted=false;
function saveCollapsedGroups(){try{localStorage.setItem('kurt-collapsed-groups',JSON.stringify(Array.from(collapsedGroups)))}catch(error){}}
function buildHeadingActions(){
 const box=document.createElement('div');box.className='heading-actions';
 const sort=document.createElement('button');sort.type='button';sort.className='heading-btn sort-btn'+(state.sort==='oldest'?' flipped':'');
 sort.title=state.sort==='oldest'?'Oldest first':'Newest first';sort.setAttribute('aria-label','Sort chats');
 sort.innerHTML='<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M8 20V5M4 9l4-4 4 4M16 4v15M12 15l4 4 4-4"/></svg>';
 sort.onclick=()=>{const list=$('chatList');list.classList.add('swapping');state.sort=state.sort==='oldest'?'newest':'oldest';setTimeout(()=>{renderChats();list.classList.remove('swapping')},150)};
 const select=document.createElement('button');select.type='button';select.id='selectChats';select.className='heading-btn';
 select.setAttribute('aria-label','Select chats');select.setAttribute('aria-pressed',String(state.selectingChats));
 select.innerHTML='<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 11l3 3 8-8M20 12v6a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h9"/></svg>';
 select.onclick=()=>setChatSelectionMode(!state.selectingChats);
 box.append(sort,select);return box;
}
function renderChats(){
 const el=$('chatList');el.replaceChildren();
 const oldest=state.sort==='oldest';
 const chats=state.chats.slice().sort((a,b)=>oldest?(a.updated||0)-(b.updated||0):(b.updated||0)-(a.updated||0));
 const sections=[];
 const pinned=chats.filter(chat=>chat.pinned);
 if(pinned.length)sections.push({key:'pinned',label:'Pinned',chats:pinned});
 const buckets=new Map();
 chats.filter(chat=>!chat.pinned).forEach(chat=>{const bucket=chatDateBucket(chat.updated);let section=buckets.get(bucket.key);if(!section){section={key:bucket.key,label:bucket.label,order:bucket.order,chats:[]};buckets.set(bucket.key,section)}section.chats.push(chat)});
 Array.from(buckets.values()).sort((a,b)=>oldest?b.order-a.order:a.order-b.order).forEach(section=>sections.push(section));
 const animate=!chatListPainted&&sections.length>0;let rowIndex=0;
 sections.forEach((section,index)=>{
  const group=document.createElement('section');group.className='chat-group'+(collapsedGroups.has(section.key)?' collapsed':'');
  const heading=document.createElement('div');heading.className='chat-date-heading';
  const toggle=document.createElement('button');toggle.type='button';toggle.className='heading-toggle';toggle.setAttribute('aria-expanded',String(!group.classList.contains('collapsed')));
  const label=document.createElement('span');label.className='heading-label';label.textContent=section.label;
  const chev=document.createElementNS('http://www.w3.org/2000/svg','svg');chev.setAttribute('class','ic chev');chev.setAttribute('aria-hidden','true');chev.innerHTML='<use href="#i-chev"/>';
  toggle.append(label,chev);
  toggle.onclick=()=>{const collapsed=group.classList.toggle('collapsed');toggle.setAttribute('aria-expanded',String(!collapsed));if(collapsed)collapsedGroups.add(section.key);else collapsedGroups.delete(section.key);saveCollapsedGroups()};
  heading.append(toggle);
  if(index===0)heading.append(buildHeadingActions());
  const body=document.createElement('div');body.className='chat-group-body';
  const inner=document.createElement('div');inner.className='chat-group-inner';
  section.chats.forEach(chat=>{const row=buildChatRow(chat);if(animate){row.classList.add('enter');row.style.animationDelay=Math.min(rowIndex,14)*30+'ms'}rowIndex++;inner.append(row)});
  body.append(inner);group.append(heading,body);el.append(group);
 });
 if(sections.length)chatListPainted=true;
 updateChatSelectionToolbar();
}
async function renameChat(id,current){const result=await inAppDialog({title:'Rename conversation',message:'Choose a clear title for this chat.',confirmText:'Rename',fields:[{name:'title',label:'Chat title',value:current,required:true,maxLength:100}]});if(!result)return;const title=result.title.trim();if(!title){toast('Chat title cannot be empty');return}try{await api('/api/chats/'+encodeURIComponent(id),{rename:title});await refreshChats();if(id===state.chatId)setTitle(title,$('chatMeta').textContent)}catch(error){toast(error.message)}}
async function togglePinChat(id,pinned){try{await api('/api/chats/'+encodeURIComponent(id),{pin:!!pinned});await refreshChats()}catch(error){toast(error.message)}}
async function deleteChat(id){const chat=state.chats.find(item=>item.id===id);if(!await confirmInApp('Delete conversation',`Delete “${chat?.title||'this chat'}” and its temporary workspace? This cannot be undone.`,'Delete chat',true))return;if(state.chatId===id)cancelRun();try{await api('/api/chats/'+encodeURIComponent(id),{delete:true});if(state.chatId===id){state.chatId=null;resetComposer();showWelcome();setTitle('New chat','')}await refreshChats();toast('Chat deleted')}catch(error){toast(error.message)}}
async function refreshBootstrap(){const data=await api('/api/bootstrap');state.models=data.models||[];state.routes=data.routes||[];state.providers=data.providers||[];state.keys=data.keys||[];state.apiGroups=data.api_groups||[];state.settings=data.settings||{};state.projectRoot=data.project_root||'';$('projectRoot').value=state.projectRoot;$('systemPrompt').value=state.settings.system_prompt||'';$('allowProjectWrites').checked=!!state.settings.allow_project_writes;fillProviders();fillModels();renderKeys();renderApiKeyChecks(currentApiKeySelection());renderApiModels();renderApiGroups();refreshFailoverMemberOptions();enhanceAllSelects()}
function fillProviders(){const providers=state.providers.map(item=>({value:item.id,label:item.label}));setSelectOptions($('keyProvider'),providers,'Choose provider')}
function renderLocalModels(){
 const root=$('localModelList');if(!root)return;root.replaceChildren();
 const models=state.models.filter(item=>item.type==='local').sort((a,b)=>(a.label||a.name).localeCompare(b.label||b.name));
 if(!models.length){root.innerHTML='<div class="empty">No local models are installed yet. Download one below.</div>';return}
 models.forEach(model=>{
  const row=document.createElement('div');row.className='local-model-row';
  const grow=document.createElement('div');grow.className='grow';
  const type=model.type==='local'?'Ollama local model':(model.provider||'API model');
  grow.innerHTML=`<b>${esc(model.label||model.name)}</b><small>${esc(model.size?String(model.size):type)}${model.current?' · currently active':''}</small>`;
  const actions=document.createElement('div');actions.className='model-actions';
  const route=state.routes.find(item=>item.value==='model|'+model.ref)||state.routes.find(item=>item.value.startsWith('keymodel|'+model.ref+'|'));
  const use=document.createElement('button');use.textContent='Use in chat';use.disabled=!route;use.onclick=()=>{if(!route)return;$('topModel').value=route.value;$('topModel').dispatchEvent(new Event('change',{bubbles:true}));activateSettingsTab('general');toast('Model selected for new chats')};
  const remove=document.createElement('button');remove.type='button';remove.className='danger';remove.textContent='Delete';remove.onclick=async()=>{
   const local=model.type==='local';
   const message=local?`Uninstall “${model.label||model.name}” from Ollama? Its downloaded model weights will be deleted.`:`Remove “${model.label||model.name}” from Kurt? Its saved API keys will remain.`;
   if(!await confirmInApp(local?'Uninstall local model':'Remove API model',message,local?'Uninstall model':'Remove model',true))return;
   try{await api('/api/models/delete',{model_ref:model.ref});await refreshBootstrap();toast(local?'Local model uninstalled':'API model removed')}catch(error){toast(error.message)}
  };
  actions.append(use,remove);row.append(grow,actions);root.append(row);
 });
}
function renderKeys(){
 const root=$('keysList');if(!root)return;root.replaceChildren();
 if(!state.keys.length){root.innerHTML='<div class="empty">No API keys saved yet.</div>';return}
 const sortMode=$('keySort')?.value||state.keySort||'name';
 const keys=state.keys.slice().sort((a,b)=>sortMode==='date'?(Number(b.created_at)||0)-(Number(a.created_at)||0):(a.label||'').localeCompare(b.label||''));
 keys.forEach(key=>{
  const row=document.createElement('div');row.className='key-row';
  const added=Number(key.created_at)?new Date(Number(key.created_at)*1000).toLocaleDateString():'Date unavailable';
  row.innerHTML=`<div class="grow"><b>${esc(key.label)}</b><small><span class="key-provider">${esc(key.provider||'Provider')}</span> · ${esc(key.masked)} · Added ${esc(added)}</small></div><button class="edit-key">Edit</button><button class="danger remove-key">Remove</button>`;
  row.querySelector('.edit-key').onclick=async()=>{
   const result=await inAppDialog({title:'Edit API key',message:'The stored key is never shown. Leave the replacement blank to keep it unchanged.',confirmText:'Save key',fields:[{name:'label',label:'Account label',value:key.label,required:true,maxLength:80},{name:'value',label:'Replacement API key',type:'password',value:'',placeholder:'Leave blank to keep current'}]});
   if(!result)return;
   try{await api('/api/settings',{key_action:'edit',id:key.id,label:result.label,value:result.value});await refreshBootstrap();toast('API key updated')}catch(error){toast(error.message)}
  };
  row.querySelector('.remove-key').onclick=async()=>{
   if(!await confirmInApp('Remove API key',`Remove “${key.label}” from local storage?`,'Remove',true))return;
   try{await api('/api/settings',{key_action:'remove',id:key.id});await refreshBootstrap();toast('API key removed')}catch(error){toast(error.message)}
  };
  root.append(row);
 });
}
function providerName(id){const p=(state.providers||[]).find(item=>item.id===String(id||'').toLowerCase());return p?p.label:(id||'Provider')}
function currentApiKeySelection(){return Array.from(document.querySelectorAll('#apiModelKeys input:checked')).map(input=>input.value)}
function syncApiKeyChecks(){
 const boxes=Array.from(document.querySelectorAll('#apiModelKeys input'));
 const chosen=boxes.find(box=>box.checked);const provider=chosen?chosen.dataset.provider:'';
 boxes.forEach(box=>{box.disabled=!!provider&&box.dataset.provider!==provider;box.parentElement.classList.toggle('disabled',box.disabled)});
}
function renderApiKeyChecks(selected=[]){
 const root=$('apiModelKeys');if(!root)return;root.replaceChildren();
 if(!state.keys.length){root.innerHTML='<div class="empty">Add an API key first under API keys.</div>';return}
 state.keys.slice().sort((a,b)=>(a.label||'').localeCompare(b.label||'')).forEach(key=>{
  const label=document.createElement('label');label.className='key-check';
  const input=document.createElement('input');input.type='checkbox';input.value=key.id;input.dataset.provider=String(key.provider||'').toLowerCase();input.checked=selected.includes(key.id);input.onchange=syncApiKeyChecks;
  const text=document.createElement('span');text.innerHTML='<b>'+esc(key.label)+'</b> <small>'+esc(providerName(key.provider))+' · '+esc(key.masked)+'</small>';
  label.append(input,text);root.append(label);
 });
 syncApiKeyChecks();
}
function openApiModelForm(group){
 state.editingApiModel=group?group.id:null;
 $('apiModelFormTitle').textContent=group?'Edit model':'Add model';
 $('addApiModel').textContent=group?'Save changes':'Save model';
 $('apiModelId').value=group?(group.model||''):'';$('apiModelLabel').value=group&&group.label&&group.label!==group.model?group.label:'';
 let selected=[];
 if(group){selected=Array.isArray(group.key_ids)?group.key_ids.slice():(group.key_id&&group.key_id!=='pool'?[group.key_id]:state.keys.filter(k=>String(k.provider||'').toLowerCase()===String(group.provider||'').toLowerCase()).map(k=>k.id))}
 renderApiKeyChecks(selected);$('apiModelCard').hidden=false;$('apiModelId').focus();
}
function closeApiModelForm(){state.editingApiModel=null;$('apiModelCard').hidden=true;$('apiModelId').value='';$('apiModelLabel').value='';renderApiKeyChecks([])}
function renderApiModels(){
 const root=$('apiModelList');if(!root)return;root.replaceChildren();
 const groups=(state.apiGroups||[]).filter(item=>item.source==='api').sort((a,b)=>String(a.label||a.model||'').localeCompare(String(b.label||b.model||'')));
 if(!groups.length){root.innerHTML='<div class="empty">No API models yet. Press Add model, select keys, and enter a model name or ID.</div>';return}
 groups.forEach(group=>{
  const row=document.createElement('div');row.className='local-model-row';
  const ids=Array.isArray(group.key_ids)?group.key_ids:(group.key_id&&group.key_id!=='pool'?[group.key_id]:null);
  const names=ids===null?['all '+providerName(group.provider)+' keys']:ids.map(id=>(state.keys.find(k=>k.id===id)||{}).label).filter(Boolean);
  const grow=document.createElement('div');grow.className='grow';
  grow.innerHTML='<b>'+esc(group.label||group.model)+'</b><small>'+esc(group.model)+' · '+esc(providerName(group.provider))+' · '+esc(names.length?names.join(', '):'no key selected')+'</small>';
  const actions=document.createElement('div');actions.className='model-actions';
  const route=state.routes.find(item=>item.value==='model|'+group.model_ref)||state.routes.find(item=>item.value.startsWith('keymodel|'+group.model_ref+'|'));
  const use=document.createElement('button');use.type='button';use.textContent='Use in chat';use.disabled=!route;use.onclick=()=>{if(!route)return;$('topModel').value=route.value;$('topModel').dispatchEvent(new Event('change',{bubbles:true}));activateSettingsTab('general');toast('Model selected for new chats')};
  const edit=document.createElement('button');edit.type='button';edit.textContent='Edit';edit.onclick=()=>openApiModelForm(group);
  const remove=document.createElement('button');remove.type='button';remove.className='danger';remove.textContent='Delete';remove.onclick=async()=>{
   if(!await confirmInApp('Remove API model','Remove “'+(group.label||group.model)+'” from Kurt? Your saved keys will remain.','Remove model',true))return;
   try{await api('/api/models/delete',{model_ref:group.model_ref});await refreshBootstrap();toast('API model removed')}catch(error){toast(error.message)}
  };
  actions.append(use,edit,remove);row.append(grow,actions);root.append(row);
 });
}
async function saveApiModel(){
 const model=$('apiModelId').value.trim(),label=$('apiModelLabel').value.trim(),key_ids=currentApiKeySelection();
 if(!key_ids.length){toast('Select at least one key');return}
 if(!model){toast('Enter the model name or ID');return}
 const editing=state.editingApiModel?(state.apiGroups||[]).find(item=>item.id===state.editingApiModel):null;
 const body={api_group_action:editing?'edit':'add',source:'api',name:(label||model).slice(0,60),label:label||model,model,key_ids,key_group:'default'};
 if(editing){body.id=editing.id;if(editing.base_url)body.base_url=editing.base_url}
 try{await api('/api/settings',body);closeApiModelForm();await refreshBootstrap();toast(editing?'API model updated':'API model added')}catch(error){toast(error.message)}
}
function priorityText(index){return index===0?'1 · Priority':(index+1)+' · Fallback'}
function applyPriorityBadges(container){Array.from(container.querySelectorAll('.priority-badge')).forEach((badge,index)=>{badge.textContent=priorityText(index);badge.classList.toggle('first',index===0)})}
function makeSortable(container,itemSelector,onDrop){
 let dragged=null;
 container.addEventListener('dragstart',event=>{const item=event.target.closest(itemSelector);if(!item||!container.contains(item))return;dragged=item;item.classList.add('dragging');event.dataTransfer.effectAllowed='move';try{event.dataTransfer.setData('text/plain','kurt-model')}catch(error){}});
 container.addEventListener('dragover',event=>{if(!dragged)return;event.preventDefault();const target=event.target.closest(itemSelector);if(!target||target===dragged||!container.contains(target))return;const rect=target.getBoundingClientRect();container.insertBefore(dragged,event.clientY>rect.top+rect.height/2?target.nextSibling:target)});
 container.addEventListener('drop',event=>{if(dragged)event.preventDefault()});
 container.addEventListener('dragend',()=>{if(!dragged)return;dragged.classList.remove('dragging');if(dragged.dataset.handleDrag)dragged.draggable=false;dragged=null;applyPriorityBadges(container);if(onDrop)onDrop()});
}
function memberRouteValue(member){
 const model=String(member?.model||'');if(!model)return '';
 const key=String(member?.key_id||'pool');
 if(key==='pool'&&(state.routes||[]).some(route=>route.value==='model|'+model))return 'model|'+model;
 return 'keymodel|'+model+'|'+key;
}
function memberFromRouteValue(value){
 const parts=String(value||'').split('|');
 if(parts[0]==='model'&&parts[1])return {model:parts[1],key_id:'pool'};
 if(parts[0]==='keymodel'&&parts[1]&&parts[2])return {model:parts[1],key_id:parts[2]};
 return null;
}
function moveFailoverMember(row,delta){
 const root=$('failoverMembers');const rows=Array.from(root.children);const index=rows.indexOf(row);const target=index+delta;
 if(index<0||target<0||target>=rows.length)return;
 root.insertBefore(row,delta<0?rows[target]:rows[target].nextSibling);applyPriorityBadges(root);
}
function addFailoverMember(member={}){
 const root=$('failoverMembers');if(!root)return;
 if(root.children.length>=12){toast('A group can contain at most 12 models');return}
 const row=document.createElement('div');row.className='failover-member-row';
 const handle=document.createElement('span');handle.className='drag-handle';handle.textContent='⋮⋮';handle.title='Drag to change priority';
 handle.addEventListener('mousedown',()=>{row.draggable=true;row.dataset.handleDrag='1'});handle.addEventListener('mouseup',()=>{row.draggable=false});
 const badge=document.createElement('span');badge.className='priority-badge';
 const select=document.createElement('select');select.className='member-route';select.setAttribute('aria-label','Local or API model');
 const locals=(state.routes||[]).filter(route=>String(route.value).startsWith('model|'));
 const keyed=(state.routes||[]).filter(route=>String(route.value).startsWith('keymodel|'));
 const placeholder=document.createElement('option');placeholder.value='';placeholder.disabled=true;placeholder.textContent=(locals.length||keyed.length)?'Choose a local or API model':'No local or API models available';select.appendChild(placeholder);
 const addGroup=(label,routes)=>{if(!routes.length)return;const group=document.createElement('optgroup');group.label=label;routes.forEach(route=>{const option=document.createElement('option');option.value=route.value;option.textContent=routeChoiceLabel(route.value);option.title=route.label;group.appendChild(option)});select.appendChild(group)};
 addGroup('Local models',locals);addGroup('API models',keyed);
 const current=memberRouteValue(member);
 if(current&&!Array.from(select.options).some(option=>option.value===current)){
  const model=state.models.find(item=>item.ref===member.model);const name=model?.label||model?.name||member.model;
  const option=document.createElement('option');option.value=current;option.textContent=name+(String(member.key_id||'pool')==='pool'?' · all keys':' · unavailable');select.appendChild(option);
 }
 select.value=current||'';
 const move=document.createElement('div');move.className='member-move';
 const up=document.createElement('button');up.type='button';up.textContent='▲';up.title='Raise priority';up.onclick=()=>moveFailoverMember(row,-1);
 const down=document.createElement('button');down.type='button';down.textContent='▼';down.title='Lower priority';down.onclick=()=>moveFailoverMember(row,1);
 move.append(up,down);
 const remove=document.createElement('button');remove.type='button';remove.className='remove-member';remove.textContent='×';remove.title='Remove model';remove.onclick=()=>{row.remove();applyPriorityBadges(root)};
 row.append(handle,badge,select,move,remove);root.appendChild(row);
 enhanceSelect(select);applyPriorityBadges(root);
}
function refreshFailoverMemberOptions(){
 const root=$('failoverMembers');if(!root||!root.children.length)return;
 const members=Array.from(root.querySelectorAll('.member-route')).map(select=>memberFromRouteValue(select.value)).filter(Boolean);
 root.replaceChildren();members.forEach(member=>addFailoverMember(member));
 if(!root.children.length)addFailoverMember();
}
function resetFailoverForm(){
 state.editingGroup=null;$('failoverGroupName').value='';$('failoverMembers').replaceChildren();
 addFailoverMember();
 $('saveFailoverGroup').textContent='Create group';$('cancelFailoverGroup').hidden=true;
}
function editFailoverGroup(id){
 const group=state.apiGroups.find(item=>item.id===id&&item.source==='failover');if(!group)return;
 state.editingGroup=id;$('failoverGroupName').value=group.name||'';$('failoverMembers').replaceChildren();
 (group.members||[]).forEach(member=>addFailoverMember(member));
 if(!$('failoverMembers').children.length)addFailoverMember();
 $('saveFailoverGroup').textContent='Save changes';$('cancelFailoverGroup').hidden=false;
 activateSettingsTab('groups');$('failoverGroupName').focus();
}
function renderApiGroups(){
 const root=$('apiGroupsList');if(!root)return;root.replaceChildren();
 const groups=state.apiGroups.filter(item=>item.source==='failover').slice().sort((a,b)=>(a.name||'').localeCompare(b.name||''));
 if(!groups.length){root.innerHTML='<div class="empty">No groups yet. Create one above and add local or API models.</div>';return}
 groups.forEach(group=>{
  const card=document.createElement('div');card.className='pool-card';
  const head=document.createElement('div');head.className='pool-head';
  const info=document.createElement('div');info.className='grow';
  const descriptions=(group.members||[]).map(member=>{
   const model=state.models.find(item=>item.ref===member.model);const name=model?.label||model?.name||member.model;
   if(!model)return name+' (unavailable)';
   if(model.type==='local')return name+' · local';
   const key=state.keys.find(item=>item.id===member.key_id);
   return name+' · '+(member.key_id&&member.key_id!=='pool'?(key?.label||'selected key'):'key pool');
  });
  info.innerHTML='<b>'+esc(group.name)+'</b><small>'+esc((group.members||[]).length+' model(s) · first is the priority, the rest are fallbacks')+'</small>';
  const actions=document.createElement('div');actions.className='pool-actions';
  const edit=document.createElement('button');edit.textContent='Edit';edit.onclick=()=>editFailoverGroup(group.id);
  const remove=document.createElement('button');remove.className='danger';remove.textContent='Remove';remove.onclick=async()=>{
   if(!await confirmInApp('Remove group','Remove “'+group.name+'”? Its models and API keys will remain available.','Remove',true))return;
   try{await api('/api/settings',{failover_action:'remove',id:group.id});await refreshBootstrap();resetFailoverForm();toast('Group removed')}catch(error){toast(error.message)}
  };
  actions.append(edit,remove);head.append(info,actions);card.append(head);
  const list=document.createElement('div');list.className='group-members';
  (group.members||[]).forEach((member,index)=>{
   const chip=document.createElement('div');chip.className='group-member';chip.draggable=true;chip.dataset.model=member.model;chip.dataset.key=member.key_id||'pool';
   chip.innerHTML='<span class="drag-handle" aria-hidden="true">⋮⋮</span><span class="priority-badge"></span><span class="grow">'+esc(descriptions[index]||member.model)+'</span>';
   list.append(chip);
  });
  applyPriorityBadges(list);
  makeSortable(list,'.group-member',async()=>{
   const members=Array.from(list.children).map(chip=>({model:chip.dataset.model,key_id:chip.dataset.key}));
   try{await api('/api/settings',{failover_action:'edit',id:group.id,name:group.name,members});await refreshBootstrap();toast('Priority order saved')}catch(error){toast(error.message);await refreshBootstrap()}
  });
  card.append(list);root.append(card);
 });
}
async function saveFailoverGroup(){
 const name=$('failoverGroupName').value.trim();
 const members=Array.from($('failoverMembers').querySelectorAll('.member-route')).map(select=>memberFromRouteValue(select.value)).filter(Boolean);
 if(!name){toast('Enter a group name');return}
 if(members.length<1){toast('Add at least one model');return}
 try{
  await api('/api/settings',{failover_action:state.editingGroup?'edit':'add',id:state.editingGroup,name,members});
  await refreshBootstrap();resetFailoverForm();toast('Failover group saved');
 }catch(error){toast(error.message)}
}
async function refreshChats(){const d=await api('/api/chats');state.chats=d.chats||[];renderChats()}
function resetComposer(){$('prompt').value='';autosize();state.selectedFiles=[];state.selectedFolders=[];state.pendingUploads.forEach(item=>{if(item.previewUrl)URL.revokeObjectURL(item.previewUrl)});state.uploadedFiles.forEach(item=>{if(item.previewUrl)URL.revokeObjectURL(item.previewUrl)});state.pendingUploads=[];state.uploadedFiles=[];state.contextCount=0;renderPendingUploads();hint();updateSend()}
async function ensureChat(initialMessage){
 if(!state.chatId){const body={create:true,mode:state.mode,effort:state.effort};if(initialMessage)body.initial_message=initialMessage;const d=await api('/api/chats',body);state.chatId=d.id}
 else if(initialMessage){await api('/api/chats/'+encodeURIComponent(state.chatId),{append_user_message:initialMessage})}
 else{await api('/api/chats/'+encodeURIComponent(state.chatId),{mode:state.mode,effort:state.effort})}
 return state.chatId;
}
async function newChat(){cancelRun();state.chatId=null;state.selectingChats=false;state.selectedChatIds=[];updateChatSelectionToolbar();resetComposer();const preferred=state.settings.default_route||'';if(preferred&&Array.from($('topModel').options).some(option=>option.value===preferred)){$('topModel').value=preferred}renderRoutePicker();showWelcome();setTitle('New chat','');renderChats();follow=true;$('messages').scrollTop=0;document.body.classList.remove('nav-open');$('prompt').focus()}
async function loadChat(id){cancelRun();clearArtifactPane();closeChatMenu();state.uploadedFiles.forEach(item=>{if(item.previewUrl)URL.revokeObjectURL(item.previewUrl)});state.uploadedFiles=[];state.pendingUploads.forEach(item=>{if(item.previewUrl)URL.revokeObjectURL(item.previewUrl)});state.pendingUploads=[];state.selectedFiles=[];state.selectedFolders=[];const data=await api('/api/chats/'+encodeURIComponent(id));state.chatId=id;state.contextCount=0;state.effort=data.effort==='expert'?'expert':'thinking';state.mode=data.mode||'normal';$('effortSelect').value=state.effort;$('operationMode').value=state.mode;syncSelect($('effortSelect'));syncSelect($('operationMode'));if(data.route){$('topModel').value=data.route}renderRoutePicker();try{const context=await api('/api/context/status?chat_id='+encodeURIComponent(id));state.contextCount=context.files||0;state.uploadedFiles=context.items||[]}catch(error){}renderPendingUploads();hint();$('messages').replaceChildren();const messages=data.messages||[];if(messages.length){setEmpty(false);messages.forEach(message=>renderMessage(message.role,message.content,message.artifacts||[],{worklog:message.worklog||[],reasoning:message.reasoning||'',thinkingSeconds:message.thinking_seconds||0,stats:message.stats||null,model:message.model||data.model||null}));follow=true;scrollEnd(true)}else showWelcome();loadUploadedImagePreviews();setTitle(data.title||'Conversation',[data.model||'',state.effort+' · '+state.mode].filter(Boolean).join(' · '));renderChats();document.body.classList.remove('nav-open')}
async function send(){
 const text=$('prompt').value.trim();if(!text||state.busy)return;
 const run=++state.run;state.effort=$('effortSelect').value;state.mode=$('operationMode').value;const route=$('topModel').value;
 const messageId=(crypto.randomUUID?crypto.randomUUID():Date.now().toString(36)+Math.random().toString(36).slice(2));
 setBusy(true);
 try{await ensureChat({id:messageId,content:text,effort:state.effort,mode:state.mode,route})}catch(error){if(run===state.run)setBusy(false);toast(error.message);return}
 if(run!==state.run)return;
 $('prompt').value='';autosize();hint();
 if($('main').classList.contains('empty')){setEmpty(false);$('messages').replaceChildren()}
 follow=true;renderMessage('user',text);const node=renderMessage('assistant','');node.classList.add('live');node.querySelector('.assistant-status').textContent='Connecting…';scrollEnd(true);
 $('chatMeta').textContent=state.effort+' · '+state.mode+' · Generating…';
 await refreshChats().catch(error=>console.warn('Could not refresh the chat list before generation:',error));
 const contentEl=node.querySelector('.content');let raw='',reasoning='',pending=false,thinkStart=0;setReasoning(node,'Preparing a public explanation of the approach and checks…',true);
 const controller=new AbortController();state.abort=controller;
 const paint=()=>{pending=false;if(run!==state.run)return;const parsed=splitResponseSummary(raw,true);if(parsed.summary)setReasoning(node,parsed.summary,true);const safe=stripUnsupportedActions(parsed.answer);contentEl.innerHTML=safe?renderMarkdown(safe):'';if(safe){bindCopy(node);typesetMath(contentEl)}node.dataset.raw=safe;scrollEnd()};
 const queue=()=>{if(!pending){pending=true;requestAnimationFrame(paint)}};
 try{
  const response=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json','X-Kurt-Token':TOKEN},body:JSON.stringify({chat_id:state.chatId,text,route,effort:state.effort,mode:state.mode,message_id:messageId}),signal:controller.signal});
  if(!response.ok){const result=await response.json().catch(()=>({}));throw new Error(result.error||response.statusText)}
  node.querySelector('.assistant-status').textContent='Thinking…';const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='';
  while(true){const packet=await reader.read();if(packet.done)break;if(run!==state.run)return;buffer+=decoder.decode(packet.value,{stream:true});let separator;
   while((separator=buffer.indexOf('\n\n'))>=0){const frame=buffer.slice(0,separator);buffer=buffer.slice(separator+2);const line=frame.split('\n').find(value=>value.startsWith('data:'));if(!line)continue;const event=JSON.parse(line.slice(5).trim());
    if(event.type==='phase'){pushWorkStep(node,event.text,event.ok,event.detail);node.querySelector('.assistant-status').textContent=event.status||'Working…'}
    else if(event.type==='reasoning'){if(!thinkStart)thinkStart=performance.now();reasoning+=event.text||'';setReasoning(node,reasoning,true)}
    else if(event.type==='reasoning_reset'){reasoning=event.text||'';setReasoning(node,reasoning,!!reasoning)}
    else if(event.type==='token'){raw+=event.text||'';node.querySelector('.assistant-status').textContent='Writing…';queue()}
    else if(event.type==='done'){pending=false;raw=event.content||'';const parsed=splitResponseSummary(raw,false);reasoning=event.reasoning||reasoning||parsed.summary;const safe=stripUnsupportedActions(parsed.answer);contentEl.innerHTML=safe?renderMarkdown(safe):'';bindCopy(node);typesetMath(contentEl);node.dataset.raw=safe;setReasoning(node,reasoning||'No separate rationale was returned. The full activity log below contains the actions recorded by the application.',false,event.thinking_seconds||(thinkStart?(performance.now()-thinkStart)/1000:0));setThink(node,event.worklog||node.__worklog||[]);setResponseMeta(node,event.stats,event.model);renderArtifactTriggers(node.querySelector('.message-artifacts'),event.artifacts||[]);setTitle(event.title||'Conversation',[event.model||'',event.effort||state.effort,event.mode||state.mode].filter(Boolean).join(' · '));scrollEnd();await refreshChats().catch(error=>console.warn('Could not refresh the chat list after generation:',error))}
    else if(event.type==='error')throw new Error(event.message||'The model request failed');
   }
  }
 }catch(error){
  if(run===state.run){if(error.name==='AbortError'){paint();refreshChats().catch(()=>{})}else{contentEl.classList.add('err');contentEl.textContent='Error: '+error.message;toast(error.message)}}
 }finally{
  node.classList.remove('live');endThink(node);
  if(run===state.run){setBusy(false);state.abort=null;const meta=$('chatMeta');if(meta.textContent.includes('Generating…'))meta.textContent=[state.effort,state.mode,'Ready'].join(' · ')}
 }
}
$('messages').addEventListener('scroll',()=>{const m=$('messages');follow=m.scrollHeight-m.scrollTop-m.clientHeight<60;$('toBottom').classList.toggle('show',!follow&&!$('main').classList.contains('empty'))});$('toBottom').onclick=()=>{follow=true;scrollEnd(true);$('toBottom').classList.remove('show')};
$('closeArtifactPane').onclick=closeArtifactPane;
$('sendBtn').onclick=()=>{if(state.busy){if(state.abort)state.abort.abort()}else send()};
$('prompt').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&e.keyCode!==229){e.preventDefault();if(!state.busy)send()}});
$('prompt').addEventListener('input',()=>{autosize();updateSend()});
$('prompt').addEventListener('paste',event=>{
 if(state.mode!=='code'&&Array.from(event.clipboardData?.items||[]).some(item=>String(item.type||'').startsWith('image/'))){event.preventDefault();toast('Normal mode cannot read pasted files or images. Switch to Code mode first.');return}
 const clipboardItems=Array.from(event.clipboardData?.items||[]);const pastedImages=clipboardItems.filter(item=>String(item.type||'').startsWith('image/')).map(item=>item.getAsFile()).filter(Boolean);
 if(pastedImages.length){event.preventDefault();stageUploadFiles(pastedImages);attachFiles();return}
 const pastedText=event.clipboardData?.getData('text/plain')||'';
 if(pastedText.length>16000&&state.mode==='code'){event.preventDefault();const stamp=new Date().toISOString().replace(/[:.]/g,'-');const file=new File([pastedText],'pasted-text-'+stamp+'.txt',{type:'text/plain'});if(file.size>10*1024*1024){toast('This pasted text exceeds the 10 MB attachment limit. Split it into smaller parts.');return}stageUploadFiles([file]);$('prompt').value='I attached a large pasted text file. Please read it and help me with its contents.';autosize();updateSend();toast('Large paste attached as a .txt file');attachFiles()}
 });
$('newChat').onclick=newChat;$('selectAllChats').onchange=()=>selectAllChats($('selectAllChats').checked);$('deleteSelectedChats').onclick=deleteSelectedChats;$('cancelChatSelect').onclick=()=>setChatSelectionMode(false);$('effortSelect').onchange=()=>{state.effort=$('effortSelect').value};$('operationMode').onchange=async()=>{state.mode=$('operationMode').value;hint();if(state.busy)cancelRun();if(state.mode!=='code'){state.selectedFiles=[];state.selectedFolders=[];state.pendingUploads.forEach(item=>{if(item.previewUrl)URL.revokeObjectURL(item.previewUrl)});state.uploadedFiles.forEach(item=>{if(item.previewUrl)URL.revokeObjectURL(item.previewUrl)});state.pendingUploads=[];state.uploadedFiles=[];state.contextCount=0;renderPendingUploads();$('filesModal').classList.remove('show')}if(state.chatId){try{await api('/api/chats/'+encodeURIComponent(state.chatId),{mode:state.mode,effort:state.effort});if(state.mode==='code'){const context=await api('/api/context/status?chat_id='+encodeURIComponent(state.chatId));state.contextCount=context.files||0;state.uploadedFiles=context.items||[];renderPendingUploads();loadUploadedImagePreviews()}}catch(error){toast(error.message)}}hint()};
function activateSettingsTab(tab){document.querySelectorAll('[data-settings-tab]').forEach(button=>button.classList.toggle('active',button.dataset.settingsTab===tab));document.querySelectorAll('[data-settings-panel]').forEach(panel=>panel.classList.toggle('active',panel.dataset.settingsPanel===tab));const area=document.querySelector('.settings-panel-area');if(area)area.scrollTop=0}
document.querySelectorAll('[data-settings-tab]').forEach(b=>b.onclick=()=>activateSettingsTab(b.dataset.settingsTab));
async function openSettings(){await refreshBootstrap();activateSettingsTab('general');$('settingsModal').classList.add('show')}
async function saveSettings(){const payload={project_root:$('projectRoot').value,system_prompt:$('systemPrompt').value,allow_project_writes:$('allowProjectWrites').checked,default_route:$('settingsModel').value};const data=await api('/api/settings',payload);state.settings=data.settings||{};state.settings.allow_project_writes=$('allowProjectWrites').checked;state.projectRoot=data.project_root;await refreshBootstrap();toast('Settings saved')}
async function browseFiles(){if(state.mode!=='code'){toast('Switch this conversation to Code mode to browse project files.');return}await ensureChat();const path=$('fileFolder').value.trim();const d=await api('/api/files?chat_id='+encodeURIComponent(state.chatId)+'&path='+encodeURIComponent(path));$('fileFolder').value=d.path;const el=$('filePicker');el.innerHTML='';if(d.parent!==null){const b=document.createElement('div');b.className='file-row folder';b.innerHTML='<span>↰</span><label>Parent folder</label>';b.onclick=()=>{$('fileFolder').value=d.parent;browseFiles()};el.appendChild(b)}d.items.forEach(item=>{const row=document.createElement('div');row.className='file-row'+(item.directory?' folder':'');if(item.directory){row.innerHTML=`<span>📁</span><label>${esc(item.name)}/</label><span class="inline-note">Open</span>`;row.onclick=()=>{$('fileFolder').value=item.path;browseFiles()}}else{row.innerHTML=`<input type="checkbox" value="${esc(item.path)}" ${state.selectedFiles.includes(item.path)?'checked':''}><label>${esc(item.name)}</label><small>${esc(item.size)}</small>`;const check=row.querySelector('input');check.onchange=()=>{if(check.checked&&!state.selectedFiles.includes(item.path))state.selectedFiles.push(item.path);else if(!check.checked)state.selectedFiles=state.selectedFiles.filter(x=>x!==item.path);renderPendingUploads()}}el.appendChild(row)})}
function formatFileSize(value){const bytes=Math.max(0,Number(value)||0);if(bytes<1024)return bytes+' B';if(bytes<1024*1024)return (bytes/1024).toFixed(1)+' KB';return (bytes/1024/1024).toFixed(1)+' MB'}
function fileIconNode(item){if(item.previewUrl&&String(item.type||item.mime||'').startsWith('image/')){const image=document.createElement('img');image.className='attachment-thumb';image.src=item.previewUrl;image.alt=item.name||'Uploaded image';return image}const icon=document.createElement('span');icon.className='attachment-file-icon';icon.innerHTML='<svg class="ic" aria-hidden="true"><use href="#i-file"/></svg>';return icon}
function makeUploadCard(item,{uploaded=false,removable=false,compact=false,index=-1}={}){
 const row=document.createElement('div');row.className='upload-item'+(compact?' attachment-chip':'');
 row.appendChild(fileIconNode(item));const info=document.createElement('span');info.className='attachment-info';
 const name=document.createElement('span');name.className='attachment-name';name.textContent=item.name||String(item.path||'').split('/').pop()||'Uploaded file';name.title=item.path||name.textContent;
 const meta=document.createElement('span');meta.className='attachment-meta';meta.textContent=[formatFileSize(item.size),item.folderRoot?'Folder upload':''].filter(Boolean).join(' · ');
 const status=document.createElement('span');status.className='attachment-status';status.textContent=uploaded?'Added to this chat':'Ready to add';
 info.append(name,meta,status);row.appendChild(info);
 if(removable){const remove=document.createElement('button');remove.type='button';remove.className='attachment-remove';remove.textContent='×';remove.title='Remove attachment';remove.setAttribute('aria-label','Remove '+name.textContent);remove.onclick=()=>{const removed=state.pendingUploads.splice(index,1)[0];if(removed?.previewUrl)URL.revokeObjectURL(removed.previewUrl);renderPendingUploads()};row.appendChild(remove)}
 return row
}
function renderComposerAttachments(){const root=$('composerAttachments');if(!root)return;root.replaceChildren();const pending=state.pendingUploads||[],uploaded=state.uploadedFiles||[];pending.forEach((item,index)=>root.appendChild(makeUploadCard(item,{removable:true,index,compact:true})));uploaded.forEach(item=>root.appendChild(makeUploadCard(item,{uploaded:true,compact:true})));root.hidden=!pending.length&&!uploaded.length}
function renderPendingUploads(){const root=$('uploadList');if(!root)return;root.replaceChildren();
 const pending=state.pendingUploads||[],uploaded=state.uploadedFiles||[];const projectCount=state.selectedFiles.length+state.selectedFolders.length;
 if(!pending.length&&!projectCount&&!uploaded.length){const empty=document.createElement('div');empty.className='upload-empty';empty.textContent='No files selected.';root.appendChild(empty)}
 if(pending.length){const label=document.createElement('div');label.className='upload-section-label';label.textContent='Ready to add';root.appendChild(label);pending.forEach((item,index)=>root.appendChild(makeUploadCard(item,{removable:true,index}))) }
 if(state.selectedFiles.length||state.selectedFolders.length){const label=document.createElement('div');label.className='upload-section-label';label.textContent='From project workspace';root.appendChild(label);state.selectedFolders.forEach(path=>{const card=makeUploadCard({name:path.split('/').pop()||'Project folder',path,size:0,folderRoot:path},{removable:false});root.appendChild(card)});state.selectedFiles.forEach(path=>root.appendChild(makeUploadCard({name:path.split('/').pop(),path,size:0},{removable:false}))) }
 if(uploaded.length){const label=document.createElement('div');label.className='upload-section-label';label.textContent='Already in this chat';root.appendChild(label);uploaded.forEach(item=>root.appendChild(makeUploadCard(item,{uploaded:true}))) }
 renderComposerAttachments()
}
function stageUploadFiles(files){for(const file of files||[]){const relative=String(file.webkitRelativePath||file.name).replace(/\\/g,'/');const folderRoot=file.webkitRelativePath?relative.split('/')[0]:'';if(state.pendingUploads.some(x=>x.path===relative))continue;const previewUrl=String(file.type||'').startsWith('image/')?URL.createObjectURL(file):null;state.pendingUploads.push({file,path:relative,name:file.name,size:file.size,type:file.type||'',folderRoot,previewUrl})}renderPendingUploads()}
function stageLocalFiles(input){stageUploadFiles(Array.from(input.files||[]));input.value=''}
function bytesToBase64(buffer){const bytes=new Uint8Array(buffer);let binary='';const chunk=0x8000;for(let i=0;i<bytes.length;i+=chunk)binary+=String.fromCharCode(...bytes.subarray(i,i+chunk));return btoa(binary)}
async function loadUploadedImagePreviews(){const items=(state.uploadedFiles||[]).filter(item=>item.image&&!item.previewUrl);await Promise.all(items.map(async item=>{try{const url='/api/context/preview?chat_id='+encodeURIComponent(state.chatId)+'&path='+encodeURIComponent(item.path);const response=await fetch(url,{headers:{'X-Kurt-Token':TOKEN}});if(!response.ok)return;item.previewUrl=URL.createObjectURL(await response.blob())}catch(error){console.warn('Could not load an attachment preview:',error)}}));renderPendingUploads()}
async function attachFiles(){try{if(state.mode!=='code'){toast('Normal mode cannot attach or read local files. Switch to Code mode first.');return}if(!state.selectedFiles.length&&!state.selectedFolders.length&&!state.pendingUploads.length){toast('Choose files first');return}const uploads=[];let total=0;for(const item of state.pendingUploads){if(item.file.size>10*1024*1024)throw new Error(item.file.name+' exceeds the 10 MB per-file limit.');total+=item.file.size;if(total>20*1024*1024)throw new Error('Local file selection exceeds the 20 MB import limit.');uploads.push({path:item.path,folder_root:item.folderRoot||'',content:bytesToBase64(await item.file.arrayBuffer())})}await ensureChat();const previewMap=new Map([...(state.uploadedFiles||[]),...state.pendingUploads].filter(item=>item.previewUrl).map(item=>[item.path,item.previewUrl]));const result=await api('/api/context/import',{chat_id:state.chatId,selected_files:state.selectedFiles,selected_folders:state.selectedFolders,uploads});state.contextCount=Number(result.items?.length??((state.contextCount||0)+Number(result.files||0)));state.uploadedFiles=(result.items||[]).map(item=>({...item,previewUrl:previewMap.get(item.path)||null}));state.selectedFiles=[];state.selectedFolders=[];state.pendingUploads.forEach(item=>{if(item.previewUrl&&!state.uploadedFiles.some(uploaded=>uploaded.previewUrl===item.previewUrl))URL.revokeObjectURL(item.previewUrl)});state.pendingUploads=[];renderPendingUploads();$('filesModal').classList.remove('show');hint();await refreshChats();loadUploadedImagePreviews();toast(result.message||'Files added to this chat')}catch(e){toast(e.message)}}
function openComposerFilePicker(){if(state.mode!=='code'){toast('File attachments are available only in Code mode. Switch modes to continue.');return}const picker=$('composerFiles');if(!picker)return;picker.click()}
async function handleComposerFiles(){const picker=$('composerFiles');if(!picker.files||!picker.files.length)return;stageLocalFiles(picker);await attachFiles()}
async function startDownload(){const model=$('downloadModel').value.trim();if(!model){toast('Enter a model ID, repository or URL');return}try{const d=await api('/api/download',{model,source:$('downloadSource').value});$('downloadCaption').textContent='Queued: '+model;$('localDownloadStatus').textContent='Download queued: '+model;$('downloadModel').value='';toast('Download started in background');pollDownload()}catch(e){toast(e.message)}}
async function pollDownload(){try{const d=await api('/api/download/status');const job=d.job;if(!job)return;const percent=job.total?job.completed/job.total*100:0;$('downloadFill').style.width=(job.state==='done'?100:percent)+'%';$('downloadCaption').textContent=job.state==='running'?`${job.model} · ${percent.toFixed(1)}% · ${job.human}`:`${job.model} · ${job.state}${job.error?' · '+job.error:''}`;if(job.state==='done'){await refreshBootstrap();$('localDownloadStatus').textContent=job.model+' installed successfully.';toast('Model download complete')}else if(job.state==='error'){$('localDownloadStatus').textContent=job.error||'Download failed';toast(job.error||'Download failed')}else{$('localDownloadStatus').textContent=job.model+' · '+percent.toFixed(1)+'% · '+job.human;setTimeout(pollDownload,1000)}}catch(e){console.error(e)}}
$('sidebarSettings').onclick=openSettings;$('contextBtn').onclick=openComposerFilePicker;$('composerFiles').onchange=handleComposerFiles;$('topModel').onchange=async()=>{renderRoutePicker();try{await api('/api/settings',{default_route:$('topModel').value});$('settingsModel').value=$('topModel').value;syncSelect($('settingsModel'));state.settings.default_route=$('topModel').value;toast('Chat route selected')}catch(error){toast(error.message)}};$('settingsModel').onchange=()=>{$('topModel').value=$('settingsModel').value;$('topModel').dispatchEvent(new Event('change',{bubbles:true}))};$('saveSettings').onclick=saveSettings;$('reloadSettings').onclick=refreshBootstrap;$('addKey').onclick=async()=>{const value=$('keyValue').value.trim();if(!value){toast('Enter an API key');return}try{await api('/api/settings',{key_action:'add',provider:$('keyProvider').value,group:'default',label:$('keyLabel').value,value});$('keyValue').value='';await refreshBootstrap();toast('API key saved locally')}catch(error){toast(error.message)}};$('keySort').onchange=()=>{state.keySort=$('keySort').value;renderKeys()};$('addFailoverMember').onclick=()=>addFailoverMember();$('saveFailoverGroup').onclick=saveFailoverGroup;$('cancelFailoverGroup').onclick=resetFailoverForm;$('refreshFiles').onclick=browseFiles;$('selectCurrentFolder').onclick=()=>{const path=$('fileFolder').value.trim();if(!state.selectedFolders.includes(path))state.selectedFolders.push(path);renderPendingUploads();toast('Current folder staged for this chat')};$('chooseLocalFiles').onclick=()=>$('localFiles').click();$('chooseLocalFolder').onclick=()=>$('localFolder').click();$('localFiles').onchange=()=>stageLocalFiles($('localFiles'));$('localFolder').onchange=()=>stageLocalFiles($('localFolder'));$('attachFiles').onclick=attachFiles;$('startDownload').onclick=startDownload;document.querySelectorAll('[data-close]').forEach(button=>button.onclick=()=>$(button.dataset.close).classList.remove('show'));document.querySelectorAll('.modal-backdrop').forEach(backdrop=>backdrop.onclick=event=>{if(event.target===backdrop)backdrop.classList.remove('show')});document.addEventListener('click',event=>{if(!event.target.closest('#chatContextMenu,.chat-more'))closeChatMenu();if(!event.target.closest('.custom-select'))closeCustomSelects();if(!event.target.closest('#routePicker,#routeFlyout'))closeRouteFlyout()});document.addEventListener('keydown',event=>{if(event.key==='Escape'){closeChatMenu();closeCustomSelects();closeRouteFlyout()}});
$('menuBtn').onclick=()=>{if(matchMedia('(max-width:700px)').matches){document.body.classList.toggle('nav-open')}else{const collapsed=document.documentElement.classList.toggle('side-collapsed');try{localStorage.setItem('kurt-side-collapsed',collapsed?'1':'0')}catch(error){}}};$('scrim').onclick=()=>document.body.classList.remove('nav-open');document.querySelector('.sidebar').addEventListener('click',event=>{if(event.target.closest('.chat-item,#newChat,#sidebarSettings'))document.body.classList.remove('nav-open')});$('themeBtn').onclick=()=>{const root=document.documentElement;root.classList.add('theme-fade');setTimeout(()=>root.classList.remove('theme-fade'),420);const theme=document.documentElement.dataset.theme==='light'?'dark':'light';document.documentElement.dataset.theme=theme;try{localStorage.setItem('kurt-theme',theme)}catch(error){}};enhanceAllSelects();
document.body.classList.add('compact-scroll');
$('showApiModelForm').onclick=()=>openApiModelForm(null);$('cancelApiModel').onclick=closeApiModelForm;$('addApiModel').onclick=saveApiModel;
makeSortable($('failoverMembers'),'.failover-member-row');
showWelcome();
resetFailoverForm();
(async()=>{
 try{await refreshBootstrap()}catch(error){toast('Settings could not be loaded: '+error.message)}
 try{await refreshChats()}catch(error){toast('Chats could not be loaded: '+error.message)}
 pollDownload();
})();
</script></body></html>'''


THINKING_KINDS = ()
MODEL_SUMMARY_KINDS = ()
THINK_OPEN_RE = re.compile(r"<think(?:ing)?>", re.I)
THINK_CLOSE_RE = re.compile(r"</think(?:ing)?>", re.I)
THINK_TAG_RE = re.compile(r"<(/)?think(?:ing)?>", re.I)
THINK_RE = re.compile(r"<think(?:ing)?>([\s\S]*?)(?:</think(?:ing)?>|\Z)", re.I)
ACTION_TAIL_RE = re.compile(r"@@@\s*(?:WRITE|EDIT)\b[\s\S]*\Z")
UNSUPPORTED_ACTION_BLOCK_RE = re.compile(
    r"@@@\s*(?:RUN|SEARCH|SYSTEM_PROMPT|READ|LIST|EXEC|BASH|SHELL)\b[\s\S]*?(?:@@@\s*END\b|\Z)", re.I
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
    """Extract the public rationale block and discard private inline thought tags."""
    reply = str(raw or "")
    summaries = [match.group(1).strip() for match in SUMMARY_ACTION_RE.finditer(reply) if match.group(1).strip()]
    reply = SUMMARY_ACTION_RE.sub("", reply)
    opener = THINK_OPEN_RE.search(reply)
    closer = THINK_CLOSE_RE.search(reply)
    if closer and (not opener or closer.start() < opener.start()):
        reply = reply[closer.end():]
    reply = re.sub(r"<think(?:ing)?>[\s\S]*?(?:</think(?:ing)?>|\Z)", "", reply, flags=re.I)
    reply = THINK_TAG_RE.sub("", reply)
    reply = re.sub(r"\n{3,}", "\n\n", reply)
    return "\n\n".join(summaries).strip(), reply.strip()


def build_public_activity_rationale(user_text, mode, worklog, artifacts, validation_errors):
    """Build an honest, user-facing activity record when the model omits its rationale block."""
    request = re.sub(r"\s+", " ", str(user_text or "")).strip()
    if len(request) > 700:
        request = request[:697].rstrip() + "..."
    sections = ["Request understood\n" + (request or "No request text was available.")]
    if mode == "code":
        sections.append(
            "Mode and permissions\nCode mode permits read-only access beneath the configured project root. "
            "File proposals and terminal commands remain subject to separate, explicit approval; "
            "the command runner is restricted to the isolated chat workspace."
        )
    else:
        sections.append(
            "Mode and permissions\nNormal mode has no local file, attachment, directory, or terminal access. "
            "A request involving local computer resources must be retried in Code mode."
        )
    entries = []
    for item in (worklog if isinstance(worklog, list) else [])[-20:]:
        data = item if isinstance(item, dict) else {"text": str(item)}
        label = str(data.get("text") or "Activity").strip()
        detail = re.sub(r"\s+", " ", str(data.get("detail") or "")).strip()
        if detail and detail != label:
            if len(detail) > 500:
                detail = detail[:497].rstrip() + "..."
            entries.append("- " + label + " — " + detail)
        elif label:
            entries.append("- " + label)
    sections.append(
        "Observed actions and checks\n" + ("\n".join(entries) if entries else "No local-file, directory, or terminal operation was performed for this reply.")
    )
    artifact_lines = []
    for artifact in (artifacts if isinstance(artifacts, list) else []):
        path = str(artifact.get("path") or "Generated output")
        validation = artifact.get("validation") if isinstance(artifact, dict) else {}
        message = str((validation or {}).get("message") or "Prepared for review")
        artifact_lines.append("- " + path + " — " + message)
    if artifact_lines:
        sections.append("Prepared outputs\n" + "\n".join(artifact_lines))
    if validation_errors:
        sections.append("Remaining validation issues\n" + "\n".join("- " + str(error) for error in validation_errors[:12]))
    else:
        sections.append("Verification scope\nOnly the checks and actions listed above are confirmed. A preview or syntax check does not mean a file was saved or a command was executed.")
    return "\n\n".join(sections)


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


def normalize_download_id(model, source="auto"):
    raw = str(model or "").strip()
    lowered = raw.lower()
    hf_prefixes = ("hf.co/", "huggingface.co/", "hf:", "https://huggingface.co/", "http://huggingface.co/",
                   "https://www.huggingface.co/", "https://hf.co/", "http://hf.co/")
    if str(source or "auto").lower() == "huggingface" or lowered.startswith(hf_prefixes):
        value = re.sub(r"^(?:https?://)?(?:www\.)?(?:huggingface\.co|hf\.co)/", "", raw, flags=re.I)
        value = re.sub(r"^hf:", "", value, flags=re.I).strip("/")
        value = re.sub(r"/(?:tree|blob|resolve)/[^:]*$", "", value)
        value = re.sub(r"\.git$", "", value)
        if not re.fullmatch(r"[\w.\-]+/[\w.\-]+(?::[\w.\-]+)?", value):
            raise ValueError("Enter a Hugging Face GGUF repository such as user/repo or user/repo:Q4_K_M.")
        return "hf.co/" + value
    return raw


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
        if self.settings.get("default_effort") == "flash":
            self.settings["default_effort"] = "thinking"
            self.save_settings()
        self.settings.setdefault("default_mode", "normal")
        self.settings.setdefault("api_groups", [])
        self.model_lock = threading.RLock()
        self.migrate_legacy_api_groups()
        configured_root = Path(self.settings.get("project_root") or Path.cwd()).expanduser()
        if not configured_root.exists() or not configured_root.is_dir():
            self.settings["project_root"] = str(Path.cwd().resolve())
            self.save_settings()
        self.chats = _load_json(CHATS_FILE, [])
        self.download_job = None
        self.download_thread = None

    def migrate_legacy_api_groups(self):
        groups = self.settings.setdefault("api_groups", [])
        known = {str(item.get("remote_id") or "") for item in groups if isinstance(item, dict)}
        changed = False
        for remote in K.REMOTE_MODELS:
            remote_id = str(remote.get("id") or "")
            if not remote_id or remote_id in known:
                continue
            groups.append({
                "id": "legacy_" + remote_id, "name": remote.get("label") or remote.get("model") or "API model",
                "source": "api", "provider": remote.get("provider"), "model": remote.get("model"),
                "label": remote.get("label") or remote.get("model"), "key_group": remote.get("key_group") or "default",
                "key_id": "pool", "base_url": remote.get("base_url") or "", "remote_id": remote_id,
            })
            known.add(remote_id)
            changed = True
        if changed:
            self.save_settings()

    def sync_api_groups(self):
        for group in self.settings.get("api_groups", []):
            if group.get("source") != "api":
                continue
            remote_id = str(group.get("remote_id") or uuid.uuid4().hex[:12])
            group["remote_id"] = remote_id
            entry = next((item for item in K.REMOTE_MODELS if item.get("id") == remote_id), None)
            payload = {
                "id": remote_id, "provider": group.get("provider"), "model": group.get("model"),
                "label": group.get("label") or group.get("model"),
                "key_group": group.get("key_group") or "default",
                "base_url": group.get("base_url") or K.default_base_url(group.get("provider")),
            }
            if entry is None:
                K.REMOTE_MODELS.append(payload)
            else:
                entry.update(payload)

    @staticmethod
    def group_key_filter(group):
        ids = group.get("key_ids")
        if isinstance(ids, list):
            return {str(item) for item in ids}
        key_id = str(group.get("key_id") or "pool")
        return None if key_id == "pool" else {key_id}

    def remote_allowed_keys(self, remote):
        group = next((item for item in self.settings.get("api_groups", []) if item.get("source") == "api" and item.get("remote_id") == remote.get("id")), None)
        return None if group is None else self.group_key_filter(group)

    def api_groups_view(self):
        view = []
        for group in self.settings.get("api_groups", []):
            item = dict(group)
            if group.get("source") == "api":
                remote = next((entry for entry in K.REMOTE_MODELS if entry.get("id") == group.get("remote_id")), None)
                item["model_ref"] = K.remote_model_ref(remote) if remote else ""
            view.append(item)
        return view

    def list_chat_routes(self):
        self.sync_api_groups()
        routes = []
        local_models = []
        try:
            local_models = [item for item in self.list_models() if item.get("type") == "local"]
        except Exception:
            pass
        for model in sorted(local_models, key=lambda item: (item.get("label") or "").lower()):
            routes.append({"value": "model|" + model["ref"], "label": "Local · " + (model.get("label") or model["name"]), "kind": "local"})

        for remote in sorted(K.REMOTE_MODELS, key=lambda item: str(item.get("label") or item.get("model") or "").lower()):
            model_ref = K.remote_model_ref(remote)
            provider = str(remote.get("provider") or "").lower()
            key_group = str(remote.get("key_group") or "default")
            keys = [item for item in K.PROVIDER_KEYS if str(item.get("provider") or "").lower() == provider]
            allowed = self.remote_allowed_keys(remote)
            if allowed is not None:
                keys = [item for item in keys if str(item.get("id")) in allowed]
            model_label = str(remote.get("label") or remote.get("model") or model_ref)
            for key in sorted(keys, key=lambda item: str(item.get("label") or "Account").lower()):
                routes.append({
                    "value": "keymodel|{}|{}".format(model_ref, key.get("id")),
                    "label": "API · {} · {}".format(model_label, key.get("label") or "Account"),
                    "kind": "key",
                })

        for group in sorted(self.settings.get("api_groups", []), key=lambda item: str(item.get("name") or "").lower()):
            group_id = str(group.get("id") or "")
            if not group_id:
                continue
            if group.get("source") == "failover":
                if group.get("members"):
                    routes.append({"value": "pool|" + group_id, "label": "Group · " + str(group.get("name") or "Fallback group"), "kind": "group"})
                continue
            if group.get("source") == "local":
                model_ref = str(group.get("local_model") or "")
                if any(item.get("ref") == model_ref for item in local_models):
                    routes.append({"value": "group|" + group_id, "label": "Local · " + str(group.get("name") or "saved route"), "kind": "local"})
                continue
            provider = str(group.get("provider") or "").lower()
            pool = str(group.get("key_group") or "default")
            keys = [item for item in K.PROVIDER_KEYS if str(item.get("provider") or "").lower() == provider]
            allowed = self.group_key_filter(group)
            if allowed is not None:
                keys = [item for item in keys if str(item.get("id")) in allowed]
            remote = next((item for item in K.REMOTE_MODELS if item.get("id") == group.get("remote_id")), None)
            if remote is not None and len(keys) > 1:
                routes.append({"value": "group|" + group_id, "label": "API · " + str(group.get("name") or group.get("label") or group.get("model") or "saved route") + " · key pool", "kind": "key"})
        return routes

    def resolve_model_key_candidate(self, model_ref, key_id="pool"):
        model_ref = str(model_ref or "").strip()
        key_id = str(key_id or "pool")
        local = next((item for item in self.list_models() if item.get("ref") == model_ref and item.get("type") == "local"), None)
        if local is not None:
            if key_id not in ("", "pool"):
                raise ValueError("Local models do not use API keys.")
            return {"model": model_ref, "key_id": None, "provider_keys": None}

        remote = next((item for item in K.REMOTE_MODELS if K.remote_model_ref(item) == model_ref), None)
        if remote is None:
            raise ValueError("The selected model is no longer configured: " + (model_ref or "(empty model)"))
        provider = str(remote.get("provider") or "").lower()
        matches = [item for item in K.PROVIDER_KEYS if str(item.get("provider") or "").lower() == provider]
        allowed = self.remote_allowed_keys(remote)
        if allowed is not None:
            matches = [item for item in matches if str(item.get("id")) in allowed]
        if key_id != "pool":
            matches = [item for item in matches if str(item.get("id")) == key_id]
        if not matches:
            raise ValueError("No compatible API key is available for " + str(remote.get("label") or remote.get("model") or model_ref) + ".")
        return {
            "model": model_ref,
            "key_id": None if key_id == "pool" else key_id,
            "provider_keys": matches,
        }

    def resolve_chat_route(self, route, legacy_model=None):
        route = str(route or "").strip()
        if not route and legacy_model:
            if str(legacy_model).startswith("remote:"):
                legacy_group = next((group for group in self.settings.get("api_groups", []) if group.get("source") == "api" and group.get("remote_id") and K.remote_model_ref(next((item for item in K.REMOTE_MODELS if item.get("id") == group.get("remote_id")), {})) == legacy_model), None)
                if legacy_group:
                    route = "group|" + str(legacy_group.get("id"))
                else:
                    remote = next((item for item in K.REMOTE_MODELS if K.remote_model_ref(item) == legacy_model), None)
                    if remote:
                        provider = str(remote.get("provider") or "").lower()
                        first_key = next((item for item in K.PROVIDER_KEYS if str(item.get("provider") or "").lower() == provider), None)
                        if first_key:
                            route = "keymodel|{}|{}".format(legacy_model, first_key.get("id"))
            if not route:
                route = "model|" + str(legacy_model)

        if route.startswith("model|"):
            model_ref = route.split("|", 1)[1]
            candidate = self.resolve_model_key_candidate(model_ref, "pool")
            return {**candidate, "route": route, "candidates": [candidate]}

        if route.startswith("keymodel|"):
            parts = route.split("|", 2)
            if len(parts) != 3:
                raise ValueError("Choose a model and API key.")
            candidate = self.resolve_model_key_candidate(parts[1], parts[2])
            return {**candidate, "route": route, "candidates": [candidate]}

        if route.startswith("pool|"):
            group_id = route.split("|", 1)[1]
            group = next((item for item in self.settings.get("api_groups", []) if str(item.get("id")) == group_id and item.get("source") == "failover"), None)
            if not group:
                raise ValueError("That failover group no longer exists. Choose another route.")
            candidates = []
            errors = []
            for member in group.get("members", []):
                try:
                    candidate = self.resolve_model_key_candidate(member.get("model"), member.get("key_id", "pool"))
                    candidate["member_label"] = str(member.get("label") or candidate["model"])
                    candidates.append(candidate)
                except Exception as exc:
                    errors.append(str(exc))
            if not candidates:
                raise ValueError("No usable model/key entries remain in this group. " + (errors[0] if errors else "Edit the group and add valid entries."))
            return {"model": candidates[0]["model"], "key_id": candidates[0].get("key_id"), "provider_keys": candidates[0].get("provider_keys"), "route": route, "candidates": candidates}

        parts = route.split("|")
        if len(parts) not in (2, 3) or parts[0] not in ("group", "key"):
            raise ValueError("Choose a local model, API key, or configured group first.")
        group_id = parts[1]
        group = next((item for item in self.settings.get("api_groups", []) if str(item.get("id")) == group_id), None)
        if not group:
            raise ValueError("That chat group no longer exists. Choose another route.")
        if group.get("source") == "local":
            if parts[0] == "key":
                raise ValueError("A local-model group does not use API keys.")
            candidate = self.resolve_model_key_candidate(group.get("local_model"), "pool")
            return {**candidate, "route": route, "candidates": [candidate]}
        self.sync_api_groups()
        remote = next((item for item in K.REMOTE_MODELS if item.get("id") == group.get("remote_id")), None)
        if remote is None:
            raise ValueError("The API model for this group is not configured.")
        model_ref = K.remote_model_ref(remote)
        key_id = "pool"
        if parts[0] == "key":
            if len(parts) != 3:
                raise ValueError("Choose the specific API key.")
            key_id = parts[2]
            configured_key = str(group.get("key_id") or "pool")
            if configured_key != "pool" and configured_key != key_id:
                raise ValueError("This group is restricted to a different API key.")
        elif len(parts) == 3:
            raise ValueError("Invalid group route.")
        elif group.get("key_id") and group.get("key_id") != "pool":
            key_id = str(group.get("key_id"))
        candidate = self.resolve_model_key_candidate(model_ref, key_id)
        return {**candidate, "route": route, "candidates": [candidate]}

    def generate_chat_title(self, user_text, assistant_text, model):
        prompt = [
            {"role": "system", "content": "Create a concise chat title of 3–7 words. Return only the title, with no quotes, numbering, or explanation. Reflect the user's request, not the assistant's reply."},
            {"role": "user", "content": "User message:\n" + str(user_text)[:1200] + "\n\nAssistant response:\n" + str(assistant_text)[:600]},
        ]
        try:
            chunks = []
            stream = K.stream_chat(prompt, model=model, think=False, temperature=0.2, max_tokens=32)
            for kind, chunk in stream:
                kind_name = str(kind or "").lower().replace("-", "_")
                if kind_name == "content" and chunk:
                    chunks.append(str(chunk))
            title = " ".join("".join(chunks).strip().strip("\"'`* ").split())
            title = re.sub(r"^(title|chat title)\s*:\s*", "", title, flags=re.I).strip(" \"'`.-")
            if title:
                return title[:58].rstrip()
        except Exception:
            pass
        return re.sub(r"\s+", " ", str(user_text or "")).strip()[:58] or "New conversation"

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

    def project_access_root(self):
        root = self.workspace()
        home = Path.home().expanduser().resolve()
        if root == Path(root.anchor) or root == home or root in home.parents:
            raise ValueError("Code mode requires a specific project folder. Choose a project directory, not the computer's system root or your entire home folder.")
        return root

    def resolve_project_access_path(self, relative, allow_root=False):
        rel = self._safe_context_relative(relative, allow_root=allow_root)
        if rel and self._skip_context_name(Path(rel).name):
            raise ValueError("This credential or secret file is protected from Code mode access.")
        root = self.project_access_root()
        candidate = root / rel if rel else root
        current = root
        for part in rel.split('/') if rel else ():
            current = current / part
            if current.is_symlink():
                raise ValueError("Symbolic links cannot be accessed in Code mode.")
        resolved = candidate.resolve()
        resolved.relative_to(root)
        return resolved

    def read_project_file(self, relative):
        path = self.resolve_project_access_path(relative)
        if not path.is_file() or path.is_symlink() or self._skip_context_name(path.name):
            raise ValueError("That file is unavailable for Code mode reading.")
        if path.stat().st_size > 1_000_000:
            raise ValueError("That file is larger than the 1 MB per-read limit. Read a smaller file or split it first.")
        raw = path.read_bytes()
        if b"\x00" in raw[:4096]:
            raise ValueError("That file appears to be binary and cannot be read as text.")
        try:
            content = raw.decode("utf-8")
        except UnicodeError as exc:
            raise ValueError("That file is not valid UTF-8 text and cannot be read as text.") from exc
        return content

    def list_project_directory(self, relative=""):
        path = self.resolve_project_access_path(relative, allow_root=True)
        if not path.is_dir():
            raise ValueError("That project path is not a directory.")
        items = []
        for item in sorted(path.iterdir(), key=lambda entry: (not entry.is_dir(), entry.name.lower())):
            if item.is_symlink() or self._skip_context_name(item.name):
                continue
            if item.is_dir() and self._skip_context_dir(item.name):
                continue
            if not item.is_dir() and not item.is_file():
                continue
            try:
                size = item.stat().st_size if item.is_file() else 0
            except OSError:
                continue
            items.append({
                "name": item.name,
                "path": item.relative_to(self.workspace()).as_posix(),
                "directory": item.is_dir(),
                "size": size,
            })
            if len(items) >= 500:
                items.append({"name": "… listing truncated at 500 entries …", "path": "", "directory": False, "size": 0})
                break
        return items

    def list_models(self):
        models = []
        try:
            for item in K.installed_models() if K.ollama_up() else []:
                models.append({
                    "ref": item["name"], "name": item["name"], "label": item["name"],
                    "provider": "Ollama", "provider_id": None, "key_group": None,
                    "type": "local", "size": item.get("size"),
                    "current": item["name"] == K.MODEL,
                })
        except Exception:
            pass
        for item in K.REMOTE_MODELS:
            ref = K.remote_model_ref(item)
            models.append({
                "ref": ref, "name": item.get("model"), "label": item.get("label") or item.get("model"),
                "provider": K.provider_label(item.get("provider")),
                "provider_id": str(item.get("provider") or "").lower(),
                "key_group": str(item.get("key_group") or "default"), "type": "remote",
                "current": ref == K.MODEL,
            })
        models.sort(key=lambda item: (not item["current"], item["provider"].lower(), item["label"].lower()))
        if K.MODEL and not any(item["ref"] == K.MODEL for item in models):
            remote = K.get_remote_model(K.MODEL)
            if remote:
                models.insert(0, {"ref": K.MODEL, "name": remote.get("model"), "label": remote.get("label") or remote.get("model"), "provider": K.provider_label(remote.get("provider")), "provider_id": str(remote.get("provider") or "").lower(), "key_group": str(remote.get("key_group") or "default"), "type": "remote", "current": True})
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
            "label": item.get("label") or "Account",
            "masked": K.mask_key(item.get("value")), "created_at": item.get("created_at"),
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
        if any(part.lower() in ({'.git', '.kurt', '.kurt_inputs', '__pycache__', 'node_modules', '.venv', 'venv', '.ssh', '.aws', '.azure', '.gnupg', '.kube', 'credentials', 'secrets', 'dist', 'build'} | {str(item).lower() for item in K.IGNORE_DIRS}) for part in parts):
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
            folder = self.resolve_project_access_path(rel, allow_root=True)
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
            source = self.resolve_project_access_path(rel)
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
            folder_root = str(item.get('folder_root') or '').strip().replace('\\', '/')
            if Path(rel).suffix.lower() == '.zip':
                target = self.resolve_chat_path(workspace, rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                extracted, _expanded = self._extract_zip_bytes(content, workspace)
                archives += 1
                imported += 1 + extracted
            elif folder_root:
                folder_root = self._safe_context_relative(folder_root)
                if rel != folder_root and not rel.startswith(folder_root + '/'):
                    raise ValueError('A folder upload contains a path outside its selected folder.')
                upload_groups.setdefault(folder_root, []).append((rel, content))
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
        items = self.context_file_list(chat_id)
        return {'ok': True, 'files': imported, 'items': items, 'bytes': total_bytes, 'archives': archives,
                'workspace': str(workspace), 'message': f'Added files to this chat workspace. Automatically unpacked {archives} folder/archive ZIP(s).'}

    def context_contents(self, chat_id):
        root = self.chat_workspace(chat_id)
        chunks = []
        used = 0
        skipped = 0
        truncated = 0
        paths = []
        for current, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if not self._skip_context_dir(d))
            for name in files:
                path = Path(current) / name
                if self._skip_context_name(name) or path.is_symlink() or '.kurt_imports' in path.parts:
                    continue
                paths.append(path)
        paths.sort(key=lambda path: (
            0 if path.name.startswith('pasted-text-') and path.suffix.lower() == '.txt' else 1,
            path.relative_to(root).as_posix().lower(),
        ))
        for path in paths:
            try:
                raw = path.read_bytes()
                if b'\x00' in raw[:4096]:
                    skipped += 1
                    continue
                content = raw.decode('utf-8')
            except (OSError, UnicodeError):
                skipped += 1
                continue
            rel = path.relative_to(root).as_posix()
            header = f'\n--- FILE: {rel} ---\n'
            remaining = MAX_WORKSPACE_CONTEXT - used - len(header) - 1
            if remaining <= 0:
                skipped += 1
                continue
            if len(content) > remaining:
                suffix = '\n[Content truncated in this prompt to fit the 1.4 MB inline context budget. The full file remains in the chat workspace.]'
                content = content[:max(0, remaining - len(suffix))] + suffix
                truncated += 1
            block = header + content + '\n'
            chunks.append(block)
            used += len(block)
        intro = 'Text file contents from this conversation’s isolated temporary workspace. Uploaded ZIP files and folders are extracted automatically; use the extracted paths shown in the workspace tree.'
        if truncated:
            intro += f'\n{truncated} text file(s) are partially included because the inline text context is limited to 1.4 MB; the full files remain in the workspace.'
        if skipped:
            intro += f'\n{skipped} file(s) were omitted from inline text context because they were binary or unreadable; the original attachments remain in this workspace.'
        image_paths = [item['path'] for item in self.context_file_list(chat_id) if item.get('image')]
        if image_paths:
            intro += '\nImage attachments available in the workspace: ' + ', '.join(image_paths[:80])
            if len(image_paths) > 80:
                intro += f' (and {len(image_paths) - 80} more)'
            intro += '\nOnly make visual claims when the active model/provider has actual vision input or an available image-reading tool.'
        return intro + ('\n' + ''.join(chunks) if chunks else '\n(No readable text files attached yet.)')

    def chat_system_prompt(self, chat_id, mode="normal", effort="thinking"):
        custom = str(self.settings.get("system_prompt") or "").strip()
        if mode == "code":
            root = self.chat_workspace(chat_id)
            base = custom or K.SYSTEM.replace("{cwd}", str(root))
            if "{cwd}" in base:
                base = base.replace("{cwd}", str(root))
        else:
            base = custom or K.SYSTEM.replace("{cwd}", "(local file access disabled in Normal mode)")
            if "{cwd}" in base:
                base = base.replace("{cwd}", "(local file access disabled in Normal mode)")
        if "WEB INTERFACE FILE OUTPUT RULES:" not in base and "KURT WEB WORKSPACE RULES:" not in base:
            base += WEB_INSTRUCTIONS
        if "KURT WEB WORKSPACE RULES:" not in base:
            base += "\n" + WEB_INSTRUCTIONS
        mode_rules = CODE_MODE_INSTRUCTIONS if mode == "code" else NORMAL_MODE_INSTRUCTIONS
        effort_rules = EFFORT_INSTRUCTIONS.get(effort, EFFORT_INSTRUCTIONS["thinking"])
        prompt = base + "\n" + KURT_IDENTITY_INSTRUCTIONS + "\n" + mode_rules + "\n" + effort_rules + "\n" + RESPONSE_FORMAT_INSTRUCTIONS
        if mode == "code":
            project = self.workspace()
            prompt += (
                "\nIsolated temporary workspace directory: " + str(root)
                + "\nConfigured Code mode project access root: " + str(project)
                + "\nFiles attached in the isolated workspace:\n" + self.tree(root)
            )
        return prompt

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
                    if existed_in_temp:
                        old, _crlf = K.read_file(path)
                    else:
                        project_path = self.resolve_project_access_path(filename)
                        if not project_path.is_file():
                            errors.append("{}: EDIT target is not available in the configured Code mode project directory; read or import it first.".format(filename))
                            continue
                        old = self.read_project_file(filename)
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
                project_path = self.resolve_project_access_path(filename)
                artifacts.append({
                    "path": filename.replace("\\", "/"), "content": content,
                    "validation": validation, "exists": project_path.exists(),
                    "temp_exists": existed_in_temp, "source": kind,
                    "approval_required": True,
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
        return len(self.context_file_list(chat_id))

    def context_file_list(self, chat_id):
        root = self.chat_workspace(chat_id)
        items = []
        for current, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if not self._skip_context_dir(d) and d not in ('.kurt_imports', '.kurt_inputs'))
            for name in sorted(files):
                if self._skip_context_name(name) or name == '.DS_Store':
                    continue
                path = Path(current) / name
                if path.is_symlink() or '.kurt_imports' in path.parts or '.kurt_inputs' in path.parts:
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                mime_type = mimetypes.guess_type(name)[0] or 'application/octet-stream'
                items.append({
                    'path': path.relative_to(root).as_posix(), 'name': name,
                    'size': size, 'type': mime_type, 'image': mime_type.startswith('image/'),
                    'archive': path.suffix.lower() == '.zip',
                })
        return items

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

    def create_chat(self, initial_message=None):
        now = time.time()
        chat = {
            "id": uuid.uuid4().hex, "title": "New conversation", "model": K.MODEL,
            "effort": "thinking" if self.settings.get("default_effort") == "flash" else self.settings.get("default_effort", "thinking"),
            "mode": self.settings.get("default_mode", "normal"),
            "route": self.settings.get("default_route", ""),
            "pinned": False, "created": now, "updated": now, "messages": [],
        }
        if isinstance(initial_message, dict):
            content = str(initial_message.get("content") or "").strip()
            message_id = str(initial_message.get("id") or "").strip()
            if content and message_id:
                chat["messages"].append({"role": "user", "content": content, "message_id": message_id})
                chat["effort"] = initial_message.get("effort") if initial_message.get("effort") in EFFORT_INSTRUCTIONS else chat["effort"]
                chat["mode"] = initial_message.get("mode") if initial_message.get("mode") in ("normal", "code") else chat["mode"]
                if initial_message.get("route"):
                    chat["route"] = str(initial_message.get("route"))
                chat["updated"] = now
        with self.lock:
            self.chats.insert(0, chat)
            self.save_chats()
        return chat

    def get_chat(self, chat_id):
        with self.lock:
            return next((chat for chat in self.chats if chat.get("id") == chat_id), None)

    def start_download(self, model, source="auto"):
        model = str(model or "").strip()
        if not model or len(model) > 300 or any(ch.isspace() for ch in model):
            raise ValueError("Enter a model ID, for example qwen2.5-coder:7b or a Hugging Face repository such as user/repo.")
        model = normalize_download_id(model, source)
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
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline'; font-src 'self' data: https://cdn.jsdelivr.net; connect-src 'self'; img-src 'self' data: blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
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
                    "models": self.app.list_models(), "routes": self.app.list_chat_routes(), "providers": providers,
                    "keys": self.app.sanitized_keys(), "api_groups": self.app.api_groups_view(),
                    "settings": {"model": K.MODEL, "default_route": self.app.settings.get("default_route", ""), "system_prompt": self.app.editable_system_prompt(), "allow_project_writes": bool(self.app.settings.get("allow_project_writes", False))},
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
                        "id": chat["id"], "title": chat.get("title"), "model": chat.get("model"), "route": chat.get("route", ""),
                        "effort": "thinking" if chat.get("effort") == "flash" else chat.get("effort", "thinking"), "mode": chat.get("mode", "normal"),
                        "pinned": bool(chat.get("pinned", False)), "messages": chat.get("messages", []),
                    })
            elif parsed.path == "/api/download/status":
                with self.app.lock:
                    self._json({"job": dict(self.app.download_job) if self.app.download_job else None})
            elif parsed.path == "/api/context/status":
                args = parse_qs(parsed.query)
                chat_id = str(args.get("chat_id", [""])[0])
                chat = self.app.get_chat(chat_id)
                if not chat:
                    self._json({"error": "Conversation not found."}, 404)
                    return
                if chat.get("mode") != "code":
                    raise ValueError("Attachments are available only in Code mode. Switch this conversation to Code mode.")
                items = self.app.context_file_list(chat_id)
                self._json({"files": len(items), "items": items})
            elif parsed.path == "/api/context/preview":
                args = parse_qs(parsed.query)
                chat_id = str(args.get("chat_id", [""])[0])
                chat = self.app.get_chat(chat_id)
                if not chat:
                    self._json({"error": "Conversation not found."}, 404)
                    return
                if chat.get("mode") != "code":
                    raise ValueError("Attachment previews are available only in Code mode.")
                relative = self.app._safe_context_relative(args.get("path", [""])[0])
                image_path = self.app.resolve_chat_path(self.app.chat_workspace(chat_id), relative)
                if not image_path.is_file() or image_path.is_symlink():
                    self._json({"error": "Attachment not found."}, 404)
                    return
                content_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
                if not content_type.startswith("image/") or image_path.stat().st_size > 8 * 1024 * 1024:
                    self._json({"error": "Only images up to 8 MB can be previewed."}, 400)
                    return
                payload = image_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(payload)
            elif parsed.path == "/api/workspace/export":
                args = parse_qs(parsed.query)
                chat_id = str(args.get("chat_id", [""])[0])
                chat = self.app.get_chat(chat_id)
                if not chat:
                    self._json({"error": "Conversation not found."}, 404)
                    return
                if chat.get("mode") != "code":
                    raise ValueError("Normal mode cannot inspect or export local attachments. Switch this conversation to Code mode.")
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
                chat = self.app.get_chat(str(args.get("chat_id", [""])[0]))
                if not chat or chat.get("mode") != "code":
                    raise ValueError("Normal mode cannot browse local directories. Switch this conversation to Code mode.")
                relative = self.app._safe_context_relative(args.get("path", [""])[0], allow_root=True)
                folder = self.app.resolve_project_access_path(relative, allow_root=True)
                if not folder.is_dir():
                    raise ValueError("Folder not found in the configured Code mode project directory.")
                items = self.app.list_project_directory(relative)
                parent_path = "" if not relative else str(Path(relative).parent).replace("\\", "/")
                parent = None if not relative else ("" if parent_path == "." else parent_path)
                self._json({"path": relative, "parent": parent, "items": [{"name": item["name"], "path": item["path"], "directory": item["directory"], "size": K.fmt_bytes(item["size"]) if item["size"] else ""} for item in items]})
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
            elif parsed.path == "/api/models/delete":
                self._delete_model(data)
            elif parsed.path == "/api/chats":
                initial = data.get("initial_message")
                if isinstance(initial, dict):
                    message_id = str(initial.get("id") or "").strip()
                    content = str(initial.get("content") or "").strip()
                    if not message_id or len(message_id) > 100 or not content or len(content) > 100000:
                        raise ValueError("Enter a valid user message before sending.")
                chat = self.app.create_chat(initial_message=initial if isinstance(initial, dict) else None)
                if data.get("mode") in ("normal", "code"):
                    chat["mode"] = data["mode"]
                if data.get("effort") in EFFORT_INSTRUCTIONS:
                    chat["effort"] = data["effort"]
                self.app.save_chats()
                self._json({"id": chat["id"]})
            elif parsed.path == "/api/chats/bulk-delete":
                ids = data.get("ids") if isinstance(data.get("ids"), list) else []
                valid_ids = {str(value) for value in ids if re.fullmatch(r"[0-9a-fA-F]{32}", str(value))}
                if not valid_ids:
                    raise ValueError("Select at least one conversation to delete.")
                with self.app.lock:
                    before = len(self.app.chats)
                    self.app.chats = [chat for chat in self.app.chats if chat.get("id") not in valid_ids]
                    deleted = before - len(self.app.chats)
                    self.app.save_chats()
                for chat_id in valid_ids:
                    shutil.rmtree(self.app.chat_workspace(chat_id, create=False), ignore_errors=True)
                    shutil.rmtree(CONFIG_DIR / "web_envs" / chat_id, ignore_errors=True)
                self._json({"ok": True, "deleted": deleted})
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
                elif isinstance(data.get("append_user_message"), dict):
                    incoming = data["append_user_message"]
                    message_id = str(incoming.get("id") or "").strip()
                    content = str(incoming.get("content") or "").strip()
                    if not message_id or len(message_id) > 100 or not content or len(content) > 100000:
                        raise ValueError("Enter a valid user message before sending.")
                    with self.app.lock:
                        if not any(item.get("message_id") == message_id for item in chat.get("messages", [])):
                            chat.setdefault("messages", []).append({"role": "user", "content": content, "message_id": message_id})
                            chat["effort"] = incoming.get("effort") if incoming.get("effort") in EFFORT_INSTRUCTIONS else chat.get("effort", "thinking")
                            chat["mode"] = incoming.get("mode") if incoming.get("mode") in ("normal", "code") else chat.get("mode", "normal")
                            if incoming.get("route"):
                                chat["route"] = str(incoming.get("route"))
                            chat["updated"] = time.time()
                            self.app.save_chats()
                    self._json({"ok": True, "message_id": message_id})
                elif "mode" in data:
                    value = str(data.get("mode") or "")
                    if value not in ("normal", "code"):
                        raise ValueError("Choose Normal or Code mode.")
                    chat["mode"] = value
                    if data.get("effort") in EFFORT_INSTRUCTIONS:
                        chat["effort"] = data["effort"]
                    chat["updated"] = time.time()
                    self.app.save_chats()
                    self._json({"ok": True, "mode": value})
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
                chat = self.app.get_chat(str(data.get("chat_id") or ""))
                if not chat or chat.get("mode") != "code":
                    raise ValueError("Normal mode cannot import or inspect local files. Switch this conversation to Code mode.")
                result = self.app.import_context(data.get("chat_id"), data.get("selected_files"), data.get("selected_folders"), data.get("uploads"))
                self._json(result)
            elif parsed.path == "/api/file/read":
                chat = self.app.get_chat(str(data.get("chat_id") or ""))
                if not chat or chat.get("mode") != "code":
                    raise ValueError("Normal mode cannot read local files. Switch this conversation to Code mode.")
                relative = str(data.get("path") or "")
                content = self.app.read_project_file(relative)
                self._json({"path": relative.replace("\\", "/"), "content": content})
            elif parsed.path == "/api/file/write":
                self._write_file(data)
            elif parsed.path == "/api/command/run":
                chat_id = str(data.get("chat_id") or "")
                chat = self.app.get_chat(chat_id)
                command = str(data.get("command") or "").strip()
                if not chat or chat.get("mode") != "code":
                    raise ValueError("Switch this conversation to Code mode before running a command.")
                if data.get("approved") is not True:
                    raise ValueError("The command has not been approved.")
                if not command or len(command) > 2000:
                    raise ValueError("Enter a valid command (maximum 2,000 characters).")
                result = self.app.run_command(chat_id, command)
                self._json(result)
            elif parsed.path == "/api/download":
                job = self.app.start_download(data.get("model"), data.get("source"))
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

    def _delete_model(self, data):
        model_ref = str(data.get("model_ref") or "").strip()
        if not model_ref:
            raise ValueError("Choose a model to delete.")
        with self.app.model_lock:
            models = self.app.list_models()
            model = next((item for item in models if item.get("ref") == model_ref), None)
            if model is None:
                raise ValueError("That model is no longer installed or configured.")
            removed_group_ids = set()
            if model.get("type") == "local":
                if not K.ollama_up():
                    raise ValueError("Ollama is not running. Start Ollama before uninstalling a local model.")
                K.http_json("/api/delete", {"name": model.get("name") or model_ref}, timeout=60)
                api_groups = self.app.settings.get("api_groups", [])
                kept_groups = []
                for group in api_groups:
                    if group.get("source") == "local" and str(group.get("local_model") or "") == model_ref:
                        removed_group_ids.add(str(group.get("id") or ""))
                        continue
                    if group.get("source") == "failover":
                        group["members"] = [member for member in group.get("members", []) if str(member.get("model") or "") != model_ref]
                        if not group["members"]:
                            removed_group_ids.add(str(group.get("id") or ""))
                            continue
                    kept_groups.append(group)
                self.app.settings["api_groups"] = kept_groups
            else:
                remote = next((item for item in K.REMOTE_MODELS if K.remote_model_ref(item) == model_ref), None)
                if remote is None:
                    raise ValueError("That API model is no longer configured.")
                remote_id = str(remote.get("id") or "")
                K.REMOTE_MODELS[:] = [item for item in K.REMOTE_MODELS if str(item.get("id") or "") != remote_id]
                kept_groups = []
                for group in self.app.settings.get("api_groups", []):
                    if group.get("source") == "api" and str(group.get("remote_id") or "") == remote_id:
                        removed_group_ids.add(str(group.get("id") or ""))
                        continue
                    if group.get("source") == "failover":
                        group["members"] = [member for member in group.get("members", []) if str(member.get("model") or "") != model_ref]
                        if not group["members"]:
                            removed_group_ids.add(str(group.get("id") or ""))
                            continue
                    kept_groups.append(group)
                self.app.settings["api_groups"] = kept_groups

            default_route = str(self.app.settings.get("default_route") or "")
            direct_prefixes = ("model|" + model_ref, "keymodel|" + model_ref + "|")
            if any(default_route == prefix or default_route.startswith(prefix) for prefix in direct_prefixes):
                self.app.settings["default_route"] = ""
            elif default_route.startswith(("group|", "pool|")):
                group_id = default_route.split("|", 1)[1]
                if group_id in removed_group_ids:
                    self.app.settings["default_route"] = ""

            if model_ref == K.MODEL or (model.get("type") == "local" and K.same_model(K.MODEL, model_ref)):
                alternatives = [item for item in self.app.list_models() if item.get("ref") != model_ref]
                if alternatives:
                    K.MODEL = K.SAVED_MODEL = alternatives[0]["ref"]
                else:
                    K.MODEL = K.SAVED_MODEL = K.DEFAULT_MODEL
            self.app.settings["model"] = K.MODEL
            K.save_config()
            self.app.save_settings()
        self._json({"ok": True, "model_ref": model_ref, "model": K.MODEL})

    def _settings(self, data):
        with self.app.model_lock:
            self._settings_locked(data)

    def _settings_locked(self, data):
        if "model" in data and not data.get("api_group_action"):
            self.app.set_model(str(data.get("model") or ""))
        if "default_route" in data:
            route_info = self.app.resolve_chat_route(data.get("default_route"))
            self.app.set_model(route_info["model"])
            self.app.settings["default_route"] = route_info["route"]
        if "project_root" in data:
            root = Path(str(data.get("project_root") or "")).expanduser().resolve()
            if not root.exists() or not root.is_dir():
                raise ValueError("Choose an existing directory for the workspace.")
            home = Path.home().expanduser().resolve()
            if root == Path(root.anchor) or root == home or root in home.parents:
                raise ValueError("Choose a specific project folder, not your home folder or the computer's system root.")
            self.app.settings["project_root"] = str(root)
        if "system_prompt" in data:
            if data.get("prompt_proposal"):
                chat = self.app.get_chat(str(data.get("chat_id") or ""))
                if not chat or chat.get("mode") != "code" or data.get("approved") is not True:
                    raise ValueError("Applying a system prompt proposal requires Code mode and your explicit confirmation.")
            prompt = str(data.get("system_prompt") or "")
            if len(prompt) > 40000:
                raise ValueError("System prompt must be 40,000 characters or fewer.")
            self.app.settings["system_prompt"] = prompt
        if "allow_project_writes" in data:
            self.app.settings["allow_project_writes"] = bool(data.get("allow_project_writes"))
        if "default_effort" in data:
            value = str(data.get("default_effort") or "thinking")
            if value not in EFFORT_INSTRUCTIONS:
                raise ValueError("Choose Thinking or Expert effort.")
            self.app.settings["default_effort"] = value
        if "default_mode" in data:
            value = str(data.get("default_mode") or "normal")
            if value not in ("normal", "code"):
                raise ValueError("Choose Normal or Code mode.")
            self.app.settings["default_mode"] = value

        key_action = data.get("key_action")
        keys_changed = False
        if key_action == "add":
            provider = str(data.get("provider", "")).lower()
            group = "default"
            value = str(data.get("value") or "").strip()
            if provider not in K.PROVIDER_SPECS:
                raise ValueError("Unknown provider.")
            if len(value) < 10 or any(ch.isspace() for ch in value):
                raise ValueError("Enter a valid API key.")
            if any(item.get("value") == value for item in K.PROVIDER_KEYS):
                raise ValueError("That API key is already saved.")
            K.PROVIDER_KEYS.append({"id": uuid.uuid4().hex[:12], "provider": provider, "group": group,
                                    "label": str(data.get("label") or "Account")[:80], "value": value,
                                    "created_at": time.time()})
            keys_changed = True
        elif key_action == "edit":
            key_id = data.get("id")
            item = next((entry for entry in K.PROVIDER_KEYS if entry.get("id") == key_id), None)
            if item is None:
                raise ValueError("API key not found.")
            value = str(data.get("value") or "").strip()
            if value and (len(value) < 10 or any(ch.isspace() for ch in value)):
                raise ValueError("That API key does not look valid.")
            if value and any(entry.get("value") == value and entry.get("id") != key_id for entry in K.PROVIDER_KEYS):
                raise ValueError("That API key is already saved.")
            item["label"] = str(data.get("label") or item.get("label") or "Account")[:80]
            if value:
                item["value"] = value
            keys_changed = True
        elif key_action == "remove":
            key_id = str(data.get("id") or "")
            K.PROVIDER_KEYS[:] = [item for item in K.PROVIDER_KEYS if str(item.get("id")) != key_id]
            for group in self.app.settings.get("api_groups", []):
                if isinstance(group.get("key_ids"), list):
                    group["key_ids"] = [item for item in group["key_ids"] if str(item) != key_id]
                if str(group.get("key_id") or "") == key_id:
                    group["key_id"] = "pool"
            keys_changed = True
        elif key_action == "rename_group":
            provider = str(data.get("provider") or "").lower()
            old_group = str(data.get("old_group") or "default").strip()
            new_group = str(data.get("new_group") or "").strip()
            if provider not in K.PROVIDER_SPECS or not old_group or not new_group or len(new_group) > 50:
                raise ValueError("Enter a valid provider and group name (1–50 characters).")
            matched = [item for item in K.PROVIDER_KEYS if str(item.get("provider", "")).lower() == provider and str(item.get("group") or "default") == old_group]
            if not matched:
                raise ValueError("No API keys exist in that group.")
            for item in matched:
                item["group"] = new_group
            for group in self.app.settings.get("api_groups", []):
                if str(group.get("provider", "")).lower() == provider and str(group.get("key_group") or "default") == old_group:
                    group["key_group"] = new_group
            for remote in K.REMOTE_MODELS:
                if str(remote.get("provider", "")).lower() == provider and str(remote.get("key_group") or "default") == old_group:
                    remote["key_group"] = new_group
            keys_changed = True

        failover_action = data.get("failover_action")
        if failover_action in ("add", "edit"):
            group_id = str(data.get("id") or "")
            groups = self.app.settings.setdefault("api_groups", [])
            current = next((item for item in groups if str(item.get("id")) == group_id and item.get("source") == "failover"), None) if failover_action == "edit" else None
            if failover_action == "edit" and current is None:
                raise ValueError("Failover group not found.")
            name = str(data.get("name") or "").strip()
            if not name or len(name) > 60:
                raise ValueError("Group name must contain 1–60 characters.")
            if any(item is not current and item.get("source") == "failover" and str(item.get("name", "")).casefold() == name.casefold() for item in groups):
                raise ValueError("A failover group with that name already exists.")
            submitted = data.get("members") if isinstance(data.get("members"), list) else []
            if len(submitted) < 1 or len(submitted) > 12:
                raise ValueError("Add between 1 and 12 local model/API key entries to a group.")
            members = []
            seen = set()
            for member in submitted:
                if not isinstance(member, dict):
                    continue
                model_ref = str(member.get("model") or "").strip()
                key_id = str(member.get("key_id") or "pool")
                self.app.resolve_model_key_candidate(model_ref, key_id)
                identity = (model_ref, key_id if key_id != "pool" else "pool")
                if identity in seen:
                    continue
                seen.add(identity)
                members.append({"model": model_ref, "key_id": identity[1]})
            if len(members) < 1:
                raise ValueError("A group needs at least one valid local model or API key entry.")
            payload = {"id": group_id or uuid.uuid4().hex[:12], "name": name, "source": "failover", "members": members}
            if current:
                current.clear()
                current.update(payload)
            else:
                groups.append(payload)
        elif failover_action == "remove":
            group_id = str(data.get("id") or "")
            group = next((item for item in self.app.settings.get("api_groups", []) if str(item.get("id")) == group_id and item.get("source") == "failover"), None)
            if group is None:
                raise ValueError("Failover group not found.")
            self.app.settings["api_groups"] = [item for item in self.app.settings.get("api_groups", []) if str(item.get("id")) != group_id]
            if str(self.app.settings.get("default_route") or "") == "pool|" + group_id:
                self.app.settings["default_route"] = ""

        group_action = data.get("api_group_action")
        if group_action in ("add", "edit"):
            group_id = str(data.get("id") or "")
            groups = self.app.settings.setdefault("api_groups", [])
            current = next((item for item in groups if str(item.get("id")) == group_id), None) if group_action == "edit" else None
            if group_action == "edit" and current is None:
                raise ValueError("API group not found.")
            name = str(data.get("name") or "").strip()
            source = str(data.get("source") or "local").lower()
            if not name or len(name) > 60:
                raise ValueError("Group name must contain 1–60 characters.")
            if source not in ("local", "api"):
                raise ValueError("Choose a local model or an API model.")
            if any(item is not current and str(item.get("name", "")).casefold() == name.casefold() for item in groups):
                raise ValueError("A group with that name already exists.")
            payload = {"id": group_id or uuid.uuid4().hex[:12], "name": name, "source": source}
            if source == "local":
                local_model = str(data.get("local_model") or "")
                if not any(item.get("ref") == local_model and item.get("type") == "local" for item in self.app.list_models()):
                    raise ValueError("Choose an installed local model.")
                payload["local_model"] = local_model
                if current and current.get("remote_id"):
                    K.REMOTE_MODELS[:] = [item for item in K.REMOTE_MODELS if item.get("id") != current.get("remote_id")]
            else:
                provider = str(data.get("provider") or "").lower()
                model_id = str(data.get("model") or "").strip()
                key_group = str(data.get("key_group") or "default").strip()
                key_id = str(data.get("key_id") or "pool")
                key_ids = None
                if isinstance(data.get("key_ids"), list):
                    key_ids = []
                    for value in data.get("key_ids"):
                        value = str(value)
                        if value not in key_ids:
                            key_ids.append(value)
                    chosen = [item for item in K.PROVIDER_KEYS if str(item.get("id")) in key_ids]
                    if not chosen or len(chosen) != len(key_ids):
                        raise ValueError("Select at least one saved API key.")
                    providers = {str(item.get("provider") or "").lower() for item in chosen}
                    if len(providers) != 1:
                        raise ValueError("Select keys from one provider for a single model.")
                    provider = providers.pop()
                    key_id = key_ids[0] if len(key_ids) == 1 else "pool"
                label = str(data.get("label") or model_id).strip()[:100]
                base_url = str(data.get("base_url") or K.default_base_url(provider)).strip().rstrip("/")
                if provider not in K.PROVIDER_SPECS or not model_id:
                    raise ValueError("Choose a provider and enter its exact API model ID.")
                if not base_url.startswith(("https://", "http://")):
                    raise ValueError("The API base URL must start with http:// or https://.")
                matches = [item for item in K.PROVIDER_KEYS if str(item.get("provider", "")).lower() == provider]
                if key_id != "pool" and not any(str(item.get("id")) == key_id for item in matches):
                    raise ValueError("Choose a key from the selected provider and group, or use the whole key pool.")
                if not matches:
                    raise ValueError("Add an API key for this provider and group before saving the route.")
                payload.update({"provider": provider, "model": model_id, "label": label, "key_group": key_group,
                                "key_id": key_id, "key_ids": key_ids, "base_url": base_url,
                                "remote_id": (current or {}).get("remote_id") or uuid.uuid4().hex[:12]})
            if current:
                current.clear()
                current.update(payload)
            else:
                groups.append(payload)
            self.app.sync_api_groups()
        elif group_action == "remove":
            group_id = str(data.get("id") or "")
            group = next((item for item in self.app.settings.get("api_groups", []) if str(item.get("id")) == group_id), None)
            if group is None:
                raise ValueError("API group not found.")
            if group.get("remote_id"):
                K.REMOTE_MODELS[:] = [item for item in K.REMOTE_MODELS if item.get("id") != group.get("remote_id")]
            self.app.settings["api_groups"] = [item for item in self.app.settings.get("api_groups", []) if str(item.get("id")) != group_id]
            if str(self.app.settings.get("default_route") or "") == "group|" + group_id:
                self.app.settings["default_route"] = ""

        if keys_changed or group_action:
            K.save_config()
        self.app.settings["model"] = K.MODEL
        self.app.save_settings()
        self._json({"ok": True, "model": K.MODEL, "project_root": str(self.app.workspace()),
                    "settings": {"model": K.MODEL, "default_route": self.app.settings.get("default_route", ""),
                                 "system_prompt": self.app.editable_system_prompt(),
                                 "allow_project_writes": bool(self.app.settings.get("allow_project_writes", False))}})

    def _write_file(self, data):
        chat_id = str(data.get("chat_id") or "")
        chat = self.app.get_chat(chat_id)
        if not chat or chat.get("mode") != "code":
            raise ValueError("Switch this conversation to Code mode before applying a file.")
        if not self.app.settings.get('allow_project_writes') or data.get('authorized') is not True or data.get('approved') is not True:
            raise ValueError('Project writes are disabled or not approved. Enable project writes in Settings and confirm the individual Apply to project action.')
        relative = str(data.get("path") or "")
        content = str(data.get("content") or "")
        path = self.app.resolve_project_access_path(relative)
        if self.app._skip_context_name(path.name):
            raise ValueError("That file type or name is protected from Code mode writes.")
        if path.exists() and path.is_symlink():
            raise ValueError("Symbolic-link targets cannot be modified in Code mode.")
        if path.exists() and not data.get("overwrite"):
            raise ValueError("The file already exists. Confirm overwrite in the browser first.")
        if not validate_content(relative, content).get("ok", False):
            raise ValueError("The file did not pass Kurt's validation checks.")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Re-resolve after creating parent directories to prevent symlink escapes.
        path = self.app.resolve_project_access_path(relative)
        if self.app._skip_context_name(path.name):
            raise ValueError("That file type or name is protected from Code mode writes.")
        if path.exists() and not data.get("overwrite"):
            raise ValueError("The file appeared after the review card was created. Review the overwrite confirmation again before applying it.")
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
        requested_route = str(data.get("route") or "")
        legacy_model = str(data.get("model") or "")
        effort = str(data.get("effort") or "thinking").lower()
        if effort == "flash":
            effort = "thinking"
        mode = str(data.get("mode") or "normal").lower()
        if not user_text:
            self._json({"error": "Message is empty."}, 400)
            return
        if len(user_text) > 100000:
            self._json({"error": "Message is too long."}, 400)
            return
        if effort not in EFFORT_INSTRUCTIONS:
            self._json({"error": "Choose Thinking or Expert effort."}, 400)
            return
        if mode not in ("normal", "code"):
            self._json({"error": "Choose Normal or Code mode."}, 400)
            return
        chat = self.app.get_chat(chat_id)
        if not chat:
            chat = self.app.create_chat()
            chat_id = chat["id"]
        message_id = str(data.get("message_id") or "").strip()
        already_saved = bool(message_id and any(item.get("message_id") == message_id for item in chat.get("messages", [])))
        try:
            route_info = self.app.resolve_chat_route(requested_route, legacy_model or None)
            model = route_info["model"]
        except Exception as exc:
            self._json({"error": str(exc)}, 400)
            return

        previous = chat.get("messages", [])[-MAX_MESSAGES_PER_CHAT:]
        if already_saved:
            previous = [item for item in previous if item.get("message_id") != message_id]
        self.app.chat_workspace(chat_id)
        message_number = len(chat.get("messages", [])) if already_saved else len(chat.get("messages", [])) + 1
        self.app.store_chat_input(chat_id, user_text, message_number)
        messages = [
            {"role": "system", "content": self.app.chat_system_prompt(chat_id, mode=mode, effort=effort)},
            {"role": "system", "content": self.app.context_contents(chat_id) if mode == "code" else "Normal mode has no access to files, attachments, directories, or local computer contents."},
        ]
        messages.extend(
            {"role": item["role"], "content": item["content"]}
            for item in previous if item.get("role") in ("user", "assistant")
        )
        messages.append({"role": "user", "content": user_text})

        if not already_saved:
            user_record = {"role": "user", "content": user_text}
            if message_id:
                user_record["message_id"] = message_id
            chat["messages"].append(user_record)
        chat["updated"] = time.time()
        if chat.get("title") in (None, "New conversation"):
            chat["title"] = "New conversation"
        chat["model"] = model
        chat["route"] = route_info["route"]
        chat["effort"] = effort
        chat["mode"] = mode
        self.app.save_chats()

        self.app.model_lock.acquire()
        try:
            route_info = self.app.resolve_chat_route(requested_route, legacy_model or None)
            model = route_info["model"]
        except Exception as exc:
            self.app.model_lock.release()
            self._json({"error": str(exc)}, 400)
            return
        original_provider_keys = list(K.PROVIDER_KEYS)
        route_candidates = route_info.get("candidates") or [route_info]
        active_candidate_index = 0
        failed_candidate_indices = set()

        def apply_route_candidate(candidate):
            nonlocal model
            model = candidate["model"]
            candidate_keys = candidate.get("provider_keys")
            K.PROVIDER_KEYS[:] = original_provider_keys
            self.app.set_model(model)
            if candidate_keys is not None:
                K.PROVIDER_KEYS[:] = list(candidate_keys)
            elif candidate.get("key_id"):
                selected_key = next((item for item in original_provider_keys if str(item.get("id")) == str(candidate["key_id"])), None)
                if selected_key is None:
                    raise ValueError("The selected API key is no longer available.")
                K.PROVIDER_KEYS[:] = [selected_key]

        try:
            apply_route_candidate(route_candidates[0])
        except Exception as exc:
            K.PROVIDER_KEYS[:] = original_provider_keys
            self.app.model_lock.release()
            self._json({"error": str(exc)}, 400)
            return

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, OSError):
            K.PROVIDER_KEYS[:] = original_provider_keys
            self.app.model_lock.release()
            return

        def emit(event):
            payload = json.dumps(event, ensure_ascii=False).encode("utf-8")
            self.wfile.write(b"data: " + payload + b"\n\n")
            self.wfile.flush()

        worklog = []
        artifacts_by_path = {}
        finished = False
        disconnected = False
        temperature = {"thinking": 0.2, "expert": 0.15}[effort]
        max_tokens = {"thinking": 8192, "expert": 16384}[effort]
        use_thinking = True
        reasoning_effort = {"thinking": "medium", "expert": "high"}[effort]
        generation_seconds = 0.0
        thinking_seconds = 0.0
        generation_calls = 0
        usage_calls = 0
        provider_output_tokens = 0
        output_chars = 0
        model_summary_chunks = []

        def log_step(text, ok=None, status="Working…", detail=None):
            item = {"text": str(text), "ok": ok, "detail": str(detail or "")[:12000]}
            worklog.append(item)
            if len(worklog) > 120:
                del worklog[:-120]
            emit({"type": "phase", "text": item["text"], "ok": ok, "detail": item["detail"], "status": status})

        def code_mode_allowed():
            current_chat = self.app.get_chat(chat_id)
            return mode == "code" and bool(current_chat) and current_chat.get("mode") == "code"

        def generate_response():
            nonlocal generation_seconds, generation_calls, usage_calls, thinking_seconds
            nonlocal provider_output_tokens, output_chars, active_candidate_index, model
            available = [index for index in range(len(route_candidates)) if index not in failed_candidate_indices]
            if active_candidate_index in available:
                available.remove(active_candidate_index)
                available.insert(0, active_candidate_index)
            last_error = None
            for candidate_index in available:
                candidate = route_candidates[candidate_index]
                chunks = []
                summary_chunks = []
                lead = "\n\n" if model_summary_chunks else ""
                thinking_ended_at = None
                think_tail = ""
                usage_tokens_this_call = None
                started_at = time.monotonic()
                generation_calls += 1
                stream = None
                try:
                    apply_route_candidate(candidate)
                    stream = K.stream_chat(
                        messages,
                        model=model,
                        think=use_thinking,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        reasoning_effort=reasoning_effort,
                    )
                    for kind, chunk in stream:
                        if not chunk:
                            continue
                        kind_name = str(kind or "").strip().lower().replace("-", "_")
                        if kind_name in MODEL_SUMMARY_KINDS:
                            if isinstance(chunk, str):
                                piece = chunk if summary_chunks else lead + chunk
                                summary_chunks.append(piece)
                                emit({"type": "reasoning", "text": piece})
                            continue
                        if kind_name in THINKING_KINDS:
                            continue
                        if kind_name in ("usage", "stats", "metadata") and isinstance(chunk, dict):
                            usage = chunk.get("usage") if isinstance(chunk.get("usage"), dict) else chunk
                            token_value = next((usage.get(key) for key in ("completion_tokens", "output_tokens", "completionTokens", "outputTokens") if usage.get(key) is not None), None)
                            try:
                                if token_value is not None:
                                    usage_tokens_this_call = max(int(token_value), usage_tokens_this_call or 0)
                            except (TypeError, ValueError):
                                pass
                            continue
                        if kind_name == "content":
                            text = chunk if isinstance(chunk, str) else str(chunk)
                            chunks.append(text)
                            if thinking_ended_at is None:
                                think_tail = (think_tail + text)[-48:]
                                if summary_chunks or THINK_CLOSE_RE.search(think_tail):
                                    thinking_ended_at = time.monotonic()
                    raw_response = "".join(chunks).strip()
                    if not raw_response:
                        raise RuntimeError("The model returned no response text.")
                    active_candidate_index = candidate_index
                    if summary_chunks:
                        thinking_seconds += max(0.0, (thinking_ended_at or time.monotonic()) - started_at)
                        model_summary_chunks.extend(summary_chunks)
                    output_chars += sum(len(chunk) for chunk in chunks)
                    return raw_response
                except (BrokenPipeError, ConnectionResetError):
                    raise
                except Exception as exc:
                    last_error = exc
                    if summary_chunks:
                        emit({"type": "reasoning_reset", "text": "".join(model_summary_chunks).strip()})
                    failed_candidate_indices.add(candidate_index)
                    remaining = [index for index in range(len(route_candidates)) if index not in failed_candidate_indices]
                    if not remaining:
                        raise RuntimeError("Every model/key entry in this group failed. Last error: " + str(exc)) from exc
                    next_candidate = route_candidates[remaining[0]]
                    log_step(
                        "This model or key did not complete the request. Switching to the next group entry…",
                        False,
                        status="Trying fallback…",
                        detail="Failed entry: {}\nReason: {}\nNext entry: {}".format(
                            candidate.get("member_label") or candidate.get("model"),
                            str(exc)[:1200],
                            next_candidate.get("member_label") or next_candidate.get("model"),
                        ),
                    )
                finally:
                    generation_seconds += max(0.0, time.monotonic() - started_at)
                    if usage_tokens_this_call is not None:
                        usage_calls += 1
                        provider_output_tokens += usage_tokens_this_call
                    close = getattr(stream, "close", None)
                    if callable(close):
                        try:
                            close()
                        except Exception:
                            pass
            raise RuntimeError("No working model/key entry was found." + (" " + str(last_error) if last_error else ""))

        def response_stats():
            seconds = max(generation_seconds, 0.0)
            has_exact_usage = generation_calls > 0 and usage_calls == generation_calls
            if has_exact_usage:
                token_count = provider_output_tokens
                estimated = False
            else:
                token_count = max(1, round(output_chars / 4)) if output_chars else 0
                estimated = True
            rate = token_count / seconds if seconds > 0 and token_count > 0 else 0.0
            return {
                "thought_seconds": round(seconds, 1),
                "output_tokens": int(token_count),
                "tokens_per_second": round(rate, 1),
                "tokens_estimated": estimated,
            }

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
            final_raw = ""
            validation_errors = []
            max_rounds = 4
            for round_index in range(max_rounds):
                if round_index == 0 and DESKTOP_VISUAL_REQUEST_RE.search(user_text):
                    if mode == "normal":
                        final_raw = (
                            "Normal mode cannot access your Mac's local files or folders. Switch this conversation to Code mode "
                            "to let me read and list files inside the configured project directory (and inspect files you attach). "
                            "Code mode still cannot capture or view your live desktop, screen, or GUI. To show me what is on screen, "
                            "attach a screenshot. File changes and developer commands remain subject to separate approval."
                        )
                    else:
                        final_raw = (
                            "I can't capture or view your live desktop, screen, or GUI in either mode. Code mode can read and list files "
                            "inside the configured project directory and inspect files you attach, but it does not provide live screen access. "
                            "Attach a screenshot to show me what is on screen. File changes and developer commands still require your approval."
                        )
                    log_step("Explained the desktop and Code mode access boundary.", True, status="Mode guidance ready…", detail="No live screen capture or unauthorized local access was attempted.")
                    break
                if round_index == 0 and mode == "normal" and NORMAL_MODE_LOCAL_ACCESS_RE.search(user_text):
                    final_raw = (
                        "Normal mode cannot access local files, folders, or terminal commands. Switch this conversation to Code mode "
                        "to let me read and list files inside the configured project directory and inspect files you attach. "
                        "Any proposed file changes or developer commands will be shown for your approval before they can run or be applied."
                    )
                    log_step("Explained the Code mode access boundary.", True, status="Mode guidance ready…", detail="No local file, directory, or terminal action was attempted in Normal mode.")
                    break
                if round_index == 0 and mode == "normal" and SYSTEM_PROMPT_REQUEST_RE.fullmatch(user_text):
                    final_raw = "Normal mode cannot inspect local application configuration. Switch this conversation to Code mode to view Kurt's editable system prompt."
                    log_step("Blocked a local configuration read in Normal mode.", False, status="Action blocked…", detail="No local configuration or file was read.")
                    break
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
                raw = generate_response()
                if not raw:
                    final_raw = "Kurt did not return any response text. Check the selected model and provider configuration."
                    break

                if not code_mode_allowed() and re.search(r"@@@\s*(?:RUN|READ|LIST|WRITE|EDIT|SYSTEM_PROMPT)\b", raw, re.I):
                    final_raw = "Normal mode cannot access, read, create, edit, or run anything on your computer. Switch this conversation to Code mode and send the request again."
                    log_step("Blocked a local computer action in Normal mode.", False, status="Action blocked…", detail="No file, directory, attachment, project, or terminal action was performed. Switch to Code mode to continue.")
                    break

                tool_actions = []
                for match in RUN_ACTION_RE.finditer(raw):
                    tool_actions.append((match.start(), "RUN", match.group(1).strip() or match.group(2).strip()))
                for match in SEARCH_ACTION_RE.finditer(raw):
                    tool_actions.append((match.start(), "SEARCH", match.group(1).strip() or match.group(2).strip()))
                for match in READ_ACTION_RE.finditer(raw):
                    tool_actions.append((match.start(), "READ", match.group(1).strip() or match.group(2).strip()))
                for match in LIST_ACTION_RE.finditer(raw):
                    tool_actions.append((match.start(), "LIST", match.group(1).strip() or match.group(2).strip()))
                tool_actions.sort(key=lambda item: item[0])

                if tool_actions:
                    if round_index >= max_rounds - 1:
                        validation_errors.append("Kurt reached the maximum of {} tool rounds.".format(max_rounds))
                    else:
                        results = []
                        for _position, action, argument in tool_actions:
                            if action == "RUN":
                                if not code_mode_allowed():
                                    results.append("[Command blocked] Local commands are unavailable because this conversation is not currently in Code mode. Tell the user to switch to Code mode.")
                                elif not argument:
                                    results.append("[Command rejected] RUN action did not include a command.")
                                else:
                                    pending_number = 1 + sum(1 for item in artifacts_by_path.values() if item.get("kind") == "command")
                                    command_path = "Command approval {}".format(pending_number)
                                    artifacts_by_path[command_path] = {
                                        "kind": "command", "path": command_path, "content": argument,
                                        "approval_required": True,
                                        "validation": {"ok": True, "checked": True, "message": "Awaiting your approval · not executed"},
                                    }
                                    log_step("Prepared a command for approval: " + argument, None, status="Awaiting approval…", detail="The command has not run. Review the command card and explicitly approve it before execution. Approved commands are restricted to the isolated chat workspace.")
                                    results.append("[Command approval pending]\nThis command has been presented to the user for approval and has NOT been executed: {}\nDo not claim it ran or repeat the action. Continue by explaining what approval is needed.".format(argument))
                            elif action == "READ":
                                if not code_mode_allowed():
                                    results.append("[Read blocked] Local file access is unavailable because this conversation is not currently in Code mode. Tell the user to switch to Code mode.")
                                elif not argument:
                                    results.append("[Read rejected] READ action needs a project-relative file path.")
                                else:
                                    try:
                                        content = self.app.read_project_file(argument)
                                        details = "[Project file read]\nPath: {}\n\n{}".format(argument, content)
                                        log_step("Read project file: " + argument, True, status="Reading project…", detail="Read-only access under the configured Code mode project root. No file was changed.")
                                        results.append(details)
                                    except Exception as exc:
                                        log_step("Project file read failed: " + argument, False, status="Could not read file…", detail=str(exc))
                                        results.append("[Project file read failed]\nPath: {}\nReason: {}".format(argument, exc))
                            elif action == "LIST":
                                if not code_mode_allowed():
                                    results.append("[Directory listing blocked] Local file access is unavailable because this conversation is not currently in Code mode. Tell the user to switch to Code mode.")
                                else:
                                    display_path = argument or "."
                                    try:
                                        items = self.app.list_project_directory(argument)
                                        lines = ["[Project directory listing]\nPath: " + display_path]
                                        lines.extend(("[DIR] " if item["directory"] else "[FILE] ") + item["path"] + (" ({})".format(item["size"]) if item["size"] else "") for item in items)
                                        details = "\n".join(lines)
                                        log_step("Listed project directory: " + display_path, True, status="Reading project…", detail="Read-only directory listing under the configured Code mode project root.")
                                        results.append(details)
                                    except Exception as exc:
                                        log_step("Project directory listing failed: " + display_path, False, status="Could not list directory…", detail=str(exc))
                                        results.append("[Project directory listing failed]\nPath: {}\nReason: {}".format(display_path, exc))
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
            stats = response_stats()
            response_summary, reply = split_thinking(final_raw)
            response_summary = response_summary or "".join(model_summary_chunks).strip()
            if not response_summary and final_raw:
                response_summary = build_public_activity_rationale(
                    user_text, mode, worklog, artifacts, validation_errors
                )
            reply = strip_unsupported_actions(reply)
            reply = re.sub(r"\n{3,}", "\n\n", reply).strip()
            if validation_errors:
                warning = "\n\n**Validation warning:** " + "; ".join(validation_errors)
                reply = (reply + warning).strip()
            visible = K.BLOCK_RE.sub("", reply).strip()
            if visible and chat.get("title") in (None, "New conversation"):
                chat["title"] = self.app.generate_chat_title(user_text, visible, model)
            if not visible and not artifacts:
                visible = "Kurt completed the request without a text response or validated file artifact. Check the work log for tool and validation results."
            if visible or artifacts:
                record = {
                    "role": "assistant", "content": visible, "artifacts": artifacts,
                    "worklog": worklog, "reasoning": response_summary,
                    "thinking_seconds": round(thinking_seconds or generation_seconds, 1),
                    "effort": effort, "mode": mode, "route": route_info["route"],
                    "model": model, "stats": stats,
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
                "worklog": worklog, "reasoning": response_summary,
                "thinking_seconds": round(thinking_seconds or generation_seconds, 1),
                "chat_id": chat_id, "title": chat["title"], "route": route_info["route"],
                "model": model, "effort": effort, "mode": mode, "stats": stats,
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
            K.PROVIDER_KEYS[:] = original_provider_keys
            self.app.model_lock.release()
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