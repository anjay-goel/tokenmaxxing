from tokenmaxxing.ingest.claude.reconcile import _rebuild_messages
from tokenmaxxing.ingest.codex import reconcile as codex_reconcile
from tokenmaxxing.ingest.opencode import recover_opencode_history
from tokenmaxxing.ingest.pi.reconcile import _rebuild_events
from tokenmaxxing.ingest.pi.subagents import _repair_subagent_runs
from tokenmaxxing.models import Source
from tokenmaxxing.repository import Repository


def rebuild_history(repository: Repository, sources: tuple[Source, ...]) -> None:
    """Reconstruct retained usage atomically, without requiring harness files."""
    with repository.transaction() as connection:
        for source in sources:
            if source == "codex":
                codex_reconcile.rebuild_codex_history(repository)
            elif source == "claude":
                messages = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT DISTINCT source_turn_id FROM observations "
                        "WHERE source='claude' AND source_turn_id IS NOT NULL"
                    )
                }
                _rebuild_messages(repository, messages)
            elif source == "pi":
                _repair_subagent_runs(repository, {})
                events = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT DISTINCT source_turn_id FROM observations "
                        "WHERE source='pi' AND source_turn_id IS NOT NULL"
                    )
                }
                _rebuild_events(repository, events)
            else:
                recover_opencode_history(repository)
