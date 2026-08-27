"""F20: cross-process read-modify-write serialization for .agent-config.json.

Covers ``update_agent_config``:
  * concurrent in-process threads each adding a distinct key — no lost update
  * concurrent *processes* (multiprocessing fork) each adding a key — flock
    serializes the RMW across the process boundary
  * an exception inside the RMW body aborts the write (file untouched)
  * a missing on-disk config aborts the RMW instead of writing a defaults blob
"""

import multiprocessing
import os
import threading

from hermes_trader.agents import config_store
from hermes_trader.agents.config_store import (
    read_agent_config,
    update_agent_config,
    write_agent_config,
)


def test_update_config_rmw_keeps_both_keys_in_threads():
    """Two threads doing read-modify-write of different keys must both land:
    with flock held across the whole RMW, the second thread reads the first
    thread's merged result before writing."""
    cfg = read_agent_config()
    cfg["rmw_thread_seed"] = 1
    write_agent_config(cfg, backup=False)

    def _set_key(key: str) -> None:
        with update_agent_config(backup=False) as c:
            c[key] = True

    t1 = threading.Thread(target=_set_key, args=("rmw_thread_key_a",))
    t2 = threading.Thread(target=_set_key, args=("rmw_thread_key_b",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    final = read_agent_config()
    assert final.get("rmw_thread_key_a") is True
    assert final.get("rmw_thread_key_b") is True
    assert final.get("rmw_thread_seed") == 1


def _proc_set_key(key: str) -> None:
    """Worker for the multiprocessing test: module-level so fork can run it."""
    from hermes_trader.agents.config_store import update_agent_config
    with update_agent_config(backup=False) as c:
        c[key] = True


def test_update_config_rmw_keeps_all_keys_cross_process():
    """Fork 5 processes that each RMW a distinct key onto the same config
    file. flock (not threading.Lock) is what serializes across the process
    boundary, so all 5 keys must survive."""
    cfg = read_agent_config()
    cfg["rmw_proc_seed"] = 1
    write_agent_config(cfg, backup=False)

    ctx = multiprocessing.get_context("fork")
    procs = [
        ctx.Process(target=_proc_set_key, args=(f"rmw_proc_key_{i}",))
        for i in range(5)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    assert all(p.exitcode == 0 for p in procs)

    final = read_agent_config()
    for i in range(5):
        assert final.get(f"rmw_proc_key_{i}") is True
    assert final.get("rmw_proc_seed") == 1


def test_update_config_aborts_on_body_exception():
    """If the RMW body raises, nothing must be written: the on-disk file
    stays at its pre-RMW state and the exception propagates."""
    cfg = read_agent_config()
    cfg["rmw_abort_seed"] = "before"
    write_agent_config(cfg, backup=False)

    try:
        with update_agent_config(backup=False) as c:
            c["rmw_abort_seed"] = "after"
            c["rmw_abort_should_not_persist"] = True
            raise RuntimeError("boom from RMW body")
    except RuntimeError:
        pass
    else:  # pragma: no cover — the body above always raises
        raise AssertionError("body exception must propagate")

    final = read_agent_config()
    assert final.get("rmw_abort_seed") == "before"
    assert "rmw_abort_should_not_persist" not in final


def test_update_config_refuses_to_create_on_missing_file():
    """A missing config file aborts the RMW (RuntimeError) rather than
    silently writing a defaults blob — operators must investigate a
    missing/corrupt file, not have it overwritten."""
    path = config_store.CONFIG_PATH
    assert os.path.exists(path)
    os.remove(path)
    try:
        raised = False
        try:
            with update_agent_config(backup=False) as c:
                c["rmw_missing_should_not_persist"] = True
        except RuntimeError:
            raised = True
        assert raised
        assert not os.path.exists(path)
    finally:
        # Restore a valid config so later tests have a file to read/write.
        write_agent_config(read_agent_config(), backup=False)
