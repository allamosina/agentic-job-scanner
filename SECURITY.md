# Security and data boundaries

This is a single-user service. Its PostgreSQL and Railway environments must remain private. Candidate evidence and policy are sent to the configured LLM provider for assessment; relevant search queries go to the configured search provider, and cards go to the authorized Telegram chat. These services are part of the operational data flow, not public GitHub storage. OpenAI calls request `store=False`; this is not a claim of zero provider-side retention.

## Controls reviewed before publication

- No real candidate profile, salary thresholds, Notion identifiers, application results or credentials in the published branch history. Public configs and test fixtures are synthetic.
- Private environment/file configuration fails on malformed input; no silent fallback to demonstration data. Secret settings are excluded from object repr and CLI errors omit raw exception content.
- Telegram handlers verify both user and chat; callback mutations additionally reference a delivered card. There is no public web API or unauthenticated application endpoint.
- SQLAlchemy queries use bound values. Migrations enable RLS on application tables without public policies. The server uses its own database connection; never expose database owner credentials in a frontend.
- Notion is read-only and uses a fixed API origin without automatic redirects. Snapshots are applied atomically; imported text cannot execute actions or rewrite hard user preferences.
- Vacancy HTTP requests reject local/private/mixed DNS addresses, unsupported schemes, credentials in URLs and nonstandard ports. Connections are pinned to the checked IP with original Host/TLS SNI, and every redirect is checked. Credential headers/query parameters are not forwarded to redirect destinations. The public fetch client ignores environment proxy settings and caps response size and redirect count.
- TLS certificate verification remains enabled. IP-based transport pooling disables keepalive to avoid sharing one TLS session between different origins on the same IP. Implementation uses the documented [HTTPX SNI extension](https://www.python-httpx.org/advanced/extensions/).
- Docker runs as an unprivileged user and copies only code, examples and migrations. `.private`, `.env`, database dumps, local data and workspace artifacts are excluded from Git/build context.
- Empty initial database, explicit paid-API activation, bounded API call counts, limited retries and no automated employer outreach.

## Verification on 2026-09-08

97 automated tests passed, including isolated PostgreSQL tests and security regressions for DNS pinning, mixed-address rejection, redirect credentials, metadata endpoints, secret repr and unauthorized Telegram commands. A public HTTPS API smoke check verified certificate/SNI handling and preservation of the original request hostname without transmitting private input.

`pip-audit` checked all 31 locked runtime dependencies: no known vulnerabilities reported, no skipped packages. This is a dated advisory-database result, not a guarantee that vulnerabilities do not exist. The public Git tree/history and Docker image were also checked for private artifacts.

## Deployment responsibilities

Configure Railway Variables privately and restrict collaborator access. Use a private PostgreSQL network or verified TLS endpoint, keep backups private, and avoid publishing production logs or `/apps` exports. Keep provider keys separate from the private configuration bundle. Re-run dependency checks after upgrades.

Publish only the intended branch (`git push origin main`). Do not mirror internal workspace refs or upload `.git`/workspace archives: local editor checkpoints and reflogs are not public deliverables. No production penetration test or Railway account-permission audit has been performed; the service is not deployed yet.
