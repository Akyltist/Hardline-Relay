"""Strict synthetic pilot names; these are not production agent identities."""

import re


_PILOT_IDENTITY = re.compile(r"pilot:[a-z][a-z0-9_]{1,47}")
_RESERVED = frozenset({"pilot:admin"})


def is_pilot_identity(value: object) -> bool:
    """Accept an exact lowercase ASCII pilot name with 2 to 48 characters."""
    return (isinstance(value, str) and _PILOT_IDENTITY.fullmatch(value) is not None
            and value not in _RESERVED)
