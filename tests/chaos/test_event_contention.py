from __future__ import annotations

import threading

from forgeflow.domain.enums import EventType, TaskStatus
from forgeflow.state.store import Store
from tests.conftest import calc_spec


def test_concurrent_event_appends_have_no_sequence_gaps(project) -> None:
    store = Store(project)
    goal = store.create_goal(calc_spec())
    start = threading.Barrier(8)
    errors: list[BaseException] = []

    def append(index: int) -> None:
        try:
            start.wait()
            store.append_event(
                goal.id,
                EventType.CHECKPOINT_CREATED,
                {"thread": index},
                actor=f"worker-{index}",
            )
        except BaseException as exc:  # pragma: no cover - failure should surface below
            errors.append(exc)

    threads = [threading.Thread(target=append, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    events = store.list_events(goal.id)
    sequences = [event.sequence for event in events]
    assert sequences == list(range(1, len(sequences) + 1))
    assert len(sequences) == len(set(sequences))


def test_failed_state_write_does_not_leave_attempt_or_claim(project, monkeypatch) -> None:
    store = Store(project)
    goal = store.create_goal(calc_spec())

    def explode(*args, **kwargs):
        raise RuntimeError("injected crash after task update, before event commit")

    monkeypatch.setattr(store, "_append_event", explode)
    try:
        store.claim_task(goal.id, owner="chaos", lease_seconds=30)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected injected failure")
    task = store.get_task(goal.id, "fix-add")
    assert task.status == TaskStatus.READY
    assert task.attempts_count == 0
    assert store.list_attempts(goal.id) == []
