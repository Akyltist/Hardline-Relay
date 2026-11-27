"""Exact pilot syntax prevents accidental access to another address namespace."""

import pytest

from experiments.pilot_identity import is_pilot_identity


@pytest.mark.parametrize("name", [
    "pilot:local", "pilot:remote", "pilot:new_member", "pilot:ab",
    "pilot:a0", "pilot:a_", "pilot:" + "a" * 48,
])
def test_exact_synthetic_identity_accepted(name):
    assert is_pilot_identity(name)


@pytest.mark.parametrize("name", [
    None, 1, [], {}, "", "admin", "pilot:admin", "codex:new_member",
    "claude:new_member", "pilot:a", "pilot:" + "a" * 49,
    "Pilot:new_member", "pilot:New_member", "pilot:_member", "pilot:0member",
    "pilot:new-member", "pilot:new.member", "pilot:new_member ",
    " pilot:new_member", "pilot:new_member\n", "pilot:new_member\r\n",
    "pilot:new_member\x00", "pilot:new:member", "pilot:n\u0435w_member",
])
def test_other_names_and_partial_matches_rejected(name):
    assert not is_pilot_identity(name)
