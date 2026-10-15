"""Fetch GND records from lobid.org (one request per ID)."""

import json

import requests

from . import _util

LOBID_URL = "https://lobid.org/gnd/{}.json"


def fetch_lobid_record(gnd_id: str, session: requests.Session) -> dict:
    """Fetch one GND record. Redirects are NOT followed.

    - 404      -> {"requested": id, "missing": True}
    - redirect -> the record was merged into another one:
                  {"requested": id, "current": new_id, "merged_into": new_id}
    - 200      -> full record (name, birth, death, raw JSON)
    """
    req = session.get(
        LOBID_URL.format(gnd_id),
        allow_redirects=False,
        headers={"User-Agent": _util.USER_AGENT, "Accept": "application/json"},
        timeout=60,
    )
    if req.status_code == 404:
        return {"requested": gnd_id, "missing": True}
    if req.is_redirect:
        location = req.headers.get("Location", "")
        new_id = location.rstrip("/").split("/")[-1].split("?")[0]
        return {"requested": gnd_id, "current": new_id, "merged_into": new_id}
    req.raise_for_status()
    d = req.json()
    return {
        "requested": gnd_id,
        "current": d.get("gndIdentifier", gnd_id),
        "merged_into": None,
        "name": d.get("preferredName", ""),
        "birth": _util.year((d.get("dateOfBirth") or [""])[0]),
        "death": _util.year((d.get("dateOfDeath") or [""])[0]),
        "raw": json.dumps(d, ensure_ascii=False),
        "deprecated": [u.rsplit("/", 1)[-1] for u in d.get("deprecatedUri", [])],
    }


def resolve_records(gnd_ids: list[str], session: requests.Session) -> list[dict]:
    """Return one full record per *current* GND ID.

    Old IDs are replaced by the ID they were merged into, missing IDs are
    dropped, duplicates are removed. Each ID is requested at most once.
    """
    cache: dict[str, dict] = {}

    def fetch(g: str) -> dict:
        if g not in cache:
            cache[g] = fetch_lobid_record(g, session)
        return cache[g]

    current: list[str] = []
    for g in gnd_ids:
        rec = fetch(g)
        if not rec.get("missing") and rec["current"] not in current:
            current.append(rec["current"])
    return [fetch(g) for g in current]