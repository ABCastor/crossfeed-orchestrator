# Crossfeed usage guide

[Back to the README](../README.md)

- [How routing works](#how-routing-works)
- [Install and admit your lanes](#install)
- [Run workers](#use-it)
- [Spend levels and model switches](#spend-levels-and-model-switches)
- [Add a provider](#add-a-provider)
- [Quota sources](#quota-sources)
- [Status, limits and tests](#status-and-limits)

Crossfeed Orchestrator decides which AI coding agent, model and subscription should take a job, then runs it through that vendor's own command-line tool. It is for people who pay for several AI coding plans (Claude, ChatGPT with Codex, Google's Antigravity, OpenCode Go, GitHub Copilot) and want their agents to use all of them well, instead of exhausting one while the others sit idle.

In aviation, crossfeed lets an engine draw from any tank so the fuel stays balanced.

It is a set of Python and bash scripts plus a skill for [Claude Code](https://docs.anthropic.com/en/docs/claude-code).  The console and API server run only when you start them.

## Official CLIs and an optional ChatGPT relay

The coding lanes run through the vendor's official binary, signed in the way the vendor intends: `claude -p`, `codex exec`, `agy --print`, `opencode run`, `copilot -p`, and OpenRouter's documented HTTP API for its free models.

An optional read-only ChatGPT route uses Crossfeed Chat, a separate local gateway at `http://127.0.0.1:4319/v1`. It relays requests through saved browser workers and requires the owner's dated acceptance of subscription-relay account risk. The server supplies the saved model rows and numeric thinking levels; Crossfeed Chat wakes sleeping workers through its paired Chrome extension, and the orchestrator records their selected labels. Set `chatgpt_gateway.pi_coding` to `true` to add Pi coding routes with local tools for current High and Extra High workers. These share the ChatGPT account and model switches; Pi's `provider-default` evidence stays separate from plain Chat requests. See [ChatGPT setup](CHATGPT.md).

One optional exception: [CodexBar](https://github.com/steipete/CodexBar), a third-party app that reads how much of each plan you have used. Crossfeed can read its output. CodexBar gets those numbers by reusing your sign-ins or browser cookies, so use it at your own risk; the other quota sources below need nothing but files and commands you control.

## How routing works

**Routes by role.** You declare *lanes* (a model, on a CLI, funded by a quota pool, allowed certain jobs) and *roles* (`implementation`, `review`, `repo-map`, `hard-reasoning` and so on).

**Steps down under pressure instead of failing.** Each role has a ranked list per pressure band. As a pool tightens, routing walks down the list and cuts parallelism before quality.

**Respects every binding limit.** A weekly window at 99% constrains the pool even if its 5-hour window is almost unused. At 90% actual usage, a limit stays critical until reset; no other window can produce a spare-capacity suggestion. Below that threshold, forecasts can relax conservation when allowance would expire unused. Selector pricing uses the greater of actual usage and projected usage for each applicable window. A model-specific limit constrains the models it names.

**Fails open when it cannot measure.** No quota source means no measurement, and no measurement is not evidence of exhaustion: routing runs at full quality. A pool that is measured as exhausted still stops.

**Holds leases and supervises every run.** Lanes have concurrency caps taken and released atomically, with dead-process detection. Every worker runs under an idle watchdog instead of a wall clock: it is killed only after a long stretch with no output and no CPU, and its whole process group goes with it.

**Shows it all on one local page.** `fleetctl.py console` opens a page on 127.0.0.1 with every plan's limits and reset times, a switch for each model and a spend level for each pool.

## Install

You need Python 3.9 or newer, bash and [jq](https://jqlang.org), plus the CLIs you want to route to, installed from their vendors' own instructions (linked under [Works with](#works-with)) and each signed in with its own login; [roster admission](../skill/references/roster-admission.md) lists the sign-in commands. No Python packages beyond the standard library.

```bash
git clone https://github.com/ABCastor/crossfeed-orchestrator ~/crossfeed-orchestrator
cd ~/crossfeed-orchestrator
mkdir -p ~/.config/orchestrator
cp examples/access-overlay.example.json ~/.config/orchestrator/access-overlay.json
python3 scripts/fleetctl.py doctor
```

`doctor` lists which CLIs are on your PATH, which pools have no quota source and which lanes you have not verified, and ends with the number of things to fix. It exits nonzero when setup gaps remain. The example thinking-level evidence has placeholder dates; verify the controls, record the source and date in `effort`, and rerun `doctor` before dispatch. The copied file is your *overlay*: the roster of lanes, pools, plans and roles for your machine.  Most are marked `"access_status": "verified"` for illustration; the Pi candidate starts unverified. Before you dispatch real work, keep `verified` only on lanes whose CLI you have signed in to and tried, and set the rest to `unverified`: the router refuses any lane that is not verified, so it never picks a tool you do not have.

To give Claude Code the skill, link it into your skills folder:

```bash
mkdir -p ~/.claude/skills/crossfeed-orchestrator
ln -s ~/crossfeed-orchestrator/skill/SKILL.md ~/.claude/skills/crossfeed-orchestrator/SKILL.md
ln -s ~/crossfeed-orchestrator/scripts ~/.claude/skills/crossfeed-orchestrator/scripts
ln -s ~/crossfeed-orchestrator/skill/references ~/.claude/skills/crossfeed-orchestrator/references
ln -s ~/crossfeed-orchestrator/skill/readers ~/.claude/skills/crossfeed-orchestrator/readers
ln -s ~/crossfeed-orchestrator/docs ~/.claude/skills/crossfeed-orchestrator/docs
```

Other agents can read `skill/SKILL.md` as plain instructions and load their file from `skill/readers/`. [Skill installation](SKILL-INSTALL.md) covers preserving operator files when refreshing an existing install. Optionally, register `scripts/hooks/model-switch-guard.py` as a Claude Code `PreToolUse` hook (matcher `Bash|Agent|Task`), so an agent that tries to start a model you switched off is told what to run instead.

On Linux, read [Linux setup](LINUX.md): what has been tested there, where quota numbers come from, and a systemd timer for the daily model-data refresh.

## Use it

```bash
python3 scripts/fleetctl.py route --role implementation   # prints one lane id, e.g. opencode-go-kimi-k2.7-code
python3 scripts/fleetctl.py brief                         # compact pool table, switched-off models, selection command
python3 scripts/fleetctl.py brief --verbose               # every limit and reset, plans, pins, effort warnings
python3 scripts/fleetctl.py dispatch --role implementation --prompt-file task.md --dir ~/code/app-worktree
python3 scripts/fleetctl.py usage                         # every quota window and where it will land
python3 scripts/fleetctl.py console                       # the same on a local page

scripts/codex-agent.sh --prompt "Add type hints to utils.py" --dir ~/code/app-worktree
scripts/opencode-agent.sh --prompt "Map the retry paths" --dir ~/code/app --role repo-map --read-only
scripts/fanout.sh tasks.jsonl --parallel 4 --out /tmp/campaign-1   # many workers, one preflight
```

The compact brief has one row per pool: `pool | level | binding window | used | resets | price | models on`, then `off:` and `pick:` lines. `price` is the selector's quota price (lambda), which rises when actual or projected usage exceeds its target. It is not the subscription fee. Unknown projections use the selector's configured fallback price; rendering an overview without selector context shows `-`. `brief --json` keeps the complete overview structure, including every limit.

Thinking levels come from the overlay's `effort` table. `route --json` includes the level; wrappers pin it and announce its source.  See [thinking-level configuration](EFFORT.md) for roles, overrides and recheck warnings.

Each wrapper prints the worker's final message on stdout and its model receipt on stderr, and exits 0 on success; the exit codes for kills, refusals and empty answers are listed at the top of each script. `dispatch` selects and runs one wrapper, streams its final text, and names the receipt and diagnostic files on stderr. [the skill](../skill/SKILL.md) is the operating manual: modes, the brief contract, isolation and liveness.

### Use your lanes from an API client

`python3 scripts/fleetctl.py serve-api --key-file /path/to/key --dir /path/to/workspace` exposes admitted read-only text models at `http://127.0.0.1:4320/v1`. `/v1/models` lists available model IDs, and `/v1/chat/completions` dispatches through the existing selector and wrappers. Pick a listed model or `crossfeed:auto`. See [the API guide](LANES-API.md) for bearer key setup, supported fields and limits.

### Spend levels and model switches

Each quota pool has one setting for how much of that plan to spend:

| Level | Routing |
|---|---|
| `off` | never used; every wrapper refuses the pool |
| `low` | cheapest capable lane first, one call at a time across the pool; a big model only through `route --one-shot` |
| `normal` | the quota bands decide (the default) |
| `high` | strong models freely: no step-down until the pool is critical |
| `forced` | used even when the quota looks low; the provider's own quota error and a paid pool's daily cap still bind |

```bash
python3 scripts/fleetctl.py level opencode-go low
python3 scripts/fleetctl.py model-toggle claude claude-opus-5-5 off   # "on" brings it back
```

Every model a pool can run has an on/off switch, and the switches are yours: agents read them, and the optional hook stops an agent from switching a model back on. With several on, Crossfeed picks the best one that is on for each task; with one on, every run uses it; with none on, the pool is not used. When a task names a model that is off, the Claude and Codex wrappers run the nearest current model that is on. Every wrapper tells its worker the selected model and returns a receipt with what was requested, what was selected, and what the provider reported. The same receipt lands in the local run ledger and the console's recent runs. If a CLI does not expose its underlying model, the receipt says it is unconfirmed.

Older models default to off unless their roster card records a comparative advantage for a named job. The console shows the reason beside the switch. A generic job description such as "repo maps" does not qualify; a record saying it is faster than the current model on your repo-map test does. See [the retention record format](../skill/references/controls.md).

```bash
python3 scripts/fleetctl.py older-models list                   # older models, on/off and why
python3 scripts/fleetctl.py older-models apply --dry-run        # preview which legacy defaults to switch off
python3 scripts/fleetctl.py older-models apply                  # persist those off switches; never turns anything on
```

### Add a provider

Open **Add provider** in the console, or use the CLI. Compatible APIs run through Pi; Crossfeed Chat uses its existing relay checks. API and CLI models stay unverified until you review their access, roles and effort in the overlay.

```bash
python3 scripts/fleetctl.py provider probe --base-url https://api.example.com/v1 --key-ref MY_API_KEY
python3 scripts/fleetctl.py provider add my-api --base-url https://api.example.com/v1 --key-ref MY_API_KEY --models model-id
python3 scripts/fleetctl.py provider list
python3 scripts/fleetctl.py provider remove my-api
```

Keys can reference an environment variable, an absolute file path or `op://vault-id/item/field`. Use `--key-stdin` to paste a key through stdin. Writes keep overlay backups. Compatible APIs default to a zero daily spend cap; set `--daily-cap` deliberately and enforce billing limits at the provider, because discovered models have no known pricing.

### Quota sources

Each pool's numbers come from the source you name in the `oracle` field of its `quota_refresh` block:

| Source | Use it for |
|---|---|
| `command` | any program that prints the JSON below; the portable choice |
| `file` | a JSON file another process keeps fresh |
| `http` | a self-hosted endpoint that reports its own usage |
| `codexbar` | the optional third-party CodexBar reader (macOS app, Linux CLI) |

```json
{"windows": {"weekly": {"used_percent": 42, "reset_at": "2026-10-11T07:00:00Z", "window_minutes": 10080}}}
```

or `{"available": false, "reason": "not logged in"}`. `window_minutes` lets the router estimate a burn rate; `will_last_to_reset` and `eta_seconds` take precedence when your source computes them. `scripts/oracle-codex-local.py` is a working `command` source for Codex that needs no extra software: it reads the rate-limit records Codex already writes into its own session files.

## Works with

[Claude Code](https://docs.anthropic.com/en/docs/claude-code), [OpenAI Codex CLI](https://github.com/openai/codex), [Google Antigravity](https://antigravity.google), [OpenCode](https://opencode.ai) with an OpenCode Go plan, [GitHub Copilot CLI](https://github.com/github/copilot-cli), and [OpenRouter](https://openrouter.ai)'s free models.

## Upstream credit

See the [upstream credit in the README](../README.md#upstream-credit).

## Status and limits

On 5 October 2026, synthetic coding tests passed 12/12 for Sol High, 9/11 for ChatGPT High through Pi and 9/10 for ChatGPT Extra High through Pi. Only 23/36 ChatGPT cells were measured, none of its nine calibration cells ran, and its underlying model identity is unconfirmed. Sol High remains the default; admitted Pi relay workers can provide fallback capacity under quota pressure.

 Earlier test suites passed on Debian 12 with Python 3.9 and 3.13; the current expanded suite awaits a fresh Linux run; the real vendor CLIs have not yet been run on Linux (see [Linux setup](LINUX.md)). Some wrappers are shaped by one plan's rules: the Copilot wrapper assumes Copilot's Auto model and a small monthly credit allowance, so it runs one read-only observer at a time.

## Tests

```bash
bash tests/run-all.sh
```

 The wrapper suites drive fake CLIs that stall, stream, ignore signals and lie about prices, and assert the real exit codes; `scripts/sabotage-check.sh` reintroduces each past defect to prove the static checks can still fail.

## Licence

The code is Apache 2.0, see [LICENSE](../LICENSE). The names, the logos, the beaver mascot and the signature artwork are not covered by that licence, and the bundled fonts keep their own (SIL Open Font License); [NOTICE](../NOTICE) has the details.
