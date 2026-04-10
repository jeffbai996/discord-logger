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

# Continuous polling
python logger.py watch

# Backfill last 100 messages (ignores state, appends to log)
python logger.py backfill 100

# Search logs
python search.py "pregnancy pillow"
python search.py --author alice -n 50
python search.py "meeting" --channel 123456789012345678
python search.py "dinner" --json
```

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

## State

`state/<channel_id>.last` stores the last seen message ID per channel. Delete to re-fetch from the beginning (note: Discord only returns the last ~100 messages without pagination cursors).

## License

MIT
