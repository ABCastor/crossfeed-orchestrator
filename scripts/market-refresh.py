#!/usr/bin/env python3
"""Refresh the model market catalog from public machine-readable sources.

Sources (all no-auth, fetched best-effort; one failing source never kills the run):
  - models.dev api.json      -> specs, pricing, modalities, context (the feed OpenCode itself uses)
  - OpenRouter /api/v1/models -> pricing/availability cross-check
  - Aider polyglot leaderboard YAML -> coding role prior
  - SWE-bench leaderboards.json     -> agentic-coding role prior

Output is host-local state, never git-tracked and never fingerprinted:
  ~/.local/state/orchestrator/market/market-catalog.json
  ~/.local/state/orchestrator/market/market-report.md

Catalog scores are PRIORS about a model, never verdicts about your route:
Go's serving provider and quantization are undisclosed, so a public benchmark
ranks the weights, not the lane.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import urllib.request
from pathlib import Path
try:
    import daily_model_refresh as daily
    import decide
    from evidence import current_aa_variants, load_lmarena, write_evidence
except ModuleNotFoundError:
    from scripts import daily_model_refresh as daily
    from scripts import decide
    from scripts.evidence import current_aa_variants, load_lmarena, write_evidence

STATE_DIR = Path(
    os.environ.get("FLEET_STATE_DIR", "~/.local/state/orchestrator")
).expanduser() / "market"
OVERLAY = Path(
    os.environ.get("ACCESS_OVERLAY",
                   (os.environ.get("XDG_CONFIG_HOME") or "~/.config")
                   + "/orchestrator/access-overlay.json")
).expanduser().resolve()  # the configured path is a link to the roster; the refresh refuses to write through links

SOURCES = {
    "models_dev": "https://models.dev/api.json",
    "openrouter": "https://openrouter.ai/api/v1/models",
    "aider": "https://raw.githubusercontent.com/Aider-AI/aider/main/aider/website/_data/polyglot_leaderboard.yml",
    "swebench": "https://raw.githubusercontent.com/SWE-bench/swe-bench.github.io/master/data/leaderboards.json",
}


AA_EXTRA_TOKENS = ["gemini3", "claudefable", "claudeopus", "claudesonnet", "gpt56"]


def fetch_artificial_analysis(tokens: dict[str, list[str]], overlay: dict | None = None) -> dict:
    """Benchmark indices from Artificial Analysis (free tier, 100 req/day, attribution required)."""
    key_path = STATE_DIR / "aa-key"
    key = os.environ.get("AA_API_KEY") or (
        key_path.read_text(encoding="utf-8").strip() if key_path.exists() else ""
    )
    if not key:
        return {}
    request = urllib.request.Request(
        "https://artificialanalysis.ai/api/v2/data/llms/models",
        headers={"x-api-key": key, "User-Agent": "fleet-market-refresh/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        print(f"market-refresh: WARN artificialanalysis: {exc}")
        return {}
    matches: dict[str, dict] = {}
    for model in payload.get("data", []):
        slug = norm(str(model.get("slug") or ""))
        hit = None
        for model_key, keys in sorted(tokens.items(), key=lambda item: -max(map(len, item[1]))):
            if any(slug == token or slug.startswith(token) for token in keys):
                hit = model_key
                break
        if hit is None and not any(extra in slug for extra in AA_EXTRA_TOKENS):
            continue
        evaluations = model.get("evaluations") or {}
        pricing = model.get("pricing") or {}
        entry = {
            "id": model.get("id"),
            "name": model.get("name"),
            "slug": model.get("slug"),
            "release_date": model.get("release_date"),
            "evaluations": dict(evaluations),
            "pricing": dict(pricing),
            "intelligence_index": evaluations.get("artificial_analysis_intelligence_index"),
            "coding_index": evaluations.get("artificial_analysis_coding_index"),
            "price_1m_blended": pricing.get("price_1m_blended_3_to_1"),
            "median_tokens_per_second": model.get("median_output_tokens_per_second"),
            "terminal_bench_4": evaluations.get("terminalbench_v4_0", evaluations.get("terminal_bench_4", evaluations.get("terminal_bench_4_0"))),
            "ttfa_s": model.get("median_time_to_first_answer_token"),
            "price_1m_input": pricing.get("price_1m_input_tokens"),
            "price_1m_output": pricing.get("price_1m_output_tokens"),
            "index_version": evaluations.get("artificial_analysis_intelligence_index_version")
                or payload.get("index_version") or (payload.get("meta") or {}).get("intelligence_index_version"),
        }
        label = str(model.get("slug") or "") + " " + str(model.get("name") or "")
        found = re.search(r"(?<![a-z])(xhigh|max|ultra|medium|high|low|none|minimal|thinking)(?![a-z])", label.lower())
        level = "none" if re.search(r"non[- ]?reasoning|non[- ]?thinking", label.lower()) else (
            found.group(1) if found else f"unspecified:{model.get('slug')}"
        )
        entry["effort"] = level
        key = hit or f"watchlist:{model.get('slug')}"
        family = matches.setdefault(key, {"levels": {}, "variants": []})
        family["variants"].append(entry)
    for key, family in matches.items():
        for level in {row["effort"] for row in family["variants"]}:
            rows = current_aa_variants([r for r in family["variants"] if r["effort"] == level], overlay or {}, key)
            if rows:
                family["levels"][level] = rows[0] if len(rows) == 1 else {"ambiguous": True, "variants": rows}
        current = [row for row in family["levels"].values() if not row.get("ambiguous")]
        if current:
            family.update(max(current, key=lambda row: row.get("intelligence_index") or 0))
    return matches


def entitled_models() -> set[str]:
    """Best-effort: what the Go plan can actually call, via the opencode CLI."""
    import subprocess

    # Scrub inherited env the way the OpenCode/Copilot wrappers do: a stale GitHub
    # token overrides the valid Keychain login (see Model Fleet Manual), and a stray
    # OPENCODE_CONFIG* would point this catalog pull at a fleet-worker profile.
    scrubbed = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "COPILOT_GITHUB_TOKEN",
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "OPENCODE_CONFIG",
            "OPENCODE_CONFIG_CONTENT",
            "OPENCODE_CONFIG_DIR",
        }
    }
    try:
        proc = subprocess.run(
            # OpenCode v2 rejects a provider argument and lists every provider.
            ["opencode", "models", "--standalone"],
            text=True,
            capture_output=True,
            timeout=60,
            env={
                **scrubbed,
                "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
                "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT": "1",
            },
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if proc.returncode != 0:
        return set()
    return {line.strip() for line in proc.stdout.splitlines() if line.strip().startswith("opencode-go/")}


def fetch(url: str, timeout: int = 60) -> str | None:
    request = urllib.request.Request(url, headers={"User-Agent": "fleet-market-refresh/1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 - best-effort source, report and continue
        print(f"market-refresh: WARN {url}: {exc}")
        return None


def norm(name: str) -> str:
    # Dot-insensitive: AA hyphenates version dots ("glm-5-2"), models.dev keeps them.
    return re.sub(r"[^a-z0-9]", "", name.lower())


def family_tokens(model_key: str) -> list[str]:
    # Whole normalized key only: loose family stems cross-match Pro with Flash.
    return [norm(re.sub(r"-free$", "", model_key))]


def extract_models_dev(raw: str | None) -> dict:
    if not raw:
        return {}
    data = json.loads(raw)
    provider = data.get("opencode-go")
    if not provider:
        return {}
    models = {}
    for model_id, model in (provider.get("models") or {}).items():
        models[model_id] = {
            "name": model.get("name"),
            "cost": model.get("cost"),
            "limit": model.get("limit"),
            "modalities": model.get("modalities"),
            "reasoning": model.get("reasoning"),
            "tool_call": model.get("tool_call"),
            "release_date": model.get("release_date"),
            "last_updated": model.get("last_updated"),
            "deprecated": (model.get("status") == "deprecated") or None,
        }
    return models


def extract_openrouter(raw: str | None, tokens: dict[str, list[str]]) -> dict:
    if not raw:
        return {}
    entries = json.loads(raw).get("data", [])
    matches: dict[str, list[dict]] = {}
    for entry in entries:
        slug = norm(str(entry.get("canonical_slug") or entry.get("id") or ""))
        for model_key, keys in tokens.items():
            if any(token in slug for token in keys):
                matches.setdefault(model_key, []).append(
                    {
                        "id": entry.get("id"),
                        "context_length": entry.get("context_length"),
                        "pricing": {
                            "prompt": (entry.get("pricing") or {}).get("prompt"),
                            "completion": (entry.get("pricing") or {}).get("completion"),
                        },
                        "input_modalities": (entry.get("architecture") or {}).get("input_modalities"),
                    }
                )
    return matches


def extract_aider(raw: str | None, tokens: dict[str, list[str]]) -> dict:
    if not raw:
        return {}
    scores: dict[str, list[dict]] = {}
    for block in re.split(r"\n- ", raw):
        model_match = re.search(r"^\s*model:\s*(.+)$", block, re.M)
        rate_match = re.search(r"^\s*pass_rate_2:\s*([\d.]+)", block, re.M)
        if not model_match or not rate_match:
            continue
        entry_name = norm(model_match.group(1))
        for model_key, keys in tokens.items():
            if any(token in entry_name for token in keys):
                scores.setdefault(model_key, []).append(
                    {"model": model_match.group(1).strip(), "pass_rate_2": float(rate_match.group(1))}
                )
    return scores


def extract_swebench(raw: str | None, tokens: dict[str, list[str]]) -> dict:
    if not raw:
        return {}
    try:
        boards = json.loads(raw).get("leaderboards", [])
    except json.JSONDecodeError:
        return {}
    scores: dict[str, list[dict]] = {}
    for board in boards:
        board_name = board.get("name", "")
        for result in board.get("results", []):
            entry_name = norm(str(result.get("name", "")))
            for model_key, keys in tokens.items():
                if any(token in entry_name for token in keys):
                    scores.setdefault(model_key, []).append(
                        {
                            "board": board_name,
                            "entry": result.get("name"),
                            "resolved": result.get("resolved"),
                        }
                    )
    return scores


def second_opinion(lane: str, changes: dict, overlay: dict) -> str:
    """Shadow review of the applied refresh by a cheap decision lane: a report line, never a gate."""
    by_id = {l["lane_id"]: l for l in overlay.get("lanes", [])}
    holders = {}
    for role in daily.CHEAP_ROLES:
        ranking = overlay.get("routing", {}).get("roles", {}).get(role, {}).get("quality_first", [])
        holders[role] = [
            f"{x} (AA {by_id.get(x, {}).get('market_scores', {}).get('aa_intelligence')}, "
            f"price {by_id.get(x, {}).get('price_1m')}, data {by_id.get(x, {}).get('data_policy', 'default')})"
            for x in ranking]
    text = (daily.summary(changes) + "\nChanges: " + json.dumps(changes)
            + "\nCheap roles after the refresh: " + json.dumps(holders))
    verdict = decide.decide("refresh-review", {"Refresh result": text}, lane=lane, data_class="public")
    if not verdict:
        return f"Second opinion ({lane}, shadow): unavailable."
    return (f"Second opinion ({lane}, shadow): {verdict['answer']} "
            f"(confidence {verdict['confidence']}): {verdict['reason']}")


# BR-REFRESH-001
def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    overlay = json.loads(OVERLAY.read_text(encoding="utf-8"))
    lane_keys = sorted(
        set(overlay.get("effort", {})) | {
            lane["model_key"]
            for lane in overlay.get("lanes", [])
            if lane.get("harness") == "opencode"
        }
    )
    catalogs = daily.discover(overlay)
    discovered = {row["model_key"] for c in catalogs for row in c.get("models") or []}
    tokens = {re.sub(r"-free$", "", key): family_tokens(key) for key in set(lane_keys) | discovered}

    raws = {name: fetch(url) for name, url in SOURCES.items()}
    try:
        models_dev = json.loads(raws["models_dev"] or "{}")
        if not isinstance(models_dev, dict):
            models_dev = {}
            raws["models_dev"] = None
    except (TypeError, ValueError):
        models_dev = {}
        raws["models_dev"] = None
    daily.enrich(catalogs, models_dev)
    catalog = {
        "schema": "fleet-market-catalog/v1",
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "sources_ok": {name: raws[name] is not None for name in SOURCES},
        "go_models": extract_models_dev(raws["models_dev"]),
        "provider_catalogs": catalogs,
        "openrouter": extract_openrouter(raws["openrouter"], tokens),
        "aider_polyglot": extract_aider(raws["aider"], tokens),
        "swebench": extract_swebench(raws["swebench"], tokens),
        "artificial_analysis": fetch_artificial_analysis(tokens, overlay),
        "attribution": "Benchmark indices courtesy of Artificial Analysis (artificialanalysis.ai).",
        "prior_not_verdict": "Public scores rank the weights, not the opencode-go route.",
    }
    arena_config = ((overlay.get("policy") or {}).get("evidence") or {}).get("lmarena") or {}
    arena_source = os.environ.get("LMARENA_LEADERBOARD") or arena_config.get("path") or arena_config.get("url")
    catalog["lmarena"] = load_lmarena(arena_source)

    catalog_path = STATE_DIR / "market-catalog.json"
    previous = {}
    if catalog_path.exists():
        try:
            previous = json.loads(catalog_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = {}

    old_ids = set((previous.get("go_models") or {}).keys())
    new_ids = set(catalog["go_models"].keys())
    appeared = sorted(new_ids - old_ids) if old_ids else []
    disappeared = sorted(old_ids - new_ids)
    entitled = {r["selector"] for c in catalogs if c["provider"] == "opencode-go"
                for r in c.get("models") or []}
    entitled_keys = {selector.split("/", 1)[-1] for selector in entitled}
    entitled_not_admitted = sorted(
        model_id
        for model_id in new_ids
        if model_id in entitled_keys and model_id not in set(lane_keys)
    )
    catalog_only = sorted(
        model_id
        for model_id in new_ids
        if model_id not in entitled_keys and model_id not in set(lane_keys)
    )
    price_changes = []
    for model_id in new_ids & old_ids:
        old_cost = (previous["go_models"].get(model_id) or {}).get("cost")
        new_cost = catalog["go_models"][model_id].get("cost")
        if old_cost != new_cost:
            price_changes.append({"model": model_id, "old": old_cost, "new": new_cost})

    report_lines = [
        "# Market report",
        "",
        f"Fetched {catalog['fetched_at']}. Sources ok: "
        + ", ".join(name for name, ok in catalog["sources_ok"].items() if ok)
        + (
            "; FAILED: " + ", ".join(name for name, ok in catalog["sources_ok"].items() if not ok)
            if not all(catalog["sources_ok"].values())
            else ""
        ),
        "",
        f"Go catalog models: {len(new_ids)}. Overlay lanes: {len(lane_keys)}.",
    ]
    if appeared:
        report_lines.append(f"NEW on the Go catalog since last fetch: {', '.join(appeared)}")
    if disappeared:
        report_lines.append(f"GONE from the Go catalog: {', '.join(disappeared)}")
    if entitled_not_admitted:
        report_lines.append(
            "LISTED by the provider CLI but NOT in the overlay (unverified admission candidates): "
            + ", ".join(entitled_not_admitted)
        )
    if catalog_only:
        report_lines.append(
            f"Catalog-only, entitlement unknown or absent ({len(catalog_only)} models): "
            + ", ".join(catalog_only)
        )
    if price_changes:
        report_lines.append("Price changes: " + json.dumps(price_changes))
    if not (appeared or disappeared or entitled_not_admitted or price_changes):
        report_lines.append("No admission-relevant changes against the previous fetch.")
    aa = catalog["artificial_analysis"]
    lane_rows = sorted(
        ((key, entry) for key, entry in aa.items() if not key.startswith("watchlist:")),
        key=lambda item: -(item[1].get("intelligence_index") or 0),
    )
    if lane_rows:
        report_lines += ["", "Lane standings (Artificial Analysis indices; intelligence / coding / $ blended per 1M / tok/s):"]
        for key, entry in lane_rows:
            report_lines.append(
                f"  {key}: {entry.get('intelligence_index')} / {entry.get('coding_index')} / "
                f"{entry.get('price_1m_blended')} / {entry.get('median_tokens_per_second')}"
            )
    report_lines += [
        "",
        catalog["attribution"],
        "Scores are model priors, not route verdicts. Admission still requires the local smoke test.",
    ]

    refresh_failed = False
    try:
        changes = daily.apply_refresh(OVERLAY, catalogs, catalog)
        report_lines.append(daily.summary(changes))
        if changes["unknown"]:
            report_lines.append("Catalog checks unknown: " + "; ".join(changes["unknown"]))
        overlay = json.loads(OVERLAY.read_text(encoding="utf-8"))
    except (OSError, ValueError, daily.patcher.PatchError):
        refresh_failed = True
        # Do not print external exception text or credentials in the report.
        report_lines.append("Daily roster: update refused; overlay retained or restored. Check catalog/roster validation.")

    review_lane = overlay.get("policy", {}).get("model_refresh", {}).get("review_lane")
    if isinstance(review_lane, str) and review_lane and not refresh_failed:
        try:
            report_lines.append(second_opinion(review_lane, changes, overlay))
        except Exception:  # noqa: BLE001 - a shadow comment must never fail the refresh
            report_lines.append(f"Second opinion ({review_lane}, shadow): unavailable.")

    catalog_path.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_evidence(STATE_DIR.parent, overlay, catalog)
    (STATE_DIR / "market-report.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    print("\n".join(report_lines))
    return 1 if refresh_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
