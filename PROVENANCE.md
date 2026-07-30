# Code and Skill Provenance

## Project lineage

OmniScientist V2 is the official next-generation implementation of the OmniScientist framework
introduced by Shao et al. in *OmniScientist: Toward a Co-evolving Ecosystem of Human and AI
Scientists* (arXiv:2511.16931, https://doi.org/10.48550/arXiv.2511.16931). It is a local-first
distillation of the maintainers' earlier HelixForge codebase.
Modules that retain lineage state it in their module documentation. The copyright holder has
authorized redistribution of those migrated portions under Apache-2.0; subsequent changes are
tracked in this repository history.

## Bundled skills

- OmniScientist research skills are maintained by this project under Apache-2.0.
- `skills/playwright` is an Apache-2.0 adaptation of the OpenAI skills repository item
  (https://github.com/openai/skills/tree/main/skills/.curated/playwright); changes and attribution
  are recorded in its `NOTICE.md`, root `NOTICE`, and frontmatter.
- `skills/summarize` is adapted from the MIT-licensed OpenClaw project; the required MIT notice and
  modification statement are retained in its `LICENSE.txt`, `NOTICE.md`, root `NOTICE`, and
  frontmatter (https://github.com/openclaw/openclaw/tree/main/skills/summarize).
- Every bundled skill carries its own `LICENSE.txt` and `NOTICE.md` so a direct folder copy or
  `omni skills export` remains a complete standalone distribution.
- Anthropic's proprietary document skills are not bundled. The project may interoperate with a
  separately installed skill only when the user has lawful access and explicitly imports/trusts it.

External skills are never treated as project-owned merely because their SKILL.md parses. Import
metadata records source and commit, and owner trust is required before execution.

## Research content

Connector results may reference metadata, abstracts, full text, figures, or datasets with their own
licenses. OmniScientist records source identifiers but does not grant redistribution rights. See
`THIRD_PARTY_SERVICES.md`.
