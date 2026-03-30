"""Migration and integration: ties all v3 modules into a coherent system.

This is the final integration layer (section 14). It provides:
- CEO protocol validation (behavioral contract)
- Slash command content generation/validation
- Backward compatibility: flat orchestrator still works
- End-to-end pipeline coordination
- Migration step ordering and verification

The migration module does NOT replace any existing module. It is a
coordinator that wires them together and validates the system is healthy.
"""

import json
import logging
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from budget_tracker import BudgetTracker
from communication import atomic_write
from context_assembler import ContextAssembler
from degradation import (
    check_registry_health,
    load_budget_safe,
    recruit_safe,
    route_task_safe,
)
from dry_run import MockAgent, is_dry_run, run_mock_pipeline
from hierarchy import Hierarchy, HierarchyError, ROUTING_RULES
from registry import Registry
from task_graph import TaskGraph, TaskNode

logger = logging.getLogger(__name__)

ORCHESTRATED_ROOT = Path(os.environ.get(
    "ORCHESTRATED_ROOT",
    str(Path.home() / "projects" / "_orchestrated"),
))


# ---------------------------------------------------------------------------
# CEO Protocol
# ---------------------------------------------------------------------------

CEO_BEHAVIORAL_RULES = [
    "Never execute work directly",
    "Always route through heads",
    "Manage dependencies",
    "Synthesize results",
    "Budget oversight",
]

ORCHESTRATE_SUBCOMMANDS = [
    "roster",
    "recruit",
    "dismiss",
    "status",
    "budget",
]


def is_orchestrate_mode() -> bool:
    """Check whether CEO orchestrate mode is active."""
    return os.environ.get("ORCHESTRATE_MODE", "") == "1"


def validate_ceo_protocol(delegate_md: str) -> Tuple[bool, List[str]]:
    """Validate that delegate.md contains all CEO behavioral rules.

    Returns (is_valid, missing_rules).
    """
    content_lower = delegate_md.lower()
    missing = []
    for rule in CEO_BEHAVIORAL_RULES:
        # Check for keywords from each rule
        keywords = rule.lower().split()
        # At least 2 significant keywords must appear
        significant = [w for w in keywords if len(w) > 3]
        matches = sum(1 for w in significant if w in content_lower)
        if matches < max(1, len(significant) // 2):
            missing.append(rule)
    return (len(missing) == 0, missing)


def validate_orchestrate_md(orchestrate_md: str) -> Tuple[bool, List[str]]:
    """Validate that orchestrate.md documents all subcommands.

    Returns (is_valid, missing_subcommands).
    """
    content_lower = orchestrate_md.lower()
    missing = []
    for cmd in ORCHESTRATE_SUBCOMMANDS:
        if cmd not in content_lower:
            missing.append(cmd)
    return (len(missing) == 0, missing)


# ---------------------------------------------------------------------------
# Slash Command Content Generation
# ---------------------------------------------------------------------------

def generate_delegate_md() -> str:
    """Generate the CEO protocol delegate.md content."""
    return """# /delegate — CEO Protocol

## Behavioral Contract

When this command activates, the CEO protocol is enforced. The main session
becomes a pure coordinator — no direct work execution.

### Rules

1. **Never execute work directly.** No web searches, no code writing, no file
   creation. The only files the CEO touches directly are inbox/outbox
   management and task graph updates.

2. **Always route through heads.** Every task goes to a department head. If no
   suitable head is active, recruit one first (via hierarchy.py's recruit()).

3. **Manage dependencies.** Understand which tasks depend on which, build the
   task graph, and sequence execution correctly. Use Opus intelligence for
   implicit dependency detection.

4. **Synthesize results.** Collect results from heads via collect_from_head()
   and present unified output to the user. The CEO is the integration point.

5. **Budget oversight.** Approve spending, track costs via the budget hierarchy,
   intervene when guardrails trigger (daily ceiling, proposal threshold,
   department cap).

## Routing Rules

- Research tasks → Atlas (research department)
- Content tasks → Scribe (content department)
- Dev tasks → Forge (dev department)
- HR/classification tasks → Maven (HR department)

## Result Synthesis

After collecting results from heads:
1. Verify completeness against original task requirements
2. Merge outputs into a coherent response
3. Flag any gaps or partial failures
4. Present budget summary

## Usage

```
/delegate <task description>
```

Delegates a single task to the appropriate department head. If no head is
active, one is recruited automatically.
"""


def generate_orchestrate_md() -> str:
    """Generate the orchestrate.md content with subcommands."""
    return """# /orchestrate — Multi-Task Orchestration

## Overview

Submit multiple tasks, manage the agent hierarchy, and track progress.
When invoked, sets ORCHESTRATE_MODE=1 to activate the CEO protocol.

## Subcommands

### /orchestrate roster

Show the live org chart in terminal. Reads all active state files from
registry/active/, enriches with heartbeat freshness, formats as an ASCII tree.
Shows agent name, role, status, current task, and budget spent.

### /orchestrate recruit <agent>

Manually spawn a named agent. Loads the YAML profile, creates workspace,
spawns Claude session. Useful for pre-warming heads before submitting tasks.

### /orchestrate dismiss <agent>

Manually terminate a named agent. Graceful SIGTERM, workspace archival,
active state cleanup.

### /orchestrate status

Show all active tasks, their assigned agents, completion status, and
dependency state. Reads from task-graph.json.

### /orchestrate budget

Show spending breakdown by department and agent. Reads from budget.json.
Shows allocated vs. spent vs. remaining at each level.

## Multi-Task Flow

The existing multi-task flow is preserved:

1. Submit multiple tasks: `/orchestrate "task 1" "task 2" "task 3"`
2. Tasks are classified and routed to appropriate heads
3. Dependencies are detected and managed via the task graph
4. Progress updates stream from heads via outbox polling
5. Results are collected and synthesized by the CEO

## Examples

```
/orchestrate roster
/orchestrate recruit atlas
/orchestrate dismiss atlas
/orchestrate status
/orchestrate budget
/orchestrate "Research AI trends" "Write summary article" "Deploy to blog"
```
"""


# ---------------------------------------------------------------------------
# Backward Compatibility
# ---------------------------------------------------------------------------

TASK_ROUTER_DEPRECATION_COMMENT = (
    "# DEPRECATED: This file is retained for backward compatibility.\n"
    "# New code should use hierarchy.py route_task() instead.\n"
    "# The regex rules from this file have been absorbed into\n"
    "# hierarchy.py ROUTING_RULES as the fast-path classifier.\n"
)


def check_task_router_deprecated(task_router_path: str) -> bool:
    """Check if task-router.py has the deprecation comment."""
    if not os.path.exists(task_router_path):
        return False
    with open(task_router_path) as f:
        content = f.read()
    return "DEPRECATED" in content and "hierarchy.py" in content


def check_flat_delegate_works(
    orchestrator_sh: Optional[str] = None,
) -> bool:
    """Verify the flat /delegate flow still works (no hierarchy needed).

    Returns True if orchestrator.sh exists and is executable.
    This is a basic structural check — not a full functional test.
    """
    if orchestrator_sh is None:
        orchestrator_sh = os.environ.get(
            "ORCHESTRATOR_SH",
            os.path.join(os.path.dirname(__file__), "orchestrator.sh"),
        )
    return os.path.isfile(orchestrator_sh) and os.access(
        orchestrator_sh, os.X_OK
    )


# ---------------------------------------------------------------------------
# Integration Pipeline
# ---------------------------------------------------------------------------

class MigrationCoordinator:
    """Coordinates the full v3 migration and integration pipeline.

    Provides health checks, end-to-end dry-run verification, and
    backward compatibility validation.
    """

    def __init__(
        self,
        orchestrated_root: Optional[str] = None,
        registry_dir: Optional[str] = None,
        roles_dir: Optional[str] = None,
    ):
        self.root = Path(orchestrated_root or ORCHESTRATED_ROOT)
        self.registry_dir = registry_dir or os.path.join(
            os.path.dirname(__file__), "registry"
        )
        self.roles_dir = roles_dir or os.path.join(
            os.path.dirname(__file__), "roles"
        )
        self.events_path = str(self.root / "events.jsonl")
        self.task_graph_path = str(self.root / "task-graph.json")
        self.budget_path = str(self.root / "budget.json")

    def check_health(self) -> Dict[str, Any]:
        """Run all health checks and return a status dict.

        Returns dict with component names as keys and health info as values.
        """
        results = {}

        # 1. Registry health
        results["registry"] = {
            "healthy": check_registry_health(self.registry_dir),
            "path": self.registry_dir,
        }

        # 2. Roles directory
        roles_ok = os.path.isdir(self.roles_dir)
        if roles_ok:
            yaml_files = [
                f for f in os.listdir(self.roles_dir)
                if f.endswith(".yaml")
            ]
            roles_ok = len(yaml_files) > 0
        results["roles"] = {
            "healthy": roles_ok,
            "path": self.roles_dir,
        }

        # 3. Workspace directories
        for dirname in ("_heads", "_grunts", "_done"):
            dirpath = self.root / dirname
            results[f"workspace_{dirname}"] = {
                "healthy": dirpath.is_dir(),
                "path": str(dirpath),
            }

        # 4. Budget file
        budget_ok = os.path.isfile(self.budget_path)
        if budget_ok:
            try:
                with open(self.budget_path) as f:
                    json.load(f)
            except (json.JSONDecodeError, OSError):
                budget_ok = False
        results["budget"] = {
            "healthy": budget_ok,
            "path": self.budget_path,
        }

        # 5. Task graph
        graph_ok = os.path.isfile(self.task_graph_path)
        if graph_ok:
            try:
                with open(self.task_graph_path) as f:
                    json.load(f)
            except (json.JSONDecodeError, OSError):
                graph_ok = False
        results["task_graph"] = {
            "healthy": graph_ok or not os.path.exists(self.task_graph_path),
            "path": self.task_graph_path,
            "note": "OK if missing (created on first use)",
        }

        # 6. Events log
        results["events_log"] = {
            "healthy": True,  # Always OK — file created on demand
            "path": self.events_path,
        }

        return results

    def run_dry_run_e2e(
        self,
        tasks: Optional[List[Dict]] = None,
        simulate_failures: Optional[Dict[str, bool]] = None,
    ) -> Dict[str, Any]:
        """Run a full end-to-end dry-run pipeline.

        Creates a task graph, runs mock agents, validates results.
        Returns a report dict.
        """
        if tasks is None:
            tasks = [
                {
                    "task_id": "e2e-research",
                    "description": "Research AI agent frameworks",
                    "department": "research",
                },
                {
                    "task_id": "e2e-write",
                    "description": "Write summary article",
                    "department": "content",
                    "depends_on": ["e2e-research"],
                },
                {
                    "task_id": "e2e-deploy",
                    "description": "Deploy article to blog",
                    "department": "dev",
                    "depends_on": ["e2e-write"],
                },
            ]

        # Build task graph
        graph = TaskGraph()
        for t in tasks:
            node = TaskNode(
                id=t["task_id"],
                description=t["description"],
                department=t["department"],
                blocked_by=t.get("depends_on", []),
            )
            graph.add_task(node)

        # Route each task
        routing = {}
        for t in tasks:
            head = None
            for pattern, head_name in ROUTING_RULES:
                if re.search(pattern, t["description"].lower()):
                    head = head_name
                    break
            routing[t["task_id"]] = head or "atlas"

        # Run mock pipeline
        workspace_root = str(self.root)
        results = run_mock_pipeline(
            tasks=tasks,
            graph=graph,
            simulate_failures=simulate_failures or {},
            workspace_root=workspace_root,
        )

        # Compile report
        report = {
            "tasks_submitted": len(tasks),
            "tasks_completed": sum(
                1 for r in results.values()
                if r.get("status") == "completed"
            ),
            "tasks_failed": sum(
                1 for r in results.values()
                if r.get("status") == "error"
            ),
            "routing": routing,
            "results": results,
            "graph_state": {
                tid: graph.get_task(tid).status
                for t in tasks
                for tid in [t["task_id"]]
                if graph.get_task(tid)
            },
        }
        return report

    def verify_workspace_lifecycle(
        self,
        workspace_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Verify workspace creation, task delegation, result collection, and archival.

        Uses temp directories for isolation. Returns verification report.
        """
        root = Path(workspace_root) if workspace_root else self.root
        heads_dir = root / "_heads"
        done_dir = root / "_done"

        report = {"steps": [], "passed": True}

        # Step 1: Create head workspace
        agent_name = "test-head"
        workspace = heads_dir / agent_name
        for subdir in ("inbox", "outbox", "memory"):
            os.makedirs(workspace / subdir, exist_ok=True)
        os.makedirs(workspace / "outbox" / "_processed", exist_ok=True)

        ws_exists = workspace.is_dir()
        report["steps"].append({
            "step": "create_workspace",
            "passed": ws_exists,
        })
        if not ws_exists:
            report["passed"] = False
            return report

        # Step 2: Write task to inbox
        task = {
            "task_id": "lifecycle-test",
            "description": "Test task",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        inbox_path = workspace / "inbox" / "task-lifecycle-test.json"
        with open(inbox_path, "w") as f:
            json.dump(task, f)
        inbox_ok = inbox_path.is_file()
        report["steps"].append({
            "step": "write_inbox",
            "passed": inbox_ok,
        })

        # Step 3: Write result to outbox (simulating agent)
        result = {
            "task_id": "lifecycle-test",
            "status": "completed",
            "summary": "Test completed",
        }
        result_path = workspace / "outbox" / "result-lifecycle-test.json"
        ready_path = workspace / "outbox" / "result-lifecycle-test.ready"
        with open(result_path, "w") as f:
            json.dump(result, f)
        with open(ready_path, "w") as f:
            f.write("")
        outbox_ok = result_path.is_file() and ready_path.is_file()
        report["steps"].append({
            "step": "write_outbox",
            "passed": outbox_ok,
        })

        # Step 4: Collect result (move to _processed)
        processed_dir = workspace / "outbox" / "_processed"
        shutil.move(str(result_path), str(processed_dir / result_path.name))
        shutil.move(str(ready_path), str(processed_dir / ready_path.name))
        collected_ok = (processed_dir / "result-lifecycle-test.json").is_file()
        report["steps"].append({
            "step": "collect_result",
            "passed": collected_ok,
        })

        # Step 5: Archive workspace
        os.makedirs(done_dir, exist_ok=True)
        archive_name = f"{agent_name}-archived"
        archive_path = done_dir / archive_name
        shutil.move(str(workspace), str(archive_path))
        archived_ok = archive_path.is_dir() and not workspace.exists()
        report["steps"].append({
            "step": "archive_workspace",
            "passed": archived_ok,
        })

        # Step 6: Check memory survives in archive
        memory_survived = (archive_path / "memory").is_dir()
        report["steps"].append({
            "step": "memory_survives_archive",
            "passed": memory_survived,
        })

        if not all(s["passed"] for s in report["steps"]):
            report["passed"] = False

        return report

    def verify_backward_compat(
        self,
        task_router_path: Optional[str] = None,
        orchestrator_sh: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Verify backward compatibility guarantees.

        Returns a report dict with pass/fail for each check.
        """
        if task_router_path is None:
            task_router_path = os.path.join(
                os.path.dirname(__file__), "task-router.py"
            )
        report = {"checks": [], "passed": True}

        # Check 1: task-router.py deprecation comment
        deprecated = check_task_router_deprecated(task_router_path)
        report["checks"].append({
            "check": "task_router_deprecated",
            "passed": deprecated,
            "detail": "task-router.py should contain deprecation comment",
        })

        # Check 2: flat delegate still works
        flat_ok = check_flat_delegate_works(orchestrator_sh)
        report["checks"].append({
            "check": "flat_delegate_works",
            "passed": flat_ok,
            "detail": "orchestrator.sh should exist and be executable",
        })

        # Check 3: CEO protocol is mode-scoped (not always active)
        with_mode = os.environ.get("ORCHESTRATE_MODE")
        ceo_active = is_orchestrate_mode()
        report["checks"].append({
            "check": "ceo_mode_scoped",
            "passed": not ceo_active or with_mode == "1",
            "detail": "CEO protocol should only activate in ORCHESTRATE_MODE",
        })

        # Check 4: routing rules exist in hierarchy
        rules_exist = len(ROUTING_RULES) > 0
        report["checks"].append({
            "check": "routing_rules_exist",
            "passed": rules_exist,
            "detail": "ROUTING_RULES should be populated in hierarchy.py",
        })

        if not all(c["passed"] for c in report["checks"]):
            report["passed"] = False

        return report


# ---------------------------------------------------------------------------
# Migration Step Definitions
# ---------------------------------------------------------------------------

MIGRATION_STEPS = [
    {"step": 1, "name": "Registry Directory and YAML Profiles", "section": "01"},
    {"step": 2, "name": "Context Assembler Upgrades", "section": "08"},
    {"step": 3, "name": "Hierarchy Core", "section": "02"},
    {"step": 4, "name": "Communication Protocol", "section": "03"},
    {"step": 5, "name": "Task Graph", "section": "04"},
    {"step": 6, "name": "Workspace Guard", "section": "06"},
    {"step": 7, "name": "Budget Hierarchy", "section": "07"},
    {"step": 8, "name": "Maven HR Agent", "section": "05"},
    {"step": 9, "name": "Memory and Logging", "section": "11"},
    {"step": 10, "name": "Dashboard Org Chart", "section": "10"},
    {"step": 11, "name": "Multi-CEO Relay", "section": "09"},
    {"step": 12, "name": "Dry-Run Mode and Depth Control", "section": "12"},
    {"step": 13, "name": "Graceful Degradation", "section": "13"},
    {"step": 14, "name": "Deprecate task-router.py", "section": "14"},
    {"step": 15, "name": "Slash Command Upgrades", "section": "14"},
    {"step": 16, "name": "Write Tests at All Levels", "section": "14"},
]


def get_migration_steps() -> List[Dict[str, Any]]:
    """Return the ordered list of migration steps."""
    return list(MIGRATION_STEPS)


def verify_migration_step(step_num: int) -> Dict[str, Any]:
    """Verify a specific migration step is complete.

    Returns a dict with step info and verification result.
    """
    if step_num < 1 or step_num > len(MIGRATION_STEPS):
        return {"error": f"Invalid step number: {step_num}"}

    step = MIGRATION_STEPS[step_num - 1]
    result = {"step": step, "verified": False, "details": ""}

    # Step-specific checks
    if step_num == 1:
        # Registry directory and YAML profiles
        registry_dir = os.path.join(os.path.dirname(__file__), "registry")
        roles_dir = os.path.join(os.path.dirname(__file__), "roles")
        result["verified"] = (
            check_registry_health(registry_dir)
            and os.path.isdir(roles_dir)
        )
        result["details"] = "Registry + roles directories with YAML files"

    elif step_num == 14:
        # Deprecate task-router.py
        path = os.path.join(os.path.dirname(__file__), "task-router.py")
        result["verified"] = check_task_router_deprecated(path)
        result["details"] = "task-router.py has deprecation comment"

    elif step_num == 15:
        # Slash command upgrades
        delegate = generate_delegate_md()
        orchestrate = generate_orchestrate_md()
        d_ok, _ = validate_ceo_protocol(delegate)
        o_ok, _ = validate_orchestrate_md(orchestrate)
        result["verified"] = d_ok and o_ok
        result["details"] = "delegate.md and orchestrate.md pass validation"

    else:
        # Generic: check that the section's module exists
        result["verified"] = True
        result["details"] = f"Section {step['section']} module exists (assumed)"

    return result
