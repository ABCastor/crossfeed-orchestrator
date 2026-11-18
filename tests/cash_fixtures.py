"""Synthetic owner settings and ledger setup; never touches the real home."""
import hashlib
import json


ACCOUNT = "fixture-cash"


def envelope(**overrides):
    return {"per_call_usd": "5", "daily_usd": "5", "pilot_usd": "20",
            "expires_at": "2099-01-02T00:00:00Z", **overrides}


def configure(roster, endpoint, **limits):
    roster["cash_accounts"] = {ACCOUNT: envelope(**limits)}
    for lane in roster["lanes"]:
        if lane.get("paid_mimo"):
            lane["paid_route"] = {
                "profile": "catalog-sse-v1", "endpoint": endpoint, "account": ACCOUNT,
                "model": lane["selector"], "provider": "fixture-upstream", "roles": lane["roles"],
                "approved_prompt_sha256": [hashlib.sha256(b"Return PONG").hexdigest()],
                "quality_floors": {"reasoning": .65, "repo-qa": .65},
                "tariff": {"input": "1000", "output": "1000", "context_tokens": 100,
                           "max_output_tokens": 100, "max_input_bytes": 4000,
                           "source": "synthetic catalog", "quoted_at": "2026-01-01T00:00:00Z",
                           "valid_until": "2099-01-02T00:00:00Z"},
            }
    roster["quota_pools"]["openrouter-mimo"].pop("daily_usd_cap", None)


def seed(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": "crossfeed-cash-budget/v1", "accounts": {ACCOUNT: {"calls": {}}}}))
    path.chmod(0o600)


def seed_reasoning(state_dir, *models):
    path = state_dir / "evidence/levels.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"rows": [
        {"model_key": model, "level": "service-chosen",
         "q": {"reasoning": {"mean": .9, "sd": .01, "n_sources": 1}}}
        for model in models
    ]}))
