# ChatGPT through Crossfeed Chat

Crossfeed Orchestrator requests read-only text answers from saved ChatGPT workers through Crossfeed Chat. The gateway is an owner-accepted subscription relay: admission requires the owner's dated account-risk acceptance. The selected worker label is recorded, but the underlying provider model identity and usage remain unconfirmed.

Configure the overlay's `chatgpt_gateway.lane_template` with harness `chatgpt-chat`, transport `api_base: "http://127.0.0.1:4319/v1"` and auth `key_file: "~/.config/crossfeed-chat-service/api-key"`. Keep the bearer key in that protected file. The `chatgpt-work` quota pool has no authoritative quota reading and does not inherit Codex usage snapshots.

The ChatGPT card and `fleetctl.py brief` show a rolling 7-day Pro estimate: answered Pro requests plus distinct Pro wake observations returned by the local `/v1/chat/completions` API and retained in `runs.jsonl`. The extension picker receipt supplies `source: "extension"`, `level: 4` and `observed_at`; repeated answers from the same wake count it once. Set `quota_pools["chatgpt-work"].pro_weekly_allowance` to your allowance; the default is 200 and zero disables estimated Pro headroom. Added Crossfeed Chat providers use the same setting on their own quota pool. Wakes without a retained API observation, including failed wakes with no answer, and requests outside the orchestrator are excluded from this estimate. Historical receipts without an observation timestamp cannot be backfilled.

When Pro is switched off, paused by Crossfeed Chat, or its estimated allowance is spent, Pro tasks use a saved current Extra High lane, then High. Both replacements must be admitted and switched on. If neither can answer, the task fails. A picker receipt showing a Pro downgrade or a worker rate-limit report pauses Pro until an explicit reset timestamp or relative reset time, or for 24 hours when unknown. This pause survives restart and applies only to Pro. The final model receipt records the original Pro request, replacement selector, and reason; it does not claim an underlying model identity. A replacement observed below High is refused.

The server's `/v1/models` response supplies each saved worker's `id`, `saved`, model `row`, numeric `level` from 0 to 4, and positive integer `replicas`. A saved label becomes both a lane and selector named `chatgpt:<label>`, with `max_parallel` equal to its replica count. An absent count defaults to one; an invalid count closes admission. The console groups workers configured with the `Latest` model row under Current, and other model rows under Older. There is no orchestrator worker map or fixed model list. A `chatgpt` slot in a routing band's list expands to admitted saved-worker lanes for that role.

```bash
python3 scripts/fleetctl.py chatgpt sync
scripts/chatgpt-agent.sh health --lane chatgpt:my-worker
scripts/chatgpt-agent.sh run --lane chatgpt:my-worker \
  --prompt-file task.md --dir ~/code/project --mode ro \
  --last /tmp/chatgpt-answer.txt
```

Crossfeed Chat wakes a saved sleeping worker when the orchestrator sends its request. Its paired Chrome extension opens a fresh chat, restores the saved model row and thinking level, starts polling, and archives the previous chat. Crossfeed Chat owns wake caps, cooldowns and archive state; `fleetctl.py doctor` reports its live wake status. The orchestrator admits valid sleeping labels and queues callers in arrival order for a lease, a reserved request slot. Each lease attempt reads the current catalog, allowing up to the label's replica count to run concurrently. A count decrease restricts new leases while existing requests finish. Invalid saved settings or an unavailable gateway close admission. The wrapper's `health` command checks gateway admission without starting a wake. The wall timeout includes queue wait and admission checks; cancellation removes the caller's ticket, and abandoned tickets are reclaimed.

Requests go to `/v1/chat/completions` with `model: "chatgpt:<label>"`, text-only input, `tool_choice: "none"` and an `Idempotency-Key`. The default key is the persisted run ID. To retry explicitly, reuse that key with the identical prompt and options. The gateway owns duplicate-request handling; the orchestrator does not automatically retry uncertain delivery. `fleetctl.py dispatch` tries its next selector choice after a worker wake or request failure. A direct wrapper call returns the error to its caller.

Install the [Crossfeed Chat](https://github.com/ABCastor/crossfeed-chat) service and pair its Chrome extension.

Crossfeed Chat runs separately. This repository does not install its service or manage its browser connection.
