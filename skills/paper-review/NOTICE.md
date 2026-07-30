# OmniScientist V2 skill notices

Copyright 2026 OmniScientist Contributors

This standalone skill is part of OmniScientist V2, the official next-generation
implementation of the OmniScientist project introduced in arXiv:2511.16931.

The skill is licensed under the Apache License, Version 2.0. The complete terms
are included in `LICENSE.txt`. Research content retrieved while using the skill
may be governed by separate publisher, dataset, or service terms.

The venue-aware paper-review implementation was contributed from
`https://gitee.com/zgc-omni/omniscientistv2.git`, branch/path `paper_review`,
commit `2f75b9f5a7d20dc744eead1f1100a9673596f88a`. It was adapted from the
upstream `omniscientist-paper-review` identity to the repository's canonical
`paper-review` provider and enhanced with Omni routing, bounded prompt
execution, standalone licensing, and portable research provenance.

The integrated visual-analysis stage invokes MinerU through its documented
command-line interface and parses its documented output files; it does not
include or modify MinerU source code. MinerU is available at
<https://github.com/opendatalab/MinerU> under its own license and model terms.
The visual-review design is informed by public multimodal paper-review systems,
including Ai-Review, but the implementation in this skill is independent and
operates on MinerU-extracted crops rather than screenshots of every PDF page.
