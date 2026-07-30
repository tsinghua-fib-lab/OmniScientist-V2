# Security Policy

## Reporting

Do not open a public issue for a suspected vulnerability. Use
[GitHub private vulnerability reporting](https://github.com/tsinghua-fib-lab/OmniScientist-V2/security/advisories/new)
and prefix the report with `OmniScientist security report`. Include affected versions,
reproduction steps, impact, and any proposed mitigation. Expect an acknowledgement within five
business days. Please allow a reasonable remediation window before public disclosure.

## Supported versions

Until the first stable release, only the latest tagged release and the current `master` branch
receive security fixes.

## Security boundaries

- Imported skills are untrusted code. They remain quarantined until explicitly reviewed and
  trusted by the machine owner.
- `security.bash_sandbox=readonly|workspace-write` includes a command guard. Kernel confinement is
  used only when a functional OS sandbox is available. It is not a substitute for a container.
- Explicit `security.os_sandbox=sandbox-exec|bwrap|firejail` settings fail closed if unavailable.
- IM channels require allow-list/pairing controls and local confirmation for sensitive operations
  by default.
- The experimental WeChat iLink connector is not an official security boundary or supported
  production integration.

Never include real API keys, private papers, personal data, or active access tokens in a report.
