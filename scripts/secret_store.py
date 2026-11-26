"""Portable storage for the few secrets Crossfeed reads on its own (no 1Password required).

Reading looks in this order and stops at the first hit:
  1. an explicit environment variable (OPENROUTER_MANAGEMENT_KEY for `openrouter-management`)
  2. a 1Password `op://` reference registered for the name, when `op` is installed
  3. the macOS Keychain (`security`)
  4. the Linux Secret Service (`secret-tool`)
  5. a mode-600 file, only when it was written with the explicit plaintext flag

Writing goes to the first writable secure backend (Keychain, then Secret Service). A plaintext
file is written only when the caller passes ``allow_file=True``. The value never appears in an
argument list, a log line, a return value of `status`, or an error message.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

SERVICE = "crossfeed-orchestrator"
# Known names only: a name is also an env var, a keychain account and a file name.
KNOWN = {
    "openrouter-management": {
        "env": "OPENROUTER_MANAGEMENT_KEY",
        "label": "OpenRouter management key",
        "hint": "Create it at openrouter.ai/settings/management-keys with Activity read access.",
    },
}
VALUE = re.compile(r"[A-Za-z0-9._~+/=:-]{8,512}\Z")
OP_REF = re.compile(r"op://[A-Za-z0-9_.-]{1,64}/[^/\r\n\0\"'\\]{1,200}/[^/\r\n\0\"'\\]{1,200}\Z")
Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


class SecretError(ValueError):
    """A message that is safe to show: it never contains a secret."""


def config_dir(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("ORCHESTRATOR_CONFIG_DIR") or Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "orchestrator")


def _check_name(name: str) -> dict[str, str]:
    if name not in KNOWN:
        raise SecretError(f"Unknown secret {name!r}; known: {', '.join(sorted(KNOWN))}")
    return KNOWN[name]


def _run(argv: list[str], *, stdin: bytes | None = None, timeout: float = 10, env: dict[str, str] | None = None) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.run(argv, input=stdin, capture_output=True, timeout=timeout, env=env)


def _owner_private(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".secret-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


class Store:
    """Backends are injectable so tests never touch a real keychain, vault or file."""

    def __init__(self, *, env: dict[str, str] | None = None, runner: Runner | None = None,
                 platform: str | None = None, which: Callable[[str], str | None] = shutil.which,
                 directory: Path | None = None, op_env: Callable[[], dict[str, str] | None] | None = None,
                 service: str = SERVICE) -> None:
        self.service = service
        self.env = os.environ if env is None else env
        self.run = runner or _run
        self.platform = platform or sys.platform
        self.which = which
        self.dir = directory or config_dir(self.env)
        self.op_env = op_env or _headless_op_env

    # -- references (not secrets) ---------------------------------------------------------------
    @property
    def refs_path(self) -> Path:
        return self.dir / "secrets.json"

    def _refs(self) -> dict[str, Any]:
        try:
            if not _owner_private(self.refs_path):
                return {}
            data = json.loads(self.refs_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def set_reference(self, name: str, ref: str) -> None:
        _check_name(name)
        if not OP_REF.fullmatch(ref.strip()):
            raise SecretError("Use a 1Password reference like op://vault/item/field.")
        refs = self._refs()
        refs[name] = {"op": ref.strip()}
        _atomic_write(self.refs_path, json.dumps(refs, indent=2, sort_keys=True).encode())

    # -- backend probes -------------------------------------------------------------------------
    def keychain_available(self) -> bool:
        return self.platform == "darwin" and bool(self.which("security"))

    def secret_service_available(self) -> bool:
        return self.platform.startswith("linux") and bool(self.which("secret-tool"))

    def file_path(self, name: str) -> Path:
        return self.dir / "secrets" / name

    def available(self) -> list[str]:
        found = []
        if self.which("op"):
            found.append("1password")
        if self.keychain_available():
            found.append("keychain")
        if self.secret_service_available():
            found.append("secret-service")
        return found

    # -- read -----------------------------------------------------------------------------------
    def _read_op(self, ref: str) -> str | None:
        binary = self.which("op")
        if not binary or not OP_REF.fullmatch(ref):
            return None
        try:
            done = self.run([binary, "read", "--no-newline", ref], timeout=10, env=self.op_env())
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.decode("utf-8", "replace") if done.returncode == 0 else None

    def _read_keychain(self, name: str) -> str | None:
        if not self.keychain_available():
            return None
        try:
            done = self.run([self.which("security") or "security", "find-generic-password", "-s", self.service, "-a", name, "-w"])
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.decode("utf-8", "replace") if done.returncode == 0 else None

    def _read_secret_service(self, name: str) -> str | None:
        if not self.secret_service_available():
            return None
        try:
            done = self.run([self.which("secret-tool") or "secret-tool", "lookup", "service", self.service, "account", name])
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.decode("utf-8", "replace") if done.returncode == 0 else None

    def _read_file(self, name: str) -> str | None:
        path = self.file_path(name)
        if not _owner_private(path):
            return None
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None

    def resolve(self, name: str) -> tuple[str | None, str]:
        """Return (value, source). The value stays with the caller; `status` is the safe view."""
        spec = _check_name(name)
        candidates: list[tuple[str, Callable[[], str | None]]] = [
            ("env", lambda: self.env.get(spec["env"])),
            ("1password", lambda: self._read_op(str(_mapping(self._refs().get(name)).get("op", "")))),
            ("keychain", lambda: self._read_keychain(name)),
            ("secret-service", lambda: self._read_secret_service(name)),
            ("file", lambda: self._read_file(name)),
        ]
        for source, read in candidates:
            value = (read() or "").strip()
            if value and VALUE.fullmatch(value):
                return value, source
        return None, "none"

    def status(self, name: str) -> dict[str, Any]:
        """Where the value comes from. Never the value, never part of it."""
        spec = _check_name(name)
        value, source = self.resolve(name)
        return {"name": name, "label": spec["label"], "present": value is not None, "source": source,
                "writable": self.writable_backend(), "env": spec["env"],
                "hint": spec["hint"]}

    # -- write ----------------------------------------------------------------------------------
    def writable_backend(self) -> str | None:
        if self.keychain_available():
            return "keychain"
        if self.secret_service_available():
            return "secret-service"
        return None

    def put(self, name: str, value: str, *, allow_file: bool = False) -> str:
        _check_name(name)
        value = value.strip()
        if not VALUE.fullmatch(value):
            raise SecretError("That does not look like a key: use 8-512 letters, digits and . _ ~ + / = : -")
        backend = self.writable_backend()
        if backend == "keychain":
            self._write_keychain(name, value)
        elif backend == "secret-service":
            self._write_secret_service(name, value)
        elif allow_file:
            # Explicit opt-in only: mode 600, owner-only directory, readable by anything running as you.
            self.file_path(name).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _atomic_write(self.file_path(name), (value + "\n").encode())
            backend = "file"
        else:
            raise SecretError("No secure store on this machine (needs macOS Keychain or Linux secret-tool). "
                              "Set the environment variable, register a 1Password reference, or pass the "
                              "explicit plaintext option to store a mode-600 file.")
        return backend

    def _write_keychain(self, name: str, value: str) -> None:
        # `security -i` reads commands from stdin, so the key never appears in the process list.
        # VALUE's alphabet needs no quoting.
        command = f"add-generic-password -U -s {self.service} -a {name} -w {value}\n".encode()
        try:
            done = self.run([self.which("security") or "security", "-i"], stdin=command, timeout=15)
        except (OSError, subprocess.SubprocessError):
            raise SecretError("The macOS Keychain could not be written.") from None
        if done.returncode != 0 or b"error" in (done.stderr or b"").lower():
            raise SecretError("The macOS Keychain refused the write.")

    def _write_secret_service(self, name: str, value: str) -> None:
        argv = [self.which("secret-tool") or "secret-tool", "store", f"--label={self.service} {name}",
                "service", self.service, "account", name]
        try:
            done = self.run(argv, stdin=value.encode(), timeout=15)
        except (OSError, subprocess.SubprocessError):
            raise SecretError("The Secret Service could not be written.") from None
        if done.returncode != 0:
            raise SecretError("The Secret Service refused the write.")

    def delete(self, name: str) -> list[str]:
        """Remove every copy this tool wrote; a 1Password item or env var is left alone."""
        _check_name(name)
        removed = []
        if self.keychain_available():
            done = self.run([self.which("security") or "security", "delete-generic-password", "-s", self.service, "-a", name])
            if done.returncode == 0:
                removed.append("keychain")
        if self.secret_service_available():
            done = self.run([self.which("secret-tool") or "secret-tool", "clear", "service", self.service, "account", name])
            if done.returncode == 0:
                removed.append("secret-service")
        path = self.file_path(name)
        if path.exists():
            path.unlink()
            removed.append("file")
        refs = self._refs()
        if refs.pop(name, None) is not None:
            _atomic_write(self.refs_path, json.dumps(refs, indent=2, sort_keys=True).encode())
            removed.append("reference")
        return removed


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _headless_op_env() -> dict[str, str] | None:
    """Reuse providers.py's service-account handling so readers from launchd never prompt."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("crossfeed_providers", Path(__file__).with_name("providers.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module._headless_op().get("env")
    except Exception:
        return None


def resolve(name: str, store: Store | None = None) -> str | None:
    """The value for readers; None when unset. Callers must not log or echo it."""
    return (store or Store()).resolve(name)[0]


def run(action: str, name: str, *, ref: str = "", plaintext: bool = False, store: Store | None = None,
        stdin=None, out=None) -> int:
    """`fleetctl secret set|status|delete NAME`. The value is read from stdin and never echoed."""
    import getpass

    out = out or sys.stdout
    stdin = stdin or sys.stdin
    store = store or Store()
    try:
        if action == "status":
            info = store.status(name)
            print(json.dumps({key: info[key] for key in ("name", "present", "source", "writable", "env")}), file=out)
            return 0 if info["present"] else 1
        if action == "delete":
            print(json.dumps({"name": name, "removed": store.delete(name)}), file=out)
            return 0
        if ref:
            store.set_reference(name, ref)
            print(json.dumps({"name": name, "stored_in": "reference"}), file=out)
            return 0
        if stdin.isatty():
            value = getpass.getpass(f"{_check_name(name)['label']} (hidden): ")
        else:
            value = stdin.readline()
        where = store.put(name, value, allow_file=plaintext)
        print(json.dumps({"name": name, "stored_in": where}), file=out)
        if where == "file":
            print("warning: stored as plaintext in a mode-600 file; any process running as you can read it.", file=sys.stderr)
        return 0
    except SecretError as error:
        print(f"secret: {error}", file=sys.stderr)
        return 2


def main(argv: list[str], **kwargs: Any) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="fleetctl secret")
    parser.add_argument("action", choices=("set", "status", "delete"))
    parser.add_argument("name", nargs="?", default="openrouter-management")
    parser.add_argument("--ref", default="", help="register a 1Password op:// reference instead of storing a value")
    parser.add_argument("--plaintext", action="store_true",
                        help="allow a mode-600 file when no Keychain or Secret Service exists")
    args = parser.parse_args(argv)
    return run(args.action, args.name, ref=args.ref, plaintext=args.plaintext, **kwargs)
