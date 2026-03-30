"""task_graph.py -- Task dependency management for Orchestrator v3.

Manages a directed acyclic graph of tasks with dependency tracking.
The CEO builds the graph, sets blocked_by relationships, and the module
enforces dependency ordering, parallel execution waves, and failure propagation.

Persisted graph: $ORCHESTRATED_ROOT/task-graph.json
"""

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from graphlib import TopologicalSorter
from typing import Dict, List, Optional


@dataclass
class TaskNode:
    """One unit of work in the task dependency graph."""

    id: str
    description: str
    department: str  # "research", "dev", "hr", "content"
    agent: Optional[str] = None  # Assigned agent name (None if unassigned)
    status: str = "pending"  # pending, in_progress, completed, failed, dependency_failed
    blocked_by: List[str] = field(default_factory=list)
    result_path: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""


VALID_STATUSES = {"pending", "in_progress", "completed", "failed", "dependency_failed"}


class TaskGraph:
    """Manages a directed acyclic graph of tasks with dependency tracking."""

    def __init__(self, nodes: Optional[Dict[str, TaskNode]] = None):
        """Initialize with optional pre-existing nodes."""
        self._nodes: Dict[str, TaskNode] = dict(nodes) if nodes else {}

    def add_task(self, node: TaskNode) -> None:
        """Add a task node to the graph. Raises ValueError if ID already exists."""
        if node.id in self._nodes:
            raise ValueError(f"Task ID already exists: {node.id}")
        now = datetime.now(timezone.utc).isoformat()
        if not node.created_at:
            node.created_at = now
        if not node.updated_at:
            node.updated_at = now
        self._nodes[node.id] = node

    def get_task(self, task_id: str) -> Optional[TaskNode]:
        """Get a task node by ID. Returns None if not found."""
        return self._nodes.get(task_id)

    def get_ready_tasks(self) -> List[str]:
        """Return task IDs whose dependencies are all completed and status is pending."""
        ready = []
        for task_id, node in self._nodes.items():
            if node.status != "pending":
                continue
            # Check all dependencies are completed
            all_deps_done = all(
                self._nodes.get(dep_id) is not None
                and self._nodes[dep_id].status == "completed"
                for dep_id in node.blocked_by
            )
            if not node.blocked_by or all_deps_done:
                ready.append(task_id)
        return ready

    def mark_completed(self, task_id: str, result_path: str) -> None:
        """Mark a task completed and set its result_path. Updates timestamp."""
        node = self._nodes.get(task_id)
        if node is None:
            raise KeyError(f"Task not found: {task_id}")
        node.status = "completed"
        node.result_path = result_path
        node.updated_at = datetime.now(timezone.utc).isoformat()

    def mark_in_progress(self, task_id: str, agent: str) -> None:
        """Mark a task as in-progress and record the assigned agent."""
        node = self._nodes.get(task_id)
        if node is None:
            raise KeyError(f"Task not found: {task_id}")
        node.status = "in_progress"
        node.agent = agent
        node.updated_at = datetime.now(timezone.utc).isoformat()

    def propagate_failure(self, task_id: str) -> List[str]:
        """Mark a task failed and transitively mark all downstream tasks as dependency_failed.

        Returns list of all task IDs that were marked dependency_failed.
        """
        node = self._nodes.get(task_id)
        if node is None:
            raise KeyError(f"Task not found: {task_id}")
        node.status = "failed"
        node.updated_at = datetime.now(timezone.utc).isoformat()

        # Build reverse dependency map: task_id -> list of tasks that depend on it
        reverse_deps: Dict[str, List[str]] = {}
        for tid, tnode in self._nodes.items():
            for dep_id in tnode.blocked_by:
                reverse_deps.setdefault(dep_id, []).append(tid)

        # BFS from failed task
        cascade_failed: List[str] = []
        queue = [task_id]
        visited = {task_id}

        while queue:
            current = queue.pop(0)
            for dependent_id in reverse_deps.get(current, []):
                if dependent_id in visited:
                    continue
                visited.add(dependent_id)
                dep_node = self._nodes.get(dependent_id)
                if dep_node is None:
                    continue
                if dep_node.status in ("completed", "failed"):
                    # Don't cascade through already-terminal nodes
                    continue
                dep_node.status = "dependency_failed"
                dep_node.updated_at = datetime.now(timezone.utc).isoformat()
                cascade_failed.append(dependent_id)
                queue.append(dependent_id)

        return cascade_failed

    def get_execution_order(self) -> List[List[str]]:
        """Return tasks grouped into parallel execution waves using TopologicalSorter.

        Each wave is a list of task IDs that can run concurrently.
        Raises CycleError if the graph contains cycles.
        """
        if not self._nodes:
            return []

        ts = TopologicalSorter()
        for task_id, node in self._nodes.items():
            ts.add(task_id, *node.blocked_by)
        ts.prepare()  # Raises CycleError if cycles exist

        waves = []
        while ts.is_active():
            ready = list(ts.get_ready())
            waves.append(ready)
            for task_id in ready:
                ts.done(task_id)
        return waves

    def save(self, path: str) -> None:
        """Persist graph to JSON file. Uses atomic write (tmp + rename)."""
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        data = {tid: asdict(node) for tid, node in self._nodes.items()}
        tmp_path = path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)

    @classmethod
    def load(cls, path: str) -> "TaskGraph":
        """Load graph from JSON file. Returns empty graph if file missing."""
        if not os.path.exists(path):
            return cls()

        with open(path) as f:
            data = json.load(f)

        nodes = {}
        for tid, node_dict in data.items():
            nodes[tid] = TaskNode(**node_dict)
        return cls(nodes=nodes)

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, task_id: str) -> bool:
        return task_id in self._nodes
