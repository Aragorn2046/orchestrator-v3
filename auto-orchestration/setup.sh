#!/usr/bin/env bash
# setup.sh — Set up the Orchestrator v3 auto-orchestration for Claude Code
#
# This script:
# 1. Copies the CEO rules file into ~/.claude/rules/
# 2. Adds the SessionStart hook to settings.json (if not present)
# 3. Creates the orchestrator project directory structure
#
# Usage: bash setup.sh [--orchestrator-dir ~/projects/orchestrator]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CLAUDE_DIR="$HOME/.claude"
RULES_DIR="$CLAUDE_DIR/rules"
SETTINGS="$CLAUDE_DIR/settings.json"
ORCHESTRATOR_DIR="${1:-$REPO_DIR}"

echo "=== Orchestrator v3 — Auto-Orchestration Setup ==="
echo ""

# ── Step 1: Rules directory ──
echo "[1/4] Setting up rules directory..."
mkdir -p "$RULES_DIR"

if [ -f "$RULES_DIR/orchestrator-ceo.md" ]; then
    echo "  Rules file already exists. Backing up to orchestrator-ceo.md.bak"
    cp "$RULES_DIR/orchestrator-ceo.md" "$RULES_DIR/orchestrator-ceo.md.bak"
fi

cp "$SCRIPT_DIR/orchestrator-ceo.md" "$RULES_DIR/orchestrator-ceo.md"
echo "  Copied orchestrator-ceo.md -> $RULES_DIR/"

# ── Step 2: SessionStart hook ──
echo "[2/4] Configuring SessionStart hook..."

HOOK_COMMAND="echo \"CEO_SESSION_INFO: type=\${CLAUDE_SESSION_TYPE:-main} orchestrate=\${ORCHESTRATE_MODE:-1} agent_id=\${ORCHESTRATOR_AGENT_ID:-none}\""

if [ ! -f "$SETTINGS" ]; then
    echo "  Creating $SETTINGS..."
    cat > "$SETTINGS" << ENDJSON
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "$HOOK_COMMAND"
          }
        ]
      }
    ]
  }
}
ENDJSON
    echo "  Created settings.json with SessionStart hook."
elif grep -q "CEO_SESSION_INFO" "$SETTINGS"; then
    echo "  SessionStart hook already present. Skipping."
else
    echo "  WARNING: settings.json exists but doesn't have the CEO hook."
    echo "  You need to manually add this to your settings.json hooks.SessionStart array:"
    echo ""
    echo "    {"
    echo "      \"type\": \"command\","
    echo "      \"command\": \"$HOOK_COMMAND\""
    echo "    }"
    echo ""
    echo "  (Auto-merging JSON is fragile — doing this manually is safer.)"
fi

# ── Step 3: Project directories ──
echo "[3/4] Creating project directories..."
mkdir -p "$HOME/projects/_orchestrated"
mkdir -p "/tmp/orchestrator/jobs"
echo "  Created ~/projects/_orchestrated/ and /tmp/orchestrator/jobs/"

# ── Step 4: Verify ──
echo "[4/4] Verifying installation..."
echo ""

PASS=true

if [ -f "$RULES_DIR/orchestrator-ceo.md" ]; then
    echo "  [OK] Rules file installed"
else
    echo "  [FAIL] Rules file missing"
    PASS=false
fi

if grep -q "CEO_SESSION_INFO" "$SETTINGS" 2>/dev/null; then
    echo "  [OK] SessionStart hook configured"
else
    echo "  [WARN] SessionStart hook needs manual configuration"
fi

if [ -d "$HOME/projects/_orchestrated" ]; then
    echo "  [OK] Orchestrated projects directory exists"
else
    echo "  [FAIL] Orchestrated projects directory missing"
    PASS=false
fi

echo ""
if $PASS; then
    echo "=== Setup complete! ==="
    echo ""
    echo "Next steps:"
    echo "  1. Start a new Claude Code session"
    echo "  2. The CEO protocol activates automatically"
    echo "  3. Try: 'Research X and then build a prototype' — watch it delegate!"
    echo ""
    echo "To opt out of CEO mode for a session:"
    echo "  export ORCHESTRATE_MODE=0"
    echo ""
else
    echo "=== Setup incomplete — see errors above ==="
fi
