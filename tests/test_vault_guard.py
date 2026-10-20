"""Vault guard: no external worker writes into the owner's notes, whatever its vendor.

Regression for the 20 Oct 2026 incident: a lead's standing brief ("Output folder: ~/Second Brain/
1_Notes/... (images in images/)") plus launchers that made the vault each worker's --dir let Codex
(danger-full-access) and Antigravity (skip-permissions) workers save ~300 MB of screenshots there.

Every case plants a throwaway "vault" (CROSSFEED_PROTECTED_ROOTS) and proves, with fake CLIs only
(no model, lease or network): each wrapper refuses a vault --dir, a vault output file and a brief
that names the vault as a write target, before it starts its CLI or creates the folder; the
Seatbelt jail blocks the write itself for workers that start anyway; and normal dispatch is unchanged.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import vault_guard  # noqa: E402

HAVE_SEATBELT = Path(vault_guard.SANDBOX_EXEC).exists()

# Every fake CLI first tries to plant a file in the vault, records whether the write went through,
# then behaves like a quiet successful worker.
FAKE_CLI = r'''#!/bin/bash
name="${0##*/}"
echo ran >"$VG_MARKS/$name.ran"
if [ -n "${VG_TARGET:-}" ]; then
  if (echo planted >"$VG_TARGET/planted-by-$name") 2>/dev/null; then echo WROTE; else echo BLOCKED; fi >"$VG_MARKS/$name.vault"
fi
case "$name" in
  codex) while [ "$#" -gt 0 ]; do [ "$1" = -o ] && { echo final >"$2"; }; shift; done; echo '{"type":"turn.completed"}';;
  *) echo final:ok;;
esac
exit 0
'''
FAKE_ROSTER = '''#!/bin/bash
case "${1:-}" in
  check|check-lane) exit 0;;
  lane-json) printf '%s\\n' '{"selector":"fake-model"}';;
  lookup) printf 'model-%s\\n' "$3";;
  *) exit 9;;
esac
'''
FAKE_FLEETCTL = '''#!/bin/bash
case "${1:-}" in
  effort) printf 'high\\t%s\\troster fake\\n' "$2";;
  route) printf '%s\\n' fake-lane;;
  acquire) printf '%s\\n' fake-lease-token;;
  release|pool-slot|model-gate) exit 0;;
  *) exit 9;;
esac
'''
# The incident's own wording, shortened.
INCIDENT_BRIEF = ("READ FIRST: (1) home `{vault}/Projects/Task Manager/Task Manager.md` (`## State`).\n"
                  "Output folder: `{vault}/Projects/Task Manager/Round 20/` (images in `images/`). "
                  "Write your named note there incrementally.\n")


class Planted(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="vault-guard-test.")
        self.root = Path(os.path.realpath(self.tmp.name))
        self.vault = self.root / "vault"
        (self.vault / "Projects").mkdir(parents=True)
        (self.vault / "Projects" / "note.md").write_text("owner's note\n")
        self.work = self.root / "work"
        self.work.mkdir()
        self.marks = self.root / "marks"
        self.marks.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("codex", "claude", "agy", "opencode", "copilot", "pi"):
            (self.bin / name).write_text(FAKE_CLI)
            (self.bin / name).chmod(0o755)
        self.env = dict(os.environ)
        for name in list(self.env):
            if name.endswith(("API_KEY", "_TOKEN")) or "OAUTH" in name:
                self.env.pop(name)
        self.env.update(
            CROSSFEED_PROTECTED_ROOTS=str(self.vault), VG_MARKS=str(self.marks), VG_TARGET=str(self.vault),
            PATH=str(self.bin) + os.pathsep + self.env.get("PATH", "/usr/bin:/bin"),
            FLEET_STATE_DIR=str(self.root / "state"), CODEX_HOME=str(self.root / "codex-home"),
            ACCESS_OVERLAY=str(ROOT / "tests" / "fixtures" / "access-overlay.test.json"))
        self.saved_roots = os.environ.get("CROSSFEED_PROTECTED_ROOTS")
        os.environ["CROSSFEED_PROTECTED_ROOTS"] = str(self.vault)   # in-process checks too
        self.before = self.snapshot()

    def tearDown(self):
        if self.saved_roots is None:
            os.environ.pop("CROSSFEED_PROTECTED_ROOTS", None)
        else:
            os.environ["CROSSFEED_PROTECTED_ROOTS"] = self.saved_roots
        self.tmp.cleanup()  # test scratch only

    def snapshot(self):
        return sorted(str(p.relative_to(self.vault)) for p in self.vault.rglob("*"))

    def assertVaultUntouched(self):
        self.assertEqual(self.snapshot(), self.before)

    def run_script(self, script, *args, env=None, timeout=60):
        return subprocess.run([str(script), *args], env=dict(self.env, **(env or {})), cwd=str(self.work),
                              capture_output=True, text=True, timeout=timeout)


class GuardRules(Planted):
    def test_folder_inside_or_containing_vault_refused_only_when_writing(self):
        self.assertTrue(vault_guard.check("agy", "write", str(self.vault / "Projects"), ""))
        self.assertTrue(vault_guard.check("codex", "write", str(self.root), ""))
        self.assertEqual(vault_guard.check("codex", "read-only", str(self.vault), ""), [])
        self.assertEqual(vault_guard.check("codex", "write", str(self.work), ""), [])

    def test_symlink_and_case_spellings_resolve_to_the_vault(self):
        link = self.work / "notes-link"
        link.symlink_to(self.vault)
        self.assertTrue(vault_guard.check("claude", "write", str(link), ""))
        self.assertTrue(vault_guard.check("claude", "write", str(self.vault).upper(), ""))

    def test_output_file_in_vault_refused(self):
        self.assertTrue(vault_guard.check("chatgpt", "read-only", None, "", [str(self.vault / "r.md")]))
        self.assertEqual(vault_guard.check("chatgpt", "read-only", None, "", [str(self.work / "r.md")]), [])

    def test_incident_brief_is_a_write_target_but_reading_is_not(self):
        hits = vault_guard.brief_targets(INCIDENT_BRIEF.format(vault=self.vault), vault_guard.protected_roots())
        self.assertEqual(len(hits), 1)
        self.assertIn("Output folder", hits[0])
        for brief in (f"Read first: {self.vault}/Projects/note.md and write your review to report.md.",
                      f"Never write into {self.vault}; deliver in this folder.",
                      f"Context home: {self.vault}/Projects/note.md (read only)."):
            with self.subTest(brief=brief):
                self.assertEqual(vault_guard.brief_targets(brief, vault_guard.protected_roots()), [])
        for brief in (f"Save screenshots into {self.vault}/Projects/images.",
                      f"Put the evidence in {self.vault}/Projects/round/",
                      f"1. Write the report to {self.vault}/Projects/report.md"):
            with self.subTest(brief=brief):
                self.assertTrue(vault_guard.brief_targets(brief, vault_guard.protected_roots()))

    def test_home_spellings_are_matched(self):
        home = vault_guard._home()
        rel = str(vault_guard.protected_roots()[0])[len(str(home)) + 1:]
        for form in (f"~/{rel}", f"$HOME/{rel}", f"{home}/{rel}"):
            with self.subTest(form=form):
                self.assertTrue(vault_guard.brief_targets(f"Output folder: `{form}/X/`", vault_guard.protected_roots()))

    def test_extra_roots_add_and_never_remove_the_vault(self):
        roots = vault_guard.protected_roots()
        self.assertEqual(roots[0], Path(os.path.realpath(vault_guard._home() / "Second Brain" / "1_Notes")))
        self.assertIn(self.vault, roots)
        os.environ["HOME"], saved = str(self.work), os.environ.get("HOME")
        try:  # a changed $HOME cannot move the protected vault
            self.assertEqual(vault_guard.protected_roots()[0], roots[0])
        finally:
            os.environ["HOME"] = saved

    def test_codex_config_granting_the_vault_is_refused(self):
        reasons = vault_guard.check("codex", "write", str(self.work), "",
                                    configs=[f'sandbox_workspace_write.writable_roots=["{self.vault}"]'])
        self.assertTrue(any("Codex config" in r for r in reasons))

    @unittest.skipUnless(HAVE_SEATBELT, "macOS Seatbelt only")
    def test_profile_blocks_every_write_route_into_the_vault(self):
        (self.work / "src.png").write_bytes(b"png")
        link = self.work / "vault-link"
        link.symlink_to(self.vault)
        script = (f'echo x >"{self.vault}/a.png"; echo "direct=$?"; '
                  f'echo x >"{link}/b.png"; echo "symlink=$?"; '
                  f'mv "{self.work}/src.png" "{self.vault}/c.png"; echo "mv=$?"; '
                  f'cp "{self.work}/src.png" "{str(self.vault).upper()}/d.png"; echo "cp_case=$?"; '
                  f'echo x >>"{self.vault}/Projects/note.md"; echo "append=$?"; '
                  f'echo x >"{self.work}/ok.txt"; echo "outside=$?"')
        result = subprocess.run([vault_guard.SANDBOX_EXEC, "-p", vault_guard.profile(), "/bin/sh", "-c", script],
                                env=self.env, capture_output=True, text=True)
        codes = dict(line.split("=") for line in result.stdout.split())
        self.assertEqual({k: v for k, v in codes.items() if k != "outside"},
                         {"direct": "1", "symlink": "1", "mv": "1", "cp_case": "1", "append": "1"})
        self.assertEqual(codes["outside"], "0")
        self.assertVaultUntouched()


class EveryWrapperRefuses(Planted):
    """A planted vault --dir, vault output file or vault-target brief never reaches the CLI."""

    def refused(self, result, wrapper):
        self.assertEqual(result.returncode, 5, f"{wrapper}: {result.stderr[-600:]}")
        self.assertIn("vault-guard: refused", result.stderr)
        self.assertEqual(list(self.marks.iterdir()), [], f"{wrapper} started its CLI")
        self.assertVaultUntouched()

    def cases(self):
        brief = INCIDENT_BRIEF.format(vault=self.vault)
        new_dir = str(self.vault / "Projects" / "Round 21")  # must not even be created
        last = str(self.vault / "Projects" / "answer.md")
        ok = ["--last", str(self.work / "answer.md")]
        return {
            "codex-agent.sh": [["--dry-run", "--prompt", "task", "--dir", new_dir],
                               ["--dry-run", "--sandbox", "danger-full-access", "--prompt", "task", "--dir", str(self.root)],
                               ["--dry-run", "--prompt", brief, "--dir", str(self.work)],
                               ["--dry-run", "--prompt", "task", "--dir", str(self.work), "--last", last]],
            "claude-agent.sh": [["--dry-run", "--prompt", "task", "--dir", str(self.vault / "Projects")],
                                ["--dry-run", "--prompt", brief, "--dir", str(self.work)],
                                ["--dry-run", "--read-only", "--prompt", "task", "--dir", str(self.work), "--last", last]],
            "agy-agent.sh": [["--lane", "fake-lane", "--prompt", "task", "--dir", new_dir],
                             ["--lane", "fake-lane", "--prompt", brief, "--dir", str(self.work)],
                             ["--lane", "fake-lane", "--prompt", "task", "--dir", str(self.work), "--last", last]],
            "opencode-agent.sh": [["--write", "--prompt", "task", "--dir", str(self.vault)],
                                  ["--prompt", brief, "--dir", str(self.work)],
                                  ["--prompt", "task", "--dir", str(self.work), "--last", last]],
            "copilot-agent.sh": [["--prompt", brief, "--dir", str(self.work)],
                                 ["--prompt", "task", "--dir", str(self.work), "--last", last]],
            "openrouter-agent.sh": [["--lane", "x", "--prompt", brief, *ok], ["--lane", "x", "--prompt", "task", "--last", last]],
            "pi-agent.sh": [["--lane", "x", "--prompt", "task", "--dir", str(self.vault), "--mode", "rw"],
                            ["--lane", "x", "--prompt", brief, "--dir", str(self.work)],
                            ["--lane", "x", "--prompt", "task", "--dir", str(self.work), "--last", last]],
            "chatgpt-agent.sh": [["--lane", "chatgpt:latest-high", "--prompt", "task", "--dir", str(self.work),
                                  "--last", last]],
        }

    def test_every_wrapper_refuses_before_starting(self):
        for wrapper, runs in self.cases().items():
            for args in runs:
                with self.subTest(wrapper=wrapper, args=[a[:40] for a in args]):
                    self.refused(self.run_script(SCRIPTS / wrapper, *args), wrapper)
        self.assertFalse((self.vault / "Projects" / "Round 21").exists())

    def test_dispatch_refuses_upfront_and_still_works_normally(self):
        brief = self.root / "brief.txt"
        fleet = [sys.executable, str(SCRIPTS / "fleetctl.py"), "dispatch", "--role", "review", "--dry-run",
                 "--prompt-file", str(brief)]
        brief.write_text(INCIDENT_BRIEF.format(vault=self.vault))
        bad = subprocess.run([*fleet, "--dir", str(self.work), "--mode", "read-only"], env=self.env,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(bad.returncode, 5, bad.stderr)
        self.assertIn("names a protected folder as a write target", bad.stderr)
        brief.write_text(f"Review the code here. Background: {self.vault}/Projects/note.md.")
        bad_dir = subprocess.run([*fleet, "--dir", str(self.vault), "--mode", "write"], env=self.env,
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(bad_dir.returncode, 5, bad_dir.stderr)
        good = subprocess.run([*fleet, "--dir", str(self.work), "--mode", "read-only"], env=self.env,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(good.returncode, 0, good.stderr[-800:])
        self.assertIn('"command_argv"', good.stdout)
        self.assertVaultUntouched()

    def test_normal_dry_runs_still_pass(self):
        for wrapper in ("codex-agent.sh", "claude-agent.sh"):
            with self.subTest(wrapper=wrapper):
                result = self.run_script(SCRIPTS / wrapper, "--dry-run", "--prompt",
                                         f"Read {self.vault}/Projects/note.md and report here.", "--dir", str(self.work))
                self.assertEqual(result.returncode, 0, result.stderr[-600:])
                self.assertIn("dry run, nothing started", result.stderr)


@unittest.skipUnless(HAVE_SEATBELT, "macOS Seatbelt only")
class WorkersThatStartCannotWrite(Planted):
    """The brief scan can miss a phrasing; the jail still blocks the write for the whole worker."""

    def fixture(self, *wrappers):
        fixture = self.root / "scripts"
        fixture.mkdir()
        for name in (*wrappers, "run-identity.sh", "run_identity.py", "vault_guard.py"):
            shutil.copy(SCRIPTS / name, fixture / name)
        for name, text in (("roster.sh", FAKE_ROSTER), ("fleetctl.py", FAKE_FLEETCTL)):
            (fixture / name).write_text(text)
            (fixture / name).chmod(0o755)
        return fixture

    def assertBlocked(self, cli, result):
        self.assertTrue((self.marks / f"{cli}.ran").exists(), f"{cli} never started: {result.stderr[-600:]}")
        self.assertEqual((self.marks / f"{cli}.vault").read_text().strip(), "BLOCKED", result.stderr[-600:])
        self.assertIn("vault-guard: this worker cannot write under", result.stderr)
        self.assertVaultUntouched()

    def test_claude_agy_copilot_workers_are_jailed(self):
        fixture = self.fixture("claude-agent.sh", "agy-agent.sh", "copilot-agent.sh")
        runs = {"claude": ["claude-agent.sh", "--prompt", "task", "--dir", str(self.work)],
                "agy": ["agy-agent.sh", "--lane", "fake-lane", "--prompt", "task", "--dir", str(self.work)],
                "copilot": ["copilot-agent.sh", "--prompt", "task", "--dir", str(self.work)]}
        for cli, (wrapper, *args) in runs.items():
            with self.subTest(cli=cli):
                self.assertBlocked(cli, self.run_script(fixture / wrapper, *args))

    def test_codex_without_its_own_sandbox_is_jailed(self):
        result = self.run_script(SCRIPTS / "codex-agent.sh", "--sandbox", "danger-full-access", "--prompt", "task",
                                 "--dir", str(self.work))
        self.assertBlocked("codex", result)

    def test_codex_in_its_own_sandbox_is_not_double_wrapped(self):
        result = self.run_script(SCRIPTS / "codex-agent.sh", "--dry-run", "--prompt", "task", "--dir", str(self.work))
        self.assertEqual(result.returncode, 0, result.stderr[-600:])
        self.assertNotIn("cannot write under", result.stderr)
        self.assertIn("-s workspace-write", result.stdout)

    def test_pi_worker_is_jailed(self):
        from tests.test_pi import PiTests  # reuse its fake Pi, roster and fleet fixture
        pi = PiTests()
        PiTests.setUp(pi)
        try:
            real_fake = pi.bin / "pi-real"
            (pi.bin / "pi").rename(real_fake)
            (pi.bin / "pi").write_text(FAKE_CLI.replace("exit 0\n", "") .replace(
                "case \"$name\" in", f'exec "{real_fake}" "$@"\ncase "$name" in'))
            (pi.bin / "pi").chmod(0o755)
            env = dict(pi.env, CROSSFEED_PROTECTED_ROOTS=str(self.vault), VG_MARKS=str(self.marks),
                       VG_TARGET=str(self.vault))
            result = subprocess.run([pi.wrapper, "--lane", "test-lane", "--prompt", "task", "--dir", str(self.work),
                                     "--mode", "rw", "--last", str(pi.last)], env=env, capture_output=True,
                                    text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr[-800:])
            self.assertEqual((self.marks / "pi.vault").read_text().strip(), "BLOCKED")
            self.assertVaultUntouched()
        finally:
            PiTests.tearDown(pi)


class EveryLaunchCarriesTheJail(unittest.TestCase):
    """Static drift check: a new launch line without the jail prefix fails here, not in the vault."""

    def test_every_cli_launch_is_prefixed(self):
        launch = re.compile(r'^\s*(?:cd "\$DIR" && exec |exec )?(?P<pre>.*?)\b(codex|claude|opencode|copilot) "\$\{args\[@\]\}"')
        for wrapper in ("codex-agent.sh", "claude-agent.sh", "opencode-agent.sh", "copilot-agent.sh"):
            lines = [line for line in (SCRIPTS / wrapper).read_text().splitlines()
                     if launch.match(line) and "printf" not in line]
            with self.subTest(wrapper=wrapper):
                self.assertTrue(lines)
                for line in lines:
                    self.assertIn('${CROSSFEED_JAIL[@]+"${CROSSFEED_JAIL[@]}"}', line)
                text = (SCRIPTS / wrapper).read_text()
                self.assertIn("\ncrossfeed_vault_guard ", text)
        agy = (SCRIPTS / "agy-agent.sh").read_text()
        self.assertIn('agy_args=( ${CROSSFEED_JAIL[@]+"${CROSSFEED_JAIL[@]}"} agy ', agy)
        self.assertLess(agy.index("\ncrossfeed_vault_guard "), agy.index('mkdir -p "$DIR"'))
        self.assertIn('[*getattr(args, "jail", []), *command]', (SCRIPTS / "pi_runner.py").read_text())


if __name__ == "__main__":
    unittest.main()
