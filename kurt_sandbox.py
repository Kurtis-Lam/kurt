"""Per-chat validation and OS-sandboxed command execution for Kurt.

Code commands are only launched inside a platform-enforced sandbox. A Python virtual
environment is used for package isolation, never as a substitute for OS confinement.
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
            },
        )
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        return result.returncode, output
    except subprocess.TimeoutExpired:
        return -1, "Validation timed out."
    except OSError as exc:
        return -1, str(exc)


def validate_content(filename, content):
    """Run fast, non-executing source checks without building any environment."""
    suffix = Path(str(filename)).suffix.lower()
    if suffix not in CODE_SUFFIXES:
        return {"ok": True, "checked": False, "message": "Not a recognised source-code file."}

    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="kurt-check-") as temporary:
            root = Path(temporary)
            work_dir = root / "workspace"
            work_dir.mkdir()
            source = work_dir / ("generated" + suffix)
            source.write_text(str(content), encoding="utf-8")

            if suffix in (".py", ".pyw"):
                try:
                    compile(str(content), str(filename), "exec", dont_inherit=True)
                except SyntaxError as exc:
                    return {
                        "ok": False, "checked": True,
                        "message": "{} (line {})".format(exc.msg, exc.lineno),
                    }
                except (ValueError, RecursionError, MemoryError) as exc:
                    return {"ok": False, "checked": True, "message": "Python compilation failed: {}".format(exc)}
                checks.append("Python compilation (no code executed)")
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
                checks.append("TypeScript compiler not configured")
            else:
                checks.append("no language compiler configured")

            return {
                "ok": True,
                "checked": True,
                "message": "Passed: " + ", ".join(checks) + ".",
            }
    except Exception as exc:
        return {"ok": False, "checked": True, "message": "Could not validate source: {}".format(exc)}


def _is_within(path, root):
    try:
        Path(path).resolve(strict=False).relative_to(Path(root).resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def _mac_sandbox_profile(write_roots, allow_network=False):
    """Build a Seatbelt profile with broad read access and narrowly scoped writes."""
    def quoted(path):
        return json.dumps(str(Path(path).resolve(strict=False)))

    existing = []
    seen = set()
    for item in write_roots:
        path = Path(item).resolve(strict=False)
        if path.exists() and path.is_dir() and str(path) not in seen:
            existing.append(path)
            seen.add(str(path))
    if not existing:
        raise ValueError("No writable session directory was provided.")

    rules = [
        '(version 1)',
        '(deny default)',
        '(allow process-fork)',
        '(allow process-exec)',
        '(allow signal (target self))',
        '(allow sysctl-read)',
        '(allow mach-lookup)',
        # Both modes may read files the current OS account itself can read.
        # The OS account boundary still applies; this does not grant privilege elevation.
        '(allow file-read*)',
        # Writes remain confined to private session state and, only when linked, the chosen folder.
    ]
    rules.append('(allow file-write* {})'.format(' '.join('(subpath {})'.format(quoted(path)) for path in existing)))
    if allow_network:
        rules.append('(allow network-outbound)')
    return '\n'.join(rules)


def run_workspace_command(workspace, env_dir, command, timeout=120, project_root=None,
                          working_directory=None, writable_roots=None):
    """Run a user-approved shell command under the macOS Seatbelt filesystem sandbox.

    Reads are permitted wherever the current OS account has read access. Writes are allowed
    only under this chat's private workspace, private venv, and the explicitly linked folder
    (if any). The command is never run unrestricted on platforms without the OS sandbox.
    """
    import subprocess
    import time

    private_root = Path(workspace).resolve()
    env_root = Path(env_dir).resolve()
    cwd = Path(working_directory or private_root).expanduser().resolve(strict=True)
    if not cwd.is_dir():
        return {'ok': False, 'returncode': -1, 'output': 'The session working directory does not exist.'}
    raw = str(command or '').strip()
    if not raw or len(raw) > 4000:
        return {'ok': False, 'returncode': -1, 'output': 'Command is empty or too long (maximum 4,000 characters).'}
    if '\x00' in raw:
        return {'ok': False, 'returncode': -1, 'output': 'The command contains a null character.'}
    if re.search(r'(?i)\b(sudo|su|doas|pkexec|runas)\b', raw):
        return {'ok': False, 'returncode': -1,
                'output': 'Privilege escalation is disabled. Commands run only with Kurt\'s current OS-user permissions.'}
    if sys.platform != 'darwin':
        return {'ok': False, 'returncode': -1,
                'output': 'macOS Seatbelt is unavailable on this platform. The command was not run; no unrestricted fallback is used.'}
    sandbox_exec = shutil.which('sandbox-exec') or ('/usr/bin/sandbox-exec' if Path('/usr/bin/sandbox-exec').is_file() else None)
    shell = '/bin/zsh' if Path('/bin/zsh').is_file() else '/bin/sh'
    if not sandbox_exec or not Path(shell).is_file():
        return {'ok': False, 'returncode': -1,
                'output': 'The macOS Seatbelt sandbox or shell is unavailable. The command was not run.'}

    bin_dir = env_root / ('Scripts' if os.name == 'nt' else 'bin')
    python = bin_dir / ('python.exe' if os.name == 'nt' else 'python')
    if not python.is_file():
        try:
            venv.EnvBuilder(with_pip=True, clear=False).create(env_root)
        except Exception as exc:
            return {'ok': False, 'returncode': -1, 'output': 'Could not prepare this chat\'s private environment: ' + str(exc)}
        if not python.is_file():
            return {'ok': False, 'returncode': -1, 'output': 'The private Python environment was not created correctly.'}

    # HOME and temporary files remain inside the private chat workspace.
    private_tmp = private_root / '.tmp'
    private_home = private_root / '.home'
    for folder in (private_tmp, private_home, env_root):
        folder.mkdir(parents=True, exist_ok=True)
        try:
            folder.chmod(0o700)
        except OSError:
            pass
    roots = list(writable_roots or [private_root, env_root])
    roots.extend([private_root, env_root, private_tmp, private_home])
    # Normalize/deduplicate roots; do not silently add the computer's home or project root.
    normalized_roots = []
    seen = set()
    for item in roots:
        path = Path(item).expanduser().resolve(strict=False)
        if path.exists() and path.is_dir() and str(path) not in seen:
            normalized_roots.append(path)
            seen.add(str(path))
    if not normalized_roots:
        return {'ok': False, 'returncode': -1, 'output': 'No writable session folders are available.'}

    # Explicit package-manager / repository commands may need outbound access. Other commands
    # receive no network permission, even though local file reads are broad.
    allow_network = bool(re.search(r'(?i)(?:\b(?:pip|pip3|npm|pnpm|yarn|cargo|go)\s+(?:install|add|get)\b|\bpython(?:3)?\s+-m\s+pip\s+install\b|\b(?:git\s+(?:clone|fetch|pull))\b|\b(?:curl|wget)\b)', raw))
    try:
        profile = _mac_sandbox_profile(normalized_roots, allow_network=allow_network)
    except Exception as exc:
        return {'ok': False, 'returncode': -1, 'output': 'Could not prepare the macOS sandbox profile: ' + str(exc)}

    safe_path = os.pathsep.join(str(item) for item in (
        bin_dir, Path('/opt/homebrew/bin'), Path('/usr/local/bin'),
        Path('/usr/bin'), Path('/bin'), Path('/usr/sbin'), Path('/sbin'),
    ))
    env = {
        'PATH': safe_path,
        'HOME': str(private_home),
        'USERPROFILE': str(private_home),
        'TMPDIR': str(private_tmp),
        'TMP': str(private_tmp),
        'TEMP': str(private_tmp),
        'PYTHONNOUSERSITE': '1',
        'VIRTUAL_ENV': str(env_root),
        'KURT_CHAT_WORKSPACE': str(private_root),
        'KURT_SESSION_ROOT': str(cwd),
        'LANG': os.environ.get('LANG', 'en_US.UTF-8'),
        'LC_ALL': os.environ.get('LC_ALL', 'en_US.UTF-8'),
    }
    started = time.monotonic()
    try:
        result = subprocess.run(
            [sandbox_exec, '-p', profile, shell, '-c', raw],
            cwd=str(cwd), env=env, capture_output=True, text=True,
            timeout=max(1, min(int(timeout), 180)), check=False, shell=False,
        )
        output = ((result.stdout or '') + (result.stderr or '')).strip()
        if result.returncode < 0 and not output:
            try:
                signal_name = __import__('signal').Signals(-result.returncode).name
            except (ValueError, AttributeError):
                signal_name = 'signal {}'.format(-result.returncode)
            output = ('The sandboxed command was terminated by {} (signal {}). This indicates an OS sandbox/process abort, not a normal mkdir permission error.'.format(signal_name, -result.returncode))
        if len(output) > 16000:
            output = output[:16000] + '\n[Output truncated]'
        return {
            'ok': result.returncode == 0,
            'returncode': result.returncode,
            'output': output or ('Command completed successfully.' if result.returncode == 0 else 'Command exited without output.'),
            'elapsed': round(time.monotonic() - started, 2),
            'working_directory': str(cwd),
            'sandbox': 'macOS Seatbelt',
            'network_allowed': allow_network,
        }
    except subprocess.TimeoutExpired:
        return {'ok': False, 'returncode': -1, 'output': 'Command timed out after {} seconds.'.format(min(int(timeout), 180))}
    except OSError as exc:
        return {'ok': False, 'returncode': -1, 'output': 'Could not start the sandboxed command: ' + str(exc)}