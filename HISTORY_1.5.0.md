# Memecoin Risk Engine — change summary for review

Covers everything since the external source review of 7 September 2026.
Versions: **analyzer 1.2.0**, **paperbot 1.5.0**. **105 tests, all passing**
(`python -m unittest discover`). 6,257 lines across 24 files. Standard library
only, except the live-execution scaffold (`solders`) and an optional `certifi`
dependency in the Helius client.

---

## 1. The external review was adopted in full

Every fix in the reviewer's patch was taken unchanged:

| Area | Change |
| --- | --- |
| Price parsing | Response shapes validated; non-finite, non-positive and malformed quotes rejected. A failed request is a distinct value from "no listing". |
| Rug labelling | Missing listings no longer produce a rug label or a zero-price liquidation. Positions stay unresolved. |
| Portfolio valuation | Equity and unrealised P&L report as unknown when positions cannot be priced, instead of silently valuing at cost. |
| Checkpoints | Actual elapsed time is used; observations outside a 600-second grace window are recorded as missed, with lateness stored. |
| Research joins | Assessment IDs added; outcomes join to the exact assessment rather than the first row with the same address. Ambiguous legacy matches are excluded. |
| Control groups | Unknown checks no longer count as negatives. Repeated flags aggregate. Both sides must meet a sample threshold. |
| Security data | Empty RugCheck/Honeypot envelopes are no longer read as clean reports. |
| Address identity | Solana mints stay case-sensitive; only Base addresses are lowercased. |
| Dashboard server | Bound to loopback, serves exactly three files, rejects source/state/traversal for GET and HEAD. |
| Capacity eviction | Unfinished horizons are logged as `capacity_evicted` before removal. |
| Report filtering | Timestamps normalised to UTC; `--since` requires discovery *and* observation after the cutoff; versions included in drift detection. |
| Log reading | A partially written final JSONL record is skipped with a warning; complete corruption still fails. |

### One correction to the reviewer's patch

The patch treated Dexscreener's `{"pairs": null}` as a transport error. Live
observation shows that is the shape returned for a token it does not index —
a complete answer meaning "no listing". Treating it as a failure would have
made unknown tokens permanently unresolvable. Two of the reviewer's tests were
adjusted, with the reason recorded in both the code and the tests.

---

## 2. Added on top, from the review's own "remaining work" list

**Valuation policy for unquotable positions.** The patch stopped the bot
closing positions it could not price, which is correct about labels but leaves
dead positions open forever, never realising their loss and eventually filling
every slot. A position with no indexed listing for `--unresolved-close-hours`
(default 24) now closes at zero under the reason *"unresolved: no indexed
listing for 24h, valued at zero (not a verified rug)"*. It is reported as its
own category, never mixed with rugs. `0` disables it.

**Bought coins get fixed-horizon observations too.** Previously only skipped
candidates were tracked at 1/6/24h, so bought and skipped cohorts were not
comparable. Both are now tracked; the verdict field says which cohort a row
belongs to. `--no-track-bought-outcomes` disables.

**Single-writer lock on the state file.** An OS-level lock on
`<state>.lock` held for the process lifetime. A second bot against the same
state exits with an explanation instead of silently interleaving writes. This
closes the duplicate-process problem hit twice in production.

**Per-host request pacing and retry.** Every API has a minimum spacing derived
from its published limit (Dexscreener ~300/min, GeckoTerminal 30/min); 429s and
5xx are retried with backoff rather than being swallowed as "no data".

**Failed requests inside the grace window are retried, not censored.** A single
transient failure no longer burns a checkpoint horizon. Attempts are counted on
the row eventually written.

**Observation-status table in the analysis.** Per score bucket: priced, no
listing, request failed, missed. Missingness is visible *before* any return
statistic is read, so a bucket whose numbers rest on an unrepresentative subset
is obvious.

---

## 3. A separate defect found during adoption: corrupted cash

Independent of the review, the simulated cash balance was found to be
**$1,724,792,700.07 on a 100-unit pot**.

**Cause.** On 23 August a fabricated Dexscreener quote closed TOSHI at
79,806.68 against an entry of 0.0001324 — a 602,769,486x move. Its "proceeds"
were credited to cash. `analyze_outcomes.py` had always excluded that trade
from statistics, so published reports stayed honest, but nothing corrected the
state. The 100x price guard added later prevented recurrence without repairing
the damage.

**Consequence, which is worse than the number.** For sixteen days the
affordability check never refused a purchase. The bot committed roughly 1,200
units against a 100-unit pot and held 60 concurrent positions where the pot
supported about 33. Rebuilding the balance honestly yields **−1,106.37**, which
is the arithmetic proof the constraint was never enforced.

**Resolution.** `repair_cash.py` rebuilds cash from first principles
(`pot − open positions + realised P&L of non-artefact trades`) rather than
patching the figure, and voids impossible trades into an audit list — which is
deliberately not the same as booking a loss, since the exit price was invented
and the true outcome is unknown. Because the constraint had been broken, the
history could not be made valid, so `--reset-trading` archived 60 positions and
491 closed trades to `paperbot_trading_archive-*.json` with the reason recorded
and restarted trading from a full pot. Research data (3,000 pending
observations, seen addresses, both JSONL logs) was preserved untouched.

`paperbot.py` now warns at startup if cash exceeds 50x the pot.

**All trading performance figures published before 7 September are withdrawn.**
Per-trade entries and exits used real prices, but the portfolio they form is one
no 100-unit pot could hold, and a properly funded bot would have declined most
of those purchases. Valid figures require roughly a week of post-reset data.
Nothing in the score-bucket, per-rule, confidence or winner-profile analysis is
affected — those read price observations that never touched the cash balance.

---

## 4. Copy-trading research track

Two scripts, neither wired into the bot.

`find_wallets.py` derives a wallet watchlist from our own data rather than a
public leaderboard: it takes coins that gained ≥50% at their 24h checkpoint,
asks Helius who bought them early, and ranks wallets by a **lift** score —
their hit rate across winners divided by their hit rate across a matched
control group. A wallet that buys everything scores 1.0 and is excluded. This
avoids scraping (a terms-of-service problem), needs no new API key, and is not
gameable by wallets farmed to attract copiers.

`wallet_research.py` then answers the make-or-break question: reconstructs each
wallet's FIFO profit and loss in SOL from Helius, and measures **week-to-week
rank correlation and top-half retention**. Retention near 50% means last week's
winners are no likelier than chance to win next week, there is nothing to copy,
and the watcher should not be built.

### Three live failures found and fixed

1. **HTTP 403.** A bare `urlopen()` sends `Python-urllib/3.x`, which Helius's
   edge refuses. The identical URL and key succeed from PowerShell. Confirmed
   experimentally: bare request → 403, same request with a browser-ish
   User-Agent → 200. `analyzer.py` never hit this because it has always set one.
2. **`URLError` on every request.** This machine's Python trust store fails SSL
   against certain hosts — the same defect that hit `wallet.py` in August. Now
   uses certifi's bundle when available, falling back gracefully. Verified live.
   The old handler printed only the exception class, hiding whether the cause
   was DNS, a refused connection or SSL; it now reports the real reason.
3. **One token's failure killed the whole run.** Seven tokens scanned cleanly,
   then a DNS blip aborted everything. Failures are now per-token, and such
   tokens count as neither reached nor skipped.

Also fixed: fabricated percentages (one token reported +1,406,818%) were being
treated as winners, so the scan chased the early buyers of bad quotes; the same
1000x bound the analysis uses now applies. And winners are scanned newest-first,
because paging walks backwards from now, so reaching an older token's launch
costs more pages — this turns "too active to reach" from the common case into
the exception.

---

## 5. Open items, unchanged from the review

- The dashboard's JavaScript scorer is a separate implementation and has drifted
  from the Python engine. Use the CLI for authoritative scores.
- State and log writes are not transactional; a crash between a log append and a
  state save can still duplicate or lose an accounting event.
- Fixed-horizon observation now covers bought coins, but candidates excluded
  before tracking are still uncounted, so selection bias is reduced, not removed.
- No chronological holdout framework yet. Premature until there is clean data.
- Four ownership/authority rules show *lower* rug rates when they fire. Two are
  Solana rules on thin samples where a launchpad effect is plausible; two are
  Base rules where that explanation cannot apply and the inversion is
  unexplained. Stratify by chain, launch source, age and liquidity before
  re-weighting. The two heaviest weights in the scorer sit on rules with no
  supporting evidence.

---

## 6. How to verify

```powershell
cd path\to\memecoin-checker
python -m unittest discover        # expect: Ran 105 tests ... OK
python paperbot.py --status
python analyze_outcomes.py --since 2026-09-07T20:47
```

The evaluation period starts **2026-09-07T20:47 UTC** — the first run of v1.5
with a working affordability constraint and no inherited artefacts. A clean run
should show no outage-cluster exclusions at all.
