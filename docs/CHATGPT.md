# ChatGPT through Crossfeed Chat

Crossfeed Orchestrator requests read-only text answers from saved ChatGPT workers through Crossfeed Chat. The gateway is an owner-accepted subscription relay: admission requires the owner's dated account-risk acceptance. The selected worker label is recorded, but the underlying provider model identity and usage remain unconfirmed.

Configure the overlay's `chatgpt_gateway.lane_template` with harness `chatgpt-chat`, transport `api_base: "http://127.0.0.1:4319/v1"` and auth `key_file: "~/.config/crossfeed-chat-service/api-key"`. Keep the bearer key in that protected file. The `chatgpt-work` quota pool has no authoritative quota reading and does not inherit Codex usage snapshots.

The ChatGPT card and `fleetctl.py brief` show a rolling 7-day Pro estimate: answered Pro requests in `runs.jsonl` plus retained Pro wakes that sent the polling prompt. Set `quota_pools["chatgpt-work"].pro_weekly_allowance` to your allowance; the default is 200 and zero disables estimated Pro headroom. Added Crossfeed Chat providers use the same setting on their own quota pool. Usage outside the orchestrator and wake records trimmed from the retained log are absent from this estimate.

When Pro is switched off, paused by Crossfeed Chat, or its estimated allowance is spent, Pro tasks use a saved current Extra High lane, then High. Both replacements must be admitted and switched on. If neither can answer, the task fails. A picker receipt showing a Pro downgrade or a worker rate-limit report pauses Pro until an explicit reset timestamp or relative reset time, or for 24 hours when unknown. This pause survives restart and applies only to Pro. The final model receipt records the original Pro request, replacement selector, and reason; it does not claim an underlying model identity. A replacement observed below High is refused.

The server's `/v1/models` response supplies each saved worker's `id`, `saved`, model `row` and numeric `level` from 0 to 4. A saved label becomes both a lane and selector named `chatgpt:<label>`. The console groups workers configured with the `Latest` model row under Current, and other model rows under Older. There is no orchestrator worker map or fixed model list. A `chatgpt` slot in a routing band's list expands to admitted saved-worker lanes for that role.

```bash
python3 scripts/fleetctl.py chatgpt sync
python3 scripts/fleetctl.py chatgpt wake my-worker
scripts/chatgpt-agent.sh health --lane chatgpt:my-worker
scripts/chatgpt-agent.sh run --lane chatgpt:my-worker \
  --prompt-file task.md --dir ~/code/project --mode ro \
  --last /tmp/chatgpt-answer.txt
```

Wake opens a fresh ChatGPT tab through Gaddi, restores the saved model row and numeric thinking level, then asks the worker to poll. The previous chat enters the archive queue. One `wake-state.json` stores current chats, pending archives, rolling caps and cooldowns. The waker retains its daily cap, label-failure cooldown, global rate-limit cooldown, read retries and stall handling.

`/v1/gateway/status` reports worker `label`, contact freshness, `polling` and `processing_claim`. A valid saved worker can be asleep before its first wake. Invalid model settings or an unavailable gateway close admission. A worker is healthy while processing a claim, or when its contact is recent and it is polling. Concurrent callers to the same saved worker wait in a process-safe first-in, first-out queue. The wall timeout includes that wait; cancellation removes the caller's ticket, and abandoned tickets are reclaimed. The per-worker lease remains the final overlap guard.

Requests go to `/v1/chat/completions` with `model: "chatgpt:<label>"`, text-only input, `tool_choice: "none"` and an `Idempotency-Key`. The default key is the persisted run ID. To retry explicitly, reuse that key with the identical prompt and options. The gateway owns duplicate-request handling; the orchestrator does not automatically retry uncertain delivery. `fleetctl.py dispatch` tries its next selector choice after a worker wake or request failure. A direct wrapper call returns the error to its caller.

Install the [Crossfeed Chat](https://github.com/ABCastor/crossfeed-chat) service and put the Gaddi CLI on PATH, or set `gaddi_cli` in the lane template to its executable path.

Crossfeed Chat runs separately. This repository does not install its service or manage its browser connection.
