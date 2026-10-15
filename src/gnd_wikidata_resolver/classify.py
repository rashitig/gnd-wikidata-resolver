"""Decide what to do when a Wikidata item has several GND IDs."""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

from .wikidata import Item

Case = Literal["ok", "merged", "pseudonyms", "different_persons", "duplicate", "unsure"]


@dataclass(frozen=True)
class Decision:
    qid: str
    gnd_ids: list[str]  # current IDs (old ones already resolved)
    label: str  # Wikidata label of the item
    case: Case
    note: str = ""
    keep: list[str] = field(default_factory=list)  # only for "different_persons"
    remove: list[str] = field(default_factory=list)  # only for "different_persons"


def _tokens(name: str) -> set[str]:
    ascii_name = (
        unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    )
    return set(re.sub(r"[^a-z ]", " ", ascii_name.lower()).split())


def same_name(a: str, b: str) -> bool:
    """>= 60% of the tokens of the shorter name occur in the other
    (think "Engel, Rudolf Theophil" and "Engel, Theophil")."""
    ta, tb = _tokens(a), _tokens(b)
    return bool(ta and tb) and len(ta & tb) / min(len(ta), len(tb)) >= 0.6


def _points_to_as_pseudonym(rec: dict, other: dict) -> bool:
    raw = rec.get("raw", "")
    return (
        other["current"] in raw
        and re.search(r"seudonym", raw, re.IGNORECASE) is not None
    )


def _date_score(wikidata_year, gnd_year) -> int:
    if not wikidata_year or not gnd_year:
        return 0  # no information, neither for nor against
    return 2 if wikidata_year == gnd_year else -2


def classify(item: Item, records: list[dict]) -> Decision:
    """:param item: the Wikidata item (label, birth, raw GND IDs).
    :param records: lobid records of the *current* GND IDs, already fetched
        and de-duplicated by the caller."""
    ids = [r["current"] for r in records]

    def decide(case: Case, note: str = "", **kw) -> Decision:
        return Decision(item.qid, ids, item.label, case, note, **kw)

    # 0) one ID left: either it was never a problem, or old IDs were merged
    if len(records) <= 1:
        if sorted(ids) != sorted(item.gnd_ids):
            return decide("merged", "Old GND ID(s) were merged; use the current ID.")
        return decide("ok")

    # 1) records refer to each other as pseudonyms -> both links can be right
    for a in records:
        for b in records:
            if a is not b and _points_to_as_pseudonym(a, b):
                return decide(
                    "pseudonyms",
                    f"{a['current']} / {b['current']} are pseudonym records "
                    f"({a['name']} | {b['name']}).",
                )

    # 2) different birth/death years -> different people, one link is wrong
    births = {r["birth"] for r in records if r["birth"]}
    deaths = {r["death"] for r in records if r["death"]}
    if len(births) > 1 or len(deaths) > 1:
        scored = sorted(
            (
                (
                    2 * same_name(r["name"], item.label)
                    + _date_score(item.birth, r["birth"])
                    + _date_score(item.death, r["death"]),
                    r,
                )
                for r in records
            ),
            key=lambda x: -x[0],
        )
        summary = "; ".join(
            f"{r['current']}: {r['name']} {r['birth'] or ''}-{r['death'] or ''}"
            for r in records
        )
        if scored[0][0] > scored[1][0]:
            keep = [scored[0][1]["current"]]
            remove = [r["current"] for _, r in scored[1:]]
            return decide(
                "different_persons",
                f"Different persons ({summary}); the item matches {keep[0]}.",
                keep=keep,
                remove=remove,
            )
        return decide(
            "unsure", f"Records differ ({summary}) but the item matches none or all."
        )

    # 3) same name and dates -> duplicate GND records of one person
    if all(same_name(records[0]["name"], r["name"]) for r in records[1:]):
        return decide(
            "duplicate",
            "Same name/dates in all records; probably duplicate GND records "
            "(very likely the same person). Consider reporting to the DNB.",
        )

    # 4) different names, no pseudonym link, no date conflict
    return decide(
        "unsure",
        "Different names, no pseudonym relation found ("
        + "; ".join(f"{r['current']}: {r['name']}" for r in records)
        + ").",
    )
