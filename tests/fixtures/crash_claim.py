from __future__ import annotations

import os
import sys
from pathlib import Path

from forgeflow.state.store import Store


def main() -> None:
    workspace = Path(sys.argv[1]).resolve()
    goal_id = sys.argv[2]
    lease_seconds = float(sys.argv[3])
    store = Store(workspace)
    claim = store.claim_task(goal_id, owner="crash-worker", lease_seconds=lease_seconds)
    print(claim.attempt.id, flush=True)
    os._exit(17)


if __name__ == "__main__":
    main()
