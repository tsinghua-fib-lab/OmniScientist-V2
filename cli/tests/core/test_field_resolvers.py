"""Fact resolution and grounded lookup share one resolver registry."""

import pytest

from omni.core.field_resolvers import (
    has_resolver,
    has_searcher,
    resolve_field,
    search_field_candidates,
)


def test_fact_resolver_registry_distinguishes_parse_only_and_lookup_adapters() -> None:
    assert has_resolver("arxiv-id")
    assert has_searcher("arxiv-id")
    assert has_resolver("doi")
    assert not has_searcher("doi")
    assert resolve_field("doi", {"identifier": "doi:10.1000/example"}).value == (
        "10.1000/example"
    )


@pytest.mark.asyncio
async def test_unknown_or_parse_only_resolver_has_no_lookup_candidates() -> None:
    assert await search_field_candidates("doi", "A paper title") == []
    assert await search_field_candidates("not-registered", "A paper title") == []
