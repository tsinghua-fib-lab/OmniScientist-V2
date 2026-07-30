# Resource evolution

Reusable resources are optional design knowledge, never a form the user must fill. There are exactly two kinds:

- component: `component.json`, content-free `fragment.html`, and local `style.css`;
- layout policy: `policy.json`, adaptive `guidance.md`, and reusable `style.css` tokens/helpers.

Components may be atoms or composites. They control internal structure, not page placement. A layout policy may describe typography, margins, gutter ranges, spans, and evidence emphasis, but cannot assign semantic regions to fixed positions.

## Three layers

- built-in: reviewed packages shipped with the Skill;
- project candidate: experimental packages under `<cwd>/.scientific-poster`, excluded from normal generation;
- user-approved: exact packages explicitly promoted by the user.

Every package version has an immutable content SHA-256 over its exact three files. The registry rejects path escape, symlinks, identity reuse with different bytes, and index/hash mismatch.

## Lifecycle

1. `query-resource` resolves one active default per resource id by kind, semantic role, and optional page mode. Normal generation uses user-approved defaults before built-ins; explicit candidate testing uses project defaults before user and built-in layers.
2. `propose-resource` validates and copies a new exact version into the project candidate layer.
3. Test the candidate in real poster HTML and pass static plus Chromium checks.
4. Ask for `APPROVE SCIENTIFIC-POSTER <kind> <content_sha256>` and call `promote-resource` with the same id, version, hash, session, and literal approval.
5. `rollback-resource` only repoints a project or user default to an already present immutable version.

Promotion never rewrites an existing version. Built-in publication remains a Git/release operation. Do not add finished claims, example paper data, logos, fixtures, starter posters, or an `assets/` directory.
