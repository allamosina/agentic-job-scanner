# Validation scope

The monitoring implementation has automated coverage for policy gates, structured evidence validation, compensation normalization, source parsing, daily Notion sync, application identity, observed-stage preservation and Telegram delivery safeguards.

PostgreSQL integration tests operate only on an isolated database ending in `_test`. Migrations create empty tables; RLS is enabled for application tables. Public test fixtures are synthetic. Production vacancies, application snapshots, CV excerpts and provider credentials are not test artifacts.

Privacy checks cover complete private bundle loading, environment precedence, invalid-bundle failure, preservation of the operational private configuration, Git/Docker exclusions and inspection of the rewritten commit tree.

Live OpenAI/Brave requests, real Telegram delivery and Railway deployment are not claimed by offline tests. Notion HTTP behavior is tested with mock transport; production authorization requires a separately configured integration.

After private/public separation: 91 tests passed, including isolated PostgreSQL tests; Ruff passed. The private bundle was checked against the original four configuration files without printing their contents.
