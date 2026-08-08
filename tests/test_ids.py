from __future__ import annotations

from agent_bridge import ids


def test_ids_are_prefixed_and_well_formed() -> None:
    message_id = ids.new_message_id()
    thread_id = ids.new_thread_id()

    assert ids.looks_like_id(message_id, ids.MESSAGE_PREFIX)
    assert ids.looks_like_id(thread_id, ids.THREAD_PREFIX)
    assert not ids.looks_like_id(message_id, ids.THREAD_PREFIX)
    assert not ids.looks_like_id("msg_short", ids.MESSAGE_PREFIX)
    assert not ids.looks_like_id("msg_" + "u" * 26, ids.MESSAGE_PREFIX)  # 'U' is not in Crockford


def test_ids_are_unique_and_time_sortable() -> None:
    generated = [ids.new_message_id() for _ in range(2000)]
    assert len(set(generated)) == len(generated)
    # The timestamp prefix is monotonic, so a batch sorts into creation order.
    assert [g[:14] for g in generated] == sorted(g[:14] for g in generated)
