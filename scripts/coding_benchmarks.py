"""Exact published Terminal-Bench 4 cells, checked 2026-11-19.

This fallback supplements the AA free feed when coding metrics are null.
Never spread a Max result across efforts or a release across snapshots.
"""

READ_ON = "2026-11-19"
URL = "https://www.tbench.ai/"
# (immutable model/release key, published effort): (score %, error %, harness)
TERMINAL_BENCH_4 = {
    ("claude-fable-5-2", "max"): (65.8, 2.8, "Claude Code"),
    ("claude-opus-5-5", "max"): (65.8, 3.1, "Claude Code"),
    ("claude-sonnet-5-5", "max"): (63.9, 3.0, "Claude Code"),
    ("claude-haiku-5-5", "max"): (40.0, 3.6, "Claude Code"),
    ("gpt-6.2-astra", "max"): (62.1, 2.3, "Codex"),
    ("gpt-6.2-sol", "max"): (61.5, 2.8, "Codex"),
    ("gpt-6-luna", "max"): (15.2, 2.7, "Codex"),
    ("gemini-3.8-flash", "high"): (18.2, 3.4, "mini-SWE-agent"),
    ("glm-5.3-flash", "max"): (37.0, 3.6, None),
    ("glm-5.3", "max"): (41.2, 3.3, None),
    ("kimi-k3.1-pro", "max"): (59.7, 4.2, None),
    ("kimi-k3", "max"): (23.0, 3.7, None),
    ("mimo-v2.6-pro", "none"): (27.0, 3.6, "mini-SWE-agent"),
    ("mimo-v2.6-flash", "none"): (16.1, 3.4, "mini-SWE-agent"),
}
# The 1017 result is not for the older generic Flash ID.
SNAPSHOT_TERMINAL_BENCH_4 = {("deepseek-v4.1-flash", "1017", "max"): (38.5, 3.8, None)}

# Artificial Analysis model pages, v5, 2026-11-19. Exact effort rows,
# (Terminal-Bench 4 score %, weighted Intelligence Index output tokens/task).
# AA's Terminal-Bench measurements use a different setup from the official leaderboard.
AA_CLAUDE_55 = {
    "claude-opus-5-5": {
        "low": (31.3131, 19060), "medium": (52.5253, 43817),
        "high": (56.5657, 60071), "xhigh": (59.5960, 103395), "max": (59.5960, 173313),
    },
    "claude-sonnet-5-5": {
        "low": (20.7071, 20856), "medium": (29.7980, 30746),
        "high": (43.9394, 58421), "xhigh": (57.0707, 122205), "max": (63.6364, 285387),
    },
}
AA_MODEL_PAGES = "https://artificialanalysis.ai/models/"

TERMINAL_FIELDS = ("terminalbench_v4_0", "terminal_bench_4_0", "terminal_bench_4", "terminal_bench_4_score")
CODING_FIELDS = ("artificial_analysis_coding_index", "coding_index")
CODING_FAMILIES = ("coding-agent", "review", "debug")
