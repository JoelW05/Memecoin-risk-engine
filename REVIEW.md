> Historical review from 7 September. For this release, read CHANGES.md (paperbot 1.6.0).

# Memecoin Risk Engine — source review

Reviewed 7 September 2026. Based on the supplied 16 source files and report. No trading logs or state dataset were supplied, so the historical returns and claimed deployment status could not be reproduced.

The project is a useful research prototype, but its original labels and comparison groups do not support the report's strongest conclusions. Improving measurement is more valuable than adjusting the scoring weights now. The reported exit totals reconcile to −$40.81 and a profit factor of approximately 0.73; that verifies the report's arithmetic, not the underlying trades.

## Implemented fixes

| Priority | Confirmed problem | Change |
| --- | --- | --- |
| High | An error object, malformed quote or missing price could become evidence of a rug. | Validate price response shapes; reject nonfinite, nonpositive and malformed quotes. Distinguish unknown requests from empty listings. Individually recheck every batch omission. |
| High | A healthy SOL quote was treated as proof that another token had rugged, including on Base. | Remove automatic zero-price liquidation from missing listings. Positions stay unresolved until a usable quote returns. Missing observations do not produce binary rug labels. |
| High | Portfolio summaries silently valued an unpriced position at its entry price. | Report total equity and unrealized P&L as unknown when positions cannot be priced. |
| High | A restart could record one current price as the 1h, 6h and 24h outcome. | Use actual elapsed time; mark observations outside the default 600-second grace window as missed. Store observation status and lateness. |
| High | Outcome features joined to the first candidate with the same address, even after reassessment. | Assign assessment IDs; preserve entry snapshots and versions. Join by ID, or an exact chain/address/time legacy key. Ambiguous legacy matches are excluded. |
| High | Unknown checks entered the negative/control group. Some threshold rules had no negative observations at all. | Exclude unknown checks, aggregate repeated flags, instrument negative liquidity/age bands and Base restriction flags, and require adequate samples on both sides. |
| High | Empty security response objects could be described as clean reports. | Validate RugCheck and Honeypot result shapes; missing Base honeypot/mintability fields remain unknown. |
| High | Solana price matching lowercased mint addresses. | Preserve Solana case; normalize only Base addresses. |
| High | The dashboard server exposed the project directory on all network interfaces. | Bind to loopback and serve only index.html, app.js and style.css. Reject source, state, wallet and traversal requests for GET and HEAD. |
| Medium | Capacity eviction silently dropped unfinished observations. | Log each unfinished horizon as capacity-evicted before removal. |
| Medium | Report filtering compared timestamps lexically and mixed entry versions. | Normalize UTC timestamps, require discovery/open time as well as observation/close time after the cutoff, and include versions in drift detection. |
| Medium | A live JSONL read could fail on a partially written final record. | Skip and warn for an incomplete final record; continue rejecting complete corrupt records. |

Versions are now analyzer **1.2.0** and paperbot **1.4.0**. The scoring weights were not retuned; scoring can change for malformed security responses now correctly treated as unavailable.

## Corrections to the report's interpretation

1. **Missing listing is not confirmed rugging.** Neither three empty checks nor an unrelated successful benchmark establishes a token-specific on-chain event. A token trading today also does not prove it never experienced a liquidity removal or later recovery.
2. **The filtered percentages are sensitivity estimates, not guaranteed bounds.** Partial outages can affect tokens differently. A priced token in the same second does not validate all other responses, and correlated real failures are possible.
3. **The ownership explanation mixes chains.** `ownership_not_renounced` and `hidden_owner` are Base rules in this implementation. Solana launchpad behavior cannot establish why those comparisons look inverted. Stratify by chain, launch source, age and liquidity before testing that hypothesis.
4. **The reported median returns exclude unpriced/rug-labeled observations.** They are conditional on a usable price, not medians for every discovered token. The existing fixed-horizon tracker covers skipped candidates; bought positions are not observed on equivalent fixed horizons.
5. **The 9/120 survival result used a defective lookup and varying follow-up durations.** It does not establish that nine out of ten discovered tokens die within a week. Repeat with a defined cohort, fixed horizon and explicit missingness accounting.
6. **“Missing data is only unknown” is inaccurate.** The original engine already adds eight points for unavailable GoPlus, plus chain-specific source penalties. Confidence is an availability heuristic, not a calibrated probability of correctness.
7. **The browser does not share the Python implementation.** `app.js` still contains its own scorer. This patch does not make dashboard scores equivalent to Python scores.
8. **A £100 account is not modeled.** The simulator uses USD-equivalent units without FX conversion. Its original £ labels should not be interpreted as sterling returns.

## Remaining engineering work, in order

- Replace the browser's separate scorer with a shared backend result, or maintain a generated rules specification and cross-language parity fixtures. Until then, use the Python CLI for results from the revised engine.
- Add recorded API fixtures, per-provider schema checks and a shared request budget with backoff. Discovery and checkpoint work remain synchronous and can delay stop checks beyond the configured tick interval. More individual fallbacks can increase API load.
- Define evidence-based liquidity-removal labels separately from no-quote observations, realized trading loss and delisting. Unresolved positions intentionally remain open and can occupy all available slots; closing them requires an explicit, documented valuation policy.
- Extend fixed-horizon observation to bought candidates, count candidates excluded before tracking, and report missingness by chain, score and discovery source. The new report censors missing observations rather than assuming a return; this alone does not eliminate selection bias.
- Make state and logs transactionally consistent and enforce one writer per state file. Atomic JSON replacement already helps, but a crash between log append and state save can still duplicate or lose accounting events.
- Use chronological holdouts with token grouping and non-overlapping outcome windows. Compare unchanged weights with proposed alternatives only on later data; report uncertainty and an untouched final evaluation period. Scikit-learn documents the relevant [data-leakage pitfalls](https://scikit-learn.org/stable/common_pitfalls.html) and [time-dependent validation constraints](https://scikit-learn.org/stable/modules/cross_validation.html).
- Keep the live-execution scaffold separate from this research patch. Its transaction construction, signing, dependencies and provider compatibility were not validated here. No wallet was loaded, funded or used, and no bot or scheduled task was started.

The report's blanket “30 pairs” limit should be described as an observation of the specific endpoint used at the time. DEX Screener's current [API reference](https://docs.dexscreener.com/api/reference) documents a chain-specific token endpoint accepting up to 30 addresses. That is not a guarantee that every response from the legacy endpoint is complete. This patch retains the legacy endpoint and defensively rechecks omissions.

## Validation and use

All **52 offline tests pass**: the original 24 and 28 new regression tests. Tests include malformed API envelopes, failed fallback requests, case-sensitive mints, invalid numbers, missed horizons, missing-price positions, valid stop settlement, ambiguous joins, unknown controls, live-file tails and real local HTTP requests against the restricted server. No live market replay was possible without the dataset.

Extract the revised archive into a separate folder. From that folder run:

```powershell
python -m unittest discover -v
python memecheck.py --help
python paperbot.py --help
```

Back up existing state and logs before adopting the revised files. Do not use `--reset` to migrate. Older state loads without a destructive migration, but historical labels are not repaired and some old feature joins will intentionally be unmatched. New logs contain observation statuses and nullable `rugged_or_delisted`; use the revised analysis script with them. Start a separately identified paper-data period for evaluation. This archive has not been deployed to your running installation.
