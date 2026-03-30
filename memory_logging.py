"""Structured JSON logging and performance tracking for Orchestrator v3.

Provides:
- LogEvent dataclass for structured events
- log_event() that writes both markdown (vault) and JSON (events.jsonl)
- Performance tracking: success_rate, average_cost, agent history queries
- Log rotation and maintenance

events.jsonl path: $ORCHESTRATED_ROOT/events.jsonl
"""

import json
import logging
import os
import socket
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_EVENTS_PATH = os.path.join(
    os.environ.get("ORCHESTRATED_ROOT", os.path.expanduser("~/projects/_orchestrated")),
    "events.jsonl",
)


@dataclass
class LogEvent:
    """Structured event for JSON logging."""
    timestamp: str
    event_type: str  # spawn, progress, complete, fail, retry, dismiss, etc.
    agent_id: str
    agent_name: str
    task_id: Optional[str]
    department: str
    machine: str
    details: Dict


def log_event(
    event_type: str,
    agent_id: str = "",
    agent_name: str = "unknown",
    task_id: Optional[str] = None,
    department: str = "",
    details: Optional[Dict] = None,
    events_path: str = DEFAULT_EVENTS_PATH,
    vault_log_func=None,
) -> LogEvent:
    """Log a structured event to events.jsonl (and optionally vault markdown).

    Heartbeat events are excluded from events.jsonl.
    Returns the LogEvent that was created.
    """
    if details is None:
        details = {}

    now = datetime.now(timezone.utc)
    machine = socket.gethostname().lower()

    event = LogEvent(
        timestamp=now.isoformat(),
        event_type=event_type,
        agent_id=agent_id,
        agent_name=agent_name,
        task_id=task_id,
        department=department,
        machine=machine,
        details=details,
    )

    # Write JSON (skip heartbeats)
    if event_type != "heartbeat":
        _append_json_event(event, events_path)

    # Write vault markdown if callback provided
    if vault_log_func is not None:
        vault_log_func(event_type, agent_id, json.dumps(details)[:200])

    return event


def _append_json_event(event: LogEvent, events_path: str) -> None:
    """Append a single JSON line to events.jsonl."""
    directory = os.path.dirname(events_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    line = json.dumps(asdict(event), separators=(",", ":"))
    with open(events_path, "a") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------------------
# Performance Tracking
# ---------------------------------------------------------------------------

def load_events(events_path: str = DEFAULT_EVENTS_PATH) -> List[Dict]:
    """Read all events from events.jsonl.

    Returns list of event dicts. Skips malformed lines.
    """
    events = []
    if not os.path.exists(events_path):
        return events

    with open(events_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping malformed event line")
    return events


def compute_success_rate(agent_name: str, events: List[Dict]) -> float:
    """Ratio of 'complete' to total terminal events (complete + fail) for an agent.

    Returns 0.0 if agent has zero terminal events.
    """
    complete = 0
    fail = 0
    for e in events:
        if e.get("agent_name") != agent_name:
            continue
        et = e.get("event_type", "")
        if et == "complete":
            complete += 1
        elif et == "fail":
            fail += 1

    total = complete + fail
    if total == 0:
        return 0.0
    return complete / total


def compute_average_cost(agent_name: str, events: List[Dict]) -> float:
    """Mean budget_spent from complete/fail events for an agent.

    Returns 0.0 if agent has zero terminal events.
    """
    costs = []
    for e in events:
        if e.get("agent_name") != agent_name:
            continue
        et = e.get("event_type", "")
        if et in ("complete", "fail"):
            budget_spent = e.get("details", {}).get("budget_spent", 0.0)
            costs.append(budget_spent)

    if not costs:
        return 0.0
    return sum(costs) / len(costs)


def query_agent_history(
    agent_name: str,
    events: List[Dict],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[Dict]:
    """Filter events by agent_name and optional date range.

    Date strings should be ISO 8601 (e.g., "2026-03-29").
    """
    filtered = []
    for e in events:
        if e.get("agent_name") != agent_name:
            continue
        ts = e.get("timestamp", "")
        if start_date and ts < start_date:
            continue
        if end_date and ts > end_date:
            continue
        filtered.append(e)
    return filtered
