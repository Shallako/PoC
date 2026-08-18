"""Each Claude call gets the model and the thinking depth its task deserves.

One hardcoded `effort: "high"` used to govern all three calls, so translating a
list of button labels was billed the same reasoning as reading a story and
turning it into twelve image prompts. Effort drives how much the model thinks
and thinking is billed at the output rate, which makes this the cheapest lever
in the app -- and the easiest to lose again, since nothing but these tests
notices if a call quietly goes back to the default.
"""

from __future__ import annotations

import pytest
from conftest import STORY, new_project, segmented

from shoulico import compiler, config, i18n, narration


def sent(claude, kind):
    """The kwargs of the last call of `kind` the app made."""
    for call in reversed(claude.calls):
        if claude.kind_of(call) == kind:
            return call
    raise AssertionError(f"no {kind} call was made")


# --------------------------------------------------------------------------- #
# What each phase actually asks for
# --------------------------------------------------------------------------- #

def test_segmenting_gets_the_expensive_model_and_deep_thinking(client, claude):
    """The one genuinely hard call: it reads a story, finds its beats, writes
    engine-targeted prompts and picks out the cast in a single pass."""
    segmented(client, scenes=3)
    call = sent(claude, "segment")
    assert call["model"] == config.SEGMENT_MODEL
    assert call["output_config"]["effort"] == config.SEGMENT_EFFORT


def test_narration_gets_the_cheaper_model(client, claude):
    pid = segmented(client, scenes=3)
    assert client.post(f"/api/projects/{pid}/narration", json={}).status_code == 200
    call = sent(claude, "narration")
    assert call["model"] == config.NARRATION_MODEL
    assert call["output_config"]["effort"] == config.NARRATION_EFFORT


def test_translating_the_interface_gets_the_cheapest_settings(client, claude):
    r = client.post("/api/ui/strings",
                    json={"code": "fr", "name": "French", "strings": {"a": "Render"}})
    assert r.status_code == 200
    call = sent(claude, "ui")
    assert call["model"] == config.TRANSLATE_MODEL
    assert call["output_config"]["effort"] == config.TRANSLATE_EFFORT


def test_the_three_phases_are_not_all_the_same_call(client, claude):
    """The whole point. If a refactor collapses these back onto one model and
    one effort, this is what says so."""
    pid = segmented(client, scenes=2)
    client.post(f"/api/projects/{pid}/narration", json={})
    client.post("/api/ui/strings",
                json={"code": "fr", "name": "French", "strings": {"a": "Render"}})

    efforts = {k: sent(claude, k)["output_config"]["effort"]
               for k in ("segment", "narration", "ui")}
    assert efforts["segment"] != efforts["ui"], (
        "a story and a list of button labels are not the same problem")
    assert config.CLAUDE_EFFORTS.index(efforts["ui"]) \
        <= config.CLAUDE_EFFORTS.index(efforts["narration"]) \
        <= config.CLAUDE_EFFORTS.index(efforts["segment"]), \
        "effort should not exceed the difficulty of the task"


# --------------------------------------------------------------------------- #
# The knobs themselves
# --------------------------------------------------------------------------- #

def test_a_caller_can_override_both(claude):
    compiler.segment(STORY, 2, api_key="k", model="claude-opus-4-8", effort="max")
    call = claude.calls[-1]
    assert call["model"] == "claude-opus-4-8"
    assert call["output_config"]["effort"] == "max"


@pytest.mark.parametrize("bad", ["", "HIGH", "highest", "none", None, 3])
def test_a_bad_effort_is_refused_before_the_call_is_made(claude, bad):
    """A typo in one config constant would otherwise surface as an opaque 400
    on the next thing anybody renders."""
    with pytest.raises(ValueError, match="effort must be one of"):
        compiler.segment(STORY, 2, api_key="k", effort=bad)
    assert claude.calls == [], "nothing should have been sent"


@pytest.mark.parametrize("effort", config.CLAUDE_EFFORTS)
def test_every_declared_effort_is_accepted(claude, effort):
    compiler.segment(STORY, 2, api_key="k", effort=effort)
    assert claude.calls[-1]["output_config"]["effort"] == effort


def test_the_configured_efforts_are_all_real_ones():
    for name in ("SEGMENT_EFFORT", "NARRATION_EFFORT", "TRANSLATE_EFFORT"):
        assert getattr(config, name) in config.CLAUDE_EFFORTS, name


# --------------------------------------------------------------------------- #
# The interaction with the overload ladder
# --------------------------------------------------------------------------- #

def test_the_fallback_still_applies_to_segmenting(client, claude):
    """Segmenting runs on the model that saturates first, so it keeps a tier to
    fall back to."""
    assert config.FALLBACK_CLAUDE_MODEL
    assert config.SEGMENT_MODEL != config.FALLBACK_CLAUDE_MODEL


def test_a_task_already_on_the_fallback_model_simply_has_none(claude):
    """Narration runs on the fallback tier itself, so its ladder is four tries
    on one model rather than five across two. That is deliberate -- there is no
    less-saturated tier to escalate to -- and this records it as a decision."""
    from fake_claude import overloaded

    claude.narration = overloaded()
    with pytest.raises(compiler.ClaudeError):
        narration.generate(STORY, [{"n": 1, "title": "T", "beat": "b"}], api_key="k")

    models = [c["model"] for c in claude.calls]
    assert set(models) == {config.NARRATION_MODEL}
    assert len(models) == compiler.CLAUDE_ATTEMPTS


def test_translation_is_unaffected_by_the_segment_model(claude):
    """strings_for used to forward its own default down to translate(), which
    would have quietly kept the expensive model on the cheapest call."""
    i18n.strings_for("fr", {"a": "Render"}, name="French", api_key="k")
    assert claude.calls[-1]["model"] == config.TRANSLATE_MODEL
