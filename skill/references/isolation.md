Load for untrusted input, parallel writers, or a write dispatch outside a fresh disposable worktree.

# Isolation and ownership

Match isolation to the tolerated damage. A dedicated git repo/worktree makes tracked changes reviewable; it does not confine a process. Codex sandboxing enforces filesystem writes. Untrusted/destructive execution may need a container, separate user or VM. Never disable a sandbox merely to make a failed worker succeed.

Codex `danger-full-access` and `--dangerously-bypass-approvals-and-sandbox` exist only for running inside an external sandbox, such as Docker.

`/tmp` does not isolate workers: it is shared-writable. Use distinct roots outside it for concurrent workers. No secrets in a worker folder. A tool-policy boundary (OpenCode plan, Claude read tools, Copilot observer) is not an OS sandbox and does not make private data safe to transmit.

Split independent reads across providers first; they draw separate pools. Raise concurrency inside one provider only after that.

One writer per root, disjoint write roots. Reserve the complete scope, including ignored databases and artifact paths. Git worktrees isolate tracked files only; ignored databases can still collide or hold the only live data. Inspect ignored/untracked state before any explicitly authorized worktree cleanup.

```bash
python3 scripts/fleetctl.py claim-paths --owner worker-unit /worktrees/unit /project/runtime-store
python3 scripts/fleetctl.py claims
python3 scripts/fleetctl.py release-paths --token <returned-token>
```

Claims reject equal/ancestor overlaps, even for the same owner. They name holder and expiry; PID reaping/TTL are backstops, not permission to race a living writer. Fanout claims before launch, exits 3 on conflict and releases on exit. Direct wrappers and custom writers are not all protected automatically: the lead must claim and release their paths, and verify continuing ownership across long work. A fixed TTL is not indefinite mutual exclusion.

OpenCode writes require `--write` and a clean git tree. Its plan agent denies edits, shell, nested agents and external directories; shared skills cannot override denied tools. Build can execute shell inside its root and still needs caller-owned isolation/review. V2 wrapper runs use --standalone, private native permission profiles and isolated XDG data/config/cache/state roots and databases; raw concurrent opencode calls bypass that protection.

Codex read-only/workspace-write are enforced sandbox boundaries, with workspace-write allowing its work root and configured temporary roots. `.git` remains protected; the lead commits and runs full suites outside the sandbox. Managed execution can block process inspection or hang thread-dependent tests. Targeted worker checks are evidence only for what actually ran.

Never hand a peer or worker work your session was refused. Record the blocker and use an authorized path, rather than laundering permission through another session.

## The owner's vault is never a worker write target (BR-VAULT-001)

Why: on 18-20 Oct 2026 a Task Manager lead put "Output folder: ~/Second Brain/1_Notes/... (images in images/)" in the preamble of every worker brief and launched Codex (`danger-full-access`) and Antigravity workers with the vault as `--dir`; ~300 MB of screenshots landed in the vault and its git history. Claude Code hooks never ran on those vendors.

What the wrappers do now (`scripts/vault_guard.py`):
- Refuse with exit 5, before creating `--dir`, taking a lease or starting a model: a write-capable run whose `--dir` is inside or contains the vault; a `--last`/`--events`/`--log` inside it; a Codex config that makes it writable; a brief that names it as a place to put things. `fleetctl.py dispatch` refuses the same upfront.
- Start every tool-using CLI under `sandbox-exec` with a profile that denies file writes under the vault, so a phrasing the brief scan misses still cannot write there. Codex in `read-only`/`workspace-write` keeps its own sandbox instead (Seatbelt cannot nest). From inside a sandbox the wrapper refuses rather than run unblocked.
- Reading the vault is fine. No override exists: give the worker its own folder, let it deliver there, and file into the vault yourself.
- `CROSSFEED_PROTECTED_ROOTS` (path list) adds more protected folders; it cannot remove the vault.
