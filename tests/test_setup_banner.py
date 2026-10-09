"""The API-key setup banner must start hidden; the page script shows it only when setup is needed."""

import re
from pathlib import Path

INDEX = Path(__file__).parent.parent / "src" / "sparql_llm" / "agent" / "webapp" / "index.html"


def test_setup_banner_is_hidden_until_the_settings_check_says_otherwise():
    # A later "display: flex" in the same inline style overrode "display: none", so the
    # banner flashed on every load until /api settings answered.
    style = re.search(r'id="setup-banner"\s+style="([^"]*)"', INDEX.read_text(encoding="utf-8")).group(1)
    displays = re.findall(r"display:\s*([\w-]+)", style)
    assert displays == ["none"]
