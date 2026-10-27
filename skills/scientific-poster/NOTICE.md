# OmniScientist V2 skill notices

Copyright 2026 OmniScientist Contributors

This standalone skill is part of OmniScientist V2, the official next-generation
implementation of the OmniScientist project introduced in arXiv:2511.16931.

Original source code and original documentation in this directory are licensed
under the Apache License, Version 2.0; the complete terms are included in
`LICENSE.txt`. That license grant does not apply to third-party visual assets,
names, marks, or research content.

Excluded third-party assets:

- `assets/venue-branding/*/logo.svg` contains conference names or marks. Each
  directory has its own `NOTICE.md` with the official source and permitted-use
  context; no Apache-2.0 license or broader trademark right is granted for it.

The default reference images are full-page rasterizations of published posters,
licensed separately under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
They are used as visual references; their scientific content, identity and marks
remain separate from the generated poster:

- `seeds/reading-research-landscape.png`: Christine M. White, Stephanie Estrera,
  Emma Friedman, Dana Santangelo, Cristian Vazquez and Chris Schatschneider,
  *Exploring data availability & reusability in published studies of reading*
  (2025). [Figshare, version 2](https://doi.org/10.6084/m9.figshare.28903940.v2),
  deposited by Christine White. PDF file 54096539, rendered at 2600 pixels on its
  long edge; complete page, unchanged content.
- `seeds/genomics-portrait.png`: Endika Varela-Martínez and coauthors,
  *Claudin and Rab proteins are key molecular components involved in coccidiosis
  resistance in Portuguese Merino sheep* (2025).
  [AL4AnimalS Poster, version 5](https://doi.org/10.6084/m9.figshare.28989440.v5),
  deposited by Endika Varela-Martínez. PDF file 54464345, rendered at 2600 pixels
  on its long edge; complete page, unchanged content.

The five earlier synthetic design boards in `seeds/` are original content-free
assets retained for existing draft bindings. New drafts select published examples.

Research content retrieved while using the skill may likewise be governed by
separate publisher, dataset, or service terms.

The `scientific-poster` implementation was contributed from
`https://gitee.com/zgc-omni/omniscientistv2.git`, branch/path
`zgy/poster/skills/scientific-poster`, commit
`b2104fe288aa4869fe5929cac2ecabb609defcb1`. It was adapted for the active
built-in inventory, Omni research/provenance contracts, standalone licensing,
and portable Claude Code, Codex, and OpenClaw execution.
