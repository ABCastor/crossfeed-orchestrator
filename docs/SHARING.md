# Sharing a subscription: who used what, who owes what

Some plans are paid by more than one person. The console's **Share** page (`/share`) writes one card per person. A card holds what they owe and what this Mac measured, copyable as text, savable as a PNG, printable, and readable from the tailnet phone view (`…/crossfeed/share`, same Tailscale login, read only).

## What it is built on

| Need | Decision | Why |
|---|---|---|
| Tell each person what they owe | The agreed share in the private ledger (`subscriptions.json`: price, period, your share, the other payer's label) | A flat plan has no per-use price. An agreed split is the one number everyone can check. Use never changes it. |
| Tell each person how much was used | Show only what this Mac measured, next to what the provider reports for the whole account. The difference is **not measured here** | A shared login carries no name. Splitting the provider's total by guess would put numbers in a person's mouth. |
| Say it once, in the card | "Estimates at API list prices from usage logs on one Mac, not what anyone pays. A shared login does not say who used it, so use is never split by person." | The card is forwarded without the console around it. |

## What a card contains

For each plan the person shares: the share in fewest words (`½ of €200.00 a month · yours €100.00`), and after the plans one USD line for everything (`≈ $428.75 a month in all, $214.38 mine · rates 2026-11-27`), converted at ECB reference rates fetched at most once a day and kept in local state; the line is absent when no rate is known. A plan whose split the access overlay states arrives confirmed; a name-based "shared with" stays marked as an assumption. Then: the account's use of its weekly allowance and what that is at API list prices (only when the plan has a weekly dollar value in the roster, `value_capacity`); what this Mac measured since the weekly reset (hourly buckets of the local usage logs); the remainder, labelled "about" or "at most". The remainder is other people's use **and also your own use on the web, a phone or another computer**. A person's card says so in those words.

## What cannot be known

- **Who used a shared login.** The provider reports one total and no name.
- **Use outside this Mac**: web chats, phones, other computers, another person's own tools.
- **What anyone pays.** Dollars are list prices (plans are flat).
- Windows are compared at the hour: the weekly reset to now, against the log's hourly buckets. A plan with several weekly allowances (Google AI Pro) has no single account figure, so its card uses the last 7 days and states no account total.
- A model with no list price makes the measured figure a floor ("at least") and the remainder a ceiling ("at most"); the figure is never shown as zero.
- A provider whose use is not logged on this Mac (OpenCode Go, Copilot) shows no measured figure.

## A second person's Crossfeed (design only, not built)

If the other person also runs Crossfeed, their Mac measures their use of the same account. The smallest honest exchange is a **usage statement** they export and you import:

```json
{"schema": 1, "subscription": "claude", "from": "2026-11-23T10:00:00Z", "to": "2026-11-26T12:00:00Z",
 "measured_usd_list": 6.40, "tokens": 5100000, "by_model": [{"model": "claude-opus-5-5", "usd": 5.1}], "complete": true}
```

No prompts, paths or account identifiers; just totals per period. Importing one adds a second measured line to the card ("Sam's Mac measured $6.40") and shrinks "not measured" accordingly. Still not known after that: anything either Mac did not see. Open points before building: how the statement is passed (file, message, link), whether to sign it (the importer can only trust a person, not the file), and one period convention (the provider's weekly reset) so the two statements line up.

## Where the code is

`scripts/sharing.py` (the model and the card text; pure functions), `scripts/usage_page.py` (the pages), `scripts/phone_view.py` (phone cards), `tests/test_sharing.py`, `tests/test_usage_page.py`, `tests/test_phone_view.py`.
