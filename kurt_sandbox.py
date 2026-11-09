"""Ephemeral validation environments for code generated or edited by Kurt.

A virtual environment isolates Python packages, not operating-system access.  This
module therefore compiles and statically validates generated files; it never
executes arbitrary generated application code automatically.
"""

import ast
import json
import os
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
