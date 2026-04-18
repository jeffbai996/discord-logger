"""Web UI for discord-logger — read/edit messages + inspect bot personas.

Edits to Discord logs are append-only to state/edits.jsonl; the raw log files
are never mutated. At read time, edits are folded over the base records.

Bot persona files ARE mutated directly, atomically (write-then-rename), with
timestamped backups in state/bot_backups/ kept last 10 per bot.

Runs on http://0.0.0.0:5050. Intended for Tailscale access only.
"""

import fcntl
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
SQUAD_BACKUP_DIR = STATE_DIR / "squad_backups"
PORT = int(os.getenv("UI_PORT", "5050"))

# Squad shared context — files any bot reads on boot. Paths default to fragserv
# layout; override via SQUAD_DIR for local testing against a fixture tree.
SQUAD_DIR = Path(os.getenv("SQUAD_DIR", "~/agents/shared/squad-context"))
SQUAD_MEMORIES_DIR = SQUAD_DIR / "memories"
SQUAD_CONFIG_FILE = SQUAD_DIR / "squad-config.json"
MAX_SQUAD_FILE_BYTES = 200 * 1024
MAX_SQUAD_BACKUPS = 10
# Memory filename stem: lowercase, digits, underscore, hyphen.
MEMORY_NAME_RE = re.compile(r"^[a-z0-9_-]+$")

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
        "id": "botA",
        "label": "Fraggy",
        "description": "American energy, cl-1 primary",
        "file": "~/agents/botA/persona.md",
    },
    {
        "id": "botB",
        "label": "Claudsson",
        "description": "Norwegian philosopher, cl-3 primary",
        "file": "~/agents/botB/CLAUDE.md",
    },
    {
        "id": "claudezong",
        "label": "claude总",
        "description": "Bilingual warmth, cl-1",
        "file": "~/.claude-alt/CLAUDE.md",
    },
]
BOT_BY_ID = {b["id"]: b for b in BOTS}
MAX_BOT_FILE_BYTES = 200 * 1024  # 200 KB
MAX_BACKUPS_PER_BOT = 10

# Upper bound on `limit` query params across list endpoints. Stops a caller
# from asking for enough rows to OOM the server.
MAX_QUERY_LIMIT = 500

# Per-message cap on how much content a user regex scans. Python's `re` has no
# timeout primitive, so truncating the search surface is the cheapest defense
# against catastrophic backtracking like `(a+)+b` on a long line.
SEARCH_CONTENT_SCAN_LIMIT = 4096

# Reject obviously pathological patterns before compiling. Not exhaustive; the
# truncation above is the real defense. This catches the classic nested-
# quantifier shape `(X+)+` / `(X*)*` / mixed — where X is any single literal
# or character class — which is what makes catastrophic backtracking trivial.
_REDOS_PATTERNS = re.compile(r"\([^)]*[+*]\)[+*]")

app = Flask(__name__)
# Reject oversized request bodies at the WSGI layer before we allocate for
# them. 512KB comfortably covers a 200KB persona/memory edit plus JSON framing.
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024


def _ise(where: str, status: int = 500):
    """Log the current exception with context and return a generic JSON error.

    We don't surface `str(e)` to clients — exception strings from fs/fcntl ops
    can leak absolute paths like `~/...`. The traceback still lands
    in server logs under `log.exception`.
    """
    log.exception("%s failed", where)
    return jsonify({"error": f"{where} failed"}), status

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


# Fields that `apply_edits(action="update")` is allowed to overwrite when
# folding edits at read time. The POST handler enforces the same whitelist,
# but a hand-edit to edits.jsonl could sneak in arbitrary field names
# (including sentinels like `_edited`/`_notes`) — block them here too.
_EDITABLE_FIELDS = frozenset({"content", "author_name"})


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
            if field in _EDITABLE_FIELDS:
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


def channel_dashboard_stats(channel_id: str, hours: int = 24, buckets: int = 8) -> dict:
    """Single-pass dashboard stats: count + last-message preview + activity buckets.

    The dashboard route used to call channel_message_count, channel_last_message,
    and channel_activity in series — three full reads of the same JSONL. This
    helper folds all three into one iterator pass. The individual helpers are
    kept for other callers.
    """
    log_file = LOG_DIR / f"{channel_id}.jsonl"
    count = 0
    last_preview: Optional[dict] = None
    counts = [0] * buckets
    if not log_file.exists():
        return {"count": count, "last_message": None, "activity": counts, "size_bytes": 0}

    size_bytes = log_file.stat().st_size
    now_ts = datetime.now(timezone.utc).timestamp()
    window_start = now_ts - hours * 3600
    bucket_size = (hours * 3600) / buckets

    try:
        # Iterate the file as a line stream instead of materialising
        # splitlines() on the full text. Keeps peak memory bounded at one line
        # even when channels grow multi-MB.
        with log_file.open("r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    msg = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                count += 1
                # Newest line overwrites the preview so when we fall out of
                # the loop the last surviving msg wins.
                last_preview = msg

                ts = msg.get("timestamp", "")
                if ts:
                    try:
                        t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        t = None
                    if t is not None and t >= window_start:
                        idx = int((t - window_start) / bucket_size)
                        if idx < 0:
                            idx = 0
                        elif idx >= buckets:
                            idx = buckets - 1
                        counts[idx] += 1
    except Exception as e:
        log.warning("dashboard stats read failed for %s: %s", channel_id, e)

    last_summary: Optional[dict] = None
    if last_preview is not None:
        last_summary = {
            "author": last_preview.get("author_name", "?"),
            "content": (last_preview.get("content") or "")[:120],
            "timestamp": last_preview.get("timestamp", ""),
            "id": last_preview.get("id", ""),
        }
    return {
        "count": count,
        "last_message": last_summary,
        "activity": counts,
        "size_bytes": size_bytes,
    }


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
        stats = channel_dashboard_stats(cid, hours=24, buckets=8)
        total_log_bytes += stats["size_bytes"]
        total_msgs += stats["count"]
        cards.append({
            "id": cid,
            "name": CHANNEL_NAMES.get(cid, cid),
            "count": stats["count"],
            "size_bytes": stats["size_bytes"],
            "last_message": stats["last_message"],
            "activity": stats["activity"],
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


def _clamp_limit(raw: Optional[str], default: int) -> int:
    """Parse a ?limit= query param, clamped to [1, MAX_QUERY_LIMIT]."""
    try:
        n = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        n = default
    if n < 1:
        n = 1
    return min(n, MAX_QUERY_LIMIT)


@app.route("/api/messages/<channel_id>")
def api_messages(channel_id: str):
    limit = _clamp_limit(request.args.get("limit"), 200)
    before = request.args.get("before") or None
    return jsonify(read_channel(channel_id, limit=limit, before=before))


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    channel = request.args.get("channel", "").strip()
    author = request.args.get("author", "").strip()
    limit = _clamp_limit(request.args.get("limit"), 100)
    show_deleted = request.args.get("show_deleted") == "1"

    if not q and not author:
        return jsonify([])

    if q and _REDOS_PATTERNS.search(q):
        return jsonify({"error": "regex rejected: nested quantifier pattern"}), 400
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

            if regex:
                # Cap the substring the regex sees per message. Messages are
                # usually short; long ones get searched only in their prefix.
                content = (msg.get("content") or "")[:SEARCH_CONTENT_SCAN_LIMIT]
                if not regex.search(content):
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
    # Hold an exclusive flock on a sibling file for the duration of the append.
    # Prevents interleaved partial lines from concurrent POSTs — single-user but
    # double-click Apply is enough to race. The lock file is cheap to leave
    # around; we keep it open only for the critical section.
    lock_path = STATE_DIR / "edits.lock"
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with open(lock_path, "w") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        with open(EDITS_FILE, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    _edits_mtime = 0.0
    log.info("Recorded edit: %s on %s", action, msg_id)
    return jsonify(entry)


# ---------- Bot persona editor ----------

# ---------- Generic atomic-write / backup primitives ----------
# Used by bot-persona edits (BOT_BACKUP_DIR) and squad-context edits
# (SQUAD_BACKUP_DIR). Backup filenames are `<prefix>.<ts>.bak`, pruned to
# `max_keep` newest.

def _backup_timestamp() -> str:
    # GMT second-resolution plus a 6-digit microsecond tag avoids collisions
    # on rapid successive writes.
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + f"-{int(time.time() * 1e6) % 1_000_000:06d}"


def _atomic_write(target: Path, encoded: bytes) -> None:
    """Write bytes to `target` atomically via tmp-in-same-dir + os.replace.

    Callers must have already validated size / path. Raises on any failure and
    cleans up the tmp file.
    """
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_bytes(encoded)
        os.replace(tmp, target)
    except Exception:
        if tmp.exists():
            try: tmp.unlink()
            except Exception: pass
        raise


def _backup_file(target: Path, backup_dir: Path, prefix: str) -> Optional[Path]:
    """Copy `target` into `backup_dir` as `<prefix>.<ts>.bak`. No-op if target missing."""
    if not target.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{prefix}.{_backup_timestamp()}.bak"
    backup_path.write_bytes(target.read_bytes())
    return backup_path


def _prune_backups_for(backup_dir: Path, prefix: str, max_keep: int) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(backup_dir.glob(f"{prefix}.*.bak"), reverse=True)
    for old in files[max_keep:]:
        try:
            old.unlink()
        except Exception as e:
            log.warning("prune backup %s: %s", old, e)


def _list_backups_for(backup_dir: Path, prefix: str) -> list[dict]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(backup_dir.glob(f"{prefix}.*.bak"), reverse=True)
    return [
        {"name": f.name, "bytes": f.stat().st_size, "ts": f.stat().st_mtime}
        for f in files
    ]


def _validate_backup_name(name: str, prefix: str) -> bool:
    """Guard restore inputs against path traversal and prefix spoofing."""
    return (
        name.startswith(f"{prefix}.")
        and name.endswith(".bak")
        and "/" not in name
        and ".." not in name
    )


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


@app.route("/api/bots/<bot_id>/backups")
def api_bot_backups(bot_id: str):
    if bot_id not in BOT_BY_ID:
        return jsonify({"error": "unknown bot"}), 404
    return jsonify(_list_backups_for(BOT_BACKUP_DIR, bot_id))


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

    try:
        backup_path = _backup_file(target, BOT_BACKUP_DIR, bot_id)
    except Exception as e:
        return _ise("backup")

    try:
        _atomic_write(target, encoded)
    except Exception as e:
        return _ise("write")

    _prune_backups_for(BOT_BACKUP_DIR, bot_id, MAX_BACKUPS_PER_BOT)
    backup_name = backup_path.name if backup_path else None
    log.info("Wrote bot file: %s (%d bytes, backup=%s)", target, len(encoded), backup_name)
    return jsonify({
        "ok": True,
        "bytes": len(encoded),
        "backup": backup_name,
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
    if not _validate_backup_name(name, bot_id):
        return jsonify({"error": "invalid backup name"}), 400

    backup_path = BOT_BACKUP_DIR / name
    if not backup_path.exists():
        return jsonify({"error": "backup not found"}), 404

    target = Path(bot["file"])
    try:
        pre_backup = _backup_file(target, BOT_BACKUP_DIR, bot_id)
        _atomic_write(target, backup_path.read_bytes())
    except Exception as e:
        return _ise("restore")

    _prune_backups_for(BOT_BACKUP_DIR, bot_id, MAX_BACKUPS_PER_BOT)
    log.info("Restored %s from %s", target, name)
    return jsonify({"ok": True, "pre_backup": pre_backup.name if pre_backup else None})


# ---------- Squad shared context ----------
# Two kinds of entries:
#   - squad-config:   fixed JSON file, schema-validated on write
#   - mem:<stem>:     markdown files in SQUAD_MEMORIES_DIR, free-form
# Backups share state/squad_backups/, prefixed `squad-config` or `mem-<stem>`,
# pruned to MAX_SQUAD_BACKUPS per prefix.
#
# Soft-delete renames a memory to `<name>.md.deleted-<ts>`; bots glob `*.md`
# so the file stops being read but stays on disk and can be un-renamed.

def _memory_backup_prefix(stem: str) -> str:
    return f"mem-{stem}"


def _squad_entry_meta(entry_id: str, path: Path, label: str, kind: str) -> dict:
    exists = path.exists()
    bytes_size = 0
    lines = 0
    last_mod = None
    content = None
    if exists:
        try:
            stat = path.stat()
            bytes_size = stat.st_size
            last_mod = stat.st_mtime
            if bytes_size <= MAX_SQUAD_FILE_BYTES:
                content = path.read_text(encoding="utf-8")
                lines = content.count("\n") + (0 if content.endswith("\n") else 1)
        except Exception as e:
            log.warning("Failed to read squad file %s: %s", path, e)
    return {
        "id": entry_id,
        "label": label,
        "kind": kind,
        "file": str(path),
        "exists": exists,
        "bytes": bytes_size,
        "lines": lines,
        "last_mod": last_mod,
        "content": content,
        "too_large": bytes_size > MAX_SQUAD_FILE_BYTES,
    }


def _list_memory_entries() -> list[dict]:
    """Enumerate live memory files (excludes *.md.deleted-* soft-deletes)."""
    if not SQUAD_MEMORIES_DIR.exists():
        return []
    entries = []
    for p in sorted(SQUAD_MEMORIES_DIR.glob("*.md")):
        stem = p.stem  # filename without .md
        entries.append(_squad_entry_meta(f"mem:{stem}", p, stem, "markdown"))
    return entries


def _resolve_squad_entry(entry_id: str) -> Optional[tuple[Path, str, str, str]]:
    """Return (path, label, kind, backup_prefix) for a squad entry_id, or None."""
    if entry_id == "squad-config":
        return (SQUAD_CONFIG_FILE, "squad-config.json", "json", "squad-config")
    if entry_id.startswith("mem:"):
        stem = entry_id[4:]
        if not MEMORY_NAME_RE.match(stem):
            return None
        path = SQUAD_MEMORIES_DIR / f"{stem}.md"
        return (path, stem, "markdown", _memory_backup_prefix(stem))
    return None


@app.route("/api/squad")
def api_squad():
    """List squad-context editable entries: squad-config + all live memories."""
    entries = []
    cfg = _squad_entry_meta("squad-config", SQUAD_CONFIG_FILE, "squad-config.json", "json")
    cfg.pop("content", None)
    entries.append(cfg)
    for m in _list_memory_entries():
        m.pop("content", None)
        entries.append(m)
    return jsonify({
        "squad_dir": str(SQUAD_DIR),
        "memories_dir": str(SQUAD_MEMORIES_DIR),
        "entries": entries,
    })


@app.route("/api/squad/<path:entry_id>")
def api_squad_detail(entry_id: str):
    resolved = _resolve_squad_entry(entry_id)
    if not resolved:
        return jsonify({"error": "unknown entry"}), 404
    path, label, kind, _prefix = resolved
    return jsonify(_squad_entry_meta(entry_id, path, label, kind))


@app.route("/api/squad/<path:entry_id>/backups")
def api_squad_backups(entry_id: str):
    resolved = _resolve_squad_entry(entry_id)
    if not resolved:
        return jsonify({"error": "unknown entry"}), 404
    _path, _label, _kind, prefix = resolved
    return jsonify(_list_backups_for(SQUAD_BACKUP_DIR, prefix))


@app.route("/api/squad/<path:entry_id>/file", methods=["POST"])
def api_squad_write(entry_id: str):
    """Overwrite a squad-context file atomically, with a backup.

    For kind=json, content is parsed via json.loads before write; parse errors
    return 400 with the parser's message so the UI can surface it.
    """
    resolved = _resolve_squad_entry(entry_id)
    if not resolved:
        return jsonify({"error": "unknown entry"}), 404
    target, _label, kind, prefix = resolved

    data = request.get_json(force=True, silent=True) or {}
    content = data.get("content")
    if not isinstance(content, str):
        return jsonify({"error": "content (string) required"}), 400
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_SQUAD_FILE_BYTES:
        return jsonify({
            "error": f"content too large ({len(encoded)} > {MAX_SQUAD_FILE_BYTES} bytes)",
        }), 400

    if kind == "json":
        try:
            json.loads(content)
        except json.JSONDecodeError as e:
            return jsonify({"error": f"invalid JSON: {e}"}), 400

    if not target.parent.exists():
        return jsonify({"error": "target directory does not exist"}), 500

    try:
        backup_path = _backup_file(target, SQUAD_BACKUP_DIR, prefix)
    except Exception as e:
        return _ise("backup")

    try:
        _atomic_write(target, encoded)
    except Exception as e:
        return _ise("write")

    _prune_backups_for(SQUAD_BACKUP_DIR, prefix, MAX_SQUAD_BACKUPS)
    backup_name = backup_path.name if backup_path else None
    log.info("Wrote squad file: %s (%d bytes, backup=%s)", target, len(encoded), backup_name)
    return jsonify({
        "ok": True,
        "bytes": len(encoded),
        "backup": backup_name,
        "last_mod": target.stat().st_mtime,
    })


@app.route("/api/squad/<path:entry_id>/restore", methods=["POST"])
def api_squad_restore(entry_id: str):
    resolved = _resolve_squad_entry(entry_id)
    if not resolved:
        return jsonify({"error": "unknown entry"}), 404
    target, _label, _kind, prefix = resolved

    data = request.get_json(force=True, silent=True) or {}
    name = data.get("name", "")
    if not _validate_backup_name(name, prefix):
        return jsonify({"error": "invalid backup name"}), 400

    backup_path = SQUAD_BACKUP_DIR / name
    if not backup_path.exists():
        return jsonify({"error": "backup not found"}), 404

    try:
        pre_backup = _backup_file(target, SQUAD_BACKUP_DIR, prefix)
        _atomic_write(target, backup_path.read_bytes())
    except Exception as e:
        return _ise("restore")

    _prune_backups_for(SQUAD_BACKUP_DIR, prefix, MAX_SQUAD_BACKUPS)
    log.info("Restored squad %s from %s", target, name)
    return jsonify({"ok": True, "pre_backup": pre_backup.name if pre_backup else None})


@app.route("/api/squad/memories", methods=["POST"])
def api_squad_memory_create():
    """Create a new memory file in SQUAD_MEMORIES_DIR.

    Body: {name: "<stem>", content?: "<markdown>"} — stem must match
    MEMORY_NAME_RE, must not already exist. `.md` is appended automatically.
    """
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    content = data.get("content", "")
    if not isinstance(content, str):
        return jsonify({"error": "content (string) required"}), 400
    if not MEMORY_NAME_RE.match(name):
        return jsonify({
            "error": "name must match [a-z0-9_-]+ (no extension, no path separators)",
        }), 400

    encoded = content.encode("utf-8")
    if len(encoded) > MAX_SQUAD_FILE_BYTES:
        return jsonify({
            "error": f"content too large ({len(encoded)} > {MAX_SQUAD_FILE_BYTES} bytes)",
        }), 400

    SQUAD_MEMORIES_DIR.mkdir(parents=True, exist_ok=True)
    target = SQUAD_MEMORIES_DIR / f"{name}.md"
    if target.exists():
        return jsonify({"error": "memory already exists"}), 409

    try:
        _atomic_write(target, encoded)
    except Exception as e:
        return _ise("write")

    log.info("Created memory: %s (%d bytes)", target, len(encoded))
    return jsonify({
        "ok": True,
        "id": f"mem:{name}",
        "file": str(target),
        "bytes": len(encoded),
        "last_mod": target.stat().st_mtime,
    })


@app.route("/api/squad/memories/<stem>/delete", methods=["POST"])
def api_squad_memory_soft_delete(stem: str):
    """Soft-delete a memory by renaming to `<name>.md.deleted-<ts>`.

    The file stays on disk (bots glob `*.md` so it's out of rotation) and can
    be restored by renaming back. No backup is taken — the file itself is the
    archive.
    """
    if not MEMORY_NAME_RE.match(stem):
        return jsonify({"error": "invalid memory name"}), 400
    target = SQUAD_MEMORIES_DIR / f"{stem}.md"
    if not target.exists():
        return jsonify({"error": "memory not found"}), 404

    deleted_name = f"{stem}.md.deleted-{_backup_timestamp()}"
    deleted_path = SQUAD_MEMORIES_DIR / deleted_name
    try:
        target.rename(deleted_path)
    except Exception as e:
        return _ise("delete")

    log.info("Soft-deleted memory: %s -> %s", target, deleted_path)
    return jsonify({"ok": True, "renamed_to": deleted_name})


def _prune_soft_deleted_memories(max_age_days: int = 30) -> int:
    """Permanently delete `<name>.md.deleted-<ts>` files older than `max_age_days`.

    Soft-delete never auto-cleans, so these accumulate every time the user
    removes a memory from the UI. We run this on startup — cheap directory
    scan, and the unit is days so it's not time-sensitive.
    """
    if not SQUAD_MEMORIES_DIR.exists():
        return 0
    cutoff = time.time() - max_age_days * 86400
    pruned = 0
    for p in SQUAD_MEMORIES_DIR.glob("*.md.deleted-*"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                pruned += 1
        except Exception as e:
            log.warning("prune soft-deleted %s: %s", p, e)
    if pruned:
        log.info("Pruned %d soft-deleted memories older than %d days", pruned, max_age_days)
    return pruned


if __name__ == "__main__":
    _prune_soft_deleted_memories()
    log.info("Starting UI on 0.0.0.0:%d — logs=%s edits=%s", PORT, LOG_DIR, EDITS_FILE)
    app.run(host="0.0.0.0", port=PORT, debug=False)
