"""ConnectorRegistry: the single catalogue of curated scientific data sources.

A **connector** is an external data/service source (arXiv, OpenAlex, Crossref,
Unpaywall, and — in future — PubMed / PDB / UniProt). It is deliberately a
different concept from a **skill** (a workflow / methodology) and a **compute
provider** (an execution environment):

* ``SkillRegistry``    → *how* to do research (methodology, workflow).
* ``ConnectorRegistry`` → *where* the evidence comes from (data sources).

Centralising connectors here means the three cross-cutting concerns are defined
in exactly one place instead of being re-derived in each skill engine:

1. **discovery / description** — what each connector provides (planning + UX);
2. **enablement** — the ``research.connectors`` allow-list kill-switch;
3. **secret-scope** — a connector may only read the config/secret keys it
   declares in :attr:`ConnectorSpec.secret_scope` (e.g. Unpaywall receives
   ``contact_email`` and nothing else). A future keyed connector therefore
   cannot reach another connector's credentials.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ConnectorSpec:
    """Static description of one curated connector."""

    name: str
    title: str
    description: str
    provides: tuple[str, ...]          # capability slugs this source can satisfy
    base_url: str
    skill: str = ""                    # the skill that drives this connector, if any
    secret_scope: tuple[str, ...] = () # config/secret keys this connector may read


@dataclass(frozen=True)
class ResolvedConnector:
    """A connector bound to just the scoped secrets it is allowed to read."""

    spec: ConnectorSpec
    secrets: Mapping[str, str] = field(default_factory=dict)


# Curated, vetted connectors. arXiv is described here for discovery/enablement,
# though its skill engine (``arxiv-fetch``) stays self-contained and portable.
_CONNECTORS: tuple[ConnectorSpec, ...] = (
    ConnectorSpec(
        name="arxiv",
        title="arXiv",
        description="Fetch arXiv preprint metadata/abstracts by id or URL.",
        provides=("paper.fetch.arxiv", "literature.search"),
        base_url="https://export.arxiv.org/api",
        skill="arxiv-fetch",
    ),
    ConnectorSpec(
        name="openalex",
        title="OpenAlex",
        description="Search the OpenAlex scholarly graph for works across all fields.",
        provides=("literature.search",),
        base_url="https://api.openalex.org/works",
        skill="openalex-search",
        secret_scope=("contact_email",),  # optional polite-pool mailto
    ),
    ConnectorSpec(
        name="crossref",
        title="Crossref",
        description="Search Crossref DOI metadata for published works.",
        provides=("literature.search",),
        base_url="https://api.crossref.org/works",
        secret_scope=("contact_email",),  # optional polite-pool mailto
    ),
    ConnectorSpec(
        name="unpaywall",
        title="Unpaywall",
        description="Resolve a DOI to its best open-access location.",
        provides=("paper.oa_lookup",),
        base_url="https://api.unpaywall.org/v2",
        secret_scope=("contact_email",),  # required contact email
    ),
    ConnectorSpec(
        name="pubmed",
        title="PubMed",
        description="Search PubMed for biomedical/life-sciences literature (NCBI).",
        provides=("literature.search",),
        base_url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils",
        secret_scope=("contact_email",),  # NCBI polite pool
    ),
    ConnectorSpec(
        name="semanticscholar",
        title="Semantic Scholar",
        description="Search Semantic Scholar (abstracts + citation graph) across all fields.",
        provides=("literature.search",),
        base_url="https://api.semanticscholar.org/graph/v1",
        secret_scope=("semantic_scholar_api_key",),  # optional higher rate limit
    ),
    ConnectorSpec(
        name="biorxiv",
        title="bioRxiv",
        description="Search recent bioRxiv life-sciences preprints.",
        provides=("literature.search",),
        base_url="https://api.biorxiv.org/details",
    ),
    ConnectorSpec(
        name="clinicaltrials",
        title="ClinicalTrials.gov",
        description="Search registered clinical studies, interventions, status, and outcomes.",
        provides=("literature.search", "trial.search"),
        base_url="https://clinicaltrials.gov/api/v2/studies",
    ),
)

# Resolvers map a scoped secret key → where it lives in settings. Only keys a
# connector declares in ``secret_scope`` are ever read for it (secret-scope).
_SECRET_RESOLVERS: dict[str, Callable[[Any], str]] = {
    "contact_email": lambda s: str(getattr(getattr(s, "research", None), "contact_email", "") or ""),
    "semantic_scholar_api_key": lambda s: str(
        getattr(getattr(s, "research", None), "semantic_scholar_api_key", "") or ""
    ),
}


class ConnectorRegistry:
    """Enablement + secret-scope resolution over the curated connector catalogue."""

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._specs: dict[str, ConnectorSpec] = {spec.name: spec for spec in _CONNECTORS}

    def all(self) -> list[ConnectorSpec]:
        return list(_CONNECTORS)

    def get(self, name: str) -> ConnectorSpec | None:
        return self._specs.get(str(name or "").strip().lower())

    def is_enabled(self, name: str) -> bool:
        """Whether connector ``name`` is enabled via ``research.connectors``.

        An empty/missing allow-list means "all enabled" (so a misconfiguration
        never locks the user out); a non-empty list is an explicit allow-list.
        Unknown connector names are never enabled.
        """
        if self.get(name) is None:
            return False
        allow = getattr(getattr(self._settings, "research", None), "connectors", None)
        if not allow:
            return True
        return name in allow

    def enabled(self) -> list[ConnectorSpec]:
        return [spec for spec in _CONNECTORS if self.is_enabled(spec.name)]

    def scoped_secrets(self, spec: ConnectorSpec) -> dict[str, str]:
        """Return only the secrets the connector declared — nothing else."""
        out: dict[str, str] = {}
        for key in spec.secret_scope:
            resolver = _SECRET_RESOLVERS.get(key)
            if resolver is not None:
                out[key] = resolver(self._settings)
        return out

    def resolve(self, name: str) -> ResolvedConnector | None:
        """Bind a connector to its scoped secrets (no enablement check here)."""
        spec = self.get(name)
        if spec is None:
            return None
        return ResolvedConnector(spec=spec, secrets=self.scoped_secrets(spec))

    def catalog_prompt(self) -> str:
        """Compact, model-facing description of the enabled data sources."""
        from omni.research.domain_packs import DomainPackRegistry

        recommended = set(DomainPackRegistry(self._settings).recommended_connectors())
        lines = ["Enabled research data connectors (accessed through skills):"]
        for spec in self.enabled():
            provides = "、".join(spec.provides)
            marker = "; recommended for the active domain" if spec.name in recommended else ""
            lines.append(f"- {spec.name} ({spec.title}): {spec.description} Provides: {provides}{marker}")
        return "\n".join(lines)


__all__ = ["ConnectorSpec", "ResolvedConnector", "ConnectorRegistry"]
