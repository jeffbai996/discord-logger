"""Search through Discord message logs."""

import json
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def search_logs(
    log_dir: str = "./logs",
    pattern: str = "",
    channel_id: str = "",
    author: str = "",
    limit: int = 20,
) -> list[dict]:
    """Search JSONL log files for messages matching criteria.

    Args:
        log_dir: Directory containing .jsonl log files
        pattern: Regex pattern to match against message content
        channel_id: Filter to specific channel (empty = all)
        author: Filter by author username (substring match)
        limit: Max results to return
    """
    log_path = Path(log_dir)
    results: list[dict] = []
    regex = re.compile(pattern, re.IGNORECASE) if pattern else None

    files = sorted(log_path.glob("*.jsonl"))
    if channel_id:
        files = [f for f in files if f.stem == channel_id]

    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            if regex and not regex.search(msg.get("content", "")):
                continue
            if author and author.lower() not in msg.get("author_name", "").lower():
                continue

            results.append(msg)
            if len(results) >= limit:
                return results

    return results


def format_message(msg: dict) -> str:
    """Format a message for display."""
    ts = msg.get("timestamp", "?")[:19]
    author = msg.get("author_name", "?")
    content = msg.get("content", "")
    att = f" [+{len(msg['attachments'])} att]" if msg.get("attachments") else ""
    return f"[{ts}] {author}: {content}{att}"


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Search Discord message logs")
    parser.add_argument("pattern", nargs="?", default="", help="Search pattern (regex)")
    parser.add_argument("--channel", "-c", default="", help="Channel ID to search")
    parser.add_argument("--author", "-a", default="", help="Filter by author username")
    parser.add_argument("--limit", "-n", type=int, default=20, help="Max results")
    parser.add_argument("--log-dir", "-d", default="./logs", help="Log directory")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    if not args.pattern and not args.author:
        print("Provide a search pattern or --author filter", file=sys.stderr)
        sys.exit(1)

    results = search_logs(
        log_dir=args.log_dir,
        pattern=args.pattern,
        channel_id=args.channel,
        author=args.author,
        limit=args.limit,
    )

    if not results:
        print("No matches found.")
        return

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for msg in results:
            print(format_message(msg))


if __name__ == "__main__":
    main()
