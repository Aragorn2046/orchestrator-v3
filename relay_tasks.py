"""Multi-CEO relay: cross-machine task dispatch for a peer network.

Provides dataclasses for relay messages (task, result, heartbeat, roster query)
and functions to send/receive them. Communication routes through a hub machine.
All paths in relay messages are machine-relative to avoid cross-platform issues.

Wraps an external relay CLI -- does not modify the transport layer.
"""

import json
import os
import platform
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ORCHESTRATED_REL = "_orchestrated"  # Relative root under ~/projects/
HUB_MACHINE = os.environ.get("HUB_MACHINE", "hub")

# Absolute path prefixes to strip from relay messages (add your own paths)
_ABSOLUTE_PREFIXES = [
    # e.g. "/home/user/projects/_orchestrated/",
]


# ---------------------------------------------------------------------------
# Message Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class RelayTaskMessage:
    """A cross-machine task dispatch message."""
    task_id: str
    from_machine: str
    to_machine: str
    department: str
    payload: dict
    callback: str  # "relay" or "outbox"
    priority: str  # "high", "normal", "low"
    budget: float

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RelayTaskMessage":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__})


@dataclass
class RelayTaskResult:
    """Cross-machine task result."""
    task_id: str
    from_machine: str
    to_machine: str
    status: str  # "completed", "failed", "partial"
    result: dict
    budget_spent: float

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RelayTaskResult":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__})


@dataclass
class CEOHeartbeat:
    """Periodic liveness signal from a CEO to the hub."""
    machine: str
    timestamp: str
    active_agents: List[str] = field(default_factory=list)
    resources: Dict = field(default_factory=dict)
    capacity: Dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CEOHeartbeat":
        return cls(**{k: data.get(k, v.default if hasattr(v, 'default') else None)
                      for k, v in cls.__dataclass_fields__.items()})


@dataclass
class CEORosterQuery:
    """Query another CEO's active agents."""
    from_machine: str
    query_type: str  # "full" or "department"
    department: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Path Handling
# ---------------------------------------------------------------------------

def get_local_home() -> str:
    """Return the local home directory."""
    return str(Path.home())


def get_orchestrated_root() -> str:
    """Return absolute path to the orchestrated root on this machine."""
    return os.environ.get(
        "ORCHESTRATED_ROOT",
        str(Path.home() / "projects" / "_orchestrated"),
    )


def resolve_relay_path(relative_path: str) -> str:
    """Convert a machine-relative path to an absolute local path.

    Input:  '_heads/atlas/outbox/result-001.json'
    Output: '/home/user/projects/_orchestrated/_heads/atlas/outbox/result-001.json'
    """
    root = get_orchestrated_root()
    # Strip leading slash if present (defensive)
    relative_path = relative_path.lstrip("/")
    return os.path.join(root, relative_path)


def strip_absolute_paths(message_dict: dict) -> dict:
    """Remove absolute path prefixes from all string values in a message dict.

    Replaces known home-based prefixes with empty string, leaving only the
    relative portion under _orchestrated/.
    """
    result = {}
    for key, value in message_dict.items():
        if isinstance(value, str):
            for prefix in _ABSOLUTE_PREFIXES:
                if prefix in value:
                    value = value.replace(prefix, "")
            result[key] = value
        elif isinstance(value, dict):
            result[key] = strip_absolute_paths(value)
        elif isinstance(value, list):
            result[key] = [
                strip_absolute_paths(item) if isinstance(item, dict)
                else item.replace(prefix, "") if isinstance(item, str) and any(p in item for p in _ABSOLUTE_PREFIXES)
                else item
                for item in value
            ]
        else:
            result[key] = value
    return result


def _strip_string(value: str) -> str:
    """Strip absolute prefixes from a single string."""
    for prefix in _ABSOLUTE_PREFIXES:
        value = value.replace(prefix, "")
    return value


# ---------------------------------------------------------------------------
# Relay Send Functions
# ---------------------------------------------------------------------------

def get_local_machine_name() -> str:
    """Determine local machine name.

    Set MACHINE env var to override hostname detection.
    Example machine names: hub, worker-1, worker-2.
    """
    machine = os.environ.get("MACHINE", "").lower()
    if machine:
        return machine
    return platform.node().lower()


def _relay_send(to_machine: str, msg_type: str, payload: dict,
                relay_cmd: Optional[str] = None) -> bool:
    """Call relay.py send with a structured message.

    Returns True if send succeeded, False on error.
    """
    if relay_cmd is None:
        relay_cmd = os.environ.get(
            "RELAY_CMD",
            os.path.expanduser("~/projects/relay/relay.py"),
        )

    message = json.dumps({
        "type": msg_type,
        "payload": payload,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    })

    try:
        result = subprocess.run(
            ["python3", relay_cmd, "send", to_machine, message],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def send_task(message: RelayTaskMessage, relay_cmd: Optional[str] = None) -> str:
    """Send a task to another machine via relay.

    If sender is not the hub, routes through the hub (hub-and-spoke).
    Returns the task_id for tracking.
    """
    local = get_local_machine_name()
    msg_dict = strip_absolute_paths(message.to_dict())

    # Hub routing: non-Day machines route through Day
    target = message.to_machine
    if local != HUB_MACHINE:
        target = HUB_MACHINE

    _relay_send(target, "task", msg_dict, relay_cmd=relay_cmd)
    return message.task_id


def send_result(result: RelayTaskResult, relay_cmd: Optional[str] = None) -> None:
    """Send a task result back to the originating CEO.

    Routes through the hub if not sending from the hub.
    """
    local = get_local_machine_name()
    msg_dict = strip_absolute_paths(result.to_dict())

    target = result.to_machine
    if local != HUB_MACHINE:
        target = HUB_MACHINE

    _relay_send(target, "task_result", msg_dict, relay_cmd=relay_cmd)


def send_heartbeat(heartbeat: CEOHeartbeat, relay_cmd: Optional[str] = None) -> None:
    """Send a CEO heartbeat to the hub.

    The hub updates its own registry directly (no relay needed).
    """
    local = get_local_machine_name()
    if local == HUB_MACHINE:
        return  # Day updates its own registry directly

    _relay_send(HUB_MACHINE, "ceo_heartbeat", heartbeat.to_dict(),
                relay_cmd=relay_cmd)


def query_roster(query: CEORosterQuery, relay_cmd: Optional[str] = None) -> List[dict]:
    """Query another CEO's active agents via relay.

    Note: This is a fire-and-forget send; actual response collection
    would happen through the inbound handler. Returns empty list as
    the response arrives asynchronously.
    """
    _relay_send(HUB_MACHINE, "ceo_roster", query.to_dict(),
                relay_cmd=relay_cmd)
    return []
