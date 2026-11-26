"""The console's Usage and Share pages: cost estimates, how each allowance fills, and who owes what.

Pure rendering, no I/O. Charts are inline SVG with HTML labels (no chart library, nothing fetched);
colours come from the console's own variables, so light and dark follow the page.
"""
from __future__ import annotations

import datetime as dt
import html
import json
from typing import Any, Callable

W, H = 300, 96          # chart viewBox; stretched to the column, so type lives in HTML, never in the SVG
SERIES = ("s1", "s2", "s3", "s4")
# Each provider draws in its own brand colour (console.css `.pv-*`); the page chrome keeps Castor's.
BRANDS = (("claude", "claude"), ("anthropic", "claude"), ("codex", "openai"), ("chatgpt", "openai"),
          ("openai", "openai"), ("antigravity", "google"), ("gemini", "google"), ("google", "google"),
          ("openrouter", "openrouter"), ("opencode", "opencode"), ("copilot", "copilot"), ("github", "copilot"))


def brand(pool_id: str) -> str:
    return next((name for needle, name in BRANDS if needle in str(pool_id).lower()), "other")


def brand_classes(pool_ids: list[str]) -> list[str]:
    """One class string per pool: its brand, and v1, v2... when the same brand is drawn again."""
    seen: dict[str, int] = {}
    out = []
    for pool_id in pool_ids:
        name = brand(pool_id)
        n = seen.get(name, 0)
        seen[name] = n + 1
        out.append(f"pv pv-{name}" + (f" v{min(n, 2)}" if n else ""))
    return out
TAGS = {"title": "h3", "sub": "p", "plan": "h4", "row": "p", "total": "p", "note": "p"}
BASIS = "Estimates at published API list prices, from usage logs on this Mac. Not what you pay: plans are flat."


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value else None


def money(value: Any, *, floor: bool = False) -> str:
    value = _num(value)
    if value is None:
        return '<span class="us-none" title="Some models in this window have no list price">—</span>'
    text = f"${value:,.2f}" if value >= 0.01 or value == 0 else f"${value:.4f}"
    return f'<span class="num">{"≥" if floor else ""}{text}</span>'


def tokens(value: Any) -> str:
    value = _num(value)
    if value is None:
        return ""
    for size, unit in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if value >= size:
            return f"{value / size:.1f}{unit}".replace(".0", "")
    return f"{value:.0f}"


def _day_label(day: str) -> str:
    try:
        when = dt.date.fromisoformat(day)
    except ValueError:
        return day
    return f"{when.day} {when:%b}"


def _clock(epoch: float, span: float) -> str:
    when = dt.datetime.fromtimestamp(epoch).astimezone()
    return f"{when:%H:%M}" if span <= 86400 * 1.5 else f"{when.day} {when:%b}"


# ---- cost ---------------------------------------------------------------------------------------------

def cost_rows(pools: list[dict[str, Any]], title: Callable[[dict[str, Any]], str]) -> list[dict[str, Any]]:
    rows = []
    for pool in pools:
        cost = pool.get("usage_cost") or {}
        if not cost or all(_num(cost.get(key)) is None for key in ("today_usd", "last_7d_usd", "last_30d_usd")):
            continue
        rows.append({"pool": pool["pool"], "name": title(pool), "cost": cost})
    rows.sort(key=lambda row: -(_num(row["cost"].get("last_30d_usd")) or 0))
    return rows


def _total(rows: list[dict[str, Any]], key: str) -> str:
    known = [_num(row["cost"].get(key)) for row in rows]
    total = sum(v for v in known if v is not None)
    return money(total, floor=any(v is None for v in known))


def cost_table(rows: list[dict[str, Any]]) -> str:
    body = []
    classes = brand_classes([row.get("pool") or row["name"] for row in rows])
    for index, row in enumerate(rows):
        cost = row["cost"]
        count = tokens(cost.get("last_30d_tokens"))
        body.append(
            f'<tr><th scope="row"><i class="key {classes[index]}" aria-hidden="true"></i>{html.escape(row["name"])}'
            + (f'<small>{count} tokens in 30 days</small>' if count else '') + '</th>'
            f'<td>{money(cost.get("today_usd"))}</td><td>{money(cost.get("last_7d_usd"))}</td>'
            f'<td>{money(cost.get("last_30d_usd"))}</td></tr>')
    foot = ('<tfoot><tr><th scope="row">Total</th>' + ''.join(f'<td>{_total(rows, key)}</td>'
            for key in ("today_usd", "last_7d_usd", "last_30d_usd")) + '</tr></tfoot>') if len(rows) > 1 else ''
    return ('<table class="us-table"><thead><tr><th scope="col">Provider</th><th scope="col">Today</th>'
            '<th scope="col">7 days</th><th scope="col">30 days</th></tr></thead>'
            f'<tbody>{"".join(body)}</tbody>{foot}</table>')


def daily_chart(rows: list[dict[str, Any]]) -> str:
    """Stacked bars: one per day for 30 days, one colour per provider."""
    series = [(index, row) for index, row in enumerate(rows) if row["cost"].get("daily")]
    classes = brand_classes([row.get("pool") or row["name"] for row in rows])
    if not series:
        return '<p class="us-empty">Daily cost appears after the next usage scan.</p>'
    days = [item["day"] for item in series[0][1]["cost"]["daily"]]
    n = len(days)
    by_day = [[(index, _num(dict((d["day"], d["usd"]) for d in row["cost"]["daily"]).get(day)) or 0.0)
               for index, row in series] for day in days]
    top = max([sum(v for _, v in stack) for stack in by_day] + [0.0])
    if top <= 0:
        return '<p class="us-empty">No priced use in the last 30 days.</p>'
    step = W / n
    gap = min(1.2, step * 0.2)
    bars = []
    for i, stack in enumerate(by_day):
        y = H
        for index, value in stack:
            if value <= 0:
                continue
            h = H * value / top
            y -= h
            bars.append(f'<rect class="{classes[index]}" x="{i * step + gap / 2:.2f}" y="{y:.2f}" '
                        f'width="{step - gap:.2f}" height="{max(h, .6):.2f}"><title>{_day_label(days[i])}: ${value:,.2f}</title></rect>')
    keys = ''.join(f'<span class="key-label"><i class="key {classes[index]}" aria-hidden="true"></i>'
                   f'{html.escape(row["name"])}</span>' for index, row in series)
    return (f'<figure class="chart" role="group" aria-label="Cost per day over the last 30 days">'
            f'<figcaption>{keys}</figcaption>'
            f'<div class="plot"><span class="yl top num">${top:,.2f}</span><span class="yl zero num">$0</span>'
            f'<svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" aria-hidden="true">'
            f'<line class="grid" x1="0" y1="{H}" x2="{W}" y2="{H}"/>{"".join(bars)}</svg></div>'
            f'<div class="xl"><span>{_day_label(days[0])}</span><span>{_day_label(days[-1])}</span></div></figure>')


def model_table(rows: list[dict[str, Any]], pretty: Callable[[str], str]) -> str:
    entries = []
    for row in rows:
        for item in row["cost"].get("by_model") or []:
            usd = _num(item.get("usd"))
            entries.append({"name": pretty(str(item.get("model") or "Unknown model")), "pool": row["name"],
                            "usd": usd, "pool_id": row.get("pool") or row["name"], "week": item.get("usd_7d"), "today": item.get("usd_today"),
                            "tokens": item.get("tokens")})
    if not entries:
        return ''
    entries.sort(key=lambda e: (-(e["usd"] if e["usd"] is not None else -1), e["name"]))
    entries = entries[:10]
    peak = max([e["usd"] or 0 for e in entries] + [0.0001])
    body = ''.join(
        f'<tr><th scope="row">{html.escape(e["name"])}<small>{html.escape(e["pool"])}</small></th>'
        f'<td class="us-bar"><span class="us-cell">{bar((e["usd"] or 0) / peak * 100, None, "us-mbar pv pv-" + brand(e["pool_id"]))}{money(e["usd"])}</span></td>'
        f'<td>{money(e["week"])}</td><td>{money(e["today"])}</td></tr>' for e in entries)
    return ('<table class="us-table us-models"><thead><tr><th scope="col">Model</th><th scope="col">30 days</th>'
            f'<th scope="col">7 days</th><th scope="col">Today</th></tr></thead><tbody>{body}</tbody></table>')


# ---- allowances ---------------------------------------------------------------------------------------

def _epoch(value: Any) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def bar(used: float, at: float | None = None, cls: str = "us-track") -> str:
    """A 0-100 bar as SVG: the page's CSP forbids style attributes, and widths must come from the data."""
    tick = f'<rect class="k" x="{min(max(at, 0), 99.4):.2f}" y="0" width="0.6" height="10"/>' if at is not None else ''
    return (f'<svg class="{cls}" viewBox="0 0 100 10" preserveAspectRatio="none" aria-hidden="true">'
            f'<rect class="t" y="2" width="100" height="6"/><rect class="f" y="2" width="{min(max(used, 0), 100):.2f}" height="6"/>'
            f'{tick}</svg>')


def _label(limit: dict[str, Any]) -> str:
    return f'{limit.get("title") or limit.get("name")}' + (f' · {limit["scope"]}' if limit.get("scope") else '')


def pace_rows(pools: list[dict[str, Any]], title: Callable[[dict[str, Any]], str]) -> str:
    groups = []
    for pool in pools:
        items = []
        for limit in pool.get("limits") or []:
            if limit.get("kind") != "window" or _num(limit.get("used_percent")) is None:
                continue
            used = min(100.0, max(0.0, limit["used_percent"]))
            pace = limit.get("pace") or {}
            elapsed = _num(pace.get("elapsed_percent"))
            behind = pace.get("status") == "deficit" or used >= 100
            words = pace.get("words") or ("full" if used >= 100 else "")
            reset = (limit.get("note") if limit.get("active") is False else
                     f'resets {limit["resets"]}' if limit.get("resets") else "reset unknown")
            items.append(
                f'<li class="{"behind" if behind else ""}"><span class="nm">{html.escape(_label(limit))}</span>'
                f'{bar(used, elapsed)}'
                f'<span class="num pc">{used:g}%</span><small>{html.escape(" · ".join(filter(None, [reset, words])))}</small></li>')
        if items:
            groups.append(f'<section class="us-pace pv pv-{brand(pool["pool"])}"><h3>{html.escape(title(pool))}</h3><ul>{"".join(items)}</ul></section>')
    return ''.join(groups) + ('<p class="us-note">The tick is how much of the window has passed; a fill beyond it is running ahead of an even pace.</p>'
                              if groups else '')


def _steps(points: list[tuple[float, float]], t0: float, span: float, now: float) -> tuple[str, str]:
    x = lambda t: (t - t0) / span * W
    y = lambda v: H - H * v / 100
    path = f"M{x(points[0][0]):.2f},{y(points[0][1]):.2f}"
    for (_, _before), (t, value) in zip(points, points[1:]):
        path += f"H{x(t):.2f}V{y(value):.2f}"
    path += f"H{x(now):.2f}"
    return path, path + f"V{H}H{x(points[0][0]):.2f}Z"


def history_charts(pools: list[dict[str, Any]], group_title: Callable[[str, list[dict[str, Any]]], str],
                   line_label: Callable[[dict[str, Any], dict[str, Any]], str],
                   series: dict[str, dict[str, dict[str, Any]]], now: float) -> str:
    """One chart per subscription: a stepped line for each window of a day or longer, 0-100% of its allowance.

    Five-hour windows are left out (they refill before a chart means anything). The time axis starts at the
    oldest reading in view, 1 to 14 days back, so a young history is not squeezed into a corner."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for pool in pools:
        groups.setdefault(pool.get("subscription") or pool["pool"], []).append(pool)
    drawn, earliest, waiting = [], now, False
    for group_id, members in groups.items():
        lines = []
        for pool in members:
            for limit in pool.get("limits") or []:
                minutes = limit.get("minutes")
                if (limit.get("kind") != "window" or not isinstance(minutes, int) or minutes < 1440
                        or _num(limit.get("used_percent")) is None):
                    continue
                recorded = (series.get(pool["pool"]) or {}).get(limit["name"]) or {}
                points = [(t, u) for t, u in recorded.get("points", []) if now - 14 * 86400 <= t <= now]
                lines.append((pool, limit, points))
        if not lines:
            continue
        if not any(len(points) >= 2 for _, _, points in lines):
            waiting = True
            continue
        earliest = min([earliest] + [points[0][0] for _, _, points in lines if len(points) >= 2])
        drawn.append((group_id, members, lines))
    if not drawn:
        return ('<p class="us-empty">The history of each allowance fills in as readings arrive; it starts when this '
                'version first ran, and nothing earlier is back-filled.</p>' if waiting or not groups else '')
    span = min(14 * 86400, max(86400, now - earliest))
    t0 = now - span
    out = []
    for group_id, members, lines in drawn:
        shapes, keys = [], []
        for index, (pool, limit, points) in enumerate(lines):
            points = [(max(t, t0), u) for t, u in points if t >= t0 - 1] or []
            if not points or points[-1][0] < now - 1:
                points.append((now, float(limit["used_percent"])))
            path, area = _steps(points, t0, span, now)
            cls = f"pv pv-{brand(pool['pool'])}" + (f" v{min(index, 2)}" if index else "")
            shapes.append(f'<path class="area {cls}" d="{area}"/><path class="line {cls}" d="{path}"/>')
            keys.append(f'<span class="key-label"><i class="key {cls}" aria-hidden="true"></i>{html.escape(line_label(pool, limit))}'
                        f' <span class="num">{limit["used_percent"]:g}%</span></span>')
        name = group_title(group_id, members)
        out.append(
            f'<figure class="chart mini" role="img" aria-label="{html.escape(name)}: share of each allowance used over time">'
            f'<figcaption><b>{html.escape(name)}</b><span class="keys">{"".join(keys)}</span></figcaption>'
            f'<div class="plot"><span class="yl top num">100%</span><span class="yl zero num">0</span>'
            f'<svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" aria-hidden="true">'
            f'<line class="grid" x1="0" y1="{H}" x2="{W}" y2="{H}"/><line class="grid" x1="0" y1="0" x2="{W}" y2="0"/>'
            f'{"".join(shapes)}</svg></div>'
            f'<div class="xl"><span>{_clock(t0, span)}</span><span>now</span></div></figure>')
    return (f'<div class="us-grid">{"".join(out)}</div>'
            '<p class="us-note">Share of each allowance used. A drop is a reset. History starts when this version '
            'first ran; nothing earlier is back-filled.</p>')


# ---- pages ----------------------------------------------------------------------------------------------

def render_usage(overview: dict[str, Any], series: dict[str, dict[str, dict[str, Any]]], now: float,
                 title: Callable[[dict[str, Any]], str], pretty: Callable[[str], str], stamp: str,
                 share: dict[str, Any] | None = None, window_title: Callable[[dict[str, Any]], str] | None = None,
                 group_title: Callable[[str, list[dict[str, Any]]], str] | None = None,
                 line_label: Callable[[dict[str, Any], dict[str, Any]], str] | None = None) -> str:
    window_title = window_title or title
    group_title = group_title or (lambda _id, members: title(members[0]))
    line_label = line_label or (lambda _pool, limit: _label(limit))
    pools = [p for p in overview.get("pools", []) if p.get("pool")]
    rows = cost_rows(pools, title)
    people = ((share or {}).get("people") or [])
    link = ('<a class="us-action" href="/share">Share costs</a>' if people else
            '<a class="us-action" href="/settings#subscriptions">Set prices to share costs</a>')
    cost = (f'<section class="us-cost" aria-labelledby="us-cost-h"><h2 id="us-cost-h">Cost</h2>'
            f'<p class="us-basis">{html.escape(BASIS)}</p>{cost_table(rows)}'
            f'<h3>Cost per day</h3>{daily_chart(rows)}'
            + (f'<h3>By model</h3>{model_table(rows, pretty)}' if any(r["cost"].get("by_model") for r in rows) else '')
            + '</section>') if rows else (
        '<section class="us-cost"><h2>Cost</h2><p class="us-empty">No usage logs are read yet. Turn on '
        '<code>local_usage_cost</code> for a pool in the roster to see its estimated cost here.</p></section>')
    allowances = (f'<section class="us-allow" aria-labelledby="us-allow-h"><h2 id="us-allow-h">Allowances</h2>'
                  f'{pace_rows(pools, window_title)}<h3>Over time</h3>{history_charts(pools, group_title, line_label, series, now) or "<p class=us-empty>No allowance windows yet.</p>"}'
                  '</section>')
    return (f'<div class="us"><div class="wb-head us-head"><h1>Usage</h1>{link}</div>'
            f'<p class="stamp">{html.escape(stamp)}</p>{cost}{allowances}</div>')


def card_html(model: dict[str, Any], person: dict[str, Any], lines: list[tuple[str, str]], text: str) -> str:
    esc = html.escape
    out = []
    for kind, content in lines:
        tag = TAGS[kind]
        out.append(f'<{tag} class="sc-{kind}">{esc(content)}</{tag}>')
    key = esc(person["key"])
    return (f'<article class="share-card" id="card-{key}" data-person="{key}" data-lines="{esc(json.dumps(lines))}">'
            f'<div class="share-sheet">{"".join(out)}</div>'
            f'<div class="share-actions"><button type="button" class="wb-add" data-share-copy>Copy text</button>'
            f'<button type="button" class="wb-add" data-share-image>Save image</button>'
            f'<a class="wb-add" href="/share/print?person={key}" target="_blank" rel="noopener">Print</a>'
            f'<span class="share-status" role="status"></span></div>'
            f'<textarea class="share-text" readonly aria-label="Plain text for {esc(person["name"])}" hidden>{esc(text)}</textarea></article>')


CANNOT = (
    "Who used a shared login. The provider reports one total for the account, and nothing in it names a person.",
    "Use outside this Mac: the web, a phone, another computer, or another person's own tools.",
    "What anyone pays. The numbers are list prices; plans are flat, so what each person owes is the agreed share.",
)


def render_share(model: dict[str, Any], cards: list[str]) -> str:
    if model.get("unavailable"):
        body = '<p class="us-empty">The subscriptions file is unreadable. Check its format and mode (600).</p>'
    elif not cards:
        body = ('<p class="us-empty">Nothing to share yet. Enter a price and your share for a subscription in '
                '<a href="/settings#subscriptions">Settings</a>; a person appears once someone else pays a part.</p>')
    else:
        body = f'<div class="share-list">{"".join(cards)}</div>'
    cannot = ''.join(f'<li>{html.escape(line)}</li>' for line in CANNOT)
    return (f'<div class="us share"><div class="wb-head us-head"><h1>Share</h1><a class="us-action" href="/usage">Usage</a></div>'
            '<p class="us-basis">One card per person: what they owe, and what this Mac measured against the account\'s total. '
            'Copy it as text, save it as an image, or print it.</p>'
            f'{body}<section class="us-cannot"><h2>Cannot be known here</h2><ul>{cannot}</ul>'
            '<p>A second person running Crossfeed could send their own measured use for the same period; '
            'see docs/SHARING.md.</p></section></div>')


def render_print(lines: list[tuple[str, str]]) -> str:
    """Only the card; the stylesheet hides everything else when printing."""
    out = ''.join(f'<{TAGS[kind]} class="sc-{kind}">{html.escape(text)}</{TAGS[kind]}>' for kind, text in lines)
    return (f'<article class="share-sheet print-sheet">{out}</article>'
            '<p class="print-bar"><button type="button" class="wb-add" data-print>Print or save as PDF</button> '
            '<a href="/share">Back</a></p>')
