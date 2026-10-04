# Install the agent skill

Use Python 3.9 or newer, bash and jq. Install and sign in to the vendor CLIs you intend to use, then copy the example overlay into `~/.config/orchestrator/access-overlay.json`. Keep only your configured lanes verified. Run `python3 scripts/fleetctl.py doctor` to identify missing prerequisites.

The skill needs its references and harness readers beside `SKILL.md`, plus the repository scripts. For a checkout at `~/crossfeed-orchestrator`:

```bash
mkdir -p ~/.claude/skills/crossfeed-orchestrator
ln -s ~/crossfeed-orchestrator/skill/SKILL.md ~/.claude/skills/crossfeed-orchestrator/SKILL.md
ln -s ~/crossfeed-orchestrator/skill/references ~/.claude/skills/crossfeed-orchestrator/references
ln -s ~/crossfeed-orchestrator/skill/readers ~/.claude/skills/crossfeed-orchestrator/readers
ln -s ~/crossfeed-orchestrator/scripts ~/.claude/skills/crossfeed-orchestrator/scripts
ln -s ~/crossfeed-orchestrator/docs ~/.claude/skills/crossfeed-orchestrator/docs
```

For another harness, make the same layout in its skills directory and use the corresponding file in `skill/readers/`. Agents can also read `skill/SKILL.md` directly from the checkout. Commands run from the checkout root or the installed skill directory.

## Upgrading an existing installation

Keep one installation and point harness links at it. Preserve optional `LIVE-RULES.md` and other operator-owned files byte-for-byte. They hold private policy and stay outside this repository. Do not replace unknown local files or remove legacy links without inspecting them.

Stage changes beside the target, verify reference and script paths, run `bash tests/run-all.sh` with synthetic configuration, and compare source hashes before replacement. Keep a recoverable old tree and read back installed hashes and links afterward. `doctor` checks adapter/version/identity drift; it does not prove installed skill source parity.

Quota source behavior is documented in [Quota refresh](QUOTA-REFRESH.md). The optional Claude Code model-switch hook is `scripts/hooks/model-switch-guard.py`, registered as a `PreToolUse` hook for `Bash|Agent|Task`.
