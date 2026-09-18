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
    endpoint = "https://query.wikidata.org/sparql"
    session = session or _session()
    try:
        req = session.get(
            endpoint,
            params={"query": query, "format": "json"},
            headers={
                "User-Agent": (
                    "GND-Wikidata-resolver/1.0 "
                    "(https://github.com/rashitig/gnd-wikidata-resolver; "
                    "6xchepfl0@mozmail.com)"
                ),
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


def get_gnd_id_by_wikidata_id(wikidata_id: str) -> dict[str, list[str]]:
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
    :return: Dictionary mapping the input Q-ID to a list of associated
        GND IDs.
    :rtype: dict[str, list[str]]
    """
    # The wdt: prefix returns only the preferred-ranked statement(s)
    # when a preference is set on Wikidata.
    query = f"""SELECT ?gndId WHERE {{
  VALUES ?gndProp {{ wdt:P227 wdt:P7902 }}
  wd:{wikidata_id} ?gndProp ?gndId .
}}
GROUP BY ?gndId"""
    results, session = sparql_query_builder(query)
    gnd: dict[str, list[str]] = {wikidata_id: []}
    if not results:
        return gnd

    for binding in results["results"]["bindings"]:
        value = binding["gndId"]["value"]
        gnd[wikidata_id].append(value)

    if len(gnd[wikidata_id]) != 1:
        # Check whether any candidates are old records
        # that redirect to a current one.
        new_gnds: list[str] = []
        for gnd_id in gnd[wikidata_id]:
            lobid_url = f"https://lobid.org/gnd/{gnd_id}.json"
            req = session.head(lobid_url, allow_redirects=False)
            if req.is_redirect:
                real_gnd_id = (
                    req.headers["Location"].split("/")[-1].replace("?format=json", "")
                )
                if real_gnd_id not in new_gnds:
                    new_gnds.append(real_gnd_id)
            elif req.status_code != 404 and gnd_id not in new_gnds:
                new_gnds.append(gnd_id)
        gnd[wikidata_id] = new_gnds
    return gnd


def query_by_gnd(gnd_id: str, label_lang: str = "en") -> dict[str, str | list[str]]:
    """Fetch all Wikidata properties and values for the item with the
    given GND ID.

    Adapted from https://christianmahnke.de/post/simple-wikidata-queries/

    :param gnd_id: The GND ID to look up (matched against P227).
    :param label_lang: Preferred language code for value labels
        (e.g. "en", "de").
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


if __name__ == "__main__":
    print(get_gnd_id_by_wikidata_id("Q72067012"))
    print(get_wikidata_id_by_gnd_id("101620017X"))
    print(query_by_gnd("101620017X"))
    #print(query_by_gnd("101620017X", "de"))