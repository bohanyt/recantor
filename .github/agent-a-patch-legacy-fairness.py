from pathlib import Path

path = Path("apps/api/tests/test_stt_jobs.py")
text = path.read_text()
old = """    assert second.dispatched == 3
    assert b_work.id in second_dispatch
    assert len(set(second_dispatch).intersection(a_ids)) == 2
"""
new = """    # Session A already owns both outstanding admission slots from pass 1.
    # Only newly-active session B may publish until those expiring hints clear or claim.
    assert second.dispatched == 1
    assert second_dispatch == [b_work.id]
    assert len(set(second_dispatch).intersection(a_ids)) == 0
"""
if old not in text:
    raise SystemExit("legacy fairness assertion block not found")
path.write_text(text.replace(old, new, 1))
