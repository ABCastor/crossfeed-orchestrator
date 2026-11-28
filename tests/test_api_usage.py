"""The API pools' spend blocks: one adapter per source, fake openers, a cache, never in a render.

No test reaches the network: the organisation cost APIs are answered by a fake opener, and the key
store is a function the test hands in.
"""
import datetime as dt
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_fleetctl import fleetctl

api_usage = fleetctl.api_usage
NOW = dt.datetime(2026, 11, 28, 12, 0, tzinfo=dt.timezone.utc)
ROSTER = {"quota_pools": {"gemini-metered": {}, "openrouter-free": {}, "chatgpt-work": {}, "claude": {"plan": {"billing": "subscription"}},
                          "acme-api": {"api_usage": {"source": "admin-cost-api", "provider": "openai"}}},
          "lanes": [{"lane_id": "gm-flash", "quota_pool": "gemini-metered"}, {"lane_id": "chatgpt:pro", "quota_pool": "chatgpt-work"}]}
SNAPSHOT = {"observed_at": "2026-11-28T11:00:00Z",
            "account_usage": {"account_balance_usd": 1.79, "account_credits_usd": 26.0, "key_limit_usd": 10,
                              "key_limit_remaining_usd": 3.39, "key_usage_daily_usd": 0.0,
                              "key_usage_weekly_usd": 0.09, "key_usage_monthly_usd": 6.61},
            "per_model": [{"model": "anthropic/claude-sonnet-5.5", "usd_30d": 3.1, "requests": 221}],
            "per_day": [{"date": "2026-11-27", "usd": 0.4, "requests": 12}]}


class Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, body, status=200):
        self.body, self.status, self.calls = body, status, []

    def __call__(self, request, timeout=None):
        self.calls.append((request.full_url, dict(request.header_items()), timeout))
        if self.status != 200:
            import urllib.error
            raise urllib.error.HTTPError(request.full_url, self.status, "x", {}, io.BytesIO(b"{}"))
        return Response(json.dumps(self.body).encode())


def day(offset, hour=10):
    return (NOW - dt.timedelta(days=offset)).replace(hour=hour).isoformat()


class Sources(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def runs(self, *records):
        (self.state / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    def test_api_pools_are_the_billed_by_token_credit_or_relay_ones_and_not_a_subscription(self):
        self.assertEqual(api_usage.api_pools(ROSTER), ["gemini-metered", "openrouter-free", "chatgpt-work", "acme-api"])

    def test_the_local_meter_sums_this_pools_runs_by_day_and_names_itself(self):
        self.runs({"ended_at": day(0), "quota_pool": "gemini-metered", "model": "gemini-flash", "tokens": {"total": 1000}, "cost": {"estimated_usd": 0.4}},
                  {"ended_at": day(0), "lane_id": "gm-flash", "model": "gemini-pro", "tokens": {"input": 5, "output": 6}, "cost": {"estimated_usd": 0.1}},
                  {"ended_at": day(2), "quota_pool": "gemini-metered", "model": "gemini-flash", "cost": {"estimated_usd": 0.25}},
                  {"ended_at": day(0), "quota_pool": "claude", "model": "opus", "cost": {"estimated_usd": 9}})
        block = api_usage.local_meter(self.state, ROSTER, "gemini-metered", NOW)
        self.assertEqual(block["source"], "local-meter")
        self.assertEqual(block["source_label"], "Measured by Crossfeed")
        self.assertEqual([d["date"] for d in block["days"]], ["2026-11-26", "2026-11-27", "2026-11-28"])
        self.assertEqual(block["days"][-1], {"date": "2026-11-28", "usd": 0.5, "requests": 2, "tokens": 1011})
        self.assertEqual(block["spend"], {"today_usd": 0.5, "week_usd": 0.75, "month_usd": 0.75})
        self.assertEqual(block["top_models"][0], {"model": "gemini-flash", "usd": 0.65, "requests": 2})

    def test_the_gemini_spend_ledgers_count_only_completed_calls(self):
        (self.state / "gemini-image-spend.json").write_text(json.dumps({"schema": "gemini-image-spend/v1", "days": {
            "2026-11-28": [{"status": "completed", "actual_usd": 0.134, "model": "gemini-3-pro-image"},
                           {"status": "pending", "reserved_usd": 5.0}]}}), encoding="utf-8")
        block = api_usage.local_meter(self.state, ROSTER, "gemini-metered", NOW)
        self.assertEqual(block["spend"]["today_usd"], 0.134)
        self.assertEqual(block["days"][-1]["requests"], 1)

    def test_a_pool_with_no_runs_has_no_block_and_a_torn_ledger_line_is_skipped(self):
        (self.state / "runs.jsonl").write_text('{"torn\n', encoding="utf-8")
        self.assertIsNone(api_usage.local_meter(self.state, ROSTER, "chatgpt-work", NOW))

    def test_a_relay_pool_reports_run_counts_not_invented_money(self):
        self.runs({"ended_at": day(0), "lane_id": "chatgpt:pro", "model": "chatgpt:pro", "tokens": {"total": 7}})
        block = api_usage.local_meter(self.state, ROSTER, "chatgpt-work", NOW)
        self.assertEqual(block["days"][-1]["requests"], 1)
        self.assertEqual(block["spend"]["month_usd"], 0.0)

    def test_openrouter_comes_from_the_saved_reading_without_a_request(self):
        block = api_usage.openrouter_block(SNAPSHOT, NOW)
        self.assertEqual((block["balance_usd"], block["credits_usd"], block["key_limit_usd"], block["key_remaining_usd"]),
                         (1.79, 26.0, 10.0, 3.39))
        self.assertEqual(block["spend"], {"today_usd": 0.0, "week_usd": 0.09, "month_usd": 6.61})
        self.assertEqual(block["top_models"], [{"model": "anthropic/claude-sonnet-5.5", "usd": 3.1, "requests": 221}])
        self.assertEqual(block["days"], [{"date": "2026-11-27", "usd": 0.4, "requests": 12}])
        self.assertEqual(block["source"], "openrouter-api")
        self.assertIsNone(api_usage.openrouter_block({"observed_at": "x"}, NOW))

    def test_the_openai_cost_report_is_read_with_the_admin_key_and_a_short_timeout(self):
        opener = FakeOpener({"data": [
            {"start_time": int(dt.datetime(2026, 11, 27, tzinfo=dt.timezone.utc).timestamp()),
             "results": [{"amount": {"value": 1.5, "currency": "usd"}, "line_item": "gpt-5, input"},
                         {"amount": {"value": 0.5, "currency": "usd"}, "line_item": "gpt-5, output"}]},
            {"start_time": int(dt.datetime(2026, 11, 28, tzinfo=dt.timezone.utc).timestamp()),
             "results": [{"amount": {"value": 2.0, "currency": "usd"}, "line_item": "gpt-5, input"}]}]})
        costs = api_usage.openai_costs("ADMIN", NOW, opener)
        url, headers, timeout = opener.calls[0]
        self.assertTrue(url.startswith("https://api.openai.com/v1/organization/costs?"))
        self.assertEqual(headers["Authorization"], "Bearer ADMIN")
        self.assertLessEqual(timeout, 10)
        self.assertEqual(costs["days"], [{"date": "2026-11-27", "usd": 2.0}, {"date": "2026-11-28", "usd": 2.0}])
        self.assertEqual(costs["spend"], {"today_usd": 2.0, "week_usd": 4.0, "month_usd": 4.0})
        self.assertEqual(costs["top_models"][0], {"model": "gpt-5, input", "usd": 3.5})

    def test_the_anthropic_cost_report_reads_cents_as_dollars(self):
        opener = FakeOpener({"data": [{"starting_at": "2026-11-28T00:00:00Z", "ending_at": "2026-11-29T00:00:00Z",
                                       "results": [{"amount": "1250.5", "currency": "USD", "model": "claude-sonnet-5-5", "description": "Tokens"}]}]})
        costs = api_usage.anthropic_costs("ADMIN", NOW, opener)
        url, headers, _ = opener.calls[0]
        self.assertTrue(url.startswith("https://api.anthropic.com/v1/organizations/cost_report?"))
        self.assertEqual(headers["X-api-key"], "ADMIN")
        self.assertEqual(costs["days"], [{"date": "2026-11-28", "usd": 12.505}])


class ActivityDays(unittest.TestCase):
    def test_the_activity_rows_are_summed_per_utc_day_oldest_first(self):
        payload = {"data": [{"date": "2026-11-27 00:00:00", "model": "a/b", "usage": 0.25, "requests": 4},
                            {"date": "2026-11-27 00:00:00", "model": "c/d", "usage": 0.15, "requests": 2},
                            {"date": "2026-11-26 00:00:00", "model": "a/b", "usage": 0.1, "requests": 1},
                            {"date": "2026-09-01 00:00:00", "model": "a/b", "usage": 9, "requests": 9}]}
        days = fleetctl.native_usage.parse_openrouter_activity_days(payload, dt.date(2026, 11, 28))
        self.assertEqual(days, [{"date": "2026-11-26", "usd": 0.1, "requests": 1}, {"date": "2026-11-27", "usd": 0.4, "requests": 6}])

    def test_an_unreadable_row_is_not_passed_off_as_a_complete_total(self):
        bad = {"data": [{"date": "not a date", "model": "a/b", "usage": 1, "requests": 1}]}
        self.assertIsNone(fleetctl.native_usage.parse_openrouter_activity_days(bad, dt.date(2026, 11, 28)))
        self.assertIsNone(fleetctl.native_usage.parse_openrouter_activity_days({"data": "x"}, dt.date(2026, 11, 28)))


class Refresh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.runtime = {"quota_snapshots": {"openrouter-free": SNAPSHOT}}

    def refresh(self, **kwargs):
        return api_usage.refresh(self.state, ROSTER, self.runtime, now=NOW, **kwargs)

    def test_refresh_saves_one_cache_file_and_the_glance_reads_it_without_any_source(self):
        pools = self.refresh()
        self.assertEqual(pools["openrouter-free"]["source"], "openrouter-api")
        path = self.state / "api-usage.json"
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        rows = [{"pool": "openrouter-free"}, {"pool": "claude"}]
        with patch.object(api_usage, "local_meter", side_effect=AssertionError("a render read a source")), \
                patch.object(api_usage.native_usage, "_http_get", side_effect=AssertionError("a render made a request")):
            api_usage.attach(rows, self.state, {"openrouter-free": 20})
        self.assertEqual(rows[0]["api"]["budget"], {"monthly_usd": 20, "source": "operator"})
        self.assertEqual(rows[0]["api"]["balance_usd"], 1.79)
        self.assertNotIn("api", rows[1])

    def test_openrouter_days_fall_back_to_the_local_meter_and_say_so(self):
        self.runtime["quota_snapshots"]["openrouter-free"] = {k: v for k, v in SNAPSHOT.items() if k != "per_day"}
        (self.state / "runs.jsonl").write_text(json.dumps({"ended_at": day(0), "quota_pool": "openrouter-free", "model": "m",
                                                           "cost": {"estimated_usd": 0.2}}) + "\n", encoding="utf-8")
        block = self.refresh()["openrouter-free"]
        self.assertEqual(block["days_source"], "local-meter")
        self.assertEqual(block["source"], "openrouter-api")
        self.assertEqual(block["days"][-1]["usd"], 0.2)

    def test_an_admin_key_reads_the_cost_api_once_inside_the_ttl_and_a_missing_key_never_does(self):
        body = {"data": [{"start_time": int(dt.datetime(2026, 11, 28, tzinfo=dt.timezone.utc).timestamp()),
                          "results": [{"amount": {"value": 4.0, "currency": "usd"}, "line_item": "gpt-5"}]}]}
        opener = FakeOpener(body)
        none = api_usage.refresh(self.state, ROSTER, self.runtime, now=NOW, opener=opener, key_for=lambda name: None, force=True)
        self.assertNotIn("acme-api", none)
        self.assertEqual(opener.calls, [])
        asked = []
        key = lambda name: asked.append(name) or "ADMIN"
        pools = api_usage.refresh(self.state, ROSTER, self.runtime, now=NOW, opener=opener, key_for=key, force=True)
        self.assertEqual(asked, ["openai-admin"])
        self.assertEqual(pools["acme-api"]["source"], "admin-cost-api")
        self.assertEqual(pools["acme-api"]["spend"]["today_usd"], 4.0)
        later = NOW + dt.timedelta(minutes=5)
        api_usage.refresh(self.state, ROSTER, self.runtime, now=later, opener=opener, key_for=key, force=False)
        self.assertEqual(len(opener.calls), 1)  # inside the 15 minute TTL: the cached answer serves
        after = NOW + dt.timedelta(minutes=20)
        api_usage.refresh(self.state, ROSTER, self.runtime, now=after, opener=opener, key_for=key)
        self.assertEqual(len(opener.calls), 2)

    def test_a_rejected_key_keeps_the_last_good_answer_with_a_fixed_message_and_never_the_key(self):
        good = FakeOpener({"data": [{"start_time": int(dt.datetime(2026, 11, 28, tzinfo=dt.timezone.utc).timestamp()),
                                     "results": [{"amount": {"value": 4.0, "currency": "usd"}, "line_item": "gpt-5"}]}]})
        api_usage.refresh(self.state, ROSTER, self.runtime, now=NOW, opener=good, key_for=lambda n: "ADMIN-SECRET-VALUE", force=True)
        bad = FakeOpener({}, status=401)
        pools = api_usage.refresh(self.state, ROSTER, self.runtime, now=NOW + dt.timedelta(hours=1), opener=bad,
                                  key_for=lambda n: "ADMIN-SECRET-VALUE", force=True)
        block = pools["acme-api"]
        self.assertEqual(block["spend"]["today_usd"], 4.0)
        self.assertEqual(block["error"], "The provider rejected the admin key.")
        self.assertNotIn("ADMIN-SECRET-VALUE", (self.state / "api-usage.json").read_text())

    def test_a_second_refresh_inside_half_a_minute_does_no_work(self):
        self.refresh()
        with patch.object(api_usage, "local_meter", side_effect=AssertionError("rebuilt")):
            self.assertIn("openrouter-free", self.refresh())

    def test_the_fleetctl_refresh_never_raises_and_respects_the_off_switch(self):
        with patch.dict(os.environ, {"FLEET_NO_AUTO_REFRESH": "1"}):
            fleetctl.refresh_api_usage(self.state, ROSTER)
        self.assertFalse((self.state / "api-usage.json").exists())
        with patch.dict(os.environ, {"FLEET_NO_AUTO_REFRESH": ""}), patch.object(api_usage, "refresh", side_effect=OSError("x")):
            fleetctl.refresh_api_usage(self.state, ROSTER)


class Budgets(unittest.TestCase):
    def test_a_budget_is_attached_for_display_and_a_bad_one_is_dropped(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            api_usage.refresh(state, ROSTER, {"quota_snapshots": {"openrouter-free": SNAPSHOT}}, now=NOW)
            for budget, expected in ((20, {"monthly_usd": 20, "source": "operator"}), (0, None), (-3, None), (True, None), ("20", None)):
                rows = [{"pool": "openrouter-free"}]
                api_usage.attach(rows, state, {"openrouter-free": budget})
                self.assertEqual(rows[0]["api"].get("budget"), expected)


if __name__ == "__main__":
    unittest.main()
