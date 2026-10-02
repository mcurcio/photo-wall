"""A test observes lock waits only in its own database.

`pg_locks` and `pg_stat_activity` are cluster-wide. Under xdist every worker's tests share the
server, so an unscoped "is anything waiting?" also counts other tests' waiters: an assertion
that this test's session waited then passes although it never did. Every query of those views
in the suite must be scoped (to a pid, a blocker's pids, or a database by `datname`);
`support.database.waiting_backends` is the scoped way to count waiters.
"""

import ast
import re
from pathlib import Path

TESTS = Path(__file__).parent
VIEWS = re.compile(r"\b(FROM|JOIN)\s+pg_(locks|stat_activity)\b", re.IGNORECASE)
SCOPED = re.compile(r"\bpid\s*=|pg_blocking_pids\(|\bdatname\s*=")


def unscoped_queries(source: str) -> list[int]:
    """Line numbers of string literals that read the lock views without a scope."""
    return [node.lineno for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and VIEWS.search(node.value) and not SCOPED.search(node.value)]


def test_every_lock_view_query_is_scoped_to_its_test():
    offenders = {str(path.relative_to(TESTS)): lines
                 for path in sorted(TESTS.rglob("*.py")) if path != Path(__file__)
                 if (lines := unscoped_queries(path.read_text()))}
    assert offenders == {}


def test_the_check_refuses_a_cluster_wide_count():
    assert unscoped_queries('q = "SELECT count(*) FROM pg_locks WHERE NOT granted"') == [1]
    assert unscoped_queries('q = "SELECT pid FROM pg_stat_activity WHERE application_name=%s"'
                            ) == [1]
    assert unscoped_queries('q = "SELECT 1 FROM pg_stat_activity WHERE pid=%s"') == []
