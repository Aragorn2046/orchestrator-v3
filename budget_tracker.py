"""Hierarchical budget tracker for Orchestrator v3.

Tracks budget at five levels: session, department, agent, task, and retry.
Provides guardrails (daily ceilings, proposal thresholds, retry caps)
and the BATS prompt injection pattern for cost-aware agents.
"""

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional


PROPOSAL_THRESHOLD = 2.0  # Tasks above this require CEO approval
DEFAULT_SESSION_CAP = 15.0
DEFAULT_DEPARTMENT_CAP = 5.0
DEFAULT_DAILY_CEILING = 25.0


class BudgetExhaustedError(Exception):
    """Department, session, or agent budget is exhausted."""


@dataclass
class TaskBudget:
    task_id: str
    agent_id: str
    department: str
    allocated: float
    spent: float
    status: str  # "running", "completed", "failed"
    retry_count: int
    original_budget: float
    allocated_at: str
    completed_at: Optional[str] = None


@dataclass
class AgentBudget:
    agent_id: str
    agent_name: str
    department: str
    daily_ceiling: float
    spent_today: float
    total_spent: float
    tasks: List[str] = field(default_factory=list)


@dataclass
class DepartmentBudget:
    name: str
    daily_cap: float
    allocated: float
    spent: float
    agents: List[str] = field(default_factory=list)


@dataclass
class BudgetState:
    session_cap: float = DEFAULT_SESSION_CAP
    total_allocated: float = 0.0
    total_spent: float = 0.0
    departments: Dict[str, DepartmentBudget] = field(default_factory=dict)
    agents: Dict[str, AgentBudget] = field(default_factory=dict)
    tasks: Dict[str, TaskBudget] = field(default_factory=dict)
    created_at: str = ""


class BudgetTracker:
    """Hierarchical budget tracker with guardrails."""

    def __init__(self, state_dir: str):
        self.state_dir = state_dir
        self.state_file = os.path.join(state_dir, "budget.json")
        os.makedirs(state_dir, exist_ok=True)
        self._state = self._load_or_create()

    def _load_or_create(self) -> BudgetState:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file) as f:
                    data = json.load(f)
                return self._deserialize(data)
            except (json.JSONDecodeError, TypeError, KeyError):
                pass
        state = BudgetState(
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._save(state)
        return state

    def _deserialize(self, data: dict) -> BudgetState:
        state = BudgetState(
            session_cap=data.get("session_cap", DEFAULT_SESSION_CAP),
            total_allocated=data.get("total_allocated", 0.0),
            total_spent=data.get("total_spent", 0.0),
            created_at=data.get("created_at", ""),
        )
        for name, dept_data in data.get("departments", {}).items():
            state.departments[name] = DepartmentBudget(**dept_data)
        for aid, agent_data in data.get("agents", {}).items():
            state.agents[aid] = AgentBudget(**agent_data)
        for tid, task_data in data.get("tasks", {}).items():
            state.tasks[tid] = TaskBudget(**task_data)
        return state

    def _save(self, state: Optional[BudgetState] = None) -> None:
        if state is None:
            state = self._state
        data = {
            "session_cap": state.session_cap,
            "total_allocated": state.total_allocated,
            "total_spent": state.total_spent,
            "created_at": state.created_at,
            "departments": {k: asdict(v) for k, v in state.departments.items()},
            "agents": {k: asdict(v) for k, v in state.agents.items()},
            "tasks": {k: asdict(v) for k, v in state.tasks.items()},
        }
        tmp_path = self.state_file + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
        os.rename(tmp_path, self.state_file)

    def get_state(self) -> BudgetState:
        return self._state

    def set_session_cap(self, cap: float) -> None:
        self._state.session_cap = cap
        self._save()

    def set_department_cap(self, department: str, cap: float) -> None:
        if department not in self._state.departments:
            self._state.departments[department] = DepartmentBudget(
                name=department, daily_cap=cap, allocated=0.0, spent=0.0
            )
        else:
            self._state.departments[department].daily_cap = cap
        self._save()

    def register_agent(
        self,
        agent_id: str,
        agent_name: str,
        department: str,
        daily_ceiling: float = DEFAULT_DAILY_CEILING,
    ) -> None:
        """Register an agent in the budget system."""
        # Ensure department exists
        if department not in self._state.departments:
            self._state.departments[department] = DepartmentBudget(
                name=department,
                daily_cap=DEFAULT_DEPARTMENT_CAP,
                allocated=0.0,
                spent=0.0,
            )

        dept = self._state.departments[department]
        if agent_id not in dept.agents:
            dept.agents.append(agent_id)

        if agent_id in self._state.agents:
            # Update ceiling without resetting spend history
            self._state.agents[agent_id].daily_ceiling = daily_ceiling
        else:
            self._state.agents[agent_id] = AgentBudget(
                agent_id=agent_id,
                agent_name=agent_name,
                department=department,
                daily_ceiling=daily_ceiling,
                spent_today=0.0,
                total_spent=0.0,
            )
        self._save()

    def allocate(
        self,
        task_id: str,
        agent_id: str,
        department: str,
        amount: float,
        retry_of: Optional[str] = None,
    ) -> dict:
        """Allocate budget for a task. Returns dict with allocation details."""
        if amount <= 0:
            raise ValueError(f"Allocation amount must be positive, got ${amount:.2f}")

        state = self._state

        # Retry cap: can't exceed original task budget
        original_budget = amount
        if retry_of and retry_of in state.tasks:
            original_budget = state.tasks[retry_of].original_budget
            if amount > original_budget:
                amount = original_budget

        # Check agent daily ceiling
        if agent_id in state.agents:
            agent = state.agents[agent_id]
            if agent.spent_today + amount > agent.daily_ceiling:
                raise BudgetExhaustedError(
                    f"Agent '{agent_id}' daily ceiling exhausted "
                    f"(${agent.spent_today:.2f} / ${agent.daily_ceiling:.2f})"
                )

        # Check department cap
        if department not in state.departments:
            state.departments[department] = DepartmentBudget(
                name=department,
                daily_cap=DEFAULT_DEPARTMENT_CAP,
                allocated=0.0,
                spent=0.0,
            )
        dept = state.departments[department]
        if dept.allocated + amount > dept.daily_cap:
            raise BudgetExhaustedError(
                f"Department '{department}' budget exhausted "
                f"(${dept.allocated:.2f} allocated / ${dept.daily_cap:.2f} cap)"
            )

        # Check session cap
        if state.total_allocated + amount > state.session_cap:
            raise BudgetExhaustedError(
                f"Session budget exhausted "
                f"(${state.total_allocated:.2f} allocated / ${state.session_cap:.2f} cap)"
            )

        # Create task record
        now = datetime.now(timezone.utc).isoformat()
        task = TaskBudget(
            task_id=task_id,
            agent_id=agent_id,
            department=department,
            allocated=amount,
            spent=0.0,
            status="running",
            retry_count=state.tasks[retry_of].retry_count + 1 if retry_of and retry_of in state.tasks else 0,
            original_budget=original_budget,
            allocated_at=now,
        )
        state.tasks[task_id] = task

        # Update department
        dept.allocated += amount

        # Update session total
        state.total_allocated += amount

        # Update agent task list
        if agent_id in state.agents:
            state.agents[agent_id].tasks.append(task_id)

        needs_approval = amount > PROPOSAL_THRESHOLD

        self._save()

        return {
            "task_id": task_id,
            "allocated": amount,
            "needs_approval": needs_approval,
            "department": department,
        }

    def spend(self, task_id: str, amount: float) -> None:
        """Record actual spend against a task."""
        if amount <= 0:
            raise ValueError(f"Spend amount must be positive, got ${amount:.2f}")

        state = self._state
        if task_id not in state.tasks:
            raise KeyError(f"No task '{task_id}' in budget state")

        task = state.tasks[task_id]
        if task.status != "running":
            raise BudgetExhaustedError(
                f"Cannot spend on task '{task_id}' with status '{task.status}'"
            )
        if task.spent + amount > task.allocated:
            raise BudgetExhaustedError(
                f"Spend ${amount:.2f} would exceed task allocation "
                f"(${task.spent:.2f} spent / ${task.allocated:.2f} allocated)"
            )
        task.spent += amount

        # Update department spend
        if task.department in state.departments:
            state.departments[task.department].spent += amount

        # Update agent spend
        if task.agent_id in state.agents:
            agent = state.agents[task.agent_id]
            agent.spent_today += amount
            agent.total_spent += amount

        # Update session total
        state.total_spent += amount

        self._save()

    def release(self, task_id: str) -> None:
        """Complete a task and return unspent budget to department pool."""
        state = self._state
        if task_id not in state.tasks:
            raise KeyError(f"No task '{task_id}' in budget state")

        task = state.tasks[task_id]
        unspent = task.allocated - task.spent
        task.status = "completed"
        task.completed_at = datetime.now(timezone.utc).isoformat()

        # Return unspent to department and session pools
        if task.department in state.departments:
            state.departments[task.department].allocated -= unspent
        state.total_allocated -= unspent

        self._save()

    def fail(self, task_id: str, refund_amount: Optional[float] = None) -> None:
        """Mark a task as failed and refund unspent budget."""
        state = self._state
        if task_id not in state.tasks:
            raise KeyError(f"No task '{task_id}' in budget state")

        task = state.tasks[task_id]
        unspent = task.allocated - task.spent
        if refund_amount is not None:
            unspent = min(refund_amount, unspent)

        task.status = "failed"
        task.completed_at = datetime.now(timezone.utc).isoformat()

        # Return unspent to pools
        if task.department in state.departments:
            state.departments[task.department].allocated -= unspent
        state.total_allocated -= unspent

        self._save()

    def reset_daily(self) -> None:
        """Reset all agents' daily spend counters."""
        for agent in self._state.agents.values():
            agent.spent_today = 0.0
        self._save()

    def format_bats(self, task_id: str) -> str:
        """Return BATS string for injection into agent prompts."""
        if task_id not in self._state.tasks:
            return ""
        task = self._state.tasks[task_id]
        remaining = task.allocated - task.spent
        return f"You have ${remaining:.2f} remaining for this task."
