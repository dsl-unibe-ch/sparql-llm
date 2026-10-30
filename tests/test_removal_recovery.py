"""A dataset removal cut short at any step finishes at the next start, on a real local Qdrant."""

import json
from pathlib import Path

import pytest

from sparql_llm import datasets
from sparql_llm.datasets import DatasetError
from sparql_llm.indexing import rebuild

from .test_crash_safety import Crash, real_qdrant  # noqa: F401
from .crash_stubs import install_stubs

A = {"id": "a", "label": "Alpha", "endpoint_url": "https://a.example/sparql"}
B = {"id": "b", "label": "Beta", "endpoint_url": "https://b.example/sparql"}


@pytest.fixture
def removable(use_projects, real_qdrant, monkeypatch: pytest.MonkeyPatch):  # noqa: F811
    """Datasets a and b, a built and unpublished, so it can be removed."""
    use_projects(A, B)
    install_stubs(monkeypatch.setattr, rebuild, "a")
    rebuild.rebuild_index_with_alias("a")
    datasets.set_published("a", False)


def _crash_once(monkeypatch: pytest.MonkeyPatch, owner, name: str) -> None:
    real = getattr(owner, name)
    armed = [True]

    def wrapper(*args, **kwargs):
        if armed[0]:
            armed[0] = False
            raise Crash
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, wrapper)


def _crash_at_intent_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    real = Path.unlink
    armed = [True]

    def unlink(self, *args, **kwargs):
        if armed[0] and self.name.startswith("removing-"):
            armed[0] = False
            raise Crash
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)


def _remove(dataset_id: str = "a") -> None:
    """What the Remove route does."""
    datasets.begin_removal(dataset_id)
    rebuild.drop_index(dataset_id)
    datasets.finish_removal(dataset_id)


def _archives(data_dir: Path) -> list[str]:
    return sorted(p.name for p in (data_dir / "removed").iterdir())


@pytest.mark.parametrize("step", ["drop_index", "shutil.move", "_write", "intent delete"])
def test_a_crash_during_removal_finishes_at_startup(step, removable, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if step == "drop_index":
        _crash_once(monkeypatch, rebuild, "drop_index")
    elif step == "shutil.move":
        _crash_once(monkeypatch, datasets.shutil, "move")
    elif step == "_write":
        _crash_once(monkeypatch, datasets, "_write")
    else:
        _crash_at_intent_delete(monkeypatch)
    with pytest.raises(Crash):
        _remove()

    rebuild.recover_after_restart()

    assert datasets.list_dataset_ids() == ["b"]
    assert not (tmp_path / "a").exists()
    archives = _archives(tmp_path)
    assert len(archives) == 1 and archives[0].startswith("a-")
    assert (tmp_path / "removed" / archives[0] / "project.json").is_file()
    assert rebuild._aliases() == set()
    assert rebuild._collections() == []
    assert datasets.pending_removals() == []
    assert not list(tmp_path.glob("removing-*.json"))


def test_recovery_of_a_removal_keeps_the_first_archive_name(removable, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    datasets.begin_removal("a")
    named = json.loads(datasets.removal_path("a").read_text(encoding="utf-8"))["archive"]
    monkeypatch.setattr(datasets, "datetime", None)  # any new name would have to come from the clock

    rebuild.recover_after_restart()

    assert (tmp_path / named / "project.json").is_file()


def test_a_failing_removal_at_startup_keeps_its_intent_and_the_app_boots(removable, monkeypatch: pytest.MonkeyPatch):
    def broken(dataset_id):
        raise RuntimeError("no")

    datasets.begin_removal("a")
    monkeypatch.setattr(rebuild, "drop_index", broken)
    rebuild.recover_after_restart()
    assert datasets.pending_removals() == ["a"]


def test_remove_dataset_keeps_its_signature_and_returns_the_archive(removable, tmp_path: Path):
    archived = datasets.remove_dataset("a")
    assert archived.parent == tmp_path / "removed"
    assert (archived / "project.json").is_file()


def test_begin_removal_refuses_a_published_dataset(removable):
    with pytest.raises(DatasetError, match="Unpublish"):
        datasets.begin_removal("b")
    assert datasets.pending_removals() == []


def test_a_second_begin_removal_reuses_the_intent(removable, monkeypatch: pytest.MonkeyPatch):
    datasets.begin_removal("a")
    first = datasets.removal_path("a").read_text(encoding="utf-8")
    monkeypatch.setattr(datasets, "datetime", None)
    datasets.begin_removal("a")
    assert datasets.removal_path("a").read_text(encoding="utf-8") == first
