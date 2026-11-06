"""The per-source docs pages (#268): site/web/sources/sources.json names every published
table exactly once, and every source in it has a page and a sidebar entry.

publish.TABLES is the truth for what the release publishes; the registry is what the
pages render from (source-facts.js, against the live manifest). Offline."""

import json
from pathlib import Path

from bioconice import publish

WEB = Path(__file__).parent.parent / "site" / "web"
REGISTRY = {k: v for k, v in json.loads((WEB / "sources" / "sources.json").read_text()).items()
            if not k.startswith("_")}


def test_registry_names_every_published_table_once():
    published = {f"{s}.{t}" for s, t, _ in publish.TABLES} - {"provenance.release"}
    own = [t for s in REGISTRY.values() for t in s["tables"]]
    shared = {t for s in REGISTRY.values() for t in s["shared"]}
    assert len(own) == len(set(own)), "a table on two sources' own lists"
    assert not shared & set(own), "a table both owned and shared"
    assert set(own) | shared == published
    for name in shared:
        assert sum(name in s["shared"] for s in REGISTRY.values()) >= 2, name


def test_every_source_has_a_page_and_a_sidebar_entry():
    quarto = (WEB / "_quarto.yml").read_text()
    for key in REGISTRY:
        page = WEB / "sources" / f"{key}.qmd"
        assert page.exists(), key
        assert f'data-source="{key}"' in page.read_text(), key
        assert f"sources/{key}.qmd" in quarto, key
