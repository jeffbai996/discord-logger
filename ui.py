"""Web UI for discord-logger — read/edit messages across channels.

Edits are append-only to state/edits.jsonl; the raw log files are never
mutated. At read time, edits are folded over the base records.

Runs on http://0.0.0.0:5050. Intended for Tailscale access only.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

LOG_DIR = Path(os.getenv("LOG_DIR", "./logs"))
STATE_DIR = Path(os.getenv("STATE_DIR", "./state"))
EDITS_FILE = STATE_DIR / "edits.jsonl"
PORT = int(os.getenv("UI_PORT", "5050"))

# Human-readable channel names — keep in sync with summarise_channels.py
CHANNEL_NAMES = {
    "1491314655224795147": "cl-2 (MacClaude primary)",
    "1491337341619671111": "cl-1 (Fraggy primary)",
    "1492197457369497822": "cl-3 (Claudsson primary)",
    "1491352842886447214": "fam (family/private)",
    "1491337758709383279": "claude-channel (general)",
    "1494449115793457153": "gem (gemma channel)",
}

app = Flask(__name__)

# Simple mtime-based cache for edits. Reloaded on disk change or POST.
_edits_cache: dict[str, list[dict]] = {}
_edits_mtime: float = 0.0


def load_edits() -> dict[str, list[dict]]:
    """Load all edits grouped by msg_id, preserving append order."""
    global _edits_cache, _edits_mtime

    if not EDITS_FILE.exists():
        _edits_cache = {}
        _edits_mtime = 0.0
        return _edits_cache

    mtime = EDITS_FILE.stat().st_mtime
    if mtime == _edits_mtime and _edits_cache:
        return _edits_cache

    edits: dict[str, list[dict]] = {}
    for line in EDITS_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        edits.setdefault(e["msg_id"], []).append(e)

    _edits_cache = edits
    _edits_mtime = mtime
    return _edits_cache


def apply_edits(msg: dict, edits: list[dict]) -> Optional[dict]:
    """Apply edits in order. Returns None if deleted."""
    result = dict(msg)
    notes: list[str] = []
    edited = False

    for e in edits:
        action = e.get("action")
        if action == "delete":
            return None
        elif action == "redact":
            result["content"] = e.get("content", "[redacted]")
            edited = True
        elif action == "update":
            field = e.get("field")
            if field:
                result[field] = e.get("value", "")
                edited = True
        elif action == "note":
            notes.append(e.get("value", ""))

    if notes:
        result["_notes"] = notes
    if edited:
        result["_edited"] = True
    return result


def read_channel(channel_id: str, limit: int = 200, before: Optional[str] = None) -> list[dict]:
    """Read messages for a channel, folded with edits, newest-first.

    Args:
        limit: max messages to return
        before: if given, only return messages with id < this (for pagination)
    """
    log_file = LOG_DIR / f"{channel_id}.jsonl"
    if not log_file.exists():
        return []

    edits_by_id = load_edits()
    results: list[dict] = []

    # Read whole file — even at 10MB this is fast on local disk
    for line in log_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_id = msg.get("id")
        if not msg_id:
            continue
        if before and msg_id >= before:
            continue

        edits = edits_by_id.get(msg_id, [])
        if edits:
            folded = apply_edits(msg, edits)
            if folded is None:
                continue  # deleted
            msg = folded
        results.append(msg)

    # Newest first, limit
    results.reverse()
    return results[:limit]


def channel_message_count(channel_id: str) -> int:
    """Fast line count — not folded with edits, just raw."""
    log_file = LOG_DIR / f"{channel_id}.jsonl"
    if not log_file.exists():
        return 0
    return sum(1 for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip())


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/channels")
def api_channels():
    """List all channels with log files."""
    channels = []
    for f in sorted(LOG_DIR.glob("*.jsonl")):
        cid = f.stem
        channels.append({
            "id": cid,
            "name": CHANNEL_NAMES.get(cid, cid),
            "count": channel_message_count(cid),
        })
    return jsonify(channels)


@app.route("/api/messages/<channel_id>")
def api_messages(channel_id: str):
    limit = int(request.args.get("limit", 200))
    before = request.args.get("before") or None
    messages = read_channel(channel_id, limit=limit, before=before)
    return jsonify(messages)


@app.route("/api/search")
def api_search():
    """Search across all channels, folded with edits."""
    import re

    q = request.args.get("q", "").strip()
    channel = request.args.get("channel", "").strip()
    author = request.args.get("author", "").strip()
    limit = int(request.args.get("limit", 100))
    show_deleted = request.args.get("show_deleted") == "1"

    if not q and not author:
        return jsonify([])

    try:
        regex = re.compile(q, re.IGNORECASE) if q else None
    except re.error as e:
        return jsonify({"error": f"Invalid regex: {e}"}), 400

    edits_by_id = load_edits()
    files = sorted(LOG_DIR.glob("*.jsonl"))
    if channel:
        files = [f for f in files if f.stem == channel]

    results: list[dict] = []
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_id = msg.get("id")
            if msg_id and msg_id in edits_by_id:
                folded = apply_edits(msg, edits_by_id[msg_id])
                if folded is None:
                    if not show_deleted:
                        continue
                    msg["_deleted"] = True
                else:
                    msg = folded

            if regex and not regex.search(msg.get("content", "")):
                continue
            if author and author.lower() not in msg.get("author_name", "").lower():
                continue

            results.append(msg)

    return jsonify(results[:limit])


@app.route("/api/edits", methods=["GET", "POST"])
def api_edits():
    global _edits_mtime

    if request.method == "GET":
        if not EDITS_FILE.exists():
            return jsonify([])
        edits = []
        for line in EDITS_FILE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                edits.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return jsonify(edits)

    # POST — append new edit
    data = request.get_json(force=True, silent=True) or {}
    msg_id = data.get("msg_id")
    action = data.get("action")

    if not msg_id or action not in ("redact", "update", "delete", "note"):
        return jsonify({"error": "msg_id and valid action required"}), 400

    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "msg_id": msg_id,
        "action": action,
    }
    if action == "redact":
        entry["content"] = data.get("content", "[redacted]")
    elif action == "update":
        field = data.get("field")
        if field not in ("content", "author_name"):
            return jsonify({"error": "field must be content or author_name"}), 400
        entry["field"] = field
        entry["value"] = data.get("value", "")
    elif action == "note":
        entry["value"] = data.get("value", "")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(EDITS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    _edits_mtime = 0.0  # invalidate cache
    log.info("Recorded edit: %s on %s", action, msg_id)
    return jsonify(entry)


if __name__ == "__main__":
    log.info("Starting UI on 0.0.0.0:%d — logs=%s edits=%s", PORT, LOG_DIR, EDITS_FILE)
    app.run(host="0.0.0.0", port=PORT, debug=False)
