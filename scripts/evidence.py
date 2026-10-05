#!/usr/bin/env python3
"""Host-local model/effort evidence. External scores are heuristic pass priors.

They are not measured task pass probabilities. Only explicit mechanical checks
update the Beta posterior. An unmeasured level never borrows another level's q.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import math
import re
import statistics
import urllib.request
from pathlib import Path

FAMILIES = ("coding-agent", "review", "repo-qa", "reasoning", "extraction", "research", "visual")
try:
    from run_identity import FAMILY_BY_ROLE
except ModuleNotFoundError:
    from scripts.run_identity import FAMILY_BY_ROLE

# This is the single assumptions table. Weights are reliability * independence;
# each publisher contributes at most one prior-strength budget, not one per metric.
# None of these maps calibrate success on our tasks. Overlay overrides must carry
# their own assumption text and keep an explicit scale/map and family scope.
ASSUMPTIONS = {
    "unknown": {"alpha": 1.0, "beta": 1.0, "sd_floor": .15,
                "assumption": "No level-specific evidence: uniform Beta prior, no interpolation."},
    "aggregation": {"strength_per_source": 4.0, "external_sd_floor": .12,
                    "freshness_half_life_days": 90.0,
                    "assumption": "Correlated public metrics have weak pseudo-sample strength; age halves weight."},
    "estimates": {"tokens_in": 4000.0, "tokens_out": 1500.0, "latency_s": 120.0,
                  "assumption": "Fallback task size and duration are placeholders, never quota measurements."},
    "sources": {
        "aa_intelligence": {"source": "artificial_analysis", "metrics": ["artificial_analysis_intelligence_index", "intelligence_index"], "anchor": True,
                            "map": "linear", "scale": 100.0, "weight": .5,
                            "families": ["reasoning", "research"],
                            "assumption": "Composite intelligence transfers weakly to these task families."},
        "aa_coding": {"source": "artificial_analysis", "metrics": ["artificial_analysis_coding_index", "coding_index"],
                      "map": "linear", "scale": 100.0, "weight": .5,
                      "families": ["coding-agent", "review"],
                      "assumption": "Coding index is an indirect prior for implementation and review."},
        "aa_terminal": {"source": "artificial_analysis", "metrics": ["terminalbench_v4_0", "terminal_bench_4_0", "terminal_bench_4"], "anchor": True,
                        "map": "fraction_or_percent", "weight": .8, "families": ["coding-agent", "repo-qa", "review"],
                        "assumption": "Terminal task success transfers heuristically; fractions or percentages accepted."},
        "aa_tau2": {"source": "artificial_analysis", "metrics": ["tau2", "tau2_bench", "tau2_bench_telecom"],
                    "map": "fraction_or_percent", "weight": .4, "families": ["coding-agent", "research"],
                    "assumption": "Tool-use benchmark transfers weakly to local agent work."},
        "aa_lcr": {"source": "artificial_analysis", "metrics": ["lcr", "aa_lcr"],
                   "map": "fraction_or_percent", "weight": .6, "families": ["reasoning", "repo-qa", "research"],
                   "assumption": "Long-context reasoning supplies indirect priors for repository and research reading."},
        "aa_ifbench": {"source": "artificial_analysis", "metrics": ["ifbench", "if_bench"], "anchor": True,
                       "map": "fraction_or_percent", "weight": .6, "families": ["extraction"],
                       "assumption": "Instruction-following transfers weakly to schema extraction."},
        "lmarena": {"source": "lmarena", "metrics": ["elo", "rating", "arena_score", "score"],
                    "map": "logistic", "center": 1000.0, "scale": 400.0, "weight": .25,
                    "families": ["visual"], "secondary": True,
                    "assumption": "Human preference Elo is a weak heuristic, not task correctness; exact levels only."},
    },
}


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError):
        return None


def _date(value):
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def _first_number(row, *fields):
    return next((value for field in fields if (value := _number(row.get(field))) is not None), None)


def load_lmarena(source=None):
    """Optional CC-BY leaderboard JSON/parquet; no package or URL is required.

    A configured URL must be a machine-readable export. No scraping or guessed
    endpoints. Parquet uses pandas only if already installed with its engine.
    """
    result = {"available": False, "rows": [], "license": "CC-BY", "reason": "not configured"}
    if not source:
        return result
    try:
        name = str(source)
        if name.startswith(("https://", "http://")):
            with urllib.request.urlopen(name, timeout=20) as response:
                raw = response.read()
        else:
            raw = Path(source).expanduser().read_bytes()
        if name.split("?", 1)[0].endswith(".parquet"):
            import pandas as pd  # Optional dependency, including its parquet engine.
            rows = pd.read_parquet(io.BytesIO(raw)).to_dict(orient="records")
        else:
            payload = json.loads(raw)
            rows = payload if isinstance(payload, list) else payload.get("rows", payload.get("data", []))
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError("unsupported leaderboard shape")
        result.update(available=True, rows=rows, reason=None)
    except Exception as exc:  # Optional IO/format/dependency failures must not stop refresh.
        # No URL, credentials, local path or provider error body enters state/logs.
        result["reason"] = "optional source unavailable: " + type(exc).__name__
    return result


def _variants(entry):
    if not isinstance(entry, dict):
        return []
    if isinstance(entry.get("variants"), list):
        return entry["variants"]
    return [entry]


def current_aa_variants(variants, overlay, model):
    """Resolve served snapshot identity, then newest release within one level.

    Raw variants remain in the catalog. Equal-date conflicting rows stay
    ambiguous rather than letting input order select a benchmark silently.
    """
    card = (overlay.get("model_cards") or {}).get(model, {})
    served = card.get("served_snapshot") or next((lane.get("served_snapshot")
        for lane in overlay.get("lanes", []) if lane.get("model_key") == model and lane.get("served_snapshot")), None)
    candidates = []
    for row in variants:
        slug = str(row.get("slug") or "")
        snapshots = re.findall(r"(?:^|-)(\d{8}|\d{4})(?=-|$)", slug)
        if served:
            declared = str(served)
            compact = declared.replace("-", "")
            declared_dates = re.findall(r"(?:^|-)(\d{8}|\d{4})(?=-|$)", declared)
            if slug != declared and not any(s in declared_dates or s == compact or s == compact[-4:] for s in snapshots):
                continue
            if not compact.isdigit():
                # A named snapshot identifies its stem too, not every preview
                # or unrelated alias that happens to carry the same date.
                effort_suffix = r"-(xhigh|max|ultra|medium|high|low|none|minimal|thinking|non-thinking)$"
                if re.sub(effort_suffix, "", slug) != re.sub(effort_suffix, "", declared):
                    continue
        elif snapshots:
            continue
        candidates.append(row)
    if not candidates:
        return []
    newest = max(str(row.get("release_date") or "") for row in candidates)
    # Exact duplicate payloads are not ambiguous measurements.
    unique = {json.dumps(row, sort_keys=True): row for row in candidates if str(row.get("release_date") or "") == newest}
    return list(unique.values())


def _records_by_task(records):
    """Link identity, telemetry, and proof by run/attempt ids before counting.

    A union is necessary: an identity can have only run_id, while telemetry also
    has afk_attempt_id and the mechanical proof has only attempt_id.
    """
    groups, aliases = {}, {}
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        keys = ["run:" + str(record["run_id"])] if record.get("run_id") else []
        keys += ["attempt:" + str(record[key]) for key in ("attempt_id", "afk_attempt_id") if record.get(key)]
        roots = {aliases[key] for key in keys if key in aliases}
        root = min(roots) if roots else index
        merged = []
        for other in roots:
            merged += groups.pop(other)
            for alias, target in list(aliases.items()):
                if target == other:
                    aliases[alias] = root
        groups.setdefault(root, []).extend(merged + [record])
        for key in keys:
            aliases[key] = root
    return list(groups.values())


def _mechanical_outcome(record):
    proof = record.get("proof") or {}
    # Existing AFK proof records provide the command exit and content hash. A
    # worker/transport failure with a passing proof is not a failed task trial.
    if record.get("attempt_kind") == "afk" or record.get("schema") == "afk-attempt/v1":
        code = proof.get("returncode")
        if type(code) is not int or not proof.get("output_sha256"):
            return None
        if record.get("result") == "verified" and code == 0 and not record.get("failure_class"):
            return True
        if record.get("result") == "failed" and code != 0:
            return False
    outcome = record.get("outcome") or {}
    if isinstance(outcome, dict) and outcome.get("mechanically_verified") is True and type(outcome.get("passed")) is bool:
        return outcome["passed"]
    return None


def _observations(overlay, records):
    lanes = {lane.get("lane_id"): lane for lane in overlay.get("lanes", [])}
    outcomes, telemetry = {}, {}
    for group in _records_by_task(records):
        # Proof metadata wins, then priced telemetry, then identity. No guessed
        # effort from today's default: it might differ from the effort that ran.
        proofs = [r for r in group if _mechanical_outcome(r) is not None]
        ordered = proofs + [r for r in group if r not in proofs]
        def first(key):
            return next((r[key] for r in ordered if r.get(key) is not None), None)
        lane = lanes.get(first("lane_id"), {})
        model = first("model_key") or first("model") or lane.get("model_key") or first("selected_model")
        level = first("effort") or first("level")
        if not model or not level:
            continue
        cell = (model, level)
        family = first("family") or FAMILY_BY_ROLE.get(first("role"))
        if family in FAMILIES and proofs:
            verdicts = {_mechanical_outcome(r) for r in proofs}
            if len(verdicts) == 1:  # Conflicting duplicate proofs are unavailable.
                counts = outcomes.setdefault((model, level, family), {"passes": 0, "trials": 0})
                counts["trials"] += 1
                counts["passes"] += int(verdicts.pop())
        bucket = telemetry.setdefault(cell, {"in": [], "out": [], "latency": [], "completion_latency": [],
                                             "evidence_flags": set(), "own_cost": {}})
        for record in proofs:
            flags = record.get("evidence_flags")
            if isinstance(flags, list):
                bucket["evidence_flags"].update(flag for flag in flags if isinstance(flag, str) and flag in {
                    "coverage_incomplete", "calibration_unmeasured", "model_identity_unconfirmed",
                    "synthetic_coding_only", "model_harness_specific"})
        pool = first("pool") or first("quota_pool") or lane.get("quota_pool")
        cost = next((r.get("own_cost") for r in ordered if isinstance(r.get("own_cost"), dict)), {})
        if pool:
            # Measured usage only; never feed a selector's estimated cost back
            # as an observation of the provider's debit.
            if isinstance(cost.get(pool), dict):
                cost = cost[pool]
            costs = bucket["own_cost"].setdefault(pool, {"percent_per_task": [], "requests_per_task": []})
            for field in costs:
                value = _number(cost.get(field))
                if value is not None:
                    costs[field].append(value)
        # One priced worker telemetry row per task, no identity/proof duplicates.
        token_record = next((r for r in group if isinstance(r.get("tokens"), dict) and r["tokens"]), None)
        if token_record:
            for target, fields in (("in", ("input", "in", "input_tokens")), ("out", ("output", "out", "output_tokens"))):
                value = next((_number(token_record["tokens"][k]) for k in fields if k in token_record["tokens"]), None)
                if value is not None:
                    bucket[target].append(value)
        first_answer = next((value for r in ordered if (value := _first_number(r, "ttfa_s", "time_to_first_answer_s")) is not None), None)
        if first_answer is not None:
            bucket["latency"].append(first_answer)
        completion = next((value for r in ordered if (value := _first_number(r, "completion_time_s")) is not None), None)
        if completion is not None and completion >= 0:
            bucket["completion_latency"].append(completion)
    return outcomes, telemetry


def _source_rows(model, level, catalog, assumptions, read_on, overlay):
    aa = (catalog.get("artificial_analysis") or {}).get(model, {})
    entry = (aa.get("levels") or {}).get(level)
    raw = aa.get("variants") or (_variants(entry) if entry else _variants(aa))
    variants = current_aa_variants([v for v in raw if v.get("effort", level) == level], overlay, model)
    ambiguous = len(variants) > 1
    rows = []
    if not ambiguous and variants:
        row = variants[0]
        rows.append(("artificial_analysis", {**row, **(row.get("evaluations") or {})}, row))
    arena = catalog.get("lmarena") or {}
    arena_rows = arena if isinstance(arena, list) else arena.get("rows", [])
    for row in arena_rows:
        if (row.get("model_key") or row.get("model")) == model and (row.get("level") or row.get("effort")) == level:
            rows.append(("lmarena", row, row))
    sources = []
    for publisher, values, raw in rows:
        for name, rule in assumptions["sources"].items():
            if rule.get("source") != publisher:
                continue
            metric = next((key for key in rule["metrics"] if _number(values.get(key)) is not None), None)
            if not metric:
                continue
            value = _number(values[metric])
            if rule["map"] == "linear":
                prior = value / rule["scale"]
            elif rule["map"] == "fraction_or_percent":
                prior = value if value <= 1 else value / 100
            elif rule["map"] == "logistic":
                prior = 1 / (1 + math.exp(max(-700, min(700, (rule["center"] - value) / rule["scale"]))))
            else:
                continue
            source_date = raw.get("read_on") or catalog.get("fetched_at") or read_on
            now, then = _date(read_on), _date(source_date)
            age = max(0, (now - then).total_seconds() / 86400) if now and then else 0
            freshness = 2 ** (-age / assumptions["aggregation"]["freshness_half_life_days"])
            sources.append({"source": publisher, "metric": metric, "value": value,
                            "weight": rule["weight"] * freshness, "read_on": source_date,
                            "prior_mean": max(0.0, min(1.0, prior)), "mapping": name,
                            "families": rule["families"], "assumption": rule["assumption"],
                            "heuristic": True, "id": raw.get("id"), "release_date": raw.get("release_date")})
    return sources, variants, ambiguous


def _admitted_cells(overlay):
    """Standing roster admission defines the percentile population, not the feed."""
    cells = set()
    cards, lanes = overlay.get("model_cards") or {}, overlay.get("lanes") or []
    for model, effort in (overlay.get("effort") or {}).items():
        card = cards.get(model, {})
        model_lanes = [lane for lane in lanes if lane.get("model_key") == model]
        pools = {lane.get("quota_pool") for lane in model_lanes}
        if card.get("pool"):
            pools.add(card["pool"])
        harnesses = None
        if overlay.get("quota_pools"):
            # Reuse standing fleet admission/retention. No runtime or IO is
            # consulted by this pure evidence builder.
            try:
                import fleetctl
            except ModuleNotFoundError:
                from scripts import fleetctl
            if not pools:
                pools = {p for p in overlay["quota_pools"] if model in fleetctl.choosable_models(overlay, p)}
            allowed = {p for p in pools if fleetctl.pool_switches(overlay, {}, p).get(model, False)}
            harnesses = {lane.get("harness") for lane in model_lanes if lane.get("quota_pool") in allowed
                         and lane.get("access_status") == "verified" and lane.get("admission_status") == "active"
                         and lane.get("roles")}
            for pool in allowed:
                if (fleetctl.pool_is_direct(overlay, pool)
                        and card.get("access_status", "verified") == "verified"
                        and card.get("admission_status", "active") == "active"):
                    harnesses.add(pool)
            if not harnesses:
                continue
        elif card.get("hidden") or card.get("status", "current") != "current" or card.get("superseded_by"):
            continue
        controls = effort.get("levels") or {}
        levels = [v for h, values in controls.items() if harnesses is None or h in harnesses for v in values] if isinstance(controls, dict) else list(controls)
        cells.update((model, level) for level in levels or [effort.get("default", "provider-default")])
    return cells


def _anchor_priors(sources_by_cell, admitted, assumptions):
    """One ranked anchor per family; missing anchors use a co-observed OLS fit."""
    priors = {}
    for family in FAMILIES:
        rules = [(name, rule) for name, rule in assumptions["sources"].items()
                 if rule.get("anchor") and family in rule.get("families", [])]
        if len(rules) > 1:
            raise ValueError("multiple anchor metrics for " + family)
        if not rules:
            for cell, sources in sources_by_cell.items():
                secondary = next((s for s in sources if family == "visual" and family in s["families"] and s["weight"] > 0), None)
                priors[cell, family] = (secondary, None, None)
            continue
        name, rule = rules[0]
        observed = {cell: next((s for s in sources_by_cell[cell] if s["mapping"] == name and s["weight"] > 0), None)
                    for cell in admitted if cell in sources_by_cell}
        observed = {cell: source for cell, source in observed.items() if source is not None}
        values = [s["prior_mean"] for s in observed.values()]
        ranked = {}
        for cell, source in observed.items():
            x = source["prior_mean"]
            # Mean rank / N, including tied ranks. Population excludes imputed,
            # old, watchlist and catalog-only cells.
            ranked[cell] = (sum(v < x for v in values) + (sum(v == x for v in values) + 1) / 2) / len(values)
        for cell, sources in sources_by_cell.items():
            anchor = next((s for s in sources if s["mapping"] == name and s["weight"] > 0), None)
            if anchor and values:
                x = anchor["prior_mean"]
                percentile = ranked.get(cell, min(1.0, (sum(v < x for v in values) + (sum(v == x for v in values) + 1) / 2) / len(values)))
                priors[cell, family] = ({**anchor, "prior_mean": percentile, "percentile_population": len(values)}, rule["metrics"][0], None)
                continue
            fits = []
            for predictor in sources:
                if predictor["weight"] <= 0 or predictor["source"] != rule["source"] or predictor["mapping"] == name:
                    continue
                pairs = []
                for known, y in ranked.items():
                    x = next((s["prior_mean"] for s in sources_by_cell[known]
                              if s["mapping"] == predictor["mapping"] and s["weight"] > 0), None)
                    if x is not None:
                        pairs.append((x, y))
                if len(pairs) < 3:
                    continue
                xbar, ybar = (statistics.mean(p[i] for p in pairs) for i in (0, 1))
                spread = sum((x - xbar) ** 2 for x, _ in pairs)
                if spread <= 1e-12:
                    continue
                slope = sum((x - xbar) * (y - ybar) for x, y in pairs) / spread
                intercept = ybar - slope * xbar
                residual = max(.1, math.sqrt(sum((y - intercept - slope * x) ** 2 for x, y in pairs) / (len(pairs) - 2)))
                estimate = max(0.0, min(1.0, intercept + slope * predictor["prior_mean"]))
                fits.append((len(pairs), residual, predictor["mapping"], estimate, predictor, slope, intercept))
            if fits:
                n, residual, _, estimate, predictor, slope, intercept = min(fits, key=lambda fit: (-fit[0], fit[1], fit[2]))
                fit = {"predictor_metric": predictor["metric"], "n": n, "residual_sd": residual,
                       "slope": slope, "intercept": intercept}
                imputed = {**predictor, "metric": rule["metrics"][0], "value": None,
                           "mapping": name, "prior_mean": estimate, "weight": min(predictor["weight"], rule["weight"]),
                           "families": rule["families"], "imputed": True, "assumption": rule["assumption"]}
                priors[cell, family] = (imputed, rule["metrics"][0], fit)
            else:
                priors[cell, family] = (None, rule["metrics"][0], None)
    return priors


def build_evidence(overlay, catalog, ledger_records, read_on=None):
    """Pure builder: one row per canonical model/level, no IO or live state."""
    read_on = read_on or catalog.get("fetched_at") or dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    assumptions = json.loads(json.dumps(ASSUMPTIONS))
    configured = ((overlay.get("policy") or {}).get("evidence") or {}).get("sources") or {}
    for name, override in configured.items():
        if isinstance(override, dict) and override.get("assumption"):
            assumptions["sources"][name] = {**assumptions["sources"].get(name, {}), **override}
    outcomes, telemetry = _observations(overlay, ledger_records)
    cells = set(telemetry) | {(m, l) for m, l, _ in outcomes}
    for model, effort in (overlay.get("effort") or {}).items():
        levels = effort.get("levels") or {}
        exposed = [v for values in levels.values() for v in values] if isinstance(levels, dict) else list(levels)
        cells.update((model, level) for level in exposed or [effort.get("default", "provider-default")])
    for model, entry in (catalog.get("artificial_analysis") or {}).items():
        if not model.startswith("watchlist:"):
            cells.update((model, level) for level in (entry.get("levels") or {}))
    admitted = _admitted_cells(overlay)
    source_data = {cell: _source_rows(*cell, catalog, assumptions, read_on, overlay) for cell in cells}
    priors = _anchor_priors({cell: data[0] for cell, data in source_data.items()}, admitted, assumptions)
    rows = []
    for model, level in sorted(cells):
        sources, variants, ambiguous = source_data[model, level]
        flags, q, own = ["external_scores_are_heuristic_priors"], {}, {}
        if ambiguous:
            flags.append("ambiguous_variants")
        for family in FAMILIES:
            anchor, anchor_metric, fit = priors[(model, level), family]
            family_sources = [anchor] if anchor else []
            prior_strength = assumptions["aggregation"]["strength_per_source"] * anchor["weight"] if anchor else 0.0
            prior_total = prior_strength * anchor["prior_mean"] if anchor else 0.0
            counts = outcomes.get((model, level, family), {"passes": 0, "trials": 0})
            own[family] = dict(counts)
            if prior_strength:
                alpha, beta = prior_total, prior_strength - prior_total
                # Prevent an extreme synthetic score from becoming a point mass.
                alpha, beta = max(alpha, 1e-6), max(beta, 1e-6)
            else:
                alpha, beta = assumptions["unknown"]["alpha"], assumptions["unknown"]["beta"]
            alpha += counts["passes"]
            beta += counts["trials"] - counts["passes"]
            mean = alpha / (alpha + beta)
            sd = math.sqrt(alpha * beta / ((alpha + beta) ** 2 * (alpha + beta + 1)))
            unknown = not family_sources and counts["trials"] == 0
            if unknown:
                sd = max(sd, assumptions["unknown"]["sd_floor"])
                flags.append("unknown:" + family)
            elif family_sources and not counts["trials"]:
                sd = max(sd, assumptions["aggregation"]["external_sd_floor"])
            if fit:
                sd = math.hypot(sd, fit["residual_sd"])
                flags.extend(["imputed", "imputed:" + family])
            q[family] = {"mean": mean, "sd": sd, "n_sources": int(bool(anchor)),
                         "sources": family_sources, "unknown": unknown,
                         "heuristic": bool(family_sources), "prior_strength": prior_strength,
                         "anchor_metric": anchor_metric, "imputed": fit is not None, "imputation": fit}
        measured = telemetry.get((model, level), {})
        tokens = {}
        for direction in ("in", "out"):
            values = measured.get(direction) or []
            tokens[direction] = statistics.median(values) if values else assumptions["estimates"]["tokens_" + direction]
            flags.append("tokens_" + direction + ("_measured" if values else "_estimated"))
        price = {"in": None, "out": None}
        for direction, field in (("in", "price_1m_input"), ("out", "price_1m_output")):
            pricing_field = "price_1m_input_tokens" if direction == "in" else "price_1m_output_tokens"
            values = [_number(v.get(field, (v.get("pricing") or {}).get(pricing_field))) for v in variants]
            values = [v for v in values if v is not None]
            if values and len(set(values)) == 1:
                price[direction] = values[0]
            else:
                price[direction] = _number(((catalog.get("go_models") or {}).get(model, {}).get("cost") or {}).get("input" if direction == "in" else "output"))
        flags.append("price_api_equivalent_estimated" if all(v is not None for v in price.values()) else "price_unknown")
        latency = measured.get("latency") or measured.get("completion_latency") or []
        if not measured.get("latency") and measured.get("completion_latency"):
            flags.append("latency_completion_measured")
        flags.extend(measured.get("evidence_flags") or [])
        external_latency = [_first_number(v, "ttfa_s", "median_time_to_first_answer_token") for v in variants]
        external_latency = [v for v in external_latency if v is not None]
        flags.append("latency_measured" if latency else "latency_external" if external_latency else "latency_estimated")
        own_cost = {pool: {**{field: statistics.median(values) for field, values in data.items() if values},
                           "trials": max((len(values) for values in data.values()), default=0)}
                    for pool, data in measured.get("own_cost", {}).items() if any(data.values())}
        effort = (overlay.get("effort") or {}).get(model, {})
        rows.append({"model_key": model, "model": model, "level": level, "q": q,
                     "sources": sources, "own": own, "own_cost": own_cost, "tokens_per_task": tokens,
                     "price_1m": price, "latency_s": statistics.median(latency or external_latency) if latency or external_latency else assumptions["estimates"]["latency_s"],
                     "read_on": read_on, "recheck": effort.get("recheck") or "New release, changed benchmark, or 30 days since read.",
                     "flags": sorted(set(flags))})
    return {"schema": "fleet-evidence-levels/v1", "rows": rows, "read_on": read_on,
            "assumptions": assumptions,
            "unavailable": {"artificial_analysis": not bool(catalog.get("artificial_analysis")),
                            "lmarena": not bool((catalog.get("lmarena") or {}).get("rows") if isinstance(catalog.get("lmarena"), dict) else catalog.get("lmarena")),
                            "own": not bool(outcomes)}}


def write_evidence(state_dir, overlay, catalog):
    """Read the host-local ledger, ignore incomplete lines, atomically publish."""
    try:
        from fleetctl import atomic_json
    except ModuleNotFoundError:
        from scripts.fleetctl import atomic_json
    state_dir = Path(state_dir)
    ledger = state_dir / "runs.jsonl"
    records = []
    if ledger.exists():
        for line in ledger.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    result = build_evidence(overlay, catalog, records)
    atomic_json(state_dir / "evidence" / "levels.json", result, allow_nan=False)
    return result
