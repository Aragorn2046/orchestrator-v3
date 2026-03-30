"""Communication protocol: message dataclasses, atomic I/O, and outbox scanning.

File-based inbox/outbox IPC system for CEO-to-head (and head-to-CEO)
communication. Uses atomic write (tmp+rename+sentinel) pattern to prevent
race conditions in concurrent file access.
"""

import dataclasses
import json
import logging
import os
import signal
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Message Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TaskMessage:
    """CEO writes to head inbox."""
    task_id: str
    from_agent: str
    description: str
    context: dict
    budget: float
    priority: str
    dependencies: list
    created_at: str

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "TaskMessage":
        data = json.loads(raw)
        required = [f.name for f in dataclasses.fields(cls)
                     if f.default is dataclasses.MISSING
                     and f.default_factory is dataclasses.MISSING]
        missing = [f for f in required if f not in data]
        if missing:
            raise ValueError(f"TaskMessage missing required fields: {', '.join(missing)}")
        return cls(**{f.name: data[f.name] for f in dataclasses.fields(cls)})


@dataclass
class ResultMessage:
    """Head writes to own outbox."""
    task_id: str
    agent_id: str
    status: str
    summary: str
    output_path: Optional[str]
    files_created: list
    files_modified: list
    warnings: list
    error: Optional[str]
    budget_spent: float
    completed_at: str

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "ResultMessage":
        data = json.loads(raw)
        required = [f.name for f in dataclasses.fields(cls)
                     if f.default is dataclasses.MISSING
                     and f.default_factory is dataclasses.MISSING]
        missing = [f for f in required if f not in data]
        if missing:
            raise ValueError(f"ResultMessage missing required fields: {', '.join(missing)}")
        return cls(**{f.name: data[f.name] for f in dataclasses.fields(cls)})


@dataclass
class ProgressMessage:
    """Head writes to own outbox mid-task."""
    task_id: str
    agent_id: str
    step: str
    percent_complete: int
    details: str
    updated_at: str

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "ProgressMessage":
        data = json.loads(raw)
        required = [f.name for f in dataclasses.fields(cls)
                     if f.default is dataclasses.MISSING
                     and f.default_factory is dataclasses.MISSING]
        missing = [f for f in required if f not in data]
        if missing:
            raise ValueError(f"ProgressMessage missing required fields: {', '.join(missing)}")
        return cls(**{f.name: data[f.name] for f in dataclasses.fields(cls)})


@dataclass
class Heartbeat:
    """Written by agent-wrapper.sh, not the agent."""
    agent_id: str
    status: str
    current_task: Optional[str]
    updated_at: str

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "Heartbeat":
        data = json.loads(raw)
        required = [f.name for f in dataclasses.fields(cls)
                     if f.default is dataclasses.MISSING
                     and f.default_factory is dataclasses.MISSING]
        missing = [f for f in required if f not in data]
        if missing:
            raise ValueError(f"Heartbeat missing required fields: {', '.join(missing)}")
        return cls(**{f.name: data[f.name] for f in dataclasses.fields(cls)})


# ---------------------------------------------------------------------------
# Atomic Write / Read Helpers
# ---------------------------------------------------------------------------

def atomic_write(filepath: str, data: str, sentinel: bool = True) -> None:
    """Write data to filepath atomically using tmp+rename pattern.

    1. Write to a .tmp-<basename> tempfile in the same directory
    2. os.replace() to final path (atomic on POSIX)
    3. If sentinel=True, write a .ready sentinel file after the main file
    """
    directory = os.path.dirname(filepath)
    basename = os.path.basename(filepath)
    tmp_path = os.path.join(directory, f".tmp-{basename}")

    os.makedirs(directory, exist_ok=True)

    with open(tmp_path, "w") as f:
        f.write(data)
    os.replace(tmp_path, filepath)

    if sentinel:
        # Sentinel path: strip extension, add .ready
        name_no_ext = os.path.splitext(filepath)[0]
        sentinel_path = name_no_ext + ".ready"
        tmp_sentinel = os.path.join(directory, f".tmp-{os.path.basename(sentinel_path)}")
        with open(tmp_sentinel, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
        os.replace(tmp_sentinel, sentinel_path)


def _sentinel_path(filepath: str) -> str:
    """Get the .ready sentinel path for a given filepath."""
    name_no_ext = os.path.splitext(filepath)[0]
    return name_no_ext + ".ready"


def atomic_read(filepath: str, retries: int = 3, backoff_base: float = 0.1) -> Optional[dict]:
    """Read a JSON file written with atomic_write.

    1. Check for .ready sentinel -- skip if missing
    2. Read and parse JSON
    3. On FileNotFoundError or JSONDecodeError: retry with exponential backoff
    4. After retries exhausted: log error, return None
    """
    sentinel = _sentinel_path(filepath)
    if not os.path.exists(sentinel):
        return None

    for attempt in range(retries):
        try:
            with open(filepath) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            if attempt < retries - 1:
                time.sleep(backoff_base * (2 ** attempt))
            else:
                logger.error(
                    "Failed to read %s after %d retries: %s",
                    filepath, retries, e,
                )
    return None


# ---------------------------------------------------------------------------
# Outbox Scanning
# ---------------------------------------------------------------------------

def scan_outbox(outbox_dir: str) -> List[Tuple[str, dict]]:
    """Scan an outbox directory for result and progress files.

    Returns list of (filepath, parsed_message) tuples.
    Only returns files that have a matching .ready sentinel.
    Skips malformed JSON (logs warning).
    """
    results = []
    if not os.path.isdir(outbox_dir):
        return results

    for entry in os.scandir(outbox_dir):
        if not entry.is_file():
            continue
        name = entry.name
        # Skip temp files, sentinel files, and non-matching patterns
        if name.startswith(".tmp-"):
            continue
        if name.endswith(".ready"):
            continue
        if not name.endswith(".json"):
            continue
        if not (name.startswith("result-") or name.startswith("progress-")):
            continue

        # Check for .ready sentinel
        sentinel = _sentinel_path(entry.path)
        if not os.path.exists(sentinel):
            continue

        try:
            with open(entry.path) as f:
                data = json.load(f)
            results.append((entry.path, data))
        except json.JSONDecodeError as e:
            logger.warning("Skipping malformed JSON in %s: %s", entry.path, e)

    return results


# ---------------------------------------------------------------------------
# Archive Processed Files
# ---------------------------------------------------------------------------

def archive_processed(filepath: str, processed_dir: str) -> None:
    """Move a processed file and its sentinel to _processed/ subdirectory."""
    os.makedirs(processed_dir, exist_ok=True)
    basename = os.path.basename(filepath)
    dest = os.path.join(processed_dir, basename)
    os.replace(filepath, dest)

    sentinel = _sentinel_path(filepath)
    if os.path.exists(sentinel):
        sentinel_dest = os.path.join(processed_dir, os.path.basename(sentinel))
        os.replace(sentinel, sentinel_dest)


# ---------------------------------------------------------------------------
# Result Collector Daemon
# ---------------------------------------------------------------------------

_running = True


def _handle_sigterm(signum, frame):
    global _running
    _running = False


def collector_loop(
    heads_dir: str,
    poll_interval: float = 1.0,
    on_result=None,
    on_progress=None,
    max_cycles: int = 0,
) -> None:
    """Continuous polling loop scanning all head outbox directories.

    Args:
        heads_dir: Path to _heads/ directory containing agent workspaces.
        poll_interval: Seconds between scan cycles.
        on_result: Callback(filepath, data) for result files.
        on_progress: Callback(filepath, data) for progress files.
        max_cycles: If > 0, exit after this many cycles (for testing).
    """
    global _running
    _running = True

    signal.signal(signal.SIGTERM, _handle_sigterm)

    cycle = 0
    while _running:
        if max_cycles > 0 and cycle >= max_cycles:
            break

        if os.path.isdir(heads_dir):
            for entry in os.scandir(heads_dir):
                if not entry.is_dir():
                    continue
                outbox = os.path.join(entry.path, "outbox")
                if not os.path.isdir(outbox):
                    continue

                processed_dir = os.path.join(outbox, "_processed")
                items = scan_outbox(outbox)

                for filepath, data in items:
                    basename = os.path.basename(filepath)
                    if basename.startswith("result-"):
                        if on_result:
                            on_result(filepath, data)
                        archive_processed(filepath, processed_dir)
                    elif basename.startswith("progress-"):
                        if on_progress:
                            on_progress(filepath, data)
                        archive_processed(filepath, processed_dir)

        cycle += 1
        if _running and (max_cycles == 0 or cycle < max_cycles):
            time.sleep(poll_interval)
