"""Shared short-id and prefix-resolution contract."""

from __future__ import annotations

from omni.core.identifiers import short_id, shortest_unique_prefixes


def test_short_id_uses_the_leading_eight_characters():
    assert short_id("4497f10e7aab1234") == "4497f10e"
    assert short_id("27a6c3fc634143b2") == "27a6c3fc"


def test_shortest_unique_prefixes_extend_colliding_eight_character_prefixes():
    values = ["deadbeef00000001", "deadbeef10000002", "cafebabe00000003"]

    assert shortest_unique_prefixes(values) == {
        values[0]: "deadbeef0",
        values[1]: "deadbeef1",
        values[2]: "cafebabe",
    }
