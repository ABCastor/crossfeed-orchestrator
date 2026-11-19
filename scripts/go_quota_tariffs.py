"""Model-specific request estimates from https://opencode.ai/go (2026-11-19).

The numbers are OpenCode's typical-workload estimates, not a fixed number of
requests included, a provider debit, or cash. Five-hour and weekly budgets are
20% and 50% of the monthly budget. Free lanes have separate caps, not a zero
cost in this meter. Temporary promotions are deliberately not hard-coded.
"""

SOURCE = "https://opencode.ai/go, read 2026-11-19"
# Five-hour requests: (Go, Go Plus). Other windows use provider budget ratios.
FIVE_HOUR = {
    "kimi-k3": (98, 392), "kimi-k3.1-pro": (147, 588),
    "qwen3.8-max": (162, 647), "grok-4.7": (169, 676),
    "step-5-preview": (426, 1700), "qwen4-max": (463, 1850),
    "gemini-4-argon": (566, 2260), "glm-5.2": (792, 2370),
    "kimi-k2.7-code": (994, 2980), "deepseek-v4-pro": (1040, 4180),
    "hy4-preview": (1350, 5410), "gpt-5.6-luna": (2050, 8200),
    "hy4": (2710, 10800), "minimax-m3": (3210, 9620),
    "mimo-v2.6-pro": (3260, 13000), "claude-haiku-5-5": (3850, 15400),
    "minimax-m3.1-flash": (3880, 15500), "gpt-6-luna": (4230, 16900),
    "hy3": (4300, 17200), "qwen3.7-plus": (4310, 12900),
    "qwen3.8-flash": (5400, 16200), "glm-5.3-flash": (6320, 18900),
    "longcat-2.0": (11400, 45700), "mimo-v2.6-flash": (30100, 60200),
    "deepseek-v4.1-flash": (43300, 86700),
    "muse-spark-1.2-contributor": (45300, 90600), "muse-spark-1.3-contributor": (45300, 90600),
}


def typical_requests(plan, model, column):
    if not isinstance(plan, dict) or model not in FIVE_HOUR or column not in (0, 1, 2):
        return None
    name = str(plan.get("name") or "").strip().casefold()
    tier = {"opencode go": 0, "opencode go plus": 1}.get(name)
    if tier is None:
        return None  # no inference from an unrecognized plan, amount or currency
    return FIVE_HOUR[model][tier] * (1, 2.5, 5)[column]
