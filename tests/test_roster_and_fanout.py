import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

# These are subprocess-level tests that pin a quota band by crafting a snapshot in
# a temp state dir, so the live CodexBar auto-refresh must stay out: otherwise a
# crafted UNKNOWN pool heals itself into the real band mid-test. Every subprocess
# below inherits os.environ, so setting it once covers them all. The refresh
# behaviour itself is tested in test_fleetctl.py against a fake codexbar binary.
os.environ["FLEET_NO_AUTO_REFRESH"] = "1"

# Same trick, for the same reason, on the roster itself. Without this every wrapper
# below falls back to the operator's own overlay under XDG config, so the suite
# passed or failed on whatever fleet the machine happens to run -- and on a clean
# checkout, where that file does not exist at all, it simply collapsed. The shipped
# example is the fixture: these assertions are about routing, not about which model
# somebody ranked first this week.
FIXTURE_OVERLAY = ROOT / "tests" / "fixtures" / "access-overlay.test.json"
os.environ["ACCESS_OVERLAY"] = str(FIXTURE_OVERLAY)


# Some wrapper tests shell out to a real CLI and need its config profile on the
# machine. They pass wherever the developer already has it installed and fail on
# a clean checkout, which is the worst possible first impression for a new
# contributor: the suite looks broken when it is merely unconfigured. Skip with a
# reason instead, so a genuine regression stays visible and a missing optional
# dependency does not masquerade as one.
def _opencode_ready() -> str:
    if not shutil.which("opencode"):
        return "opencode CLI not on PATH"
    profile = Path.home() / ".config" / "opencode" / "fleet-worker"
    if not profile.is_dir():
        return f"opencode lean worker profile missing at {profile}"
    return ""


requires_opencode = unittest.skipIf(_opencode_ready(), _opencode_ready() or "ok")


class FleetPreflightTests(unittest.TestCase):
    def run_roster(self, *args):
        return subprocess.run(
            [str(SCRIPTS / "roster.sh"), *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def run_fanout(self, tasks):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            work.mkdir()
            tasks_path = root / "tasks.jsonl"
            normalized = []
            for task in tasks:
                item = dict(task)
                item.setdefault("dir", str(work))
                normalized.append(item)
            tasks_path.write_text(
                "".join(json.dumps(task) + "\n" for task in normalized),
                encoding="utf-8",
            )
            return subprocess.run(
                [
                    str(SCRIPTS / "fanout.sh"),
                    str(tasks_path),
                    "--parallel",
                    "4",
                    "--out",
                    str(root / "out"),
                    "--dry-run",
                ],
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, "FLEET_STATE_DIR": str(root / "state")},
            )

    def test_roster_validates_and_resolves(self):
        self.assertEqual(self.run_roster("validate").returncode, 0)
        resolved = self.run_roster("resolve-lane", "qwen3.7-plus", "opencode")
        self.assertEqual(resolved.returncode, 0)
        self.assertEqual(resolved.stdout.strip(), "opencode-go-qwen3.7-plus")

    def test_provisional_evidence_does_not_block_active_lane(self):
        admitted = self.run_roster("check-lane", "opencode-go-kimi-k3", "read-only")
        self.assertEqual(admitted.returncode, 0, admitted.stderr)

    def test_unknown_quota_uses_smart_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [str(SCRIPTS / "fleetctl.py"), "--state-dir", tmp, "route", "--role", "review"],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "opencode-go-kimi-k3")

    def test_implementation_role_uses_coding_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    str(SCRIPTS / "fleetctl.py"), "--state-dir", tmp, "route",
                    "--role", "implementation", "--mode", "write",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "opencode-go-kimi-k2.7-code")

    def test_prepaid_gemini_pro_lane_exists_but_never_wins_a_route(self):
        """The prepaid Pro lane exists to kill a paid lane, not to win routes.

        Its roles [] is load-bearing, not an oversight: Artificial Analysis scores
        3.1-pro BELOW 3.6-flash on both axes, so auto-routing to it would make routing
        worse. Anyone adding a role name here should have to delete this test first.
        """
        lane = self.run_roster("lane-json", "antigravity-gemini-pro")
        self.assertEqual(lane.returncode, 0, lane.stderr)
        pro = json.loads(lane.stdout)
        self.assertEqual(pro["selector"], "gemini-3.1-pro-high")
        self.assertEqual(pro["roles"], [])
        # Shared pool with flash: this lane moves Pro off the metered lane, it adds no headroom.
        self.assertEqual(pro["quota_pool"], "antigravity-gemini")

        for role in ("default", "hard-reasoning", "implementation", "review", "debug", "repo-map"):
            with tempfile.TemporaryDirectory() as tmp:
                routed = subprocess.run(
                    [str(SCRIPTS / "fleetctl.py"), "--state-dir", tmp, "route",
                     "--role", role, "--mode", "read-only", "--harness", "agy"],
                    text=True, capture_output=True, check=False,
                )
            self.assertEqual(routed.returncode, 0, routed.stderr)
            self.assertNotEqual(
                routed.stdout.strip(), "antigravity-gemini-pro",
                f"role {role} routed to the Pro lane, which benchmarks below flash",
            )

    def test_antigravity_claude_lanes_share_one_window_and_keep_their_selectors(self):
        """The two Antigravity Claude lanes are NOT named alike, and share one budget.

        `claude-sonnet-4-6` has no -thinking suffix while `claude-opus-4-6-thinking` does.
        Five plausible symmetric spellings were rejected by agy's validator,
        so anyone "tidying" these two into a matching pattern breaks the Sonnet lane.
        """
        lanes = {}
        for lane_id in ("antigravity-3p-opus", "antigravity-3p-sonnet"):
            result = self.run_roster("lane-json", lane_id)
            self.assertEqual(result.returncode, 0, result.stderr)
            lanes[lane_id] = json.loads(result.stdout)

        self.assertEqual(lanes["antigravity-3p-opus"]["selector"], "claude-opus-4-6-thinking")
        self.assertEqual(lanes["antigravity-3p-sonnet"]["selector"], "claude-sonnet-4-6")

        # One window for both: a second roled lane here would split scarce calls, not add any.
        self.assertEqual(
            {l["quota_pool"] for l in lanes.values()}, {"antigravity-3p"},
        )
        self.assertEqual(lanes["antigravity-3p-sonnet"]["roles"], [])
        self.assertEqual(lanes["antigravity-3p-opus"]["roles"], ["review"])

    def test_valid_mixed_read_only_campaign(self):
        result = self.run_fanout(
            [
                {"id": "flash", "agent": "opencode", "model_key": "deepseek-v4-flash", "prompt": "Map A."},
                {
                    "id": "qwen",
                    "agent": "opencode",
                    "model_key": "qwen3.7-plus",
                    "prompt": "Map B.",
                },
            ]
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unsafe_id_is_rejected(self):
        result = self.run_fanout([{"id": "../escape", "agent": "codex", "prompt": "Noop."}])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe task id", result.stderr)

    def test_unknown_agent_is_rejected(self):
        result = self.run_fanout([{"id": "bad", "agent": "../../evil", "prompt": "Noop."}])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown agent", result.stderr)

    def test_duplicate_write_root_is_rejected(self):
        result = self.run_fanout(
            [
                {"id": "a", "agent": "codex", "mode": "write", "prompt": "A."},
                {"id": "b", "agent": "codex", "mode": "write", "prompt": "B."},
            ]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("writable worktree overlaps", result.stderr)

    def test_root_and_subdirectory_writers_are_one_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            child = repo / "nested"
            child.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            tasks = root / "tasks.jsonl"
            tasks.write_text(
                "".join(
                    json.dumps(task) + "\n"
                    for task in (
                        {"id": "root", "agent": "codex", "mode": "write", "dir": str(repo), "prompt": "A."},
                        {"id": "child", "agent": "codex", "mode": "write", "dir": str(child), "prompt": "B."},
                    )
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [str(SCRIPTS / "fanout.sh"), str(tasks), "--dry-run", "--out", str(root / "out")],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("writable worktree overlaps", result.stderr)

    def test_lane_parallel_cap_is_enforced(self):
        tasks = [
            {"id": "glm-a", "agent": "opencode", "model_key": "glm-5.2", "prompt": "A."},
            {"id": "glm-b", "agent": "opencode", "model_key": "glm-5.2", "prompt": "B."},
        ]
        result = self.run_fanout(tasks)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("max_tasks_per_run", result.stderr)

    def test_rejected_lane_stays_unusable(self):
        result = self.run_fanout(
            [
                {
                    "id": "mimo-pro",
                    "agent": "opencode",
                    "model_key": "mimo-v2.5-pro",
                    "prompt": "Noop.",
                }
            ]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("lane admission is rejected", result.stderr)

    def test_fanout_preserves_shared_context(self):
        result = self.run_fanout(
            [{"id": "shared", "agent": "opencode", "context": "shared", "prompt": "Review."}]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(result.stdout.strip())
        self.assertEqual(manifest["context"], "shared")

    def test_fanout_rejects_invalid_context(self):
        result = self.run_fanout(
            [{"id": "bad-context", "agent": "opencode", "context": "huge", "prompt": "Review."}]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("context must be lean or shared", result.stderr)

    def test_fanout_research_role_uses_frontier_route_and_lean_context(self):
        result = self.run_fanout(
            [{"id": "sources", "agent": "opencode", "role": "research-scout", "prompt": "Find public sources."}]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(result.stdout.strip())
        self.assertEqual(manifest["role"], "research-scout")
        self.assertEqual(manifest["lane_id"], "opencode-go-kimi-k3")
        self.assertEqual(manifest["context"], "lean")

    def test_fanout_rejects_unsafe_research_scout_combinations(self):
        cases = [
            ({"context": "shared"}, "shared"),
            ({"modality": "image"}, "image"),
            ({"mode": "write"}, "write"),
            ({"file": str(Path(__file__).resolve())}, "attachment"),
        ]
        for extra, label in cases:
            with self.subTest(label=label):
                task = {"id": f"unsafe-{label}", "agent": "opencode", "role": "research-scout", "prompt": "No."}
                task.update(extra)
                result = self.run_fanout([task])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("research-scout must be read-only, lean, text-only, and attachment-free", result.stderr)

    @requires_opencode
    def test_wrapper_terminates_file_array_before_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            work_dir = root / "work"
            bin_dir.mkdir()
            work_dir.mkdir()
            attachment = root / "safe.png"
            attachment.write_bytes(b"synthetic")
            args_file = root / "args.txt"
            config_file = root / "config-dir.txt"
            exa_file = root / "exa.txt"
            search_env_file = root / "search-env.txt"
            hostile_config = root / "hostile-fleet-profile"
            hostile_config.mkdir()
            (hostile_config / "AGENTS.md").write_text("HOSTILE_OVERRIDE", encoding="utf-8")
            (hostile_config / "opencode.jsonc").write_text(
                '{"agent":{"fleet-research":{"tools":{"bash":true,"task":true,"read":true}}}}',
                encoding="utf-8",
            )
            hostile_tmp = root / "hostile-tmp"
            hostile_tmp.mkdir()
            (hostile_tmp / "AGENTS.md").write_text("HOSTILE_TMP_INSTRUCTION", encoding="utf-8")
            stub = bin_dir / "opencode"
            stub.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$@\" > \"$OPENCODE_STUB_ARGS\"\n"
                "printf '%s\\n' \"${OPENCODE_CONFIG_DIR:-}\" > \"$OPENCODE_STUB_CONFIG\"\n"
                "printf '%s\\n' \"${OPENCODE_ENABLE_EXA:-}\" > \"$OPENCODE_STUB_EXA\"\n"
                "{\n"
                "printf 'provider=%s\\n' \"${OPENCODE_WEBSEARCH_PROVIDER:-}\"\n"
                "printf 'parallel_key=%s\\n' \"${PARALLEL_API_KEY:-}\"\n"
                "printf 'exa_key=%s\\n' \"${EXA_API_KEY:-}\"\n"
                "printf 'auto_share=%s\\n' \"${OPENCODE_AUTO_SHARE:-}\"\n"
                "printf 'enable_parallel=%s\\n' \"${OPENCODE_ENABLE_PARALLEL:-}\"\n"
                "printf 'experimental_parallel=%s\\n' \"${OPENCODE_EXPERIMENTAL_PARALLEL:-}\"\n"
                "printf 'experimental_exa=%s\\n' \"${OPENCODE_EXPERIMENTAL_EXA:-}\"\n"
                "printf 'experimental=%s\\n' \"${OPENCODE_EXPERIMENTAL:-}\"\n"
                "printf 'xdg_data_home=%s\\n' \"${XDG_DATA_HOME:-}\"\n"
                "if [ -f \"${XDG_DATA_HOME:-}/opencode/auth.json\" ]; then printf 'auth_present=1\\n'; else printf 'auth_present=0\\n'; fi\n"
                "} > \"$OPENCODE_STUB_SEARCH_ENV\"\n"
                "printf '%s\\n' '{\"type\":\"text\",\"part\":{\"text\":\"STUB_OK\"}}'\n"
                "printf '%s\\n' '{\"type\":\"step_finish\",\"part\":{\"reason\":\"stop\",\"tokens\":{\"total\":1,\"input\":1,\"output\":0,\"reasoning\":0,\"cache\":{\"read\":0,\"write\":0}},\"cost\":0}}'\n",
                encoding="utf-8",
            )
            stub.chmod(0o755)
            result = subprocess.run(
                [
                    str(SCRIPTS / "opencode-agent.sh"),
                    "--lane", "opencode-go-kimi-k3",
                    "--modality", "image",
                    "--file", str(attachment),
                    "--dir", str(work_dir),
                    "--prompt", "SENTINEL_PROMPT",
                ],
                text=True,
                capture_output=True,
                check=False,
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "OPENCODE_STUB_ARGS": str(args_file),
                    "OPENCODE_STUB_CONFIG": str(config_file),
                    "OPENCODE_STUB_EXA": str(exa_file),
                    "OPENCODE_STUB_SEARCH_ENV": str(search_env_file),
                    "OPENCODE_FLEET_CONFIG_DIR": str(hostile_config),
                    "OPENCODE_WEBSEARCH_PROVIDER": "parallel",
                    "PARALLEL_API_KEY": "fake-parallel-key",
                    "EXA_API_KEY": "fake-exa-key",
                    "OPENCODE_ENABLE_PARALLEL": "1",
                    "OPENCODE_EXPERIMENTAL_PARALLEL": "1",
                    "OPENCODE_EXPERIMENTAL_EXA": "1",
                    "OPENCODE_EXPERIMENTAL": "1",
                    "OPENCODE_AUTO_SHARE": "true",
                    "FLEET_STATE_DIR": str(root / "state"),
                    "AGENT_SYNC_VERIFY": str(root / "missing-verifier"),
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "STUB_OK\n")
            self.assertIn("Crossfeed model receipt:", result.stderr)
            arguments = args_file.read_text(encoding="utf-8").splitlines()
            separator = arguments.index("--")
            self.assertTrue("\n".join(arguments[separator + 1:]).endswith("=== CROSSFEED TASK ===\nSENTINEL_PROMPT"))
            self.assertIn("Crossfeed selected model: kimi-k3", "\n".join(arguments[separator + 1:]))
            self.assertLess(arguments.index(str(attachment)), separator)
            self.assertTrue(config_file.read_text(encoding="utf-8").strip().endswith("/fleet-worker"))
            self.assertNotEqual(config_file.read_text(encoding="utf-8").strip(), str(hostile_config))
            self.assertEqual(exa_file.read_text(encoding="utf-8").strip(), "")
            clean_env = dict(
                line.split("=", 1)
                for line in search_env_file.read_text(encoding="utf-8").splitlines()
            )
            self.assertEqual(clean_env["provider"], "")
            self.assertEqual(clean_env["parallel_key"], "")
            self.assertEqual(clean_env["exa_key"], "")
            self.assertEqual(clean_env["auto_share"], "false")
            isolated_home = Path(clean_env["xdg_data_home"])
            self.assertEqual(isolated_home.parent, root / "state" / "oc-homes")
            self.assertEqual(clean_env["auth_present"], "1")
            self.assertFalse(isolated_home.exists())
            self.assertTrue(all(clean_env[name] == "" for name in (
                "enable_parallel", "experimental_parallel", "experimental_exa", "experimental"
            )))

            shared = subprocess.run(
                [
                    str(SCRIPTS / "opencode-agent.sh"),
                    "--lane", "opencode-go-kimi-k3",
                    "--context", "shared",
                    "--dir", str(work_dir),
                    "--prompt", "SHARED_PROMPT",
                ],
                text=True,
                capture_output=True,
                check=False,
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "OPENCODE_CONFIG_DIR": "/bogus/inherited",
                    "OPENCODE_ENABLE_EXA": "inherited-unsafe-value",
                    "OPENCODE_WEBSEARCH_PROVIDER": "parallel",
                    "PARALLEL_API_KEY": "fake-parallel-key",
                    "OPENCODE_AUTO_SHARE": "true",
                    "OPENCODE_STUB_ARGS": str(args_file),
                    "OPENCODE_STUB_CONFIG": str(config_file),
                    "OPENCODE_STUB_EXA": str(exa_file),
                    "OPENCODE_STUB_SEARCH_ENV": str(search_env_file),
                    "FLEET_STATE_DIR": str(root / "state"),
                    "AGENT_SYNC_VERIFY": str(root / "missing-verifier"),
                },
            )
            self.assertEqual(shared.returncode, 0, shared.stderr)
            self.assertEqual(config_file.read_text(encoding="utf-8").strip(), "")
            self.assertEqual(exa_file.read_text(encoding="utf-8").strip(), "")
            shared_env = dict(
                line.split("=", 1)
                for line in search_env_file.read_text(encoding="utf-8").splitlines()
            )
            self.assertEqual(shared_env["provider"], "")
            self.assertEqual(shared_env["parallel_key"], "")
            self.assertEqual(shared_env["auto_share"], "false")

            research = subprocess.run(
                [
                    str(SCRIPTS / "opencode-agent.sh"),
                    "--web-search",
                    "--dir", str(work_dir),
                    "--prompt", "PUBLIC_SENTINEL",
                ],
                text=True,
                capture_output=True,
                check=False,
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "TMPDIR": str(hostile_tmp),
                    "OPENCODE_ENABLE_EXA": "inherited-unsafe-value",
                    "OPENCODE_WEBSEARCH_PROVIDER": "parallel",
                    "PARALLEL_API_KEY": "fake-parallel-key",
                    "EXA_API_KEY": "fake-exa-key",
                    "OPENCODE_ENABLE_PARALLEL": "1",
                    "OPENCODE_EXPERIMENTAL_PARALLEL": "1",
                    "OPENCODE_EXPERIMENTAL_EXA": "1",
                    "OPENCODE_EXPERIMENTAL": "1",
                    "OPENCODE_AUTO_SHARE": "true",
                    "OPENCODE_FLEET_CONFIG_DIR": str(hostile_config),
                    "OPENCODE_STUB_ARGS": str(args_file),
                    "OPENCODE_STUB_CONFIG": str(config_file),
                    "OPENCODE_STUB_EXA": str(exa_file),
                    "OPENCODE_STUB_SEARCH_ENV": str(search_env_file),
                    "FLEET_STATE_DIR": str(root / "state"),
                    "AGENT_SYNC_VERIFY": str(root / "missing-verifier"),
                },
            )
            self.assertEqual(research.returncode, 0, research.stderr)
            research_args = args_file.read_text(encoding="utf-8").splitlines()
            self.assertEqual(research_args[research_args.index("--agent") + 1], "fleet-research")
            self.assertEqual(research_args[research_args.index("--model") + 1], "opencode-go/kimi-k3")
            research_dir = Path(research_args[research_args.index("--dir") + 1])
            self.assertNotEqual(research_dir, work_dir)
            self.assertNotEqual(research_dir.parent, hostile_tmp)
            self.assertFalse(research_dir.exists())
            research_prompt = "\n".join(research_args[research_args.index("--") + 1:])
            self.assertIn("Public-web source scouting only", research_prompt)
            self.assertTrue(research_prompt.endswith("PUBLIC_SENTINEL"))
            self.assertEqual(exa_file.read_text(encoding="utf-8").strip(), "1")
            research_env = dict(
                line.split("=", 1)
                for line in search_env_file.read_text(encoding="utf-8").splitlines()
            )
            self.assertEqual(research_env["provider"], "exa")
            self.assertEqual(research_env["parallel_key"], "")
            self.assertEqual(research_env["exa_key"], "")
            self.assertEqual(research_env["auto_share"], "false")
            self.assertTrue(all(research_env[name] == "" for name in (
                "enable_parallel", "experimental_parallel", "experimental_exa", "experimental"
            )))

            args_file.unlink()
            missing = subprocess.run(
                [
                    str(SCRIPTS / "opencode-agent.sh"),
                    "--lane", "opencode-go-kimi-k3",
                    "--dir", str(work_dir),
                    "--prompt", "MISSING_PROFILE",
                ],
                text=True,
                capture_output=True,
                check=False,
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "HOME": str(root / "missing-home"),
                    "OPENCODE_STUB_ARGS": str(args_file),
                    "OPENCODE_STUB_CONFIG": str(config_file),
                    "OPENCODE_STUB_EXA": str(exa_file),
                    "OPENCODE_STUB_SEARCH_ENV": str(search_env_file),
                    "FLEET_STATE_DIR": str(root / "state"),
                    "AGENT_SYNC_VERIFY": str(root / "missing-verifier"),
                },
            )
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("lean profile is incomplete", missing.stderr)
            self.assertFalse(args_file.exists())

            rejection_cases = [
                (["--web-search", "--write"], "read-only"),
                (["--web-search", "--context", "shared"], "requires --context lean"),
                (["--web-search", "--modality", "image"], "accepts text only"),
                (["--web-search", "--file", str(attachment)], "rejects attachments"),
                (["--web-search", "--role", "review"], "non-research role"),
            ]
            for flags, expected in rejection_cases:
                with self.subTest(wrapper_rejection=expected):
                    args_file.unlink(missing_ok=True)
                    rejected = subprocess.run(
                        [
                            str(SCRIPTS / "opencode-agent.sh"),
                            *flags,
                            "--dir", str(work_dir),
                            "--prompt", "MUST_NOT_RUN",
                        ],
                        text=True,
                        capture_output=True,
                        check=False,
                        env={
                            **os.environ,
                            "PATH": f"{bin_dir}:{os.environ['PATH']}",
                            "OPENCODE_STUB_ARGS": str(args_file),
                            "OPENCODE_STUB_CONFIG": str(config_file),
                            "OPENCODE_STUB_EXA": str(exa_file),
                            "OPENCODE_STUB_SEARCH_ENV": str(search_env_file),
                            "FLEET_STATE_DIR": str(root / "state"),
                            "AGENT_SYNC_VERIFY": str(root / "missing-verifier"),
                        },
                    )
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertIn(expected, rejected.stderr)
                    self.assertFalse(args_file.exists())

    def test_wrapper_help_exposes_context_profiles(self):
        result = subprocess.run(
            [str(SCRIPTS / "opencode-agent.sh"), "--help"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--context lean|shared", result.stdout)
        self.assertIn("--shared-context", result.stdout)
        self.assertIn("--web-search", result.stdout)

    def test_direct_wrapper_uses_toolless_isolated_model_call_and_marks_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            home = root / "home"
            work_dir = root / "work"
            args_file = root / "args.txt"
            env_file = root / "env.txt"
            config_file = root / "config.json"
            state_dir = root / "state"
            bin_dir.mkdir()
            work_dir.mkdir()
            auth = home / ".local" / "share" / "opencode" / "auth.json"
            auth.parent.mkdir(parents=True)
            auth.write_text('{"stub":true}', encoding="utf-8")
            stub = bin_dir / "opencode"
            stub.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$@\" > \"$OPENCODE_STUB_ARGS\"\n"
                "printf 'xdg_data_home=%s\\nconfig_dir=%s\\nproject_config=%s\\n' \"${XDG_DATA_HOME:-}\" \"${OPENCODE_CONFIG_DIR:-}\" \"${OPENCODE_DISABLE_PROJECT_CONFIG:-}\" > \"$OPENCODE_STUB_ENV\"\n"
                "cp \"$OPENCODE_CONFIG_DIR/opencode.json\" \"$OPENCODE_STUB_CONFIG\"\n"
                "printf '%s\\n' '{\"type\":\"text\",\"part\":{\"text\":\"DIRECT_OK\"}}'\n"
                "printf '%s\\n' '{\"type\":\"step_finish\",\"part\":{\"reason\":\"stop\",\"tokens\":{\"total\":1,\"input\":1,\"output\":0,\"reasoning\":0,\"cache\":{\"read\":0,\"write\":0}},\"cost\":0}}'\n",
                encoding="utf-8",
            )
            stub.chmod(0o755)
            result = subprocess.run(
                [
                    str(SCRIPTS / "opencode-agent.sh"), "--direct",
                    "--lane", "opencode-go-kimi-k3", "--dir", str(work_dir),
                    "--prompt", "CLASSIFY_THIS",
                ],
                text=True,
                capture_output=True,
                check=False,
                env={
                    **os.environ, "HOME": str(home), "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "FLEET_STATE_DIR": str(state_dir), "OPENCODE_STUB_ARGS": str(args_file),
                    "OPENCODE_STUB_ENV": str(env_file), "OPENCODE_STUB_CONFIG": str(config_file),
                    "ACCESS_OVERLAY": str(FIXTURE_OVERLAY),
                    "AGENT_SYNC_VERIFY": str(root / "missing-verifier"),
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "DIRECT_OK\n")
            self.assertIn("Crossfeed model receipt:", result.stderr)
            arguments = args_file.read_text(encoding="utf-8").splitlines()
            self.assertEqual(arguments[arguments.index("--agent") + 1], "direct")
            self.assertNotIn("--file", arguments)
            self.assertTrue("\n".join(arguments[arguments.index("--") + 1:]).endswith("=== CROSSFEED TASK ===\nCLASSIFY_THIS"))
            direct_config = json.loads(config_file.read_text(encoding="utf-8"))
            self.assertEqual(direct_config["agent"]["direct"]["tools"], {"*": False})
            self.assertEqual(direct_config["agent"]["direct"]["permission"], {"*": "deny"})
            run_env = dict(line.split("=", 1) for line in env_file.read_text(encoding="utf-8").splitlines())
            isolated_home = Path(run_env["xdg_data_home"])
            self.assertEqual(isolated_home.parent, state_dir / "oc-homes")
            self.assertEqual(run_env["project_config"], "1")
            self.assertFalse(isolated_home.exists())
            ledger = [json.loads(line) for line in (state_dir / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(ledger), 2)
            self.assertEqual(ledger[1]["schema"], "crossfeed-model-run/v1")
            self.assertEqual(ledger[1]["selected_model"], "kimi-k3")
            self.assertEqual(ledger[0]["execution_mode"], "direct")
            self.assertEqual(ledger[0]["agent_profile"], "direct")

    def test_swarms_shrink_by_quota_band_and_hard_stop(self):
        expected = {
            None: ("UNKNOWN", ["opencode-go-kimi-k3"]),
            10: ("ABUNDANT", [
                "opencode-go-kimi-k3", "opencode-go-glm-5.2",
                "opencode-go-deepseek-v4-pro", "opencode-go-qwen3.7-max",
                "opencode-go-grok-4.5",
            ]),
            80: ("CONSERVE", ["opencode-go-kimi-k3"]),
            95: ("CRITICAL", ["opencode-go-deepseek-v4-flash"]),
        }
        for used, (state, lanes) in expected.items():
            with self.subTest(used=used), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state_dir = root / "state"
                if used is not None:
                    subprocess.run(
                        [
                            str(SCRIPTS / "fleetctl.py"), "--state-dir", str(state_dir), "snapshot",
                            "--rolling-used", str(used), "--rolling-reset-seconds", "3600",
                            "--weekly-used", str(used), "--weekly-reset-seconds", "7200",
                            "--monthly-used", str(used), "--monthly-reset-seconds", "10800",
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                result = subprocess.run(
                    [str(SCRIPTS / "swarm.sh"), "review", "--prompt", "Review.", "--dry-run", "--out", str(root / "out")],
                    text=True,
                    capture_output=True,
                    check=False,
                    env={**os.environ, "FLEET_STATE_DIR": str(state_dir)},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"quota_state={state}", result.stderr)
                manifests = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
                self.assertEqual([item["lane_id"] for item in manifests], lanes)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            subprocess.run(
                [
                    str(SCRIPTS / "fleetctl.py"), "--state-dir", str(state_dir), "snapshot",
                    "--rolling-used", "100", "--rolling-reset-seconds", "3600",
                    "--weekly-used", "100", "--weekly-reset-seconds", "7200",
                    "--monthly-used", "100", "--monthly-reset-seconds", "10800",
                ], check=True, capture_output=True, text=True,
            )
            result = subprocess.run(
                [str(SCRIPTS / "swarm.sh"), "review", "--prompt", "No.", "--dry-run", "--out", str(root / "out")],
                text=True, capture_output=True, check=False,
                env={**os.environ, "FLEET_STATE_DIR": str(state_dir)},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("quota pool is exhausted", result.stderr)

    def test_audio_and_video_swarm_use_native_gemini_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for modality, suffix in (("audio", ".wav"), ("video", ".mp4")):
                with self.subTest(modality=modality):
                    attachment = root / f"sample{suffix}"
                    attachment.write_bytes(b"synthetic")
                    result = subprocess.run(
                        [
                            str(SCRIPTS / "swarm.sh"), "media-review", "--prompt", "Inspect.",
                            "--file", str(attachment), "--modality", modality, "--dry-run",
                            "--out", str(root / f"{modality}-out"),
                        ],
                        text=True, capture_output=True, check=False,
                        env={**os.environ, "FLEET_STATE_DIR": str(root / "state")},
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    # Compare unescaped: the dispatcher shell-quotes its argv, so a
                    # checkout path containing a space arrives as "Second\ Brain"
                    # and a naive substring match fails on a correctly-escaped
                    # command line.
                    self.assertIn(
                        str(SCRIPTS / "gemini-media.sh"),
                        result.stdout.replace("\\", ""),
                    )
                    self.assertIn(f"--modality {modality}", result.stdout)
                    self.assertIn(str(attachment), result.stdout)
                    self.assertNotIn("opencode-agent.sh", result.stdout)


class OpencodeInjectTests(unittest.TestCase):
    """--inject prepends a specific context slice; it validates before any model call."""

    def run_agent(self, *args, cwd=None):
        return subprocess.run(
            [str(SCRIPTS / "opencode-agent.sh"), *args],
            text=True, capture_output=True, check=False, cwd=cwd,
        )

    @requires_opencode
    def test_inject_missing_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_agent(
                "--prompt", "do the thing", "--dir", tmp,
                "--inject", str(Path(tmp) / "nope.md"),
                cwd=tmp,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not found", result.stderr)

    @requires_opencode
    def test_inject_rejected_for_web_search_scout(self):
        with tempfile.TemporaryDirectory() as tmp:
            slice_file = Path(tmp) / "slice.md"
            slice_file.write_text("one fact the task needs", encoding="utf-8")
            result = self.run_agent(
                "--prompt", "find sources", "--dir", tmp,
                "--web-search", "--inject", str(slice_file),
                cwd=tmp,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cannot combine", result.stderr)


class AfkControllerTests(unittest.TestCase):
    """The controller test seam prevents regression tests from spending quota."""

    def run_afk(self, actions, routes, max_attempts=3):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        work = root / "work"
        state = root / "state"
        work.mkdir()
        runner = root / "runner.sh"
        body = ["#!/usr/bin/env bash", "set -euo pipefail", "route=''; dir=''", "while [ \"$#\" -gt 0 ]; do", "  case \"$1\" in", "    --route) route=\"$2\"; shift 2;;", "    --objective) shift 2;;", "    --dir) dir=\"$2\"; shift 2;;", "  esac", "done", "echo done"]
        for route, action in actions.items():
            body.append(f'if [ "$route" = {shlex.quote(route)} ]; then {action}; fi')
        runner.write_text("\n".join(body) + "\n", encoding="utf-8")
        runner.chmod(0o755)
        proof_file = work / "proof.txt"
        proof = f'test "$(cat {shlex.quote(str(proof_file))} 2>/dev/null)" = PASS'
        result = subprocess.run(
            [
                str(SCRIPTS / "afk-run.sh"), "--objective", "make proof pass",
                "--proof-command", proof, "--time-budget-s", "30",
                "--max-attempts", str(max_attempts), "--routes", ",".join(routes),
                "--dir", str(work), "--runner", str(runner),
            ],
            text=True, capture_output=True, check=False,
            env={**os.environ, "FLEET_STATE_DIR": str(state)},
        )
        ledger = [
            json.loads(line) for line in (state / "runs.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        return result, ledger, state

    def test_afk_switches_route_after_failed_proof(self):
        result, ledger, state = self.run_afk(
            {"opencode-go-kimi-k3": 'printf PASS > "$dir/proof.txt"'},
            ["opencode-go-deepseek-v4-flash", "opencode-go-kimi-k3"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        # Matched per attempt LINE, not as one contiguous string: since
        # scripts/outcome-taxonomy.sh landed, every attempt also carries
        # `outcome=` and `reason=` between the route and the verdict. Pinning the
        # exact adjacency froze the log format, which is the opposite of what
        # this test is for -- the claim is "that route ended that way".
        self.assertRegex(
            result.stdout,
            r"route=opencode-go-deepseek-v4-flash\b[^\n]* outcome=FAILED\b[^\n]* result=failed\b",
        )
        self.assertRegex(
            result.stdout,
            r"route=opencode-go-kimi-k3\b[^\n]* outcome=SUCCEEDED\b[^\n]* result=verified\b",
        )
        self.assertEqual([(r["route"], r["failure_class"]) for r in ledger], [
            ("opencode-go-deepseek-v4-flash", "proof-failed"),
            ("opencode-go-kimi-k3", None),
        ])
        ranked = subprocess.run(
            [str(SCRIPTS / "fleetctl.py"), "--state-dir", str(state), "rank-routes", "--json"],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(ranked.returncode, 0, ranked.stderr)
        self.assertEqual(json.loads(ranked.stdout)[0]["route"], "opencode-go-kimi-k3")

    def test_afk_done_without_proof_is_rejected(self):
        result, ledger, _ = self.run_afk({}, ["opencode-go-deepseek-v4-flash"], max_attempts=1)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(ledger[0]["result"], "failed")
        self.assertEqual(ledger[0]["failure_class"], "proof-failed")
        self.assertEqual(ledger[0]["worker_returncode"], 0)

    def test_afk_attempt_budget_stops(self):
        result, ledger, _ = self.run_afk(
            {}, ["opencode-go-deepseek-v4-flash", "opencode-go-kimi-k3"], max_attempts=1
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("attempt budget exhausted", result.stderr)
        self.assertEqual(len(ledger), 1)

    def test_afk_no_progress_stops_after_two_unchanged_proofs(self):
        result, ledger, _ = self.run_afk(
            {},
            ["opencode-go-deepseek-v4-flash", "opencode-go-kimi-k3", "opencode-go-qwen3.7-plus"],
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("no measurable progress", result.stderr)
        self.assertEqual(len(ledger), 2)
        self.assertEqual(ledger[0]["proof"]["output_sha256"], ledger[1]["proof"]["output_sha256"])


class EmptyOutputIsNeverSuccessTests(unittest.TestCase):
    """No wrapper, and no fan-out, may report an answer it never received.

    Bought after a council lens vanished and only a hand-count of output
    bytes caught it. Two independent holes: agy-agent.sh died on a bad --lane carrying
    roster.sh's own exit 3, which is this fleet's code for AGY QUOTA EXHAUSTED, with
    empty stderr; and fanout.sh scored exit 0 as PASS without ever looking at the result
    file. Every test here stubs the model binary on PATH and redirects HOME, so none of
    them spend quota or read live fleet state.
    """

    def _stub_env(self, root, binary, body):
        """Put a fake model binary first on PATH; return an env with a private HOME."""
        bindir = root / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        stub = bindir / binary
        stub.write_text("#!/usr/bin/env bash\n" + body + "\n", encoding="utf-8")
        stub.chmod(0o755)
        home = root / "home"
        home.mkdir(parents=True, exist_ok=True)
        return {
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(home),
            "FLEET_STATE_DIR": str(root / "state"),
        }

    def _run(self, script, args, env):
        return subprocess.run(
            [str(SCRIPTS / script), *args],
            text=True, capture_output=True, check=False, env=env,
        )

    def test_agy_unknown_lane_exits_2_not_the_quota_code(self):
        """A mistyped lane must not reach the caller wearing the quota exit code."""
        with tempfile.TemporaryDirectory() as tmp:
            env = self._stub_env(Path(tmp), "agy", "echo ALIVE")
            # antigravity-gemini is the QUOTA POOL name; the lane is antigravity-gemini-flash.
            result = self._run(
                "agy-agent.sh",
                ["--dir", tmp, "--lane", "antigravity-gemini", "--prompt", "hi"],
                env,
            )
            self.assertEqual(result.returncode, 2, result.stderr)  # 3 reads as AGY QUOTA EXHAUSTED
            self.assertIn("unknown lane", result.stderr)
            self.assertEqual(result.stdout, "")

    def test_agy_empty_output_exits_4(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self._stub_env(Path(tmp), "agy", "exit 0")
            result = self._run(
                "agy-agent.sh",
                ["--dir", tmp, "--model", "gemini-3.8-flash-high", "--prompt", "hi"],
                env,
            )
            self.assertEqual(result.returncode, 4, result.stderr)
            self.assertIn("EMPTY OUTPUT", result.stderr)

    def test_agy_empty_output_with_a_429_in_the_log_exits_3(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self._stub_env(root, "agy", "exit 0")
            log_dir = root / "home" / ".gemini" / "antigravity-cli"
            log_dir.mkdir(parents=True)
            (log_dir / "cli.log").write_text(
                "status: RESOURCE_EXHAUSTED\n", encoding="utf-8",
            )
            result = self._run(
                "agy-agent.sh",
                ["--dir", tmp, "--model", "gemini-3.8-flash-high", "--prompt", "hi"],
                env,
            )
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertIn("QUOTA EXHAUSTED", result.stderr)

    def test_agy_real_answer_still_exits_0(self):
        """Falsification twin: prove the guard can go green, not just red."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self._stub_env(root, "agy", "echo ALIVE")
            last = root / "last.txt"
            result = self._run(
                "agy-agent.sh",
                ["--dir", tmp, "--model", "gemini-3.8-flash-high", "--prompt", "hi",
                 "--last", str(last)],
                env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "ALIVE\n")
            self.assertIn("Crossfeed model receipt:", result.stderr)
            self.assertEqual(last.read_text(encoding="utf-8"), "ALIVE\n")
            receipt = json.loads(Path(str(last) + ".crossfeed.json").read_text())
            self.assertEqual(receipt["returncode"], 0)
            self.assertIn(receipt["run_id"], result.stderr)

    def test_agy_effort_selector_and_quota_lease_agree(self):
        live_overlay = ROOT / "tests" / "fixtures" / "access-overlay.test.json"
        roster = json.loads(live_overlay.read_text())
        lanes = {lane["lane_id"]: lane for lane in roster["lanes"]}
        cases = [
            (["--role", "default"], "gemini-3.8-flash-medium", "medium", "antigravity-gemini"),
            (["--role", "default", "--effort", "low"], "gemini-3.8-flash-low", "low", "antigravity-gemini"),
            (["--model", "gemini-3.8-flash-low"], "gemini-3.8-flash-low", "low", "antigravity-gemini"),
            (["--model", "gemini-3.8-flash-high"], "gemini-3.8-flash-high", "high", "antigravity-gemini"),
            (["--lane", "antigravity-gemini-flash-38-high"], "gemini-3.8-flash-high", "high", "antigravity-gemini"),
            (["--model", "gemini-3.8-flash-high", "--effort", "low"], "gemini-3.8-flash-low", "low", "antigravity-gemini"),
            (["--model", "gemini-3.8-flash-low", "--role", "probe", "--effort", "low"],
             "gemini-3.8-flash-low", "low", "antigravity-gemini"),
            (["--model", "claude-opus-4-6-thinking", "--role", "default"],
             "claude-opus-4-6-thinking", None, "antigravity-3p"),
        ]
        for args, model, effort, pool in cases:
            with self.subTest(model=model, args=args), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                capture = root / "capture.py"
                capture.write_text(
                    "import json, os, sys\nfrom pathlib import Path\n"
                    "state = json.loads((Path(os.environ['FLEET_STATE_DIR']) / 'runtime.json').read_text())\n"
                    "Path(os.environ['CAPTURE_FILE']).write_text(json.dumps({'argv':sys.argv[1:], 'leases':state['leases']}))\n"
                    "print('ALIVE')\n", encoding="utf-8",
                )
                env = self._stub_env(root, "agy", f"exec python3 {shlex.quote(str(capture))} \"$@\"")
                env.update(ACCESS_OVERLAY=str(live_overlay), CAPTURE_FILE=str(root / "captured.json"))
                result = self._run("agy-agent.sh", ["--dir", tmp, "--prompt", "hi", *args], env)
                self.assertEqual(result.returncode, 0, result.stderr)
                actual = json.loads((root / "captured.json").read_text())
                argv = actual["argv"]
                self.assertEqual(argv[argv.index("--model") + 1], model)
                if effort:
                    self.assertEqual(argv[argv.index("--effort") + 1], effort)
                    self.assertIn(f"effort {effort}:", result.stderr)
                else:
                    self.assertNotIn("--effort", argv)
                    self.assertIn("level control support not established", result.stderr)
                self.assertEqual(len(actual["leases"]), 1)
                self.assertEqual(lanes[actual["leases"][0]["lane_id"]]["quota_pool"], pool)
                self.assertEqual(json.loads((root / "state" / "runtime.json").read_text())["leases"], [])

    def test_agy_refuses_model_lane_mismatch_before_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); marker = root / "called"
            env = self._stub_env(root, "agy", f"touch {shlex.quote(str(marker))}; echo ALIVE")
            result = self._run("agy-agent.sh", ["--dir", tmp, "--prompt", "hi",
                              "--lane", "antigravity-gemini-flash", "--model", "claude-opus-4-6-thinking"], env)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("does not belong to lane", result.stderr)
            self.assertFalse(marker.exists())

    def test_agy_unranked_effort_cannot_bypass_family_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); marker = root / "called"
            data = json.loads((ROOT / "tests" / "fixtures" / "access-overlay.test.json").read_text())
            for lane in data["lanes"]:
                if lane["model_key"] == "gemini-3.8-flash":
                    lane["admission_status"] = "candidate"
            overlay = root / "overlay.json"; overlay.write_text(json.dumps(data))
            env = self._stub_env(root, "agy", f"touch {shlex.quote(str(marker))}; echo ALIVE")
            env["ACCESS_OVERLAY"] = str(overlay)
            result = self._run("agy-agent.sh", ["--dir", tmp, "--prompt", "hi",
                              "--model", "gemini-3.8-flash-low", "--effort", "low"], env)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("no admitted AGY model family", result.stderr)
            self.assertFalse(marker.exists())

    def test_claude_empty_output_exits_4_and_leaves_no_result_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self._stub_env(root, "claude", "exit 0")
            last = root / "last.txt"
            result = self._run(
                "claude-agent.sh",
                ["--dir", tmp, "--prompt", "hi", "--last", str(last)],
                env,
            )
            self.assertEqual(result.returncode, 4, result.stderr)
            self.assertIn("EMPTY OUTPUT", result.stderr)
            self.assertFalse(last.exists(), "an empty run must leave no result file behind")

    def test_claude_real_answer_still_exits_0(self):
        """Falsification twin for the claude wrapper."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self._stub_env(root, "claude", "echo ANSWER")
            last = root / "last.txt"
            result = self._run(
                "claude-agent.sh",
                ["--dir", tmp, "--prompt", "hi", "--last", str(last)],
                env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "ANSWER\n")
            self.assertIn("Crossfeed model receipt:", result.stderr)
            self.assertEqual(last.read_text(encoding="utf-8"), "ANSWER\n")
            receipt = json.loads(Path(str(last) + ".crossfeed.json").read_text())
            self.assertEqual(receipt["returncode"], 0)
            self.assertIn(receipt["run_id"], result.stderr)

    def test_opencode_unknown_lane_exits_2_not_the_modality_code(self):
        """A mistyped lane must not reach the caller wearing the modality-reject code.

        opencode-agent.sh answers 3 for "this lane does not admit that modality", and
        roster.sh answers 3 for "unknown lane". A bare assignment let roster's 3 through
        untouched, so fanout.sh recorded FAIL(3) and afk-run.sh a worker-returncode of 3
        for a plain typo. --direct skips the lean-profile check, which would otherwise
        stop the run before lane resolution under a stubbed HOME.
        """
        with tempfile.TemporaryDirectory() as tmp:
            env = self._stub_env(Path(tmp), "opencode", "echo ALIVE")
            result = self._run(
                "opencode-agent.sh",
                ["--dir", tmp, "--lane", "bogus-lane-xyz", "--prompt", "hi", "--direct"],
                env,
            )
            self.assertEqual(result.returncode, 2, result.stderr)  # 3 reads as modality reject
            self.assertIn("opencode-agent: unknown lane", result.stderr)
            self.assertIn("roster: unknown lane", result.stderr)  # callee's message survives
            self.assertEqual(result.stdout, "")

    def test_opencode_unresolvable_model_key_exits_2(self):
        """The same catch on the --model-key path, which shares roster's exit 3."""
        with tempfile.TemporaryDirectory() as tmp:
            env = self._stub_env(Path(tmp), "opencode", "echo ALIVE")
            result = self._run(
                "opencode-agent.sh",
                ["--dir", tmp, "--model-key", "not-a-real-key", "--prompt", "hi", "--direct"],
                env,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("no OpenCode lane for --model-key", result.stderr)
            self.assertEqual(result.stdout, "")

    def test_opencode_real_lane_still_resolves(self):
        """Falsification twin: the guard must not swallow a lane that is genuinely fine.

        Stops at the auth check, which sits strictly after lane_setup, so a clean pass
        through lane resolution is proved without acquiring a lease or calling a model.
        """
        with tempfile.TemporaryDirectory() as tmp:
            env = self._stub_env(Path(tmp), "opencode", "echo ALIVE")
            result = self._run(
                "opencode-agent.sh",
                ["--dir", tmp, "--lane", "opencode-go-deepseek-v4-flash",
                 "--prompt", "hi", "--direct"],
                env,
            )
            self.assertIn("OpenCode auth not found", result.stderr)
            self.assertNotIn("unknown lane", result.stderr)
            self.assertNotIn("is not an OpenCode lane", result.stderr)
            self.assertNotIn("does not admit", result.stderr)

    def _fanout_with_stub_wrapper(self, root, wrapper_body, task_fields=None):
        """Run a one-task fan-out against a scripts/ farm with claude-agent.sh stubbed.

        Symlinking the rest of scripts/ keeps fanout.sh, fleetctl.py and roster.sh real,
        so the only thing faked is the worker whose emptiness is under test.
        """
        farm = root / "scripts"
        farm.mkdir(parents=True, exist_ok=True)
        for entry in SCRIPTS.iterdir():
            if entry.name != "claude-agent.sh":
                (farm / entry.name).symlink_to(entry)
        stub = farm / "claude-agent.sh"
        stub.write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\nlast=''\n"
            + f"printf '%s\\n' \"$@\" > {shlex.quote(str(root / 'wrapper-argv'))}\n"
            +
            'while [ $# -gt 0 ]; do case "$1" in --last) last="$2"; shift 2;; *) shift;; esac; done\n'
            + wrapper_body + "\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)

        work = root / "work"
        work.mkdir(parents=True, exist_ok=True)
        tasks = root / "tasks.jsonl"
        tasks.write_text(
            json.dumps({"id": "lens01", "agent": "claude", "prompt": "one lens",
                        "dir": str(work), **(task_fields or {})}) + "\n",
            encoding="utf-8",
        )
        out = root / "out"
        result = subprocess.run(
            [str(farm / "fanout.sh"), str(tasks), "--parallel", "1", "--out", str(out)],
            text=True, capture_output=True, check=False,
            env={
                **os.environ,
                "ACCESS_OVERLAY": str(FIXTURE_OVERLAY),
                "FLEET_STATE_DIR": str(root / "state"),
            },
        )
        summary = (out / "summary.tsv").read_text(encoding="utf-8") if (out / "summary.tsv").exists() else ""
        return result, summary

    # The summary vocabulary is scripts/outcome-taxonomy.sh, not the older
    # PASS/FAIL(empty) pair: a clean exit with nothing in the deliverable is
    # FAILED with the reason named, so the reason is asserted too. A class alone
    # would still pass if every failure collapsed back to one opaque bucket.
    def test_fanout_scores_an_empty_result_file_as_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, summary = self._fanout_with_stub_wrapper(
                Path(tmp), ': > "$last"; exit 0',
            )
            self.assertIn("FAILED\tlens01", summary, result.stderr)
            self.assertIn("missing-or-blank-deliverable", summary, result.stderr)
            self.assertNotIn("SUCCEEDED", summary)

    def test_fanout_still_passes_a_real_result(self):
        """Falsification twin: the fan-out guard must not fail an honest worker."""
        with tempfile.TemporaryDirectory() as tmp:
            result, summary = self._fanout_with_stub_wrapper(
                Path(tmp), 'printf "a real answer\\n" > "$last"; exit 0',
            )
            self.assertIn("SUCCEEDED\tlens01", summary, result.stderr)
            self.assertIn("completed", summary, result.stderr)
            self.assertNotIn("FAILED", summary)

    def test_fanout_passes_role_and_preserves_caller_effort(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result, summary = self._fanout_with_stub_wrapper(
                root, 'printf "a real answer\\n" > "$last"; exit 0',
                {"role": "probe", "effort": "low"},
            )
            self.assertIn("SUCCEEDED\tlens01", summary, result.stderr)
            argv = (root / "wrapper-argv").read_text().splitlines()
            self.assertEqual(argv[argv.index("--role") + 1], "probe")
            self.assertEqual(argv[argv.index("--effort") + 1], "low")


if __name__ == "__main__":
    unittest.main()
