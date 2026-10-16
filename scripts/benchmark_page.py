"""Plain-language, family-bounded recommendations over private runtime model cards."""
from collections import defaultdict
from datetime import datetime
from html import escape
import re
from urllib.parse import quote

FAMILIES = {'visual': 'design & visuals', 'reasoning': 'writing & hard logic',
            'coding-agent': 'coding & fixing files', 'repo-qa': 'explaining a codebase',
            'review': 'reviewing & checking work', 'research': 'research & synthesis',
            'extraction': 'pulling structured data', 'writing': 'writing & clear arguments'}
LEVELS = dict(minimal='Minimal thinking', low='Light thinking', medium='Normal thinking',
              high='Deep thinking', xhigh='Extra-deep thinking', max='Maximum thinking',
              ultra='Ultra thinking', none='No extra thinking', thinking='Thinking on',
              instant='Instant reply', pro='Pro mode', **{'provider-default': 'Standard setting',
              'service-chosen': 'Chosen automatically'})
POOLS = {'claude': 'Claude subscription', 'codex': 'ChatGPT / Codex subscription',
         'antigravity-gemini': 'Gemini subscription (Antigravity)',
         'antigravity-argon': 'Antigravity Argon allowance',
         'antigravity-3p': 'Antigravity third-party pool', 'opencode-go': 'OpenCode Go subscription',
         'openrouter-free': 'OpenRouter free pool', 'github-copilot-student': 'GitHub Copilot Student',
         'gemini-metered': 'Metered Gemini API'}
HARNESSES = dict(claude='Claude Code', codex='Codex', agy='Antigravity', opencode='OpenCode',
                 openrouter='OpenRouter', copilot='GitHub Copilot')
METRICS = {'artificial_analysis_intelligence_index': 'Artificial Analysis Intelligence Index',
           'artificial_analysis_coding_index': 'Artificial Analysis Coding Index',
           'terminalbench_v4_0': 'Terminal-Bench 4.0 (command-line tasks)',
           'tau2': 'TAU-bench 2 (tool use)', 'lcr': 'Long-Context Reasoning',
           'ifbench': 'IFBench (following strict instructions)', 'elo': 'LMArena human preference Elo',
           'rating': 'LMArena human preference Elo', 'arena_score': 'LMArena human preference Elo'}
FLAGS = {
    'external_scores_are_heuristic_priors': 'Outside scores are rough starting estimates, not local tests',
    'latency_completion_measured': 'Reply speed measured on our own full task runs',
    'latency_measured': 'Reply speed measured locally', 'latency_external': 'Reply speed from public benchmarks',
    'latency_estimated': 'Reply speed is a 120s placeholder (not measured yet)',
    'price_api_equivalent_estimated': 'Token price is the public API equivalent, not subscription draw',
    'price_unknown': 'Token price not listed yet', 'tokens_in_estimated': 'Input size estimated at 4,000 tokens per task',
    'tokens_out_estimated': 'Output size estimated at 1,500 tokens per task',
    'tokens_in_measured': 'Input tokens measured from real runs', 'tokens_out_measured': 'Output tokens measured from real runs',
    'imputed': 'Some task estimates are inferred from neighbouring benchmarks',
    'ambiguous_variants': 'Public benchmark does not separate thinking levels for this model',
    'model_harness_specific': 'Results depend on the specific CLI tool used',
    'model_identity_unconfirmed': 'Provider did not confirm the exact underlying model version',
    'cost_unknown_nominal': 'Exact allowance draw per task is not published by the provider'}
JOBS = [('design', 'Best for design', ('visual',)), ('writing', 'Best for writing', ('writing', 'reasoning')),
        ('coding', 'Best for coding', ('coding-agent',)), ('research', 'Best for research', ('research',)),
        ('review', 'Best for reviews / checking work', ('review', 'repo-qa')),
        ('bulk', 'Best cheap helper for bulk work', ('coding-agent', 'extraction')),
        ('fast', 'Fast answers', tuple(FAMILIES))]


def plain(value):
    return str(value).replace('_', ' ').replace('-', ' ').capitalize()


def name(row):
    known = {'hy3': 'Hunyuan 3 (HY3)'}
    if row['model'] in known:
        return known[row['model']]
    words = re.sub(r'(?<=\d)-(?=\d)', '.', row['model']).replace(':', '-').split('-')
    brands = {'gpt': 'GPT', 'glm': 'GLM', 'mimo': 'MiMo', 'deepseek': 'DeepSeek',
              'chatgpt': 'ChatGPT', 'qwen3.8': 'Qwen 3.8'}
    return ' '.join(brands.get(w, w.upper() if re.match(r'^v\d', w) else w.title()) for w in words)


def level(row):
    value = row.get('level', 'provider-default')
    return ('Standard setting (effort not split by public ranking)' if value.startswith('unspecified:')
            else LEVELS.get(value, plain(value)))


def sources(row, working=False):
    return [s for s in row.get('sources', []) if not s.get('availability', {}).get('switched_off')
            and (not working or not s.get('availability', {}).get('blocked'))]


def availability(row):
    active = sources(row)
    if not active:
        return 'Switched off / not in an active pool'
    if sources(row, True):
        return 'No known quota block'  # Unknown gauges do not establish exhaustion.
    blocks = [s['availability']['blocked'] for s in active]
    known = [b['until'] for b in blocks if b.get('reset_known') and b.get('until')]
    return ('Not available until ' + min(known).replace('T', ' ').replace('Z', ' UTC')
            if len(known) == len(blocks) else 'Not available: quota error; reset not published')


def cost_feel(row):
    options = sources(row, True) or sources(row)
    if not options:
        return 'No active allowance'
    phrases = []
    for s in options:
        pool, percent = s.get('pool'), s.get('cost', {}).get('percent')
        if pool in {'openrouter-free', 'github-copilot-student'}:
            text = 'Free / included (zero allowance impact)'
        elif pool == 'opencode-go':
            draw = ('tiny draw, <0.01%/task' if percent < .005 else f'{percent:.3g}% allowance/task') if percent is not None else 'allowance draw unmeasured'
            text = 'Included in OpenCode Go (' + draw + ')'
        elif pool == 'antigravity-gemini':
            text = 'Included in your Gemini subscription'
        elif pool == 'gemini-metered':
            text = 'Metered Gemini API (billed per call; see configured cap)'
        else:
            text = 'Uses your ' + POOLS.get(pool, plain(pool)) + ' allowance'
        if percent is not None and pool != 'opencode-go':
            text += f' (~{percent:.3g}%/task)'
        elif pool in {'claude', 'codex', 'antigravity-3p'}:
            text += ' (draw unmeasured)'
        if text not in phrases:
            phrases.append(text)
    return '; or '.join(phrases)


def measured(row, families):
    return {f: q for f, q in row.get('quality', {}).items() if f in families and not q.get('unknown', False)}


def local(row, families):
    return {f: c for f, c in row.get('own', {}).items() if f in families and c.get('trials', 0) > 0}


def outside(row, families):
    return [s for s in row.get('outside', []) if set(s.get('families', [])) & set(families)]


def confidence(row, families):
    counts = local(row, families)
    if counts:
        return 'own', f'Measured by us ({sum(c["passes"] for c in counts.values())}/{sum(c["trials"] for c in counts.values())})'
    if outside(row, families):
        return 'outside', 'Outside rankings only'
    return 'guess', 'Guess · owner’s prior' if set(row.get('owner_prior', [])) & set(families) else 'Guess · not measured yet'


def badge(row, families):
    cls, text = confidence(row, families)
    return f'<span class="conf conf-{cls}">{escape(text)}</span>'


def quality_score(row, families, penalty=.5):
    qs = list(measured(row, families).values())
    return sum(q['mean'] - penalty * q.get('sd', 0) for q in qs) / len(qs) if qs else 0


def has_evidence(row):
    return bool(local(row, FAMILIES) or row.get('outside') or row.get('owner_prior'))


def lineage(row):
    return row['model'].split('-')[0].split(':')[0]


def outside_design_score(row):
    q = measured(row, ('visual',)).get('visual', {})
    # Older outside-only cards have an unmodified mean; local posteriors cannot rank publishers.
    return row.get('outside_quality', {}).get('visual', q.get('mean') if not local(row, ('visual',)) else None)


def design_choices(rows):
    priors = [r for r in rows if 'visual' in r.get('owner_prior', []) and sources(r)]
    prior = max(priors, key=lambda r: (quality_score(r, ('visual',)), r['model'], r.get('level', '')), default=None)
    ranked = [r for r in rows if sources(r, True) and outside(r, ('visual',))
              and outside_design_score(r) is not None]
    working = max(ranked, key=lambda r: (outside_design_score(r), r['model'], r.get('level', '')), default=None)
    return prior, working


def design_status(row):
    if sources(row, True):
        return availability(row)
    blocks = [s['availability']['blocked'] for s in sources(row)]
    if blocks and all(b.get('reset_known') and b.get('until') for b in blocks):
        reset = min(b['until'] for b in blocks)
        return 'Out of allowance until ' + datetime.fromisoformat(reset.replace('Z', '+00:00')).strftime('%d %b')
    return availability(row)


def select_jobs(rows):
    result = []
    for key, title, families in JOBS:
        candidates = []
        for row in rows:
            if not sources(row, True):
                continue
            counts, rankings = local(row, families), outside(row, families)
            prior = set(row.get('owner_prior', [])) & set(families)
            if not (counts or rankings or prior):
                continue
            speed = row.get('latency_s')
            flags = set(row.get('flags', []))
            own_speed = bool(flags & {'latency_measured', 'latency_completion_measured'})
            score = quality_score(row, families, .35 if key == 'research' else .5)
            if key == 'research' and (speed is None or speed > 180):
                continue
            if key == 'bulk' and not any(s.get('pool') in {'openrouter-free', 'github-copilot-student', 'opencode-go'}
                    or (s.get('price_1m', {}).get('out') is not None and s['price_1m']['out'] <= 1.5)
                    or (s.get('cost', {}).get('percent') is not None and s['cost']['percent'] <= .01)
                    for s in sources(row, True)):
                continue
            if key == 'fast':
                if speed is None or speed <= 0 or (not own_speed and speed > 35) or quality_score(row, families, 0) < .70:
                    continue
                rank = (int(own_speed), -speed, score)
            else:
                rank = (2 if counts else 1 if rankings else 0, score, -(speed or float('inf')))
            # Slow maximum effort earns its slot only when the quicker setting loses meaningful quality.
            if speed and speed > 180 and any(other['model'] == row['model']
                    and sources(other, True) and 0 < (other.get('latency_s') or float('inf')) < 90
                    and quality_score(other, families) >= score - .04 for other in rows):
                continue
            candidates.append((rank, row))
        candidates.sort(key=lambda item: (item[0], item[1]['model'], item[1].get('level', '')), reverse=True)
        unique = []
        for rank, row in candidates:
            if all(r['model'] != row['model'] for _, r in unique):
                unique.append((rank, row))
        if key == 'fast' and any(rank[0] for rank, _ in unique):
            unique = [(rank, row) for rank, row in unique if rank[0]]
        picks = unique[:2]
        if len(picks) == 2:
            top = picks[0][1]
            top_pools = {s['pool'] for s in sources(top, True)}
            alternatives = unique[1:]
            if key == 'writing':
                alt = next((item for item in alternatives if confidence(item[1], families)[0] == 'guess'
                    and not top_pools & {s['pool'] for s in sources(item[1], True)}), None)
            elif key == 'review':
                alt = next((item for item in alternatives if lineage(item[1]) != lineage(top)), None)
            elif key == 'coding':
                alt = next((item for item in alternatives if item[0][0] == picks[1][0][0]
                    and item[0][1] >= picks[1][0][1] - .03
                    and not top_pools & {s['pool'] for s in sources(item[1], True)}), None)
            else:
                alt = None
            if alt:
                picks[1] = alt
        result.append((key, title, families, [row for _, row in picks]))
    return result


def why(row, families, fast=False):
    counts = local(row, families)
    labels = ', '.join(FAMILIES[f] for f in families if f in counts)
    if counts:
        text = f'Passed {sum(c["passes"] for c in counts.values())} of {sum(c["trials"] for c in counts.values())} local checks for {labels}.'
    elif outside(row, families):
        metrics = ', '.join(dict.fromkeys(METRICS.get(s.get('metric'), plain(s.get('metric', 'ranking'))) for s in outside(row, families)))
        text = f'No test of ours yet for this job. Outside rankings: {metrics}; starting estimate {quality_score(row, families, 0):.0%}.'
    else:
        text = 'No test of ours yet for this job. Your starting judgement from its provisional roles; an owner’s prior, not a result.'
    if counts and measured(row, families):
        text += f' Starting estimate {quality_score(row, families, 0):.0%} for this job.'
    if row.get('latency_s') and (fast or set(row.get('flags', [])) & {'latency_measured', 'latency_completion_measured'}):
        timing = 'locally measured' if set(row.get('flags', [])) & {'latency_measured', 'latency_completion_measured'} else 'public or estimated'
        text += f' About {row["latency_s"]:.0f}s per reply ({timing}).'
    return text


def anchor(model):
    return 'model-' + model


def summary_phrases(row, rows):
    qs = sorted(measured(row, FAMILIES).items(), key=lambda item: item[1]['mean'], reverse=True)
    if qs:
        good = ', '.join(FAMILIES.get(f, plain(f)) for f, _ in qs[:2])
        if qs[0][1]['mean'] < .22:
            good = 'Only low starting estimates so far'
        elif qs[0][1]['mean'] < .5:
            good += f' (modest estimate, {qs[0][1]["mean"]:.0%})'
        weakest = [(f, q) for f, q in qs[2:] if q['mean'] < .45]
        bad = ', '.join(FAMILIES.get(f, plain(f)) for f, _ in weakest[-2:]) if weakest else ''
        if not bad and len(qs) >= 2 and qs[0][1]['mean'] - qs[-1][1]['mean'] >= .14:
            bad = f'{FAMILIES.get(qs[-1][0], plain(qs[-1][0]))} ({qs[-1][1]["mean"]:.0%} vs {qs[0][1]["mean"]:.0%} on its best)'
    else:
        good = ', '.join(FAMILIES.get(f, plain(f)) for f in row.get('owner_prior', []))
        good = good + ' (owner’s prior)' if good else 'Waiting for first benchmark'
        bad = ''
    if not bad:
        slow = next((r for r in rows if r.get('level') == 'max' and (r.get('latency_s') or 0) > 140), None)
        bad = f'Slow on maximum thinking (~{slow["latency_s"]:.0f}s)' if slow else 'Limits not established'
        if 'visual' not in measured(row, FAMILIES):
            bad += '; design unmeasured'
    return good, bad


def metric_score(s):
    value, metric = s.get('value'), s.get('metric', '')
    if not isinstance(value, (int, float)):
        return 'unmeasured'
    if metric in {'elo', 'rating', 'arena_score'}:
        return f'{value:.0f} Elo'
    if metric.startswith('artificial_analysis_'):
        return f'{value:.1f} / 100'
    return f'{value:.1%}'


def nerd_panel(best, rows):
    e = lambda x: escape(str(x))
    scores = []
    for setting in rows:
        for f, q in measured(setting, setting.get('quality', {})).items():
            count = local(setting, (f,)).get(f)
            provenance = '; '.join(f'{p.get("source")}: n={p.get("n")}, {p.get("date") or "undated"}' for p in setting.get('own_provenance', {}).get(f, []))
            origin = (f'{count["passes"]}/{count["trials"]} (n={count["trials"]}) local checks; {provenance}' if count else
                      f'Public anchor: {METRICS.get(q.get("anchor_metric"), plain(q.get("anchor_metric", "unknown")))}; prior strength {q.get("prior_strength", 0):g}')
            scores.append(f'<li>{e(level(setting))}: {e(FAMILIES.get(f, plain(f)))}: {q["mean"]:.0%} (±{q.get("sd", 0):.0%}) <small>{e(origin)}</small></li>')
    rankings = [f'<li>{e(level(setting))}: {e(s.get("source", "Public ranking"))}: {e(METRICS.get(s.get("metric"), plain(s.get("metric", "unknown"))))} {metric_score(s)}; weight {s.get("weight", 0):g}; {e(s.get("read_on") or "undated")}; {e(", ".join(FAMILIES.get(f, plain(f)) for f in s.get("families", [])))}</li>' for setting in rows for s in setting.get('outside', [])]
    prices = []
    flags = set()
    for row in rows:
        flags.update(row.get('flags', []))
        for s in row.get('sources', []):
            flags.update(s.get('flags', []))
            price, cost = s.get('price_1m', {}), s.get('cost', {})
            money = lambda v: 'unknown' if v is None else f'${v:g}'
            prices.append(f'<li>{e(level(row))}: {e(POOLS.get(s.get("pool"), plain(s.get("pool"))))} / {e(HARNESSES.get(s.get("harness"), plain(s.get("harness"))))}: {money(price.get("in"))} in / {money(price.get("out"))} out per million tokens (API equivalent). Allowance: {e(cost.get("percent"))}%/task; estimated dollars {money(cost.get("estimated_usd"))}; scarcity weight {e(s.get("quota_price"))}; {e(cost.get("basis", "unknown"))}. <small>{e(s.get("quota_state", "UNKNOWN"))}{e(" until " + s["quota_reset_at"] if s.get("quota_reset_at") else "")} · {e(availability(dict(row, sources=[s])))}; source timing: {e(s.get("latency") or "unmeasured")}</small></li>')
    settings = []
    for r in rows:
        estimates = '; '.join(f'{FAMILIES.get(f, plain(f))} {q["mean"]:.0%} ±{q.get("sd", 0):.0%}' for f, q in measured(r, r.get('quality', {})).items())
        settings.append(f'<tr><th scope="row">{e(level(r))}</th><td>{e(availability(r))}<br>{badge(r, FAMILIES)}</td><td>{e(estimates or "Unmeasured")}</td><td>{e(str(r["latency_s"]) + "s" if r.get("latency_s") else "Unmeasured")}</td></tr>')
    notes = []
    for flag in sorted(flags):
        translation = FLAGS.get(flag)
        if not translation and flag.startswith('imputed:'):
            translation = FAMILIES.get(flag.split(':', 1)[1], 'Task') + ' estimate inferred from related benchmarks'
        notes.append(f'<li>{e(translation or plain(flag))} <code>{e(flag)}</code></li>')
    return (f'<div class="nerd-panel"><p>Model: <code>{e(best["model"])}</code></p><p>These are rough starting estimates, updated by local checks. Compare the same task and tool; held-out evaluations stay separate.</p>'
            '<h4>Our checks: task scores at best setting</h4><ul>' + (''.join(scores) or '<li>No measured task scores</li>') + '</ul>'
            '<h4>Outside benchmark readings</h4><ul>' + (''.join(rankings) or '<li>No outside scores</li>') + '</ul>'
            '<h4>Cost and quota: subscription and cost</h4><ul>' + (''.join(dict.fromkeys(prices)) or '<li>No configured source</li>') + '</ul>'
            '<h4>All thinking levels</h4><table><thead><tr><th>Setting</th><th>Status / evidence</th><th>Task estimates</th><th>Reply speed</th></tr></thead><tbody>' + ''.join(settings) + '</tbody></table>'
            '<h4>Data notes and cautions</h4><ul>' + (''.join(notes) or '<li>No additional flags</li>') + '</ul></div>')


def render_body(cards):
    e = lambda x: escape(str(x))
    rows = cards.get('rows', [])
    groups = defaultdict(list)
    for row in rows:
        groups[row['model']].append(row)
    bests = {}
    for model, settings in groups.items():
        bests[model] = max(settings, key=lambda r: (bool(sources(r)) and bool(local(r, FAMILIES)),
            bool(local(r, FAMILIES)), bool(sources(r)) and bool(r.get('outside')), bool(r.get('outside')),
            quality_score(r, FAMILIES, 0), r.get('level') in {'high', 'medium', 'provider-default'}))
    main, pending, off = [], [], []
    for model, settings in sorted(groups.items()):
        best = bests[model]
        good, bad = summary_phrases(best, settings)
        active = any(sources(r) for r in settings)
        known = any(has_evidence(r) for r in settings)
        model_routes = dict(best, sources=[s for r in settings for s in r.get('sources', [])])
        status = availability(model_routes)
        prior = any(r.get('owner_prior') for r in settings)
        row = (f'<article id="{e(anchor(model))}"><details class="model-disclosure"><summary class="model-line">'
               f'<span><strong>{e(name(best))}</strong>{badge(best, FAMILIES)}'
               f'{"<span class=conf>New · provisional</span>" if prior else ""}</span>'
               f'<span><small>Good at</small>{e(good)}</span><span><small>Limits</small>{e(bad)}</span>'
               f'<span><small>Cost / availability</small>{e(cost_feel(model_routes))}<br>{e(status)}</span>'
               f'<span class="details-label">Details ({len(settings)} settings)</span></summary>'
               + nerd_panel(best, settings) + '</details></article>')
        (main if active and known else pending if active else off).append(row)
    jobs = []
    for key, title, families, picks in select_jobs(rows):
        choices = []
        if key == 'design':
            prior, working = design_choices(rows)
            for label, r in [('Top pick · your judgement', prior), ('Working choice', working)]:
                if r is None:
                    text = ('No owner’s prior for design is configured.' if 'Top pick' in label else
                            'Missing WebDev Arena or visual-family outside ranking for available models. Use your owner’s prior for design when its allowance returns.')
                    choices.append(f'<div class="job-pick"><small>{label}</small><p>{e(text)}</p></div>')
                    continue
                evidence = 'Guess · owner’s prior' if 'Top pick' in label else 'Outside rankings only'
                choices.append(f'<div class="job-pick"><small>{label}</small><a class="pick-name" href="#{quote(anchor(r["model"]), safe="")}"><span translate="no">{e(name(r))}</span> <span class="pick-setting">{e(level(r))}</span></a><span class="conf">{e(evidence)}</span><p>{e(design_status(r))}</p><p class="cost-feel">{e(cost_feel(r))}</p></div>')
            jobs.append(f'<section class="job-row"><h3>{e(title)}</h3><div class="job-choices">{"".join(choices)}</div></section>')
            continue
        for i in range(2):
            label = 'Top pick' if i == 0 else 'Runner-up'
            if i >= len(picks):
                choices.append(f'<div class="job-pick"><small>{label}</small><p>No available model with evidence or an owner’s prior for this job yet.</p></div>')
                continue
            r = picks[i]
            choices.append(f'<div class="job-pick"><small>{label}</small><a class="pick-name" href="#{quote(anchor(r["model"]), safe="")}"><span translate="no">{e(name(r))}</span> <span class="pick-setting">{e(level(r))}</span></a>{badge(r, families)}<p>{e(why(r, families, key == "fast"))}</p><p class="cost-feel">{e(cost_feel(r))}</p></div>')
        jobs.append(f'<section class="job-row"><h3>{e(title)}</h3><div class="job-choices">{"".join(choices)}</div></section>')
    local_count = sum(any(local(r, FAMILIES) for r in rs) for rs in groups.values())
    outside_count = sum(any(r.get('outside') for r in rs) for rs in groups.values())
    new_count = sum(any(r.get('owner_prior') for r in rs) for rs in groups.values())
    try:
        date = datetime.fromisoformat(cards.get('read_on', '').replace('Z', '+00:00')).strftime('%d %B %Y')
    except (ValueError, AttributeError):
        date = 'not built yet'
    extras = [f'<li><a href="#{quote(anchor(model), safe="")}">{e(name(bests[model]))}</a>: {e(", ".join(level(r) for r in rs if not has_evidence(r)))}</li>' for model, rs in groups.items() if any(has_evidence(r) for r in rs) and any(not has_evidence(r) for r in rs)]
    return ('<div class="bench-page"><h1>What each model is best at</h1><p class="lede">Our own checks come first. Where we have no local test for a job, we label outside rankings or your starting judgement.</p>'
        f'<div class="bench-intro">{e(date)} · {local_count} locally tested · {outside_count} with outside rankings · {new_count} provisional admissions '
        '<a href="/model-cards.json" download="model-cards.json">Private model cards ↓</a></div>'
        '<p class="quiet">Working choices use current switches and quota readings. Unknown quota is not a guarantee of availability. Estimates are not calibrated success probabilities.</p>'
        + ('' if rows else '<p>No evidence or thinking levels configured. Import fit results or run a market refresh.</p>') + '<h2>Best models by job</h2><div class="jobs-ledger">' + ''.join(jobs) + '</div>'
        '<h2>Models switched on</h2><div class="model-list">' + (''.join(main) or '<p>No measured models switched on yet.</p>') + '</div>'
        f'<details class="unmeasured-box"><summary>Not measured yet ({len(pending) + len(off)} models)</summary><h3>Switched on, waiting for first benchmark</h3>'
        + (''.join(pending) or '<p>None</p>') + '<h3>Switched off / not in an active pool</h3>' + (''.join(off) or '<p>None</p>')
        + '<h3>Unmeasured thinking settings on measured models</h3><ul>' + (''.join(extras) or '<li>None</li>') + '</ul></details>'
        '<p class="quiet">Exports include private runtime publisher readings. Keep them private.</p></div>'
)
