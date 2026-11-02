# Security reporting

Use GitHub's private vulnerability reporting for issues that could expose
credentials, evidence or local files; execute unintended commands; bypass local
API authentication; or corrupt another user's review history.

Open this repository's **Security → Advisories → Report a vulnerability** form
when that feature is enabled. The report is private. If the button is absent,
open an issue asking for a private security reporting channel, with only the
affected component and version. Do not put exploit instructions, credentials or
personal data in that public request. Repository administrators can enable the
feature using GitHub's [private vulnerability reporting instructions](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository).

Include the affected release or commit, operating system, affected API or UI
path, expected and observed behavior, impact and a minimal reproduction. Prefer
a synthetic source record and a fresh temporary data directory. If relevant,
describe whether the result occurs in source execution, a frozen Windows build
or both. Verify the issue against the latest tagged release or default branch
when practical.

Do not attach `server.json`, live instance tokens, model API keys, database
copies, authenticated browser profiles, private source posts or unrelated local
files. Replace secrets and personal information in logs and screenshots with
explicit placeholders. A normal issue is appropriate for non-sensitive bugs or
feature requests.

## Local trust boundaries

The service is intended for loopback use on a user's machine. Its token and
database are local application data; they are not a public server deployment
configuration. The database can contain original evidence, review text and model
credentials. Do not publish that data or expose the service through a public
listener or port forwarding.

Collected posts, comments, imported files and model responses are untrusted
content. They must not become instructions to run commands, send messages,
change accounts or edit settings. Model suggestions remain separate from human
and external reviews, with source references and version checks.

Security reports are especially useful for loopback authentication/origin
checks, unsafe URL handling or redirects, subprocess argument handling,
credential disclosure, untrusted-content rendering and task/review lifecycle
isolation. External platform restrictions, market coverage and model quality
are separate from these security boundaries.
