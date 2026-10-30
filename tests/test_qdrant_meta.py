"""Qdrant's local mode rewrites meta.json in place: a kill can truncate it, and every index would go with it."""

import json
from pathlib import Path

from qdrant_client import QdrantClient

from sparql_llm.indexing.qdrant_meta import repair_meta, snapshot_meta

VALID = json.dumps({"collections": {"a__v1": {}}, "aliases": {"a__docs": "a__v1"}})
OLDER = json.dumps({"collections": {}, "aliases": {}})


def _vectordb(tmp_path: Path, meta: str | None = VALID, bak: str | None = None) -> Path:
    vectordb = tmp_path / "vectordb"
    vectordb.mkdir()
    if meta is not None:
        (vectordb / "meta.json").write_text(meta, encoding="utf-8")
    if bak is not None:
        (vectordb / "meta.json.bak").write_text(bak, encoding="utf-8")
    return vectordb


def test_snapshot_copies_a_valid_meta(tmp_path):
    vectordb = _vectordb(tmp_path)
    assert snapshot_meta(vectordb) is True
    assert (vectordb / "meta.json.bak").read_text(encoding="utf-8") == VALID


def test_snapshot_refuses_a_damaged_meta(tmp_path):
    vectordb = _vectordb(tmp_path, meta='{"collections"', bak=OLDER)
    assert snapshot_meta(vectordb) is False
    assert (vectordb / "meta.json.bak").read_text(encoding="utf-8") == OLDER


def test_repair_leaves_a_valid_meta_alone(tmp_path):
    vectordb = _vectordb(tmp_path, bak=OLDER)
    assert repair_meta(vectordb) == "ok"
    assert (vectordb / "meta.json").read_text(encoding="utf-8") == VALID
    assert (vectordb / "meta.json.bak").read_text(encoding="utf-8") == OLDER


def test_repair_restores_a_truncated_meta(tmp_path):
    vectordb = _vectordb(tmp_path, meta="", bak=VALID)
    assert repair_meta(vectordb) == "restored"
    assert (vectordb / "meta.json").read_text(encoding="utf-8") == VALID


def test_repair_restores_a_missing_meta_from_backup(tmp_path):
    vectordb = _vectordb(tmp_path, meta=None, bak=VALID)
    assert repair_meta(vectordb) == "restored"
    assert (vectordb / "meta.json").read_text(encoding="utf-8") == VALID


def test_repair_treats_a_meta_without_the_two_maps_as_damaged(tmp_path):
    vectordb = _vectordb(tmp_path, meta='{"collections": []}', bak=VALID)
    assert repair_meta(vectordb) == "restored"


def test_repair_without_backup_moves_it_aside(tmp_path):
    vectordb = _vectordb(tmp_path, meta="garbage")
    assert repair_meta(vectordb) == "moved-aside"
    assert (vectordb / "meta.json.damaged").read_text(encoding="utf-8") == "garbage"
    assert not (vectordb / "meta.json").exists()


def test_repair_ignores_a_damaged_backup(tmp_path):
    vectordb = _vectordb(tmp_path, meta="garbage", bak="also garbage")
    assert repair_meta(vectordb) == "moved-aside"


def test_repair_on_an_empty_install_does_nothing(tmp_path):
    vectordb = tmp_path / "vectordb"
    assert repair_meta(vectordb) == "ok"
    assert not vectordb.exists()


def test_a_client_opens_after_moving_aside(tmp_path):
    vectordb = _vectordb(tmp_path, meta="garbage")
    repair_meta(vectordb)
    client = QdrantClient(path=str(vectordb))
    try:
        assert client.get_collections().collections == []
    finally:
        client.close()
