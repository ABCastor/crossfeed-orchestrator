# OpenRouter MiMo business rules

Scope: the new opt-in paid MiMo lane only. This ledger claims conformance for the rules listed — it is NOT a claim of repo-wide coverage.

## BR-MIMO-001 — MiMo paid calls require explicit opt-in [DECIDED · owner instruction supplied as 13 Oct 2026]

- RULE: Only `xiaomi/mimo-v2.6-pro` and `xiaomi/mimo-v2.6-flash` may use the paid route. Direct calls require `--paid-mimo`; selector and dispatch require an allow list naming `openrouter-mimo`. Every other default OpenRouter call keeps the live free-only gate. No automatic paid fallback.
- Authority: the supplied lane brief quotes the owner's approval of paid MiMo via OpenRouter and explicitly requests this isolated opt-in route. Implementation verification occurred on 12 Oct 2026; the approval date above is caller-supplied.
- implemented-by: `scripts/openrouter-agent.sh:main`, `scripts/openrouter_budget.py:priced_request`, `scripts/selector.py:enumerate_options`, `scripts/selector.py:_command`.
- tested-by: `tests/test_openrouter_mimo.py:MimoTests.test_paid_model_without_opt_in_cannot_generate`, with the observable control `test_explicit_paid_route_publishes_and_accounts_cost`; also `test_paid_opt_in_cannot_authorize_other_models` and `test_selector_paid_only_with_allow_and_dispatch_uses_opt_in`, `test_paid_lane_requires_1password_not_a_key_file`, and `test_paid_lane_cannot_claim_repository_review`.

## BR-MIMO-002 — Reserve within a shared daily cap before sending [DECIDED · supplied lane brief]

- RULE: Pro and Flash share a provisional $5/day cap, configurable by the owner. Before each paid call, reserve its worst-case cost from live pricing. Concurrent/incomplete requests count against the same cap. Replace a reservation only with valid reported cost; missing usage, interruptions and ambiguous failures retain it. No automatic paid retries. Unexpected excess billing blocks future paid admission pending owner reconciliation.
- Authority: the lane brief requires a hard daily cap enforced before each call from live prices. Reservation and no-retry accounting implement that bound; they do not grant broader paid access. Reported costs belong to their local settlement day; unresolved calls remain reserved across resets.
- implemented-by: `scripts/openrouter_budget.py:Budget`, `scripts/openrouter_budget.py:priced_request`, `scripts/openrouter-agent.sh:main`.
- tested-by: `tests/test_openrouter_mimo.py:MimoTests.test_price_jump_is_refused_before_post`, `test_pro_and_flash_share_one_cap`, `test_missing_cost_keeps_reservation_and_refuses_followup`, `test_paid_http_failure_has_no_retry_and_keeps_reservation`, `test_concurrent_reservations_cannot_overspend`, `test_bad_costs_and_corrupt_ledger_fail_closed`, `test_billing_overrun_blocks_new_calls`, `test_extra_fees_and_missing_prices_are_refused`, `test_timeout_keeps_paid_reservation`, `test_midnight_resets_settled_cost_but_keeps_unknown_reservations`.
