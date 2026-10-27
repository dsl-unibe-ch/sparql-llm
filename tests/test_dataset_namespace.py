"""A dataset id never names a folder the installation shares: removing it archives only its own files.

Datasets live in data/<id>/ beside the vector store, the logs and the archive: a dataset
called "vectordb", once removed, would have moved the index every dataset uses.
"""

import json

import pytest

from sparql_llm.datasets import DatasetError, add_dataset, check_removable, list_dataset_ids


@pytest.mark.parametrize("dataset_id", ["vectordb", "logs", "removed"])
def test_ids_of_shared_folders_are_refused(swiss_project, dataset_id: str):
    with pytest.raises(DatasetError):
        add_dataset(dataset_id, "Innocent name")
    assert dataset_id not in list_dataset_ids()


def test_an_id_whose_folder_already_exists_is_refused(swiss_project):
    other = swiss_project.dir.parent / "ontology"
    other.mkdir()
    (other / "keep.ttl").write_text("sentinel", encoding="utf-8")
    with pytest.raises(DatasetError):
        add_dataset("ontology", "Ontology")
    assert (other / "keep.ttl").read_text(encoding="utf-8") == "sentinel"


def test_a_folder_that_is_not_the_datasets_own_is_never_archived(swiss_project):
    data_dir = swiss_project.dir.parent
    # Registered before this check existed: "vectordb" names the shared store, not a dataset.
    store = data_dir / "vectordb"
    store.mkdir()
    (store / "collection.bin").write_text("sentinel", encoding="utf-8")
    entries = json.loads((data_dir / "datasets.json").read_text(encoding="utf-8"))
    (data_dir / "datasets.json").write_text(json.dumps([*entries, {"id": "vectordb", "published": False}]), encoding="utf-8")

    with pytest.raises(DatasetError):
        check_removable("vectordb")
    assert (store / "collection.bin").read_text(encoding="utf-8") == "sentinel"
