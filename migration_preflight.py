#!/usr/bin/env python3
"""Application-owned migration preflight for the dependency-free hello-app fixture.

The reference app has no database and therefore declares migration mode `none`.
Applications that add schema/data migrations should replace this hook with their
own non-mutating compatibility checks. Solo VPS intentionally does not implement
a generic migration engine.
"""

from __future__ import annotations

MIGRATION_MODE = "none"


def main() -> int:
    if MIGRATION_MODE != "none":
        raise RuntimeError(
            "hello-app fixture must remain database-free; replace this hook in a stateful application"
        )
    print("PASS application migration preflight")
    print(f"  migration_mode: {MIGRATION_MODE}")
    print("  database_mutation: false")
    print("  rollback_scope: container-image-only")
    print("  database_schema_rollback: false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
