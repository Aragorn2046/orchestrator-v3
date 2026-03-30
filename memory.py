"""Agent memory operations: append, read, list, TTL cleanup.

Each head agent has a memory/ directory at $ORCHESTRATED_ROOT/_heads/<name>/memory/.
Memory files are append-only markdown, one file per topic (kebab-case filename).
TTL is per-file based on mtime -- frequently appended files persist longer.
"""

import glob
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_BASE_PATH = os.path.join(
    os.environ.get("ORCHESTRATED_ROOT", os.path.expanduser("~/projects/_orchestrated")),
    "_heads",
)
DEFAULT_TTL_DAYS = 30


def _to_kebab_case(text: str) -> str:
    """Convert a topic string to kebab-case filename (without extension)."""
    # Lowercase, replace non-alphanumeric with hyphens, collapse multiples
    result = re.sub(r"[^a-z0-9]+", "-", text.lower().strip())
    return result.strip("-")


def _memory_dir(agent_name: str, base_path: str = DEFAULT_BASE_PATH) -> str:
    """Get the memory directory path for an agent."""
    return os.path.join(base_path, agent_name, "memory")


def _memory_file(agent_name: str, topic: str, base_path: str = DEFAULT_BASE_PATH) -> str:
    """Get the full path to a memory file."""
    return os.path.join(_memory_dir(agent_name, base_path), f"{_to_kebab_case(topic)}.md")


def append_memory(
    agent_name: str,
    topic: str,
    title: str,
    content: str,
    sources: Optional[List[str]] = None,
    confidence: str = "Medium",
    base_path: str = DEFAULT_BASE_PATH,
) -> str:
    """Append an entry to agent's memory file. Creates file if needed.

    Returns the absolute path to the memory file.
    Topic is converted to kebab-case for the filename.
    """
    mem_dir = _memory_dir(agent_name, base_path)
    os.makedirs(mem_dir, exist_ok=True)

    filepath = _memory_file(agent_name, topic, base_path)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    entry_parts = [f"## {now} -- {title}", "", content, ""]

    if sources:
        entry_parts.append(f"Sources: {', '.join(sources)}")
    entry_parts.append(f"Confidence: {confidence}")
    entry_parts.append("")  # trailing newline

    entry = "\n".join(entry_parts)

    with open(filepath, "a") as f:
        f.write(entry)

    return os.path.abspath(filepath)


def read_memory(
    agent_name: str,
    topic: str,
    base_path: str = DEFAULT_BASE_PATH,
) -> Optional[str]:
    """Read full content of a memory file. Returns None if not found."""
    filepath = _memory_file(agent_name, topic, base_path)
    if not os.path.exists(filepath):
        return None
    with open(filepath) as f:
        return f.read()


def list_memories(
    agent_name: str,
    base_path: str = DEFAULT_BASE_PATH,
) -> List[Dict]:
    """List all memory files for an agent.

    Returns list of dicts: {topic, path, modified, size_bytes}.
    """
    mem_dir = _memory_dir(agent_name, base_path)
    if not os.path.isdir(mem_dir):
        return []

    memories = []
    for entry in os.scandir(mem_dir):
        if not entry.name.endswith(".md"):
            continue
        stat = entry.stat()
        topic = entry.name[:-3]  # strip .md
        memories.append({
            "topic": topic,
            "path": entry.path,
            "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "size_bytes": stat.st_size,
        })

    memories.sort(key=lambda m: m["topic"])
    return memories


def read_cross_department(
    reader_agent: str,
    owner_agent: str,
    topic: str,
    base_path: str = DEFAULT_BASE_PATH,
) -> Optional[str]:
    """Read another head's memory file (read-only cross-department access).

    Functionally identical to read_memory but clarifies intent.
    Write restriction is enforced by session-guard, not here.
    """
    return read_memory(owner_agent, topic, base_path)


def cleanup_expired_memories(
    ttl_days: int = DEFAULT_TTL_DAYS,
    base_path: str = DEFAULT_BASE_PATH,
) -> List[str]:
    """Scan all head memory directories, remove files with mtime > ttl_days ago.

    Returns list of removed file paths for logging.
    """
    removed = []
    cutoff = time.time() - (ttl_days * 86400)

    pattern = os.path.join(base_path, "*", "memory", "*.md")
    for filepath in glob.glob(pattern):
        try:
            if os.path.getmtime(filepath) < cutoff:
                os.remove(filepath)
                removed.append(filepath)
        except OSError as e:
            logger.warning("Failed to remove expired memory %s: %s", filepath, e)

    return removed
