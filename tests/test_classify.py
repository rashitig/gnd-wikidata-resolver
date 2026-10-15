from gnd_wikidata_resolver.classify import classify, same_name
from gnd_wikidata_resolver.wikidata import Item


def rec(gnd, name, birth=None, death=None, raw=""):
    return {
        "requested": gnd,
        "current": gnd,
        "name": name,
        "birth": birth,
        "death": death,
        "raw": raw,
    }


def test_single_id_is_ok():
    assert (
        classify(
            Item("Q1", "Hans Müller", gnd_ids=["1"]), [rec("1", "Müller, Hans")]
        ).case
        == "ok"
    )


def test_merged_id():
    assert (
        classify(
            Item("Q1", "Hans Müller", gnd_ids=["OLD"]), [rec("NEW", "Müller, Hans")]
        ).case
        == "merged"
    )


def test_pseudonyms():
    assert (
        classify(
            Item("Q1", "Johann Ecker", "1766", gnd_ids=["1", "2"]),
            [
                rec("1", "Ecker, Johann", "1766", raw="2 Pseudonym"),
                rec("2", "Eckberger, J. A."),
            ],
        ).case
        == "pseudonyms"
    )


def test_different_persons_keeps_matching_id():
    d = classify(
        Item("Q1", "Hans Müller", "1950", gnd_ids=["1", "2"]),
        [rec("1", "Müller, Hans", "1900"), rec("2", "Müller, Hans", "1950")],
    )
    assert (d.case, d.keep, d.remove) == ("different_persons", ["2"], ["1"])


def test_duplicate_same_name_and_dates():
    assert (
        classify(
            Item("Q1", "Hans Müller", gnd_ids=["1", "2"]),
            [rec("1", "Müller, Hans", "1900"), rec("2", "Müller, Hans", "1900")],
        ).case
        == "duplicate"
    )


def test_unsure_different_names():
    assert (
        classify(
            Item("Q1", "A B", gnd_ids=["1", "2"]),
            [rec("1", "Schmidt, Anna"), rec("2", "Weber, Karl")],
        ).case
        == "unsure"
    )


def test_same_name_token_overlap():
    assert not same_name("Engel, Rudolf Theophil", "Engel, Theo")
    assert same_name("Engel, Rudolf Theophil", "Engel, Theophil")
