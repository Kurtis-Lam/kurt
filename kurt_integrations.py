"""Local stdio and remote Streamable HTTP MCP connections, plus reusable skills."""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class MCPError(RuntimeError):
    pass


BUILTIN_SKILLS = {
    "image-prompt": {
        "name": "Image prompt designer",
        "description": "Turn a brief into a detailed, model-aware image-generation prompt.",
        "instructions": """You are helping create a strong image-generation prompt. First identify the subject, action, setting, composition, camera/framing, lighting, palette, visual style, and important constraints. Preserve all user-specified details. Make the prompt concrete and visually coherent, avoid contradictory style directions, and include a concise negative prompt only when the target tool supports it. Do not claim an image has been generated; use an available connected image-generation MCP tool when the user requests generation.""",
    },
    "video-prompt": {
        "name": "Video prompt designer",
        "description": "Create structured video prompts with shots, motion, timing, and audio cues.",
        "instructions": """You are helping prepare a video-generation prompt. Specify aspect ratio, duration, frame rate when supported, scene continuity, camera movement, subject motion, environment, lighting, visual style, and audio/dialogue requirements. For multi-shot work, use a time-coded shot list and keep characters, wardrobe, geography, and props consistent. Do not imply a video exists until a connected MCP tool returns a result. Prefer a connected video-generation tool when the user asks to render a clip.""",
    },
    "storyboard": {
        "name": "Storyboard planner",
        "description": "Break a concept into a coherent sequence of visual shots.",
        "instructions": """Create a production-ready storyboard. Begin with the scene objective and visual continuity rules, then list shots in order with approximate duration, framing, camera movement, visible action, lighting, and audio. Keep each shot independently understandable while maintaining spatial and character continuity. Flag details that need a reference image rather than inventing them.""",
    },
}


def _read_jsonrpc(proc: subprocess.Popen, events: "queue.Queue[str]", request_id: int, timeout: float) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MCPError("Timed out waiting for the MCP server response.")
        try:
            line = events.get(timeout=remaining)
        except queue.Empty as exc:
            raise MCPError("Timed out waiting for the MCP server response.") from exc
        if line is None:
            code = proc.poll()
            raise MCPError("The MCP server exited before responding (exit code {}). Check its command and environment variables.".format(code))
        try:
            message = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            # Servers must use stdout for protocol messages. Ignore stray startup text
            # so that a helpful diagnostic can be shown if the handshake eventually times out.
            continue
        if isinstance(message, dict) and message.get("id") == request_id:
            if message.get("error"):
                error = message["error"]
                raise MCPError(str(error.get("message") or error)[:2000])
            result = message.get("result")
            if not isinstance(result, dict):
                raise MCPError("The MCP server returned an invalid JSON-RPC result.")
            return result



def _http_mcp_session(server: Dict[str, Any], operation: str, tool_name: str = "", arguments: Optional[Dict[str, Any]] = None, timeout: int = 120) -> Dict[str, Any]:
    """Connect to an MCP server over Streamable HTTP (API-key/custom-header auth)."""
    endpoint = str(server.get("url") or "").strip()
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
        raise MCPError("Use a valid HTTPS MCP URL (HTTP is allowed only for localhost).")
    headers = server.get("headers") if isinstance(server.get("headers"), dict) else {}
    session_id = None
    protocol_version = "2025-03-26"
    opener = urllib.request.build_opener(_NoRedirectHandler())

    def request(method: str, packet: Dict[str, Any], wait: float, expects_result: bool = True):
        nonlocal session_id
        request_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "Kurt/0.6.0 MCP",
        }
        for key, value in headers.items():
            request_headers[str(key)] = str(value)
        if session_id:
            request_headers["Mcp-Session-Id"] = session_id
            request_headers["MCP-Protocol-Version"] = protocol_version
        body = json.dumps(packet, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(endpoint, data=body, headers=request_headers, method="POST")
        try:
            with opener.open(req, timeout=max(1, wait)) as response:
                returned_session = response.headers.get("Mcp-Session-Id")
                if returned_session:
                    session_id = returned_session
                status = response.status
                raw = response.read(25 * 1024 * 1024 + 1)
                content_type = response.headers.get("Content-Type", "").lower()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(4000).decode("utf-8", "replace")
            except Exception:
                pass
            if exc.code == 202 and not expects_result:
                return None
            raise MCPError("MCP HTTP request failed ({}): {}".format(exc.code, detail[:1500] or exc.reason)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MCPError("Could not reach the MCP server: {}".format(exc)) from exc
        if len(raw) > 25 * 1024 * 1024:
            raise MCPError("The MCP server response exceeded 25 MB.")
        if status == 202 and not expects_result:
            return None
        if not expects_result and status in (200, 202, 204):
            return None
        if not raw:
            if expects_result:
                raise MCPError("The MCP server returned an empty response.")
            return None
        candidates = []
        if "text/event-stream" in content_type:
            data_lines = []
            for line in raw.decode("utf-8", "replace").splitlines() + [""]:
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                elif not line.strip() and data_lines:
                    joined = "\n".join(data_lines)
                    data_lines = []
                    try:
                        candidates.append(json.loads(joined))
                    except json.JSONDecodeError:
                        continue
        else:
            try:
                candidates.append(json.loads(raw.decode("utf-8", "replace")))
            except json.JSONDecodeError as exc:
                raise MCPError("The MCP server returned invalid JSON.") from exc
        for message in candidates:
            if not isinstance(message, dict):
                continue
            if message.get("id") != packet.get("id"):
                continue
            if message.get("error"):
                error = message["error"]
                raise MCPError(str(error.get("message") or error)[:2500])
            result = message.get("result")
            if not isinstance(result, dict):
                raise MCPError("The MCP server returned an invalid JSON-RPC result.")
            return result
        if expects_result:
            raise MCPError("The MCP server response did not contain the expected JSON-RPC reply.")
        return None

    try:
        initialize = request("POST", {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "Kurt", "version": "0.6.0"},
            },
        }, min(60, timeout), True)
        negotiated = initialize.get("protocolVersion")
        if isinstance(negotiated, str) and negotiated:
            protocol_version = negotiated
        request("POST", {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, min(30, timeout), False)
        if operation == "list":
            result = request("POST", {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, min(60, timeout), True)
            tools = result.get("tools")
            if not isinstance(tools, list):
                raise MCPError("The MCP server did not return a tools list.")
            return {"tools": [tool for tool in tools if isinstance(tool, dict)][:300]}
        if operation == "call":
            if not tool_name:
                raise MCPError("Choose an MCP tool to call.")
            result = request("POST", {
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments or {}},
            }, timeout, True)
            if result.get("isError"):
                content = result.get("content") or []
                detail = "\n".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
                raise MCPError((detail or "The MCP tool reported an error.")[:4000])
            return result
        raise MCPError("Unsupported MCP operation.")
    finally:
        if session_id:
            try:
                delete_req = urllib.request.Request(endpoint, data=None, headers={
                    **{str(key): str(value) for key, value in headers.items()},
                    "Mcp-Session-Id": session_id,
                    "MCP-Protocol-Version": protocol_version,
                }, method="DELETE")
                with opener.open(delete_req, timeout=3):
                    pass
            except Exception:
                pass


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _mcp_session(server: Dict[str, Any], operation: str, tool_name: str = "", arguments: Optional[Dict[str, Any]] = None, timeout: int = 120) -> Dict[str, Any]:
    if str(server.get("transport") or "stdio").lower() in ("http", "streamable-http", "streamable_http"):
        return _http_mcp_session(server, operation, tool_name=tool_name, arguments=arguments, timeout=timeout)
    command = str(server.get("command") or "").strip()
    args = server.get("args") if isinstance(server.get("args"), list) else []
    if not command:
        raise MCPError("This MCP server has no command configured.")
    executable = command if os.path.isabs(command) else shutil.which(command)
    if not executable:
        raise MCPError("Command {!r} was not found. Install it or use its full executable path.".format(command))
    argv = [executable] + [str(item) for item in args]
    env = os.environ.copy()
    env_values = server.get("env") if isinstance(server.get("env"), dict) else {}
    for key, value in env_values.items():
        env[str(key)] = str(value)
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=str(Path.home()),
            env=env,
        )
    except OSError as exc:
        raise MCPError("Could not start the MCP server: {}".format(exc)) from exc

    events: "queue.Queue[str]" = queue.Queue()

    def read_stdout() -> None:
        try:
            for line in proc.stdout:
                events.put(line.rstrip("\r\n"))
        finally:
            events.put(None)

    reader = threading.Thread(target=read_stdout, daemon=True, name="kurt-mcp-stdout")
    reader.start()
    next_id = 1

    def send(method: str, params: Dict[str, Any], wait: float) -> Dict[str, Any]:
        nonlocal next_id
        request_id = next_id
        next_id += 1
        packet = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            proc.stdin.write(json.dumps(packet, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise MCPError("The MCP server closed its input stream.") from exc
        return _read_jsonrpc(proc, events, request_id, wait)

    try:
        send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "Kurt", "version": "0.6.0"},
        }, min(60, timeout))
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n")
        proc.stdin.flush()
        if operation == "list":
            result = send("tools/list", {}, min(60, timeout))
            tools = result.get("tools")
            if not isinstance(tools, list):
                raise MCPError("The MCP server did not return a tools list.")
            return {"tools": [tool for tool in tools if isinstance(tool, dict)][:300]}
        if operation == "call":
            if not tool_name:
                raise MCPError("Choose an MCP tool to call.")
            result = send("tools/call", {"name": tool_name, "arguments": arguments or {}}, timeout)
            if result.get("isError"):
                content = result.get("content") or []
                detail = "\n".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
                raise MCPError((detail or "The MCP tool reported an error.")[:4000])
            return result
        raise MCPError("Unsupported MCP operation.")
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)


def list_mcp_tools(server: Dict[str, Any], timeout: int = 120) -> List[Dict[str, Any]]:
    return _mcp_session(server, "list", timeout=timeout).get("tools", [])


def call_mcp_tool(server: Dict[str, Any], tool_name: str, arguments: Dict[str, Any], timeout: int = 600) -> Dict[str, Any]:
    return _mcp_session(server, "call", tool_name=tool_name, arguments=arguments, timeout=timeout)


def validate_mcp_config(data: Dict[str, Any], existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    name = re.sub(r"\s+", " ", str(data.get("name") or (existing or {}).get("name") or "")).strip()
    if not name or len(name) > 60:
        raise MCPError("Give this MCP server a name (1–60 characters).")
    transport = str(data.get("transport") or (existing or {}).get("transport") or "stdio").strip().lower()
    if transport in ("http", "streamable_http"):
        transport = "streamable-http"
    if transport not in ("stdio", "streamable-http"):
        raise MCPError("Choose Local stdio or Remote Streamable HTTP.")

    command = str(data.get("command") or (existing or {}).get("command") or "").strip()
    url = str(data.get("url") or (existing or {}).get("url") or "").strip()
    if transport == "stdio":
        if not command or len(command) > 500 or any(ch in command for ch in "\r\n\0"):
            raise MCPError("Enter one executable command, such as comfy-mcp, uvx, npx, or python. Shell command strings are not supported.")
    else:
        parsed_url = urllib.parse.urlparse(url)
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        if (parsed_url.scheme != "https" and not (parsed_url.scheme == "http" and parsed_url.hostname in local_hosts)) or not parsed_url.hostname or parsed_url.username or parsed_url.password or len(url) > 2000:
            raise MCPError("Enter a valid HTTPS MCP URL. Plain HTTP is allowed only for localhost.")

    raw_args = data.get("args")
    if raw_args is None:
        args = list((existing or {}).get("args") or [])
    elif isinstance(raw_args, list):
        args = [str(item) for item in raw_args]
    elif isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args or "[]")
        except json.JSONDecodeError as exc:
            raise MCPError("Arguments must be a JSON array, for example [\"--stdio\"].") from exc
        if not isinstance(parsed, list):
            raise MCPError("Arguments must be a JSON array.")
        args = [str(item) for item in parsed]
    else:
        raise MCPError("Arguments must be a JSON array.")
    if len(args) > 64 or any(len(item) > 4000 or "\0" in item for item in args):
        raise MCPError("Use no more than 64 arguments, each 4,000 characters or fewer.")

    env = dict((existing or {}).get("env") or {})
    raw_env = data.get("env")
    if raw_env not in (None, ""):
        if isinstance(raw_env, str):
            try:
                raw_env = json.loads(raw_env)
            except json.JSONDecodeError as exc:
                raise MCPError("Environment variables must be a JSON object, for example {\"COMFY_BIN\": \"/path/to/comfy\"}.") from exc
        if not isinstance(raw_env, dict):
            raise MCPError("Environment variables must be a JSON object.")
        blocked = {"PATH", "HOME", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "SHELL", "LD_PRELOAD"}
        for key, value in raw_env.items():
            key = str(key).upper()
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,79}", key) or key in blocked or key.startswith(("DYLD_", "LD_")):
                raise MCPError("Environment variable name {!r} is not allowed here.".format(key))
            value = str(value)
            if len(value) > 12000 or "\0" in value:
                raise MCPError("Environment values must be 12,000 characters or fewer.")
            env[key] = value
    if len(env) > 64:
        raise MCPError("Use no more than 64 environment variables.")

    request_headers = dict((existing or {}).get("headers") or {})
    raw_headers = data.get("headers")
    if raw_headers not in (None, ""):
        if isinstance(raw_headers, str):
            try:
                raw_headers = json.loads(raw_headers)
            except json.JSONDecodeError as exc:
                raise MCPError("HTTP headers must be a JSON object, for example {\"X-API-Key\": \"...\"}.") from exc
        if not isinstance(raw_headers, dict):
            raise MCPError("HTTP headers must be a JSON object.")
        reserved = {"host", "content-length", "content-type", "accept", "connection", "transfer-encoding", "origin", "mcp-session-id", "mcp-protocol-version"}
        for key, value in raw_headers.items():
            key = str(key).strip()
            if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", key) or key.lower() in reserved:
                raise MCPError("HTTP header name {!r} is not allowed.".format(key))
            value = str(value)
            if len(value) > 12000 or any(ch in value for ch in "\r\n\0"):
                raise MCPError("HTTP header values must be single-line and no longer than 12,000 characters.")
            request_headers[key] = value
    if len(request_headers) > 32:
        raise MCPError("Use no more than 32 custom HTTP headers.")

    server_id = str(data.get("id") or (existing or {}).get("id") or uuid.uuid4().hex[:12])
    if not re.fullmatch(r"[a-f0-9]{8,32}", server_id):
        raise MCPError("Invalid MCP server ID.")
    return {
        "id": server_id,
        "name": name,
        "transport": transport,
        "command": command if transport == "stdio" else "",
        "url": url if transport == "streamable-http" else "",
        "args": args if transport == "stdio" else [],
        "env": env if transport == "stdio" else {},
        "headers": request_headers if transport == "streamable-http" else {},
        "enabled": True,
        "tools": list((existing or {}).get("tools") or []),
        "last_error": "",
        "updated_at": time.time(),
    }


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return value[:48] or "skill"


def list_skills(skills_dir: Path) -> List[Dict[str, Any]]:
    skills = []
    for skill_id, item in BUILTIN_SKILLS.items():
        skills.append({"id": skill_id, "name": item["name"], "description": item["description"], "builtin": True})
    try:
        skills_dir.mkdir(parents=True, exist_ok=True)
        skills_dir.chmod(0o700)
        for folder in sorted(skills_dir.iterdir(), key=lambda path: path.name.casefold()):
            if folder.is_symlink() or not folder.is_dir() or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", folder.name):
                continue
            file = folder / "SKILL.md"
            if file.is_symlink() or not file.is_file() or file.stat().st_size > 50000:
                continue
            content = file.read_text(encoding="utf-8", errors="replace")
            name = folder.name.replace("-", " ").title()
            description = "Custom skill"
            if content.startswith("---"):
                meta = content.split("---", 2)
                if len(meta) >= 3:
                    for line in meta[1].splitlines():
                        key, sep, value = line.partition(":")
                        if sep and key.strip().lower() == "name":
                            name = value.strip().strip("\"'")[:80] or name
                        elif sep and key.strip().lower() == "description":
                            description = value.strip().strip("\"'")[:200] or description
            skills.append({"id": folder.name, "name": name, "description": description, "builtin": False})
    except OSError:
        pass
    return skills


def get_skill(skills_dir: Path, skill_id: str) -> Optional[Dict[str, str]]:
    skill_id = str(skill_id or "")
    if skill_id in BUILTIN_SKILLS:
        return dict(BUILTIN_SKILLS[skill_id])
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", skill_id):
        return None
    folder = skills_dir / skill_id
    file = folder / "SKILL.md"
    try:
        if folder.is_symlink() or file.is_symlink() or not file.is_file() or file.stat().st_size > 50000:
            return None
        content = file.read_text(encoding="utf-8")
    except OSError:
        return None
    name = skill_id.replace("-", " ").title()
    description = "Custom skill"
    instructions = content
    if content.startswith("---"):
        meta = content.split("---", 2)
        if len(meta) == 3:
            instructions = meta[2].strip()
            for line in meta[1].splitlines():
                key, sep, value = line.partition(":")
                if sep and key.strip().lower() == "name":
                    name = value.strip().strip("\"'")[:80] or name
                elif sep and key.strip().lower() == "description":
                    description = value.strip().strip("\"'")[:200] or description
    return {"name": name, "description": description, "instructions": instructions}


def save_skill(skills_dir: Path, data: Dict[str, Any]) -> Dict[str, str]:
    name = re.sub(r"\s+", " ", str(data.get("name") or "")).strip()
    description = re.sub(r"\s+", " ", str(data.get("description") or "")).strip()
    instructions = str(data.get("instructions") or "").strip()
    skill_id = str(data.get("id") or _slug(name))
    if skill_id in BUILTIN_SKILLS:
        raise MCPError("Built-in skills cannot be overwritten.")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", skill_id):
        raise MCPError("Skill IDs may contain lowercase letters, numbers, hyphens, and underscores.")
    if not name or len(name) > 80 or len(description) > 200 or not instructions or len(instructions) > 40000:
        raise MCPError("Add a name, description (up to 200 characters), and instructions (1–40,000 characters).")
    skills_dir.mkdir(parents=True, exist_ok=True)
    skills_dir.chmod(0o700)
    folder = skills_dir / skill_id
    folder.mkdir(mode=0o700, exist_ok=True)
    if folder.is_symlink():
        raise MCPError("Symbolic-link skill folders are not allowed.")
    target = folder / "SKILL.md"
    payload = "---\nname: {}\ndescription: {}\n---\n\n{}\n".format(
        name.replace("\n", " "), description.replace("\n", " "), instructions
    )
    target.write_text(payload, encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return {"id": skill_id, "name": name, "description": description, "builtin": False}


def delete_skill(skills_dir: Path, skill_id: str) -> None:
    if skill_id in BUILTIN_SKILLS or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", skill_id):
        raise MCPError("That skill cannot be deleted.")
    folder = skills_dir / skill_id
    target = folder / "SKILL.md"
    if folder.is_symlink() or target.is_symlink() or not target.is_file():
        raise MCPError("Skill not found.")
    target.unlink()
    folder.rmdir()
