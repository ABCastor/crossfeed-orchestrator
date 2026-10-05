<img src="docs/readme-header.svg" width="500" alt="Crossfeed Orchestrator">

Crossfeed Orchestrator routes AI coding work to the right provider and model across your subscriptions, then supervises each worker.

Your local console controls which models agents may use and how freely they spend each plan. Routing reads those settings before every job.

[![Tests](https://github.com/ABCastor/crossfeed-orchestrator/actions/workflows/tests.yml/badge.svg)](https://github.com/ABCastor/crossfeed-orchestrator/actions/workflows/tests.yml)

## Install

You need Python 3.9+, bash, [jq](https://jqlang.org) and your chosen vendor CLIs, installed and signed in. No extra Python packages.

```bash
git clone https://github.com/ABCastor/crossfeed-orchestrator ~/crossfeed-orchestrator
cd ~/crossfeed-orchestrator
mkdir -p ~/.config/orchestrator
cp examples/access-overlay.example.json ~/.config/orchestrator/access-overlay.json
python3 scripts/fleetctl.py doctor
python3 scripts/fleetctl.py console
```

The *overlay* is your machine's roster of plans, models and roles. Replace the example settings with your own. Keep only signed-in, tested lanes `verified`; mark the others `unverified`. `doctor` reports setup gaps and exits nonzero until you fix them. Before dispatch, verify the example thinking-level settings and record their evidence dates. [Installation details](docs/USAGE.md#install), [agent skill setup](docs/SKILL-INSTALL.md) and [Linux setup](docs/LINUX.md).

## Give it work

```bash
python3 scripts/fleetctl.py brief
python3 scripts/fleetctl.py select --role implementation --json
python3 scripts/fleetctl.py dispatch --role review --mode read-only \
  --prompt "Review the retry paths and report concrete defects." --dir "$PWD" \
  --last /tmp/review.txt
```

A *lane* is a model on a tool, funded by a quota pool. Crossfeed selects a lane and thinking level, reserves its capacity and runs it under an idle watchdog. Each run records the selected model; provider identity is confirmed only when the provider supplies it.

## Documentation

- [Demo console](docs/DEMO.md), [quota refresh](docs/QUOTA-REFRESH.md) and [benchmark harness](bench/README.md).
- [Usage guide](docs/USAGE.md) and [portable examples](docs/PORTABLE-USAGE.md): routing, wrappers, parallel jobs, spend levels, model switches, providers and quota sources.
- [Selector](docs/SELECTOR.md), [thinking levels](docs/EFFORT.md) and [adapter contracts](docs/ADAPTERS.md).
- [ChatGPT relay setup and limits](docs/CHATGPT.md) and [API client setup](docs/LANES-API.md).
- [Agent operating manual](skill/SKILL.md): briefs, isolation, supervision and verification.

Works with [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex](https://github.com/openai/codex), [Antigravity](https://antigravity.google), [OpenCode](https://opencode.ai), [Copilot CLI](https://github.com/github/copilot-cli) and [OpenRouter](https://openrouter.ai). [Pi](https://github.com/earendil-works/pi) supports admitted API and partner-plan routes.

## Limits and tests

The selector has not yet been proven better than simpler routing policies on hard tasks. Use the [benchmark harness](bench/README.md) to measure your own workloads.

Missing quota measurements stay unknown. The Copilot wrapper runs one read-only Auto observer at a time; Antigravity requires write mode. ChatGPT relay lanes need dated acceptance of account risk, and their underlying model identity remains unconfirmed. [Full status and limits](docs/USAGE.md#status-and-limits).

```bash
bash tests/run-all.sh
```

The suite uses fake CLIs and synthetic data. It does not prove live provider access or the correctness of an agent's answer.

## Upstream credit

Credit to upstream contributor: Rocco Angelella's [PiLink](https://github.com/roccoangelella/PiLink) introduced the ChatGPT gateway approach. Crossfeed Chat implements it separately.

## Licence

The code is [MIT](LICENSE). The artwork and bundled fonts have separate terms in [NOTICE](NOTICE).

<p>
  <a href="https://abcastor.com"><img src="docs/castor-footer.svg" width="350" alt="Chip, the Castor beaver, by Castor, we give a dam"></a>
</p>
