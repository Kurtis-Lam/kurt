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
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import kurt as K
from kurt_sandbox import is_code_file, validate_content

CONFIG_DIR = K.CONFIG_DIR
WEB_SETTINGS_FILE = CONFIG_DIR / "web_settings.json"
CHATS_FILE = CONFIG_DIR / "chats.json"
MAX_BODY = 32 * 1024 * 1024
MAX_IMPORT_BYTES = 20 * 1024 * 1024
MAX_WORKSPACE_CONTEXT = 1_400_000
MAX_CHATS = 100
MAX_MESSAGES_PER_CHAT = 300

WEB_INSTRUCTIONS = r"""
WEB INTERFACE FILE OUTPUT RULES:
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
- Do not emit READ or RUN action blocks. Attached text files are included in context;
  binary assets and archives are preserved in the temporary workspace.
- Do not emit shell commands or claim to have inspected files that were not attached.
- Keep paths relative to this conversation's temporary workspace. Never target paths
  outside it or claim that the original project was changed.
"""

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
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);font:14.5px/1.55 var(--sans);overflow:hidden;-webkit-font-smoothing:antialiased}
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
.app{height:100vh;height:100dvh;display:grid;grid-template-columns:268px minmax(0,1fr)}
.sidebar{background:var(--side);border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0;padding:12px 10px;gap:2px}
.brand{display:flex;align-items:center;gap:10px;padding:4px 8px 14px}
.brand b{font-size:17px;letter-spacing:-.01em;font-weight:650}
.nav-btn{display:flex;align-items:center;gap:10px;width:100%;text-align:left;background:transparent;border:0;border-radius:9px;padding:8px 10px;color:var(--text)}
.nav-btn:hover{background:var(--raise)}
.nav-btn .ico{width:22px;height:22px;display:grid;place-items:center;color:var(--muted);flex:none}
.nav-btn.new .ico{background:var(--green);color:var(--green-ink);border-radius:50%}
.nav-btn.new .ic{width:15px;height:15px;stroke-width:2.4}
.side-title{color:var(--muted);font-weight:600;font-size:12px;padding:16px 10px 6px}
.chat-list{overflow:auto;min-height:80px;flex:1}
.chat-item{padding:7px 10px;border-radius:8px;cursor:pointer;color:var(--muted);margin:1px 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chat-item:hover{background:var(--raise);color:var(--text)}
.chat-item.active{background:var(--raise);color:var(--text)}
.side-bottom{border-top:1px solid var(--line);padding-top:8px;display:grid;gap:2px}
.local-note{font-size:12px;color:var(--muted);padding:6px 10px 0}
.status-dot{width:7px;height:7px;border-radius:50%;display:inline-block;background:var(--green);margin-right:7px}
.stage{display:grid;grid-template-rows:auto 2px minmax(0,1fr);min-width:0;min-height:0}
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
.main{min-width:0;min-height:0;display:flex;flex-direction:column}
.messages{flex:1;overflow-y:auto;overscroll-behavior:contain;padding:24px max(20px,calc((100% - 760px)/2)) 12px}
.main.empty{justify-content:center;padding-bottom:7vh}
.main.empty .messages{flex:0 0 auto;overflow:visible;padding:0 20px}
.welcome{text-align:center;padding-bottom:22px}
.welcome h1{font:400 34px/1.2 var(--serif);letter-spacing:-.01em;margin:0}
.message{max-width:760px;margin:0 auto 26px}
.message.user .bubble{display:inline-block;max-width:100%;background:var(--raise);border-radius:16px;padding:9px 15px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:15px;line-height:1.6}
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
.msg-foot{display:flex;align-items:center;gap:4px;margin-top:6px;min-height:30px}
.msg-foot .mark{margin-left:auto}
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
.codebox pre{margin:0;padding:14px;overflow:auto;max-height:480px;white-space:pre;font:13px/1.6 var(--mono)}
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
.file-actions{display:flex;gap:8px;flex-wrap:wrap;margin:9px 0}
.file-row .file-open{flex:none}
.file-row .file-add{flex:none}
.upload-list{font-size:12px;color:var(--muted);padding:6px 0;overflow-wrap:anywhere}
.inline-note{color:var(--muted);font-size:12px;margin:5px 0 10px}
.check-row{display:flex;align-items:center;gap:9px;color:var(--muted);font-size:13px;margin:8px 0 13px}
.check-row input{width:auto}
.file-picker{border:1px solid var(--line);border-radius:10px;max-height:260px;overflow-y:auto}
.file-row{padding:7px 10px;border-bottom:1px solid var(--line);display:flex;gap:9px;align-items:center}
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
.messages{padding:18px 14px 8px}
.composer-wrap{padding:6px 10px 10px}
.welcome h1{font-size:28px}
.settings-grid{grid-template-columns:1fr}
}
@media(prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important;scroll-behavior:auto!important}}
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
<button class="nav-btn" id="openFiles"><span class="ico"><svg class="ic"><use href="#i-file"/></svg></span>Workspace files</button>
<div class="side-title">Recents</div>
<div id="chatList" class="chat-list"></div>
<div class="side-bottom">
<button class="nav-btn" id="sidebarDownload"><span class="ico"><svg class="ic"><use href="#i-download"/></svg></span>Download a model</button>
<button class="nav-btn" id="sidebarSettings"><span class="ico"><svg class="ic"><use href="#i-sliders"/></svg></span>Settings</button>
<div class="local-note"><span class="status-dot"></span>Running on localhost only</div>
</div>
</aside>
<div class="stage">
<header class="topbar">
<button class="icon-btn menu-btn" id="menuBtn" aria-label="Open menu"><svg class="ic"><use href="#i-menu"/></svg></button>
<div class="titles"><div id="chatTitle" class="chat-title">New chat</div><div id="chatMeta" class="chat-meta"></div></div>
<span class="workspace-label" id="workspaceLabel" title="Workspace directory"></span>
<button class="pill" id="exportWorkspace" title="Download this conversation's isolated workspace as a ZIP">Download workspace</button>
<button class="icon-btn" id="clearChat" title="Clear conversation" aria-label="Clear conversation"><svg class="ic"><use href="#i-trash"/></svg></button>
<button class="icon-btn" id="themeBtn" title="Switch colour theme" aria-label="Switch colour theme"><svg class="ic"><use href="#i-theme"/></svg></button>
</header>
<div class="download-strip" title="Background model download"><div id="downloadFill" class="download-fill"></div><div id="downloadCaption" class="download-caption"></div></div>
<main class="main empty" id="main">
<div id="messages" class="messages"></div>
<div class="composer-wrap">
<button class="to-bottom" id="toBottom" aria-label="Scroll to latest message" title="Scroll to latest"><svg class="ic"><use href="#i-down"/></svg></button>
<div class="composer">
<textarea id="prompt" rows="1" placeholder="Message Kurt" aria-label="Message Kurt"></textarea>
<div class="compose-bottom">
<button class="icon-btn" id="contextBtn" title="Attach workspace files" aria-label="Attach workspace files"><svg class="ic"><use href="#i-plus"/></svg></button>
<span class="spacer"></span>
<select id="topModel" class="model-pill" title="Active model" aria-label="Active model"></select>
<button class="send" id="sendBtn" title="Send message" aria-label="Send message" disabled><svg class="ic"><use href="#i-up"/></svg></button>
</div>
</div>
<div class="compose-hint" id="composeHint">Kurt can make mistakes. Files are staged in this chat’s isolated workspace. Project files stay unchanged unless you explicitly enable and apply changes.</div>
</div>
<div class="chips" id="chips"></div>
</main>
</div></div>
<div class="modal-backdrop" id="settingsModal"><div class="modal"><div class="modal-head"><h2>Kurt settings</h2><button class="ghost" data-close="settingsModal">Close</button></div><div class="modal-body">
<div class="settings-grid"><div class="field"><label>Active model</label><select id="settingsModel"></select></div><div class="field"><label>Project directory (read-only by default)</label><input id="projectRoot" placeholder="/Users/you/project"></div></div>
<label class="check-row"><input id="allowProjectWrites" type="checkbox"> Allow applying generated files to the project (each file still requires confirmation)</label>
<div class="field"><label>System prompt</label><textarea id="systemPrompt" style="min-height:150px"></textarea></div><div class="row"><button class="primary" id="saveSettings">Save settings</button><button id="reloadSettings">Reload from disk</button></div>
<div class="section" id="localModelsSection"><h3>Local models</h3><div class="inline-note">Installed Ollama models are listed here. Select one to make it the active model, or download another model below.</div><div id="localModelList"></div><div class="row"><input id="downloadModel" aria-label="Ollama model name" placeholder="Model ID, e.g. qwen2.5-coder:7b"><button id="startDownload" class="primary">Download model</button></div><div class="inline-note" id="localDownloadStatus">Downloads run in the background; progress appears at the top of the window.</div></div>
<div class="section"><h3>API key groups</h3><div class="inline-note">Keys in the same provider + group form a rotation pool. Kurt retries another key for quota, rate-limit, authentication, server, and connection errors before response content starts.</div><div id="keysList"></div><div class="settings-grid"><div class="field"><label>Provider</label><select id="keyProvider"></select></div><div class="field"><label>Key group</label><input id="keyGroup" list="keyPoolOptions" value="default" placeholder="e.g. personal"></div><div class="field"><label>Account label</label><input id="keyLabel" placeholder="Personal account"></div><div class="field"><label>API key</label><input id="keyValue" type="password" placeholder="Paste key (stored locally)"></div></div><button id="addKey" class="primary">Add API key</button><datalist id="keyPoolOptions"></datalist></div>
<div class="section"><h3>Remote API models</h3><div class="inline-note">Each model uses the provider and key group selected below.</div><div class="settings-grid"><div class="field"><label>Provider</label><select id="remoteProvider"></select></div><div class="field"><label>API model ID</label><input id="remoteModelId" placeholder="e.g. openrouter/free"></div><div class="field"><label>Display name</label><input id="remoteLabel" placeholder="Model name"></div><div class="field"><label>Key group</label><input id="remoteGroup" list="keyPoolOptions" value="default"></div><div class="field" style="grid-column:1/-1"><label>API base URL (optional for standard providers)</label><input id="remoteBase" placeholder="https://api.example.com/v1"></div></div><button id="addRemote" class="primary">Add remote model</button><div id="remoteList" style="margin-top:10px"></div></div>
</div></div></div>
<div class="modal-backdrop" id="filesModal"><div class="modal"><div class="modal-head"><h2>Add files to this chat</h2><button class="ghost" data-close="filesModal">Close</button></div><div class="modal-body"><p style="color:var(--muted);margin-top:0">Files and folders are copied into this conversation’s isolated temporary workspace. Folders and ZIP files are automatically archived and unpacked there. Readable text is included with requests to your selected AI provider; common secret files such as .env and private keys are skipped. Your original project is not edited.</p><div class="field"><label>Browse your configured project</label><div class="row"><input id="fileFolder" value="" placeholder="Project root"><button id="refreshFiles">Browse</button></div></div><div class="file-actions"><button id="selectCurrentFolder">Add this folder</button><button id="chooseLocalFiles">Choose files from computer</button><button id="chooseLocalFolder">Choose folder from computer</button><input id="localFiles" type="file" multiple hidden><input id="localFolder" type="file" webkitdirectory directory multiple hidden></div><div class="upload-list" id="uploadList">No local files selected.</div><div class="inline-note">Use Open to navigate into a folder, or Add folder to include it recursively.</div><div class="file-picker" id="filePicker"></div><div class="row" style="margin-top:12px"><button class="primary" id="attachFiles">Add to chat workspace</button></div></div></div></div>
<div class="toast" id="toast"></div>
<script>
const TOKEN='__TOKEN__';const HINT='Kurt can make mistakes. Attachments are copied into an isolated chat workspace; the project stays unchanged unless you explicitly apply a file.';let state={models:[],providers:[],keys:[],remoteModels:[],chats:[],chatId:null,projectRoot:'',selectedFiles:[],selectedFolders:[],pendingUploads:[],contextCount:0,busy:false,abort:null,run:0,settings:{}};
const $=id=>document.getElementById(id);const api=async(path,body)=>{const res=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json','X-Kurt-Token':TOKEN},body:body?JSON.stringify(body):undefined});const data=await res.json().catch(()=>({error:'Invalid server response'}));if(!res.ok)throw new Error(data.error||res.statusText);return data};
function toast(msg){const el=$('toast');el.textContent=msg;el.classList.add('show');setTimeout(()=>el.classList.remove('show'),2600)}
function modelLabel(m){return `${m.label||m.name||m.model} · ${m.provider}${m.current?' · active':''}`}
function fillModels(){const html=state.models.map(m=>`<option value="${esc(m.ref)}">${esc(modelLabel(m))}</option>`).join('');$('topModel').innerHTML=html||'<option>No models configured</option>';$('settingsModel').innerHTML=html||'<option>No models configured</option>';if(state.settings.model){$('topModel').value=state.settings.model;$('settingsModel').value=state.settings.model}renderLocalModels()}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function inline(s){const spans=[];s=s.replace(/`([^`\n]+)`/g,(_,c)=>{spans.push('<code>'+c+'</code>');return '\u0001'+(spans.length-1)+'\u0001'});s=s.replace(/\*\*([^*\n]+?)\*\*/g,'<strong>$1</strong>').replace(/(^|[^*\w])\*([^*\n]+?)\*(?![*\w])/g,'$1<em>$2</em>').replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,'<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');return s.replace(/\u0001(\d+)\u0001/g,(_,i)=>spans[+i])}
function mdBlocks(text){const lines=esc(text).split('\n');let html='',para=[],list=null,quote=[];const flushP=()=>{if(para.length){html+='<p>'+inline(para.join('\n')).replace(/\n/g,'<br>')+'</p>';para=[]}};const flushL=()=>{if(list){html+='<'+list.t+'>'+list.items.map(i=>'<li>'+inline(i)+'</li>').join('')+'</'+list.t+'>';list=null}};const flushQ=()=>{if(quote.length){html+='<blockquote>'+inline(quote.join('<br>'))+'</blockquote>';quote=[]}};for(const line of lines){let m;if(!line.trim()){flushP();flushL();flushQ();continue}if(m=/^(#{1,6})\s+(.*)$/.exec(line)){flushP();flushL();flushQ();const n=Math.min(m[1].length,3);html+='<h'+n+'>'+inline(m[2])+'</h'+n+'>';continue}if(/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)){flushP();flushL();flushQ();html+='<hr>';continue}if(m=/^&gt;\s?(.*)$/.exec(line)){flushP();flushL();quote.push(m[1]);continue}if(m=/^\s*[-*+]\s+(.*)$/.exec(line)){flushP();flushQ();if(!list||list.t!=='ul'){flushL();list={t:'ul',items:[]}}list.items.push(m[1]);continue}if(m=/^\s*\d+[.)]\s+(.*)$/.exec(line)){flushP();flushQ();if(!list||list.t!=='ol'){flushL();list={t:'ol',items:[]}}list.items.push(m[1]);continue}flushL();flushQ();para.push(line)}flushP();flushL();flushQ();return html}
function renderMarkdown(src){src=String(src||'');return src.split(/(```[^\n]*\n[\s\S]*?```|```[^\n]*\n[\s\S]*$|```[^\n]*$)/).map((seg,i)=>{if(i%2===0)return mdBlocks(seg);const nl=seg.indexOf('\n');const lang=(nl<0?seg.slice(3):seg.slice(3,nl)).trim().split(/\s+/)[0]||'';let code=nl<0?'':seg.slice(nl+1);if(nl>=0&&seg.endsWith('```'))code=code.replace(/\n?```$/,'');return '<div class="codebox"><div class="codehead"><span>'+esc(lang||'code')+'</span><button class="copy-mini" data-copy-code>Copy code</button></div><pre><code>'+esc(code.replace(/\n$/,''))+'</code></pre></div>'}).join('')}
function splitThink(raw,streaming){let think='',answer='',open=false;const closer=/<\/think(?:ing)?>/i.exec(raw),opener=/<think(?:ing)?>/i.exec(raw);if(closer&&(!opener||closer.index<opener.index)){think+=raw.slice(0,closer.index)+'\n';raw=raw.slice(closer.index+closer[0].length)}while(raw.length){const m=/<think(?:ing)?>/i.exec(raw);if(!m){answer+=raw;break}answer+=raw.slice(0,m.index);const after=raw.slice(m.index+m[0].length);const c=/<\/think(?:ing)?>/i.exec(after);if(!c){think+=after;open=true;break}think+=after.slice(0,c.index)+'\n';raw=after.slice(c.index+c[0].length)}answer=answer.replace(/^\s+/,'');if(streaming)answer=answer.replace(/<\/?(?:t(?:h(?:i(?:n(?:k(?:i(?:n(?:g)?)?)?)?)?)?)?)?$/i,'');return{think:think.trim(),answer,open}}
function fmtSecs(s){s=Math.max(1,Math.round(s));return s<60?s+'s':Math.floor(s/60)+'m '+(s%60)+'s'}
function bindCopy(root){root.querySelectorAll('[data-copy-code]').forEach(b=>b.onclick=()=>{const code=b.closest('.codebox').querySelector('code');navigator.clipboard.writeText(code?code.textContent:'').then(()=>toast('Code copied'))})}
function thinkBlock(){const d=document.createElement('div');d.className='think';d.hidden=true;d.innerHTML='<button class="think-head" type="button" aria-expanded="false"><svg class="ic chev"><use href="#i-chev"/></svg><span class="think-label">Thinking</span></button><div class="think-body"></div>';d.querySelector('.think-head').onclick=()=>{const open=d.classList.toggle('open');d.querySelector('.think-head').setAttribute('aria-expanded',String(open))};return d}
function setThink(node,text,opt){const o=opt||{};const t=node.querySelector('.think');if(!t)return;if(!text&&!o.live){t.hidden=true;return}t.hidden=false;t.classList.toggle('live',!!o.live);t.querySelector('.think-label').textContent=o.live?'Thinking…':(o.secs?'Thought for '+fmtSecs(o.secs):'Thought process');const b=t.querySelector('.think-body');if(b.textContent!==text){b.textContent=text;if(o.live&&t.classList.contains('open'))b.scrollTop=b.scrollHeight}}
function endThink(node){const t=node.querySelector('.think');if(!t)return;t.classList.remove('live');const l=t.querySelector('.think-label');if(l.textContent==='Thinking…')l.textContent='Thought process'}
let follow=true;
function scrollEnd(force){const m=$('messages');if(force||follow)m.scrollTop=m.scrollHeight}
function setEmpty(on){$('main').classList.toggle('empty',on);if(on)$('toBottom').classList.remove('show')}
function setTitle(title,meta){$('chatTitle').textContent=title||'New chat';$('chatMeta').textContent=meta||''}
function autosize(){const t=$('prompt');t.style.height='auto';t.style.height=Math.min(t.scrollHeight,220)+'px'}
function updateSend(){$('sendBtn').disabled=!state.busy&&!$('prompt').value.trim()}
function hint(){$('composeHint').textContent=state.contextCount?state.contextCount+' item(s) in this chat’s isolated workspace':HINT}
function setBusy(b){state.busy=b;const btn=$('sendBtn');btn.querySelector('use').setAttribute('href',b?'#i-stop':'#i-up');btn.title=b?'Stop response':'Send message';btn.setAttribute('aria-label',btn.title);updateSend()}
function cancelRun(){if(state.abort)state.abort.abort();state.abort=null;state.run++;setBusy(false)}
function renderMessage(role,content,artifacts,extra){const ex=extra||{};const wrap=document.createElement('div');wrap.className='message '+role;wrap.dataset.raw=content||'';const bubble=document.createElement('div');bubble.className='bubble';wrap.appendChild(bubble);if(role==='user'){bubble.textContent=content}else{bubble.appendChild(thinkBlock());bubble.insertAdjacentHTML('beforeend','<div class="content"></div><div class="artifacts"></div><div class="msg-foot"><button class="icon-btn copy-msg" title="Copy" aria-label="Copy message"><svg class="ic"><use href="#i-copy"/></svg></button><svg class="mark" viewBox="0 0 20 16" aria-hidden="true"><use href="#kurt-k"/></svg></div>');bubble.querySelector('.content').innerHTML=renderMarkdown(content);bindCopy(wrap);setThink(wrap,ex.thinking||'',{secs:ex.secs||null});const slot=bubble.querySelector('.artifacts');(artifacts||[]).forEach(a=>slot.appendChild(artifactCard(a)));bubble.querySelector('.copy-msg').onclick=()=>navigator.clipboard.writeText(wrap.dataset.raw||'').then(()=>toast('Message copied'))}$('messages').appendChild(wrap);return wrap}
function artifactCard(a){const card=document.createElement('div');card.className='artifact'+(a.validation?.ok?'':' bad');card.innerHTML=`<div class="artifact-top"><div class="file-icon"><svg class="ic"><use href="#i-file"/></svg></div><div class="file-name">${esc(a.path)}</div><div class="file-check">${esc(a.validation?.message||'Saved in temporary workspace')}</div><span class="artifact-chev"><svg class="ic"><use href="#i-chev"/></svg></span></div><div class="artifact-body"><div class="codebox"><div class="codehead"><span>${esc((a.path.split('.').pop()||'FILE').toUpperCase())}</span><button class="copy-mini copy-artifact">Copy code</button></div><pre><code></code></pre></div><div class="artifact-actions"><button class="copy-artifact">Copy</button><button class="download-artifact">Download file</button><button class="primary apply-artifact">Apply to project</button></div></div>`;card.querySelector('code').textContent=a.content;card.querySelector('.artifact-top').onclick=()=>card.classList.toggle('open');card.querySelectorAll('.copy-artifact').forEach(b=>b.onclick=()=>navigator.clipboard.writeText(a.content).then(()=>toast('File content copied')));card.querySelector('.download-artifact').onclick=()=>{const blob=new Blob([a.content],{type:'application/octet-stream'}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=a.path.split('/').pop();link.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};card.querySelector('.apply-artifact').onclick=async()=>{if(!a.validation?.ok){toast('Fix validation errors before applying');return}if(!state.settings.allow_project_writes){toast('Enable project writes in Settings first');return}const overwrite=confirm(`Apply ${a.path} to ${state.projectRoot}?${a.exists?'\nThis will overwrite the existing project file.':''}\n\nThe isolated workspace copy will remain unchanged.`);if(!overwrite)return;try{const r=await api('/api/file/write',{path:a.path,content:a.content,overwrite,authorized:true});toast(r.message||'Applied to project');}catch(e){toast(e.message)}};return card}
const SUGGESTIONS=[['Understand my project','Inspect the current project structure and explain its architecture.'],['Build a feature','Help me plan and implement a new feature. Ask for any missing requirements first.'],['Generate a file','Write a small utility script and return it as a file card.'],['Debug code','Help me debug this issue. Start by asking for the relevant error and files.']];
function showWelcome(){setEmpty(true);follow=true;$('messages').innerHTML='<div class="welcome"><svg class="mark xl" viewBox="0 0 20 16" aria-hidden="true"><use href="#kurt-k"/></svg><h1>What are we building?</h1></div>';$('chips').innerHTML=SUGGESTIONS.map(s=>'<button class="chip" data-prompt="'+esc(s[1])+'">'+esc(s[0])+'</button>').join('');document.querySelectorAll('#chips [data-prompt]').forEach(b=>b.onclick=()=>{$('prompt').value=b.dataset.prompt;autosize();updateSend();$('prompt').focus()})}
function renderChats(){const el=$('chatList');el.innerHTML='';state.chats.forEach(c=>{const d=document.createElement('div');d.className='chat-item'+(c.id===state.chatId?' active':'');d.textContent=c.title||'New conversation';d.onclick=()=>loadChat(c.id);el.appendChild(d)})}
async function refreshBootstrap(){const d=await api('/api/bootstrap');state.models=d.models||[];state.providers=d.providers||[];state.keys=d.keys||[];state.remoteModels=d.remote_models||[];state.settings=d.settings||{};state.projectRoot=d.project_root||'';$('workspaceLabel').textContent=state.projectRoot;$('projectRoot').value=state.projectRoot;$('systemPrompt').value=state.settings.system_prompt||'';$('allowProjectWrites').checked=!!state.settings.allow_project_writes;fillModels();renderKeys();renderRemoteModels();fillProviders()}
function fillProviders(){const opts=state.providers.map(p=>`<option value="${esc(p.id)}">${esc(p.label)}</option>`).join('');$('keyProvider').innerHTML=opts;$('remoteProvider').innerHTML=opts;const groups=[...new Set(state.keys.map(k=>k.group||'default'))].sort();$('keyPoolOptions').innerHTML=groups.map(g=>`<option value="${esc(g)}"></option>`).join('')}
function renderLocalModels(){const el=$('localModelList');if(!el)return;el.innerHTML='';const local=state.models.filter(m=>m.type==='local');if(!local.length){el.innerHTML='<div class="empty">No Ollama models are installed yet.</div>';return}local.forEach(m=>{const row=document.createElement('div');row.className='local-model-row';const grow=document.createElement('div');grow.className='grow';grow.innerHTML=`<b>${esc(m.label||m.name)}</b><small>${esc(m.size?String(m.size):'Ollama local model')}${m.current?' · active':''}</small>`;const set=document.createElement('button');set.textContent=m.current?'Current':'Set active';set.disabled=!!m.current;set.onclick=async()=>{try{await api('/api/settings',{model:m.ref});await refreshBootstrap();toast('Active model changed')}catch(e){toast(e.message)}};row.append(grow,set);el.appendChild(row)})}
function renderKeys(){const el=$('keysList');el.innerHTML='';if(!state.keys.length){el.innerHTML='<div class="empty">No API keys configured yet.</div>';fillProviders();return}const groups=new Map();state.keys.forEach(k=>{const provider=k.provider||'unknown',group=k.group||'default',key=provider+'\u0000'+group;if(!groups.has(key))groups.set(key,{provider,group,keys:[]});groups.get(key).keys.push(k)});[...groups.values()].sort((a,b)=>(a.provider+a.group).localeCompare(b.provider+b.group)).forEach(pool=>{const card=document.createElement('div');card.className='pool-card';const head=document.createElement('div');head.className='pool-head';const grow=document.createElement('div');grow.className='grow';grow.innerHTML=`<b>${esc(pool.provider)} · ${esc(pool.group)}</b><small>${pool.keys.length} key(s) · automatic failover enabled for this pool</small>`;const actions=document.createElement('div');actions.className='pool-actions';const rename=document.createElement('button');rename.textContent='Rename group';rename.onclick=async()=>{const next=prompt('New group name (all keys and linked models in this pool will move):',pool.group);if(next===null||!next.trim()||next.trim()===pool.group)return;try{await api('/api/settings',{key_action:'rename_group',provider:pool.provider,old_group:pool.group,new_group:next.trim()});await refreshBootstrap();toast('Key group renamed')}catch(e){toast(e.message)}};actions.append(rename);head.append(grow,actions);card.append(head);pool.keys.forEach(k=>{const row=document.createElement('div');row.className='key-row';row.innerHTML=`<div class="grow"><b>${esc(k.label)}</b><small>${esc(k.masked)}</small></div><button class="edit-key">Edit</button><button class="danger">Remove</button>`;row.querySelector('.edit-key').onclick=async()=>{const label=prompt('Account label:',k.label);if(label===null)return;const group=prompt('Move this key to a different pool (use Rename group to move the whole pool):',k.group);if(group===null)return;const value=prompt('New API key (leave blank to keep current):','');if(value===null)return;try{await api('/api/settings',{key_action:'edit',id:k.id,label,group,value});await refreshBootstrap();toast('API key updated')}catch(e){toast(e.message)}};row.querySelector('.danger').onclick=async()=>{if(!confirm('Remove this API key?'))return;try{await api('/api/settings',{key_action:'remove',id:k.id});await refreshBootstrap()}catch(e){toast(e.message)}};card.append(row)});el.append(card)});fillProviders()}

function renderRemoteModels(){const el=$('remoteList');el.innerHTML='';state.remoteModels.forEach(m=>{const d=document.createElement('div');d.className='model-row';d.innerHTML=`<div class="grow"><b>${esc(m.label||m.model)}</b><small>${esc(m.provider)} · ${esc(m.model)} · pool ${esc(m.key_group||'default')}</small></div><button class="edit-model">Edit</button><button class="danger">Remove</button>`;d.querySelector('.edit-model').onclick=async()=>{const model=prompt('API model ID:',m.model);if(model===null)return;const label=prompt('Display name:',m.label||m.model);if(label===null)return;const group=prompt('Key pool:',m.key_group||'default');if(group===null)return;const base=prompt('API base URL:',m.base_url||'');if(base===null)return;try{await api('/api/settings',{model_action:'edit',id:m.id,model,label,key_group:group,base_url:base});await refreshBootstrap();toast('Model settings updated')}catch(e){toast(e.message)}};d.querySelector('.danger').onclick=async()=>{if(!confirm('Remove this remote model?'))return;await api('/api/settings',{model_action:'remove',id:m.id});await refreshBootstrap()};el.appendChild(d)})}
async function refreshChats(){const d=await api('/api/chats');state.chats=d.chats||[];renderChats()}
function resetComposer(){$('prompt').value='';autosize();state.selectedFiles=[];state.selectedFolders=[];state.pendingUploads=[];state.contextCount=0;renderPendingUploads();hint();updateSend()}
async function ensureChat(){if(!state.chatId){const d=await api('/api/chats',{create:true});state.chatId=d.id}return state.chatId}
async function newChat(){cancelRun();state.chatId=null;resetComposer();showWelcome();setTitle('New chat','');renderChats();document.body.classList.remove('nav-open');$('prompt').focus()}
async function loadChat(id){cancelRun();const d=await api('/api/chats/'+encodeURIComponent(id));state.chatId=id;state.contextCount=0;try{const c=await api('/api/context/status?chat_id='+encodeURIComponent(id));state.contextCount=c.files||0}catch(e){}hint();$('messages').innerHTML='';const list=d.messages||[];if(list.length){setEmpty(false);list.forEach(m=>renderMessage(m.role,m.content,m.artifacts||[],{thinking:m.thinking,secs:m.thinking_secs}));follow=true;scrollEnd(true)}else showWelcome();setTitle(d.title||'Conversation',d.model||'');renderChats()}
async function send(){const text=$('prompt').value.trim();if(!text||state.busy)return;const run=++state.run;setBusy(true);try{await ensureChat()}catch(e){if(run===state.run)setBusy(false);toast(e.message);return}if(run!==state.run)return;$('prompt').value='';autosize();hint();const full=text;if(run!==state.run)return;if($('main').classList.contains('empty')){setEmpty(false);$('messages').innerHTML=''}follow=true;renderMessage('user',text);const node=renderMessage('assistant','');node.classList.add('live');scrollEnd(true);$('chatMeta').textContent='Generating…';const contentEl=node.querySelector('.content');let raw='',fieldThink='',tThink=null,tDone=null,pending=false;const controller=new AbortController();state.abort=controller;
const paint=()=>{pending=false;if(run!==state.run)return;const s=splitThink(raw,true);const think=[fieldThink.trim(),s.think].filter(Boolean).join('\n\n');const open=!!(s.open||(fieldThink&&!s.answer));if(think&&tThink===null)tThink=Date.now();if(tThink!==null&&!open&&tDone===null)tDone=Date.now();setThink(node,think,{live:open,secs:tDone?(tDone-tThink)/1000:null});if(s.answer){contentEl.innerHTML=renderMarkdown(s.answer);bindCopy(node)}node.dataset.raw=s.answer;scrollEnd()};
const queue=()=>{if(!pending){pending=true;requestAnimationFrame(paint)}};
try{const res=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json','X-Kurt-Token':TOKEN},body:JSON.stringify({chat_id:state.chatId,text:full,model:$('topModel').value}),signal:controller.signal});if(!res.ok){const x=await res.json().catch(()=>({}));throw new Error(x.error||res.statusText)}const reader=res.body.getReader(),decoder=new TextDecoder();let buffer='';while(true){const {value,done}=await reader.read();if(done)break;if(run!==state.run)return;buffer+=decoder.decode(value,{stream:true});let sep;while((sep=buffer.indexOf('\n\n'))>=0){const frame=buffer.slice(0,sep);buffer=buffer.slice(sep+2);const dataLine=frame.split('\n').find(x=>x.startsWith('data:'));if(!dataLine)continue;const event=JSON.parse(dataLine.slice(5).trim());if(event.type==='token'){raw+=event.text;queue()}else if(event.type==='thinking'){fieldThink+=event.text;queue()}else if(event.type==='done'){pending=false;contentEl.innerHTML=renderMarkdown(event.content);bindCopy(node);node.dataset.raw=event.content||'';setThink(node,event.thinking||'',{secs:event.thinking_secs||null});const slot=node.querySelector('.artifacts');(event.artifacts||[]).forEach(a=>slot.appendChild(artifactCard(a)));setTitle(event.title||'Conversation',event.model||'Completed');scrollEnd();await refreshChats()}else if(event.type==='error')throw new Error(event.message)}}}catch(e){if(run===state.run){if(e.name==='AbortError'){paint();refreshChats().catch(()=>{})}else{contentEl.classList.add('err');contentEl.textContent='Error: '+e.message;toast(e.message)}}}finally{node.classList.remove('live');endThink(node);if(run===state.run){setBusy(false);state.abort=null;const meta=$('chatMeta');if(meta.textContent==='Generating…')meta.textContent='Ready'}}}
$('messages').addEventListener('scroll',()=>{const m=$('messages');follow=m.scrollHeight-m.scrollTop-m.clientHeight<60;$('toBottom').classList.toggle('show',!follow&&!$('main').classList.contains('empty'))});
$('toBottom').onclick=()=>{follow=true;scrollEnd(true);$('toBottom').classList.remove('show')};
$('sendBtn').onclick=()=>{if(state.busy){if(state.abort)state.abort.abort()}else send()};
$('prompt').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&e.keyCode!==229){e.preventDefault();if(!state.busy)send()}});
$('prompt').addEventListener('input',()=>{autosize();updateSend()});
$('newChat').onclick=newChat;
async function openSettings(){await refreshBootstrap();$('settingsModal').classList.add('show')}
async function saveSettings(){const payload={project_root:$('projectRoot').value,system_prompt:$('systemPrompt').value,allow_project_writes:$('allowProjectWrites').checked};if($('settingsModel').value)payload.model=$('settingsModel').value;const d=await api('/api/settings',payload);state.settings=d.settings||{};state.settings.allow_project_writes=$('allowProjectWrites').checked;state.projectRoot=d.project_root;$('workspaceLabel').textContent=state.projectRoot;if(d.model){$('topModel').value=d.model;$('settingsModel').value=d.model}toast('Settings saved')}
async function browseFiles(){const path=$('fileFolder').value.trim();const d=await api('/api/files?path='+encodeURIComponent(path));$('fileFolder').value=d.path;const el=$('filePicker');el.innerHTML='';if(d.parent!==null){const b=document.createElement('div');b.className='file-row';b.innerHTML='<span>↰</span><label>.. (parent folder)</label>';b.onclick=()=>{$('fileFolder').value=d.parent;browseFiles()};el.appendChild(b)}d.items.forEach(item=>{const row=document.createElement('div');row.className='file-row';if(item.directory){row.innerHTML=`<span>📁</span><label>${esc(item.name)}/</label><button class="file-open">Open</button><button class="file-add">Add folder</button>`;row.querySelector('.file-open').onclick=()=>{$('fileFolder').value=item.path;browseFiles()};row.querySelector('.file-add').onclick=()=>{if(!state.selectedFolders.includes(item.path))state.selectedFolders.push(item.path);renderPendingUploads();toast('Folder staged for this chat')}}else{row.innerHTML=`<input type="checkbox" value="${esc(item.path)}" ${state.selectedFiles.includes(item.path)?'checked':''}><label>${esc(item.name)}</label><small>${esc(item.size)}</small>`;const check=row.querySelector('input');check.onchange=()=>{if(check.checked&&!state.selectedFiles.includes(item.path))state.selectedFiles.push(item.path);else if(!check.checked)state.selectedFiles=state.selectedFiles.filter(x=>x!==item.path);renderPendingUploads()}}el.appendChild(row)})}
function renderPendingUploads(){const labels=[];if(state.selectedFiles.length)labels.push(state.selectedFiles.length+' project file(s)');if(state.selectedFolders.length)labels.push(state.selectedFolders.length+' project folder(s)');if(state.pendingUploads.length)labels.push(state.pendingUploads.length+' computer file(s)');$('uploadList').textContent=labels.length?labels.join(' · '):'No files or folders staged.'}
function stageLocalFiles(input){const files=Array.from(input.files||[]);for(const file of files){const path=(file.webkitRelativePath||file.name).replace(/\\/g,'/');if(!state.pendingUploads.some(x=>x.path===path))state.pendingUploads.push({file,path})}input.value='';renderPendingUploads()}
function bytesToBase64(buffer){const bytes=new Uint8Array(buffer);let binary='';const chunk=0x8000;for(let i=0;i<bytes.length;i+=chunk)binary+=String.fromCharCode(...bytes.subarray(i,i+chunk));return btoa(binary)}
async function attachFiles(){try{await ensureChat();const selectedFiles=state.selectedFiles.slice();const uploads=[];let total=0;for(const item of state.pendingUploads){total+=item.file.size;if(total>20*1024*1024)throw new Error('Local file selection exceeds the 20 MB import limit.');uploads.push({path:item.path,content:bytesToBase64(await item.file.arrayBuffer())})}const result=await api('/api/context/import',{chat_id:state.chatId,selected_files:state.selectedFiles,selected_folders:state.selectedFolders,uploads});state.contextCount=(state.contextCount||0)+Number(result.files||0);state.selectedFiles=[];state.selectedFolders=[];state.pendingUploads=[];renderPendingUploads();$('filesModal').classList.remove('show');hint();toast(result.message||'Context added to isolated workspace')}catch(e){toast(e.message)}}
async function startDownload(){const model=$('downloadModel').value.trim();if(!model){toast('Enter an Ollama model ID');return}try{const d=await api('/api/download',{model});$('downloadCaption').textContent='Queued: '+model;$('localDownloadStatus').textContent='Download queued: '+model;$('downloadModel').value='';toast('Download started in background');pollDownload()}catch(e){toast(e.message)}}
async function pollDownload(){try{const d=await api('/api/download/status');const job=d.job;if(!job)return;const percent=job.total?job.completed/job.total*100:0;$('downloadFill').style.width=(job.state==='done'?100:percent)+'%';$('downloadCaption').textContent=job.state==='running'?`${job.model} · ${percent.toFixed(1)}% · ${job.human}`:`${job.model} · ${job.state}${job.error?' · '+job.error:''}`;if(job.state==='done'){await refreshBootstrap();$('localDownloadStatus').textContent=job.model+' installed successfully.';toast('Model download complete')}else if(job.state==='error'){$('localDownloadStatus').textContent=job.error||'Download failed';toast(job.error||'Download failed')}else{$('localDownloadStatus').textContent=job.model+' · '+percent.toFixed(1)+'% · '+job.human;setTimeout(pollDownload,1000)}}catch(e){console.error(e)}}
$('sidebarSettings').onclick=openSettings;$('sidebarDownload').onclick=async()=>{await openSettings();$('localModelsSection').scrollIntoView({behavior:'smooth',block:'start'})};$('openFiles').onclick=$('contextBtn').onclick=()=>{$('filesModal').classList.add('show');renderPendingUploads();browseFiles()};$('topModel').onchange=async()=>{try{await api('/api/settings',{model:$('topModel').value});$('settingsModel').value=$('topModel').value;await refreshBootstrap();toast('Active model changed')}catch(e){toast(e.message)}};$('settingsModel').onchange=async()=>{const model=$('settingsModel').value;$('topModel').value=model;try{await api('/api/settings',{model});await refreshBootstrap();toast('Active model changed')}catch(e){toast(e.message)}};$('saveSettings').onclick=saveSettings;$('reloadSettings').onclick=refreshBootstrap;$('addKey').onclick=async()=>{const value=$('keyValue').value.trim();if(!value){toast('Enter an API key');return}await api('/api/settings',{key_action:'add',provider:$('keyProvider').value,group:$('keyGroup').value,label:$('keyLabel').value,value});$('keyValue').value='';await refreshBootstrap();toast('API key saved locally')};$('addRemote').onclick=async()=>{const d=await api('/api/settings',{model_action:'add',provider:$('remoteProvider').value,model:$('remoteModelId').value,label:$('remoteLabel').value,key_group:$('remoteGroup').value,base_url:$('remoteBase').value});await refreshBootstrap();toast(d.message||'Model added')};$('refreshFiles').onclick=browseFiles;$('selectCurrentFolder').onclick=()=>{const path=$('fileFolder').value.trim();if(!state.selectedFolders.includes(path))state.selectedFolders.push(path);renderPendingUploads();toast('Current folder staged for this chat')};$('chooseLocalFiles').onclick=()=>$('localFiles').click();$('chooseLocalFolder').onclick=()=>$('localFolder').click();$('localFiles').onchange=()=>stageLocalFiles($('localFiles'));$('localFolder').onchange=()=>stageLocalFiles($('localFolder'));$('attachFiles').onclick=attachFiles;$('startDownload').onclick=startDownload;$('exportWorkspace').onclick=async()=>{if(!state.chatId){toast('Start a conversation first');return}try{const res=await fetch('/api/workspace/export?chat_id='+encodeURIComponent(state.chatId),{headers:{'X-Kurt-Token':TOKEN}});if(!res.ok){const d=await res.json().catch(()=>({}));throw new Error(d.error||res.statusText)}const blob=await res.blob(),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download='kurt-workspace-'+state.chatId.slice(0,8)+'.zip';link.click();setTimeout(()=>URL.revokeObjectURL(url),2000)}catch(e){toast(e.message)}};$('clearChat').onclick=async()=>{if(!state.chatId)return;if(!confirm('Clear this conversation?'))return;cancelRun();await api('/api/chats/'+encodeURIComponent(state.chatId),{clear:true});showWelcome();$('chatTitle').textContent='New conversation';await refreshChats()};document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>$(b.dataset.close).classList.remove('show'));document.querySelectorAll('.modal-backdrop').forEach(m=>m.onclick=e=>{if(e.target===m)m.classList.remove('show')});
$('menuBtn').onclick=()=>document.body.classList.toggle('nav-open');$('scrim').onclick=()=>document.body.classList.remove('nav-open');document.querySelector('.sidebar').addEventListener('click',e=>{if(e.target.closest('.chat-item,#newChat,#openFiles,#sidebarSettings,#sidebarDownload'))document.body.classList.remove('nav-open')});$('themeBtn').onclick=()=>{const t=document.documentElement.dataset.theme==='light'?'dark':'light';document.documentElement.dataset.theme=t;try{localStorage.setItem('kurt-theme',t)}catch(e){}};
(async()=>{try{await refreshBootstrap();await refreshChats();showWelcome();pollDownload()}catch(e){toast(e.message)}})();
</script></body></html>'''


THINKING_KINDS = ("thinking", "reasoning", "thought")
THINK_OPEN_RE = re.compile(r"<think(?:ing)?>", re.I)
THINK_CLOSE_RE = re.compile(r"</think(?:ing)?>", re.I)
THINK_TAG_RE = re.compile(r"<(/)?think(?:ing)?>", re.I)
THINK_RE = re.compile(r"<think(?:ing)?>([\s\S]*?)(?:</think(?:ing)?>|\Z)", re.I)
ACTION_TAIL_RE = re.compile(r"@@@\s*(?:WRITE|EDIT)\b[\s\S]*\Z")


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
        self.chats = sorted(self.chats, key=lambda item: item.get("updated", 0), reverse=True)[:MAX_CHATS]
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

    def chat_system_prompt(self, chat_id):
        root = self.chat_workspace(chat_id)
        project = self.workspace()
        custom = str(self.settings.get('system_prompt') or '').strip()
        base = custom or K.SYSTEM.replace('{cwd}', str(root))
        if '{cwd}' in base:
            base = base.replace('{cwd}', str(root))
        if 'WEB INTERFACE FILE OUTPUT RULES:' not in base:
            base += WEB_INSTRUCTIONS
        return (base + '\nIsolated temporary workspace directory: ' + str(root) +
                '\nOriginal project directory (read-only unless the user explicitly authorizes apply actions): ' + str(project) +
                '\nFiles in isolated workspace:\n' + self.tree(root))

    @staticmethod
    def tree(root, limit=220):
        rows = []
        for current, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if not KurtWebApp._skip_context_dir(d))
            for name in sorted(files):
                if self._skip_context_name(name) or name == '.DS_Store':
                    continue
                rows.append(str((Path(current) / name).relative_to(root)).replace('\\', '/'))
                if len(rows) >= limit:
                    rows.append('... (truncated)')
                    return '\n'.join(rows)
        return '\n'.join(rows) or '(empty workspace)'

    def artifacts_from_answer(self, answer, chat_id):
        artifacts = []
        root = self.chat_workspace(chat_id)
        project_root = self.workspace()
        for kind, arg, body in K.BLOCK_RE.findall(answer):
            if kind not in ('WRITE', 'EDIT'):
                continue
            filename = arg.strip()
            try:
                path = self.resolve_chat_path(root, filename)
                if path.is_dir():
                    continue
                existed_in_temp = path.is_file()
                content = body
                if kind == 'EDIT':
                    if not existed_in_temp:
                        continue
                    old, _crlf = K.read_file(path)
                    pairs = K.PAIR_RE.findall(body)
                    if not pairs:
                        continue
                    content = old
                    valid = True
                    for search, replacement in pairs:
                        start, end, how, fix = K.find_span(content, search)
                        if start is None:
                            valid = False
                            break
                        if how not in ('exact', 'trimmed'):
                            replacement = K.reindent(replacement, fix)
                        content = content[:start] + replacement + content[end:]
                    if not valid:
                        continue
                content = content if content.endswith('\n') else content + '\n'
                validation = validate_content(filename, content)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding='utf-8')
                project_path = (project_root / filename).resolve()
                project_path.relative_to(project_root)
                artifacts.append({
                    'path': filename.replace('\\', '/'), 'content': content,
                    'validation': validation, 'exists': project_path.exists(),
                    'temp_exists': existed_in_temp, 'source': kind,
                })
            except Exception:
                continue
        return artifacts

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
        chat = {"id": uuid.uuid4().hex, "title": "New conversation", "model": K.MODEL,
                "created": now, "updated": now, "messages": []}
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
                    chats = [{"id": c["id"], "title": c.get("title", "New conversation"), "updated": c.get("updated", 0)} for c in self.app.chats]
                self._json({"chats": chats})
            elif parsed.path.startswith("/api/chats/"):
                chat_id = unquote(parsed.path.rsplit("/", 1)[-1])
                chat = self.app.get_chat(chat_id)
                if not chat:
                    self._json({"error": "Conversation not found."}, 404)
                else:
                    self._json({"id": chat["id"], "title": chat.get("title"), "model": chat.get("model"), "messages": chat.get("messages", [])})
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
                if data.get("clear"):
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
        text = str(data.get("text") or "").strip()
        model = str(data.get("model") or K.MODEL)
        if not text:
            self._json({"error": "Message is empty."}, 400)
            return
        if len(text) > 100000:
            self._json({"error": "Message is too long."}, 400)
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
        self.app.store_chat_input(chat_id, text, len(chat.get("messages", [])) + 1)
        messages = [
            {"role": "system", "content": self.app.chat_system_prompt(chat_id)},
            {"role": "system", "content": self.app.context_contents(chat_id)},
        ]
        messages.extend({"role": item["role"], "content": item["content"]}
                        for item in previous if item.get("role") in ("user", "assistant"))
        messages.append({"role": "user", "content": text})
        user_display = text
        chat["messages"].append({"role": "user", "content": user_display})
        chat["updated"] = time.time()
        if chat.get("title") in (None, "New conversation"):
            chat["title"] = re.sub(r"\s+", " ", text)[:58] or "New conversation"
        chat["model"] = model
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

        answer = []
        field_thinking = []
        clock = {"start": None, "end": None, "field": False}
        tail = ""
        finished = False

        def mark_open():
            if clock["start"] is None:
                clock["start"] = time.time()

        def mark_close():
            if clock["start"] is not None and clock["end"] is None:
                clock["end"] = time.time()

        def finish(stopped=False):
            full = "".join(answer).strip()
            inline_thinking, reply = split_thinking(full)
            thinking = "\n\n".join(part for part in ("".join(field_thinking).strip(), inline_thinking) if part)
            if stopped:
                reply = ACTION_TAIL_RE.sub("", reply).strip()
                artifacts = []
            else:
                artifacts = self.app.artifacts_from_answer(reply, chat_id)
            visible = K.BLOCK_RE.sub("", reply).strip()
            secs = None
            if thinking and clock["start"] is not None:
                secs = round((clock["end"] or time.time()) - clock["start"], 1)
            if visible or thinking or not stopped:
                record = {"role": "assistant", "content": visible, "artifacts": artifacts}
                if thinking:
                    record["thinking"] = thinking
                    record["thinking_secs"] = secs
                if stopped:
                    record["stopped"] = True
                chat["messages"].append(record)
                chat["updated"] = time.time()
                chat["model"] = model
                self.app.save_chats()
            return visible, artifacts, thinking, secs

        stream = K.stream_chat(messages, model=model)
        try:
            for kind, chunk in stream:
                if not chunk:
                    continue
                if kind in THINKING_KINDS:
                    field_thinking.append(chunk)
                    clock["field"] = True
                    mark_open()
                    emit({"type": "thinking", "text": chunk})
                elif kind == "content":
                    if clock["field"]:
                        clock["field"] = False
                        mark_close()
                    answer.append(chunk)
                    combined = tail + chunk
                    offset = len(tail)
                    for match in THINK_TAG_RE.finditer(combined):
                        if match.end() <= offset:
                            continue
                        if match.group(1):
                            mark_close()
                        else:
                            mark_open()
                    tail = combined[-16:]
                    emit({"type": "token", "text": chunk})
            visible, artifacts, thinking, secs = finish()
            finished = True
            emit({"type": "done", "content": visible, "artifacts": artifacts,
                  "thinking": thinking, "thinking_secs": secs,
                  "chat_id": chat_id, "title": chat["title"], "model": model})
        except (BrokenPipeError, ConnectionResetError):
            if not finished:
                finish(stopped=True)
            return
        except Exception as exc:
            try:
                emit({"type": "error", "message": str(exc)})
            except OSError:
                pass
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
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
    server = None
    last_error = None
    for port in range(8765, 8785):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError as exc:
            last_error = exc
    if server is None:
        raise RuntimeError("Could not find a free localhost port between 8765 and 8784: {}".format(last_error))
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