"""BR-QUOTA-003: the operator picks which Claude limit reset to use, and the claim is sent once.

The one live test is the operator's own first click; here every request is a fake opener, and the
checks are the request itself (no POST without --yes, the exact URL, headers and body) plus a
stdout that never carries a grant id, a request id or the organization.
"""
import contextlib
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from tests.test_fleetctl import fleetctl
from tests.test_native_usage import CEDAR_GRANTS, Response, answering, fixture

native = fleetctl.native_usage
NOW = fleetctl.utc_now()
ORG = "org-uuid-placeholder"
GRANT_ID = "private-grant-id"
REQUEST_ID = "fixed-request-id"


class FakeAnthropic:
    """A usage answer that keeps answering, and the one claim POST it saw."""

    def __init__(self, grant_body=None, claim_status=200, claim_text=""):
        self.usage = json.dumps({**json.loads(fixture("claude-usage.json")), **json.loads(json.dumps(CEDAR_GRANTS))})
        self.grant_body = grant_body if grant_body is not None else self.usage
        self.claim_status, self.claim_text = claim_status, claim_text
        self.usage_reads, self.claims = 0, []
        self.answer = None

    def __call__(self, request, timeout=5):
        body = request.data.decode() if getattr(request, "data", None) else None
        if request.get_method() == "POST":
            self.claims.append(request)
            if self.claim_status >= 400:
                raise urllib.error.HTTPError(request.full_url, self.claim_status, "error", {},
                                             io.BytesIO(self.claim_text.encode()))
            return Response(self.claim_text, self.claim_status)
        self.usage_reads += 1
        # After the claim, the used grant's resets_left drops, as a real answer would.
        if self.claims:
            used = json.loads(self.usage)
            used["cedar_ember"]["grants"][0]["resets_left"] = 0
            return Response(json.dumps(used))
        return Response(self.answer if self.answer is not None else self.usage)


class UseResetTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.config = self.root / ".claude.json"
        self.config.write_text(json.dumps({"oauthAccount": {"organizationUuid": ORG}}))
        patches = (patch.object(native, "claude_token", return_value=("placeholder-token", "Claude Max", None)),
                   patch.object(native, "claude_organization_uuid", return_value=ORG),
                   patch.object(native, "claude_reset_request_id", return_value=REQUEST_ID),
                   patch.object(native, "claude_cli_version", return_value="2.1.332"))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_command(self, fake, *extra, key="weekly"):
        # A missing overlay keeps the run off the machine's own roster; the save-after-claim test patches it in.
        argv = ["fleetctl.py", "--state-dir", str(self.root), "--overlay", str(self.root / "no-overlay.json"),
                "use-reset", "claude", "--key", key, *extra]
        out, err = io.StringIO(), io.StringIO()
        with patch.object(native.urllib.request, "build_opener") as build:
            build.return_value.open = fake
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                    patch.object(fleetctl.sys, "argv", argv):
                code = fleetctl.main()
        return code, out.getvalue(), err.getvalue()

    # BR-QUOTA-003
    def test_without_yes_it_says_which_reset_and_sends_nothing(self):
        fake = FakeAnthropic()
        code, out, err = self.run_command(fake)
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.claims, [])
        self.assertEqual(fake.usage_reads, 1)
        self.assertIn("Would use: One full limit reset (November 2026). Left: 1", out)
        self.assertIn("--yes", out)

    # BR-QUOTA-003
    def test_yes_reads_again_then_sends_exactly_one_claim(self):
        fake = FakeAnthropic()
        code, out, err = self.run_command(fake, "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(fake.claims), 1)
        self.assertEqual(fake.usage_reads, 3)          # which one, the fresh answer, then the re-read
        request = fake.claims[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url,
                         f"https://api.anthropic.com/api/organizations/{ORG}/reset_rate_limits")
        headers = {name.casefold(): value for name, value in request.header_items()}
        self.assertEqual(headers["authorization"], "Bearer placeholder-token")
        self.assertEqual(headers["anthropic-beta"], "oauth-2025-04-20")
        self.assertEqual(headers["user-agent"], "claude-cli/2.1.332 (external, cli)")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(json.loads(request.data),
                         {"program": "cedar_ember", "grant_id": GRANT_ID, "request_id": REQUEST_ID})
        self.assertEqual(out.strip(), "Used: One full limit reset (November 2026). Left: 0")

    # BR-QUOTA-004
    def test_a_sent_claim_saves_a_fresh_reading_of_every_claude_pool_at_once(self):
        """The menu showed 100% after a used reset: the claim's re-read was printed, never saved."""
        sources = {"claude": {"oracle": "claude"}, "claude-fable": {"oracle": "claude"}, "codex": {"oracle": "codex"}}
        with patch.object(fleetctl, "read_overlay", return_value={"lanes": []}), \
                patch.object(fleetctl, "quota_sources", return_value=sources), \
                patch.object(fleetctl, "refresh_stale_pools", return_value={}) as reread:
            code, _out, err = self.run_command(FakeAnthropic(), "--yes")
            self.assertEqual(code, 0, err)
            reread.assert_called_once()
            self.assertEqual(sorted(reread.call_args.args[1]), ["claude", "claude-fable"])
            self.assertIs(reread.call_args.kwargs["force"], True)
            reread.reset_mock()
            self.run_command(FakeAnthropic())                       # no --yes: nothing sent, nothing re-read
            self.run_command(FakeAnthropic(claim_status=409), "--yes")  # refused: the limits did not change
            reread.assert_not_called()

    # BR-QUOTA-003
    def test_the_printed_outcome_names_no_grant_request_or_organization(self):
        fake = FakeAnthropic()
        code, out, err = self.run_command(fake, "--yes", "--json")
        said = out + err
        self.assertEqual(code, 0, err)
        for secret in (GRANT_ID, REQUEST_ID, ORG, "private-launch-id"):
            self.assertNotIn(secret, said)
        body = json.loads(out)
        self.assertEqual((body["ok"], body["label"], body["left"], body["sent"]),
                         (True, "One full limit reset (November 2026)", 0, True))

    # BR-QUOTA-003
    def test_a_refused_or_failed_claim_is_one_plain_failure(self):
        for status in (401, 404):
            with self.subTest(status=status):
                fake = FakeAnthropic(claim_status=status, claim_text='{"error":"no"}')
                code, out, err = self.run_command(fake, "--yes")
                self.assertEqual(code, 1)
                self.assertEqual(len(fake.claims), 1)     # one attempt, no retry
                self.assertIn(f"Anthropic did not accept the reset (HTTP {status})", err)
                self.assertEqual(fake.usage_reads, 2)     # which one, the fresh answer: never a re-read
                self.assertNotIn(ORG, out + err)

    def test_an_unreadable_answer_or_an_unknown_key_sends_nothing(self):
        broken = FakeAnthropic()
        broken.answer = "<html>sign in</html>"
        code, out, err = self.run_command(broken, "--yes")
        self.assertEqual((code, broken.claims), (1, []))
        self.assertIn("unreadable", err)
        unknown = FakeAnthropic()
        code, out, err = self.run_command(unknown, "--yes", key="not-a-window")
        self.assertEqual((code, unknown.claims), (1, []))
        self.assertIn("no limit reset with the key not-a-window", err)

    def test_a_claim_that_leaves_the_grant_untouched_is_reported_plainly(self):
        fake = FakeAnthropic()
        original = fake.__call__

        def unchanged(request, timeout=5):
            if getattr(request, "data", None):     # the claim itself
                fake.claims.append(request)
                return Response("", 200)
            fake.usage_reads += 1
            return Response(fake.usage)            # resets_left never drops

        code, out, err = self.run_command(unchanged, "--yes")
        self.assertEqual((code, len(fake.claims)), (1, 1))
        self.assertIn("still shows 1 left", err)


if __name__ == "__main__":
    unittest.main()
