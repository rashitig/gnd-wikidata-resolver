import pytest

import gnd_wikidata_resolver as pkg
from gnd_wikidata_resolver.wikidata import Item


def record(gnd, name="X, Y", **extra):
    return {"requested": gnd, "current": gnd, "merged_into": None,
            "name": name, "birth": None, "death": None, "raw": "", **extra}


LOBID_DB = {
    "OLD": {"requested": "OLD", "current": "NEW", "merged_into": "NEW"},  # merged
    "NEW": record("NEW", deprecated=["OLDER"]),
    "1": record("1"),
}


@pytest.fixture(autouse=True)
def clear_cache():
    pkg._resolve_qid.cache_clear()
    yield
    pkg._resolve_qid.cache_clear()


@pytest.fixture
def lobid_calls(monkeypatch):
    """Fake lobid for every place the package calls it; returns the list of requested IDs."""
    calls = []

    def fake(gnd, session):
        calls.append(gnd)
        return LOBID_DB[gnd]            # KeyError = unexpected request

    monkeypatch.setattr("gnd_wikidata_resolver.lobid.fetch_lobid_record", fake)
    monkeypatch.setattr(pkg, "fetch_lobid_record", fake)
    return calls


@pytest.fixture
def use_item(monkeypatch):
    """use_item("1", "2") makes Wikidata return Q1 with these GND IDs; returns the request log."""
    def _use(*gnd_ids):
        calls = []

        def fake(qid, session):
            calls.append(qid)
            return Item("Q1", "Y X", gnd_ids=list(gnd_ids))

        monkeypatch.setattr(pkg, "fetch_item", fake)
        return calls
    return _use


def test_old_id_is_resolved_and_each_id_fetched_once(use_item, lobid_calls):
    use_item("OLD", "NEW")
    assert pkg.get_gnd_id_by_wikidata_id("Q1") == {"Q1": ["NEW"]}
    assert sorted(lobid_calls) == ["NEW", "OLD"]


def test_second_call_for_same_qid_makes_no_new_requests(use_item, lobid_calls):
    item_calls = use_item("1")
    pkg.get_gnd_id_by_wikidata_id("Q1")
    pkg.get_gnd_id_by_wikidata_id("Q1")
    pkg.classify_item("Q1")                  # same cache entry
    assert item_calls == ["Q1"]
    assert lobid_calls == ["1"]


def test_item_without_gnd_ids_returns_empty_list(use_item, lobid_calls):
    use_item()
    assert pkg.get_gnd_id_by_wikidata_id("Q1") == {"Q1": []}
    assert lobid_calls == []


def test_get_wikidata_id_by_gnd_id_follows_redirect_and_adds_old_ids(monkeypatch, lobid_calls):
    seen = {}
    monkeypatch.setattr(pkg, "qids_for_gnd_ids",
                        lambda ids, session: seen.setdefault("ids", ids) and ["Q42"])
    assert pkg.get_wikidata_id_by_gnd_id("OLD") == {"OLD": ["Q42"]}
    assert seen["ids"] == ["OLD", "NEW", "OLDER"]