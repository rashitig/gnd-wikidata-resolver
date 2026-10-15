"""Everything that talks to the Wikidata Query Service."""

from dataclasses import dataclass, field

import requests

from . import _util

SPARQL_URL = "https://query.wikidata.org/sparql"

def make_session() -> requests.Session:
    """Session with automatic retries and the User-Agent set once for all requests."""
    session = requests.Session()
    retries = requests.adapters.Retry(
        total=20, backoff_factor=0.1, status_forcelist=[500, 502, 503, 504]
    )
    session.mount("http://", requests.adapters.HTTPAdapter(max_retries=retries))
    session.mount("https://", requests.adapters.HTTPAdapter(max_retries=retries))
    session.headers["User-Agent"] = _util.USER_AGENT
    return session


def sparql_query(query: str, session: requests.Session | None = None):
    """Run a SPARQL query. Returns (parsed JSON or None, session)."""
    session = session or make_session()
    try:
        req = session.get(
            SPARQL_URL,
            params={"query": query, "format": "json"},
            headers={"Accept": "application/sparql-results+json"},
            timeout=60,
        )
        req.raise_for_status()
        return req.json(), session
    except requests.RequestException as e:
        print(f"Error accessing Wikidata endpoint: {e}")
    except requests.JSONDecodeError as e:
        print(f"Error decoding JSON: {e}")
    return None, session


@dataclass(frozen=True)
class Item:
    qid: str
    label: str = ""
    birth: str | None = None
    death: str | None = None
    gnd_ids: list[str] = field(default_factory=list)  # raw, may contain old IDs


def fetch_item(qid: str, session: requests.Session) -> Item:
    """Label, birth/death year and GND IDs (P227 and P7902) of one item."""
    query = f"""SELECT ?itemLabel ?gndId ?birth ?death WHERE {{
  VALUES ?gndProp {{ wdt:P227 wdt:P7902 }}
  BIND(wd:{qid} AS ?item)
  ?item ?gndProp ?gndId .
  OPTIONAL {{ ?item wdt:P569 ?birth . }}
  OPTIONAL {{ ?item wdt:P570 ?death . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "[AUTO_LANGUAGE],de,en". }}
}}"""
    results, _ = sparql_query(query, session)
    if not results:
        return Item(qid)
    label, birth, death, ids = "", None, None, []
    for b in results["results"]["bindings"]:
        if b["gndId"]["value"] not in ids:
            ids.append(b["gndId"]["value"])
        label = b["itemLabel"]["value"]
        if "birth" in b:
            birth = _util.year(b["birth"]["value"])
        if "death" in b:
            death = _util.year(b["death"]["value"])
    return Item(qid, label, birth, death, ids)


# ---- reverse direction: GND ID -> Wikidata -------------------------------
def qids_for_gnd_ids(gnd_ids: list, session) -> dict[str, list[str]]:
    """Look up the Wikidata Q-ID(s) associated with a given GND ID.

    Checks both P227 and P7902, since either property may hold the
    GND ID depending on how the item was linked.

    :param gnd_id: The GND IDs to look up.
    :return: Dictionary mapping the input GND ID to a list of matching
        Wikidata Q-IDs (usually just one, but can be more if the GND ID
        is linked from multiple items).
    :rtype: dict[str, list[str]]
    """
    if not gnd_ids:
        return []

    values = " ".join(f'"{g}"' for g in gnd_ids)
    query = f"""SELECT DISTINCT ?item WHERE {{
  VALUES ?gnd {{ {values} }}
  {{ ?item wdt:P227 ?gnd }} UNION {{ ?item wdt:P7902 ?gnd }}
}}"""
    results, _ = sparql_query(query, session)
    if not results:
        return []

    return [b["item"]["value"].rsplit("/", 1)[-1] for b in results["results"]["bindings"]]


def get_wikidata_item_properties_by_gnd_id(
    gnd_id: str, label_lang: str = "en"
) -> dict[str, str | list[str]]:
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

    results, _ = sparql_query(query)
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
