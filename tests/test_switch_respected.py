"""A model switched off in the Crossfeed console cannot be started, on any path, and agents know it first.

The failure this guards against: a model was switched off in the console and an agent still used it. The
agent ran codex-agent.sh --model gpt-6-astra; the wrapper ran GPT-6 Sol and said so only on stderr, and the
agent's command and report said Astra. A switch that does not hold is no switch.

One test (or one small group) per path, each failing if a switched-off model can still start:
  direct wrappers    codex-agent.sh, claude-agent.sh: the stand-in runs and the LAST stderr line says so
  gated transports   agy-agent.sh --model, gemini-media.sh, gemini-image.sh: exit 5, nothing started
  leased lanes       opencode-agent.sh / openrouter-agent.sh --model: refused at the lease, nothing started
  pins               a managed seat file follows the switches (backup, undo byte for byte, owner edits
                     respected); a watched file is never written and is named in brief
  model-gate CLI     says why and what to name instead
  the hook           refuses Bash commands and subagents that would start a switched-off model; fails open
Every test uses a temporary HOME, state folder and roster copied from examples/, never the live files.
"""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
HOOK = SCRIPTS / "hooks" / "model-switch-guard.py"
# The source tree ships an example roster; an installed copy of the skill tests with its fixture instead.
EXAMPLE = next((path for path in (ROOT / "tests" / "fixtures" / "access-overlay.test.json",
                                  ROOT / "examples" / "access-overlay.example.json") if path.exists()),
               ROOT / "examples" / "access-overlay.example.json")
NOTICE = "Crossfeed: this run used gpt-6.1-sol, not gpt-6-astra (switched off in the console). Say gpt-6.1-sol when you report it."


def fixture_roster():
    """The shipped example roster, plus a priced Gemini image lane so the image transport has a switch."""
    roster = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    roster["lanes"] = [lane for lane in roster["lanes"] if lane["model_key"] != "gemini-3.8-flash"]
    for role in roster["routing"]["roles"].values():
        for band, lanes in role.items():
            if isinstance(lanes, list):
                role[band] = [lane for lane in lanes if not lane.startswith("antigravity-gemini-flash-38-")]
    if "openrouter-free" not in roster["quota_pools"]:   # an installed copy's fixture has no OpenRouter pool
        roster["quota_pools"]["openrouter-free"] = {"label": "OpenRouter", "plan": {"name": "Free models", "billing": "free"}, "quota_refresh": None}
        roster["lanes"].append({'lane_id': 'openrouter-free-router', 'model_key': 'openrouter-free-router', 'harness': 'openrouter', 'provider': 'openrouter', 'selector': 'openrouter/free', 'access_status': 'verified', 'admission_status': 'active', 'verified_at': '2026-01-01', 'quality_tier': 'standard', 'roles': [], 'capabilities': {'input': ['text'], 'output': ['text'], 'native_search': False, 'note': "OpenRouter's own router over whatever free models are live; identity varies call to call."}, 'allowed_modes': ['read-only'], 'quota_pool': 'openrouter-free', 'max_parallel': 1, 'max_tasks_per_run': 4, 'timeout_s': 180, 'retries': 0, 'notes': 'Explicit selection only. The data policy of free providers is often unpublished: send public or synthetic material only.'})
    roster["lanes"].append({
        "lane_id": "gemini-metered-image", "model_key": "gemini-3-pro-image", "quota_pool": "gemini-metered",
        "harness": "gemini-image", "provider": "google", "selector": "google/gemini-3-pro-image",
        "access_status": "verified", "admission_status": "active", "allowed_modes": ["read-only"], "roles": [],
        "capabilities": {"input": ["text", "image"]}, "max_parallel": 1, "timeout_s": 600,
    })
    return roster


def executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


class Sandbox(unittest.TestCase):
    """A temporary HOME, state folder, Codex home and roster; fake CLIs first on PATH."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.codex_home = self.home / ".codex"
        self.bin = self.root / "bin"
        for folder in (self.home, self.state, self.codex_home, self.bin):
            folder.mkdir(parents=True)
        (self.codex_home / "config.toml").write_text('model = "gpt-6-astra"\n', encoding="utf-8")
        self.overlay = self.root / "overlay.json"
        self.write_roster(fixture_roster())
        self.env = dict(os.environ, HOME=str(self.home), FLEET_STATE_DIR=str(self.state),
                        ACCESS_OVERLAY=str(self.overlay), CODEX_HOME=str(self.codex_home),
                        FLEET_NO_AUTO_REFRESH="1", FLEET_POOL_WAIT_S="0", TMPDIR=str(self.root),
                        PATH=f"{self.bin}:{os.environ['PATH']}",
                        # a call that slipped past a gate must fail fast, never reach a real provider
                        HTTPS_PROXY="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9",
                        https_proxy="http://127.0.0.1:9", http_proxy="http://127.0.0.1:9")
        for name in ("XDG_CONFIG_HOME", "XDG_STATE_HOME"):
            self.env.pop(name, None)

    def tearDown(self):
        self.tmp.cleanup()

    def write_roster(self, roster):
        self.roster = roster
        self.overlay.write_text(json.dumps(roster), encoding="utf-8")

    def fleet(self, *args, check=True):
        result = subprocess.run(["python3", str(SCRIPTS / "fleetctl.py"), *args], env=self.env,
                                capture_output=True, text=True, timeout=60)
        if check and result.returncode not in (0, 1):
            self.fail(f"fleetctl {args} exited {result.returncode}: {result.stderr}")
        return result

    def off(self, pool, model):
        self.assertEqual(self.fleet("model-toggle", pool, model, "off").stdout.strip(), "off")

    def run_script(self, name, *args, timeout=60):
        return subprocess.run(["bash", str(SCRIPTS / name), *args], env=self.env, cwd=self.root,
                              capture_output=True, text=True, timeout=timeout)

    def fake_cli(self, name, body=""):
        """A fake CLI that records its arguments to <name>.args and never talks to anyone."""
        args_file = self.root / f"{name}.args"
        executable(self.bin / name, "#!/bin/bash\n"
                   f"printf '%s\\n' \"$@\" > '{args_file}'\n" + body)
        return args_file


def last_line(text):
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


# ---- direct wrappers: the stand-in runs, and the last thing said is which model ran -------------------
FAKE_CODEX = (
    "echo 'codex banner noise' >&2\n"
    "last=''; while [ $# -gt 0 ]; do case \"$1\" in -o) last=\"$2\"; shift 2;; *) shift;; esac; done\n"
    "[ -n \"$last\" ] && echo 'final answer' > \"$last\"\n"
    "exit ${FAKE_RC:-0}\n"
)
FAKE_CLAUDE = "echo 'claude progress' >&2\necho 'final answer'\nexit ${FAKE_RC:-0}\n"


class DirectWrapperTests(Sandbox):
    def test_codex_agent_runs_the_stand_in_and_its_last_stderr_line_says_so(self):
        args_file = self.fake_cli("codex", FAKE_CODEX)
        self.off("codex", "gpt-6-astra")
        result = self.run_script("codex-agent.sh", "--model", "gpt-6-astra", "--reasoning", "high",
                                 "--prompt", "hi", "--dir", str(self.root), "--idle-timeout", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = args_file.read_text().splitlines()
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6.1-sol")
        self.assertNotIn("gpt-6-astra", argv)
        self.assertEqual(last_line(result.stderr), NOTICE)
        self.assertEqual(result.stdout, "final answer\n")
        self.assertIn("Crossfeed model receipt: requested gpt-6-astra; selected gpt-6.1-sol", result.stderr)

    def test_codex_agent_says_it_last_even_when_the_run_fails(self):
        self.fake_cli("codex", FAKE_CODEX)
        self.off("codex", "gpt-6-astra")
        self.env["FAKE_RC"] = "3"
        result = self.run_script("codex-agent.sh", "--model", "gpt-6-astra", "--prompt", "hi",
                                 "--dir", str(self.root), "--idle-timeout", "0")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(last_line(result.stderr), NOTICE)

    def test_codex_agent_with_no_model_names_the_default_it_stood_in_for(self):
        args_file = self.fake_cli("codex", FAKE_CODEX)
        self.off("codex", "gpt-6-astra")                           # the Codex config's default
        result = self.run_script("codex-agent.sh", "--prompt", "hi", "--dir", str(self.root), "--idle-timeout", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = args_file.read_text().splitlines()
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6.1-sol")
        self.assertEqual(last_line(result.stderr), NOTICE)

    def test_no_notice_when_the_model_asked_for_is_on_and_a_dry_run_says_would(self):
        self.fake_cli("codex", FAKE_CODEX)
        self.off("codex", "gpt-6-astra")
        result = self.run_script("codex-agent.sh", "--model", "gpt-6.1-sol", "--prompt", "hi",
                                 "--dir", str(self.root), "--idle-timeout", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Crossfeed:", result.stderr)
        dry = self.run_script("codex-agent.sh", "--model", "gpt-6-astra", "--prompt", "hi", "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(last_line(dry.stderr), NOTICE.replace("this run used", "this run would use"))

    def test_claude_agent_runs_the_stand_in_for_opus_and_says_so_last(self):
        args_file = self.fake_cli("claude", FAKE_CLAUDE)
        self.off("claude", "claude-opus-5-5")
        result = self.run_script("claude-agent.sh", "--prompt", "hi", "--dir", str(self.root), "--idle-timeout", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = args_file.read_text().splitlines()
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5-5")
        self.assertEqual(last_line(result.stderr), "Crossfeed: this run used claude-sonnet-5-5, not opus "
                         "(switched off in the console). Say claude-sonnet-5-5 when you report it.")
        self.assertEqual(result.stdout, "final answer\n")
        self.assertIn("Crossfeed model receipt:", result.stderr)

    def test_fanout_summary_names_the_model_that_ran(self):
        self.fake_cli("codex", FAKE_CODEX)
        self.off("codex", "gpt-6-astra")
        tasks = self.root / "tasks.jsonl"
        tasks.write_text(json.dumps({"id": "t1", "prompt": "hi", "dir": str(self.root), "agent": "codex",
                                     "mode": "read-only", "model": "gpt-6-astra"}) + "\n")
        out = self.root / "run"
        result = self.run_script("fanout.sh", str(tasks), "--out", str(out), timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        row = (out / "summary.tsv").read_text().strip().split("\t")
        self.assertEqual(row[:4], ["SUCCEEDED", "t1", "codex", "gpt-6.1-sol"])
        self.assertIn("stand-in-for=gpt-6-astra", row[4])
        self.assertIn(NOTICE, result.stderr)


# ---- transports that take a model by name: refused before anything starts --------------------------------
class GatedTransportTests(Sandbox):
    def test_agy_model_off_exits_5_and_agy_never_starts(self):
        args_file = self.fake_cli("agy", "echo answer\n")
        self.off("antigravity-gemini", "gemini-3.6-flash")
        result = self.run_script("agy-agent.sh", "--model", "gemini-3.6-flash-high", "--prompt", "hi",
                                 "--dir", str(self.root))
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertIn("gemini-3.6-flash-high is switched off", result.stderr)
        self.assertIn("Name gemini-3.1-pro-high instead", result.stderr)
        self.assertFalse(args_file.exists())

    def test_agy_model_that_is_on_passes_the_gate(self):
        args_file = self.fake_cli("agy", "echo answer\n")
        result = self.run_script("agy-agent.sh", "--model", "gemini-3.6-flash-high", "--prompt", "hi",
                                 "--dir", str(self.root), "--idle-timeout", "0", timeout=120)
        self.assertNotEqual(result.returncode, 5, result.stderr)
        self.assertTrue(args_file.exists(), result.stderr)
        argv = args_file.read_text().splitlines()
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-3.6-flash-high")

    def media_env(self):
        key = self.root / "fake-key"
        key.write_text("not-a-real-key\n")
        self.env["TRANSCRIBE_KEY_FILE"] = str(key)
        self.env["GEMINI_MEDIA_LEDGER"] = str(self.root / "media-ledger.json")
        self.env["GEMINI_IMAGE_LEDGER"] = str(self.root / "image-ledger.json")

    def test_gemini_media_model_off_exits_5_and_makes_no_call(self):
        self.media_env()
        audio = self.root / "note.m4a"
        audio.write_bytes(b"\0" * 64)
        self.off("gemini-metered", "gemini-flash-lite-latest")
        result = self.run_script("gemini-media.sh", "--file", str(audio), "--modality", "audio", "--prompt", "hi")
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertIn("gemini-flash-lite-latest is switched off", result.stderr)
        self.assertFalse((self.root / "media-ledger.json").exists())   # the spend is reserved before any call

    def test_gemini_image_model_off_exits_5_and_makes_no_call(self):
        self.media_env()
        self.off("gemini-metered", "gemini-3-pro-image")
        out = self.root / "image.png"
        result = self.run_script("gemini-image.sh", "--prompt", "a beaver", "--out", str(out))
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertIn("gemini-3-pro-image is switched off", result.stderr)
        self.assertFalse((self.root / "image-ledger.json").exists())
        self.assertFalse(out.exists())

    def test_opencode_agent_model_off_is_refused_and_opencode_never_starts(self):
        args_file = self.fake_cli("opencode", "exit 0\n")
        auth = self.home / ".local" / "share" / "opencode" / "auth.json"
        auth.parent.mkdir(parents=True)
        auth.write_text("{}")                                       # a stand-in, never a real sign-in
        self.off("opencode-go", "kimi-k3")
        result = self.run_script("opencode-agent.sh", "--model", "opencode-go/kimi-k3", "--direct",
                                 "--prompt", "hi", "--dir", str(self.root))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("model kimi-k3 is switched off", result.stderr)
        self.assertFalse(args_file.exists())

    def test_openrouter_agent_model_off_is_refused_before_any_call(self):
        key = self.root / "openrouter-key"
        key.write_text("not-a-real-key\n")
        self.env["OPENROUTER_KEY_FILE"] = str(key)
        self.off("openrouter-free", "openrouter-free-router")
        result = self.run_script("openrouter-agent.sh", "--model", "openrouter/free", "--prompt", "hi")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("switched off", result.stderr)


# ---- the gate itself ---------------------------------------------------------------------------
class ModelGateCliTests(Sandbox):
    def test_model_gate_says_why_and_what_to_name_instead(self):
        self.assertEqual(self.fleet("model-gate", "gemini-3.6-flash-high", "--harness", "agy").returncode, 0)
        self.off("antigravity-gemini", "gemini-3.6-flash")
        refused = self.fleet("model-gate", "gemini-3.6-flash-high", "--harness", "agy", check=False)
        self.assertEqual(refused.returncode, 5)
        self.assertIn("Name gemini-3.1-pro-high instead", refused.stderr)
        self.assertIn("Only the owner can switch it back on", refused.stderr)
        self.assertNotIn("model-toggle", refused.stderr)           # no command an agent could run to undo it
        bare = self.fleet("model-gate", "gemini-3.6-flash-high", "--harness", "agy", "--reason-only", check=False)
        self.assertEqual(bare.returncode, 5)
        self.assertNotIn("instead", bare.stderr)
        self.assertEqual(self.fleet("model-gate", "not-a-listed-model").returncode, 0)

    def test_model_gate_for_a_direct_pool_and_a_provider_set_to_off(self):
        self.off("codex", "gpt-6-astra")
        refused = self.fleet("model-gate", "gpt-6-astra", "--pool", "codex", check=False)
        self.assertEqual(refused.returncode, 5)
        self.assertIn("Name gpt-6.1-sol instead, the model that runs in its place", refused.stderr)
        self.assertEqual(self.fleet("model-gate", "gpt-6.1-sol", "--pool", "codex").returncode, 0)
        self.fleet("level", "antigravity-3p", "off")
        pool_off = self.fleet("model-gate", "claude-sonnet-4-6", "--harness", "agy", check=False)
        self.assertEqual(pool_off.returncode, 5)
        self.assertIn("is set to Off", pool_off.stderr)

    def test_model_gate_refuses_a_retired_model(self):
        roster = fixture_roster()
        roster["model_cards"]["gemini-3.6-flash"] = {"retired_on": "2026-09-01"}
        self.write_roster(roster)
        refused = self.fleet("model-gate", "gemini-3.6-flash-high", "--harness", "agy", check=False)
        self.assertEqual(refused.returncode, 5)
        self.assertIn("retired by its provider on 2026-09-01", refused.stderr)

    def test_model_run_explain_names_what_ran_for_what_and_why(self):
        self.off("codex", "gpt-6-astra")
        self.assertEqual(self.fleet("model-run", "codex", "gpt-6-astra", "--explain").stdout.strip(),
                         "gpt-6.1-sol gpt-6-astra off")
        self.assertEqual(self.fleet("model-run", "codex", "gpt-6.1-sol", "--explain").stdout, "")


# ---- pins: model names written in files Crossfeed does not own -----------------------------------------------
BUILDER = ('name = "builder"\r\nmodel = "gpt-6-astra"\r\nmodel_reasoning_effort = "high"\r\n'
           '[mcp_servers.docs]\r\nmodel = "gpt-6-astra"\r\n')
OPENCODE = ('{\n  // "model": "opencode-go/kimi-k3" was tried here\n  "$schema": "https://opencode.ai/config.json",\n'
            '  "model": "opencode-go/kimi-k3",\n  "small_model": "opencode-go/kimi-k3",\n'
            '  "agent": {"plan": {"model": "opencode-go/kimi-k3"}}\n}\n')


class PinsTests(Sandbox):
    def setUp(self):
        super().setUp()
        agents = self.codex_home / "agents"
        agents.mkdir()
        self.builder = agents / "builder.toml"
        self.builder.write_bytes(BUILDER.encode())
        self.explorer = agents / "explorer.toml"
        self.explorer.write_text('model = "gpt-6-luna"\n')
        self.config = self.codex_home / "config.toml"
        opencode = self.home / ".config" / "opencode"
        opencode.mkdir(parents=True)
        self.opencode = opencode / "opencode.jsonc"
        self.opencode.write_text(OPENCODE)
        roster = fixture_roster()
        roster["quota_pools"]["codex"]["model_pins"] = [
            {"path": "~/.codex/agents/*.toml", "key": "model", "format": "toml", "manage": True, "what": "seats"},
            {"path": "~/.codex/config.toml", "key": "model", "format": "toml", "manage": False, "what": "app"}]
        roster["quota_pools"]["opencode-go"]["model_pins"] = [
            {"path": "~/.config/opencode/opencode.jsonc", "key": key, "format": "json", "manage": True}
            for key in ("model", "small_model")]
        self.write_roster(roster)

    def test_a_managed_seat_moves_with_the_switch_and_undo_puts_it_back_byte_for_byte(self):
        self.off("codex", "gpt-6-astra")                            # model-toggle syncs the pins itself
        text = self.builder.read_bytes().decode()
        self.assertIn('model = "gpt-6.1-sol"\r\n', text)
        self.assertIn('[mcp_servers.docs]\r\nmodel = "gpt-6-astra"', text)   # a table's own key is not the seat's
        backups = list((self.state / "pin-backups").iterdir())
        self.assertEqual([b.read_bytes() for b in backups if "builder" in b.name], [BUILDER.encode()])
        self.assertEqual(self.explorer.read_text(), 'model = "gpt-6-luna"\n')
        self.fleet("pins", "undo")
        self.assertEqual(self.builder.read_bytes(), BUILDER.encode())
        self.fleet("pins", "sync")
        self.fleet("model-toggle", "codex", "gpt-6-astra", "on")    # switching it on gives the seat its own model back
        self.assertEqual(self.builder.read_bytes(), BUILDER.encode())

    def test_an_owner_edit_after_a_rewrite_is_respected(self):
        self.off("codex", "gpt-6-astra")
        self.builder.write_bytes(self.builder.read_bytes().replace(b'"gpt-6.1-sol"', b'"gpt-6-luna"', 1))
        rows = json.loads(self.fleet("pins", "sync", "--json").stdout)
        row = next(r for r in rows if r["file"] == "builder.toml")
        self.assertEqual((row["action"], row["value"], row["original"]), ("ok", "gpt-6-luna", None))
        self.fleet("model-toggle", "codex", "gpt-6-astra", "on")
        self.fleet("pins", "undo")
        self.assertIn(b'model = "gpt-6-luna"', self.builder.read_bytes())

    def test_a_watched_file_is_never_written_and_brief_names_it(self):
        before = self.config.read_bytes()
        self.off("codex", "gpt-6-astra")
        sync = self.fleet("pins", "sync")
        self.assertEqual(sync.returncode, 1)                        # a file still names a model that is off
        self.assertIn("watched only, not written", sync.stdout)
        self.assertEqual(self.config.read_bytes(), before)
        brief = self.fleet("brief", "--verbose", "--no-refresh").stdout
        self.assertIn("Switched off:", brief)
        self.assertIn("gpt-6-astra (gpt-6.1-sol runs instead)", brief)
        self.assertIn("~/.codex/config.toml still names gpt-6-astra", brief)

    def test_json_pins_are_read_at_the_top_level_only_and_both_keys_move(self):
        self.off("opencode-go", "kimi-k3")
        text = self.opencode.read_text()
        self.assertIn('// "model": "opencode-go/kimi-k3" was tried here', text)   # a comment is not a pin
        self.assertIn('"agent": {"plan": {"model": "opencode-go/kimi-k3"}}', text)  # nor is a nested block
        self.assertNotIn('  "model": "opencode-go/kimi-k3",', text)
        self.assertNotIn('"small_model": "opencode-go/kimi-k3"', text)
        backups = [b for b in (self.state / "pin-backups").iterdir() if "opencode" in b.name]
        self.assertEqual([b.read_text() for b in backups], [OPENCODE])   # one backup, the file as it was
        self.fleet("pins", "undo")
        self.assertEqual(self.opencode.read_text(), OPENCODE)

    def test_pins_status_writes_nothing(self):
        self.off("codex", "gpt-6-astra")
        self.fleet("pins", "undo")
        fresh = self.root / "fresh-state"
        env = dict(self.env, FLEET_STATE_DIR=str(fresh))
        before = self.builder.read_bytes()
        status = subprocess.run(["python3", str(SCRIPTS / "fleetctl.py"), "pins", "status"], env=env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(status.returncode, 0, status.stderr)    # a fresh state folder has every model on
        self.assertFalse(fresh.exists())
        self.assertEqual(self.builder.read_bytes(), before)


# ---- the hook: agents learn before they run anything --------------------------------------------------------
class HookTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.off("codex", "gpt-6-astra")
        self.off("claude", "claude-opus-5-5")

    def hook(self, payload, env=None):
        started = time.monotonic()
        result = subprocess.run(["python3", str(HOOK)], input=json.dumps(payload) if not isinstance(payload, str) else payload,
                                env=env or self.env, capture_output=True, text=True, timeout=30)
        self.elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        if not result.stdout.strip():
            return None
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual((output["hookEventName"], output["permissionDecision"]), ("PreToolUse", "deny"))
        return output["permissionDecisionReason"]

    def bash(self, command):
        return self.hook({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(self.root)})

    def test_a_wrapper_naming_a_switched_off_model_is_refused_with_what_to_write(self):
        reason = self.bash('~/orchestrator/scripts/codex-agent.sh --model gpt-6-astra --reasoning high --prompt "x"')
        self.assertIn("Write --model gpt-6.1-sol", reason)
        self.assertIn("gpt-6-astra is switched off", reason)
        self.assertIn("Write --model gpt-6.1-sol", self.bash("cd /tmp && timeout 900 bash codex-agent.sh --prompt x"))
        self.assertIn("Write --model claude-sonnet-5-5", self.bash("claude-agent.sh --prompt x"))
        self.assertIsNone(self.bash("codex-agent.sh --model gpt-6.1-sol --prompt x"))

    def test_raw_clis_are_refused_too(self):
        self.assertIn("Write -m gpt-6.1-sol", self.bash('codex exec -m gpt-6-astra "hi"'))
        self.assertIn("codex exec with no -m runs gpt-6-astra", self.bash('codex exec -s read-only "hi"'))
        self.assertIn("Write --model claude-sonnet-5-5", self.bash('claude -p --model opus "hi"'))
        self.assertIn("Write -m gpt-6.1-sol", self.bash("bash -c 'codex exec -m gpt-6-astra hi'"))
        self.assertIsNone(self.bash('codex exec -m gpt-6.1-sol "hi"'))
        self.assertIsNone(self.bash("codex --version"))

    def test_gated_transports_and_lanes(self):
        self.off("antigravity-gemini", "gemini-3.6-flash")
        self.assertIn("Name gemini-3.1-pro-high instead", self.bash("agy-agent.sh --model gemini-3.6-flash-high --prompt x"))
        self.assertIn("gemini-3.6-flash", self.bash("agy-agent.sh --lane antigravity-gemini-flash --prompt x"))
        self.off("opencode-go", "kimi-k3")
        self.assertIn("kimi-k3", self.bash("opencode-agent.sh --model-key kimi-k3 --prompt x"))
        self.assertIn("kimi-k3", self.bash("opencode run -m opencode-go/kimi-k3 hi"))

    def test_a_fanout_tasks_file_naming_a_switched_off_model_is_refused(self):
        tasks = self.root / "tasks.jsonl"
        tasks.write_text(json.dumps({"id": "unit-1", "prompt": "x", "agent": "codex", "model": "gpt-6-astra"}) + "\n"
                         + json.dumps({"id": "unit-2", "prompt": "x", "agent": "codex", "model": "gpt-6.1-sol"}) + "\n")
        reason = self.bash("scripts/fanout.sh tasks.jsonl --parallel 2")
        self.assertIn("unit-1", reason)
        self.assertNotIn("unit-2", reason)

    def test_an_agent_may_not_switch_a_model_back_on(self):
        self.assertIn("switches are the owner's", self.bash("python3 scripts/fleetctl.py model-toggle codex gpt-6-astra on"))
        self.assertIn("switches are the owner's", self.bash("fleetctl.py level codex normal"))
        self.assertIsNone(self.bash("fleetctl.py model-toggle codex gpt-6.1-sol off"))    # off is always fine
        self.assertIsNone(self.bash("FLEET_STATE_DIR=/tmp/fixture fleetctl.py model-toggle codex gpt-6-astra on"))

    def test_text_that_only_mentions_a_command_is_not_refused(self):
        self.assertIsNone(self.bash('git commit -m "codex-agent.sh --model gpt-6-astra now stands in"'))
        self.assertIsNone(self.bash("cat > run.sh <<'EOF'\ncodex exec -m gpt-6-astra hi\nEOF"))
        self.assertIsNone(self.bash("echo 'claude --model opus'"))
        self.assertIsNone(self.bash("ls -la"))
        self.assertLess(self.elapsed, 2.0)

    def test_a_subagent_model_that_is_off_is_refused(self):
        reason = self.hook({"tool_name": "Agent", "tool_input": {"description": "x", "prompt": "y", "model": "opus"}})
        self.assertIn('Use model "sonnet" instead', reason)
        self.assertIsNone(self.hook({"tool_name": "Agent", "tool_input": {"description": "x", "prompt": "y", "model": "sonnet"}}))
        self.assertIsNone(self.hook({"tool_name": "Agent", "tool_input": {"description": "x", "prompt": "y"}}))
        self.assertIsNotNone(self.hook({"tool_name": "Task", "tool_input": {"prompt": "y", "model": "opus"}}))

    def test_it_fails_open(self):
        self.overlay.write_text("{ not json")
        self.assertIsNone(self.bash("codex-agent.sh --model gpt-6-astra --prompt x"))
        self.assertIsNone(self.hook("this is not json"))
        self.assertIsNone(self.hook(""))
        missing = dict(self.env, ACCESS_OVERLAY=str(self.root / "missing.json"))
        self.assertIsNone(self.hook({"tool_name": "Agent", "tool_input": {"model": "opus"}}, env=missing))


if __name__ == "__main__":
    unittest.main()
