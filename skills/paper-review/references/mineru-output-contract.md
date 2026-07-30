# MinerU output contract used by paper-review

The integration uses the public MinerU CLI:

```bash
mineru -p paper.pdf -o <output-directory> -b pipeline
```

It prefers `*_content_list.json`, whose documented items include a zero-based
`page_idx`, a normalized `bbox`, and type-specific fields:

- `image`: `img_path`, `image_caption`, and `image_footnote`;
- `table`: `img_path`, `table_caption`, `table_footnote`, and `table_body`;
- `chart`: image path and chart caption/footnote fields;
- rendered equation blocks may also expose an image path.

The parser also accepts the page-grouped `*_content_list_v2.json` form as a
fallback and recursively checks its `content`, `blocks`, and `children`
containers. Unknown fields are ignored.

Every referenced image path is resolved under the MinerU output directory.
Remote URLs, missing files, and paths that escape that directory are rejected.

Authoritative references:

- MinerU CLI: <https://opendatalab.github.io/MinerU/usage/cli_tools/>
- MinerU output files: <https://opendatalab.github.io/MinerU/reference/output_files/>
- MinerU repository: <https://github.com/opendatalab/MinerU>

MinerU's output contract can evolve. Keep the stable content-list parser as the
default and add fixtures before accepting new layouts.
