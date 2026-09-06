"""INV-01: ``domain/`` is a pure function library.

This is the invariant everything downstream depends on -- replay determinism,
counterfactual eval runs, the nine-second corpus pass -- and it is the only one
that cannot be added later without a rewrite. So its test ships before most of
the code it guards.

It is also trivially violated by an innocent ``datetime.now()``, which is why
this is an AST scan and not a code review convention.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "incidentpilot"
DOMAIN = SRC / "domain"

# Anything that reaches the network, the disk, a database, a clock, or an
# entropy source. `random` and `uuid` sit alongside the I/O modules because they
# are far easier to add accidentally than an HTTP client, and they break replay
# determinism just as completely.
FORBIDDEN_MODULES: frozenset[str] = frozenset(
    {
        "asyncio",
        "httpx",
        "requests",
        "urllib",
        "socket",
        "sqlalchemy",
        "redis",
        "psycopg",
        "slack_sdk",
        "slack_bolt",
        "openai",
        "anthropic",
        "prometheus_client",
        "structlog",
        "logging",
        "random",
        "uuid",
        "os",
        "pathlib",
        "subprocess",
        "threading",
    }
)

# Packages inside the application that domain/ must never reach into. The
# dependency arrow points one way: everything may import domain/, domain/ may
# import nothing.
FORBIDDEN_INTERNAL: frozenset[str] = frozenset(
    {
        "incidentpilot.db",
        "incidentpilot.adapters",
        "incidentpilot.api",
        "incidentpilot.orchestration",
        "incidentpilot.telemetry",
        "incidentpilot.transcript",
        "incidentpilot.impact",
        "incidentpilot.pir",
        "incidentpilot.resilience",
        "incidentpilot.privacy",
        "incidentpilot.runbooks",
        "incidentpilot.config",
    }
)

# (module, attribute) pairs that read the wall clock.
FORBIDDEN_CALLS: frozenset[tuple[str, str]] = frozenset(
    {
        ("datetime", "now"),
        ("datetime", "utcnow"),
        ("datetime", "today"),
        ("time", "time"),
        ("time", "monotonic"),
        ("time", "time_ns"),
    }
)

# Builtins that reach the outside world without importing anything. The import
# scan cannot see these -- `open("service-graph.yaml")` needs no import at all,
# and it is exactly the shortcut someone takes when adding a config loader to a
# domain module in a hurry.
FORBIDDEN_BUILTINS: frozenset[str] = frozenset({"open", "input", "eval", "exec", "compile"})


def _domain_files() -> list[Path]:
    return sorted(p for p in DOMAIN.rglob("*.py") if p.name != "__init__.py")


def _root(name: str | None) -> str:
    return (name or "").split(".")[0]


def test_domain_directory_exists() -> None:
    """A passing purity test over zero files would be a false negative."""
    assert DOMAIN.is_dir(), f"domain package missing at {DOMAIN}"
    assert _domain_files(), "no modules found in domain/ -- purity test is vacuous"


def test_domain_has_no_impure_imports() -> None:
    violations: list[str] = []
    for path in _domain_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _root(alias.name) in FORBIDDEN_MODULES:
                        violations.append(f"{path.name}:{node.lineno} import {alias.name}")
                    if any(alias.name.startswith(p) for p in FORBIDDEN_INTERNAL):
                        violations.append(f"{path.name}:{node.lineno} import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:  # relative import: stays inside domain/, fine
                    continue
                if _root(module) in FORBIDDEN_MODULES:
                    violations.append(f"{path.name}:{node.lineno} from {module}")
                if any(module.startswith(p) for p in FORBIDDEN_INTERNAL):
                    violations.append(f"{path.name}:{node.lineno} from {module}")
    assert not violations, "impure imports in domain/:\n  " + "\n  ".join(violations)


def test_domain_never_reads_the_clock() -> None:
    """A clock in domain/ is the first cause of flaky replay.

    Time is injected: windows and durations are computed by the caller (or by
    SQL) and handed in as data.
    """
    violations: list[str] = []
    for path in _domain_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            owner = func.value
            owner_name = (
                owner.id
                if isinstance(owner, ast.Name)
                else (owner.attr if isinstance(owner, ast.Attribute) else None)
            )
            if owner_name and (owner_name, func.attr) in FORBIDDEN_CALLS:
                violations.append(f"{path.name}:{node.lineno} {owner_name}.{func.attr}()")
    assert not violations, "clock access in domain/:\n  " + "\n  ".join(violations)


def test_domain_never_calls_an_io_builtin() -> None:
    """`open()` imports nothing, so the import scan cannot see it.

    This is the shortcut someone takes when a domain module needs config: read
    the YAML right here. The loader belongs outside domain/, and this test is
    what says so at the moment it happens rather than in review three days later.
    """
    violations: list[str] = []
    for path in _domain_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in FORBIDDEN_BUILTINS
            ):
                violations.append(f"{path.name}:{node.lineno} {node.func.id}()")
    assert not violations, "I/O builtin used in domain/:\n  " + "\n  ".join(violations)


def test_domain_declares_no_module_level_mutable_state() -> None:
    """Module-level mutable state makes a pure module quietly stateful.

    Frozen constants are fine and are the point (STABLE_LABELS, SEVERITY_RANK).
    What is not fine is a module-level list or dict that something appends to,
    because then import order changes behaviour and replay stops being
    reproducible.
    """
    violations: list[str] = []
    for path in _domain_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                if target.id.isupper():
                    continue  # UPPER_CASE is the constant convention
                violations.append(f"{path.name}:{node.lineno} module-level binding {target.id!r}")
    assert not violations, "non-constant module-level state in domain/:\n  " + "\n  ".join(
        violations
    )
