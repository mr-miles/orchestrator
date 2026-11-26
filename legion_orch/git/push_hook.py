"""The pre-receive hook's check of a push's paths (push_scope.py): ``LEGION_SCOPE`` the scope,
the ``old new ref`` lines on stdin; prints what is refused – git shows it to the pusher – and
exits 1 if anything is. A module of its own so ``python -m`` runs it without a warning (the
package imports push_scope)."""
from __future__ import annotations

import os
import sys

from .push_scope import PushScope, check


def main() -> int:
    scope = PushScope.from_json(os.environ["LEGION_SCOPE"])
    updates = [tuple(line.split()) for line in sys.stdin if line.strip()]
    if refused := check(scope, updates):            # type: ignore[arg-type]
        print("legion: push refused – it changes paths outside this run's scope:",
              *refused, sep="\n", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
