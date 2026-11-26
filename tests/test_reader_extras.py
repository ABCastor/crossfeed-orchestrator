"""Display-only reader evidence: no use of local sign-ins or network in these tests."""
import datetime as dt
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_native_usage import NOW, Response, TOKEN, fleetctl, native


class ReaderExtras(unittest.TestCase):
    def test_codex_separates_reset_inventory_from_spend_credits(self):
        expiry = native._iso(NOW + dt.timedelta(days=2))
        renewal = native._iso(NOW + dt.timedelta(days=30))
        available = {"id": "private-inventory-id", "status": "available", "expires_at": expiry}
        expired = {**available, "expires_at": native._iso(NOW)}
        inventory = {"available_count": 3, "credits": [available, expired, {"status": "available"},
                                                         {"status": "redeemed", "expires_at": expiry}]}
        usage = {"credits": {"balance": "123.50", "expires_at": expiry, "renews_at": renewal}}
        seen = []

        def opener(request, timeout):
            seen.append(request.full_url)
            self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
            return Response(json.dumps(usage if request.full_url == native.CODEX_USAGE_URL else inventory))

        auth = json.dumps({"tokens": {"access_token": TOKEN}}).encode()
        with patch.object(native, "_owner_file", return_value=auth), patch.object(native, "_now", return_value=NOW):
            snapshot = native.observe_codex_oauth({"reset_credits": True}, opener=opener)
        self.assertEqual(seen, [native.CODEX_USAGE_URL, native.CODEX_RESET_CREDITS_URL])
        self.assertEqual(snapshot["credits"], {"balance": 2, "unit": "limit resets", "expires_at": expiry,
                                               "renews_at": None, "source": native.CODEX_RESET_CREDITS_SOURCE,
                                               "expirations": [expiry, None]})
        self.assertEqual(snapshot["spend_credits"]["balance"], 123.5)
        self.assertEqual(snapshot["spend_credits"]["renews_at"], renewal)
        self.assertEqual(snapshot["windows"], {})
        self.assertNotIn("private-inventory-id", json.dumps(snapshot))

    def test_claude_extra_is_paid_headroom_in_major_currency_units(self):
        raw = {"is_enabled": True, "monthly_limit": 500, "used_credits": 125, "currency": "EUR"}
        snapshot = native.parse_claude(json.dumps({"extra_usage": raw}), observed_at=NOW)
        self.assertEqual(snapshot["windows"], {})
        self.assertEqual(snapshot["credits"], {"balance": 3.75, "unit": "EUR", "limit": 5.0, "used": 1.25,
                                               "kind": "extra_usage", "renews_at": None, "expires_at": None,
                                               "source": native.CLAUDE_SOURCE})
        self.assertIsNone(native.parse_claude(json.dumps({"extra_usage": {**raw, "is_enabled": False}})))

    def test_openrouter_aggregates_30_completed_utc_days_by_model(self):
        def row(days, model, usd, requests):
            return {"date": (NOW.date() - dt.timedelta(days=days)).isoformat(), "model": model,
                    "usage": usd, "requests": requests, "provider": "not published"}
        data = [row(1, "openai/gpt", 1.25, 2), row(30, "openai/gpt", 2.5, 1),
                row(4, "openrouter/free", 0, 10), row(0, "openai/gpt", 50, 99), row(31, "openai/gpt", 50, 99)]
        self.assertEqual(native.parse_openrouter_activity({"data": data}, NOW.date()),
                         [{"model": "openai/gpt", "usd_30d": 3.75, "requests": 3},
                          {"model": "openrouter/free", "usd_30d": 0, "requests": 10}])
        self.assertIsNone(native.parse_openrouter_activity({"data": [{**data[0], "usage": "bad"}]}, NOW.date()))

    def test_openrouter_management_rejection_still_returns_cash_not_zero_per_model(self):
        def opener(request, timeout):
            self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
            if request.full_url.endswith("/activity"):
                return Response('{}', 403)
            return Response('{"data":{"total_credits":1,"total_usage":0,"usage_daily":0}}')
        with patch.object(native, "_owner_file", return_value=TOKEN.encode()), \
                patch.object(native, "_management_key", return_value=None):
            snapshot = native.observe_openrouter({"activity": True}, opener=opener)
        self.assertTrue(snapshot["available"])
        self.assertIn("management key", snapshot["per_model_error"])
        self.assertNotIn("per_model", snapshot)
        self.assertEqual(snapshot["account_usage"]["account_balance_usd"], 1.0)

    def test_usage_reports_measurement_despite_forced_or_declared_routing(self):
        old = native._iso(NOW - dt.timedelta(days=2))
        reset = native._iso(NOW + dt.timedelta(days=3))
        pool_rows = {"codex": {"quota_refresh": {"oracle": "codex-oauth"}},
                     "chatgpt-work": {"plan": {"limit": "none-known"}},
                     "openrouter-free": {"plan": {"limit": "none-known"},
                                         "quota_refresh": {"oracle": "openrouter", "ttl_s": 1800}}}
        runtime = {"switches": {"codex": "forced"}, "quota_snapshots": {
            "codex": {"source": native.CODEX_OAUTH_SOURCE, "observed_at": native._iso(NOW),
                      "windows": {"weekly": {"used_percent": 92, "reset_at": reset}}},
            "chatgpt-work": {"source": "codexbar", "observed_at": old,
                              "windows": {"weekly": {"used_percent": 63, "reset_at": old}}},
            "openrouter-free": {"source": native.OPENROUTER_SOURCE, "observed_at": old, "windows": {},
                                "account_usage": {"account_balance_usd": 1.5}},
        }, "quota_refresh_errors": {"openrouter-free": {"reason": "unreachable", "attempted_at": native._iso(NOW)}}}
        roster = {"quota_pools": pool_rows, "lanes": []}
        with tempfile.TemporaryDirectory() as folder, patch.object(fleetctl, "utc_now", return_value=NOW):
            state = Path(folder)
            (state / "runtime.json").write_text(json.dumps(runtime))
            with patch("sys.stdout", new_callable=io.StringIO) as out:
                fleetctl.show_usage(state, state / "absent.db", True, roster)
            report = json.loads(out.getvalue())["quota_pools"]
            with patch("sys.stdout", new_callable=io.StringIO) as out:
                fleetctl.show_usage(state, state / "absent.db", False, roster)
            words = out.getvalue()
        self.assertEqual(report["codex"]["bottleneck_used_percent"], 92)
        self.assertEqual(report["codex"]["age_seconds"], 0)
        self.assertEqual(report["chatgpt-work"]["last_observed_windows"]["weekly"]["used_percent"], 63)
        self.assertEqual(report["chatgpt-work"]["age_seconds"], 172800)
        self.assertEqual(report["openrouter-free"]["source"], native.OPENROUTER_SOURCE)
        self.assertEqual(report["openrouter-free"]["age_seconds"], 172800)
        self.assertIn("openrouter-free (openrouter-account): account only — $1.50 balance; no percentage quota, stale", words)


class LocalCost(unittest.TestCase):
    def test_codex_cumulative_counters_deduplicate_and_bill_only_increment(self):
        model = {"gpt-fixture": {"cost": {"input": 2, "output": 10, "cache_read": .2}}}
        ts = native._iso(NOW)
        old = native._iso(NOW - dt.timedelta(days=31))
        baseline = {"input_tokens": 11, "output_tokens": 1, "cached_input_tokens": 10}
        final = {"input_tokens": 111, "output_tokens": 11, "cached_input_tokens": 50}
        def event(stamp, usage):
            return {"type": "event_msg", "timestamp": stamp, "payload": {"type": "token_count",
                    "info": {"total_token_usage": usage}}}
        records = [{"type": "turn_context", "payload": {"model": "gpt-fixture"}}, event(old, baseline),
                   event(ts, final), event(ts, final)]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "codex.jsonl"
            path.write_text("\n".join(map(json.dumps, records)))
            os.utime(path, (NOW.timestamp(), NOW.timestamp()))
            cost = native.local_usage_cost("codex", [root], model, NOW)
            unpriced = native.local_usage_cost("codex", [root], {}, NOW)
        self.assertEqual(cost["today_tokens"], 110)
        self.assertAlmostEqual(cost["last_30d_usd"], (60*2 + 40*.2 + 10*10)/1e6)
        self.assertIsNone(unpriced["last_30d_usd"])
        self.assertEqual(unpriced["last_30d_tokens"], 110)

    def test_claude_stream_counts_final_usage_and_cache_writes_once(self):
        model = {"claude-fixture": {"cost": {"input": 4, "output": 20, "cache_read": .2, "cache_write": 5}}}
        def record(out):
            return {"type": "assistant", "timestamp": native._iso(NOW), "requestId": "fixture",
                    "message": {"id": "msg", "model": "claude-fixture", "usage": {
                        "input_tokens": 10, "output_tokens": out, "cache_read_input_tokens": 10,
                        "cache_creation_input_tokens": 10, "cache_creation": {"ephemeral_1h_input_tokens": 5}}}}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "claude.jsonl"
            path.write_text("\n".join(map(json.dumps, [record(1), record(8), record(2)])))
            os.utime(path, (NOW.timestamp(), NOW.timestamp()))
            cost = native.local_usage_cost("claude", [root], model, NOW)
        self.assertEqual(cost["today_tokens"], 38)
        self.assertAlmostEqual(cost["today_usd"], (4*10 + 20*8 + .2*10 + 5*5 + 8*5)/1e6)

    def test_breakdown_gives_seven_days_daily_series_hours_and_cost_by_model(self):
        prices = {"claude-big": {"cost": {"input": 10, "output": 50}}, "claude-small": {"cost": {"input": 1, "output": 5}}}

        def record(when, model, out, tag):
            return {"type": "assistant", "timestamp": native._iso(when), "requestId": tag,
                    "message": {"id": tag, "model": model, "usage": {"input_tokens": 1000, "output_tokens": out}}}
        rows = [record(NOW, "claude-big", 1000, "a"),                                  # today
                record(NOW - dt.timedelta(days=3), "claude-small", 1000, "b"),         # inside 7 days
                record(NOW - dt.timedelta(days=20), "claude-big", 1000, "c"),          # only in 30 days
                record(NOW - dt.timedelta(days=2), "claude-unpriced", 1000, "d")]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "claude.jsonl"
            path.write_text("\n".join(map(json.dumps, rows)))
            os.utime(path, (NOW.timestamp(), NOW.timestamp()))
            full = native.local_usage_cost("claude", [Path(folder)], prices, NOW)
            path.write_text("\n".join(map(json.dumps, rows[:3])))
            clean = native.local_usage_cost("claude", [Path(folder)], prices, NOW)
        big, small = (10 * 1000 + 50 * 1000) / 1e6, (1000 + 5000) / 1e6
        self.assertAlmostEqual(clean["today_usd"], big)
        self.assertAlmostEqual(clean["last_7d_usd"], big + small)
        self.assertAlmostEqual(clean["last_30d_usd"], 2 * big + small)
        self.assertEqual(clean["last_7d_tokens"], 4000)
        self.assertEqual(len(clean["daily"]), 30)
        self.assertEqual(clean["daily"][-1]["day"], NOW.astimezone().date().isoformat())
        self.assertAlmostEqual(sum(day["usd"] for day in clean["daily"]), clean["last_30d_usd"])
        self.assertEqual([row["model"] for row in clean["by_model"]][:1], ["claude-big"])
        top = {row["model"]: row for row in clean["by_model"]}
        self.assertAlmostEqual(top["claude-big"]["usd"], 2 * big)
        self.assertAlmostEqual(top["claude-big"]["usd_7d"], big)
        self.assertAlmostEqual(top["claude-small"]["usd_7d"], small)
        self.assertEqual(top["claude-small"]["usd_today"], 0)
        # Hourly buckets keep only the last 8 days; the 20-day-old row is not there.
        self.assertEqual(len(clean["hourly"]), 2)
        self.assertAlmostEqual(sum(row[2] for row in clean["hourly"]), big + small)
        # One unpriced model hides the dollar total of every window it falls in, never turns into zero.
        self.assertIsNone(full["last_7d_usd"])
        self.assertIsNone(full["last_30d_usd"])
        self.assertAlmostEqual(full["today_usd"], big)

    def test_failed_scan_keeps_portable_last_good_cost_stale(self):
        config = {"oracle": "claude", "local_usage_cost": True}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache = root / "claude-cli-usage-cost.json"
            past = NOW - dt.timedelta(hours=1)
            native._write_owner_json(cache, {"observed_at": native._iso(past), "last_30d_usd": 42})
            with patch.object(native, "_cached_list_prices", return_value={}), \
                    patch.object(native, "local_usage_cost", side_effect=OSError):
                native._write_usage_cost("claude", cache)
            row = native.usage_cost(config, root, NOW, refresh=False)
            self.assertEqual((row["age_s"], row["stale"], row["last_30d_usd"]), (3600, True, 42))


if __name__ == "__main__":
    unittest.main()
