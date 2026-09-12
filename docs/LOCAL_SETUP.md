# Local macOS setup

This fork runs the Radar collector, chain watcher, API, and dashboard on a Mac.
It is an analytics installation. It has no brokerage connection, private keys,
funded order execution, or automatic trade approvals.

## Open and manage

- Dashboard: http://127.0.0.1:4322
- API documentation: http://127.0.0.1:8767/docs
- API health: http://127.0.0.1:8767/api/health
- Pipeline health: http://127.0.0.1:8767/api/system
- Source: https://github.com/masonearl/fomo-robinhood-radar

From the repository directory:

```sh
.venv/bin/python scripts/local_radar.py status
.venv/bin/python scripts/local_radar.py stop
.venv/bin/python scripts/local_radar.py start
.venv/bin/python scripts/local_radar.py restart
```

The four services are macOS user LaunchAgents. They start at login and restart
after a process failure. The Mac must remain signed in, awake, and online to
collect continuously. Stopping services leaves them installed for the next login;
`uninstall` removes the four service definitions and preserves the database.

Logs are in `~/Library/Logs/fomo-radar-local/`. LaunchAgent definitions are in
`~/Library/LaunchAgents/com.masonearl.fomo-radar-local.*.plist`. These local files
are not part of the fork. Log files are not automatically rotated; check their
size periodically for a long-running installation.

## Install on another Mac

Requires Python >=3.11, uv, npm, and Node >=22.19. The first installation used
Python 3.14.2 and Node 24.19.0. Node 22.14 is too old for the npm lockfile's undici
dependency. `--node` lets the launcher use a newer runtime without changing the
system Node installation.

```sh
uv venv .venv
uv pip install --python .venv/bin/python -e '.[dev]'
cp deploy/local.env.example .env
chmod 600 .env
npm --prefix site ci --no-fund --no-audit
.venv/bin/fomo-radar init
.venv/bin/fomo-radar discover --trenches
.venv/bin/fomo-radar track --limit 400
.venv/bin/python -m pytest -q
.venv/bin/python scripts/local_radar.py build --node /absolute/path/to/node
.venv/bin/python scripts/local_radar.py install --node /absolute/path/to/node
.venv/bin/python scripts/local_radar.py start
```

Build the site again and restart services after changing frontend files. Re-run
`install --node ...` followed by `restart` if the checkout or Node path changes.
The local launcher reserves loopback ports 4322 and 8767; it deliberately binds
both servers to this computer only.

## Data and scoring

The local configuration uses the free Robinhood Chain RPC for fills and
`https://rhtrenches.com` for wallet discovery. The old upstream default,
`https://robinhoodtrenches.com`, returned a 301 redirect on 2026-09-12. Its
replacement returned the same supported API format with 147 wallets.

The watcher polls on a 20-second target interval. RPC work or rate limits can
make a tick take longer. The main loop targets 15 minutes for tracking and token
updates, and one hour for wallet discovery; steps run sequentially. This is a
local database, not a copy of the author's historical database or scores.

Each cycle also reads older blocks toward a seven-day history target. It stores a
checkpoint after each successful range and resumes there next time; a failed range
does not advance the checkpoint. A changed wallet roster starts a new history
pass for that roster. The request budget is checked between ranges, so a single
range and its receipt/decimal lookups can exceed the nominal 20-request allowance.
History continues to fill in over subsequent cycles while the live watcher runs.

The first direct chain scan imported 546 fills for 147 wallets over about 5.7
hours, with 23 RPC requests and zero collection errors. Further fills arrive
through the watcher. Summary statistics from the third-party indexer are labeled
as that source; they are not independently reproduced profits. The scoring
export's `last_7d` and `last_30d` fields only contain locally collected fills
inside those windows, not guaranteed complete seven- or thirty-day histories.

## Automatic local scoring

The local profile uses `SCORER=rules` and `SCORE_INTERVAL=900`. It scores every
discovered wallet without a model API key. Scores are transparent research
rankings; this is a different scoring method from upstream's LLM judgement,
and no neural model was trained or copied.

Only locally stored fills explicitly marked `trade` enter the scoring ledger.
The ledger matches buys, partial sells, rebuys, and complete position closures.
Unknown-cost inventory, absent prices, ambiguous same-second buy/sell ordering,
future timestamps, quote assets, unverified fills, and gifts do not create profits.
Third-party PnL and follower counts do not increase the score.

Before entering trusted feeds, a wallet must have at least 20 usable verified fills,
eight complete position cycles across four tokens, and 24 hours between its oldest
and newest usable fills. Open known cost must not exceed matched closed cost.
A single win accounting for more than 60% of gross gains keeps the score below 60.
The configured 30 bps cost buffer applies to buy and sell dollar volume; it is an
assumption, not a reconstruction of actual fees, gas, or achievable copy fills.

For an eligible sample, the score combines observed win rate, profit factor,
and number of closed cycles. A losing sample is capped below 40; a thin sample
stays below 60 and remains watched. The cohort is still collected after a low score,
so a later record can change its ranking. No unseen inventory, open PnL, or missing
history is assumed to be zero profit or free inventory. These scores do not
estimate a probability of winning or demonstrate future profitability.

The wallet page shows the scorer version, sample counts, cost exclusions, and
reasoning. The home page shows all 147 scores and the separate number eligible
for signals. At deployment, all wallets remained below the eligibility cutoff;
the system was collecting evidence rather than generating trade signals.

## Optional model review

The upstream manual export/import workflow remains available:

```sh
.venv/bin/fomo-radar score --export pending_scores.json --unscored
# Review the contexts and schema in that file with a model, saving its results.
.venv/bin/fomo-radar score --import scored_review.json --model-label manual:review
```

Set `SCORER=manual` and restart the services before maintaining manual scores,
otherwise the automatic rules will replace them on the next cycle.
An Anthropic API key and `SCORER=api` enable upstream's unattended model scoring;
this needs separate API billing and is not configured here. The API path now
sends complete JSON context instead of cutting the context mid-value at 2,048
characters. Put credentials locally in `.env`, never in Git. FOMOAPI, Telegram,
browser capture, and paid data sources remain optional and unconfigured.

The upstream burst settings are preserved: conviction delta 4.0, at least three
wallets, a 30-minute window. These are not an exact implementation of the post's
"four 80+ wallets" sentence: conviction sums squared normalized scores, and
four wallets scored 80 contribute 2.56. Treat the code's rule and the marketing
claim separately when evaluating results.

The frontend identifies this as a local research installation. It refreshes every
30 seconds while visible and outside the search input. The API's `health.ok`
only indicates that the API/database responds; `/api/system` checks watcher
heartbeats, collection age, and score coverage/freshness. A quiet market with
healthy heartbeats is different from a stopped watcher. The dashboard reports
`collecting_evidence` when services and scoring work but no wallet qualifies.

## Validation on 2026-09-12

- All 207 tests passed; one dependency deprecation warning.
- Production Astro build completed with Node 24.19.0.
- All four LaunchAgents started and served real local data.
- Main, bursts, leaderboard, and exits pages returned HTTP 200.
- Scoring context export completed without an API key.
- All 147 wallets were scored with the configured free rules.
- A live collector cycle completed tracking, history, enrichment, holdings and scoring.
- Isolated fixtures verified complete scoring-to-burst API flow, fake-fill exclusion,
  cost accounting, weak-evidence gates, quiet/stale health, and backfill resumption.

`.env`, SQLite files, generated scores, frontend dependencies, build output, and
logs stay outside version control. Upstream code and its MIT license are retained.
