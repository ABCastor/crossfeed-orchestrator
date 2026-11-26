"""secret_store: backend order and the rule that a value never reaches an argv, a status or an error.

Every backend is a fake runner; nothing here touches a real keychain, vault or network."""
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import secret_store as ss

KEY = "sk-or-v1-0123456789abcdef"
OTHER = "sk-or-v1-fedcba9876543210"
REF = "op://4kr2jxlokteecriaokqrdsnixm/OpenRouter Management Key - Crossfeed/credential"


class FakeSystem:
    """Records every command; plays 1Password, the Keychain and secret-tool."""

    def __init__(self, op=None, keychain=None, service=None):  # None = tool not installed
        self.op = op
        self.keychain = None if keychain is None else dict(keychain)
        self.service = None if service is None else dict(service)
        self.calls = []

    def which(self, name):
        present = {"op": self.op is not None, "security": self.keychain is not None,
                   "secret-tool": self.service is not None}
        return f"/fake/{name}" if present.get(name) else None

    def __call__(self, argv, *, stdin=None, timeout=10, env=None):
        self.calls.append((list(argv), stdin))
        tool = argv[0].rsplit("/", 1)[-1]
        done = lambda code=0, out=b"": subprocess.CompletedProcess(argv, code, out, b"")
        if tool == "op":
            return done(0, self.op.encode()) if argv[2:3] == ["--no-newline"] and argv[3] == REF and self.op else done(1)
        if tool == "security" and argv[1] == "find-generic-password":
            value = self.keychain.get(argv[argv.index("-a") + 1])
            return done(0, (value + "\n").encode()) if value else done(44)
        if tool == "security" and argv[1] == "-i":
            parts = stdin.decode().split()
            self.keychain[parts[parts.index("-a") + 1]] = parts[parts.index("-w") + 1]
            return done()
        if tool == "security" and argv[1] == "delete-generic-password":
            return done(0) if self.keychain.pop(argv[argv.index("-a") + 1], None) else done(44)
        if tool == "secret-tool" and argv[1] == "lookup":
            value = self.service.get(argv[-1])
            return done(0, value.encode()) if value else done(1)
        if tool == "secret-tool" and argv[1] == "store":
            self.service[argv[-1]] = stdin.decode()
            return done()
        if tool == "secret-tool" and argv[1] == "clear":
            return done(0) if self.service.pop(argv[-1], None) else done(1)
        raise AssertionError(argv)


class SecretStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def store(self, system, env=None, platform="darwin"):
        return ss.Store(env=env or {}, runner=system, platform=platform, which=system.which,
                        directory=self.dir, op_env=lambda: None)

    def test_order_env_then_op_then_keychain_then_service_then_file(self):
        system = FakeSystem(op=OTHER, keychain={"openrouter-management": "keychain-value-1"})
        store = self.store(system, env={"OPENROUTER_MANAGEMENT_KEY": KEY})
        store.set_reference("openrouter-management", REF)
        self.assertEqual(store.resolve("openrouter-management"), (KEY, "env"))
        store.env = {}
        self.assertEqual(store.resolve("openrouter-management"), (OTHER, "1password"))
        system.op = None
        self.assertEqual(store.resolve("openrouter-management"), ("keychain-value-1", "keychain"))
        system.keychain.clear()
        linux = self.store(FakeSystem(keychain=None, service={"openrouter-management": "service-value-1"}), platform="linux")
        self.assertEqual(linux.resolve("openrouter-management"), ("service-value-1", "secret-service"))
        bare = self.store(FakeSystem(keychain=None), platform="linux")
        self.assertEqual(bare.resolve("openrouter-management"), (None, "none"))
        bare.put("openrouter-management", KEY, allow_file=True)
        self.assertEqual(bare.resolve("openrouter-management"), (KEY, "file"))

    def test_put_prefers_keychain_and_never_puts_the_value_in_argv(self):
        system = FakeSystem(keychain={})
        where = self.store(system).put("openrouter-management", KEY)
        self.assertEqual(where, "keychain")
        self.assertEqual(system.keychain["openrouter-management"], KEY)
        for argv, _stdin in system.calls:
            self.assertFalse(any(KEY in part for part in argv), argv)
        self.assertFalse(any(self.dir.rglob("*")), "nothing is written to disk when a keychain exists")

    def test_put_uses_secret_tool_stdin_on_linux(self):
        system = FakeSystem(keychain=None, service={})
        where = self.store(system, platform="linux").put("openrouter-management", KEY)
        self.assertEqual(where, "secret-service")
        argv, stdin = system.calls[-1]
        self.assertEqual(stdin, KEY.encode())
        self.assertFalse(any(KEY in part for part in argv))

    def test_no_secure_store_refuses_unless_plaintext_is_explicit(self):
        system = FakeSystem(keychain=None)
        store = self.store(system, platform="linux")
        with self.assertRaises(ss.SecretError) as caught:
            store.put("openrouter-management", KEY)
        self.assertNotIn(KEY, str(caught.exception))
        self.assertFalse((self.dir / "secrets").exists())
        self.assertEqual(store.put("openrouter-management", KEY, allow_file=True), "file")
        path = store.file_path("openrouter-management")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        path.chmod(0o644)  # a file anyone can read is not trusted
        self.assertEqual(store.resolve("openrouter-management"), (None, "none"))

    def test_status_never_contains_the_value(self):
        system = FakeSystem(keychain={"openrouter-management": KEY})
        info = self.store(system).status("openrouter-management")
        self.assertEqual((info["present"], info["source"]), (True, "keychain"))
        self.assertNotIn(KEY, json.dumps(info))
        absent = self.store(FakeSystem(keychain={})).status("openrouter-management")
        self.assertEqual((absent["present"], absent["source"]), (False, "none"))

    def test_rejects_unknown_names_malformed_values_and_references(self):
        store = self.store(FakeSystem(keychain={}))
        with self.assertRaises(ss.SecretError):
            store.put("something-else", KEY)
        for bad in ("short", "has space in it 12345", "quote\"inject-12345", "new\nline-12345678", "x" * 600):
            with self.assertRaises(ss.SecretError) as caught:
                store.put("openrouter-management", bad)
            self.assertNotIn(bad, str(caught.exception))
        with self.assertRaises(ss.SecretError):
            store.set_reference("openrouter-management", "https://example.com/key")
        self.assertEqual(store.resolve("openrouter-management"), (None, "none"))

    def test_malformed_backend_output_is_ignored(self):
        system = FakeSystem(keychain={"openrouter-management": "has space"})
        self.assertEqual(self.store(system).resolve("openrouter-management"), (None, "none"))

    def test_reference_file_is_private_and_holds_no_value(self):
        store = self.store(FakeSystem(op=KEY))
        store.set_reference("openrouter-management", REF)
        raw = store.refs_path.read_text()
        self.assertIn(REF, raw)
        self.assertNotIn(KEY, raw)
        self.assertEqual(stat.S_IMODE(store.refs_path.stat().st_mode), 0o600)
        self.assertEqual(store.resolve("openrouter-management"), (KEY, "1password"))

    def test_delete_removes_what_this_tool_wrote(self):
        system = FakeSystem(keychain={"openrouter-management": KEY})
        store = self.store(system)
        store.set_reference("openrouter-management", REF)
        self.assertEqual(sorted(store.delete("openrouter-management")), ["keychain", "reference"])
        self.assertEqual(store.resolve("openrouter-management"), (None, "none"))

    def test_cli_reads_stdin_and_prints_only_where(self):
        system = FakeSystem(keychain={})
        store = self.store(system)
        out = io.StringIO()
        code = ss.main(["set", "openrouter-management"], store=store, stdin=io.StringIO(KEY + "\n"), out=out)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue()), {"name": "openrouter-management", "stored_in": "keychain"})
        self.assertNotIn(KEY, out.getvalue())
        out = io.StringIO()
        self.assertEqual(ss.main(["status", "openrouter-management"], store=store, out=out), 0)
        self.assertNotIn(KEY, out.getvalue())
        self.assertEqual(ss.main(["set", "openrouter-management"], store=store, stdin=io.StringIO("bad\n"), out=io.StringIO()), 2)


if __name__ == "__main__":
    unittest.main()
