"""Maintenance: memory TTL cleanup + log rotation.

Designed to run as a daily cron job. Idempotent -- safe to run multiple times.

Usage:
    python3 maintenance.py [--memory-ttl DAYS] [--log-retention DAYS]
"""

import glob
import logging
import os
import sys
from datetime import datetime, timezone

from memory import cleanup_expired_memories, DEFAULT_BASE_PATH, DEFAULT_TTL_DAYS

logger = logging.getLogger(__name__)

DEFAULT_LOG_DIR = os.environ.get(
    "ORCHESTRATED_ROOT",
    os.path.expanduser("~/projects/_orchestrated"),
)
DEFAULT_RETENTION_DAYS = 30


def rotate_event_log(
    log_dir: str = DEFAULT_LOG_DIR,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> None:
    """Rotate events.jsonl:

    1. Rename events.jsonl to events-YYYY-MM-DD.jsonl
    2. Create fresh empty events.jsonl
    3. Delete rotated logs older than retention_days
    """
    events_path = os.path.join(log_dir, "events.jsonl")

    if not os.path.exists(events_path):
        logger.info("No events.jsonl to rotate")
        return

    # Only rotate if the file has content
    if os.path.getsize(events_path) == 0:
        logger.info("events.jsonl is empty, skipping rotation")
        return

    # Determine rotated filename
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rotated_name = f"events-{date_str}.jsonl"
    rotated_path = os.path.join(log_dir, rotated_name)

    # Handle duplicate rotation (same day)
    if os.path.exists(rotated_path):
        counter = 1
        while os.path.exists(rotated_path):
            rotated_name = f"events-{date_str}-{counter}.jsonl"
            rotated_path = os.path.join(log_dir, rotated_name)
            counter += 1

    # Rename current to dated
    os.rename(events_path, rotated_path)
    logger.info("Rotated events.jsonl to %s", rotated_name)

    # Create fresh empty file
    with open(events_path, "w") as f:
        pass

    # Clean old rotated logs
    import time
    cutoff = time.time() - (retention_days * 86400)
    for path in glob.glob(os.path.join(log_dir, "events-*.jsonl")):
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                logger.info("Deleted old rotated log: %s", os.path.basename(path))
        except OSError as e:
            logger.warning("Failed to delete %s: %s", path, e)


def run_maintenance(
    memory_ttl_days: int = DEFAULT_TTL_DAYS,
    log_retention_days: int = DEFAULT_RETENTION_DAYS,
    memory_base_path: str = DEFAULT_BASE_PATH,
    log_dir: str = DEFAULT_LOG_DIR,
) -> None:
    """Run all maintenance tasks: memory cleanup + log rotation."""
    logger.info("Running maintenance: memory_ttl=%dd, log_retention=%dd",
                memory_ttl_days, log_retention_days)

    # Memory TTL cleanup
    removed = cleanup_expired_memories(memory_ttl_days, memory_base_path)
    if removed:
        logger.info("Removed %d expired memory files", len(removed))

    # Log rotation
    rotate_event_log(log_dir, log_retention_days)

    logger.info("Maintenance complete")


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="Orchestrator maintenance")
    parser.add_argument("--memory-ttl", type=int, default=DEFAULT_TTL_DAYS)
    parser.add_argument("--log-retention", type=int, default=DEFAULT_RETENTION_DAYS)
    args = parser.parse_args()

    run_maintenance(args.memory_ttl, args.log_retention)
