"""Utilities for resolving identifiers between Wikidata (Q-IDs) and GND
(Gemeinsame Normdatei) authority IDs.

Wikidata items can be linked to GND IDs via two properties:
    - P227  (GND ID)
    - P7902 (GND ID for organizations/institutions)

A single Wikidata item may reference more than one GND ID. This module
handles the common edge cases:
    - Preferred-rank statements are used when available (via the `wdt:`
      truthy prefix).
    - Old/superseded GND authority records are resolved to their current
      ID via the lobid.org redirect.
    - Pseudonym cases (distinct GND records for a real name and an alias)
      are NOT resolved automatically -- both IDs are returned as-is.
"""

import requests
import json
import re
import unicodedata

UA = {
    # "User-Agent": (
    #     "GND-Wikidata-resolver/1.0 "
    #     "(https://github.com/rashitig/gnd-wikidata-resolver; "
    #     "6xchepfl0@mozmail.com)"
    # )
    "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:76.0) Gecko/20100101 Firefox/76.0"
}
SPARQL = "https://query.wikidata.org/sparql"
LOBID = "https://lobid.org/gnd/{}.json"


def _session() -> requests.Session:
    """Create a requests session configured with automatic retries.

    :return: A session whose connections are safe to reuse.
    :rtype: requests.Session
    """
    session = requests.Session()
    retries = requests.adapters.Retry(
        total=20, backoff_factor=0.1, status_forcelist=[500, 502, 503, 504]
    )
    session.mount("http://", requests.adapters.HTTPAdapter(max_retries=retries))
    session.mount("https://", requests.adapters.HTTPAdapter(max_retries=retries))
    return session


def sparql_query_builder(query: str, session: requests.Session | None = None):
    """Run a SPARQL query against the Wikidata Query Service.

    :param query: The SPARQL query string to execute.
    :param session: Optional existing session to reuse. A new one is
        created if not provided.
    :return: A tuple of (parsed JSON results, session used), or
        (None, session) if the request failed.
    :rtype: tuple[dict | None, requests.Session]
    """
    session = session or _session()
    try:
        req = session.get(
            SPARQL,
            params={"query": query, "format": "json"},
            headers={
                "User-Agent": UA["User-Agent"],
                "Accept": "application/sparql-results+json",
            },
        )
        req.raise_for_status()
        return req.json(), session

    except requests.RequestException as e:
        print(f"Error accessing Wikidata endpoint: {e}")
    except requests.JSONDecodeError as e:
        print(f"Error decoding JSON: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")

    return None, session


def get_wikidata_id_by_gnd_id(gnd_id: str) -> dict[str, list[str]]:
    """Look up the Wikidata Q-ID(s) associated with a given GND ID.

    Checks both P227 and P7902, since either property may hold the
    GND ID depending on how the item was linked.

    :param gnd_id: The GND ID to look up.
    :return: Dictionary mapping the input GND ID to a list of matching
        Wikidata Q-IDs (usually just one, but can be more if the GND ID
        is linked from multiple items).
    :rtype: dict[str, list[str]]
    """
    query = f"""SELECT ?item WHERE {{
  VALUES ?gndProp {{ wdt:P227 wdt:P7902 }}
  ?item ?gndProp "{gnd_id}" .
}}
GROUP BY ?item"""
    results, _ = sparql_query_builder(query)
    wikidata: dict[str, list[str]] = {gnd_id: []}
    if not results:
        return wikidata

    for binding in results["results"]["bindings"]:
        value = binding["item"]["value"].split("/")[-1]
        wikidata[gnd_id].append(value)
    return wikidata


def get_gnd_id_by_wikidata_id(wikidata_id: str) -> dict:
    """Get the GND ID(s) associated with a given Wikidata Q-ID.

    If the Wikidata item has several GND IDs and none is preferred in
    the Wikidata ranking, each candidate ID is checked against the
    lobid API to resolve old/superseded authority records to their
    current ID.

    Example of an old-authority-record case:
        https://www.wikidata.org/wiki/Q312384

    Pseudonym cases are not resolved automatically, e.g.:
        https://d-nb.info/gnd/118822039
        https://d-nb.info/gnd/1067756124

    :param wikidata_id: The Wikidata Q-ID to look up.
    :return: Dictionary mapping the input Q-ID to a dict containing
        a list of associated GND IDs and label, birth, and death of the
        wikidata item.
    :rtype: dict[str, list[str]]
    """
    # The wdt: prefix returns only the preferred-ranked statement(s)
    # when a preference is set on Wikidata.
    query = f"""SELECT ?itemLabel ?gndId ?birth ?death WHERE {{
  VALUES ?gndProp {{ wdt:P227 wdt:P7902 }}
  BIND(wd:{wikidata_id} AS ?item)
  
  ?item ?gndProp ?gndId .
  
  OPTIONAL {{ ?item wdt:P569 ?birth . }}
  OPTIONAL {{ ?item wdt:P570 ?death . }}
  
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "[AUTO_LANGUAGE],de,en". }}
}}
"""
    results, session = sparql_query_builder(query)
    gnd = {
        wikidata_id: {
            "gnd_id": [],
            "wikidata_label": "",
            "wikidata_birth": None,
            "wikidata_death": None,
            "decision": "",
        }
    }
    if not results:
        return gnd

    for binding in results["results"]["bindings"]:
        value = binding["gndId"]["value"]
        gnd[wikidata_id]["gnd_id"].append(value)
        value = binding["itemLabel"]["value"]
        gnd[wikidata_id]["wikidata_label"] = value
        value = binding.get("birth")
        if value:
            gnd[wikidata_id]["wikidata_birth"] = year(value["value"])
        value = binding.get("death")
        if value:
            gnd[wikidata_id]["wikidata_death"] = year(value["value"])

    # Fetch every candidate from lobid exactly once. The same records are
    # used (a) to resolve old/superseded IDs to their current ID and
    # (b) by classify() below, so lobid is never queried twice.
    records: dict[str, dict] = {}

    def fetch(gnd_id: str) -> dict:
        if gnd_id not in records:
            records[gnd_id] = lobid(gnd_id, session)
        return records[gnd_id]

    if len(gnd[wikidata_id]["gnd_id"]) > 1:
        new_gnds: list[str] = []
        for gnd_id in gnd[wikidata_id]["gnd_id"]:
            rec = fetch(gnd_id)
            if rec.get("missing"):
                continue
            if rec["current"] not in new_gnds:
                new_gnds.append(rec["current"])
        gnd[wikidata_id]["gnd_id"] = new_gnds

    # Then check for the remaining cases what case it is
    # is it a pseudonym, a duplicate, unsure
    if len(gnd[wikidata_id]["gnd_id"]) > 1:
        # classify() writes its result into gnd[wikidata_id]["decision"]
        classify(gnd[wikidata_id], [fetch(g) for g in gnd[wikidata_id]["gnd_id"]])

    return gnd


def query_by_gnd(gnd_id: str, label_lang: str = "en") -> dict[str, str | list[str]]:
    """Fetch all Wikidata properties and values for the item with the
    given GND ID.

    Adapted from https://christianmahnke.de/post/simple-wikidata-queries/

    :param gnd_id: The GND ID to look up (matched against P227 and P7902).
    :param label_lang: Preferred language code for value labels
        (e.g. "en", "de"). Defaults to "en".
    :return: Dictionary mapping property labels to their value(s). A
        property with multiple values maps to a list.
    :rtype: dict[str, str | list[str]]
    """
    query = f"""
    SELECT ?item ?propertyLabel ?valueLabel ?valueURI
    WHERE {{
      {{ ?item wdt:P227 "{gnd_id}" . }}
      UNION
      {{ ?item wdt:P7902 "{gnd_id}" . }}
      ?item ?wdt ?o .
      ?property wikibase:directClaim ?wdt .

      SERVICE wikibase:label {{
        bd:serviceParam wikibase:language "en,de,mul,fr,it" .
        ?property rdfs:label ?propertyLabel .
      }}

      OPTIONAL {{
        FILTER(isIRI(?o))
        ?o rdfs:label ?langLabel .
        FILTER(LANG(?langLabel) = "{label_lang}")
      }}

      BIND(COALESCE(?langLabel, STR(?o)) AS ?valueLabel)
      BIND(IF(isIRI(?o), ?o, ?undefined) AS ?valueURI)
    }}
    """

    results, _ = sparql_query_builder(query)
    wikidata: dict[str, str | list[str]] = {}
    if not results:
        return wikidata

    for binding in results["results"]["bindings"]:
        key = binding["propertyLabel"]["value"]
        value = binding["valueLabel"]["value"]
        if key in wikidata:
            if isinstance(wikidata[key], list):
                wikidata[key].append(value)
            else:
                wikidata[key] = [wikidata[key], value]
        else:
            wikidata[key] = value
    return wikidata


###########
def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z ]", " ", s.lower()).split()

def year(s):
    m = re.search(r"\d{4}", s or "")
    return m.group(0) if m else None

def same_name(a, b):
    ta, tb = set(norm(a)), set(norm(b))
    return bool(ta and tb) and len(ta & tb) / min(len(ta), len(tb)) >= 0.6
    # think Engel, Rudolf Theophil and Engel, Theo


def lobid(gnd: str, session: requests.Session | None = None) -> dict:
    """Fetch the lobid record of a GND ID with a single request.

    Redirects are not followed: a redirect means the record was merged
    into another one, so ``current`` / ``merged_into`` hold the new ID
    (and only that is known about it; fetch the new ID to get its data).
    A 404 gives ``{"requested": gnd, "missing": True}``.
    """
    session = session or _session()
    req = session.get(
        LOBID.format(gnd),
        allow_redirects=False,
        headers={**UA, "Accept": "application/json"},
        timeout=60,
    )
    if req.status_code == 404:
        return {"requested": gnd, "missing": True}
    if req.is_redirect:
        new_id = (
            req.headers["Location"]
            .split("/")[-1]
            .replace("?format=json", "")
            .removesuffix(".json")
        )
        return {"requested": gnd, "current": new_id, "merged_into": new_id}
    req.raise_for_status()
    d = req.json()
    return {
        "requested": gnd,
        "current": d.get("gndIdentifier", gnd),
        "merged_into": None,
        "name": d.get("preferredName", ""),
        "birth": year((d.get("dateOfBirth") or [""])[0]),
        "death": year((d.get("dateOfDeath") or [""])[0]),
        "undifferentiated": "UndifferentiatedPerson" in d.get("type", []),
        "raw": json.dumps(d, ensure_ascii=False),
    }


def mentions_pseudonym(rec, other):
    """True if rec points to the other GND ID and the record talks about a pseudonym."""
    raw = rec.get("raw", "")
    return other["current"] in raw and re.search(r"seudonym", raw, re.IGNORECASE) is not None


def classify(wikidata_dict, recs):
    """Classify an item with several GND IDs and store the outcome in
    ``wikidata_dict["decision"]`` (also returns the dict).

    :param wikidata_dict: The inner dict built in get_gnd_id_by_wikidata_id.
    :param recs: The lobid() records of wikidata_dict["gnd_id"], already
        fetched by the caller (no further lobid requests are made here).
    """

    # cand = sorted(
    #     items.values(),
    #     key=lambda i: -len(i["gnds"] & set(gnd_ids))
    #     )
    # item = cand[0] if cand else None
    # qid = item["qid"] if item else None

    # 1) outdated / merged IDs -> plain linking mistake
    # this should be caught beforehand
    merged = [r for r in recs if r.get("merged_into")]
    if merged:
        keep = sorted({r.get("merged_into") or r["requested"] for r in recs})
        rm = [r["requested"] for r in merged]
        wikidata_dict["decision"] = f"auto: ID(s) {', '.join(rm)} were merged in the GND, replace by current ID."
        return wikidata_dict

    if any(r.get("missing") for r in recs):
        wikidata_dict["decision"] = "auto: no DNB record found for at least one ID."
        return wikidata_dict

    # 2) pseudonym relation between records
    for a in recs:
        for b in recs:
            if a is not b and mentions_pseudonym(a, b):
                wikidata_dict["decision"] = f"auto: DNB records {a['requested']} / {b['requested']} refer to each other as pseudonym ({a['name']} | {b['name']})."
                return wikidata_dict

    # 3) clearly different persons (different birth/death year) -> one is a wrong link
    births = {r["birth"] for r in recs if r["birth"]}
    deaths = {r["death"] for r in recs if r["death"]}
    different = len(births) > 1 or len(deaths) > 1
    if different:
        scored = []
        for r in recs:
            s = (2 if same_name(r["name"], wikidata_dict["wikidata_label"]) else 0) + (
                2 if wikidata_dict["wikidata_birth"] and r["birth"] == wikidata_dict["wikidata_birth"] else 0
            )
            scored.append((s, r))
        scored.sort(key=lambda x: -x[0])
        if scored[0][0] > scored[1][0]:
            keep = [scored[0][1]["requested"]]
            rm = [r["requested"] for _, r in scored[1:]]
            wikidata_dict["decision"] = f"""auto: DNB records describe different persons ({'; '.join(
                        f"{r['requested']}: {r['name']} {r['birth'] or ''}-{r['death'] or ''}"
                        for r in recs
                    )}); item matches {keep[0]}."""
            return wikidata_dict
        wikidata_dict["decision"] = f"""auto: records differ ({"; ".join(
                f"{r['requested']}: {r['name']} {r['birth'] or ''}" for r in recs
            )}) but the item matches none/all of them."""
        return wikidata_dict

    # 4) same person duplicated in the GND (same name / same dates) -> cannot fix on Wikidata
    if all(same_name(recs[0]["name"], r["name"]) for r in recs[1:]):
        wikidata_dict["decision"] = "auto: same name/dates in all records - probably duplicate GND records (very likely same person); consider reporting to DNB."
        return wikidata_dict


    # 5) different names, no pseudonym link, no date conflict
    wikidata_dict["decision"] = f"""auto: different names, no pseudonym relation found ({"; ".join(f"{r['requested']}: {r['name']}" for r in recs)})."""
    return wikidata_dict


if __name__ == "__main__":
    print(get_gnd_id_by_wikidata_id("Q1688416"))
    #print(get_wikidata_id_by_gnd_id("101620017X"))
    #print(query_by_gnd("101620017X"))
    # print(query_by_gnd("101620017X", "de"))