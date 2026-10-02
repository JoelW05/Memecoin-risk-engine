# Memecoin Risk Engine

A risk scorer for Solana and Base memecoins, and a paper-trading harness built to find out whether the score predicts anything.

Most of the work in this repository is not the scorer. It is the measurement infrastructure that had to be built, broken, and rebuilt before any number it produced could be trusted. Four separate defects were found in which the system's own data collection was corrupting its results — each one making the results look *better* than reality — and each is documented below with the evidence that exposed it.

Python standard library only, apart from an optional `certifi` and a deliberately isolated live-execution scaffold. **205 tests.**

---

## The one validated result

Delisting rate at 24 hours, by risk bucket. Two readings, the second on data collected after the first:

| Risk bucket | Reading 1: ~43,000 observations (to 21 Sep) | Reading 2: 13,309 new tokens (21–27 Sep) |
|---|---:|---:|
| Lower (0-20) | **0.4%** | **0.0%** (n=185) |
| Moderate (21-45) | **15.5%** | **14.5%** (n=3,208) |
| High (46-70) | **43.3%** | **45.2%** (n=9,483) |
| Extreme (71-100) | **69.9%** | **74.1%** (n=433) |

Reading 1 is section 1a of `analyze_outcomes.py`; reading 2 is the forward window of `reweight_experiment.py`, one row per token. "Delisted" means Dexscreener no longer indexes the token at 24 hours: the nearest observable proxy for "gone", not a verified on-chain liquidity removal. Both readings are cleanly monotonic across four buckets. A token the scorer rates Lower was still listed a day later more than 99% of the time; one it rates Extreme had vanished in roughly 70% of cases.

**What this does not show.** It predicts *survival*, not *return* or *drawdown*. Median 24-hour returns by bucket are roughly −2% to −6% and not reliably ordered, and on the broader question "delisted, or still listed but down 50% or more at 24 hours" the score is close to chance (AUC 0.53 on the forward window). The score tells you what will still exist tomorrow. It does not tell you what will go up, or what will crash while remaining listed.

**There is no validated trading edge.** Across 98 closed paper trades since 7 Sep the profit factor is 1.47 (+25.5 on a 100-unit pot), down from above 5 on the first dozen trades. That is not evidence of an edge and is not presented as one. No real money has ever been traded.

---

## The part worth reading: four ways the measurement lied

### 1. API rate limits were being recorded as tokens dying

The bot priced tokens one HTTP request at a time. Against a free API's rate limit, requests began failing — and a failed request was indistinguishable, in the code, from "this token has no liquidity any more". Rate-limit bursts therefore appeared as dozens of coins rugging simultaneously.

**How it surfaced.** A spot-check of the "rugged" list contained wrapped SOL, WETH and cbBTC — three of the most liquid assets in existence — all recorded as having rugged in the same second.

**Fixes.** Batched pricing (30x fewer requests), per-host pacing derived from each provider's published limit, and a hard type distinction between "request failed" and "provider returned no listing" that the type system now enforces at every call site.

### 2. The batching fix introduced a subtler version of the same bug

Dexscreener's multi-address endpoint returns at most 30 *pairs*, not 30 *tokens*. Tokens at the tail of a request silently fell off the response — and an absent token looks exactly like a delisted one. The request succeeded, so no circuit breaker caught it, and the same tokens fell off every cycle, so they hit the streak limit together and closed as a cluster.

**Fix.** Small batches, and any token missing from a response that hit the cap is re-asked individually before anything concludes it is gone.

### 3. A fabricated price left $1.7bn of phantom cash in the ledger

A glitched quote closed one position at 79,806.68 against an entry of 0.0001324 — a 602,769,486x move — and credited the proceeds to the simulated cash balance. The analysis had always excluded that trade from its statistics, so published results stayed honest, but nothing corrected the state file.

**The consequence was worse than the number.** For sixteen days the affordability constraint never once refused a purchase. The bot committed roughly 1,200 units against a 100-unit pot. Rebuilding the balance honestly yields a negative figure, which is the arithmetic proof that the constraint was never enforced.

**Resolution.** Cash is now rebuilt from first principles rather than patched, impossible trades are voided into an audit list (deliberately *not* the same as booking a loss, since the true outcome is unknown), and an inconsistent ledger now refuses to trade rather than warning. **All trading results from that period were withdrawn** — the per-trade prices were real, but the portfolio they formed was one no such pot could have held.

### 4. The research dataset had silently stopped collecting

Checkpoint observations were only examined inside the discovery branch of the tick loop. Once per-host pacing was added, a discovery cycle ran far longer than the 600-second grace window, so virtually every checkpoint was already late by the time anything looked at it.

**How it surfaced.** An observation-status table added on a reviewer's advice, reporting every due checkpoint before any censoring. It showed **2 priced out of 4,475 due**. Every prior report had simply not counted the ones it never saw.

**Fix.** Checkpoints are checked every tick. Capacity pressure now refuses new candidates rather than evicting in-flight ones — the old behaviour discarded the observation closest to completing its 24-hour horizon to make room for one that had produced nothing. Priced observations went from 2 to 15,966.

---

## What this demonstrates

- **Instrumenting a system to catch its own measurement failures.** The observation-status table exists specifically to make missing data visible rather than absent, and it immediately found a defect that every previous report had hidden.
- **Distinguishing missing data from negative data.** The recurring root cause across all four defects. Absence of evidence was being recorded as evidence of death, in four different places, each time flattering the results.
- **Withdrawing published results when they turn out to be unsound**, rather than retrofitting a story onto them.
- **Pre-registering a test, and reporting that it failed.** A re-weighting that improved ranking on a chronological holdout (AUC 0.663 to 0.719) was not deployed, because it zeroed safety rules whose risk the delisting label cannot see. A safety-constrained variant was frozen with a timestamp and hash and tested on 13,309 later tokens under a pass rule written in advance. It failed (significantly worse on Solana), and the weights were left unchanged.
- **Crash-safe persistence.** A durable journal commits state and append-only logs together; the test suite kills a real child process mid-append and asserts recovery leaves exactly one copy of the event.
- **Acting on external review.** Two independent code reviews were adopted, including three defects found in code written the day before: a broken producer/consumer handoff, look-ahead bias in winner selection, and airdrops being counted as purchases.

---

## Architecture

| Component | File | Role |
|---|---|---|
| Scoring engine | `analyzer.py` | Fetching, validation, rules, confidence, trade parameters. Shared by every component. |
| Paper bot | `paperbot.py` | Discovery, entry gates, simulated execution, fixed-horizon observation, valuation policy |
| Persistence | `state_store.py` | Journalled commits across state and multiple append-only logs |
| Analysis | `analyze_outcomes.py` | Observation status, bucket and per-rule attribution, confidence, P&L |
| Analysis | `reweight_experiment.py` | Chronological-holdout and pre-registered forward tests of alternative rule weights (read-only) |
| Analysis | `trade_review.py` | Read-only review of the paper book: open positions by age, exits, buys per day |
| CLI | `memecheck.py` | Score a single token |
| Dashboard | `index.html`, `app.js`, `server.py` | Browser front end (own scorer implementation — see Limitations) |
| Wallet research | `find_wallets.py`, `wallet_research.py` | Copy-trading feasibility study (see below) |
| Live scaffold | `wallet.py`, `jupiter_swap.py` | Inert. Never funded, never executed. |

Data sources: Dexscreener, GeckoTerminal, GoPlus, RugCheck, Honeypot.is — all free tiers. Helius for the wallet research, which needs your own key.

### Scoring

Additive points per rule, capped at 100, bucketed into four risk labels. Rules cover liquidity depth, pool age, holder concentration, mint and freeze authority, contract verification, honeypot simulation, taxes and social presence. Missing data lowers a separate **confidence** score and is recorded as `unknown` rather than silently treated as clean — a token nothing could be checked on must never read the same as one checked and found safe.

### Copy-trading study

`find_wallets.py` derives a wallet watchlist from this system's own outcome data rather than a public leaderboard — it takes coins that gained 50%+, asks who bought them early, and ranks wallets by lift over a matched control group. `wallet_research.py` then measures week-to-week persistence. If retention sits near chance, there is nothing to copy and the watcher does not get built. Deliberately structured so selection and evaluation windows cannot overlap.

---

## Running it

```bash
python -m unittest discover        # 205 tests
python memecheck.py <token address or Dexscreener link>
python paperbot.py --once
python analyze_outcomes.py --since 2026-09-07T20:47
```

No dependencies for the above. The live scaffold needs `pip install solders certifi`; the wallet research needs a Helius key in `HELIUS_API_KEY`.

---

## Limitations

- The dashboard carries its own JavaScript reimplementation of the scorer, which has drifted from the Python engine. Use the CLI for authoritative scores.
- Six rules (authority, ownership, honeypot and RugCheck checks) show *lower* delisting rates when they fire, including the two heaviest weights in the scorer, Solana mint and freeze authority. The inversion held up in later data, but these rules fire on under 1% of tokens, so correcting them changes little. Plausible explanations: launchpads renounce authority automatically, and tokens that cannot be sold stay listed, so a delisting label cannot see the risk those rules exist for. Neither is tested, and the weights were left as they are.
- The Solana top-holder concentration rule has never fired: the data provider's Solana response does not include the field it reads, so it always reports "unknown". It was found by checking per-rule firing counts, and is not yet fixed.
- The observed sample is conditioned on what discovery surfaced and on usable observations. Fixing the accounting independently did not make the sample independent of trading decisions.
- No scoring weights have been fitted to outcomes. A chronological-holdout framework for testing re-weightings is built and has been run once; its pre-registered forward test failed (see above), so the original weights stand. A different approach, such as separate weights per chain, would need a new frozen test on new data.

---

## Disclaimer

Research code. Not financial advice, not a trading recommendation, and not a validated strategy. No real funds have ever been traded by this system. Memecoins are a market in which most participants lose money, and nothing here changes that.

## Licence

MIT.
