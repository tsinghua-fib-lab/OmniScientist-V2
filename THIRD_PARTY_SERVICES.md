# Third-Party Services and Research Data

OmniScientist can call external model, literature, compute, and messaging services. These services
are not operated or guaranteed by the OmniScientist project.

| Service category | Typical data sent | Operator responsibility |
|---|---|---|
| LLM/embedding provider | prompts, context, selected file/artifact text | configure an approved endpoint and review its privacy/retention terms |
| arXiv/OpenAlex/Crossref/PubMed/Semantic Scholar | queries, identifiers, optional contact email | follow API terms, attribution, and rate limits |
| Unpaywall | DOI and required contact email | use only lawful open-access locations |
| Feishu/DingTalk/WeCom/gateway | messages, user/channel identifiers, files | configure app permissions and retention appropriately |
| Experimental WeChat iLink | QR login state, messages, files | explicitly opt in and assess account/service-term risk |
| Docker/SSH/Slurm/Modal | commands, environment, mounted/input files | control compute credentials, isolation, and data residency |

Metadata availability does not grant redistribution rights to article full text, figures, datasets,
or model outputs. Users are responsible for source-specific licenses, quotation limits, personal
data, export controls, and institutional policy. Connector endpoints and terms may change; pin and
document inputs for reproducible research.
