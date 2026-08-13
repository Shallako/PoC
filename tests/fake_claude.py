"""A stand-in for the Anthropic client.

It is installed in place of compiler._client, so everything downstream of the
SDK boundary runs for real: the streaming context manager, the stop_reason
checks, the JSON parse, the ordinal renumbering, the positional fallback in
narration. Only the network call is fake.

The default responses are derived from what the caller actually asked for -- the
scene count is read back out of the user turn -- so a test that asks for six
scenes gets six scenes without scripting anything.
"""

from __future__ import annotations

import json
import re


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _StopDetails:
    def __init__(self, category: str) -> None:
        self.category = category


class Reply:
    """A raw message to hand back, for the paths that aren't a happy JSON body."""

    def __init__(self, text: str = "", stop_reason: str = "end_turn",
                 refusal_category: str | None = None) -> None:
        self.content = [_Block(text)]
        self.stop_reason = stop_reason
        self.stop_details = _StopDetails(refusal_category) if refusal_category else None


class APIError(Exception):
    """Shaped like an anthropic.APIStatusError, which is all the classifier reads.

    Deliberately not a real SDK exception: building one needs an httpx response,
    and the point of duck-typing the classifier is that it does not care.
    """

    def __init__(self, status_code: int, error_type: str = "api_error",
                 message: str = "", request_id: str = "req_fake0001") -> None:
        super().__init__(
            f"Error code: {status_code} - {{'type': 'error', 'error': "
            f"{{'type': '{error_type}', 'message': '{message}'}}, "
            f"'request_id': '{request_id}'}}"
        )
        self.status_code = status_code
        self.request_id = request_id
        self.body = {"type": "error",
                     "error": {"type": error_type, "message": message}}


def overloaded(request_id: str = "req_fake0001") -> APIError:
    """The 529 that sends people to the issue tracker."""
    return APIError(529, "overloaded_error", "Overloaded", request_id)


class _Stream:
    def __init__(self, message) -> None:
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message


class _Messages:
    def __init__(self, fake: "FakeClaude") -> None:
        self._fake = fake

    def stream(self, **kwargs):
        return _Stream(self._fake._respond(kwargs))


class FakeClaude:
    """Set .segment / .narration to a dict, a callable(kwargs)->dict, a Reply,
    or an Exception to raise."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.segment = default_segment
        self.narration = default_narration
        self.ui = default_ui
        self.messages = _Messages(self)

    # -- assertions helpers ------------------------------------------- #

    def last_user_turn(self) -> str:
        return self.calls[-1]["messages"][0]["content"]

    def last_system(self) -> str:
        return self.calls[-1]["system"]

    def kind_of(self, call: dict) -> str:
        props = call["output_config"]["format"]["schema"]["properties"]
        if "style_profile" in props:
            return "segment"
        return "ui" if "items" in props else "narration"

    # -- the fake itself ---------------------------------------------- #

    def _respond(self, kwargs: dict):
        self.calls.append(kwargs)
        spec = getattr(self, self.kind_of(kwargs))
        if isinstance(spec, Exception):
            raise spec
        if callable(spec):
            spec = spec(kwargs)
        if isinstance(spec, Reply):
            return spec
        if isinstance(spec, Exception):
            raise spec
        return Reply(json.dumps(spec))


# --------------------------------------------------------------------- #
# Default bodies
# --------------------------------------------------------------------- #

def _count_from(user: str, pattern: str, fallback: int = 3) -> int:
    match = re.search(pattern, user)
    return int(match.group(1)) if match else fallback


STYLE_PROFILE = (
    "African-Anime illustration, bold graphic linework and cel shading, summer 1992, "
    "warm dusk palette of amber and deep blue. The narrator is a lean man of twenty-two "
    "in a white tank top; his cousin is broader, older, in a red cap he never removes. "
    "No lettering or written words anywhere in the image."
)


def default_segment(kwargs: dict) -> dict:
    user = kwargs["messages"][0]["content"]
    count = _count_from(user, r"exactly (\d+) visual beats")
    return {
        "language": {"code": "en", "name": "English", "native_name": "English"},
        "style_profile": STYLE_PROFILE,
        "scenes": [
            {
                "ordinal": i,
                "title": f"Beat {i}",
                "beat": f"What happens in beat {i}.",
                "prompt": (f"Wide shot of beat {i}, two men on a porch at dusk, "
                           f"moths around a bare bulb, exactly four players at the table."),
            }
            for i in range(1, count + 1)
        ],
    }


def default_ui(kwargs: dict) -> dict:
    """Echo every key back with a marker, so a test can tell translated from source."""
    user = kwargs["messages"][0]["content"]
    body = json.loads(re.search(r"<strings>\n(.*)\n</strings>", user, re.S).group(1))
    return {"items": [{"key": k, "text": f"[fr] {v}"} for k, v in body.items()]}


def default_narration(kwargs: dict) -> dict:
    user = kwargs["messages"][0]["content"]
    count = _count_from(user, r"for each of the (\d+) beats")
    return {
        "lines": [
            {"ordinal": i,
             "text": f"Line {i}. We did not know it yet, but the night had already "
                     f"decided what it wanted from us."}
            for i in range(1, count + 1)
        ]
    }
