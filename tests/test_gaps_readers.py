"""Round 7 gap readers (CodexBar-only items): AGY per-model quota and desktop cost, Codex billing,
provider incident badges, OpenRouter per-model spend with a management key.

Every provider answer is a fixture in the shape the real endpoint returned on 26 Nov 2026 with
identifiers removed; nothing here uses a real token, database, keychain or network."""
import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_native_usage import NOW, Response, TOKEN, native

MGMT = "sk-or-v1-managementplaceholder0123"
ACCOUNT = "acct-placeholder-0123"


# ---- a tiny protobuf writer, the inverse of native._pb_fields ----------------------------------------
def varint(n):
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def field(number, value):
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    data = value.encode() if isinstance(value, str) else value
    return varint(number << 3 | 2) + varint(len(data)) + data


def timestamp(ms):
    return field(1, ms // 1000) + field(2, (ms % 1000) * 1_000_000)


def turn(model, *, inp=100, new=0, read=0, out=10, reasoning=0, response=None, label=None, step=None, ms=None, bot=None):
    usage = field(1, inp) + field(2, new) + field(5, read) + field(9, out) + field(10, reasoning)
    if response:
        usage += field(11, response)
    if bot:
        usage += field(7, bot)
    chat = field(4, usage) + (field(9, field(4, timestamp(ms))) if ms else b"") + field(19, model)
    if label:
        chat += field(21, label)
    return field(1, chat) + (field(4, step) if step else b"")


def step_meta(step, ms, bot=None):
    return field(1, timestamp(ms)) + (field(9, field(7, bot)) if bot else b"") + field(12, step)


def make_db(path, rows, steps=()):
    connection = sqlite3.connect(path)
    connection.execute("create table gen_metadata (idx integer primary key, data blob, size integer not null default 0)")
    connection.execute("create table steps (idx integer, metadata blob)")
    for idx, blob in enumerate(rows):
        connection.execute("insert into gen_metadata (idx, data, size) values (?, ?, ?)", (idx, blob, len(blob)))
    for idx, blob in enumerate(steps):
        connection.execute("insert into steps (idx, metadata) values (?, ?)", (idx, blob))
    connection.commit()
    connection.close()


PRICES = {"gemini-4-neon": {"cost": {"input": 2.0, "output": 12.0, "cache_read": 0.2}},
          "gemini-4-hydrogen": {"cost": {"input": 1.0, "output": 4.0}}}


class AntigravityCost(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = dt.datetime.now(dt.timezone.utc)
        self.ms = int(self.now.timestamp() * 1000) - 60_000

    def cost(self, **kwargs):
        return native.local_antigravity_cost(PRICES, [self.root], self.now, **kwargs)

    def test_prices_tokens_dedupes_copies_and_falls_back_to_the_base_model_price(self):
        rows = [
            turn("gemini-4-neon-tiered", inp=1000, new=500, read=2000, out=100, reasoning=50, response="r1", ms=self.ms),
            turn("gemini-4-neon-tiered", inp=1000, new=500, read=2000, out=100, reasoning=50, response="r1", ms=self.ms),
            turn("gemini-4-hydrogen", inp=2_000_000, out=0, ms=self.ms - 86_400_000 * 3),
        ]
        make_db(self.root / "c1.db", rows)
        (self.root / "copy").mkdir()
        make_db(self.root / "copy" / "c1.db", rows[:1])  # the same conversation, copied into a second root
        result = native.local_antigravity_cost(PRICES, [self.root, self.root / "copy"], self.now)
        # the first row only: input 1500 at $2, reasoning counts as output, cache read at $0.2
        expected = (1500 * 2.0 + 150 * 12.0 + 2000 * 0.2) / 1_000_000
        self.assertEqual(result["today_tokens"], 1500 + 100 + 2000 + 50)
        self.assertAlmostEqual(result["today_usd"], expected, places=8)
        self.assertEqual(result["last_30d_requests"], 2)
        self.assertAlmostEqual(result["last_30d_usd"], expected + 2.0, places=8)
        self.assertEqual([row["model"] for row in result["by_model"]], ["gemini-4-hydrogen", "gemini-4-neon-tiered"])
        self.assertFalse(result["partial"])
        self.assertEqual(result["source"], "antigravity-desktop-history")

    def test_breakdown_adds_seven_days_a_daily_series_and_per_model_windows(self):
        make_db(self.root / "c.db", [
            turn("gemini-4-hydrogen", inp=1_000_000, out=0, response="a", ms=self.ms),
            turn("gemini-4-hydrogen", inp=1_000_000, out=0, response="b", ms=self.ms - 86_400_000 * 3),
            turn("gemini-4-hydrogen", inp=1_000_000, out=0, response="c", ms=self.ms - 86_400_000 * 20),
            turn("gemini-9-unknown", inp=500, out=5, response="d", ms=self.ms),
        ])
        result = self.cost()
        self.assertAlmostEqual(result["last_7d_usd"], 2.0, places=6)  # priced rows only, like the 30-day total
        self.assertAlmostEqual(result["last_30d_usd"], 3.0, places=6)
        self.assertEqual(len(result["daily"]), 30)
        self.assertAlmostEqual(sum(day["usd"] for day in result["daily"]), 3.0, places=6)
        hydrogen = next(row for row in result["by_model"] if row["model"] == "gemini-4-hydrogen")
        self.assertAlmostEqual(hydrogen["usd_7d"], 2.0, places=6)
        self.assertAlmostEqual(hydrogen["usd_today"], 1.0, places=6)
        self.assertAlmostEqual(sum(row[2] for row in result["hourly"]), 2.0, places=6)

    def test_unpriced_model_keeps_tokens_and_is_counted_not_zeroed(self):
        make_db(self.root / "c.db", [turn("gemini-9-unknown", inp=500, out=5, ms=self.ms)])
        result = self.cost()
        self.assertEqual(result["last_30d_tokens"], 505)
        self.assertIsNone(result["last_30d_usd"])
        self.assertEqual(result["last_30d_unpriced_requests"], 1)

    def test_rows_without_a_time_are_dated_from_their_step_or_left_out_and_flagged(self):
        step = "11111111-2222-3333-4444-555555555555"
        make_db(self.root / "c.db",
                [turn("gemini-4-neon", inp=1000, out=0, step=step, bot="bot-a"),
                 turn("gemini-4-neon", inp=7, out=0, step="no-such-step")],
                [step_meta(step, self.ms, "bot-a")])
        result = self.cost()
        self.assertEqual(result["last_30d_requests"], 1)
        self.assertEqual(result["last_30d_tokens"], 1000)
        self.assertTrue(result["partial"], "an undated row means the total is a lower bound")

    def test_model_is_inherited_from_a_unique_label_and_old_rows_are_outside_the_window(self):
        make_db(self.root / "c.db", [
            turn("gemini-4-hydrogen", inp=1_000_000, out=0, label="L", response="a", ms=self.ms),
            turn("", inp=1_000_000, out=0, label="L", response="b", ms=self.ms),
            turn("gemini-4-hydrogen", inp=9_000_000, out=0, ms=self.ms - 86_400_000 * 45),
        ])
        result = self.cost()
        self.assertEqual(result["last_30d_requests"], 2)
        self.assertAlmostEqual(result["last_30d_usd"], 2.0, places=6)

    def test_no_databases_is_none_and_garbage_is_skipped(self):
        self.assertIsNone(self.cost())
        (self.root / "bad.db").write_bytes(b"not a database")
        result = self.cost()
        self.assertEqual(result["last_30d_requests"], 0)
        self.assertTrue(result["partial"], "an unreadable database means the total is a lower bound")

    def test_parser_rejects_malformed_protobuf(self):
        self.assertIsNone(native.parse_antigravity_turn(b"\x0a\xff\xff\xff\xff\x0f"))
        self.assertIsNone(native.parse_antigravity_turn(field(4, "step-only")))


class AntigravityModelQuota(unittest.TestCase):
    BUCKETS = {"buckets": [
        {"modelId": "gemini-4-argon-high", "remainingFraction": 0.72, "resetTime": "2026-11-26T14:13:49Z", "tokenType": "WTUS"},
        {"modelId": "gemini-4-argon-low", "remainingFraction": 0.72, "resetTime": "2026-11-26T14:13:49Z"},
        {"modelId": "gemini-4-neon-high", "remainingFraction": 0.72, "resetTime": "2026-11-26T14:13:49Z"},
        {"modelId": "claude-opus-5-5-low", "remainingFraction": 0.5, "resetTime": "2026-11-30T20:39:38Z"},
        {"modelId": "gemini-4-argon-medium"},  # protobuf zero is not guessed
    ]}
    SUMMARY = {"antigravity-quota-summary-gemini-5h": {"used_percent": 28, "reset_at": "2026-11-26T14:13:49Z",
                                                        "label": "Gemini 5-hour"}}

    def test_groups_models_by_pool_and_names_the_summary_window_they_share(self):
        rows = native.parse_antigravity_model_quota(self.BUCKETS, ["argon"], None, self.SUMMARY)
        self.assertEqual(rows, [{"label": "Argon models", "models": ["gemini-4-argon-high", "gemini-4-argon-low"],
                                 "used_percent": 28.0, "reset_at": "2026-11-26T14:13:49Z",
                                 "shared_with": "Gemini 5-hour"}])

    def test_nothing_is_returned_unless_a_family_is_requested(self):
        self.assertEqual(native.parse_antigravity_model_quota(self.BUCKETS, [], None), [])
        self.assertIsNone(native.parse_antigravity_model_quota({"other": 1}, ["argon"]))

    def test_fetch_available_models_shape_is_read_the_same_way(self):
        payload = {"models": {"gemini-4-argon-high": {"quotaInfo": {"remainingFraction": 0.9, "resetTime": "2026-12-03T09:21:28Z"}},
                              "chat_1": {"quotaInfo": {"remainingFraction": 1}}}}
        rows = native.parse_antigravity_model_quota(payload, ["argon"])
        self.assertEqual((rows[0]["used_percent"], rows[0]["reset_at"]), (10.0, "2026-12-03T09:21:28Z"))
        self.assertNotIn("shared_with", rows[0])


class CodexBilling(unittest.TestCase):
    SUB = {"id": "x", "plan_type": "pro", "active_until": "2026-12-04T10:58:38Z", "will_renew": True,
           "billing_period": "monthly", "billing_currency": "EUR",
           "entitlement": {"subscription_plan": "chatgptpro", "expires_at": "2026-12-04T16:58:38+00:00",
                           "renews_at": "2026-12-04T10:58:38+00:00", "cancels_at": None}}
    BAL = {"balance": "12048.6", "expiring_balance_details": [
        {"amount_granted": "62500", "amount_remaining": "12048.5", "expiry_date": "2027-01-01T00:00:00Z",
         "grant_type": "promotional_credit"}]}
    EVENTS = {"data": [{"date": "2026-11-26", "product_surface": "exec", "credit_amount": 100.5, "usage_id": "secret-event-id"},
                       {"date": "2026-11-26", "product_surface": "desktop_app", "credit_amount": 20, "usage_id": "secret-event-id-2"},
                       {"date": "2026-11-20", "product_surface": "exec", "credit_amount": 5, "usage_id": "e3"},
                       {"date": "2026-09-01", "product_surface": "exec", "credit_amount": 999, "usage_id": "old"}]}
    BREAKDOWN = {"units": "percent", "group_by": "day", "data": [
        {"date": "2026-11-25", "attribution": [{"model": "gpt-6-astra", "value": 2.5, "surface": "cli"},
                                               {"model": "gpt-6.2-sol", "value": 1.5, "surface": "cli"}], "models": []},
        {"date": "2026-11-26", "attribution": [{"model": "gpt-6-astra", "value": 4.0}], "models": []}]}

    def test_parse_combines_the_four_answers_without_identifiers(self):
        billing = native.parse_codex_billing(self.SUB, self.BAL, self.BREAKDOWN, self.EVENTS, dt.date(2026, 11, 26))
        self.assertEqual((billing["plan"], billing["period"], billing["currency"]), ("pro", "monthly", "EUR"))
        self.assertEqual((billing["active_until"], billing["will_renew"], billing["renews_at"]),
                         ("2026-12-04T10:58:38Z", True, "2026-12-04T10:58:38Z"))
        self.assertEqual(billing["grants"], [{"credits": 12048.5, "expires_at": "2027-01-01T00:00:00Z",
                                              "granted": 62500.0, "kind": "promotional_credit"}])
        self.assertEqual((billing["credits_today"], billing["credits_7d"], billing["credits_30d"]), (120.5, 125.5, 125.5))
        self.assertEqual(len(billing["credit_history"]), 30)
        self.assertEqual(billing["credit_history"][-1], {"date": "2026-11-26", "credits": 120.5})
        self.assertEqual(billing["limit_use_history"], [{"date": "2026-11-25", "percent": 4.0}, {"date": "2026-11-26", "percent": 4.0}])
        self.assertEqual(billing["limit_use_by_model_30d"][0], {"model": "gpt-6-astra", "percent": 6.5})
        self.assertNotIn("secret-event-id", json.dumps(billing))

    def test_a_cancelled_subscription_has_no_renewal_date(self):
        sub = {**self.SUB, "will_renew": False, "entitlement": {**self.SUB["entitlement"], "cancels_at": "2026-12-04T10:58:38Z"}}
        billing = native.parse_codex_billing(sub, None, None, None, dt.date(2026, 11, 26))
        self.assertNotIn("renews_at", billing)
        self.assertEqual((billing["will_renew"], billing["cancels_at"]), (False, "2026-12-04T10:58:38Z"))
        self.assertIsNone(native.parse_codex_billing(None, None, None, None, dt.date(2026, 11, 26)))

    def observe(self, fail=()):
        seen = []

        def opener(request, timeout):
            url = request.full_url
            seen.append(url)
            self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
            self.assertNotIn("Cookie", request.headers)
            if any(name in url for name in fail):
                return Response("{}", 500)
            if "wham/usage/daily" in url:
                return Response(json.dumps(self.BREAKDOWN))
            if "credit-usage-events" in url:
                return Response(json.dumps(self.EVENTS))
            if "remaining_balance" in url:
                return Response(json.dumps(self.BAL))
            if "subscriptions" in url:
                return Response(json.dumps(self.SUB))
            if url == native.CODEX_USAGE_URL:
                return Response(json.dumps({"plan_type": "pro", "rate_limit": {"primary_window": {
                    "used_percent": 10, "reset_at": int(NOW.timestamp()) + 3600, "limit_window_seconds": 604800}}}))
            raise AssertionError(url)

        auth = json.dumps({"tokens": {"access_token": TOKEN, "account_id": ACCOUNT}}).encode()
        with patch.object(native, "_owner_file", return_value=auth):
            return native.observe_codex_oauth({}, opener=opener), seen

    def test_observe_uses_the_cli_bearer_and_account_query_only(self):
        snapshot, seen = self.observe()
        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["billing"]["plan"], "pro")
        self.assertEqual(snapshot["billing"]["source"], native.CODEX_BILLING_SOURCE)
        self.assertNotIn("missing", snapshot["billing"])
        self.assertIn(f"{native.CODEX_BACKEND}subscriptions?account_id={ACCOUNT}", seen)
        self.assertNotIn(ACCOUNT, json.dumps(snapshot))

    def test_one_failed_endpoint_is_named_and_the_quota_reading_survives(self):
        snapshot, _ = self.observe(fail=("remaining_balance",))
        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["billing"]["missing"], ["balance"])
        self.assertNotIn("grants", snapshot["billing"])
        self.assertIn("windows", snapshot)

    def test_billing_can_be_switched_off(self):
        auth = json.dumps({"tokens": {"access_token": TOKEN, "account_id": ACCOUNT}}).encode()
        urls = []

        def opener(request, timeout):
            urls.append(request.full_url)
            return Response(json.dumps({"plan_type": "pro", "rate_limit": {"primary_window": {
                "used_percent": 10, "reset_at": int(NOW.timestamp()) + 3600, "limit_window_seconds": 604800}}}))

        with patch.object(native, "_owner_file", return_value=auth):
            snapshot = native.observe_codex_oauth({"billing": False}, opener=opener)
        self.assertEqual(urls, [native.CODEX_USAGE_URL])
        self.assertNotIn("billing", snapshot)


class IncidentBadges(unittest.TestCase):
    def test_statuspage_uses_the_page_indicator_and_names_the_open_incident(self):
        payload = {"page": {"updated_at": "2026-11-26T09:10:07Z"}, "status": {"indicator": "minor", "description": "Partially Degraded"},
                   "components": [{"name": "claude.ai", "status": "degraded_performance"}, {"name": "API", "status": "operational"}],
                   "incidents": [{"name": "Add-in missing", "impact": "major", "status": "identified", "created_at": "2026-11-24T00:10:40Z"},
                                 {"name": "Old", "impact": "critical", "status": "resolved"}]}
        record = native.parse_statuspage_summary(payload)
        self.assertEqual((record["level"], record["text"], record["affected"]), ("minor", "Add-in missing", ["claude.ai"]))
        clear = native.parse_statuspage_summary({"status": {"indicator": "none"}, "components": [], "incidents": []})
        self.assertEqual(clear["level"], "none")
        self.assertIsNone(native.parse_statuspage_summary({"nope": 1}))

    def test_datadog_opencode_and_google_feeds(self):
        datadog = {"components": [{"name": "API", "type": "ComponentGroup", "components": [
            {"name": "Chat", "status": "partial_outage"}, {"name": "Image", "status": "operational"}]}],
            "incidents": [{"title": "Provider errors", "resolved": False, "publishedDate": "2026-11-25T14:52:00Z"},
                          {"title": "Done", "resolved": True}]}
        record = native.parse_datadog_status(datadog)
        self.assertEqual((record["level"], record["text"], record["affected"]), ("major", "Provider errors", ["Chat"]))
        self.assertEqual(native.parse_datadog_status({"components": [{"name": "A", "status": "operational"}], "incidents": [
            {"title": "Done", "resolved": True}]})["level"], "none")
        opencode = {"status": "degraded", "components": [{"service": "console", "status": "degraded", "reason": "errors", "since": 1795619312497},
                                                           {"service": "go", "status": "operational", "reason": "none"}]}
        record = native.parse_opencode_status(opencode)
        self.assertEqual((record["level"], record["text"], record["affected"]), ("minor", "Errors", ["console"]))
        incidents = [{"end": "2026-11-01T00:00:00Z", "status_impact": "SERVICE_OUTAGE",
                      "currently_affected_products": [{"id": native.GOOGLE_GEMINI_PRODUCT}]},
                     {"status_impact": "SERVICE_DISRUPTION", "external_desc": "**Summary**\nGemini API errors",
                      "begin": "2026-11-26T08:00:00Z", "currently_affected_products": [{"id": native.GOOGLE_GEMINI_PRODUCT}]},
                     {"status_impact": "SERVICE_OUTAGE", "currently_affected_products": [{"id": "other-product"}]}]
        record = native.parse_google_status(incidents)
        self.assertEqual((record["level"], record["text"]), ("major", "Gemini API errors"))
        self.assertEqual(native.parse_google_status([])["level"], "none")

    def test_fetch_keeps_only_feeds_that_answered_and_sends_no_credentials(self):
        seen = []

        def opener(request, timeout):
            seen.append(dict(request.header_items()))
            if "openai" in request.full_url:
                return Response('{"status": {"indicator": "none"}, "components": [], "incidents": []}')
            return Response("{}", 500)

        records = native.fetch_provider_status(opener, NOW)
        self.assertEqual(list(records), ["openai"])
        self.assertEqual(records["openai"]["checked_at"], native._iso(NOW))
        self.assertTrue(all("Authorization" not in headers and "Cookie" not in headers for headers in seen))

    def test_pool_projection_reads_the_cache_without_network_when_not_refreshing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            cache = state / "provider-status.json"
            record = {"level": "minor", "text": "Elevated errors", "since": None, "affected": [], "feed": "statuspage",
                      "checked_at": native._iso(NOW)}
            native._write_owner_json(cache, {"openai": record})
            cache.chmod(0o600)
            pool = native.provider_incident("codex", "codex-oauth", state, NOW, refresh=False)
            self.assertEqual((pool["level"], pool["age_s"], pool["stale"]), ("minor", 0, False))
            self.assertIsNone(native.provider_incident("claude", "claude", state, NOW, refresh=False))
            self.assertIsNone(native.provider_incident("chatgpt-lane", None, state, NOW, refresh=False))


class OpenRouterManagementKey(unittest.TestCase):
    def run_observer(self, management, activity_status=200):
        calls = {}

        def opener(request, timeout):
            calls[request.full_url.rsplit("/", 1)[-1]] = request.get_header("Authorization")
            if request.full_url.endswith("/activity"):
                day = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=2)).isoformat()
                body = {"data": [{"date": day, "model": "deepseek/deepseek-v4", "usage": 0.5, "requests": 3},
                                 {"date": day, "model": "openrouter/free", "usage": 0, "requests": 9}]}
                return Response(json.dumps(body), activity_status)
            return Response('{"data":{"total_credits":5,"total_usage":1,"usage_daily":0}}')

        with patch.object(native, "_owner_file", return_value=TOKEN.encode()), \
                patch.object(native, "_management_key", return_value=management):
            return native.observe_openrouter({"activity": True}, opener=opener), calls

    def test_activity_uses_the_management_key_and_cash_keeps_the_inference_key(self):
        snapshot, calls = self.run_observer(MGMT)
        self.assertEqual(calls["activity"], f"Bearer {MGMT}")
        self.assertEqual(calls["credits"], f"Bearer {TOKEN}")
        self.assertEqual([(row["model"], row["usd_30d"], row["requests"]) for row in snapshot["per_model"]],
                         [("deepseek/deepseek-v4", 0.5, 3), ("openrouter/free", 0, 9)])
        self.assertNotIn(MGMT, json.dumps(snapshot))
        self.assertNotIn("per_model_error", snapshot)

    def test_without_a_management_key_the_error_says_how_to_add_one(self):
        def opener(request, timeout):
            return Response("{}", 403) if request.full_url.endswith("/activity") else \
                Response('{"data":{"total_credits":5,"total_usage":1}}')
        with patch.object(native, "_owner_file", return_value=TOKEN.encode()), \
                patch.object(native, "_management_key", return_value=None):
            snapshot = native.observe_openrouter({"activity": True}, opener=opener)
        self.assertIn("fleetctl secret set openrouter-management", snapshot["per_model_error"])
        self.assertTrue(snapshot["available"])

    def test_a_rejected_management_key_is_reported_as_rejected_never_as_zero(self):
        snapshot, _ = self.run_observer(MGMT, activity_status=403)
        self.assertIn("rejected", snapshot["per_model_error"])
        self.assertNotIn("per_model", snapshot)
        self.assertNotIn(MGMT, json.dumps(snapshot))


if __name__ == "__main__":
    unittest.main()
