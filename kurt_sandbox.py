"""Per-chat validation and OS-sandboxed command execution for Kurt.

Code commands are only launched inside a platform-enforced sandbox. A Python virtual
environment is used for package isolation, never as a substitute for OS confinement.
"""

import ast
import codecs
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time as _time
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


TTY_ROWS = 24
TTY_COLS = 80
TTY_KEYS = {
    'ENTER': '\r', 'RETURN': '\r', 'SPACE': ' ', 'TAB': '\t', 'ESC': '\x1b', 'ESCAPE': '\x1b',
    'BACKSPACE': '\x7f', 'UP': '\x1b[A', 'DOWN': '\x1b[B', 'RIGHT': '\x1b[C', 'LEFT': '\x1b[D',
    'CTRL-C': '\x03', 'CTRL-D': '\x04', 'CTRL-Q': '\x11',
}


def parse_tty_keys(text):
    """Turn a line such as 'w a ENTER UP q' into a list of byte strings to type."""
    keys = []
    for token in str(text or '').split():
        named = TTY_KEYS.get(token.upper())
        if named is not None:
            keys.append(named)
        else:
            keys.extend(token)
    return keys[:200]


class _Screen:
    """A tiny terminal emulator so curses output can be read as a final screen."""

    def __init__(self, rows=TTY_ROWS, cols=TTY_COLS):
        self.rows, self.cols = rows, cols
        self.grid = [[' '] * cols for _ in range(rows)]
        self.row = self.col = 0

    def _scroll(self):
        self.grid.pop(0)
        self.grid.append([' '] * self.cols)

    def _put(self, ch):
        if self.col >= self.cols:
            self.col = 0
            self.row += 1
        if self.row >= self.rows:
            self.row = self.rows - 1
            self._scroll()
        self.grid[self.row][self.col] = ch
        self.col += 1

    def feed(self, text):
        i, n = 0, len(text)
        while i < n:
            ch = text[i]
            if ch == '\x1b':
                if i + 1 < n and text[i + 1] == '[':
                    j = i + 2
                    while j < n and not ('@' <= text[j] <= '~'):
                        j += 1
                    if j >= n:
                        break
                    self._csi(text[i + 2:j], text[j])
                    i = j + 1
                    continue
                if i + 1 < n and text[i + 1] == ']':
                    j = i + 2
                    while j < n and text[j] not in '\x07':
                        if text[j] == '\x1b' and j + 1 < n and text[j + 1] == '\\':
                            j += 1
                            break
                        j += 1
                    i = j + 1
                    continue
                i += 3 if i + 1 < n and text[i + 1] in '()#' else 2
                continue
            if ch == '\r':
                self.col = 0
            elif ch == '\n':
                self.row += 1
                if self.row >= self.rows:
                    self.row = self.rows - 1
                    self._scroll()
            elif ch == '\b':
                self.col = max(0, self.col - 1)
            elif ch == '\t':
                self.col = min(self.cols - 1, (self.col // 8 + 1) * 8)
            elif ch >= ' ':
                self._put(ch)
            i += 1

    def _csi(self, params, final):
        private = params.startswith('?') or params.startswith('>')
        nums = []
        for part in params.lstrip('?>').split(';'):
            try:
                nums.append(int(part))
            except ValueError:
                nums.append(0)
        first = nums[0] if nums and nums[0] else 1
        if private:
            return
        if final in 'Hf':
            self.row = min(self.rows - 1, max(0, (nums[0] or 1) - 1))
            self.col = min(self.cols - 1, max(0, ((nums[1] if len(nums) > 1 else 1) or 1) - 1))
        elif final == 'A':
            self.row = max(0, self.row - first)
        elif final == 'B':
            self.row = min(self.rows - 1, self.row + first)
        elif final == 'C':
            self.col = min(self.cols - 1, self.col + first)
        elif final == 'D':
            self.col = max(0, self.col - first)
        elif final == 'G':
            self.col = min(self.cols - 1, first - 1)
        elif final == 'd':
            self.row = min(self.rows - 1, first - 1)
        elif final == 'J':
            mode = nums[0] if nums else 0
            if mode in (2, 3):
                self.grid = [[' '] * self.cols for _ in range(self.rows)]
            elif mode == 0:
                for c in range(self.col, self.cols):
                    self.grid[self.row][c] = ' '
                for r in range(self.row + 1, self.rows):
                    self.grid[r] = [' '] * self.cols
            elif mode == 1:
                for c in range(0, self.col + 1):
                    self.grid[self.row][c] = ' '
                for r in range(0, self.row):
                    self.grid[r] = [' '] * self.cols
        elif final == 'K':
            mode = nums[0] if nums else 0
            if mode == 0:
                for c in range(self.col, self.cols):
                    self.grid[self.row][c] = ' '
            elif mode == 1:
                for c in range(0, self.col + 1):
                    self.grid[self.row][c] = ' '
            else:
                self.grid[self.row] = [' '] * self.cols
        elif final == 'X':
            for c in range(self.col, min(self.cols, self.col + first)):
                self.grid[self.row][c] = ' '

    def text(self):
        lines = [''.join(row).rstrip() for row in self.grid]
        while lines and not lines[-1]:
            lines.pop()
        return '\n'.join(lines)


def run_in_pty(argv, cwd, env, timeout=10, keys=None, on_output=None):
    """Run argv attached to a pseudo terminal, type optional keys, return the final screen.

    The process is stopped when it exits or when the timeout passes. A program that is
    still alive at the timeout (a game loop, a server) is killed and reported as such.
    """
    import fcntl
    import pty
    import select
    import signal
    import struct
    import termios
    import time

    child_env = dict(env)
    child_env.update({'TERM': 'xterm-256color', 'LINES': str(TTY_ROWS), 'COLUMNS': str(TTY_COLS)})
    pid, fd = pty.fork()
    if pid == 0:
        try:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack('HHHH', TTY_ROWS, TTY_COLS, 0, 0))
            os.chdir(str(cwd))
            os.execvpe(argv[0], argv, child_env)
        except BaseException:
            pass
        os._exit(127)
    timeout = max(1.0, min(float(timeout), 60.0))
    started = time.monotonic()
    pending = list(keys or [])
    next_key = started + 1.2
    raw = b''
    status = None
    killed = False
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    screen = _Screen()

    def notify(text, reset=False):
        if not on_output or not text:
            return
        try:
            on_output(text, reset=reset)
        except TypeError:
            try:
                on_output(text)
            except Exception:
                pass
        except Exception:
            pass
    try:
        while True:
            now = time.monotonic()
            if pending and now >= next_key:
                try:
                    os.write(fd, pending.pop(0).encode('utf-8', 'replace'))
                except OSError:
                    pending = []
                next_key = now + 0.35
            if now - started > timeout:
                killed = True
                break
            ready, _w, _x = select.select([fd], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    chunk = b''
                if not chunk:
                    break
                raw += chunk
                decoded = decoder.decode(chunk, final=False)
                if decoded:
                    screen.feed(decoded)
                    notify(screen.text(), reset=True)
                if len(raw) > 2_000_000:
                    killed = True
                    break
        if killed:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(pid, sig)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        os.kill(pid, sig)
                    except OSError:
                        pass
                time.sleep(0.2)
        try:
            deadline = time.monotonic() + 3
            while status is None and time.monotonic() < deadline:
                done, status_value = os.waitpid(pid, os.WNOHANG)
                if done:
                    status = status_value
                    break
                time.sleep(0.05)
        except ChildProcessError:
            status = 0
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    if status is None:
        code = -9
    elif os.WIFEXITED(status):
        code = os.WEXITSTATUS(status)
    elif os.WIFSIGNALED(status):
        code = -os.WTERMSIG(status)
    else:
        code = -1
    tail = decoder.decode(b'', final=True)
    if tail:
        screen.feed(tail)
        notify(screen.text(), reset=True)
    final = screen.text() or '(the program drew nothing)'
    return {'returncode': code, 'killed': killed, 'screen': final, 'seconds': round(time.monotonic() - started, 1),
            'keys': len(keys or [])}


def _mac_sandbox_profile(write_roots, allow_network=False, tty=False):
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
    if tty:
        rules.append('(allow pseudo-tty)')
        rules.append('(allow file-ioctl)')
    if allow_network:
        rules.append('(allow network-outbound)')
    return '\n'.join(rules)


_NET_CACHE = {'at': 0.0, 'ok': True}
_NET_PROBES = (('pypi.org', 443), ('github.com', 443), ('1.1.1.1', 443))
INSTALL_TIMEOUT = 90


def internet_reachable(timeout=2.5):
    now = _time.monotonic()
    if _NET_CACHE['at'] and now - _NET_CACHE['at'] < 20:
        return _NET_CACHE['ok']
    ok = False
    for host, port in _NET_PROBES:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                ok = True
                break
        except OSError:
            continue
    _NET_CACHE['at'] = now
    _NET_CACHE['ok'] = ok
    return ok


def run_workspace_command(workspace, env_dir, command, timeout=120, project_root=None,
                          working_directory=None, writable_roots=None, tty=None, on_output=None):
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
    if allow_network and not re.search(r'(?i)localhost|127\.0\.0\.1|\[::1\]', raw) and not internet_reachable():
        return {'ok': False, 'returncode': -1, 'elapsed': 0.0, 'working_directory': str(cwd),
                'sandbox': 'macOS Seatbelt', 'network_allowed': False,
                'output': 'No internet connection: pypi.org, github.com and 1.1.1.1 could not be reached within a few seconds, so this command was not run. '
                          'Do not retry installs or downloads. Build the result with what is already available (the Python standard library and system tools).'}
    try:
        profile = _mac_sandbox_profile(normalized_roots, allow_network=allow_network, tty=bool(tty))
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
        'PIP_DEFAULT_TIMEOUT': '15',
        'PIP_RETRIES': '1',
        'PIP_DISABLE_PIP_VERSION_CHECK': '1',
        'PIP_NO_INPUT': '1',
        'NPM_CONFIG_FETCH_TIMEOUT': '20000',
        'NPM_CONFIG_FETCH_RETRIES': '1',
        'NPM_CONFIG_FUND': 'false',
        'NPM_CONFIG_AUDIT': 'false',
    }
    started = time.monotonic()
    if tty:
        try:
            outcome = run_in_pty([sandbox_exec, '-p', profile, shell, '-c', raw], cwd, env,
                                 timeout=tty.get('timeout', 10) if isinstance(tty, dict) else 10,
                                 keys=tty.get('keys') if isinstance(tty, dict) else None,
                                 on_output=on_output)
        except OSError as exc:
            return {'ok': False, 'returncode': -1, 'output': 'Could not start the sandboxed terminal command: ' + str(exc)}
        note = ('The program was still running after {} s and was stopped. That is normal for a game or server: judge it by the screen below.'
                .format(outcome['seconds']) if outcome['killed'] else 'The program exited with code {}.'.format(outcome['returncode']))
        text = 'Ran in a pseudo terminal ({}x{}), typed {} key(s). {}\nFinal screen:\n{}'.format(
            TTY_COLS, TTY_ROWS, outcome['keys'], note, outcome['screen'])
        crashed = (not outcome['killed']) and outcome['returncode'] != 0
        return {'ok': not crashed, 'returncode': outcome['returncode'], 'output': text[:16000],
                'elapsed': round(time.monotonic() - started, 2), 'working_directory': str(cwd),
                'sandbox': 'macOS Seatbelt', 'network_allowed': allow_network, 'tty': True}
    limit = max(1, min(int(timeout), 180))
    if allow_network:
        limit = min(limit, INSTALL_TIMEOUT)
    try:
        process = subprocess.Popen(
            [sandbox_exec, '-p', profile, shell, '-c', raw],
            cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=False, bufsize=0, shell=False, start_new_session=True,
        )
        import selectors
        selector = selectors.DefaultSelector()
        if process.stdout is not None:
            selector.register(process.stdout, selectors.EVENT_READ)
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        captured = []
        captured_length = 0
        output_truncated = False
        timed_out = False

        def deliver(chunk):
            if not chunk:
                return
            nonlocal captured_length, output_truncated
            captured.append(chunk)
            captured_length += len(chunk)
            if captured_length > 16000:
                joined = ''.join(captured)
                joined = joined[-16000:]
                captured[:] = [joined]
                captured_length = len(joined)
                output_truncated = True
            if on_output:
                try:
                    on_output(chunk, reset=False)
                except TypeError:
                    try:
                        on_output(chunk)
                    except Exception:
                        pass
                except Exception:
                    pass

        deadline = started + limit
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0 and process.poll() is None:
                    timed_out = True
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except OSError:
                        process.kill()
                    deadline = time.monotonic() + 4
                ready = selector.select(min(0.12, max(0.01, remaining)) if not timed_out else 0.12)
                for key, _mask in ready:
                    try:
                        data = os.read(key.fd, 8192)
                    except OSError:
                        data = b''
                    if not data:
                        try:
                            selector.unregister(key.fileobj)
                        except Exception:
                            pass
                        continue
                    deliver(decoder.decode(data, final=False))
                if timed_out and time.monotonic() >= deadline and selector.get_map():
                    break
            if process.poll() is None:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
        finally:
            try:
                selector.close()
            except Exception:
                pass
            try:
                if process.stdout:
                    process.stdout.close()
            except Exception:
                pass
        tail = decoder.decode(b'', final=True)
        if tail:
            deliver(tail)
        output = ''.join(captured).strip()
        returncode = process.returncode if process.returncode is not None else -1
        if timed_out:
            hint = ' The network looks too slow or unreachable: stop retrying downloads and use what is already installed.' if allow_network else ''
            output = (output + '\n' if output else '') + 'Command timed out after {} seconds and was stopped.{}'.format(limit, hint)
            deliver('\nCommand timed out after {} seconds and was stopped.{}'.format(limit, hint))
            returncode = -1
        if returncode < 0 and not output:
            try:
                signal_name = signal.Signals(-returncode).name
            except (ValueError, AttributeError):
                signal_name = 'signal {}'.format(-returncode)
            output = ('The sandboxed command was terminated by {} (signal {}). This indicates an OS sandbox/process abort, not a normal mkdir permission error.'.format(signal_name, -returncode))
            deliver(output)
        if output_truncated:
            output = '[Earlier output omitted]\n' + output
        return {
            'ok': returncode == 0 and not timed_out,
            'returncode': returncode,
            'output': output or ('Command completed successfully.' if returncode == 0 else 'Command exited without output.'),
            'elapsed': round(time.monotonic() - started, 2),
            'working_directory': str(cwd),
            'sandbox': 'macOS Seatbelt',
            'network_allowed': allow_network,
        }
    except OSError as exc:
        return {'ok': False, 'returncode': -1, 'output': 'Could not start the sandboxed command: ' + str(exc)}