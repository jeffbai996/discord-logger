# discord-logger

Lightweight Discord channel logger. Polls channels via the Discord REST API and appends messages to local JSONL files. No database, no bot framework — just flat files you can grep.

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
Tailscale-only access — it has no authentication.

For local use, `python ui.py` is fine. For the fragserv deployment, prefer
gunicorn so a stuck request doesn't block the whole UI:

```
gunicorn -w 2 -b 0.0.0.0:5050 --timeout 30 ui:app
```

The systemd user unit `discord-logger-ui.service` can use either; switch its
`ExecStart` line when you want the upgrade.

## License

MIT

## Architectural Debt (Todo)
- **State persistence**: Move from flat files to SQLite for atomic commits and easier querying.
- **Blocking IO**: Migrate to async so one slow channel doesn't block the poll cycle.
- **Message lifecycle**: Implement Gateway listeners to capture edits/deletes that REST polling misses.
- **Log rotation**: Date-based rotation to prevent monolithic files on high-traffic channels.
