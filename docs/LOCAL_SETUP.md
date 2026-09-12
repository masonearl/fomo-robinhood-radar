# Local macOS setup

This fork runs the Radar collector, chain watcher, API, and dashboard on a Mac.
It is an analytics installation. It has no brokerage connection, private keys,
funded order execution, or automatic trade approvals.

## Open and manage

- Dashboard: http://127.0.0.1:4322
- API documentation: http://127.0.0.1:8767/docs
- API health: http://127.0.0.1:8767/api/health
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

The first direct chain scan imported 546 fills for 147 wallets over about 5.7
hours, with 23 RPC requests and zero collection errors. Further fills arrive
through the watcher. Summary statistics from the third-party indexer are labeled
as that source; they are not independently reproduced profits. The scoring
export's `last_7d` and `last_30d` fields only contain locally collected fills
inside those windows, not guaranteed complete seven- or thirty-day histories.

Scoring is manual by default and requires no model API key:

```sh
.venv/bin/fomo-radar score --export pending_scores.json --unscored
# Review the contexts and schema in that file with a model, saving its results.
.venv/bin/fomo-radar score --import scored_review.json --model-label manual:review
```

The collector refreshes `pending_scores.json`; it does not import scores itself.
No wallets were assigned a score during initial setup. Trusted signal feeds
will remain empty until reviewed scores exist. An Anthropic API key and
`SCORER=api` enable the upstream unattended scoring path; this requires separate
API billing and is not configured here. Enter any key locally in `.env`, never
in Git. FOMOAPI, Telegram, browser capture, and paid data sources are optional
and are not configured.

The upstream burst settings are preserved: conviction delta 4.0, at least three
wallets, a 30-minute window. These are not an exact implementation of the post's
"four 80+ wallets" sentence: conviction sums squared normalized scores, and
four wallets scored 80 contribute 2.56. Treat the code's rule and the marketing
claim separately when evaluating results.

The frontend identifies this as a local research installation and explains its
empty score state. The API's `health.ok` indicates that the API/database responds;
inspect fill timestamps, service status, and collector logs for freshness.

## Validation on 2026-09-12

- All 195 upstream tests passed; one dependency deprecation warning.
- Production Astro build completed with Node 24.19.0.
- All four LaunchAgents started and served real local data.
- Main, bursts, leaderboard, and exits pages returned HTTP 200.
- Scoring context export completed without an API key.

`.env`, SQLite files, generated scores, frontend dependencies, build output, and
logs stay outside version control. Upstream code and its MIT license are retained.
