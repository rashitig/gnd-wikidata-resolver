"""Resolve Wikidata Q-IDs to GND IDs."""

from functools import cache

from ._util import set_user_agent
from .classify import Decision, classify
from .lobid import lobid, resolve_records
from .wikidata import fetch_item, make_session, qids_for_gnd_ids

__all__ = [
    "Decision",
    "classify_item",
    "get_gnd_id_by_wikidata_id",
    "get_wikidata_id_by_gnd_id",
    "set_user_agent",
]


@cache
def _resolve_qid(qid: str) -> Decision:
    """Query Wikidata and lobid once per Q-ID and classify the result."""
    session = make_session()
    item = fetch_item(qid, session)  # Item: qid, label, birth, death, gnd_ids
    records = resolve_records(
        item.gnd_ids, session
    )  # list of lobid records, current IDs only
    return classify(item, records)  # fetches redirect targets once


def get_gnd_id_by_wikidata_id(qid: str) -> list[str]:
    """Current GND IDs of a Wikidata item (old IDs resolved via lobid)."""
    return {qid: list(_resolve_qid(qid).gnd_ids)}


def classify_item(qid: str) -> Decision:
    """GND IDs plus the decision: ok / merged / pseudonyms / ..."""
    return _resolve_qid(qid)


def get_wikidata_id_by_gnd_id(gnd_id: str) -> list[str]:
    """Q-IDs of all Wikidata items linked to this GND ID, including items
    that still store an old (merged) or the current version of it."""
    session = make_session()
    candidates = [gnd_id]

    rec = lobid(gnd_id, session)
    if rec.get("merged_into"):  # old ID -> follow the redirect
        candidates.append(rec["merged_into"])
        rec = lobid(rec["merged_into"], session)
    if not rec.get("missing"):  # current record -> add its old IDs
        candidates.append(rec["current"])
        candidates.extend(rec.get("deprecated", []))
    return {gnd_id: qids_for_gnd_ids(list(dict.fromkeys(candidates)), session)}
