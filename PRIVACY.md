# Privacy and Data Flow

OmniScientist stores workspaces, sessions, memory, tasks, research objects, and artifacts locally
in SQLite and the filesystem. Local persistence does **not** mean that every operation is offline.

## Data that may leave the machine

- **Model providers:** prompts, selected conversation context, tool observations, and requested
  artifact context are sent to the configured LLM endpoint. Embeddings are sent only when a remote
  embedding provider is enabled.
- **Research connectors:** search terms, paper identifiers, DOIs, and configured contact email may
  be sent to arXiv, OpenAlex, Crossref, Unpaywall, PubMed, or Semantic Scholar.
- **IM channels:** inbound messages and outbound answers/files pass through Feishu, DingTalk,
  WeCom/operator gateways, or an explicitly enabled experimental iLink connector.
- **Update checks:** the installed version may query the configured package/repository endpoint.

OmniScientist does not provide telemetry or a hosted account service by default. Third-party
providers apply their own retention and privacy terms.

## User controls

- Disable remote embeddings or select the offline mock provider.
- Restrict connectors with `research.connectors` and web hosts with `web_fetch.allow_hosts`.
- Disable IM channels and remove their credential/config files with `omni channel remove --purge`.
- Inspect and delete tasks, sessions, memories, and artifacts with their corresponding CLI groups.
- Delete a workspace directory under `~/.omni/workspaces` or named project under
  `~/.omni/projects` to remove its local persisted data after stopping `omni serve`.

Do not submit confidential, regulated, or copyrighted material to a provider unless its terms and
your organization policy permit that processing.
