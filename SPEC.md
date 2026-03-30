# Orchestrator v3: Agent Hierarchy System

## Summary

Evolve the flat orchestrator (`~/scripts/orchestrator/`) into a corporate hierarchy of named AI agents. The main session becomes the CEO who never does work, only delegates. Below it: department heads (Opus) who own domains, and grunts (Sonnet/Haiku) who execute tasks. Add workspace isolation, persistent agent identity, depth control, and an EMOC dashboard org chart.

## Existing System

The orchestrator already has 7 scripts, 8 templates, 2 slash commands, and an EMOC dashboard. See `~/scripts/orchestrator/` for the full codebase. The EMOC dashboard at `~/projects/orchestrator-dashboard/` provides live session monitoring, history, and cross-machine visibility.

### Scripts to modify
- `orchestrator.sh` — add recruit/dismiss/roster commands, depth enforcement, head workspace management
- `task-router.py` — becomes Maven's classification brain (route to department, not just template)
- `context-assembler.py` — builds agent identity CLAUDE.md (personality + context + delegation rules)
- `result-collector.py` — poll head outboxes in addition to grunt result.json files
- `budget-tracker.py` — track per-agent and per-department spend
- `log-orchestration.py` — log agent names, reporting chains, delegation depth
- `history.py` — query by agent name, department, role

### Files to create
- `~/scripts/orchestrator/registry/` — agent profile YAMLs (atlas.yaml, forge.yaml, maven.yaml, scribe.yaml)
- `~/scripts/orchestrator/roles/` — grunt role templates (scout.yaml, smith.yaml, quill.yaml)
- `~/scripts/orchestrator/registry/active/` — runtime state JSON per active agent
- `~/scripts/orchestrator/hierarchy.py` — agent lifecycle: recruit, dismiss, roster, route-to-head
- `~/scripts/claude-hooks/session-guard.sh` — upgrade: depth enforcement via ORCHESTRATOR_DEPTH
- `~/.claude/commands/delegate.md` — upgrade: CEO protocol (never do work, route through heads)
- `~/.claude/commands/orchestrate.md` — upgrade: roster, recruit, dismiss subcommands

### Dashboard changes (~/projects/orchestrator-dashboard/)
- `server.py` — new API endpoints: `/api/roster`, `/api/agents/<name>`, `/api/agents/<name>/history`
- `static/index.html` — new Org Chart tab with tree visualization, agent detail view

## Scope

### In scope
1. Agent registry with YAML profiles (4 heads: Atlas, Forge, Maven, Scribe)
2. Role templates for grunts (3 roles: Scout, Smith, Quill)
3. Workspace isolation (persistent head dirs, ephemeral grunt dirs)
4. CEO protocol: main session behavioral contract — delegate, never execute
5. Maven recruiter: match task → department → agent → spawn
6. Depth control: session-guard enforces CEO(0) → Head(1) → Grunt(2) max
7. Communication protocol: file-based inbox/outbox for heads
8. EMOC org chart: live hierarchy view in dashboard
9. Per-agent budget and history tracking
10. Cross-machine routing: Forge can dispatch grunts to Dawn for GPU tasks

### Out of scope (future)
- Agent-to-agent real-time messaging (claude-peers style)
- Automatic agent learning from past tasks
- Agent promotion/demotion
- Voice interface for CEO ("Hey Atlas, what are you working on?")

## Architecture

### Agent Hierarchy
```
Aragorn → CEO (depth 0, Opus, main session)
            ├── Atlas (depth 1, Opus, research head)
            │     ├── Scout-1 (depth 2, Sonnet)
            │     └── Scout-2 (depth 2, Sonnet)
            ├── Forge (depth 1, Opus, dev head)
            │     ├── Smith-1 (depth 2, Sonnet)
            │     └── Smith-2 (depth 2, Sonnet)
            ├── Maven (depth 1, Sonnet, HR/recruitment)
            └── Scribe (depth 1, Opus, content head)
                  ├── Quill-1 (depth 2, Sonnet)
                  └── Quill-2 (depth 2, Haiku)
```

### Communication Flow
```
Aragorn: "Research AI chip supply chains and write a LinkedIn post about it"
  → CEO classifies: research task + writing task
  → CEO → Atlas inbox: "Research AI chip supply chains"
  → CEO → Scribe inbox: "Write LinkedIn post about AI chip supply chains (wait for Atlas research)"
  → Atlas spawns Scout-1 (Sonnet) for web research
  → Scout-1 writes result.json → Atlas collects, synthesizes
  → Atlas writes result to outbox → CEO collects
  → CEO passes research to Scribe inbox (as dependency)
  → Scribe spawns Quill-1 (Sonnet) to draft
  → Quill-1 writes result.json → Scribe reviews, refines
  → Scribe writes result to outbox → CEO collects
  → CEO synthesizes and presents to Aragorn
```

### Workspace Layout
```
~/projects/_orchestrated/
  _heads/
    atlas/
      CLAUDE.md           # Identity + personality + context
      inbox/              # Tasks from CEO
      outbox/             # Results for CEO
      memory/             # Persistent notes across tasks
    forge/
    maven/
    scribe/
  _grunts/
    scout-1-abc123/       # Ephemeral per-job
    smith-2-def456/
  _done/                  # Archived grunt workspaces
```

### Agent Profile Schema
```yaml
name: Atlas
display_name: "Atlas — Head of Research"
role: head
department: research
model: opus
budget_cap: 5.00
personality: >
  You are Atlas, Head of Research at EMOC. Methodical, thorough, skeptical.
  You delegate web searches and data gathering to your Scouts.
  You synthesize findings into structured briefs.
  You never accept claims without sources.
context_files:
  - "$VAULT/Worldview/Worldview & Core Theses.md"
allowed_tools:
  - Read, Write, Edit, Glob, Grep, Bash, WebSearch, WebFetch, Agent
can_spawn:
  - scout
reports_to: ceo
result_contract: >
  Write results to your outbox as markdown files.
  Include: executive summary, key findings, sources, confidence level.
```

## CEO Review Decisions (2026-03-29)

### Implementation Approach: Lean Hierarchy (B)
Ship CEO + Atlas first. Maven as Python function (not agent). Forge/Scribe added when needed.

### Accepted Scope Expansions
1. **Self-healing workflows** (M) — Heads auto-retry grunt failures with diagnosis
2. **Cross-task agent memory** (S) — memory/ dir with append-only markdown findings
3. **Dependency-aware task chaining** (M) — task-graph.json tracks dependencies
4. **Proactive work proposals** (L) — Agents propose tasks to CEO inbox (Phase 3)
5. **Agent performance tracking** (S) — Success rate, cost, duration per agent
6. **Daily standup summary** (S) — Cron-generated org status (Phase 3)
7. `/ask <agent>` command (S) — Query agent memory directly (Phase 2)
8. **Task dependency visualization** (M) — EMOC shows task chains (Phase 3)

### Architecture Fixes from Review
- **Background collector**: result-collector.py runs as daemon, writes unified status file
- **Crash detection**: Session ID guard (not just PID) for orphaned task detection
- **Workspace isolation**: Session-guard blocks file ops outside AGENT_WORKSPACE
- **Dependency failure propagation**: `dependency_failed` status propagates to blocked tasks
- **Structured JSON logging**: All agent actions logged as JSON lines
- **EMOC empty state**: Show potential org structure dimmed when no agents active
- **Spending guardrails**: Per-retry cap (capped at original task budget), daily agent ceiling ($25 default), proposal approval threshold ($2)
- **Depth control**: Enforcement in spawning code (orchestrator.sh), not just env var
- **Task graph**: Simple task-graph.json for dependency tracking

### Phased Delivery
- **Phase 1**: Foundation (registry, CEO protocol, workspace isolation, depth control, collector daemon, logging, dry-run, guardrails, EMOC org chart)
- **Phase 2**: Intelligence (agent memory, self-healing, performance tracking, crash detection, /ask command)
- **Phase 3**: Autonomy (task chaining, dependency viz, standup summary, proactive proposals)
- **Phase 4**: Scale (Forge, Scribe, cross-machine routing)

## Testing Strategy

- Unit tests for hierarchy.py (route, recruit, dismiss, roster)
- Integration tests for workspace isolation (create, populate, archive)
- Integration tests for depth enforcement (session-guard rejects depth 3+)
- Integration tests for communication protocol (inbox write, outbox read, collection)
- **Dry-run mode**: Mock scripts simulate agent responses for $0 pipeline testing
- E2E test: CEO receives task → delegates to head → head spawns grunt → result flows back
- Dashboard tests: org chart renders correctly, agent detail loads, empty state works

## Verification

1. `roster` shows the live org chart in terminal
2. EMOC dashboard shows hierarchy with real-time status
3. CEO session never writes code or searches the web (only delegates)
4. Depth guard blocks grunts from spawning external sessions (enforced in parent, not child)
5. Named agents trackable: "What did Atlas do today?" returns task history
6. Budget tracked per-agent: "$2.40 spent by Atlas today across 3 tasks"
7. Spending guardrails prevent autonomous overspend
8. Dry-run mode tests full pipeline without API calls
