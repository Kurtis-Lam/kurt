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
<title>Kurt · Local AI Workspace</title>
<style>
:root{color-scheme:dark;--bg:#090d16;--panel:#111827;--panel2:#151e2d;--line:rgba(190,210,240,.12);--muted:#8d9bb0;--text:#eaf0f9;--green:#8ae6b0;--cyan:#8bd7e8;--danger:#ff8791;--shadow:0 22px 65px rgba(0,0,0,.35)}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(1000px 650px at 75% -5%,#172d3d 0%,transparent 65%),radial-gradient(800px 500px at -10% 100%,#17233e 0%,transparent 65%),var(--bg);font:14px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--text);height:100vh;overflow:hidden}button,input,textarea,select{font:inherit;color:inherit}button{cursor:pointer;border:1px solid var(--line);background:#1a2637;border-radius:10px;padding:9px 12px;transition:.16s}button:hover{border-color:#7fc9d5;background:#23374a}button.primary{background:linear-gradient(135deg,#7bdbab,#7dc9db);color:#07131b;border:0;font-weight:750}button.ghost{background:transparent}button.danger{color:#ffabb1}button:disabled{opacity:.45;cursor:not-allowed}.app{height:100vh;display:grid;grid-template-rows:64px 4px 1fr}.topbar{display:flex;align-items:center;gap:15px;padding:0 20px;border-bottom:1px solid var(--line);background:rgba(8,13,22,.72);backdrop-filter:blur(22px);z-index:3}.brand{display:flex;align-items:center;gap:11px;font-weight:800;letter-spacing:.02em;font-size:17px;min-width:185px}.mark{width:32px;height:32px;border-radius:11px;display:grid;place-items:center;color:#07151a;background:linear-gradient(140deg,#a1edb9,#69c5d7);box-shadow:0 5px 22px #5ecbb044;font-weight:950}.top-model{max-width:340px;min-width:180px;background:#121d2b;border:1px solid var(--line);border-radius:10px;padding:8px 32px 8px 11px}.top-spacer{flex:1}.workspace-label{font-size:12px;color:var(--muted);max-width:260px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.download-strip{background:#152437;position:relative;overflow:hidden}.download-fill{height:100%;width:0;background:linear-gradient(90deg,#68dba0,#77d1df);transition:width .25s}.download-caption{position:absolute;right:16px;top:5px;font-size:10px;color:#dceaf5;z-index:1;line-height:1}.layout{display:grid;grid-template-columns:252px minmax(0,1fr);min-height:0}.sidebar{border-right:1px solid var(--line);background:rgba(9,15,25,.58);backdrop-filter:blur(14px);display:flex;flex-direction:column;min-height:0;padding:15px 12px;gap:12px}.side-actions{display:grid;grid-template-columns:1fr 1fr;gap:8px}.side-title{color:var(--muted);font-weight:750;font-size:10px;letter-spacing:.12em;text-transform:uppercase;padding:5px 8px}.chat-list{overflow:auto;min-height:80px;flex:1}.chat-item{padding:10px 11px;border-radius:10px;cursor:pointer;color:#c5d0de;margin:2px 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.chat-item:hover,.chat-item.active{background:#1a2a3a;color:#fff}.side-bottom{border-top:1px solid var(--line);padding-top:10px;display:grid;gap:7px}.side-bottom button{text-align:left;background:transparent}.main{min-width:0;min-height:0;display:flex;flex-direction:column;position:relative}.chat-head{height:58px;flex-shrink:0;display:flex;align-items:center;justify-content:space-between;padding:0 24px;border-bottom:1px solid var(--line)}.chat-title{font-weight:700}.chat-meta{color:var(--muted);font-size:12px}.messages{flex:1;overflow:auto;padding:26px max(24px,calc((100% - 920px)/2));scroll-behavior:smooth}.welcome{max-width:760px;margin:7vh auto 0}.welcome h1{font-size:30px;letter-spacing:-.04em;margin:0 0 8px}.welcome p{color:var(--muted);font-size:15px}.suggestions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:25px}.suggestion{text-align:left;padding:15px;background:rgba(22,34,50,.65);border:1px solid var(--line);border-radius:14px}.suggestion span{display:block;color:var(--muted);font-size:12px;margin-top:4px}.message{max-width:900px;margin:0 auto 22px;display:flex;gap:13px;align-items:flex-start}.avatar{width:29px;height:29px;border-radius:10px;display:grid;place-items:center;flex:none;background:#1b3440;color:var(--green);font-weight:800}.message.user .avatar{background:#263047;color:#b4c7ef}.bubble{min-width:0;max-width:820px;width:100%;padding:2px 0}.role{font-size:11px;color:var(--muted);font-weight:750;letter-spacing:.06em;text-transform:uppercase;margin-bottom:5px}.content{overflow-wrap:anywhere}.content p{margin:0 0 12px}.content h1,.content h2,.content h3{line-height:1.2;margin:20px 0 10px}.content h1{font-size:24px}.content h2{font-size:20px}.content h3{font-size:17px}.content ul,.content ol{padding-left:23px}.content li{margin:4px 0}.content blockquote{border-left:3px solid #75c9d5;padding:4px 13px;margin:12px 0;color:#b5c5d4;background:#142131;border-radius:0 8px 8px 0}.content a{color:#8ed9e2}.content code:not(pre code){background:#253142;border:1px solid #344458;border-radius:5px;padding:2px 5px;font-size:.9em}.codebox{position:relative;background:#0a111c;border:1px solid #25364a;border-radius:12px;overflow:hidden;margin:12px 0}.codehead{display:flex;justify-content:space-between;align-items:center;padding:7px 11px;border-bottom:1px solid #233247;color:#9fb1c6;font-size:11px}.codebox pre{margin:0;padding:14px;overflow:auto;max-height:480px;white-space:pre;font:12.5px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;color:#e0eaf5}.copy-mini{font-size:11px;padding:4px 8px;background:#1b2a3c}.artifact{margin:15px 0;border:1px solid rgba(137,214,204,.28);background:linear-gradient(135deg,rgba(27,57,63,.58),rgba(20,31,48,.72));border-radius:15px;box-shadow:0 14px 38px #0003;overflow:hidden;backdrop-filter:blur(18px)}.artifact-top{display:flex;align-items:center;gap:10px;padding:13px 15px;cursor:pointer}.file-icon{width:32px;height:32px;border-radius:9px;background:#26434b;display:grid;place-items:center;color:#9de4c2}.file-name{font-weight:700;flex:1;min-width:0;overflow-wrap:anywhere}.file-check{font-size:11px;color:#9edbb9}.artifact-body{display:none;border-top:1px solid var(--line)}.artifact.open .artifact-body{display:block}.artifact-actions{display:flex;gap:7px;padding:10px 13px;border-top:1px solid var(--line);flex-wrap:wrap}.artifact-actions button{font-size:12px}.composer-wrap{padding:12px 24px 20px;background:linear-gradient(transparent,rgba(8,13,22,.92) 22%);flex-shrink:0}.composer{max-width:920px;margin:auto;border:1px solid rgba(165,204,222,.23);border-radius:17px;background:rgba(21,32,47,.88);backdrop-filter:blur(22px);box-shadow:var(--shadow);padding:12px}.composer textarea{display:block;width:100%;resize:none;min-height:55px;max-height:190px;background:transparent;border:0;outline:none;padding:5px;color:var(--text)}.compose-bottom{display:flex;align-items:center;gap:8px}.compose-hint{color:var(--muted);font-size:11px;flex:1}.compose-bottom button{font-size:12px}.modal-backdrop{position:fixed;inset:0;background:#030711b8;backdrop-filter:blur(9px);z-index:10;display:none;align-items:center;justify-content:center;padding:20px}.modal-backdrop.show{display:flex}.modal{width:min(850px,100%);max-height:88vh;overflow:auto;background:#111b2a;border:1px solid #34465a;border-radius:18px;box-shadow:0 30px 100px #0009}.modal-head{position:sticky;top:0;z-index:2;background:#111b2aeb;backdrop-filter:blur(16px);padding:17px 20px;display:flex;align-items:center;border-bottom:1px solid var(--line)}.modal-head h2{font-size:17px;margin:0;flex:1}.modal-body{padding:18px 20px 24px}.settings-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.field{display:grid;gap:5px;margin-bottom:13px}.field label{font-size:12px;color:#a9b8c9}.field input,.field select,.field textarea{background:#0b1421;border:1px solid #2a3a4e;border-radius:9px;padding:9px 10px;outline:none;width:100%}.field textarea{min-height:145px;resize:vertical;font:12px/1.55 ui-monospace,monospace}.field input:focus,.field select:focus,.field textarea:focus{border-color:#7ed1d4}.section{border-top:1px solid var(--line);margin-top:16px;padding-top:15px}.section h3{font-size:13px;margin:0 0 12px}.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.row>*{flex:1;min-width:100px}.key-row,.model-row{display:flex;gap:10px;align-items:center;padding:9px 10px;border:1px solid var(--line);border-radius:9px;margin:6px 0}.key-row .grow,.model-row .grow{flex:1;min-width:0}.key-row small,.model-row small{display:block;color:var(--muted)}.file-picker{border:1px solid var(--line);border-radius:10px;overflow:hidden;max-height:190px;overflow-y:auto}.file-row{padding:7px 10px;border-bottom:1px solid #ffffff0b;display:flex;gap:9px;align-items:center}.file-row:last-child{border:0}.file-row label{flex:1;overflow-wrap:anywhere}.toast{position:fixed;bottom:22px;left:50%;transform:translate(-50%,20px);background:#213348;border:1px solid #557082;padding:9px 15px;border-radius:10px;opacity:0;transition:.2s;pointer-events:none;z-index:20}.toast.show{opacity:1;transform:translate(-50%,0)}.empty{color:var(--muted);font-size:12px;padding:12px}.status-dot{width:7px;height:7px;border-radius:50%;display:inline-block;background:#8be5b1;margin-right:6px}
@media(max-width:850px){.layout{grid-template-columns:205px 1fr}.brand{min-width:initial}.workspace-label{display:none}.top-model{max-width:240px}}
@media(max-width:650px){.layout{grid-template-columns:1fr}.sidebar{display:none}.topbar{padding:0 12px;gap:8px}.brand{font-size:14px}.top-model{min-width:0;max-width:46vw;font-size:11px}.chat-head{padding:0 14px}.messages{padding:18px 14px}.composer-wrap{padding:8px 10px 12px}.suggestions{grid-template-columns:1fr}.settings-grid{grid-template-columns:1fr}.download-caption{right:7px}}
</style></head>
<body>
<div class="app">
<header class="topbar"><div class="brand"><div class="mark">K</div><span>kurt</span><span style="color:var(--muted);font-size:11px;font-weight:500">LOCAL WORKSPACE</span></div>
<select id="topModel" class="top-model" title="Active model"></select><div class="top-spacer"></div><span class="workspace-label" id="workspaceLabel"></span><button class="ghost" id="downloadBtn">Download model</button><button class="ghost" id="settingsBtn">Settings</button></header>
<div class="download-strip" title="Background model download"><div id="downloadFill" class="download-fill"></div><div id="downloadCaption" class="download-caption"></div></div>
<div class="layout"><aside class="sidebar"><div class="side-actions"><button class="primary" id="newChat">＋ New chat</button><button id="openFiles">Workspace files</button></div><div class="side-title">Recent chats</div><div id="chatList" class="chat-list"></div><div class="side-bottom"><button id="sidebarDownload">↓ Download a model</button><button id="sidebarSettings">⚙ Configure Kurt</button><div style="font-size:10px;color:var(--muted);padding:5px 8px"><span class="status-dot"></span>Server bound to localhost only</div></div></aside>
<main class="main"><div class="chat-head"><div><div id="chatTitle" class="chat-title">New conversation</div><div id="chatMeta" class="chat-meta">Select a model and start chatting</div></div><div class="row" style="flex:initial"><button id="contextBtn" class="ghost">＋ Add context</button><button id="clearChat" class="ghost">Clear</button></div></div><div id="messages" class="messages"><div class="welcome"><div class="mark" style="width:46px;height:46px;border-radius:15px;font-size:20px">K</div><h1>What are we building?</h1><p>Your local AI workspace. Chat with local or hosted models, manage your keys, and turn responses into files in your workspace.</p><div class="suggestions"><button class="suggestion" data-prompt="Inspect the current project structure and explain its architecture.">Understand my project<span>Review files and explain how everything fits together</span></button><button class="suggestion" data-prompt="Help me plan and implement a new feature. Ask for any missing requirements first.">Build a feature<span>Plan and implement a change in my workspace</span></button><button class="suggestion" data-prompt="Write a small utility script and return it as a file card.">Generate a file<span>Get a downloadable file or write it to the workspace</span></button><button class="suggestion" data-prompt="Help me debug this issue. Start by asking for the relevant error and files.">Debug code<span>Trace a problem and validate the proposed fix</span></button></div></div></div>
<div class="composer-wrap"><div class="composer"><textarea id="prompt" rows="2" placeholder="Message Kurt… (Enter to send, Shift+Enter for a new line)"></textarea><div class="compose-bottom"><span class="compose-hint" id="composeHint">Files are only written when you choose “Write to workspace”.</span><button id="stopBtn" class="ghost" style="display:none">Stop</button><button class="primary" id="sendBtn">Send ↑</button></div></div></div></main></div></div>
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
function renderMessage(role,content,artifacts=[]){const wrap=document.createElement('div');wrap.className='message '+role;wrap.innerHTML=`<div class="avatar">${role==='assistant'?'K':'Y'}</div><div class="bubble"><div class="role">${role==='assistant'?'Kurt':'You'}</div><div class="content">${renderMarkdown(content)}</div><div class="artifacts"></div></div>`;bindCopy(wrap);const target=wrap.querySelector('.artifacts');artifacts.forEach(a=>target.appendChild(artifactCard(a)));$('messages').appendChild(wrap);$('messages').scrollTop=$('messages').scrollHeight;return wrap}
function artifactCard(a){const card=document.createElement('div');card.className='artifact';card.innerHTML=`<div class="artifact-top"><div class="file-icon">⌘</div><div class="file-name">${esc(a.path)}</div><div class="file-check">${esc(a.validation?.message||'File preview')}</div><span>⌄</span></div><div class="artifact-body"><div class="codebox"><div class="codehead"><span>${esc((a.path.split('.').pop()||'FILE').toUpperCase())}</span><button class="copy-mini copy-artifact">Copy code</button></div><pre><code></code></pre></div><div class="artifact-actions"><button class="copy-artifact">Copy</button><button class="download-artifact">Download file</button><button class="primary write-artifact">Write to workspace</button></div></div>`;card.querySelector('code').textContent=a.content;card.querySelector('.artifact-top').onclick=()=>card.classList.toggle('open');card.querySelectorAll('.copy-artifact').forEach(b=>b.onclick=()=>navigator.clipboard.writeText(a.content).then(()=>toast('File content copied')));card.querySelector('.download-artifact').onclick=()=>{const blob=new Blob([a.content],{type:'application/octet-stream'}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=a.path.split('/').pop();link.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};card.querySelector('.write-artifact').onclick=async()=>{if(!a.validation?.ok){toast('Fix validation errors before writing');return}const overwrite=confirm(`Write ${a.path} into ${state.projectRoot}?${a.exists?'\nThis will overwrite the existing file.':''}`);if(!overwrite)return;try{const r=await api('/api/file/write',{path:a.path,content:a.content,overwrite});toast(r.message||'File written');}catch(e){toast(e.message)}};return card}
function chatWelcome(){$('messages').innerHTML=`<div class="welcome"><div class="mark" style="width:46px;height:46px;border-radius:15px;font-size:20px">K</div><h1>What are we building?</h1><p>Your local AI workspace. Chat with local or hosted models, manage your keys, and turn responses into files in your workspace.</p><div class="suggestions"><button class="suggestion" data-prompt="Inspect the current project structure and explain its architecture.">Understand my project<span>Review files and explain how everything fits together</span></button><button class="suggestion" data-prompt="Help me plan and implement a new feature. Ask for any missing requirements first.">Build a feature<span>Plan and implement a change in my workspace</span></button><button class="suggestion" data-prompt="Write a small utility script and return it as a file card.">Generate a file<span>Get a downloadable file or write it to the workspace</span></button><button class="suggestion" data-prompt="Help me debug this issue. Start by asking for the relevant error and files.">Debug code<span>Trace a problem and validate the proposed fix</span></button></div></div>`;document.querySelectorAll('[data-prompt]').forEach(b=>b.onclick=()=>{$('prompt').value=b.dataset.prompt;send()})}
function renderChats(){const el=$('chatList');el.innerHTML='';state.chats.forEach(c=>{const d=document.createElement('div');d.className='chat-item'+(c.id===state.chatId?' active':'');d.textContent=c.title||'New conversation';d.onclick=()=>loadChat(c.id);el.appendChild(d)})}
async function refreshBootstrap(){const d=await api('/api/bootstrap');state.models=d.models||[];state.providers=d.providers||[];state.keys=d.keys||[];state.remoteModels=d.remote_models||[];state.settings=d.settings||{};state.projectRoot=d.project_root||'';$('workspaceLabel').textContent=state.projectRoot;$('projectRoot').value=state.projectRoot;$('systemPrompt').value=state.settings.system_prompt||'';fillModels();renderKeys();renderRemoteModels();fillProviders()}
function fillProviders(){const opts=state.providers.map(p=>`<option value="${esc(p.id)}">${esc(p.label)}</option>`).join('');$('keyProvider').innerHTML=opts;$('remoteProvider').innerHTML=opts}
function renderKeys(){const el=$('keysList');el.innerHTML='';if(!state.keys.length){el.innerHTML='<div class="empty">No API keys configured yet.</div>';return}state.keys.forEach(k=>{const d=document.createElement('div');d.className='key-row';d.innerHTML=`<div class="grow"><b>${esc(k.label)}</b><small>${esc(k.provider)} · pool ${esc(k.group)} · ${esc(k.masked)}</small></div><button class="edit-key">Edit</button><button class="danger">Remove</button>`;d.querySelector('.edit-key').onclick=async()=>{const label=prompt('Account label:',k.label);if(label===null)return;const group=prompt('Key pool:',k.group);if(group===null)return;const value=prompt('New API key (leave blank to keep current):','');if(value===null)return;try{await api('/api/settings',{key_action:'edit',id:k.id,label,group,value});await refreshBootstrap();toast('API key updated')}catch(e){toast(e.message)}};d.querySelector('.danger').onclick=async()=>{if(!confirm('Remove this API key?'))return;await api('/api/settings',{key_action:'remove',id:k.id});await refreshBootstrap()};el.appendChild(d)})}
function renderRemoteModels(){const el=$('remoteList');el.innerHTML='';state.remoteModels.forEach(m=>{const d=document.createElement('div');d.className='model-row';d.innerHTML=`<div class="grow"><b>${esc(m.label||m.model)}</b><small>${esc(m.provider)} · ${esc(m.model)} · pool ${esc(m.key_group||'default')}</small></div><button class="edit-model">Edit</button><button class="danger">Remove</button>`;d.querySelector('.edit-model').onclick=async()=>{const model=prompt('API model ID:',m.model);if(model===null)return;const label=prompt('Display name:',m.label||m.model);if(label===null)return;const group=prompt('Key pool:',m.key_group||'default');if(group===null)return;const base=prompt('API base URL:',m.base_url||'');if(base===null)return;try{await api('/api/settings',{model_action:'edit',id:m.id,model,label,key_group:group,base_url:base});await refreshBootstrap();toast('Model settings updated')}catch(e){toast(e.message)}};d.querySelector('.danger').onclick=async()=>{if(!confirm('Remove this remote model?'))return;await api('/api/settings',{model_action:'remove',id:m.id});await refreshBootstrap()};el.appendChild(d)})}
async function refreshChats(){const d=await api('/api/chats');state.chats=d.chats||[];renderChats()}
async function newChat(){const d=await api('/api/chats',{create:true});state.chatId=d.id;chatWelcome();$('chatTitle').textContent='New conversation';$('chatMeta').textContent='Ready';await refreshChats()}
async function loadChat(id){const d=await api('/api/chats/'+encodeURIComponent(id));state.chatId=id;$('messages').innerHTML='';(d.messages||[]).forEach(m=>renderMessage(m.role,m.content,m.artifacts||[]));$('chatTitle').textContent=d.title||'Conversation';$('chatMeta').textContent=d.model||'Saved conversation';renderChats()}
async function send(){const text=$('prompt').value.trim();if(!text||state.busy)return;const selected=state.selectedFiles.slice();state.selectedFiles=[];$('prompt').value='';if(!state.chatId)await newChat();let full=text;if(selected.length){full+='\n\nSelected workspace files:\n';for(const file of selected){try{const d=await api('/api/file/read',{path:file});full+=`\n--- ${file} ---\n${d.content}\n`}catch(e){full+=`\n[Could not read ${file}: ${e.message}]\n`}}}renderMessage('user',text);state.busy=true;$('sendBtn').disabled=true;$('stopBtn').style.display='inline-block';$('chatMeta').textContent='Generating…';const assistantNode=renderMessage('assistant','');const contentEl=assistantNode.querySelector('.content');let collected='';const controller=new AbortController();state.abort=controller;try{const res=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json','X-Kurt-Token':TOKEN},body:JSON.stringify({chat_id:state.chatId,text:full,model:$('topModel').value}),signal:controller.signal});if(!res.ok){const x=await res.json();throw new Error(x.error||res.statusText)}const reader=res.body.getReader(),decoder=new TextDecoder();let buffer='';while(true){const {value,done}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});let sep;while((sep=buffer.indexOf('\n\n'))>=0){const frame=buffer.slice(0,sep);buffer=buffer.slice(sep+2);const dataLine=frame.split('\n').find(x=>x.startsWith('data:'));if(!dataLine)continue;const event=JSON.parse(dataLine.slice(5).trim());if(event.type==='token'){collected+=event.text;contentEl.innerHTML=renderMarkdown(collected);bindCopy(assistantNode);$('messages').scrollTop=$('messages').scrollHeight}else if(event.type==='done'){contentEl.innerHTML=renderMarkdown(event.content);bindCopy(assistantNode);const slot=assistantNode.querySelector('.artifacts');(event.artifacts||[]).forEach(a=>slot.appendChild(artifactCard(a)));$('chatTitle').textContent=event.title||'Conversation';$('chatMeta').textContent=event.model||'Completed';await refreshChats()}else if(event.type==='error')throw new Error(event.message)}}}catch(e){if(e.name!=='AbortError'){contentEl.textContent='Error: '+e.message;toast(e.message)}}finally{state.busy=false;state.abort=null;$('sendBtn').disabled=false;$('stopBtn').style.display='none';if($('chatMeta').textContent==='Generating…')$('chatMeta').textContent='Ready'}}
async function openSettings(){await refreshBootstrap();$('settingsModal').classList.add('show')}
async function saveSettings(){const payload={project_root:$('projectRoot').value,system_prompt:$('systemPrompt').value};if($('settingsModel').value)payload.model=$('settingsModel').value;const d=await api('/api/settings',payload);state.settings=d.settings;state.projectRoot=d.project_root;$('workspaceLabel').textContent=state.projectRoot;if(d.model)$('topModel').value=d.model;toast('Settings saved')}
async function browseFiles(){const path=$('fileFolder').value.trim();const d=await api('/api/files?path='+encodeURIComponent(path));$('fileFolder').value=d.path;const el=$('filePicker');el.innerHTML='';if(d.parent!==null){const b=document.createElement('div');b.className='file-row';b.innerHTML='<span>↰</span><label>.. (parent folder)</label>';b.onclick=()=>{$('fileFolder').value=d.parent;browseFiles()};el.appendChild(b)}d.items.forEach(item=>{const row=document.createElement('div');row.className='file-row';if(item.directory){row.innerHTML=`<span>📁</span><label>${esc(item.name)}/</label><button>Open</button>`;row.querySelector('button').onclick=()=>{$('fileFolder').value=item.path;browseFiles()}}else{row.innerHTML=`<input type="checkbox" value="${esc(item.path)}"><label>${esc(item.name)}</label><small>${esc(item.size)}</small>`}el.appendChild(row)})}
async function startDownload(){const model=$('downloadModel').value.trim();if(!model)return;const d=await api('/api/download',{model});$('downloadCaption').textContent='Queued: '+model;toast('Download started in background');$('downloadModal').classList.remove('show');pollDownload()}
async function pollDownload(){try{const d=await api('/api/download/status');const job=d.job;if(!job)return;const percent=job.total?job.completed/job.total*100:0;$('downloadFill').style.width=(job.state==='done'?100:percent)+'%';$('downloadCaption').textContent=job.state==='running'?`${job.model} · ${percent.toFixed(1)}% · ${job.human}`:`${job.model} · ${job.state}${job.error?' · '+job.error:''}`;if(job.state==='done'){await refreshBootstrap();toast('Model download complete')}else if(job.state==='error')toast(job.error||'Download failed');else setTimeout(pollDownload,1000)}catch(e){console.error(e)}}
$('sendBtn').onclick=send;$('stopBtn').onclick=()=>state.abort?.abort();$('prompt').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}});$('prompt').addEventListener('input',e=>{e.target.style.height='auto';e.target.style.height=Math.min(e.target.scrollHeight,190)+'px'});$('newChat').onclick=newChat;$('settingsBtn').onclick=$('sidebarSettings').onclick=openSettings;$('downloadBtn').onclick=$('sidebarDownload').onclick=()=>$('downloadModal').classList.add('show');$('openFiles').onclick=$('contextBtn').onclick=()=>{$('filesModal').classList.add('show');browseFiles()};$('topModel').onchange=async()=>{await api('/api/settings',{model:$('topModel').value});$('settingsModel').value=$('topModel').value;toast('Active model changed')};$('settingsModel').onchange=()=>$('topModel').value=$('settingsModel').value;$('saveSettings').onclick=saveSettings;$('reloadSettings').onclick=refreshBootstrap;$('addKey').onclick=async()=>{const value=$('keyValue').value.trim();if(!value){toast('Enter an API key');return}await api('/api/settings',{key_action:'add',provider:$('keyProvider').value,group:$('keyGroup').value,label:$('keyLabel').value,value});$('keyValue').value='';await refreshBootstrap();toast('API key saved locally')};$('addRemote').onclick=async()=>{const d=await api('/api/settings',{model_action:'add',provider:$('remoteProvider').value,model:$('remoteModelId').value,label:$('remoteLabel').value,key_group:$('remoteGroup').value,base_url:$('remoteBase').value});await refreshBootstrap();toast(d.message||'Model added')};$('refreshFiles').onclick=browseFiles;$('attachFiles').onclick=()=>{state.selectedFiles=Array.from(document.querySelectorAll('#filePicker input:checked')).map(x=>x.value);$('filesModal').classList.remove('show');$('composeHint').textContent=state.selectedFiles.length?state.selectedFiles.length+' file(s) attached to next message':'Files are only written when you choose “Write to workspace”.';toast(state.selectedFiles.length+' file(s) selected')};$('startDownload').onclick=startDownload;$('clearChat').onclick=async()=>{if(!state.chatId)return;if(!confirm('Clear this conversation?'))return;await api('/api/chats/'+encodeURIComponent(state.chatId),{clear:true});chatWelcome();$('chatTitle').textContent='New conversation';await refreshChats()};document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>$ (b.dataset.close).classList.remove('show'));document.querySelectorAll('.modal-backdrop').forEach(m=>m.onclick=e=>{if(e.target===m)m.classList.remove('show')});
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
