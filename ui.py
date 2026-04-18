"""Web UI for discord-logger — read/edit messages + inspect bot personas.

Edits to Discord logs are append-only to state/edits.jsonl; the raw log files
are never mutated. At read time, edits are folded over the base records.

Bot persona files ARE mutated directly, atomically (write-then-rename), with
timestamped backups in state/bot_backups/ kept last 10 per bot.

Runs on http://0.0.0.0:5050. Intended for Tailscale access only.
"""

import json
import logging
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
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
BOT_BACKUP_DIR = STATE_DIR / "bot_backups"
PORT = int(os.getenv("UI_PORT", "5050"))

# Human-readable channel names — keep in sync with summarise_channels.py
CHANNEL_NAMES = {
    "1491314655224795147": "cl-2",
    "1491337341619671111": "cl-1",
    "1492197457369497822": "cl-3",
    "1491352842886447214": "private",
    "1491337758709383279": "claude-channel",
    "1494449115793457153": "gem",
}

# Display order matches Discord's sidebar order
CHANNEL_ORDER = [
    "1491337341619671111",  # cl-1
    "1491314655224795147",  # cl-2
    "1492197457369497822",  # cl-3
    "1494449115793457153",  # gem
    "1491352842886447214",  # private
    "1491337758709383279",  # claude-channel (if present)
]

# Bots whose persona files are editable from this UI
BOTS = [
    {
        "id": "fraggy",
        "label": "Fraggy",
        "description": "American energy, cl-1 primary",
        "file": "/home/jbai/claude-agents/fraggy/persona.md",
    },
    {
        "id": "claudsson",
        "label": "Claudsson",
        "description": "Norwegian philosopher, cl-3 primary",
        "file": "/home/jbai/claude-agents/claudsson/CLAUDE.md",
    },
    {
        "id": "claudezong",
        "label": "claude总",
        "description": "Bilingual warmth, cl-1",
        "file": "/home/jbai/.claude-alt/CLAUDE.md",
    },
]
BOT_BY_ID = {b["id"]: b for b in BOTS}
MAX_BOT_FILE_BYTES = 200 * 1024  # 200 KB
MAX_BACKUPS_PER_BOT = 10

app = Flask(__name__)

# Simple mtime-based cache for edits. Reloaded on disk change or POST.
_edits_cache: dict[str, list[dict]] = {}
_edits_mtime: float = 0.0


def load_edits() -> dict[str, list[dict]]:
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
    log_file = LOG_DIR / f"{channel_id}.jsonl"
    if not log_file.exists():
        return []

    edits_by_id = load_edits()
    results: list[dict] = []

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

    results.reverse()
    return results[:limit]


def channel_message_count(channel_id: str) -> int:
    log_file = LOG_DIR / f"{channel_id}.jsonl"
    if not log_file.exists():
        return 0
    return sum(1 for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip())


def ordered_channels() -> list[str]:
    """Return channel IDs that have log files, in CHANNEL_ORDER first, unknowns last."""
    on_disk = {f.stem for f in LOG_DIR.glob("*.jsonl")}
    ordered = [cid for cid in CHANNEL_ORDER if cid in on_disk]
    unknowns = sorted(on_disk - set(CHANNEL_ORDER))
    return ordered + unknowns


def channel_last_message(channel_id: str) -> Optional[dict]:
    """Get the last message (newest) as a preview summary."""
    log_file = LOG_DIR / f"{channel_id}.jsonl"
    if not log_file.exists():
        return None
    # Read lines backward to find the last non-empty JSON line
    try:
        lines = log_file.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
            return {
                "author": msg.get("author_name", "?"),
                "content": (msg.get("content") or "")[:120],
                "timestamp": msg.get("timestamp", ""),
                "id": msg.get("id", ""),
            }
        except json.JSONDecodeError:
            continue
    return None


def channel_activity(channel_id: str, hours: int = 24, buckets: int = 8) -> list[int]:
    """Counts messages per bucket over the last `hours` hours, oldest-first."""
    log_file = LOG_DIR / f"{channel_id}.jsonl"
    if not log_file.exists():
        return [0] * buckets

    now = datetime.now(timezone.utc)
    window_start = now.timestamp() - hours * 3600
    bucket_size = (hours * 3600) / buckets
    counts = [0] * buckets

    # Parse only what's needed — iterate lines, grab timestamp
    try:
        text = log_file.read_text(encoding="utf-8")
    except Exception:
        return counts
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = msg.get("timestamp", "")
        if not ts:
            continue
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if t < window_start:
            continue
        idx = int((t - window_start) / bucket_size)
        if idx < 0:
            idx = 0
        if idx >= buckets:
            idx = buckets - 1
        counts[idx] += 1
    return counts


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/channels")
def api_channels():
    channels = []
    for cid in ordered_channels():
        channels.append({
            "id": cid,
            "name": CHANNEL_NAMES.get(cid, cid),
            "count": channel_message_count(cid),
        })
    return jsonify(channels)


@app.route("/api/dashboard")
def api_dashboard():
    """Combined dashboard payload: channels with size, last message, activity."""
    cards = []
    total_log_bytes = 0
    total_msgs = 0
    for cid in ordered_channels():
        log_file = LOG_DIR / f"{cid}.jsonl"
        size = log_file.stat().st_size if log_file.exists() else 0
        count = channel_message_count(cid)
        total_log_bytes += size
        total_msgs += count
        cards.append({
            "id": cid,
            "name": CHANNEL_NAMES.get(cid, cid),
            "count": count,
            "size_bytes": size,
            "last_message": channel_last_message(cid),
            "activity": channel_activity(cid, hours=24, buckets=8),
        })

    edits_bytes = EDITS_FILE.stat().st_size if EDITS_FILE.exists() else 0
    edits_count = 0
    if EDITS_FILE.exists():
        edits_count = sum(1 for line in EDITS_FILE.read_text(encoding="utf-8").splitlines() if line.strip())

    return jsonify({
        "cards": cards,
        "totals": {
            "channels": len(cards),
            "messages": total_msgs,
            "log_bytes": total_log_bytes,
            "edits_bytes": edits_bytes,
            "edits_count": edits_count,
        },
    })


@app.route("/api/messages/<channel_id>")
def api_messages(channel_id: str):
    limit = int(request.args.get("limit", 200))
    before = request.args.get("before") or None
    return jsonify(read_channel(channel_id, limit=limit, before=before))


@app.route("/api/search")
def api_search():
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
    files = [LOG_DIR / f"{cid}.jsonl" for cid in ordered_channels()]
    if channel:
        files = [f for f in files if f.stem == channel]

    results: list[dict] = []
    for f in files:
        if not f.exists():
            continue
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

    _edits_mtime = 0.0
    log.info("Recorded edit: %s on %s", action, msg_id)
    return jsonify(entry)


# ---------- Bot persona editor ----------

def _bot_meta(bot: dict) -> dict:
    """Return live file metadata for a bot."""
    p = Path(bot["file"])
    exists = p.exists()
    stat = p.stat() if exists else None
    content = None
    bytes_size = 0
    lines = 0
    last_mod = None
    if exists:
        try:
            bytes_size = stat.st_size
            last_mod = stat.st_mtime
            # line count without full read if file is huge — small in practice
            if bytes_size <= MAX_BOT_FILE_BYTES:
                content = p.read_text(encoding="utf-8")
                lines = content.count("\n") + (0 if content.endswith("\n") else 1)
        except Exception as e:
            log.warning("Failed to read bot file %s: %s", p, e)
            content = None
    return {
        "id": bot["id"],
        "label": bot["label"],
        "description": bot["description"],
        "file": bot["file"],
        "exists": exists,
        "bytes": bytes_size,
        "lines": lines,
        "last_mod": last_mod,
        "content": content,
        "too_large": bytes_size > MAX_BOT_FILE_BYTES,
    }


@app.route("/api/bots")
def api_bots():
    """List all editable bots with metadata (no content)."""
    out = []
    for b in BOTS:
        m = _bot_meta(b)
        m.pop("content", None)  # summary view — no content
        out.append(m)
    return jsonify(out)


@app.route("/api/bots/<bot_id>")
def api_bot_detail(bot_id: str):
    """Full bot metadata including file content."""
    bot = BOT_BY_ID.get(bot_id)
    if not bot:
        return jsonify({"error": "unknown bot"}), 404
    return jsonify(_bot_meta(bot))


def _list_backups(bot_id: str) -> list[dict]:
    BOT_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    pattern = f"{bot_id}.*.bak"
    files = sorted(BOT_BACKUP_DIR.glob(pattern), reverse=True)
    return [
        {
            "name": f.name,
            "bytes": f.stat().st_size,
            "ts": f.stat().st_mtime,
        }
        for f in files
    ]


def _prune_backups(bot_id: str) -> None:
    files = sorted(BOT_BACKUP_DIR.glob(f"{bot_id}.*.bak"), reverse=True)
    for old in files[MAX_BACKUPS_PER_BOT:]:
        try:
            old.unlink()
        except Exception as e:
            log.warning("prune backup %s: %s", old, e)


@app.route("/api/bots/<bot_id>/backups")
def api_bot_backups(bot_id: str):
    if bot_id not in BOT_BY_ID:
        return jsonify({"error": "unknown bot"}), 404
    return jsonify(_list_backups(bot_id))


@app.route("/api/bots/<bot_id>/file", methods=["POST"])
def api_bot_write(bot_id: str):
    """Overwrite a bot's persona file atomically, with a backup."""
    bot = BOT_BY_ID.get(bot_id)
    if not bot:
        return jsonify({"error": "unknown bot"}), 404

    data = request.get_json(force=True, silent=True) or {}
    content = data.get("content")
    if not isinstance(content, str):
        return jsonify({"error": "content (string) required"}), 400
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_BOT_FILE_BYTES:
        return jsonify({
            "error": f"content too large ({len(encoded)} > {MAX_BOT_FILE_BYTES} bytes)",
        }), 400

    target = Path(bot["file"])
    if not target.parent.exists():
        return jsonify({"error": "target directory does not exist"}), 500

    # Backup existing file (if any) to state/bot_backups/<bot_id>.<ts>.bak
    BOT_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts_tag = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + f"-{int(time.time() * 1e6) % 1_000_000:06d}"
    backup_name = BOT_BACKUP_DIR / f"{bot_id}.{ts_tag}.bak"
    if target.exists():
        try:
            backup_name.write_bytes(target.read_bytes())
        except Exception as e:
            return jsonify({"error": f"backup failed: {e}"}), 500

    # Atomic write — write to tmp in same dir, then rename on top
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_bytes(encoded)
        os.replace(tmp, target)
    except Exception as e:
        if tmp.exists():
            try: tmp.unlink()
            except Exception: pass
        return jsonify({"error": f"write failed: {e}"}), 500

    _prune_backups(bot_id)
    log.info("Wrote bot file: %s (%d bytes, backup=%s)", target, len(encoded), backup_name.name)
    return jsonify({
        "ok": True,
        "bytes": len(encoded),
        "backup": backup_name.name,
        "last_mod": target.stat().st_mtime,
    })


@app.route("/api/bots/<bot_id>/restore", methods=["POST"])
def api_bot_restore(bot_id: str):
    """Restore a named backup to become the current persona file."""
    bot = BOT_BY_ID.get(bot_id)
    if not bot:
        return jsonify({"error": "unknown bot"}), 404

    data = request.get_json(force=True, silent=True) or {}
    name = data.get("name", "")
    # Validate backup name belongs to this bot and no path traversal
    if not name.startswith(f"{bot_id}.") or not name.endswith(".bak") or "/" in name or ".." in name:
        return jsonify({"error": "invalid backup name"}), 400

    backup_path = BOT_BACKUP_DIR / name
    if not backup_path.exists():
        return jsonify({"error": "backup not found"}), 404

    target = Path(bot["file"])
    # Snapshot current as a pre-restore backup so restore itself is reversible
    ts_tag = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + f"-{int(time.time() * 1e6) % 1_000_000:06d}"
    pre_backup = BOT_BACKUP_DIR / f"{bot_id}.{ts_tag}.bak"
    try:
        if target.exists():
            pre_backup.write_bytes(target.read_bytes())
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(backup_path.read_bytes())
        os.replace(tmp, target)
    except Exception as e:
        return jsonify({"error": f"restore failed: {e}"}), 500

    _prune_backups(bot_id)
    log.info("Restored %s from %s", target, name)
    return jsonify({"ok": True, "pre_backup": pre_backup.name})


if __name__ == "__main__":
    log.info("Starting UI on 0.0.0.0:%d — logs=%s edits=%s", PORT, LOG_DIR, EDITS_FILE)
    app.run(host="0.0.0.0", port=PORT, debug=False)
