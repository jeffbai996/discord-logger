"""Discord channel logger — polls channels and appends messages to JSONL files."""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

BASE_URL = "https://discord.com/api/v10"


def get_config() -> dict:
    """Load and validate configuration from environment."""
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        log.error("DISCORD_BOT_TOKEN not set")
        sys.exit(1)

    raw_ids = os.getenv("DISCORD_CHANNEL_IDS", "")
    channel_ids = [cid.strip() for cid in raw_ids.split(",") if cid.strip()]
    if not channel_ids:
        log.error("DISCORD_CHANNEL_IDS not set or empty")
        sys.exit(1)

    log_dir = Path(os.getenv("LOG_DIR", "./logs"))
    state_dir = Path(os.getenv("STATE_DIR", "./state"))
    poll_interval = int(os.getenv("POLL_INTERVAL", "300"))

    log_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    return {
        "token": token,
        "channel_ids": channel_ids,
        "log_dir": log_dir,
        "state_dir": state_dir,
        "poll_interval": poll_interval,
    }


def get_last_message_id(state_dir: Path, channel_id: str) -> Optional[str]:
    """Read the last seen message ID for a channel."""
    state_file = state_dir / f"{channel_id}.last"
    try:
        return state_file.read_text().strip() or None
    except FileNotFoundError:
        return None


def save_last_message_id(state_dir: Path, channel_id: str, message_id: str) -> None:
    """Persist the last seen message ID for a channel."""
    state_file = state_dir / f"{channel_id}.last"
    state_file.write_text(message_id)


def fetch_messages(
    token: str, channel_id: str, after: Optional[str] = None, limit: int = 100
) -> list[dict]:
    """Fetch up to `limit` messages from a Discord channel via REST API.

    Paginates automatically when limit > 100 (Discord's per-request max).
    Returns messages oldest-first.
    """
    headers = {"Authorization": f"Bot {token}"}
    all_messages: list[dict] = []
    cursor = after

    while len(all_messages) < limit:
        batch_size = min(100, limit - len(all_messages))
        params: dict = {"limit": batch_size}
        if cursor:
            params["after"] = cursor

        try:
            resp = requests.get(
                f"{BASE_URL}/channels/{channel_id}/messages",
                headers=headers,
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            log.error("Failed to fetch channel %s: %s", channel_id, e)
            break

        batch = resp.json()
        if not batch:
            break

        # Discord returns newest-first; reverse to get oldest-first
        batch.reverse()
        all_messages.extend(batch)
        cursor = batch[-1]["id"]

        if len(batch) < batch_size:
            break  # no more pages

        time.sleep(0.5)  # rate limit courtesy between pages

    return all_messages


def slim_message(msg: dict) -> dict:
    """Extract the fields we care about from a Discord message object."""
    attachments = [
        {
            "id": att.get("id"),
            "filename": att.get("filename"),
            "content_type": att.get("content_type"),
            "size": att.get("size"),
            "url": att.get("url"),
        }
        for att in msg.get("attachments", [])
    ]

    record: dict = {
        "id": msg["id"],
        "channel_id": msg["channel_id"],
        "timestamp": msg["timestamp"],
        "author_id": msg["author"]["id"],
        "author_name": msg["author"].get("username", "unknown"),
        "content": msg.get("content", ""),
    }
    if attachments:
        record["attachments"] = attachments
    reply_to = msg.get("message_reference", {}).get("message_id")
    if reply_to:
        record["reply_to"] = reply_to

    return record


def poll_channel(
    token: str, channel_id: str, log_dir: Path, state_dir: Path
) -> int:
    """Fetch new messages for a channel and append to its log file.

    Returns the number of new messages logged.
    """
    after = get_last_message_id(state_dir, channel_id)
    all_messages = fetch_messages(token, channel_id, after=after)

    if not all_messages:
        return 0

    log_file = log_dir / f"{channel_id}.jsonl"
    with open(log_file, "a", encoding="utf-8") as f:
        for msg in all_messages:
            f.write(json.dumps(slim_message(msg), ensure_ascii=False) + "\n")

    save_last_message_id(state_dir, channel_id, all_messages[-1]["id"])
    log.info("Channel %s: logged %d new messages", channel_id, len(all_messages))
    return len(all_messages)


def _read_timestamp(path: Path) -> Optional[float]:
    """Read a Unix timestamp from a state file."""
    try:
        return float(path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _write_timestamp(path: Path, ts: float) -> None:
    path.write_text(str(ts))


IDLE_THRESHOLD = 1800  # 30 min with no messages → switch to backoff
BACKOFF_INTERVAL = 1800  # skip polls for 30 min during idle


def run_once(config: dict) -> int:
    """Poll all channels once. Returns total new messages.

    Implements adaptive backoff: if no messages for IDLE_THRESHOLD seconds,
    sets a next-poll timestamp BACKOFF_INTERVAL seconds in the future.
    Cron invocations that land before that timestamp are skipped.
    Any new messages reset to normal polling immediately.
    """
    state_dir = config["state_dir"]
    next_poll_file = state_dir / "next_poll.txt"
    activity_file = state_dir / "last_activity.txt"

    # Check if we're in backoff — skip if next poll is in the future
    next_poll = _read_timestamp(next_poll_file)
    now = time.time()
    if next_poll and now < next_poll:
        log.info("Idle backoff — next poll in %ds, skipping", int(next_poll - now))
        return 0

    total = 0
    for channel_id in config["channel_ids"]:
        total += poll_channel(
            config["token"], channel_id, config["log_dir"], config["state_dir"]
        )

    if total > 0:
        # Activity detected — reset to normal polling
        _write_timestamp(activity_file, now)
        next_poll_file.unlink(missing_ok=True)
    else:
        # No messages — check if idle long enough to backoff
        last_activity = _read_timestamp(activity_file)
        if last_activity is None:
            # First run or missing file — seed it
            _write_timestamp(activity_file, now)
        elif now - last_activity >= IDLE_THRESHOLD:
            _write_timestamp(next_poll_file, now + BACKOFF_INTERVAL)
            log.info("No activity for %dm — backing off for %dm",
                     int((now - last_activity) / 60), BACKOFF_INTERVAL // 60)

    return total


def run_watch(config: dict) -> None:
    """Continuously poll channels at the configured interval."""
    log.info(
        "Watching %d channel(s), poll every %ds",
        len(config["channel_ids"]),
        config["poll_interval"],
    )
    while True:
        run_once(config)
        time.sleep(config["poll_interval"])


def _get_seen_ids(log_file: Path) -> set[str]:
    """Read all message IDs already in a log file to prevent duplicates."""
    if not log_file.exists():
        return set()
    seen = set()
    for line in log_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            seen.add(json.loads(line)["id"])
        except (json.JSONDecodeError, KeyError):
            continue
    return seen


def main() -> None:
    parser = argparse.ArgumentParser(description="Discord channel logger")
    parser.add_argument(
        "mode",
        nargs="?",
        default="once",
        choices=["once", "watch", "backfill"],
        help="once (default), watch (continuous), backfill (fetch last N messages)",
    )
    parser.add_argument(
        "limit",
        nargs="?",
        type=int,
        default=100,
        help="Message limit for backfill mode (default 100)",
    )
    args = parser.parse_args()
    config = get_config()

    if args.mode == "watch":
        run_watch(config)
    elif args.mode == "once":
        total = run_once(config)
        log.info("Done — %d new message(s) total", total)
    elif args.mode == "backfill":
        for channel_id in config["channel_ids"]:
            log_file = config["log_dir"] / f"{channel_id}.jsonl"
            seen_ids = _get_seen_ids(log_file)
            messages = fetch_messages(config["token"], channel_id, limit=args.limit)
            new = [m for m in messages if m["id"] not in seen_ids]
            if not new:
                log.info("Channel %s: nothing new to backfill", channel_id)
                continue
            with open(log_file, "a", encoding="utf-8") as f:
                for msg in new:
                    f.write(json.dumps(slim_message(msg), ensure_ascii=False) + "\n")
            log.info("Backfilled %d messages for channel %s", len(new), channel_id)


if __name__ == "__main__":
    main()
