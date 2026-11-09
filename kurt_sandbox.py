"""Ephemeral validation environments for code generated or edited by Kurt.

A virtual environment isolates Python packages, not operating-system access.  This
module therefore compiles and statically validates generated files; it never
executes arbitrary generated application code automatically.
"""

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import venv
from html.parser import HTMLParser
from pathlib import Path


CODE_SUFFIXES = {
    ".py", ".pyw", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
    ".json", ".html", ".htm", ".css", ".scss", ".sh", ".bash",
    ".java", ".c", ".h", ".cpp", ".hpp", ".rs", ".go", ".rb",
}


class _HTMLCheck(HTMLParser):
    pass


def is_code_file(filename):
    return Path(str(filename)).suffix.lower() in CODE_SUFFIXES


def _run(command, timeout=20):
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": tempfile.gettempdir(),
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "VIRTUAL_ENV": str(Path(command[0]).parent.parent),
            },
        )
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        return result.returncode, output
    except subprocess.TimeoutExpired:
        return -1, "Validation timed out."
    except OSError as exc:
        return -1, str(exc)


def validate_content(filename, content):
    """Create a fresh temporary venv and run non-executing source checks."""
    suffix = Path(str(filename)).suffix.lower()
    if suffix not in CODE_SUFFIXES:
        return {"ok": True, "checked": False, "message": "Not a recognised source-code file."}

    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="kurt-check-") as temporary:
            root = Path(temporary)
            env_dir = root / "venv"
            work_dir = root / "workspace"
            work_dir.mkdir()
            venv.EnvBuilder(with_pip=False, clear=True).create(env_dir)
            if os.name == "nt":
                python = env_dir / "Scripts" / "python.exe"
            else:
                python = env_dir / "bin" / "python"
            source = work_dir / ("generated" + suffix)
            source.write_text(str(content), encoding="utf-8")

            if suffix in (".py", ".pyw"):
                code, output = _run([str(python), "-I", "-m", "py_compile", str(source)])
                if code:
                    return {"ok": False, "checked": True, "message": output or "Python compilation failed."}
                try:
                    ast.parse(str(content), filename=str(filename))
                except SyntaxError as exc:
                    return {
                        "ok": False, "checked": True,
                        "message": "{} (line {})".format(exc.msg, exc.lineno),
                    }
                checks.append("Python bytecode compilation")
                checks.append("Python AST parse")
            elif suffix == ".json":
                json.loads(content)
                checks.append("JSON parse")
            elif suffix in (".html", ".htm"):
                parser = _HTMLCheck()
                parser.feed(str(content))
                parser.close()
                checks.append("HTML parser")
            elif suffix in (".js", ".mjs", ".cjs") and shutil.which("node"):
                code, output = _run([shutil.which("node"), "--check", str(source)])
                if code:
                    return {"ok": False, "checked": True, "message": output or "JavaScript syntax check failed."}
                checks.append("Node.js syntax check")
            elif suffix in (".sh", ".bash") and shutil.which("bash"):
                code, output = _run([shutil.which("bash"), "-n", str(source)])
                if code:
                    return {"ok": False, "checked": True, "message": output or "Shell syntax check failed."}
                checks.append("Bash syntax check")
            elif suffix == ".rb" and shutil.which("ruby"):
                code, output = _run([shutil.which("ruby"), "-c", str(source)])
                if code:
                    return {"ok": False, "checked": True, "message": output or "Ruby syntax check failed."}
                checks.append("Ruby syntax check")
            elif suffix in (".ts", ".tsx"):
                checks.append("Temporary Python venv created; TypeScript compiler not configured")
            else:
                checks.append("Temporary Python venv created; no language compiler configured")

            return {
                "ok": True,
                "checked": True,
                "message": "Passed: " + ", ".join(checks) + ".",
            }
    except Exception as exc:
        return {"ok": False, "checked": True, "message": "Could not validate source: {}".format(exc)}


def run_workspace_command(workspace, env_dir, command, timeout=90):
    """Run one non-shell validation command inside a chat's Python virtual environment.

    A venv isolates Python packages but is not an OS security boundary. Commands are
    therefore parsed without a shell, restricted to known developer tools, and always
    run with the conversation workspace as their working directory.
    """
    import shlex
    import subprocess
    import sys
    import time

    root = Path(workspace).resolve()
    env_root = Path(env_dir).resolve()
    raw = str(command or "").strip()
    if not raw or len(raw) > 600:
        return {"ok": False, "returncode": -1, "output": "Command is empty or too long."}
    if any(ch in raw for ch in ("\n", "\r", ";", "|", "&", "<", ">", "`", "$")):
        return {"ok": False, "returncode": -1, "output": "Shell operators and multi-line commands are not allowed. Run one command at a time."}
    try:
        args = shlex.split(raw, posix=(os.name != "nt"))
    except ValueError as exc:
        return {"ok": False, "returncode": -1, "output": "Could not parse command: " + str(exc)}
    if not args or len(args) > 32:
        return {"ok": False, "returncode": -1, "output": "Command has too many arguments."}

    if os.name == "nt":
        scripts = env_root / "Scripts"
        python = scripts / "python.exe"
        bin_dir = scripts
    else:
        bin_dir = env_root / "bin"
        python = bin_dir / "python"
    if not python.is_file():
        try:
            venv.EnvBuilder(with_pip=True, clear=False).create(env_root)
        except Exception as exc:
            return {"ok": False, "returncode": -1, "output": "Could not prepare chat virtual environment: " + str(exc)}
        if not python.is_file():
            return {"ok": False, "returncode": -1, "output": "The chat virtual environment was not created correctly."}

    executable = Path(args[0]).name.lower()
    command_args = args[1:]
    if executable in ("python", "python3", "python.exe"):
        executable_path = str(python)
    elif executable in ("pytest", "pytest.exe", "ruff", "ruff.exe", "mypy", "mypy.exe"):
        executable_path = str(bin_dir / (executable + (".exe" if os.name == "nt" else "")))
        if not Path(executable_path).is_file():
            return {
                "ok": False, "returncode": -1,
                "output": "{} is not installed in this chat's venv. Ask Kurt to install it with `python -m pip install {}` first.".format(executable, executable),
            }
    elif executable in ("node", "node.exe"):
        if not command_args or command_args[0] != "--check":
            return {"ok": False, "returncode": -1, "output": "Node is only enabled for syntax checks (`node --check relative/file.js`)."}
        executable_path = shutil.which("node")
        if not executable_path:
            return {"ok": False, "returncode": -1, "output": "Node.js is not installed on this computer."}
    elif executable in ("bash", "bash.exe"):
        if not command_args or command_args[0] != "-n":
            return {"ok": False, "returncode": -1, "output": "Bash is only enabled for syntax checks (`bash -n relative/file.sh`)."}
        executable_path = shutil.which("bash")
        if not executable_path:
            return {"ok": False, "returncode": -1, "output": "Bash is not installed on this computer."}
    else:
        return {
            "ok": False, "returncode": -1,
            "output": "Command is not permitted. Use python, pytest, ruff, mypy, node --check, or bash -n.",
        }

    if executable in ("python", "python3", "python.exe"):
        if command_args[:2] and command_args[0] == "-m":
            module = command_args[1] if len(command_args) > 1 else ""
            if module not in {"py_compile", "compileall", "pytest", "unittest", "pip"}:
                return {"ok": False, "returncode": -1, "output": "Python modules are limited to py_compile, compileall, pytest, unittest, and pip."}
        elif command_args and command_args[0] in ("-c", "-", "-m"):
            return {"ok": False, "returncode": -1, "output": "Inline Python and stdin execution are not allowed. Use a workspace file or an approved -m tool."}

    for token in command_args:
        if token.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", token) or token.startswith("~"):
            return {"ok": False, "returncode": -1, "output": "Use relative paths inside the chat workspace, not absolute paths."}
        if ".." in re.split(r"[\\/]", token):
            return {"ok": False, "returncode": -1, "output": "Path traversal outside the chat workspace is not allowed."}

    final_args = [executable_path] + command_args
    env = {
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
        "HOME": str(root),
        "USERPROFILE": str(root),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "VIRTUAL_ENV": str(env_root),
        "KURT_CHAT_WORKSPACE": str(root),
    }
    for name in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "LANG", "LC_ALL"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    started = time.time()
    try:
        result = subprocess.run(
            final_args,
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=max(1, min(int(timeout), 120)),
            check=False,
            shell=False,
        )
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        if len(output) > 12000:
            output = output[:12000] + "\n[Output truncated]"
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "output": output or ("Command completed successfully." if result.returncode == 0 else "Command exited without output."),
            "elapsed": round(time.time() - started, 2),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": -1, "output": "Command timed out after {} seconds.".format(min(int(timeout), 120))}
    except OSError as exc:
        return {"ok": False, "returncode": -1, "output": "Could not start command: " + str(exc)}
