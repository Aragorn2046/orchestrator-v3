#!/usr/bin/env python3
"""cli.py -- Command-line interface for Orchestrator v3.

Bridges the v3 Python modules to shell/slash-command invocation.
All output is JSON for easy parsing by Claude sessions.
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Ensure our modules are importable
sys.path.insert(0, str(Path(__file__).parent))

from registry import Registry
from hierarchy import Hierarchy, HierarchyError, SpawnFailedError, AgentNotFoundError
from context_assembler import ContextAssembler
from budget_tracker import BudgetTracker, BudgetExhaustedError
from task_graph import TaskGraph, TaskNode
from org_chart import build_org_chart
from maven import fast_path_classify


def get_components(orchestrator_sh=None):
    """Initialize all v3 components."""
    project_root = str(Path(__file__).parent)
    registry = Registry(base_dir=project_root)
    assembler = ContextAssembler()
    budget = BudgetTracker(state_dir=os.path.join(project_root, "state"))
    hierarchy = Hierarchy(
        registry=registry,
        assembler=assembler,
        budget_tracker=budget,
        orchestrator_sh=orchestrator_sh or os.environ.get(
            "ORCHESTRATOR_SH",
            os.path.join(str(Path(__file__).parent), "orchestrator.sh"),
        ),
    )
    return registry, assembler, budget, hierarchy


def cmd_recruit(args):
    """Recruit a named agent."""
    registry, assembler, budget, hierarchy = get_components()
    try:
        agent_id = hierarchy.recruit(
            agent_name=args.name,
            task_id=args.task_id or f"task-{os.urandom(4).hex()}",
            task_description=args.task,
            budget=args.budget,
        )
        print(json.dumps({"status": "ok", "agent_id": agent_id}))
    except ValueError as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)
    except BudgetExhaustedError as e:
        print(json.dumps({"status": "budget_exhausted", "error": str(e)}))
        sys.exit(1)
    except SpawnFailedError as e:
        print(json.dumps({"status": "spawn_failed", "error": str(e)}))
        sys.exit(1)


def cmd_dismiss(args):
    """Dismiss an agent."""
    _, _, _, hierarchy = get_components()
    try:
        hierarchy.dismiss(args.agent_id, archive=not args.no_archive)
        print(json.dumps({"status": "ok", "agent_id": args.agent_id}))
    except AgentNotFoundError as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)


def cmd_roster(args):
    """List active agents."""
    _, _, _, hierarchy = get_components()
    agents = hierarchy.roster(machine=args.machine)
    print(json.dumps({"status": "ok", "agents": agents, "count": len(agents)}))


def cmd_route(args):
    """Route a task to a department head."""
    _, _, _, hierarchy = get_components()
    head = hierarchy.route_task(args.task)
    if head:
        print(json.dumps({"status": "ok", "head": head, "task": args.task}))
    else:
        print(json.dumps({"status": "ambiguous", "head": None, "task": args.task,
                          "message": "No regex match — needs Maven LLM classification"}))


def cmd_delegate(args):
    """Delegate a task to a head's inbox."""
    _, _, _, hierarchy = get_components()
    task = {
        "description": args.task,
        "budget": args.budget,
        "priority": args.priority or "normal",
        "from_agent": "ceo",
    }
    if args.task_id:
        task["task_id"] = args.task_id
    task_id = hierarchy.delegate_to_head(args.head, task)
    print(json.dumps({"status": "ok", "task_id": task_id, "head": args.head}))


def cmd_collect(args):
    """Collect results from a head's outbox."""
    _, _, _, hierarchy = get_components()
    results = hierarchy.collect_from_head(args.head)
    print(json.dumps({"status": "ok", "results": results, "count": len(results)}))


def cmd_budget(args):
    """Show budget status."""
    project_root = str(Path(__file__).parent)
    budget = BudgetTracker(state_dir=os.path.join(project_root, "state"))
    from dataclasses import asdict
    state = budget.get_state()
    print(json.dumps({"status": "ok", "budget": asdict(state)}, default=str))


def cmd_org_chart(args):
    """Show organization chart."""
    project_root = Path(__file__).parent
    tree = build_org_chart(
        registry_dir=str(project_root / "registry"),
        roles_dir=str(project_root / "roles"),
        active_dir=str(project_root / "active"),
    )
    print(json.dumps({"status": "ok", "tree": tree}))


def cmd_dry_run(args):
    """Simulate task routing without spawning."""
    _, _, _, hierarchy = get_components()
    head = hierarchy.route_task(args.task)
    result = {
        "task": args.task,
        "routed_to": head,
        "budget": args.budget,
        "would_recruit": head is not None,
        "dry_run": True,
    }
    print(json.dumps({"status": "ok", **result}))


def main():
    parser = argparse.ArgumentParser(description="Orchestrator v3 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    # recruit
    p = sub.add_parser("recruit", help="Recruit a named agent")
    p.add_argument("name", help="Agent name (atlas, forge, maven, scribe)")
    p.add_argument("--task", required=True, help="Task description")
    p.add_argument("--task-id", help="Task ID (auto-generated if omitted)")
    p.add_argument("--budget", type=float, default=5.0, help="Budget in USD")
    p.set_defaults(func=cmd_recruit)

    # dismiss
    p = sub.add_parser("dismiss", help="Dismiss an agent")
    p.add_argument("agent_id", help="Agent ID to dismiss")
    p.add_argument("--no-archive", action="store_true", help="Don't archive workspace")
    p.set_defaults(func=cmd_dismiss)

    # roster
    p = sub.add_parser("roster", help="List active agents")
    p.add_argument("--machine", help="Filter by machine")
    p.set_defaults(func=cmd_roster)

    # route
    p = sub.add_parser("route", help="Route a task to a department head")
    p.add_argument("task", help="Task description")
    p.set_defaults(func=cmd_route)

    # delegate
    p = sub.add_parser("delegate", help="Delegate task to a head's inbox")
    p.add_argument("head", help="Head name (atlas, forge, maven, scribe)")
    p.add_argument("--task", required=True, help="Task description")
    p.add_argument("--task-id", help="Task ID")
    p.add_argument("--budget", type=float, default=5.0, help="Budget")
    p.add_argument("--priority", help="Priority (low, normal, high, critical)")
    p.set_defaults(func=cmd_delegate)

    # collect
    p = sub.add_parser("collect", help="Collect results from a head")
    p.add_argument("head", help="Head name")
    p.set_defaults(func=cmd_collect)

    # budget
    p = sub.add_parser("budget", help="Show budget status")
    p.set_defaults(func=cmd_budget)

    # org-chart
    p = sub.add_parser("org-chart", help="Show organization chart")
    p.set_defaults(func=cmd_org_chart)

    # dry-run
    p = sub.add_parser("dry-run", help="Simulate task routing")
    p.add_argument("task", help="Task description")
    p.add_argument("--budget", type=float, default=5.0, help="Simulated budget")
    p.set_defaults(func=cmd_dry_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
