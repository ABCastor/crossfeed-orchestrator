# Local lanes API

Use the orchestrator's admitted subscription lanes from a client that speaks [OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat). The server binds to `127.0.0.1` only. Installing Crossfeed does not start a service.

## Start the server

Provide a bearer key in `CROSSFEED_API_KEY` or a file owned by you with permissions `0600`. There is no key argument, and startup never prints the key. To generate a fresh file without displaying its contents:

```bash
python3 - <<'PY'
import os, secrets
from pathlib import Path
path = Path.home() / '.config/orchestrator/lanes-api.key'
path.parent.mkdir(parents=True, exist_ok=True)
with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as file:
    file.write(secrets.token_urlsafe(32) + '\n')
PY
python3 scripts/fleetctl.py serve-api \
  --key-file ~/.config/orchestrator/lanes-api.key \
  --dir /path/to/workspace
```

The default base URL is `http://127.0.0.1:4320/v1`. Use `--port` to change it, `--role` to choose the selector role (default: `review`), and the existing global `--overlay` and `--state-dir` flags to select fleet configuration. Stop with Ctrl-C. The worker directory is server configuration, so a caller cannot change it in a request.

## Configure a client

Set its OpenAI-compatible base URL to `http://127.0.0.1:4320/v1` and its API key to your bearer key. Both `GET /v1/models` and `POST /v1/chat/completions` require `Authorization: Bearer <key>`. Model listing follows the [OpenAI list format](https://developers.openai.com/api/reference/resources/models/methods/list).

Pick an ID returned by `/v1/models`, such as `codex:gpt-6.1-sol`, `claude:<model>`, `opencode:<model>` or `chatgpt:<worker>`. `crossfeed:auto` lets the selector choose and use its ranked fallbacks. An explicit ID restricts the selector to that model's admitted effort levels. The existing direct wrappers can still substitute a model if you switch the requested one off between selection and launch; the response and model receipt report the wrapper's selection. Retirement rules, role policy, quota pressure, capacity and spend caps still apply. The catalog refreshes on each request, so a listed model can become unavailable before dispatch. AGY remains excluded because its wrapper has no proven read-only boundary.

This standard-library example reads the key without putting it in a shell argument or printing it:

```python
import json
from pathlib import Path
from urllib.request import Request, urlopen

key = (Path.home() / '.config/orchestrator/lanes-api.key').read_text().strip()
request = Request('http://127.0.0.1:4320/v1/chat/completions',
                  data=json.dumps({
                      'model': 'crossfeed:auto',
                      'messages': [{'role': 'user', 'content': 'Explain the purpose of this project.'}],
                  }).encode(),
                  headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
with urlopen(request, timeout=900) as response:
    completion = json.load(response)
print(completion['choices'][0]['message']['content'])
```

## Supported requests and limits

The request accepts `model`, `messages`, optional `stream` and optional `n=1`. Message roles are `system`, `developer`, `user` and `assistant`; content is a string or a list of text parts. A nonempty user message is required. The conversation is serialized into one wrapper prompt with its message roles preserved. Each call starts a fresh worker; the API keeps no conversation session.

Every dispatch uses read-only text mode through the existing wrappers, including their admission gates, quota leases, local ledger and selection/model receipts. Read-only is the wrapper's workspace boundary; inherited MCP tools and other external capabilities follow its existing configuration. This API does not add a separate tool sandbox. Responses carry the standard completion fields plus `crossfeed` metadata with the requested model, dispatch ID, receipt paths and available model identity. The response's `model` identifies the selected lane. An underlying provider model is reported only when confirmed by its matching model receipt. Token usage is omitted because the wrappers do not supply a uniform authoritative count.

`stream=true` returns buffered SSE: one content chunk, a stop chunk, then `[DONE]`, after the worker finishes. It does not stream live tokens. Set a client timeout long enough for your chosen wrapper. Disconnecting does not cancel an already launched worker; its normal watchdog and quota lease rules continue to apply.

Tools, tool messages, images, audio, write mode, multiple choices and generation controls such as `temperature` or `max_tokens` are refused with a JSON error. A client must allow these fields to be omitted. Clients that require tool calling need a different interface. Bodies are limited to 64 KiB to stay below portable wrapper argument limits. Authentication errors return 401, invalid requests 400, unavailable explicit models 404, admission/refusal errors 429, and worker failures 502. Configuration failures return a generic 500 without exposing request contents or credentials.

## Verify

```bash
python3 -m unittest tests.test_lanes_api
bash tests/run-all.sh
```

The API suite starts the actual CLI server on an ephemeral loopback port with an isolated roster and fake wrappers. It exercises authentication, catalog admission, JSON and SSE responses, literal message content, constrained selection, live switches, quota leases, receipts and failures. A real Codex wrapper with a fake CLI verifies replacement identity and the ledger without consuming a live subscription.
