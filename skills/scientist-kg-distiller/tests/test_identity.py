from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from kg_distiller.identity import IdentityAmbiguityError, IdentityResolver
from kg_distiller.models import IdentityCandidate


class _OfflineIdentityHttp:
    """Provider fixture that answers every identity request from memory."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        del params, headers
        self.urls.append(url)
        if url.endswith("/authors"):
            return {
                "results": [
                    {
                        "id": "https://openalex.org/A123",
                        "display_name": "Ada Lovelace",
                        "display_name_alternatives": ["Augusta Ada King"],
                        "last_known_institutions": [
                            {"display_name": "University of London"}
                        ],
                        "topics": [
                            {"topic": {"display_name": "Mathematics"}}
                        ],
                        "works_count": 12,
                        "cited_by_count": 500,
                    }
                ]
            }
        if url.endswith("/works"):
            return {"results": [{"title": "Analytical Engine Notes"}]}
        if "semanticscholar.org" in url:
            return {
                "data": [
                    {
                        "authorId": "S456",
                        "name": "Ada Lovelace",
                        "paperCount": 10,
                        "citationCount": 450,
                        "papers": [
                            {
                                "title": "Analytical Engine Notes",
                                "fieldsOfStudy": ["Mathematics"],
                            }
                        ],
                    }
                ]
            }
        if "wikidata.org/w/api.php" in url:
            return {"search": []}
        raise AssertionError(f"Unexpected offline identity URL: {url}")


class IdentityResolverTest(unittest.TestCase):
    def test_resolve_merges_provider_records_and_auto_selects_identity(self) -> None:
        http = _OfflineIdentityHttp()
        resolver = IdentityResolver(http=http)

        candidates = resolver.resolve(
            "Ada Lovelace",
            field="mathematics",
            institution="University of London",
        )

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(
            candidate.provider_ids,
            {"openalex": "A123", "semantic_scholar": "S456"},
        )
        self.assertIn("cross-provider work-title agreement", candidate.evidence)
        self.assertGreaterEqual(candidate.score, 0.78)
        self.assertIs(resolver.choose(candidates), candidate)
        self.assertTrue(all(url.startswith("https://") for url in http.urls))

    def test_choose_requires_explicit_selection_for_ambiguous_candidates(self) -> None:
        resolver = IdentityResolver(http=_OfflineIdentityHttp())
        candidates = [
            IdentityCandidate("openalex:A1", "Same Name", score=0.60),
            IdentityCandidate("openalex:A2", "Same Name", score=0.55),
        ]

        with self.assertRaisesRegex(IdentityAmbiguityError, "Identity is ambiguous"):
            resolver.choose(candidates)

        selected = resolver.choose(candidates, selected_candidate_id="openalex:A2")
        self.assertEqual(selected.candidate_id, "openalex:A2")
        self.assertEqual(selected.score, 1.0)
        self.assertIn("explicitly selected by user", selected.evidence)


if __name__ == "__main__":
    unittest.main()
