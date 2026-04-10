"""Discord channel logger — polls channels and appends messages to JSONL files."""

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
    """Fetch messages from a Discord channel via REST API.

    Returns messages oldest-first. Discord returns newest-first,
    so we reverse before returning.
    """
    headers = {"Authorization": f"Bot {token}"}
    params: dict = {"limit": limit}
    if after:
        params["after"] = after

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
        return []

    messages = resp.json()
    messages.reverse()  # oldest first
    return messages


def slim_message(msg: dict) -> dict:
    """Extract the fields we care about from a Discord message object."""
    attachments = []
    for att in msg.get("attachments", []):
        attachments.append({
            "id": att.get("id"),
            "filename": att.get("filename"),
            "content_type": att.get("content_type"),
            "size": att.get("size"),
            "url": att.get("url"),
        })

    return {
        "id": msg["id"],
        "channel_id": msg["channel_id"],
        "timestamp": msg["timestamp"],
        "author_id": msg["author"]["id"],
        "author_name": msg["author"].get("username", "unknown"),
        "content": msg.get("content", ""),
        "attachments": attachments if attachments else None,
        "reply_to": msg.get("message_reference", {}).get("message_id"),
    }


def poll_channel(
    token: str, channel_id: str, log_dir: Path, state_dir: Path
) -> int:
    """Fetch new messages for a channel and append to its log file.

    Returns the number of new messages logged.
    """
    after = get_last_message_id(state_dir, channel_id)
    all_messages: list[dict] = []

    # Paginate — Discord returns max 100 per request
    cursor = after
    while True:
        batch = fetch_messages(token, channel_id, after=cursor)
        if not batch:
            break
        all_messages.extend(batch)
        cursor = batch[-1]["id"]
        if len(batch) < 100:
            break
        time.sleep(0.5)  # rate limit courtesy

    if not all_messages:
        return 0

    log_file = log_dir / f"{channel_id}.jsonl"
    with open(log_file, "a", encoding="utf-8") as f:
        for msg in all_messages:
            slim = slim_message(msg)
            # Drop None values to keep lines compact
            slim = {k: v for k, v in slim.items() if v is not None}
            f.write(json.dumps(slim, ensure_ascii=False) + "\n")

    save_last_message_id(state_dir, channel_id, all_messages[-1]["id"])
    log.info("Channel %s: logged %d new messages", channel_id, len(all_messages))
    return len(all_messages)


def run_once(config: dict) -> int:
    """Poll all channels once. Returns total new messages."""
    total = 0
    for channel_id in config["channel_ids"]:
        total += poll_channel(
            config["token"], channel_id, config["log_dir"], config["state_dir"]
        )
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


def main() -> None:
    config = get_config()
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"

    if mode == "watch":
        run_watch(config)
    elif mode == "once":
        total = run_once(config)
        log.info("Done — %d new message(s) total", total)
    elif mode == "backfill":
        # Backfill ignores state — fetches last N messages
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 100
        for channel_id in config["channel_ids"]:
            messages = fetch_messages(config["token"], channel_id, limit=min(limit, 100))
            log_file = config["log_dir"] / f"{channel_id}.jsonl"
            with open(log_file, "a", encoding="utf-8") as f:
                for msg in messages:
                    slim = slim_message(msg)
                    slim = {k: v for k, v in slim.items() if v is not None}
                    f.write(json.dumps(slim, ensure_ascii=False) + "\n")
            log.info("Backfilled %d messages for channel %s", len(messages), channel_id)
    else:
        print("Usage: python logger.py [once|watch|backfill [limit]]")
        sys.exit(1)


if __name__ == "__main__":
    main()
