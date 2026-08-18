"""Read a style direction the way an image engine's safety classifier will.

This exists because of one measured incident. A twelve-scene story was rendered
four times and refused sixteen out of sixteen every time -- **$2.88 spent, no
pictures** -- with the same sentence each round:

    HTTP 451: Prompt references minors. Content involving minors is not allowed.

The story was never the problem: `story_chars` is 4327 in the activity log for
every one of those runs and for the two that later worked. The style direction
was, and it ended:

    ..., no pants or jacket, summer outfits

Two things about that are worth generalising. "no pants" is a *negative wardrobe
instruction*, and a classifier reads an instruction to remove clothing exactly as
written -- the intent (summer clothes, not winter ones) is invisible to it. And a
cast of young men with no age stated anywhere leaves the classifier to guess how
old they are, which it does conservatively. Neither half is refused on its own.
Together they are the most-refused pattern in image generation there is.

So this module is a pre-flight read of the style direction, run before the story
is sent to Claude -- which is the last moment it is free. Everything downstream
is not: the style direction becomes the shared style block, the block is appended
to every scene prompt *and* every character portrait, and one bad phrase in it
therefore poisons every image in the batch at once. That is why sixteen of
sixteen failed rather than one of sixteen.

What this is not
----------------
It is not the engine's classifier and cannot be. It has no model behind it, sees
none of the story, and the real rule set is unpublished and moves. It will miss
things, and it will occasionally flag a phrase that would have been fine. So
nothing here blocks: every finding is a warning with a way forward, and the user
can always go ahead. A pre-flight check that refused to let you fly would be
worse than the problem.
"""

from __future__ import annotations

import re

# Severity. Only two, because a third would be a distinction nobody acts on
# differently: either this is likely to cost you a batch, or it is something
# worth knowing while you are already here.
WARN = "warn"       # a refusal is likely -- worth stopping to read
NOTE = "note"       # will probably render, but not the way you meant

# --------------------------------------------------------------------------- #
# Ages
#
# The classifier's question is "could this be a minor?", and silence is not a
# defence: an undescribed age is an ambiguity it resolves against you. So there
# are two findings here -- words that name a child, and numbers that are under
# eighteen -- and separately, over in the FE help, the advice to state an adult
# age rather than leave it out.
# --------------------------------------------------------------------------- #

_MINOR_WORDS = (
    "child", "children", "kid", "kids", "boy", "boys", "girl", "girls",
    "teen", "teens", "teenage", "teenager", "teenagers", "adolescent",
    "minor", "minors", "underage", "toddler", "toddlers", "baby", "babies",
    "infant", "infants", "youngster", "youngsters", "schoolboy", "schoolgirl",
    "preteen", "pre-teen", "juvenile", "kindergarten", "elementary school",
    "middle school", "high school", "schoolchildren", "pupil", "pupils",
)

# Written ages. Both forms a person actually types, and *only* the vocabulary of
# ages -- an earlier version accepted any word here and read "a palette of 3
# colours" as a three-year-old. A style direction is full of numbers: years,
# focal lengths, counts of people, aspect ratios. Almost none of them are ages.
_WORD_AGES = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}
# Longest first, so "seventeen" is never matched as "seven".
_WORD_AGE = "|".join(sorted(_WORD_AGES, key=len, reverse=True))
_TENS = "twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
_NUM = rf"\d{{1,2}}|(?:{_TENS})[\s-](?:{_WORD_AGE})|{_TENS}|{_WORD_AGE}"

# "of" alone is not an age context -- it is the commonest word in a description.
# It counts only after a word that names a person.
_PERSON = (r"man|men|woman|women|boy|boys|girl|girls|child|children|person|"
           r"people|guy|guys|lad|lass|kid|kids|male|female|figure|character|"
           r"narrator|adult|adults|teen|teenager")

_AGE_PATTERNS = (
    re.compile(rf"\b({_NUM})[\s-]*(?:year|yr)s?[\s-]*old\b", re.I),
    re.compile(rf"\baged?\s+({_NUM})\b", re.I),
    re.compile(rf"\b(?:{_PERSON})\s+(?:of|aged)\s+({_NUM})\b", re.I),
)

# "two men in their twenties" is the phrasing this module's own advice tells
# people to write, so it has to read as an adult age -- otherwise following the
# suggestion would not clear the warning that produced it.
_DECADES = re.compile(
    r"\bin\s+(?:his|her|their|its)\s+"
    r"(teens|twenties|thirties|forties|fifties|sixties|seventies|eighties|nineties)\b",
    re.I)

ADULT = 18

# --------------------------------------------------------------------------- #
# Clothing
#
# The measured half. A negative wardrobe instruction is the trap: it is written
# to mean "dressed for summer" and read as "remove the clothing named". The
# adjacency is deliberate -- "no pants" is flagged, "no other pants" is not,
# because the second is a restriction on what else may appear rather than an
# instruction to take anything off.
# --------------------------------------------------------------------------- #

_GARMENTS = ("pants", "trousers", "shirt", "shirts", "top", "tops", "clothes",
             "clothing", "jacket", "jackets", "underwear", "bra", "dress",
             "skirt", "shorts", "coat", "sleeves")

_UNDRESS = (
    re.compile(r"\bno\s+(" + "|".join(_GARMENTS) + r")\b", re.I),
    re.compile(r"\bwithout\s+(?:a\s+|any\s+)?(" + "|".join(_GARMENTS) + r")\b", re.I),
    re.compile(r"\b(nude|nudity|naked|topless|bottomless|undressed|unclothed|"
               r"shirtless|bare[\s-]?chested|lingerie|negligee)\b", re.I),
)

# --------------------------------------------------------------------------- #
# The rest
#
# Shorter lists, and deliberately so. These are the categories every image API
# publishes a rule about; the point is to name the category and say what to do,
# not to pretend to hold the vocabulary of a real classifier.
# --------------------------------------------------------------------------- #

_GORE = re.compile(
    r"\b(gore|gory|mutilat\w*|dismember\w*|decapitat\w*|disembowel\w*|corpse|"
    r"corpses|entrails|bloodied|blood[\s-]?soaked|severed\s+(?:head|limb|arm|leg))\b",
    re.I)

_LIKENESS = re.compile(
    r"\b(?:looks?\s+like|resembl\w+|likeness\s+of|lookalike|look[\s-]alike|"
    r"portrait\s+of\s+(?:the\s+)?(?:celebrity|actor|actress|singer|president)|"
    r"famous\s+(?:actor|actress|singer|musician|politician|celebrity)|"
    r"celebrity)\b", re.I)

# Named because these are refused by name, not by a general rule about
# intellectual property, and a user is far more likely to reach for one of these
# than to describe an infringement in the abstract.
_BRANDS = re.compile(
    r"\b(disney|pixar|marvel|dc\s+comics|star\s+wars|pok[eé]mon|nintendo|"
    r"mickey\s+mouse|spider[\s-]?man|batman|superman|studio\s+ghibli|"
    r"simpsons|harry\s+potter|hello\s+kitty|barbie|lego)\b", re.I)

# Not a refusal -- a quality note. Image models are weak at negation and often
# draw the thing they were told to leave out.
#
# With one exception, which is why there is a second list: suppressing text is
# the one negation that reliably works, it is the most common complaint about
# generated images, and this project's own compiled prompts end with a "no
# lettering anywhere" clause. Flagging that would be advising against something
# the app itself does on purpose.
_NEGATION = re.compile(r"\bn(?:o|ot|ever)\s+(?!other\b)([a-z][a-z-]*)", re.I)
_NEGATION_FINE = frozenset({
    "text", "texts", "lettering", "letters", "words", "wording", "writing",
    "written", "captions", "caption", "subtitles", "watermark", "watermarks",
    "signature", "signatures", "logo", "logos", "numbers", "numerals", "typography",
})

_MESSAGES = {
    "minor-words": (
        WARN,
        "This names a child or a young person ({matched}). Image services refuse "
        "anything that could depict a minor, and they refuse the whole request "
        "rather than the phrase.",
        "If everyone in these pictures is an adult, say so in as many words -- "
        "\"two men in their twenties\" rather than \"two young guys\". If the "
        "story really is about children, this engine will not draw them and no "
        "wording gets round that."),
    "minor-age": (
        WARN,
        "This gives an age under {adult} ({matched}), which is refused outright.",
        "State an adult age instead. If the age is not what the picture is "
        "about, drop the number and describe the build and bearing."),
    "undress": (
        WARN,
        "This asks for clothing to be removed or absent ({matched}). Read "
        "literally -- which is how a classifier reads it -- that is a request "
        "for an undressed subject.",
        "Say what they are wearing, not what they are not. \"no pants\" reads as "
        "an instruction to undress somebody; \"khaki shorts and canvas sneakers\" "
        "gets you the same summer picture and passes."),
    "minor-and-undress": (
        WARN,
        "Both an unstated-or-young age and an instruction about removing clothing "
        "are present. Separately each is sometimes allowed; together they are the "
        "single most reliably refused combination there is.",
        "Fix the wardrobe phrasing first -- write what people wear -- and give "
        "every person an explicit adult age. This exact pair cost four rejected "
        "batches in this project's own history."),
    "gore": (
        WARN,
        "This describes graphic injury or death ({matched}).",
        "Imply it instead of depicting it: aftermath, reaction, an object, a "
        "look. It also usually makes the better picture."),
    "likeness": (
        WARN,
        "This asks for the likeness of a real, identifiable person ({matched}).",
        "Describe the features you actually want -- height, build, hair, "
        "bearing, wardrobe -- rather than naming somebody to copy."),
    "brand": (
        WARN,
        "This names a trademarked character or franchise ({matched}).",
        "Describe the look you are after in your own words: \"flat cel shading, "
        "heavy outlines, saturated primaries\" rather than the studio's name."),
    "negation": (
        NOTE,
        "This tells the engine what *not* to draw ({matched}). Image models are "
        "weak at negation and often render the thing they were told to leave out.",
        "Rewrite it as something to include. \"no crowds\" becomes \"an empty "
        "street\"; \"not modern\" becomes \"1950s\"."),
}


def _found(code: str, matched: list[str]) -> dict:
    severity, message, suggestion = _MESSAGES[code]
    shown = ", ".join(dict.fromkeys(matched))     # de-duplicated, order kept
    return {
        "code": code,
        "severity": severity,
        "matched": list(dict.fromkeys(matched)),
        "message": message.format(matched=shown, adult=ADULT),
        "suggestion": suggestion,
    }


def _minor_words(text: str) -> list[str]:
    hits = []
    for word in _MINOR_WORDS:
        if re.search(r"\b" + re.escape(word) + r"\b", text, re.I):
            hits.append(word)
    return hits


def _young_ages(text: str) -> list[str]:
    """Ages below eighteen, digits or words, only in a real age context."""
    hits = []
    for pattern in _AGE_PATTERNS:
        for match in pattern.finditer(text):
            value = _as_age(match.group(1))
            if value is not None and value < ADULT:
                hits.append(match.group(0).strip())
    for match in _DECADES.finditer(text):
        if match.group(1).lower() == "teens":
            hits.append(match.group(0).strip())
    return hits


def _as_age(raw: str):
    raw = raw.strip().lower()
    if raw.isdigit():
        value = int(raw)
        return value if 1 <= value <= 99 else None
    if raw in _WORD_AGES:
        return _WORD_AGES[raw]
    # "twenty-three" and up. The exact value does not matter: everything
    # compounded off twenty or higher is an adult, and this is the form the
    # fix for the minors warning is written in.
    if re.match(rf"^(?:{_TENS})\b", raw):
        return ADULT
    return None


def _matches(pattern: re.Pattern, text: str) -> list[str]:
    return [m.group(0).strip() for m in pattern.finditer(text)]


def screen(text: str) -> list[dict]:
    """Findings, worst first. An empty list means nothing stood out.

    Deliberately cheap and synchronous: it runs on every keystroke's worth of
    debounce in the page, and the moment it needs a model call it stops being
    something you can put in front of a button.
    """
    text = (text or "").strip()
    if not text:
        return []

    findings = []
    young = _minor_words(text)
    ages = _young_ages(text)
    undress = [m for pattern in _UNDRESS for m in _matches(pattern, text)]

    if young:
        findings.append(_found("minor-words", young))
    if ages:
        findings.append(_found("minor-age", ages))
    if undress:
        findings.append(_found("undress", undress))
    # The compound finding, and the reason this module exists. Raised whenever
    # undress language meets *any* sign of youth -- including no age at all,
    # which is how the incident that prompted this was actually written.
    if undress and (young or ages or not _states_an_adult_age(text)):
        findings.append(_found("minor-and-undress", undress + young + ages))

    for code, pattern in (("gore", _GORE), ("likeness", _LIKENESS),
                          ("brand", _BRANDS)):
        hits = _matches(pattern, text)
        if hits:
            findings.append(_found(code, hits))

    # Only worth saying when it is not already the point of a louder finding.
    if not undress:
        hits = [m.group(0).strip() for m in _NEGATION.finditer(text)
                if m.group(1).lower() not in _NEGATION_FINE]
        if hits:
            findings.append(_found("negation", hits))

    findings.sort(key=lambda f: 0 if f["severity"] == WARN else 1)
    return findings


def _states_an_adult_age(text: str) -> bool:
    """Does this say, anywhere, that the people in it are adults?

    The absence of an age is the ambiguity a classifier resolves against you, so
    "there is no age here" has to count as a risk rather than as an all-clear.
    """
    if re.search(r"\b(adult|adults|grown|middle[\s-]aged|elderly)\b", text, re.I):
        return True
    for match in _DECADES.finditer(text):
        if match.group(1).lower() != "teens":
            return True
    for pattern in _AGE_PATTERNS:
        for match in pattern.finditer(text):
            value = _as_age(match.group(1))
            if value is not None and value >= ADULT:
                return True
    return False


def worst(findings: list[dict]) -> str | None:
    for finding in findings:
        if finding["severity"] == WARN:
            return WARN
    return NOTE if findings else None
