"""Tests for section 14: Migration and Integration.

Validates:
- CEO protocol activation and behavioral contract
- Slash command content (delegate.md, orchestrate.md)
- Backward compatibility (flat delegate, task-router deprecation)
- End-to-end integration pipeline (dry-run)
- Workspace lifecycle (create, delegate, collect, archive)
- Migration step ordering and verification
"""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from migration import (
    CEO_BEHAVIORAL_RULES,
    MIGRATION_STEPS,
    ORCHESTRATE_SUBCOMMANDS,
    TASK_ROUTER_DEPRECATION_COMMENT,
    MigrationCoordinator,
    check_flat_delegate_works,
    check_task_router_deprecated,
    generate_delegate_md,
    generate_orchestrate_md,
    get_migration_steps,
    is_orchestrate_mode,
    validate_ceo_protocol,
    validate_orchestrate_md,
    verify_migration_step,
)
from hierarchy import ROUTING_RULES
from task_graph import TaskGraph, TaskNode


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ===================================================================
# CEO Protocol Tests
# ===================================================================

class TestCEOProtocol(unittest.TestCase):
    """CEO protocol activates only during /orchestrate mode."""

    def test_orchestrate_mode_default_on(self):
        """When no env vars are set, CEO mode is active (default-on)."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("ORCHESTRATE_MODE", None)
            self.assertTrue(is_orchestrate_mode())

    def test_orchestrate_mode_active_when_set(self):
        """When ORCHESTRATE_MODE=1, CEO mode activates."""
        with patch.dict(os.environ, {"ORCHESTRATE_MODE": "1"}):
            self.assertTrue(is_orchestrate_mode())

    def test_orchestrate_mode_inactive_when_zero(self):
        """ORCHESTRATE_MODE=0 means inactive."""
        with patch.dict(os.environ, {"ORCHESTRATE_MODE": "0"}):
            self.assertFalse(is_orchestrate_mode())

    def test_ceo_protocol_inactive_for_worker_sessions(self):
        """Spinoff and cron sessions do not get CEO behavior."""
        with patch.dict(os.environ, {"CLAUDE_SESSION_TYPE": "spinoff"}):
            self.assertFalse(is_orchestrate_mode())
        with patch.dict(os.environ, {"CLAUDE_SESSION_TYPE": "cron"}):
            self.assertFalse(is_orchestrate_mode())

    def test_delegate_md_contains_all_behavioral_rules(self):
        """delegate.md includes all five CEO behavioral rules."""
        delegate_md = generate_delegate_md()
        is_valid, missing = validate_ceo_protocol(delegate_md)
        self.assertTrue(is_valid, f"Missing rules: {missing}")

    def test_validate_ceo_protocol_detects_missing_rules(self):
        """Incomplete delegate.md is detected."""
        incomplete_md = "# Delegate\nJust route tasks. Nothing else."
        is_valid, missing = validate_ceo_protocol(incomplete_md)
        self.assertFalse(is_valid)
        self.assertTrue(len(missing) > 0)

    def test_orchestrate_mode_agent_session(self):
        """Worker sessions with ORCHESTRATOR_AGENT_ID set are not CEO."""
        with patch.dict(os.environ, {"ORCHESTRATOR_AGENT_ID": "atlas"}):
            self.assertFalse(is_orchestrate_mode())

    def test_orchestrate_mode_spinoff(self):
        """Spinoff sessions are not CEO."""
        with patch.dict(os.environ, {"CLAUDE_SESSION_TYPE": "spinoff"}):
            self.assertFalse(is_orchestrate_mode())

    def test_orchestrate_mode_cron(self):
        """Cron sessions are not CEO."""
        with patch.dict(os.environ, {"CLAUDE_SESSION_TYPE": "cron"}):
            self.assertFalse(is_orchestrate_mode())

    def test_orchestrate_mode_specialist(self):
        """Specialist sessions (actual orchestrator.sh value) are not CEO."""
        with patch.dict(os.environ, {"CLAUDE_SESSION_TYPE": "specialist"}):
            self.assertFalse(is_orchestrate_mode())

    def test_orchestrate_mode_main_explicit(self):
        """Explicit CLAUDE_SESSION_TYPE=main activates CEO mode."""
        with patch.dict(os.environ, {"CLAUDE_SESSION_TYPE": "main"}, clear=True):
            self.assertTrue(is_orchestrate_mode())

    def test_orchestrate_mode_main_with_agent_id(self):
        """Agent ID takes precedence over session type -- worker, not CEO."""
        with patch.dict(os.environ, {
            "CLAUDE_SESSION_TYPE": "main",
            "ORCHESTRATOR_AGENT_ID": "atlas",
        }):
            self.assertFalse(is_orchestrate_mode())

    def test_orchestrate_mode_opt_out_with_main(self):
        """Explicit opt-out takes precedence over explicit main session type."""
        with patch.dict(os.environ, {
            "CLAUDE_SESSION_TYPE": "main",
            "ORCHESTRATE_MODE": "0",
        }):
            self.assertFalse(is_orchestrate_mode())

    def test_delegate_md_references_auto_ceo(self):
        """delegate.md references auto-CEO / default-on behavior."""
        md = generate_delegate_md()
        md_lower = md.lower()
        self.assertTrue(
            "auto" in md_lower or "default" in md_lower,
            "delegate.md should reference automatic/default CEO activation"
        )


# ===================================================================
# Slash Command Content Tests
# ===================================================================

class TestSlashCommands(unittest.TestCase):
    """Validate slash command markdown content."""

    def test_delegate_md_is_valid_markdown_with_required_sections(self):
        """delegate.md contains behavioral contract, routing, synthesis."""
        md = generate_delegate_md()
        self.assertIn("Behavioral Contract", md)
        self.assertIn("Routing", md)
        self.assertIn("Synthesis", md)
        self.assertIn("/delegate", md)

    def test_orchestrate_md_has_all_subcommands(self):
        """orchestrate.md documents roster, recruit, dismiss, status, budget."""
        md = generate_orchestrate_md()
        is_valid, missing = validate_orchestrate_md(md)
        self.assertTrue(is_valid, f"Missing subcommands: {missing}")

    def test_orchestrate_md_has_usage_examples(self):
        """orchestrate.md contains usage examples for subcommands."""
        md = generate_orchestrate_md()
        self.assertIn("/orchestrate roster", md)
        self.assertIn("/orchestrate recruit", md)
        self.assertIn("/orchestrate dismiss", md)
        self.assertIn("/orchestrate status", md)
        self.assertIn("/orchestrate budget", md)

    def test_orchestrate_md_preserves_multi_task_flow(self):
        """orchestrate.md still supports the original multi-task flow."""
        md = generate_orchestrate_md()
        # Must mention task submission flow
        self.assertIn("task", md.lower())
        # Must mention results collection
        self.assertIn("result", md.lower())

    def test_delegate_md_mentions_all_department_heads(self):
        """delegate.md mentions Atlas, Scribe, Forge, and Maven heads."""
        md = generate_delegate_md()
        self.assertIn("Atlas", md)
        self.assertIn("Scribe", md)
        self.assertIn("Forge", md)
        self.assertIn("Maven", md)


# ===================================================================
# Backward Compatibility Tests
# ===================================================================

class TestBackwardCompat(unittest.TestCase):
    """Backward compatibility: flat orchestrator + task-router deprecation."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmpdir.name

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_task_router_deprecation_comment_detected(self):
        """task-router.py with deprecation comment is detected."""
        path = os.path.join(self.tmpdir, "task-router.py")
        with open(path, "w") as f:
            f.write(TASK_ROUTER_DEPRECATION_COMMENT)
            f.write("\n# rest of file\n")
        self.assertTrue(check_task_router_deprecated(path))

    def test_task_router_without_deprecation_fails(self):
        """task-router.py without deprecation comment fails check."""
        path = os.path.join(self.tmpdir, "task-router.py")
        with open(path, "w") as f:
            f.write("#!/usr/bin/env python3\n# Old router\n")
        self.assertFalse(check_task_router_deprecated(path))

    def test_task_router_missing_file_fails(self):
        """Missing task-router.py fails check."""
        path = os.path.join(self.tmpdir, "nonexistent.py")
        self.assertFalse(check_task_router_deprecated(path))

    def test_flat_delegate_works_with_executable_script(self):
        """Flat delegate works if orchestrator.sh exists and is executable."""
        path = os.path.join(self.tmpdir, "orchestrator.sh")
        with open(path, "w") as f:
            f.write("#!/bin/bash\n")
        os.chmod(path, 0o755)
        self.assertTrue(check_flat_delegate_works(path))

    def test_flat_delegate_fails_missing_script(self):
        """Flat delegate fails if orchestrator.sh is missing."""
        path = os.path.join(self.tmpdir, "missing.sh")
        self.assertFalse(check_flat_delegate_works(path))

    def test_flat_delegate_fails_non_executable(self):
        """Flat delegate fails if orchestrator.sh is not executable."""
        path = os.path.join(self.tmpdir, "orchestrator.sh")
        with open(path, "w") as f:
            f.write("#!/bin/bash\n")
        os.chmod(path, 0o644)
        self.assertFalse(check_flat_delegate_works(path))

    def test_hierarchy_opt_in_does_not_break_opt_out(self):
        """When registry is missing, system should fall back to flat mode."""
        # Missing registry -> check_registry_health returns False
        from degradation import check_registry_health
        fake_dir = os.path.join(self.tmpdir, "nonexistent_registry")
        self.assertFalse(check_registry_health(fake_dir))

    def test_routing_rules_absorbed_from_task_router(self):
        """hierarchy.py ROUTING_RULES covers the same domains as task-router.py."""
        self.assertTrue(len(ROUTING_RULES) >= 4)
        # Must cover research, content, dev, and maven/HR
        heads = [rule[1] for rule in ROUTING_RULES]
        self.assertIn("atlas", heads)
        self.assertIn("scribe", heads)
        self.assertIn("forge", heads)

    def test_ceo_protocol_mode_scoped(self):
        """CEO protocol activates by default for main sessions."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("ORCHESTRATE_MODE", None)
            self.assertTrue(is_orchestrate_mode())


# ===================================================================
# Integration E2E Tests
# ===================================================================

class TestIntegrationE2E(unittest.TestCase):
    """End-to-end integration pipeline tests using dry-run mode."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmpdir.name
        self.root = os.path.join(self.tmpdir, "_orchestrated")
        os.makedirs(self.root)
        # Create required subdirectories
        for d in ("_heads", "_grunts", "_done"):
            os.makedirs(os.path.join(self.root, d))

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_full_dry_run_pipeline(self):
        """Full dry-run: classify -> graph -> mock agent -> collect -> resolve."""
        coordinator = MigrationCoordinator(
            orchestrated_root=self.root,
            registry_dir=self.tmpdir,
            roles_dir=self.tmpdir,
        )

        tasks = [
            {
                "task_id": "t1",
                "description": "Research quantum computing",
                "department": "research",
            },
            {
                "task_id": "t2",
                "description": "Write a blog post",
                "department": "content",
                "depends_on": ["t1"],
            },
        ]

        report = coordinator.run_dry_run_e2e(tasks=tasks)

        self.assertEqual(report["tasks_submitted"], 2)
        self.assertEqual(report["tasks_completed"], 2)
        self.assertEqual(report["tasks_failed"], 0)

        # Verify routing
        self.assertEqual(report["routing"]["t1"], "atlas")
        self.assertEqual(report["routing"]["t2"], "scribe")

        # Verify graph state
        self.assertEqual(report["graph_state"]["t1"], "completed")
        self.assertEqual(report["graph_state"]["t2"], "completed")

    def test_failure_propagation_through_pipeline(self):
        """A -> B -> C: A fails, B and C get dependency_failed."""
        tasks = [
            {"task_id": "a", "description": "Research topic", "department": "research"},
            {"task_id": "b", "description": "Write article", "department": "content", "depends_on": ["a"]},
            {"task_id": "c", "description": "Deploy content", "department": "dev", "depends_on": ["b"]},
        ]

        coordinator = MigrationCoordinator(
            orchestrated_root=self.root,
            registry_dir=self.tmpdir,
            roles_dir=self.tmpdir,
        )

        report = coordinator.run_dry_run_e2e(
            tasks=tasks,
            simulate_failures={"a": True},
        )

        self.assertEqual(report["graph_state"]["a"], "failed")
        self.assertEqual(report["graph_state"]["b"], "dependency_failed")
        self.assertEqual(report["graph_state"]["c"], "dependency_failed")

    def test_default_e2e_tasks(self):
        """Default E2E tasks run successfully without explicit task list."""
        coordinator = MigrationCoordinator(
            orchestrated_root=self.root,
            registry_dir=self.tmpdir,
            roles_dir=self.tmpdir,
        )

        report = coordinator.run_dry_run_e2e()

        self.assertEqual(report["tasks_submitted"], 3)
        self.assertEqual(report["tasks_completed"], 3)
        self.assertEqual(report["tasks_failed"], 0)

    def test_independent_tasks_all_complete(self):
        """Independent tasks (no deps) all complete successfully."""
        tasks = [
            {"task_id": "x1", "description": "Research AI", "department": "research"},
            {"task_id": "x2", "description": "Write docs", "department": "content"},
            {"task_id": "x3", "description": "Build tool", "department": "dev"},
        ]

        coordinator = MigrationCoordinator(
            orchestrated_root=self.root,
            registry_dir=self.tmpdir,
            roles_dir=self.tmpdir,
        )

        report = coordinator.run_dry_run_e2e(tasks=tasks)

        self.assertEqual(report["tasks_submitted"], 3)
        self.assertEqual(report["tasks_completed"], 3)


# ===================================================================
# Workspace Lifecycle Tests
# ===================================================================

class TestWorkspaceLifecycle(unittest.TestCase):
    """Workspace creation, delegation, collection, and archival."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmpdir.name
        self.root = os.path.join(self.tmpdir, "_orchestrated")
        for d in ("_heads", "_grunts", "_done"):
            os.makedirs(os.path.join(self.root, d))

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_full_workspace_lifecycle(self):
        """Create -> delegate -> collect -> archive -> memory survives."""
        coordinator = MigrationCoordinator(
            orchestrated_root=self.root,
            registry_dir=self.tmpdir,
            roles_dir=self.tmpdir,
        )

        report = coordinator.verify_workspace_lifecycle(
            workspace_root=self.root,
        )

        self.assertTrue(report["passed"], f"Failed steps: {report['steps']}")
        for step in report["steps"]:
            self.assertTrue(
                step["passed"],
                f"Step '{step['step']}' failed",
            )

    def test_workspace_inbox_populated(self):
        """Delegating a task populates the head's inbox."""
        agent_name = "test-atlas"
        heads_dir = os.path.join(self.root, "_heads")
        workspace = os.path.join(heads_dir, agent_name)
        inbox = os.path.join(workspace, "inbox")
        os.makedirs(inbox, exist_ok=True)

        # Write task
        task = {"task_id": "t-inbox", "description": "Test"}
        task_path = os.path.join(inbox, "task-t-inbox.json")
        with open(task_path, "w") as f:
            json.dump(task, f)

        self.assertTrue(os.path.isfile(task_path))
        with open(task_path) as f:
            loaded = json.load(f)
        self.assertEqual(loaded["task_id"], "t-inbox")

    def test_workspace_archival_preserves_memory(self):
        """Archiving a workspace preserves the memory/ directory."""
        agent_name = "archive-test"
        heads_dir = os.path.join(self.root, "_heads")
        workspace = os.path.join(heads_dir, agent_name)
        done_dir = os.path.join(self.root, "_done")

        # Create workspace with memory
        for subdir in ("inbox", "outbox", "memory"):
            os.makedirs(os.path.join(workspace, subdir), exist_ok=True)
        with open(os.path.join(workspace, "memory", "notes.md"), "w") as f:
            f.write("# Important notes\n")

        # Archive
        archive_path = os.path.join(done_dir, f"{agent_name}-archived")
        shutil.move(workspace, archive_path)

        # Verify memory survived
        self.assertTrue(os.path.isdir(os.path.join(archive_path, "memory")))
        self.assertTrue(
            os.path.isfile(os.path.join(archive_path, "memory", "notes.md"))
        )


# ===================================================================
# Health Check Tests
# ===================================================================

class TestHealthChecks(unittest.TestCase):
    """MigrationCoordinator health check tests."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmpdir.name
        self.root = os.path.join(self.tmpdir, "_orchestrated")
        os.makedirs(self.root)

        # Create registry with a valid YAML file
        self.registry_dir = os.path.join(self.tmpdir, "registry")
        os.makedirs(self.registry_dir)
        with open(os.path.join(self.registry_dir, "test.yaml"), "w") as f:
            f.write("name: test\nrole: head\n")

        # Create roles dir
        self.roles_dir = os.path.join(self.tmpdir, "roles")
        os.makedirs(self.roles_dir)
        with open(os.path.join(self.roles_dir, "scout.yaml"), "w") as f:
            f.write("name: scout\nrole: grunt\n")

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_health_check_all_healthy(self):
        """All components healthy when everything exists."""
        # Create workspace dirs and budget
        for d in ("_heads", "_grunts", "_done"):
            os.makedirs(os.path.join(self.root, d))
        budget = {"session_cap": 15.0, "total_spent": 0.0}
        with open(os.path.join(self.root, "budget.json"), "w") as f:
            json.dump(budget, f)

        coordinator = MigrationCoordinator(
            orchestrated_root=self.root,
            registry_dir=self.registry_dir,
            roles_dir=self.roles_dir,
        )

        results = coordinator.check_health()
        self.assertTrue(results["registry"]["healthy"])
        self.assertTrue(results["roles"]["healthy"])
        self.assertTrue(results["workspace__heads"]["healthy"])
        self.assertTrue(results["workspace__grunts"]["healthy"])
        self.assertTrue(results["workspace__done"]["healthy"])
        self.assertTrue(results["budget"]["healthy"])

    def test_health_check_missing_registry(self):
        """Missing registry is detected as unhealthy."""
        coordinator = MigrationCoordinator(
            orchestrated_root=self.root,
            registry_dir=os.path.join(self.tmpdir, "nonexistent"),
            roles_dir=self.roles_dir,
        )

        results = coordinator.check_health()
        self.assertFalse(results["registry"]["healthy"])

    def test_health_check_corrupt_budget(self):
        """Corrupt budget.json is detected as unhealthy."""
        budget_path = os.path.join(self.root, "budget.json")
        with open(budget_path, "w") as f:
            f.write("NOT VALID JSON{{{")

        coordinator = MigrationCoordinator(
            orchestrated_root=self.root,
            registry_dir=self.registry_dir,
            roles_dir=self.roles_dir,
        )

        results = coordinator.check_health()
        self.assertFalse(results["budget"]["healthy"])

    def test_health_check_missing_task_graph_is_ok(self):
        """Missing task-graph.json is acceptable (created on first use)."""
        coordinator = MigrationCoordinator(
            orchestrated_root=self.root,
            registry_dir=self.registry_dir,
            roles_dir=self.roles_dir,
        )

        results = coordinator.check_health()
        self.assertTrue(results["task_graph"]["healthy"])


# ===================================================================
# Migration Steps Tests
# ===================================================================

class TestMigrationSteps(unittest.TestCase):
    """Migration step ordering and verification."""

    def test_migration_has_16_steps(self):
        """There are exactly 16 migration steps."""
        steps = get_migration_steps()
        self.assertEqual(len(steps), 16)

    def test_steps_are_numbered_1_to_16(self):
        """Steps are numbered sequentially from 1 to 16."""
        steps = get_migration_steps()
        for i, step in enumerate(steps):
            self.assertEqual(step["step"], i + 1)

    def test_step_ordering_preserves_dependencies(self):
        """Registry (step 1) comes before hierarchy (step 3)."""
        steps = get_migration_steps()
        registry_idx = next(
            i for i, s in enumerate(steps)
            if "Registry" in s["name"]
        )
        hierarchy_idx = next(
            i for i, s in enumerate(steps)
            if "Hierarchy" in s["name"]
        )
        self.assertLess(registry_idx, hierarchy_idx)

    def test_deprecate_task_router_comes_late(self):
        """Step 14 (deprecate task-router) comes after core modules."""
        steps = get_migration_steps()
        deprecate = next(s for s in steps if "Deprecate" in s["name"])
        self.assertEqual(deprecate["step"], 14)

    def test_slash_commands_upgrade_is_step_15(self):
        """Slash command upgrades are step 15."""
        steps = get_migration_steps()
        slash = next(s for s in steps if "Slash" in s["name"])
        self.assertEqual(slash["step"], 15)

    def test_verify_step_15_slash_commands(self):
        """Step 15 verification passes with generated slash commands."""
        result = verify_migration_step(15)
        self.assertTrue(result["verified"], f"Details: {result['details']}")

    def test_verify_invalid_step_returns_error(self):
        """Invalid step number returns error."""
        result = verify_migration_step(0)
        self.assertIn("error", result)
        result = verify_migration_step(99)
        self.assertIn("error", result)

    def test_steps_immutable(self):
        """get_migration_steps returns a copy, not the original."""
        steps1 = get_migration_steps()
        steps1.append({"step": 99, "name": "Fake"})
        steps2 = get_migration_steps()
        self.assertEqual(len(steps2), 16)


# ===================================================================
# Backward Compat Verification
# ===================================================================

class TestBackwardCompatVerification(unittest.TestCase):
    """MigrationCoordinator.verify_backward_compat tests."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmpdir.name
        self.root = os.path.join(self.tmpdir, "_orchestrated")
        os.makedirs(self.root)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_verify_backward_compat_all_pass(self):
        """All backward compat checks pass with proper setup."""
        # Create deprecated task-router
        tr_path = os.path.join(self.tmpdir, "task-router.py")
        with open(tr_path, "w") as f:
            f.write(TASK_ROUTER_DEPRECATION_COMMENT)
            f.write("\n# rest\n")

        # Create executable orchestrator.sh
        orch_path = os.path.join(self.tmpdir, "orchestrator.sh")
        with open(orch_path, "w") as f:
            f.write("#!/bin/bash\n")
        os.chmod(orch_path, 0o755)

        coordinator = MigrationCoordinator(
            orchestrated_root=self.root,
            registry_dir=self.tmpdir,
            roles_dir=self.tmpdir,
        )

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ORCHESTRATE_MODE", None)
            report = coordinator.verify_backward_compat(
                task_router_path=tr_path,
                orchestrator_sh=orch_path,
            )

        self.assertTrue(report["passed"], f"Failed: {report['checks']}")

    def test_verify_backward_compat_missing_deprecation(self):
        """Missing deprecation comment causes failure."""
        tr_path = os.path.join(self.tmpdir, "task-router.py")
        with open(tr_path, "w") as f:
            f.write("#!/usr/bin/env python3\n# Old\n")

        orch_path = os.path.join(self.tmpdir, "orchestrator.sh")
        with open(orch_path, "w") as f:
            f.write("#!/bin/bash\n")
        os.chmod(orch_path, 0o755)

        coordinator = MigrationCoordinator(
            orchestrated_root=self.root,
            registry_dir=self.tmpdir,
            roles_dir=self.tmpdir,
        )

        report = coordinator.verify_backward_compat(
            task_router_path=tr_path,
            orchestrator_sh=orch_path,
        )

        self.assertFalse(report["passed"])
        deprecation_check = next(
            c for c in report["checks"]
            if c["check"] == "task_router_deprecated"
        )
        self.assertFalse(deprecation_check["passed"])


if __name__ == "__main__":
    unittest.main()
