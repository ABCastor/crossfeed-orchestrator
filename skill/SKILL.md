---
name: crossfeed-orchestrator
description: >-
  Hand work to other AI coding agents through their official CLIs (Claude Code, Codex, Antigravity,
  OpenCode, GitHub Copilot, OpenRouter's free tier), with optional read-only ChatGPT chat workers through
  Crossfeed Chat, choosing the model from what each subscription
  has left. Use to delegate a task to another model, fan out many tasks in parallel, or convene an
  adversarial council of independent models before a consequential call.
---

# Crossfeed Orchestrator

You lead: design, brief, verify and integrate. Workers use another vendor's official CLI on a folder. Never dispatch this host to itself as an external worker.

Every lead must obey the brief's `lead:` line: delegate tasks through external `dispatch`, keep its own turns short, and tell the owner it is preserving its pool.

Paths are relative to this folder. Load `readers/<harness>.md` once per session: `claude-code`, `codex`, `pi` or `opencode`. Run from the installed skill folder or repo root (parent of `skill/`). Never copy scripts into projects. Read optional `LIVE-RULES.md` once: private operator policy, installer-kept, never in repo. Respect live skill/roster/repo writer locks in the context home.

## Every dispatch: brief, then dispatch

```bash
python3 scripts/fleetctl.py brief
python3 scripts/fleetctl.py dispatch --role review --prompt-file /project/brief.txt --dir /project --mode read-only --last /project/report.md
```

`dispatch` selects pool/model/level, streams final text, passes through wrapper exit, names receipt on stderr. Inspect with `select --role R [--stakes S] --json`; `--family F` selects task/evidence family. `brief`, `select` and `dispatch` detect the lead's pool from the harness environment; pass `--lead POOL` when needed. A pressured lead pool is excluded except for irreversible stakes, which still respect off switches and hard quota gates. Writes: `--mode write` in a fresh worktree; `--dry-run` makes no model call. Selection may lack policy: load `references/routing-by-hand.md` for exceptions and explain overrides.

Stakes: `low` = cheap and reversible; `normal`; `high` = costly to redo; `irreversible` = cannot undo, no price term. Save through routing, never downgrading judgment; reduce parallelism before quality.

Brief, one row per pool:

```text
pool | level | binding window | used | resets | price | models on
off: <disabled models and stand-ins>
pick: fleetctl.py select --role R [--stakes S]
```

`price` is quota price (lambda): zero for projected surplus, fallback for unknown projections. `brief --verbose` gives limits, resets, plans, pins and effort warnings; `brief --json` gives full data. Console wins over every model name. Report confirmed receipt identity; if unconfirmed, report selected identity as selected. Never name a switched-off model in a command or report.

## Pick the mode

State the inferred mode.

| Task | Mode |
|---|---|
| Unsettled fork | Council |
| Decision taken | Worker |
| Breadth | Fan-out |
| Bounded question | Single worker |
| Wide design space | Constraint-diverse generation: each worker gets a different constraint; compare, recommend, propose a hybrid |

Never reopen a settled decision. Council on your own doubt. Same-vendor subagents are never independent reviewers/council seats: use `--exclude-lineage <lead vendor>`.

## Hard rules

- Official CLIs, intended logins, owner's account; recheck terms before sustained automation. API and partner-plan lanes must declare and enforce their authentication class; never infer billing from an API key. Crossfeed Chat is a separate read-only subscription-relay route requiring dated account-risk acceptance, which does not imply vendor permission; see `docs/CHATGPT.md`. `claude -p` never uses `--bare` (API billing). `fleetctl.py doctor` names missing prerequisites.
- Spend levels/switches change only on the owner's word. Never switch a model on yourself or undo an owner limit.
- Free/anonymous lanes, OpenRouter free lanes and research-scout: public/synthetic material only. No private facts, notes, memory, personal/client data, credentials or unreleased names. Citations count only after you open them. OpenRouter checks live free-only pricing before every call.
- Paid pools require explicit admission and a configured daily cap. Read the overlay and optional `LIVE-RULES.md` for spending policy. Copilot is one read-only Auto observer, never swarm, `/fleet`, `/research` or named models.
- UNKNOWN quota fails open for routing; keep frontier lanes single-slot until the gauge is proven. EXHAUSTED and an open quota-error circuit fail closed. `forced` never overrides a known failure or a paid cap. Spend expiring surplus on useful quality-sensitive work; do not start unresumable work that cannot finish before reset. Front-load expensive work on a pool that has just reset.
- Always pass agy `--lane` or `--role`; bare agy uses its settings model.

## Run it

```bash
scripts/fanout.sh tasks.jsonl --parallel 3
scripts/swarm.sh review --dir /project --prompt "Attack PLAN.md against code and tests."
```

Load `references/fanout.md` for tasks/profiles. Dispatch retries a top-three candidate only for identified prelaunch refusals (busy lease/gate), never raw 4/5 alone. Inspect partial edits after a started worker fails.

| Wrapper exit | Meaning |
|---|---|
| 0 | Success; verify deliverable |
| 4 | Claude/Codex/agy: empty output; OpenCode: lease/event JSON failure; Copilot: invalid JSON; OpenRouter: lease failure |
| 5 | Prelaunch refusal (off/switch/quota/cap/no route), or OpenCode session error, Copilot missing/failing result |
| 8 | Receipt/ledger/telemetry failure; output may exist |
| 124 | Wall-clock kill |
| 125 | Idle watchdog kill |

Other codes: wrapper-specific or CLI passthrough; see lane reference. OpenCode 6 = no successful terminal step, 7 = empty output.

| fanout exit | Meaning |
|---|---|
| 0 | Complete |
| 1 | Incomplete; read `summary.tsv` |
| 2 | Bad call/missing tasks |
| 3 | Write path held |
| 4 | Refused, nothing dispatched, no `summary.tsv` |

## The brief contract

One problem, cold-agent brief, named context home per worker. Put deterministic work in code or the brief. Start lean; inject only the needed skill/memory slice. State exact write scope, acceptance check and writable report path inside `--dir`. Create the report first, write incrementally. Require a digest: conclusion, evidence, changed paths, verification. Verify claims before they count.

Prefer disjoint tasks to duplicate opinions. Let `fanout.sh` carry parallelism: never wrap each worker in another expensive lead-pool agent. Never add a management tier: right-size the brief, not a foreman. For every convened opinion, including fan-outs, verification passes and GPT Pro, state the frame and evidence flatly; never pre-argue the answer. A long brief is a bias smell.

Workers see `--dir`, its `AGENTS.md`/`CLAUDE.md` and prompt; OpenRouter sees only the prompt. Never hand a peer or worker work your own session was refused.

## Liveness

Idle watchdog: 2400 seconds by default, output or process-group CPU; HTTP watches stream bytes. No default wall clock except OpenCode's roster `timeout_s`. Never add outer `timeout`/`gtimeout`; use wrapper `--timeout` for a hard budget.

Never detach: no `nohup`, trailing `&`, `disown` or `setsid`. Run foreground inside a harness-tracked tool call (reader file). After 124/125 inspect `git status` and partial reports before relaunching.

## Isolation and writers

Writes need a clean git worktree (required for OpenCode `--write`). One writer per root, disjoint write roots; fanout exit 3 when held. Ignored databases collide across worktrees: use separate databases. `/tmp` does not isolate workers. No secrets in a worker folder. If a harness sandbox blocks `.git` writes or local sockets, the lead commits and runs full suites outside that sandbox.

## Load on demand

Under `references/`, load as needed:

| Trigger | File |
|---|---|
| Selection exception | `routing-by-hand.md` |
| Levels, switches, stand-ins, pins | `controls.md` |
| Gauges, emergency, lead CRITICAL | `quota.md` |
| Tasks, swarm, project wiring | `fanout.md` |
| Council | `council.md` |
| Killed/hung run, wrapper edits | `liveness.md` |
| Untrusted input, other write isolation | `isolation.md` |
| Level override | `effort.md` |
| Setup/admission | `roster-admission.md` |
| Codex details | `lane-codex.md` |
| Scripted `claude -p` | `headless-claude.md` |
| agy | `lane-antigravity.md` |
| OpenCode media/context/scout | `lane-opencode.md` |
| Copilot | `lane-copilot.md` |
| OpenRouter | `lane-openrouter.md` |
| Images/audio/video | `media.md` |

For away windows or delegated decisions, load optional installer-kept live-only `references/afk-mode.md` or `references/delegated-decisions.md`. Never invent operator policy. Quota-refresh edits need engine `docs/QUOTA-REFRESH.md`.

## Changing the scripts

Touched `scripts/`? Run `tests/run-all.sh`. Keep the idle watchdog/default here for `scripts/check-dispatch-invariants.sh`.
