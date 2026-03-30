"""CEO Scheduler: serialized cross-machine task dispatcher (runs on the hub).

Receives cross-machine task requests, queues them, and dispatches sequentially.
Maintains a CEO liveness registry based on heartbeats.
"""

import json
import logging
import os
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional

from relay_tasks import (
    RelayTaskMessage,
    CEOHeartbeat,
    _relay_send,
    strip_absolute_paths,
)

logger = logging.getLogger(__name__)

HEARTBEAT_TIMEOUT_SECONDS = 90

# Resource affinity map: resource_hint -> preferred machine
# Customize for your machines, e.g. {"gpu": "gpu-worker", "always_on": "hub"}
RESOURCE_AFFINITY = {
    "gpu": "gpu-worker",
    "always_on": "hub",
}


class CEOScheduler:
    """Serialized cross-machine task dispatcher. Runs only on the hub.

    Maintains:
    - task_queue: FIFO queue of pending cross-machine tasks
    - ceo_registry: dict mapping machine name to CEOHeartbeat data
    - dispatch_lock: threading.Lock ensuring one dispatch at a time
    """

    def __init__(self, registry_path: str, relay_queue_dir: Optional[str] = None):
        """Initialize scheduler.

        Args:
            registry_path: Path to ceo_registry.json
            relay_queue_dir: Path to relay-queue/ directory for offline tasks
        """
        self.registry_path = registry_path
        _root = os.environ.get(
            "ORCHESTRATED_ROOT",
            os.path.expanduser("~/projects/_orchestrated"),
        )
        self.relay_queue_dir = relay_queue_dir or os.path.join(_root, "relay-queue")
        self.task_queue: Deque[RelayTaskMessage] = deque()
        self.ceo_registry: Dict[str, dict] = {}
        self.dispatch_lock = threading.Lock()

        os.makedirs(os.path.dirname(registry_path), exist_ok=True)
        os.makedirs(self.relay_queue_dir, exist_ok=True)

        self._load_registry()

    def _load_registry(self) -> None:
        """Load CEO registry from disk."""
        if os.path.exists(self.registry_path):
            try:
                with open(self.registry_path) as f:
                    self.ceo_registry = json.load(f)
            except (json.JSONDecodeError, OSError):
                self.ceo_registry = {}

    def _save_registry(self) -> None:
        """Persist CEO registry to disk atomically."""
        tmp_path = self.registry_path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(self.ceo_registry, f, indent=2)
        os.replace(tmp_path, self.registry_path)

    def submit_task(self, message: RelayTaskMessage) -> int:
        """Add a task to the dispatch queue. Returns queue position (0-based)."""
        self.task_queue.append(message)
        return len(self.task_queue) - 1

    def dispatch_next(self, relay_cmd: Optional[str] = None) -> bool:
        """Dispatch the next queued task.

        Returns True if dispatched, False if queue empty or target unavailable.
        Thread-safe via dispatch_lock (serialized dispatch).
        """
        with self.dispatch_lock:
            if not self.task_queue:
                return False

            message = self.task_queue.popleft()

            # Check target CEO liveness
            status = self.get_ceo_status(message.to_machine)
            if status is None:
                # CEO is down -- queue for retry
                self._queue_for_retry(message)
                return False

            # Check budget
            capacity = status.get("capacity", {})
            remaining = capacity.get("budget_remaining", 0.0)
            if remaining <= 0 and message.budget > 0:
                # Budget exhausted -- reject
                logger.warning(
                    "Target %s budget exhausted, rejecting task %s",
                    message.to_machine, message.task_id,
                )
                return False

            # Dispatch via relay
            msg_dict = strip_absolute_paths(message.to_dict())
            success = _relay_send(
                message.to_machine, "task", msg_dict, relay_cmd=relay_cmd
            )
            if not success:
                self._queue_for_retry(message)
                return False

            return True

    def update_ceo_registry(self, heartbeat: CEOHeartbeat) -> None:
        """Update or create entry in CEO liveness registry."""
        self.ceo_registry[heartbeat.machine] = heartbeat.to_dict()
        self._save_registry()

    def get_ceo_status(self, machine: str) -> Optional[dict]:
        """Get current status of a CEO. Returns None if unknown or stale."""
        entry = self.ceo_registry.get(machine)
        if entry is None:
            return None

        # Check heartbeat freshness
        try:
            last_hb = datetime.fromisoformat(entry["timestamp"])
            # Ensure timezone-aware comparison
            if last_hb.tzinfo is None:
                last_hb = last_hb.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            age = (now - last_hb).total_seconds()
            if age > HEARTBEAT_TIMEOUT_SECONDS:
                return None
        except (KeyError, ValueError):
            return None

        return entry

    def route_by_affinity(self, department: str, resource_hint: Optional[str] = None) -> str:
        """Determine which machine should handle a task.

        Routing rules:
        - resource_hint="gpu" -> gpu-worker machine
        - resource_hint="always_on" -> hub machine
        - Otherwise: machine with most remaining budget
        """
        if resource_hint and resource_hint in RESOURCE_AFFINITY:
            target = RESOURCE_AFFINITY[resource_hint]
            # Verify target is alive
            if self.get_ceo_status(target) is not None:
                return target

        # Find machine with most remaining budget
        best_machine = os.environ.get("HUB_MACHINE", "hub")  # Default fallback
        best_budget = -1.0

        for machine_name, entry in self.ceo_registry.items():
            status = self.get_ceo_status(machine_name)
            if status is None:
                continue
            remaining = status.get("capacity", {}).get("budget_remaining", 0.0)
            if remaining > best_budget:
                best_budget = remaining
                best_machine = machine_name

        return best_machine

    def _queue_for_retry(self, message: RelayTaskMessage) -> str:
        """Write a task to the relay-queue directory for later retry."""
        os.makedirs(self.relay_queue_dir, exist_ok=True)
        filepath = os.path.join(
            self.relay_queue_dir, f"{message.task_id}.json"
        )
        tmp_path = filepath + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(message.to_dict(), f, indent=2)
        os.replace(tmp_path, filepath)

        # Write .ready sentinel
        ready_path = os.path.join(
            self.relay_queue_dir, f"{message.task_id}.ready"
        )
        with open(ready_path, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())

        return filepath
