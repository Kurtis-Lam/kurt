"""Kurt's loopback-only local web interface.

The server uses only the Python standard library and reuses Kurt's provider
registry, model configuration, and Ollama client.
"""

import hashlib
import hmac
import html
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
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import kurt as K
from kurt_sandbox import is_code_file, validate_content

CONFIG_DIR = K.CONFIG_DIR
WEB_SETTINGS_FILE = CONFIG_DIR / "web_settings.json"
CHATS_FILE = CONFIG_DIR / "chats.json"
MAX_BODY = 4 * 1024 * 1024
MAX_CHATS = 100
MAX_MESSAGES_PER_CHAT = 300

WEB_INSTRUCTIONS = r"""

WEB INTERFACE FILE OUTPUT RULES:
- You are working in the workspace directory specified below.
- When the user asks you to create or modify a file, explain the change and emit a
  complete file using this exact action format (do not wrap it in a Markdown fence):
  @@@ WRITE relative/path.ext
  complete file contents
  @@@ END
- For an existing file, request its contents from the user or use supplied context;
  then emit a complete replacement with WRITE. Never invent unseen file contents.
- Do not emit READ or RUN action blocks in web mode. If you need file contents,
  ask the user to attach them with the workspace context picker. The web app previews
  WRITE/EDIT files, validates source syntax, and offers Download or Write actions.
- Do not emit shell commands or claim to have inspected files that were not attached.
- Do not claim that a file was saved until the user selects Write and the app confirms it.
- Keep file paths relative to the workspace. Never target paths outside it.
"""

HTML_PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kurt</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='-5 -6 30 28'%3E%3Crect x='-5' y='-6' width='30' height='28' rx='6' fill='%230b120e'/%3E%3Cpath fill='%233dd66e' d='M0 0h6v4h-6zM14 0h6v2h-6zM11 2h6v2h-6zM0 4h1v12h-1zM2 4h2v12h-2zM5 4h1v12h-1zM8 4h6v2h-6zM6 6h5v4h-5zM1 8h1v8h-1zM4 8h1v8h-1zM8 10h6v2h-6zM11 12h6v2h-6zM14 14h6v2h-6z'/%3E%3C/svg%3E">
<script>try{document.documentElement.dataset.theme=localStorage.getItem('kurt-theme')||(matchMedia('(prefers-color-scheme: light)').matches?'light':'dark')}catch(e){document.documentElement.dataset.theme='dark'}</script>
<style>
:root{color-scheme:dark;--bg:#0e1511;--side:#0a100d;--panel:#141e19;--raise:#1c2922;--line:#22302a;--line2:#33463c;--text:#e6efe9;--muted:#8d9e94;--green:#3dd66e;--green-ink:#05200e;--green-soft:rgba(61,214,110,.12);--danger:#ff8d8d;--code:#0a110d;--shadow:0 18px 50px rgba(0,0,0,.4);--sans:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;--mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace}
:root[data-theme=light]{color-scheme:light;--bg:#f6f9f6;--side:#ebf1ec;--panel:#fff;--raise:#e4ece5;--line:#d8e2da;--line2:#bfcec3;--text:#16231b;--muted:#5b6c61;--green:#157f3c;--green-ink:#fff;--green-soft:rgba(21,127,60,.1);--danger:#b3261e;--code:#eff4ef;--shadow:0 16px 40px rgba(22,35,27,.14)}
*{box-sizing:border-box;scrollbar-color:var(--line2) transparent}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);font:14.5px/1.55 var(--sans);overflow:hidden}
button,input,textarea,select{font:inherit;color:inherit}
button{cursor:pointer;border:1px solid var(--line2);background:var(--raise);border-radius:7px;padding:7px 12px;transition:background .15s,border-color .15s,color .15s}
button:hover{border-color:var(--green)}
button.primary{background:var(--green);color:var(--green-ink);border-color:var(--green);font-weight:650}
button.primary:hover{filter:brightness(1.08)}
button.ghost{background:transparent;border-color:transparent;color:var(--muted)}
button.ghost:hover{color:var(--text);background:var(--raise)}
button.danger{color:var(--danger)}
button:disabled{opacity:.45;cursor:not-allowed}
:focus-visible{outline:2px solid var(--green);outline-offset:2px}
.logo{display:inline-grid;place-items:center;width:36px;height:36px;border-radius:9px;background:#0b120e;border:1px solid var(--line2);color:#3dd66e;flex:none}
.logo svg{width:22px;height:17.6px;display:block}
.logo.sm{width:28px;height:28px;border-radius:7px}.logo.sm svg{width:17px;height:13.6px}
.logo.xl{width:88px;height:88px;border-radius:20px}.logo.xl svg{width:54px;height:43.2px}
.app{height:100vh;height:100dvh;display:grid;grid-template-columns:260px minmax(0,1fr)}
.sidebar{background:var(--side);border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0;padding:16px 12px 12px;gap:12px}
.brand{display:flex;align-items:center;gap:11px;padding:2px 6px 8px}
.brand b{display:block;font-size:18px;line-height:1.2;letter-spacing:-.01em}
.brand small{display:block;color:var(--muted);font-size:12px}
.side-actions{display:grid;gap:8px}
.side-title{color:var(--muted);font-weight:600;font-size:12px;padding:6px 8px 0}
.chat-list{overflow:auto;min-height:80px;flex:1}
.chat-item{padding:8px 10px;border-radius:7px;cursor:pointer;color:var(--muted);margin:1px 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chat-item:hover{background:var(--raise);color:var(--text)}
.chat-item.active{background:var(--raise);color:var(--text);box-shadow:inset 2px 0 0 var(--green)}
.side-bottom{border-top:1px solid var(--line);padding-top:10px;display:grid;gap:4px}
.side-bottom button{text-align:left}
.local-note{font-size:12px;color:var(--muted);padding:6px 8px 0}
.status-dot{width:7px;height:7px;border-radius:50%;display:inline-block;background:var(--green);margin-right:7px}
.stage{display:grid;grid-template-rows:auto 3px minmax(0,1fr);min-width:0;min-height:0}
.topbar{display:flex;align-items:center;gap:8px;padding:10px 20px;background:var(--bg)}
.titles{flex:1;min-width:0}
.chat-title{font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chat-meta{color:var(--muted);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.workspace-label{font:12px var(--mono);color:var(--muted);max-width:240px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;direction:rtl}
.top-model{max-width:280px;min-width:150px;background:var(--panel);border:1px solid var(--line2);border-radius:7px;padding:7px 8px}
.menu-btn{display:none}
#themeBtn::before{content:"Light theme"}
:root[data-theme=light] #themeBtn::before{content:"Dark theme"}
.download-strip{position:relative;height:3px;background:var(--line)}
.download-fill{height:100%;width:0;background:var(--green);transition:width .25s}
.download-caption{position:absolute;right:20px;top:9px;z-index:6;font-size:12px;background:var(--panel);border:1px solid var(--line2);border-radius:999px;padding:3px 11px;box-shadow:var(--shadow)}
.download-caption:empty{display:none}
.main{min-width:0;min-height:0;display:flex;flex-direction:column}
.messages{flex:1;overflow:auto;padding:28px max(20px,calc((100% - 820px)/2)) 12px;scroll-behavior:smooth}
.welcome{max-width:640px;margin:8vh auto 0}
.welcome h1{font-size:30px;line-height:1.15;letter-spacing:-.02em;margin:24px 0 8px}
.welcome p{color:var(--muted);font-size:15px;max-width:52ch;margin:0}
.suggestions{margin-top:28px;border-top:1px solid var(--line)}
.suggestion{display:grid;grid-template-columns:22px 1fr;width:100%;text-align:left;background:transparent;border:0;border-bottom:1px solid var(--line);border-radius:0;padding:13px 6px;font-weight:600}
.suggestion::before{content:">";font:700 14px/1.5 var(--mono);color:var(--green)}
.suggestion span{grid-column:2;display:block;color:var(--muted);font-size:13px;font-weight:400}
.suggestion:hover{background:var(--raise)}
.message{display:grid;grid-template-columns:30px minmax(0,1fr);gap:12px;max-width:820px;margin:0 auto 26px}
.avatar{display:grid;place-items:center;width:30px;height:30px}
.you{font:600 20px/1 var(--mono);color:var(--muted)}
.bubble{min-width:0}
.message.user .bubble{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px 14px}
.role{font-size:12px;color:var(--muted);font-weight:600;margin-bottom:2px}
.content{overflow-wrap:anywhere;font-size:15px;line-height:1.7}
.content>p,.content>ul,.content>ol,.content>blockquote{max-width:72ch}
.content p{margin:0 0 12px}
.content h1,.content h2,.content h3{line-height:1.25;margin:22px 0 10px;letter-spacing:-.01em}
.content h1{font-size:24px}.content h2{font-size:20px}.content h3{font-size:17px}
.content ul,.content ol{padding-left:23px}.content li{margin:4px 0}
.content blockquote{border-left:3px solid var(--green);padding:2px 14px;margin:12px 0;color:var(--muted)}
.content a{color:var(--green)}
.content code:not(pre code){font:.88em var(--mono);background:var(--raise);border:1px solid var(--line);border-radius:5px;padding:1px 5px}
.content:empty::after{content:"\258D";color:var(--green);animation:blink 1s steps(2,start) infinite}
.message.live .logo{animation:blink 1.1s steps(2,start) infinite}
@keyframes blink{50%{opacity:.35}}
.codebox{background:var(--code);border:1px solid var(--line);border-radius:8px;overflow:hidden;margin:12px 0}
.codehead{display:flex;justify-content:space-between;align-items:center;padding:5px 6px 5px 12px;border-bottom:1px solid var(--line);color:var(--muted);font:12px var(--mono)}
.codebox pre{margin:0;padding:14px;overflow:auto;max-height:480px;white-space:pre;font:13px/1.6 var(--mono)}
.copy-mini{font-size:12px;padding:3px 8px;background:transparent;border-color:transparent;color:var(--muted)}
.copy-mini:hover{color:var(--text)}
.artifact{margin:14px 0;border:1px solid var(--line2);border-left:3px solid var(--green);background:var(--panel);border-radius:8px;overflow:hidden}
.artifact.bad{border-left-color:var(--danger)}
.artifact-top{display:flex;align-items:center;gap:10px;padding:11px 14px;cursor:pointer}
.file-icon{width:30px;height:30px;border-radius:7px;background:var(--raise);display:grid;place-items:center;color:var(--green);font:600 13px var(--mono)}
.file-name{font:600 13px var(--mono);flex:1;min-width:0;overflow-wrap:anywhere}
.file-check{font-size:12px;color:var(--green)}
.artifact.bad .file-check{color:var(--danger)}
.artifact-top>span:last-child{color:var(--muted);transition:transform .15s}
.artifact.open .artifact-top>span:last-child{transform:rotate(180deg)}
.artifact-body{display:none;border-top:1px solid var(--line)}
.artifact.open .artifact-body{display:block}
.artifact-body .codebox{margin:0;border:0;border-radius:0}
.artifact-actions{display:flex;gap:7px;padding:10px 12px;border-top:1px solid var(--line);flex-wrap:wrap}
.artifact-actions button{font-size:13px}
.composer-wrap{padding:8px 20px 18px;flex-shrink:0}
.composer{max-width:820px;margin:0 auto;border:1px solid var(--line2);border-radius:12px;background:var(--panel);padding:10px 12px 10px 14px;display:grid;grid-template-columns:auto 1fr;column-gap:10px}
.composer:focus-within{border-color:var(--green);box-shadow:0 0 0 3px var(--green-soft)}
.glyph{font:700 15px/1.5 var(--mono);color:var(--green);padding-top:6px}
.composer textarea{display:block;width:100%;resize:none;min-height:52px;max-height:190px;background:transparent;border:0;outline:none;padding:5px 0;font:14px/1.6 var(--mono)}
.compose-bottom{grid-column:1/-1;display:flex;align-items:center;gap:8px}
.compose-hint{color:var(--muted);font-size:12px;flex:1}
.modal-backdrop{position:fixed;inset:0;background:rgba(4,10,7,.6);z-index:30;display:none;align-items:center;justify-content:center;padding:20px}
.modal-backdrop.show{display:flex}
.modal{width:min(850px,100%);max-height:88vh;overflow:auto;background:var(--panel);border:1px solid var(--line2);border-radius:12px;box-shadow:var(--shadow)}
.modal-head{position:sticky;top:0;z-index:2;background:var(--panel);padding:14px 20px;display:flex;align-items:center;border-bottom:1px solid var(--line)}
.modal-head h2{font-size:17px;margin:0;flex:1}
.modal-body{padding:18px 20px 24px}
.settings-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.field{display:grid;gap:5px;margin-bottom:13px}
.field label{font-size:13px;color:var(--muted)}
.field input,.field select,.field textarea{background:var(--bg);border:1px solid var(--line2);border-radius:7px;padding:8px 10px;outline:none;width:100%}
.field textarea{min-height:145px;resize:vertical;font:12.5px/1.55 var(--mono)}
.field input:focus,.field select:focus,.field textarea:focus{border-color:var(--green)}
.section{border-top:1px solid var(--line);margin-top:18px;padding-top:16px}
.section h3{font-size:14px;margin:0 0 12px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.row>*{flex:1;min-width:100px}
.key-row,.model-row{display:flex;gap:10px;align-items:center;padding:9px 10px;border:1px solid var(--line);border-radius:8px;margin:6px 0}
.key-row .grow,.model-row .grow{flex:1;min-width:0}
.key-row small,.model-row small{display:block;color:var(--muted)}
.file-picker{border:1px solid var(--line);border-radius:8px;max-height:260px;overflow-y:auto}
.file-row{padding:7px 10px;border-bottom:1px solid var(--line);display:flex;gap:9px;align-items:center}
.file-row:last-child{border:0}
.file-row label{flex:1;overflow-wrap:anywhere}
.toast{position:fixed;bottom:24px;left:50%;transform:translate(-50%,16px);background:var(--panel);border:1px solid var(--line2);padding:9px 15px;border-radius:8px;box-shadow:var(--shadow);opacity:0;transition:.2s;pointer-events:none;z-index:40}
.toast.show{opacity:1;transform:translate(-50%,0)}
.empty{color:var(--muted);font-size:13px;padding:12px}
.scrim{display:none}
@media(max-width:900px){.workspace-label{display:none}}
@media(max-width:700px){
.app{grid-template-columns:1fr}
.sidebar{position:fixed;inset:0 auto 0 0;width:min(300px,86vw);z-index:20;transform:translateX(-102%);transition:transform .2s;box-shadow:var(--shadow)}
body.nav-open .sidebar{transform:none}
body.nav-open .scrim{display:block;position:fixed;inset:0;background:rgba(4,10,7,.55);z-index:15}
.menu-btn{display:inline-block}
.topbar{padding:8px 12px}
.top-model{min-width:0;max-width:44vw;font-size:12px}
#contextBtn,#clearChat,#themeBtn{display:none}
.messages{padding:18px 14px 8px}
.composer-wrap{padding:6px 10px 12px}
.settings-grid{grid-template-columns:1fr}
}
@media(prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important;scroll-behavior:auto!important}}
</style></head>
<body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><symbol id="kurt-k" viewBox="0 0 20 16"><path fill="currentColor" shape-rendering="crispEdges" d="M0 0h6v4h-6zM14 0h6v2h-6zM11 2h6v2h-6zM0 4h1v12h-1zM2 4h2v12h-2zM5 4h1v12h-1zM8 4h6v2h-6zM6 6h5v4h-5zM1 8h1v8h-1zM4 8h1v8h-1zM8 10h6v2h-6zM11 12h6v2h-6zM14 14h6v2h-6z"/></symbol></svg>
<div class="scrim" id="scrim"></div>
<div class="app">
<aside class="sidebar"><div class="brand"><span class="logo"><svg viewBox="0 0 20 16" aria-hidden="true"><use href="#kurt-k"/></svg></span><div><b>Kurt</b><small>Local coding companion</small></div></div><div class="side-actions"><button class="primary" id="newChat">New chat</button><button id="openFiles">Workspace files</button></div><div class="side-title">Recent chats</div><div id="chatList" class="chat-list"></div><div class="side-bottom"><button class="ghost" id="sidebarDownload">Download a model</button><button class="ghost" id="sidebarSettings">Settings</button><div class="local-note"><span class="status-dot"></span>Running on localhost only</div></div></aside>
<div class="stage">
<header class="topbar"><button class="ghost menu-btn" id="menuBtn" aria-label="Open menu">☰</button><div class="titles"><div id="chatTitle" class="chat-title">New conversation</div><div id="chatMeta" class="chat-meta">Select a model and start chatting</div></div><span class="workspace-label" id="workspaceLabel" title="Workspace directory"></span><select id="topModel" class="top-model" title="Active model" aria-label="Active model"></select><button class="ghost" id="contextBtn">Add context</button><button class="ghost" id="clearChat">Clear</button><button class="ghost" id="themeBtn" aria-label="Switch colour theme"></button></header>
<div class="download-strip" title="Background model download"><div id="downloadFill" class="download-fill"></div><div id="downloadCaption" class="download-caption"></div></div>
<main class="main"><div id="messages" class="messages"></div>
<div class="composer-wrap"><div class="composer"><span class="glyph" aria-hidden="true">&gt;</span><textarea id="prompt" rows="2" placeholder="Message Kurt. Enter sends, Shift+Enter adds a line." aria-label="Message Kurt"></textarea><div class="compose-bottom"><span class="compose-hint" id="composeHint">Files are only written when you choose “Write to workspace”.</span><button id="stopBtn" class="ghost" style="display:none">Stop</button><button class="primary" id="sendBtn">Send</button></div></div></div></main>
</div></div>
<div class="modal-backdrop" id="settingsModal"><div class="modal"><div class="modal-head"><h2>Kurt settings</h2><button class="ghost" data-close="settingsModal">Close</button></div><div class="modal-body">
<div class="settings-grid"><div class="field"><label>Active model</label><select id="settingsModel"></select></div><div class="field"><label>Workspace directory</label><input id="projectRoot" placeholder="/Users/you/project"></div></div>
<div class="field"><label>System prompt</label><textarea id="systemPrompt" style="min-height:190px"></textarea></div><div class="row"><button class="primary" id="saveSettings">Save settings</button><button id="reloadSettings">Reload from disk</button></div>
<div class="section"><h3>API keys · key pools</h3><div id="keysList"></div><div class="settings-grid"><div class="field"><label>Provider</label><select id="keyProvider"></select></div><div class="field"><label>Key pool</label><input id="keyGroup" value="default"></div><div class="field"><label>Account label</label><input id="keyLabel" placeholder="Personal account"></div><div class="field"><label>API key</label><input id="keyValue" type="password" placeholder="Paste key (stored locally)"></div></div><button id="addKey" class="primary">Add API key</button></div>
<div class="section"><h3>Remote model configuration</h3><div class="settings-grid"><div class="field"><label>Provider</label><select id="remoteProvider"></select></div><div class="field"><label>API model ID</label><input id="remoteModelId" placeholder="e.g. openrouter/free"></div><div class="field"><label>Display name</label><input id="remoteLabel" placeholder="Model name"></div><div class="field"><label>Key pool</label><input id="remoteGroup" value="default"></div><div class="field" style="grid-column:1/-1"><label>API base URL (optional for standard providers)</label><input id="remoteBase" placeholder="https://api.example.com/v1"></div></div><button id="addRemote" class="primary">Add remote model</button><div id="remoteList" style="margin-top:10px"></div></div>
</div></div></div>
<div class="modal-backdrop" id="filesModal"><div class="modal"><div class="modal-head"><h2>Workspace context</h2><button class="ghost" data-close="filesModal">Close</button></div><div class="modal-body"><p style="color:var(--muted);margin-top:0">Choose files to include in your next message. File content stays on this machine unless it is sent to your selected AI provider.</p><div class="field"><label>Folder</label><div class="row"><input id="fileFolder" value=""><button id="refreshFiles">Browse</button></div></div><div class="file-picker" id="filePicker"></div><div class="row" style="margin-top:12px"><button class="primary" id="attachFiles">Attach selected files</button></div></div></div></div>
<div class="modal-backdrop" id="downloadModal"><div class="modal" style="max-width:520px"><div class="modal-head"><h2>Download a local model</h2><button class="ghost" data-close="downloadModal">Close</button></div><div class="modal-body"><p style="color:var(--muted)">Downloads run in the background. You can keep chatting while the progress bar at the top updates.</p><div class="field"><label>Ollama model name</label><input id="downloadModel" placeholder="qwen2.5-coder:7b"></div><button class="primary" id="startDownload">Start download</button><p style="font-size:11px;color:var(--muted)">This downloads Ollama-compatible model IDs. Hosted API models do not need downloading.</p></div></div></div>
<div class="toast" id="toast"></div>
<script>
const TOKEN='__TOKEN__';let state={models:[],providers:[],keys:[],remoteModels:[],chats:[],chatId:null,projectRoot:'',selectedFiles:[],busy:false,abort:null,settings:{}};
const $=id=>document.getElementById(id);const api=async(path,body)=>{const res=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json','X-Kurt-Token':TOKEN},body:body?JSON.stringify(body):undefined});const data=await res.json().catch(()=>({error:'Invalid server response'}));if(!res.ok)throw new Error(data.error||res.statusText);return data};
function toast(msg){const el=$('toast');el.textContent=msg;el.classList.add('show');setTimeout(()=>el.classList.remove('show'),2600)}
function modelLabel(m){return `${m.label||m.name||m.model} · ${m.provider}${m.current?' · active':''}`}
function fillModels(){const html=state.models.map(m=>`<option value="${esc(m.ref)}">${esc(modelLabel(m))}</option>`).join('');$('topModel').innerHTML=html||'<option>No models configured</option>';$('settingsModel').innerHTML=html||'<option>No models configured</option>';if(state.settings.model){$('topModel').value=state.settings.model;$('settingsModel').value=state.settings.model} }
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function renderMarkdown(src){let s=esc(src);const blocks=[];s=s.replace(/```([\w.+-]*)\n([\s\S]*?)```/g,(_,lang,code)=>{const id='code'+blocks.length;blocks.push(`<div class="codebox"><div class="codehead"><span>${esc(lang||'CODE')}</span><button class="copy-mini" data-copy-code="${id}">Copy code</button></div><pre><code id="${id}">${code.replace(/\n$/,'')}</code></pre></div>`);return `\n@@CODE${blocks.length-1}@@\n`});s=s.replace(/^### (.*)$/gm,'<h3>$1</h3>').replace(/^## (.*)$/gm,'<h2>$1</h2>').replace(/^# (.*)$/gm,'<h1>$1</h1>').replace(/^> (.*)$/gm,'<blockquote>$1</blockquote>').replace(/^[-*] (.*)$/gm,'<li>$1</li>').replace(/^(\d+)\. (.*)$/gm,'<li>$2</li>').replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>').replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*([^*]+)\*/g,'<em>$1</em>');s=s.replace(/(?:<li>.*?<\/li>\n?)+/gs,m=>'<ul>'+m+'</ul>');s=s.split(/\n{2,}/).map(p=>{p=p.trim();if(!p)return '';if(/^<(h[1-3]|ul|ol|blockquote|div|pre)/.test(p)||p.startsWith('@@CODE'))return p;return '<p>'+p.replace(/\n/g,'<br>')+'</p>'}).join('');s=s.replace(/@@CODE(\d+)@@/g,(_,i)=>blocks[Number(i)]);return s}
function bindCopy(root){root.querySelectorAll('[data-copy-code]').forEach(b=>b.onclick=()=>{const el=$(b.dataset.copyCode);navigator.clipboard.writeText(el?.textContent||'').then(()=>toast('Code copied'))})}
function renderMessage(role,content,artifacts=[]){const wrap=document.createElement('div');wrap.className='message '+role;wrap.innerHTML=`<div class="avatar">${role==='assistant'?'<span class="logo sm"><svg viewBox="0 0 20 16" aria-hidden="true"><use href="#kurt-k"/></svg></span>':'<span class="you">›</span>'}</div><div class="bubble"><div class="role">${role==='assistant'?'Kurt':'You'}</div><div class="content">${renderMarkdown(content)}</div><div class="artifacts"></div></div>`;bindCopy(wrap);const target=wrap.querySelector('.artifacts');artifacts.forEach(a=>target.appendChild(artifactCard(a)));$('messages').appendChild(wrap);$('messages').scrollTop=$('messages').scrollHeight;return wrap}
function artifactCard(a){const card=document.createElement('div');card.className='artifact'+(a.validation?.ok?'':' bad');card.innerHTML=`<div class="artifact-top"><div class="file-icon">⌘</div><div class="file-name">${esc(a.path)}</div><div class="file-check">${esc(a.validation?.message||'File preview')}</div><span>⌄</span></div><div class="artifact-body"><div class="codebox"><div class="codehead"><span>${esc((a.path.split('.').pop()||'FILE').toUpperCase())}</span><button class="copy-mini copy-artifact">Copy code</button></div><pre><code></code></pre></div><div class="artifact-actions"><button class="copy-artifact">Copy</button><button class="download-artifact">Download file</button><button class="primary write-artifact">Write to workspace</button></div></div>`;card.querySelector('code').textContent=a.content;card.querySelector('.artifact-top').onclick=()=>card.classList.toggle('open');card.querySelectorAll('.copy-artifact').forEach(b=>b.onclick=()=>navigator.clipboard.writeText(a.content).then(()=>toast('File content copied')));card.querySelector('.download-artifact').onclick=()=>{const blob=new Blob([a.content],{type:'application/octet-stream'}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=a.path.split('/').pop();link.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};card.querySelector('.write-artifact').onclick=async()=>{if(!a.validation?.ok){toast('Fix validation errors before writing');return}const overwrite=confirm(`Write ${a.path} into ${state.projectRoot}?${a.exists?'\nThis will overwrite the existing file.':''}`);if(!overwrite)return;try{const r=await api('/api/file/write',{path:a.path,content:a.content,overwrite});toast(r.message||'File written');}catch(e){toast(e.message)}};return card}
const SUGGESTIONS=[['Understand my project','Inspect the current project structure and explain its architecture.','Review files and explain how everything fits together'],['Build a feature','Help me plan and implement a new feature. Ask for any missing requirements first.','Plan and implement a change in my workspace'],['Generate a file','Write a small utility script and return it as a file card.','Get a downloadable file or write it to the workspace'],['Debug code','Help me debug this issue. Start by asking for the relevant error and files.','Trace a problem and validate the proposed fix']];
function chatWelcome(){$('messages').innerHTML=`<div class="welcome"><span class="logo xl"><svg viewBox="0 0 20 16" aria-hidden="true"><use href="#kurt-k"/></svg></span><h1>What are we building?</h1><p>Chat with a local or hosted model, attach files from your workspace, and review every file before Kurt writes it.</p><div class="suggestions">${SUGGESTIONS.map(s=>`<button class="suggestion" data-prompt="${esc(s[1])}">${esc(s[0])}<span>${esc(s[2])}</span></button>`).join('')}</div></div>`;document.querySelectorAll('[data-prompt]').forEach(b=>b.onclick=()=>{$('prompt').value=b.dataset.prompt;send()})}
function renderChats(){const el=$('chatList');el.innerHTML='';state.chats.forEach(c=>{const d=document.createElement('div');d.className='chat-item'+(c.id===state.chatId?' active':'');d.textContent=c.title||'New conversation';d.onclick=()=>loadChat(c.id);el.appendChild(d)})}
async function refreshBootstrap(){const d=await api('/api/bootstrap');state.models=d.models||[];state.providers=d.providers||[];state.keys=d.keys||[];state.remoteModels=d.remote_models||[];state.settings=d.settings||{};state.projectRoot=d.project_root||'';$('workspaceLabel').textContent=state.projectRoot;$('projectRoot').value=state.projectRoot;$('systemPrompt').value=state.settings.system_prompt||'';fillModels();renderKeys();renderRemoteModels();fillProviders()}
function fillProviders(){const opts=state.providers.map(p=>`<option value="${esc(p.id)}">${esc(p.label)}</option>`).join('');$('keyProvider').innerHTML=opts;$('remoteProvider').innerHTML=opts}
function renderKeys(){const el=$('keysList');el.innerHTML='';if(!state.keys.length){el.innerHTML='<div class="empty">No API keys configured yet.</div>';return}state.keys.forEach(k=>{const d=document.createElement('div');d.className='key-row';d.innerHTML=`<div class="grow"><b>${esc(k.label)}</b><small>${esc(k.provider)} · pool ${esc(k.group)} · ${esc(k.masked)}</small></div><button class="edit-key">Edit</button><button class="danger">Remove</button>`;d.querySelector('.edit-key').onclick=async()=>{const label=prompt('Account label:',k.label);if(label===null)return;const group=prompt('Key pool:',k.group);if(group===null)return;const value=prompt('New API key (leave blank to keep current):','');if(value===null)return;try{await api('/api/settings',{key_action:'edit',id:k.id,label,group,value});await refreshBootstrap();toast('API key updated')}catch(e){toast(e.message)}};d.querySelector('.danger').onclick=async()=>{if(!confirm('Remove this API key?'))return;await api('/api/settings',{key_action:'remove',id:k.id});await refreshBootstrap()};el.appendChild(d)})}
function renderRemoteModels(){const el=$('remoteList');el.innerHTML='';state.remoteModels.forEach(m=>{const d=document.createElement('div');d.className='model-row';d.innerHTML=`<div class="grow"><b>${esc(m.label||m.model)}</b><small>${esc(m.provider)} · ${esc(m.model)} · pool ${esc(m.key_group||'default')}</small></div><button class="edit-model">Edit</button><button class="danger">Remove</button>`;d.querySelector('.edit-model').onclick=async()=>{const model=prompt('API model ID:',m.model);if(model===null)return;const label=prompt('Display name:',m.label||m.model);if(label===null)return;const group=prompt('Key pool:',m.key_group||'default');if(group===null)return;const base=prompt('API base URL:',m.base_url||'');if(base===null)return;try{await api('/api/settings',{model_action:'edit',id:m.id,model,label,key_group:group,base_url:base});await refreshBootstrap();toast('Model settings updated')}catch(e){toast(e.message)}};d.querySelector('.danger').onclick=async()=>{if(!confirm('Remove this remote model?'))return;await api('/api/settings',{model_action:'remove',id:m.id});await refreshBootstrap()};el.appendChild(d)})}
async function refreshChats(){const d=await api('/api/chats');state.chats=d.chats||[];renderChats()}
async function newChat(){const d=await api('/api/chats',{create:true});state.chatId=d.id;chatWelcome();$('chatTitle').textContent='New conversation';$('chatMeta').textContent='Ready';await refreshChats()}
async function loadChat(id){const d=await api('/api/chats/'+encodeURIComponent(id));state.chatId=id;$('messages').innerHTML='';if((d.messages||[]).length)d.messages.forEach(m=>renderMessage(m.role,m.content,m.artifacts||[]));else chatWelcome();$('chatTitle').textContent=d.title||'Conversation';$('chatMeta').textContent=d.model||'Saved conversation';renderChats()}
async function send(){const text=$('prompt').value.trim();if(!text||state.busy)return;const selected=state.selectedFiles.slice();state.selectedFiles=[];$('prompt').value='';if(!state.chatId)await newChat();let full=text;if(selected.length){full+='\n\nSelected workspace files:\n';for(const file of selected){try{const d=await api('/api/file/read',{path:file});full+=`\n--- ${file} ---\n${d.content}\n`}catch(e){full+=`\n[Could not read ${file}: ${e.message}]\n`}}}renderMessage('user',text);state.busy=true;$('sendBtn').disabled=true;$('stopBtn').style.display='inline-block';$('chatMeta').textContent='Generating…';const assistantNode=renderMessage('assistant','');assistantNode.classList.add('live');const contentEl=assistantNode.querySelector('.content');let collected='';const controller=new AbortController();state.abort=controller;try{const res=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json','X-Kurt-Token':TOKEN},body:JSON.stringify({chat_id:state.chatId,text:full,model:$('topModel').value}),signal:controller.signal});if(!res.ok){const x=await res.json();throw new Error(x.error||res.statusText)}const reader=res.body.getReader(),decoder=new TextDecoder();let buffer='';while(true){const {value,done}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});let sep;while((sep=buffer.indexOf('\n\n'))>=0){const frame=buffer.slice(0,sep);buffer=buffer.slice(sep+2);const dataLine=frame.split('\n').find(x=>x.startsWith('data:'));if(!dataLine)continue;const event=JSON.parse(dataLine.slice(5).trim());if(event.type==='token'){collected+=event.text;contentEl.innerHTML=renderMarkdown(collected);bindCopy(assistantNode);$('messages').scrollTop=$('messages').scrollHeight}else if(event.type==='done'){contentEl.innerHTML=renderMarkdown(event.content);bindCopy(assistantNode);const slot=assistantNode.querySelector('.artifacts');(event.artifacts||[]).forEach(a=>slot.appendChild(artifactCard(a)));$('chatTitle').textContent=event.title||'Conversation';$('chatMeta').textContent=event.model||'Completed';await refreshChats()}else if(event.type==='error')throw new Error(event.message)}}}catch(e){if(e.name!=='AbortError'){contentEl.textContent='Error: '+e.message;toast(e.message)}}finally{assistantNode.classList.remove('live');state.busy=false;state.abort=null;$('sendBtn').disabled=false;$('stopBtn').style.display='none';if($('chatMeta').textContent==='Generating…')$('chatMeta').textContent='Ready'}}
async function openSettings(){await refreshBootstrap();$('settingsModal').classList.add('show')}
async function saveSettings(){const payload={project_root:$('projectRoot').value,system_prompt:$('systemPrompt').value};if($('settingsModel').value)payload.model=$('settingsModel').value;const d=await api('/api/settings',payload);state.settings=d.settings;state.projectRoot=d.project_root;$('workspaceLabel').textContent=state.projectRoot;if(d.model)$('topModel').value=d.model;toast('Settings saved')}
async function browseFiles(){const path=$('fileFolder').value.trim();const d=await api('/api/files?path='+encodeURIComponent(path));$('fileFolder').value=d.path;const el=$('filePicker');el.innerHTML='';if(d.parent!==null){const b=document.createElement('div');b.className='file-row';b.innerHTML='<span>↰</span><label>.. (parent folder)</label>';b.onclick=()=>{$('fileFolder').value=d.parent;browseFiles()};el.appendChild(b)}d.items.forEach(item=>{const row=document.createElement('div');row.className='file-row';if(item.directory){row.innerHTML=`<span>📁</span><label>${esc(item.name)}/</label><button>Open</button>`;row.querySelector('button').onclick=()=>{$('fileFolder').value=item.path;browseFiles()}}else{row.innerHTML=`<input type="checkbox" value="${esc(item.path)}"><label>${esc(item.name)}</label><small>${esc(item.size)}</small>`}el.appendChild(row)})}
async function startDownload(){const model=$('downloadModel').value.trim();if(!model)return;const d=await api('/api/download',{model});$('downloadCaption').textContent='Queued: '+model;toast('Download started in background');$('downloadModal').classList.remove('show');pollDownload()}
async function pollDownload(){try{const d=await api('/api/download/status');const job=d.job;if(!job)return;const percent=job.total?job.completed/job.total*100:0;$('downloadFill').style.width=(job.state==='done'?100:percent)+'%';$('downloadCaption').textContent=job.state==='running'?`${job.model} · ${percent.toFixed(1)}% · ${job.human}`:`${job.model} · ${job.state}${job.error?' · '+job.error:''}`;if(job.state==='done'){await refreshBootstrap();toast('Model download complete')}else if(job.state==='error')toast(job.error||'Download failed');else setTimeout(pollDownload,1000)}catch(e){console.error(e)}}
$('sendBtn').onclick=send;$('stopBtn').onclick=()=>state.abort?.abort();$('prompt').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}});$('prompt').addEventListener('input',e=>{e.target.style.height='auto';e.target.style.height=Math.min(e.target.scrollHeight,190)+'px'});$('newChat').onclick=newChat;$('sidebarSettings').onclick=openSettings;$('sidebarDownload').onclick=()=>$('downloadModal').classList.add('show');$('openFiles').onclick=$('contextBtn').onclick=()=>{$('filesModal').classList.add('show');browseFiles()};$('topModel').onchange=async()=>{await api('/api/settings',{model:$('topModel').value});$('settingsModel').value=$('topModel').value;toast('Active model changed')};$('settingsModel').onchange=()=>$('topModel').value=$('settingsModel').value;$('saveSettings').onclick=saveSettings;$('reloadSettings').onclick=refreshBootstrap;$('addKey').onclick=async()=>{const value=$('keyValue').value.trim();if(!value){toast('Enter an API key');return}await api('/api/settings',{key_action:'add',provider:$('keyProvider').value,group:$('keyGroup').value,label:$('keyLabel').value,value});$('keyValue').value='';await refreshBootstrap();toast('API key saved locally')};$('addRemote').onclick=async()=>{const d=await api('/api/settings',{model_action:'add',provider:$('remoteProvider').value,model:$('remoteModelId').value,label:$('remoteLabel').value,key_group:$('remoteGroup').value,base_url:$('remoteBase').value});await refreshBootstrap();toast(d.message||'Model added')};$('refreshFiles').onclick=browseFiles;$('attachFiles').onclick=()=>{state.selectedFiles=Array.from(document.querySelectorAll('#filePicker input:checked')).map(x=>x.value);$('filesModal').classList.remove('show');$('composeHint').textContent=state.selectedFiles.length?state.selectedFiles.length+' file(s) attached to next message':'Files are only written when you choose “Write to workspace”.';toast(state.selectedFiles.length+' file(s) selected')};$('startDownload').onclick=startDownload;$('clearChat').onclick=async()=>{if(!state.chatId)return;if(!confirm('Clear this conversation?'))return;await api('/api/chats/'+encodeURIComponent(state.chatId),{clear:true});chatWelcome();$('chatTitle').textContent='New conversation';await refreshChats()};document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>$ (b.dataset.close).classList.remove('show'));document.querySelectorAll('.modal-backdrop').forEach(m=>m.onclick=e=>{if(e.target===m)m.classList.remove('show')});
$('menuBtn').onclick=()=>document.body.classList.toggle('nav-open');$('scrim').onclick=()=>document.body.classList.remove('nav-open');document.querySelector('.sidebar').addEventListener('click',e=>{if(e.target.closest('.chat-item,#newChat,#openFiles,#sidebarSettings,#sidebarDownload'))document.body.classList.remove('nav-open')});$('themeBtn').onclick=()=>{const t=document.documentElement.dataset.theme==='light'?'dark':'light';document.documentElement.dataset.theme=t;try{localStorage.setItem('kurt-theme',t)}catch(e){}};
(async()=>{try{await refreshBootstrap();await refreshChats();if(state.chats.length)await loadChat(state.chats[0].id);else await newChat();pollDownload()}catch(e){toast(e.message)}})();
</script></body></html>'''


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

    def chat_system_prompt(self):
        root = self.workspace()
        custom = str(self.settings.get("system_prompt") or "").strip()
        base = custom or K.SYSTEM.replace("{cwd}", str(root))
        if "{cwd}" in base:
            base = base.replace("{cwd}", str(root))
        if "WEB INTERFACE FILE OUTPUT RULES:" not in base:
            base += WEB_INSTRUCTIONS
        return base + "\nWorkspace directory: " + str(root) + "\nWorkspace files:\n" + self.tree(root)

    @staticmethod
    def tree(root, limit=180):
        ignored = set(K.IGNORE_DIRS) | {".git", ".venv", "venv", "__pycache__"}
        rows = []
        for current, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in ignored and not d.startswith("."))
            for name in sorted(files):
                if name.startswith(".") and name not in (".env.example", ".gitignore"):
                    continue
                rows.append(str((Path(current) / name).relative_to(root)).replace("\\", "/"))
                if len(rows) >= limit:
                    rows.append("... (truncated)")
                    return "\n".join(rows)
        return "\n".join(rows) or "(empty workspace)"

    def artifacts_from_answer(self, answer):
        artifacts = []
        for kind, arg, body in K.BLOCK_RE.findall(answer):
            if kind not in ("WRITE", "EDIT"):
                continue
            filename = arg.strip()
            try:
                path = self.resolve_workspace_path(filename)
                if path.is_dir():
                    continue
                content = body
                if kind == "EDIT":
                    if not path.is_file():
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
                        if how not in ("exact", "trimmed"):
                            replacement = K.reindent(replacement, fix)
                        content = content[:start] + replacement + content[end:]
                    if not valid:
                        continue
                content = content if content.endswith("\n") else content + "\n"
                validation = validate_content(filename, content)
                artifacts.append({
                    "path": filename.replace("\\", "/"), "content": content,
                    "validation": validation, "exists": path.exists(), "source": kind,
                })
            except Exception:
                continue
        return artifacts

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
            raise ValueError("Request is too large (maximum 4 MB).")
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
                    "settings": {"model": K.MODEL, "system_prompt": self.app.editable_system_prompt()},
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
                    self.app.save_chats()
                    self._json({"ok": True})
                else:
                    self._json({"error": "Unsupported operation."}, 400)
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
                    "settings": {"model": K.MODEL, "system_prompt": self.app.editable_system_prompt()}})

    def _write_file(self, data):
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
        messages = [{"role": "system", "content": self.app.chat_system_prompt()}]
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
        try:
            for kind, chunk in K.stream_chat(messages, model=model):
                if kind == "content" and chunk:
                    answer.append(chunk)
                    emit({"type": "token", "text": chunk})
            full = "".join(answer).strip()
            artifacts = self.app.artifacts_from_answer(full)
            visible = K.BLOCK_RE.sub("", full).strip()
            chat["messages"].append({"role": "assistant", "content": visible,
                                     "artifacts": artifacts})
            chat["updated"] = time.time()
            chat["model"] = model
            self.app.save_chats()
            emit({"type": "done", "content": visible, "artifacts": artifacts,
                  "chat_id": chat_id, "title": chat["title"], "model": model})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            emit({"type": "error", "message": str(exc)})


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