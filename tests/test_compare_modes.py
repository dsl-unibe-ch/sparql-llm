"""scripts/compare_modes.py: the evaluation runner curators' questions go through."""

import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "scripts"


def _load():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("compare_modes", SCRIPTS / "compare_modes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_expected_keys_take_years_numbers_and_names():
    compare = _load()
    assert compare.expected_keys("1908-09-19 / 1979-08-26") == ["1908", "1979"]
    assert compare.expected_keys("father Valtari (ent Helenius), Toimi Armas") == ["Valtari", "Helenius", "Toimi", "Armas"]
    assert compare.expected_keys("Helsingin kaupunki 171, Kansallis-Osake-Pankki 87") == [
        "171", "87", "Helsingin", "Kansallis-Osake-Pankki"
    ]


def test_questions_file_becomes_scored_items(tmp_path):
    compare = _load()
    path = tmp_path / "q.json"
    path.write_text(json.dumps([{"question": "How many?", "expected": "10,137"}]), encoding="utf-8")
    assert compare.load_questions(path) == [
        {"id": "Q01", "set": "questions", "scoring": "keys", "question": "How many?", "expected": "10,137", "keys": ["10137"]}
    ]
