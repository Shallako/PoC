"""The pre-flight read of the style direction.

Every case here is either the incident that caused the module to exist or a
phrase that must *not* trip it. The second half matters as much as the first: a
check that cries wolf on ordinary art direction gets ignored, and then it is
worse than nothing.

The incident, from projects/mv-boston/activity.jsonl: four consecutive renders,
sixteen of sixteen refused each time, $2.88 spent and no pictures, with
`story_chars: 4327` unchanged throughout -- so the story was never the problem.
The style direction was, and it ended "no pants or jacket, summer outfits".
"""

from __future__ import annotations

import re
from pathlib import Path

from shoulico import screening

# The real one, verbatim from the project that paid for it.
INCIDENT = (
    "Afro-manga inspired illustration, July 4, 1992 summer on Martha's Vineyard, "
    "social gathering takes place inside the house, Exterior front porch of a white "
    "clapboard summer house on a humid holiday afternoon, screen door swinging open, "
    "paper bunting strung along the railing, no pants or jacket, summer outfits"
)


def codes(text) -> set:
    return {f["code"] for f in screening.screen(text)}


# --------------------------------------------------------------------- #
# The incident
# --------------------------------------------------------------------- #

def test_the_hint_that_cost_two_pounds_eighty_eight_is_caught():
    found = screening.screen(INCIDENT)
    assert "undress" in {f["code"] for f in found}

    undress = next(f for f in found if f["code"] == "undress")
    assert undress["severity"] == screening.WARN
    assert undress["matched"] == ["no pants"]
    # The alternative is the point. A warning with no way forward is a wall.
    assert "what they are wearing" in undress["suggestion"]


def test_no_adult_age_beside_an_undress_phrase_is_the_compound_warning():
    """Neither half is reliably refused alone. Together they are, and that is
    what the four rejected batches actually were."""
    assert "minor-and-undress" in codes(INCIDENT)


def test_naming_adult_ages_stands_the_compound_warning_down():
    """What the project's own successful run did: it kept the wardrobe phrasing
    and added explicit ages, and sixteen of sixteen rendered."""
    fixed = INCIDENT + ", a man of twenty-three and a man of twenty-six"
    found = codes(fixed)
    assert "minor-and-undress" not in found
    # The wardrobe phrasing is still worth saying something about.
    assert "undress" in found


def test_the_repair_the_advice_recommends_is_clean():
    """Follow the suggestion and the check goes quiet -- otherwise the advice is
    not advice."""
    repaired = INCIDENT.replace(
        "no pants or jacket, summer outfits",
        "khaki shorts, cotton T-shirts and canvas sneakers, two men in their twenties")
    assert screening.screen(repaired) == []


# --------------------------------------------------------------------- #
# Ages
# --------------------------------------------------------------------- #

def test_words_that_name_a_child_are_caught():
    for phrase in ("two boys on a bike", "a teenage girl at the window",
                   "children playing", "outside the high school"):
        assert "minor-words" in codes(phrase), phrase


def test_numbers_under_eighteen_are_caught_in_either_form():
    for phrase in ("a girl aged 12", "a 15 year old at the counter",
                   "a boy of 9", "a fifteen-year-old in a doorway"):
        assert "minor-age" in codes(phrase), phrase


def test_adult_ages_are_left_alone():
    for phrase in ("a woman of 30 in a red coat", "a man of twenty-three",
                   "a 45 year old mechanic", "aged 61, weathered hands"):
        assert not codes(phrase), phrase


def test_a_number_that_is_not_an_age_is_not_read_as_one():
    """A style direction is full of numbers: years, lenses, counts. Reading any
    of them as somebody's age would flag half the sensible hints there are."""
    for phrase in ("summer 1992, 35mm lens, f/1.4",
                   "exactly four players at the table",
                   "a palette of 3 colours, 16:9",
                   "shot on 8mm film"):
        assert not codes(phrase), phrase


# --------------------------------------------------------------------- #
# Clothing
# --------------------------------------------------------------------- #

def test_wardrobe_described_by_its_absence_is_caught():
    for phrase in ("no pants", "without a shirt", "shirtless on the porch",
                   "topless", "no clothes"):
        assert "undress" in codes(phrase), phrase


def test_restricting_what_else_appears_is_not_an_undress_instruction():
    """"no other pants or jacket" restricts what may also be worn; "no pants"
    removes them. The project's successful style block used the first form."""
    assert "undress" not in codes(
        "khaki shorts throughout, with no other pants or jacket")


def test_naming_the_clothes_is_never_flagged():
    assert not codes("cream linen camp shirt, khaki shorts, leather sandals")


# --------------------------------------------------------------------- #
# The rest, and the noise floor
# --------------------------------------------------------------------- #

def test_the_other_published_refusal_categories_are_named():
    assert "gore" in codes("a decapitated corpse in the road")
    assert "likeness" in codes("the lead looks like a famous actor")
    assert "brand" in codes("in the style of Studio Ghibli")


def test_telling_the_engine_what_not_to_draw_is_a_note_not_a_warning():
    found = screening.screen("watercolour, no crowds, not modern")
    assert [f["code"] for f in found] == ["negation"]
    assert found[0]["severity"] == screening.NOTE


def test_suppressing_text_is_the_one_negation_that_works():
    """This app's own compiled prompts end with a "no lettering anywhere"
    clause. Warning against it would be advising against what it does."""
    assert not codes("No text, lettering or numbers anywhere in the image")


def test_ordinary_art_direction_says_nothing():
    for phrase in ("bold anime linework, cel-shaded, summer 1992",
                   "1970s Kodachrome, grainy, warm highlights",
                   "flat vector illustration, three flat colours",
                   "charcoal on paper, heavy hatching, high contrast",
                   "wide cinematic framing, anamorphic flare, dusk"):
        assert not codes(phrase), phrase


def test_an_empty_direction_is_not_a_problem():
    assert screening.screen("") == []
    assert screening.screen(None) == []
    assert screening.worst([]) is None


def test_warnings_are_ordered_before_notes():
    found = screening.screen("two boys, no crowds")
    assert [f["severity"] for f in found] == sorted(
        [f["severity"] for f in found], key=lambda s: 0 if s == screening.WARN else 1)
    assert screening.worst(found) == screening.WARN


# --------------------------------------------------------------------- #
# The endpoint, and the page that reads it
# --------------------------------------------------------------------- #

def test_the_endpoint_screens_without_a_project_or_a_key(client):
    """It runs before anything exists and before anything is spent, so it must
    not need a project, a key, or a network."""
    r = client.post("/api/screen", json={"text": INCIDENT})
    assert r.status_code == 200
    body = r.json()
    assert body["worst"] == screening.WARN
    assert "undress" in {f["code"] for f in body["findings"]}
    assert all(f["message"] and f["suggestion"] for f in body["findings"])


def test_the_endpoint_is_quiet_on_a_clean_direction(client):
    r = client.post("/api/screen", json={"text": "cel-shaded, warm dusk palette"})
    assert r.json() == {"findings": [], "worst": None}


def test_the_endpoint_bounds_what_it_will_read(client):
    from shoulico import security
    r = client.post("/api/screen", json={"text": "no pants " * 20000})
    assert r.status_code == 200
    # Truncated, not refused: a long paste should still get an answer.
    assert "undress" in {f["code"] for f in r.json()["findings"]}


PAGE = Path(__file__).resolve().parent.parent / "shoulico" / "static" / "index.html"


def test_every_finding_the_server_can_emit_has_wording_in_the_page():
    """The page holds its own copy so findings are translated with the rest of
    the interface. That is two places for one fact, so this is the thing that
    notices when they drift: add a rule without wording and this fails."""
    page = PAGE.read_text(encoding="utf-8")
    for code in screening._MESSAGES:
        assert f'"screen.{code}"' in page, f"no page wording for {code}"
        assert f'"screen.{code}.fix"' in page, f"no page suggestion for {code}"


def test_the_help_warns_about_the_refusals_that_actually_happen():
    page = PAGE.read_text(encoding="utf-8")
    help_text = re.search(r'"help\.styleHint": "(.*?)(?<!\\)",\n', page, re.S)
    assert help_text, "help.styleHint not found"
    body = help_text.group(1)
    for promise in ("minor", "no pants", "every scene prompt", "twenties"):
        assert promise in body, promise


def test_the_recommended_phrasing_is_recognised_as_an_adult_age():
    """The suggestion says to write "two men in their twenties". If that did not
    register as an adult age, following the advice would leave the warning up --
    which is how an earlier version of this actually behaved."""
    assert "minor-and-undress" not in codes(
        "no pants, summer outfits, two men in their twenties")
    assert "minor-and-undress" in codes("no pants, summer outfits, two lads in their teens")
    assert "minor-age" in codes("two lads in their teens")


def test_a_compound_adult_age_is_not_read_as_its_first_word():
    """"twenty-three" must not resolve to twenty-nothing or to "three"."""
    assert not codes("a man of twenty-three")
    assert not codes("a woman of thirty-one, sharp features")
