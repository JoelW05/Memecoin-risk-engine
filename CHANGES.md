# Memecoin Risk Engine — review and release 1.6.0

8 September 2026. **Analyzer 1.2.0; paperbot 1.6.0.** This review used the supplied v1.5.0 archive with the three separately attached wallet files applied over it. The starting suite reproduced **105 passing tests**. The revised suite passes **159 tests**, including a real child-process interruption during a commit.

## Assessment of the current project

The project has made substantial progress in separating observations, missing data and valuation policy. The correction accepting an explicit Dexscreener `pairs: null` as no listing is retained. Bought-token checkpoints, request retries, writer locking and the explicit unresolved-position policy are also retained.

The historical cash corruption makes withdrawing the old portfolio results appropriate. However, “the research findings stand” is stronger than the evidence allows: older research still has historical label errors and cohort limitations. Before bought-token tracking existed, a corrupted affordability constraint could change which coins were bought and consequently omitted from skipped-only observations. Independent cash accounting does not make the observed sample independent of trading decisions.

The archive contains source and reports, not the production state, transaction cache or outcome dataset. I cannot verify the reported repair, the live evaluation start, historical rule rankings, or the claimed −96% median from these attachments. Zero outage-cluster exclusions would be a useful diagnostic, but cannot establish that all pricing and label paths are correct. A week of data also does not automatically establish statistical reliability.

## Changes implemented

### Accounting and persistence

- **Corrupted cash now prevents trading.** Validate finite values, positive starting pot, nonnegative cash/investments, the existing extreme-cash bound, each closed trade's proceeds-minus-cost calculation, and `pot − open investment + realized P&L = cash`. A mismatch raises an error before an entry; startup and each trading operation also validate the ledger. No balance is silently repaired.
- **Recoverable state/log commits.** New `state_store.py` stages CSV/JSONL output and state changes together. A durable `.txn` journal records the commit before outputs are applied. Recovery completes partial appends and state replacement without duplicating records. Exceptions before commit roll back memory and staged output. A pending commit stops further operations until recovery.
- **Position exits precede discovery.** Position checking, due observations and discovery commit separately. A failed discovery operation cannot undo a position exit already committed earlier in the tick. One-shot mode also checks newly opened positions afterward. API work remains synchronous; slow calls can still delay later ticks.
- **Repair scripts respect the writer lock.** `repair_cash.py` and `repair_state.py` acquire the same canonical-path lock as the bot and release it on completion. Pending commits are recovered before an applied repair; dry runs refuse pending journals instead of silently modifying them.
- **Read-only/reset and path guards.** Reject `--reset` combined with `--status`/`--report`, duplicate state/log paths, nonfinite or negative monetary options, and percentage fees at or above 100.
- **Missing-listing clock excludes observed request failures.** A failed request or invalid price breaks the prior no-listing interval, so an outage cannot silently age an existing interval into the 24-hour zero-valuation policy. This does not prove continuous coverage during downtime or sleep.
- **More complete provenance.** Candidate configuration now includes fees, observation flags, timing and discovery settings. Closed trades record the exit bot version, exit costs and valuation policy separately from their entry version.

### Wallet discovery and research

- **Watchlist handoff works.** Strip inline `#` comments, blank/comment lines and UTF-8 BOMs; deduplicate addresses. Previously, the producer's `ADDRESS # winners=...` lines became malformed addresses in the consumer.
- **Stop choosing the best later outcome.** Select each token's earliest eligible discovery observation, rather than whichever later reassessment had the largest return. This reduces outcome-dependent selection; it does not eliminate survivorship among usable observations.
- **Buy attribution requires payment.** A token receipt, self-transfer or fee-only airdrop is not sufficient. The conservative parser requires opposing SOL/token flows and rejects failed transactions and malformed/nonfinite transfer data. Fees are applied after identifying the economic direction.
- **Respect the discovery window.** Count transfers from ten minutes before discovery through the configured post-discovery window. The scan no longer includes arbitrarily old buyers from the last page, and describes window coverage rather than claiming to have reached token launch.
- **Incomplete history is explicit.** Reject malformed or unordered history pages, stop repeated cursors, deduplicate wallet transaction signatures, and reject wallet histories whose page budget expires before the requested cutoff. Incomplete wallets are reported and excluded rather than silently ranked on partial history.
- **Require a control cohort.** No ranked watchlist is written when no winner or no control histories were covered. A successful scan with no qualifying wallets writes an empty, explanatory watchlist, replacing stale prior selections. Watchlist/cache replacements use temporary files and atomic replacement.
- **Bound cache reuse.** Cache entries include schema, fetch time, lookback, page budget and evaluation start. Old bare-list caches are refreshed; expiry defaults to six hours. Transactions are filtered again to the requested time interval on reuse.
- **Prospective evaluation boundary.** New watchlists record their selection time. `wallet_research.py` automatically evaluates only subsequent transactions, and rejects an explicit evaluation start before selection. An unmarked/manual list is labeled exploratory. Keep a selected watchlist fixed while future data accumulates; regenerating it starts a new selection period.
- **Honest week comparisons.** Compare adjacent, complete calendar weeks only, bounded by the requested start and cached coverage. Display the chance overlap implied by actual group sizes instead of assuming 50% despite ties or odd sample sizes. These are descriptive statistics, not a test establishing a tradable edge.
- **Safer errors.** Redact the API key if a transport error contains it; retry malformed JSON responses through the existing retry mechanism. No live Helius request or key inspection was performed during this review.

### Analysis

- Add `--chain`, `--analyzer-version`, `--paperbot-version` and exclusive `--until` filters across candidates, observations and closed trades. Version selection refers to the entry/discovery version; exit version is now recorded separately.
- Do not classify multiple horizons for the same token as a multi-token outage cluster. Missing timestamps do not manufacture a cluster either. The filter remains a heuristic for historical labels.
- Correct the per-trade downside metric to `mean(return) / sqrt(mean(min(return, 0)^2))`. Equal-sized losses now have nonzero downside risk. This is not an annualized portfolio Sortino ratio.
- Correct stale text that described all checkpoints as skipped-only and claimed realized-only drawdown was a bound on portfolio drawdown.

The motivation for keeping selection and evaluation separate follows the standard [data-leakage principle](https://scikit-learn.org/stable/common_pitfalls.html#data-leakage). The existing Helius integration uses its [Enhanced Transactions API](https://www.helius.dev/docs/api-reference/enhanced-transactions/gettransactions); the release retains that interface and does not claim that transfer heuristics are a complete transaction decoder.

## Validation

**159 tests pass: the original 105, with corrected policy fixtures, plus 54 additional regressions.** Existing wallet fixtures were updated to include actual payment, use the earliest observation, expect repeated-cursor termination, and expect an explicitly empty completed watchlist. These changes replace incorrect expectations, rather than suppressing failures.

New tests cover accounting rejection, outage-clock behavior, producer/consumer compatibility, failed/airdrop transactions, historical coverage, cache expiry, prospective boundaries, date/chain filtering, tied groups, downside arithmetic, and real buy/sell persistence. Commit tests interrupt execution before logging, midway through an append, between multiple outputs and after state replacement. One child process exits without running cleanup after a log append; recovery restores the intended state with one copy of the event. Conflicting log boundaries are refused rather than overwritten.

These are offline and local-filesystem tests. They do not verify exchange execution, provider uptime, real trade history, sudden power loss on every storage device, or profitability.

## Adoption and recovery

1. Extract the complete release into a separate directory and run `python -m unittest discover -v`. **Do not copy only the three wallet files:** `paperbot.py` now requires `state_store.py` and the analysis changes belong with this release.
2. Stop the existing bot/supervisor using its normal process controls. Back up state and logs. Replace the source files together, preserving the production data. No production process or scheduled task was changed by this review.
3. Start using the existing state/log paths. The state schema remains readable; a damaged ledger is refused for inspection. Do not use `--reset` as an upgrade step. `python repair_cash.py --dry-run` provides a separate diagnostic when the writer is stopped.
4. If a `.txn` journal remains after interruption, keep it with the state and its original log paths. Restarting normally finishes recovery. Do not delete the journal, edit its logs, or move the installation before recovery. If recovery reports conflicting log content, preserve the files for inspection.

Each operation has a recoverable commit, not instantaneous multi-file snapshot isolation. Readers can briefly see committed log data ahead of the state file during replay. Use one state writer and dedicated logs; do not share logs between installations. Legacy repair rewrites are locked but are not all journaled. The historical `repair_state.py` reconstructs missing fields and cannot recreate old checkpoint prices; it should not be used to manufacture a clean historical dataset.

Example separated analysis:

```powershell
python analyze_outcomes.py --since 2026-09-07T20:47:00Z --chain solana --paperbot-version 1.5.0
python analyze_outcomes.py --since 2026-09-07T20:47:00Z --chain base --until 2026-09-15T00:00:00Z
```

For wallet research, generate a watchlist once, retain that file, and run `wallet_research.py` later as new observations accumulate. It may report too little complete prospective data initially; that is expected. Old caches can cause a fresh API fetch because their coverage metadata is unavailable.

## Remaining limitations

- The dashboard still contains its own JavaScript scorer. Use the Python CLI for the revised engine; dashboard parity remains separate work.
- Sampling remains conditional on discovery, usable observations, sufficient API coverage and the recognized swap subset. Infinite lift with zero observed control hits is not certainty. Reported persistence also conditions on realized activity in both weeks.
- Transfer-based P&L does not fully decode routed/wrapped SOL flows, token-to-token swaps, account rent, transfer taxes, externally acquired inventory or every program's semantics. It needs recorded real fixtures before its figures can be treated as audited wallet returns.
- No new scoring weights were fitted, no measured trading advantage was established, and no wallet or live-execution path was used.
- Source fixes cannot retroactively validate the withdrawn trading history or historical research labels. Preserve the old material as an audit record and evaluate clearly identified later cohorts.
