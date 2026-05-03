# discord-logger

> **Archived.** This repo is preserved as-is. It works, but I'm no longer
> developing it. Fork it if you want to extend it.

Lightweight Discord channel logger. Polls channels via the Discord REST API and appends messages to local JSONL files. No database, no bot framework — just flat files you can grep.

Built as a quick-and-dirty tool for personal use. Useful as a reference for:
- Polling Discord's REST API without the bot/gateway overhead
- File-locked cron + watch concurrency (`state/poll.lock` via `fcntl`)
- Adaptive backoff to silence quiet hours
- A Flask web UI for browsing JSONL logs locally

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your bot token and channel IDs
```

## Usage

```bash
# Poll once (for cron)
python logger.py once

# Continuous polling (honors POLL_INTERVAL, default 300s)
python logger.py watch

# Backfill — fetches last N messages, dedupes against existing log
python logger.py backfill 500

# Search logs
python search.py "pregnancy pillow"
python search.py --author alice -n 50
python search.py "meeting" --channel 123456789012345678
python search.py "dinner" --json
```

`once` and `watch` are safe to run concurrently — a file lock in `state/poll.lock`
prevents overlap. A cron `once` invocation that lands while `watch` is polling
will log "Another poll is in progress, skipping" and exit cleanly.

## Cron example

Poll every 5 minutes:

```
*/5 * * * * cd /path/to/discord-logger && /path/to/venv/bin/python logger.py once >> /dev/null 2>&1
```

## Log format

One JSON object per line in `logs/<channel_id>.jsonl`:

```json
{"id": "123", "channel_id": "456", "timestamp": "2026-01-01T00:00:00.000000+00:00", "author_id": "789", "author_name": "alice", "content": "hello world"}
```

`attachments` and `reply_to` are only present when non-empty.

## State

- `state/<channel_id>.last` — last seen message ID per channel. Delete to re-fetch (Discord returns at most ~100 messages without a cursor; use `backfill` for a wider window).
- `state/last_activity.txt` — timestamp of last poll that produced messages; drives adaptive backoff.
- `state/next_poll.txt` — if present and in the future, poll cycles skip until that time. Deleted on any new activity.
- `state/poll.lock` — fcntl advisory lock; prevents overlapping invocations.

## Adaptive backoff

After 30 min with no new messages, sets a 30-min skip window. Any activity
resets to normal polling immediately. Primarily benefits cron-driven setups —
`watch` mode still sleeps `POLL_INTERVAL` between cycles, it just no-ops during
the skip window.

## Rate limits

`fetch_messages` honors Discord's 429 `Retry-After` header (capped at 60s to
avoid stalling cron). For 5xx and network errors, pagination stops and the
cursor advances to the last successfully-fetched message — Discord's `after`
parameter guarantees no gaps, so the next poll resumes cleanly.

## Web UI

The Flask web UI at `ui.py` runs on port 5050 (default) and is meant for
private-network-only access (e.g. Tailscale or VPN) — it has no authentication.

### Web UI configuration

Set these env vars before launching `ui.py`:

| Var | Purpose |
| --- | --- |
| `UI_PORT` | port to bind (default 5050) |
| `SQUAD_DIR` | directory for shared context files (default `~/.local/share/discord-logger/shared`) |
| `CHANNELS_CONFIG` | path to a JSON file mapping channel IDs to display labels + order. See `CHANNELS_CONFIG_EXAMPLE.json` |
| `BOTS_CONFIG` | path to a JSON file listing bot personas editable from the UI. See `BOTS_CONFIG_EXAMPLE.json` |

Without `CHANNELS_CONFIG` or `BOTS_CONFIG`, the UI runs but those panels stay empty.

For local use, `python ui.py` is fine. For a multi-user / always-on
deployment, prefer gunicorn so a stuck request doesn't block the whole UI:

```
gunicorn -w 2 -b 0.0.0.0:5050 --timeout 30 ui:app
```

The systemd user unit `discord-logger-ui.service` can use either; switch its
`ExecStart` line when you want the upgrade.

## License

MIT

## Known limitations (won't fix — repo archived)

- **State persistence is flat files.** SQLite would be cleaner but adds dependencies.
- **Blocking IO.** One slow channel blocks the poll cycle. Async refactor would fix it.
- **Message lifecycle.** REST polling misses edits and deletes that don't bump the message ID. A Gateway listener would catch them.
- **No log rotation.** High-traffic channels grow monolithic files. Date-partitioned files would be the obvious next step.

If you need any of these, fork.
