"""The rebuild lock belongs to its job: never taken from a live one, cleared when a restart killed it.

The app runs one worker: at startup no rebuild can be running, so a lock or a "running"
job left on disk is what a kill (the deploy's pkill, a crash) left behind.
"""

import os
import time

import pytest
from starlette.testclient import TestClient

from sparql_llm.indexing import rebuild

A = {"id": "a", "label": "Alpha", "endpoint_url": "https://a.example/sparql"}


@pytest.fixture
def a(use_projects):
    use_projects(A)
    yield
    rebuild.lock_file().unlink(missing_ok=True)


def test_an_old_lock_of_a_live_job_is_not_taken(a):
    assert rebuild._acquire_lock("a")
    old = time.time() - 2 * 3600
    os.utime(rebuild.lock_file(), (old, old))
    assert not rebuild._acquire_lock("a")


def test_only_the_owner_releases_the_lock(a):
    token = rebuild._acquire_lock("a")
    rebuild._release_lock("someone-else")
    assert rebuild.lock_file().exists()
    rebuild._release_lock(token)
    assert not rebuild.lock_file().exists()


def test_a_restart_clears_the_dead_lock_and_says_the_job_was_interrupted(a):
    rebuild._acquire_lock("a")
    rebuild._set_job("a", "running", "Indexing…")

    rebuild.recover_after_restart()

    assert not rebuild.lock_file().exists()
    job = rebuild.read_job("a")
    assert job["state"] == "failed"
    assert "interrupted" in job["message"].lower()
    assert rebuild._acquire_lock("a")


def test_app_startup_recovers(a):
    from sparql_llm.agent import main

    rebuild._acquire_lock("a")
    with TestClient(main.app):
        pass
    assert not rebuild.lock_file().exists()


def test_a_start_that_fails_releases_the_lock(a, monkeypatch: pytest.MonkeyPatch):
    def full_disk(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(rebuild, "_set_job", full_disk)
    with pytest.raises(OSError):
        rebuild.start_rebuild("a")
    assert not rebuild.lock_file().exists()


def test_a_lock_that_cannot_be_written_leaves_no_file(a, monkeypatch: pytest.MonkeyPatch):
    def full_disk(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(rebuild.os, "write", full_disk)
    with pytest.raises(OSError):
        rebuild._acquire_lock("a")
    assert not rebuild.lock_file().exists()
