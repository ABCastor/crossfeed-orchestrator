Load once per session when Pi is the lead harness.

# Pi reader

Pi's bash blocks and has no background mode or default timeout. For parallel external work, issue one foreground fanout.sh call rather than detaching several shell processes. Never pass Pi bash timeout as an outer wall clock; intentional worker budgets belong in wrapper/task options. Keep all work tracked until the call returns.

One worker: run `python3 scripts/fleetctl.py dispatch ... --last <dir>/report.md` in the foreground. Pi does not wait for backgrounded children: a trailing `&` returns immediately and the worker runs unseen and untracked. Pressing Esc to abort Pi's bash call kills every worker in that call; treat it as a kill: inspect `git status` and partial reports before relaunching.

Pi truncates large output to its tail. Give workers unique --last paths inside their working roots, then read complete files and receipt sidecars. For fanout read status first, summary.tsv and each task digest; refusal 4 wrote no summary. An incomplete profile warning cannot be hidden by truncation or an exit 0.

The lead's provider determines lineage, not the word Pi. Pass --exclude-lineage with that provider (for example openai for an OpenAI lead). Never run the same provider as its independent reviewer or council seat, and never self-dispatch Pi as an external copy. Distinct-lens native work remains same-lineage when it uses the lead's vendor.

Pi resolves document paths relative to the skill folder. Use the canonical existing skill link; an extra orchestrator link/name can create duplicate discovery. Load only this reader and the reference needed by the current job. Phase C acceptance requires a real Pi three-worker fanout proving this foreground/report workflow; static docs and tests do not establish that live smoke.
