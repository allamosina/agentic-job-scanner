# Agentic Job Scanner

A personal job monitoring service built with Python, PostgreSQL, Telegram and optional LLM-assisted evaluation. It collects public vacancies, checks responsibilities against candidate evidence, explains fit, and learns cautiously from explicit feedback and application outcomes.

## Capabilities

- Public Greenhouse, Lever and Ashby connectors, limited JobPosting JSON-LD collection, optional Brave discovery.
- Versioned vacancies, source provenance, evidence-based structured assessments and configurable salary bands.
- Telegram digests, feedback buttons, saved jobs and explanations; access restricted to a configured user and chat.
- Read-only daily Notion application sync, exact application matching, observed stage history and bounded outcome adjustments.
- PostgreSQL migrations, API call caps, source health reporting, deduplicated delivery and retry handling.

The checked-in configs are **synthetic examples**. They are not the author's CV, salary expectations, company watchlists or application history. The service refuses CLI bot/scan/outbound/Notion-sync startup without private configuration.

## Private configuration

Use one complete JSON bundle with four objects: `preferences`, `candidate`, `sources`, `notion`. Each object follows the corresponding YAML example in `config/`.

- Local: keep the bundle in `.private/config.json`, set `PRIVATE_CONFIG_PATH=.private/config.json` in ignored `.env`.
- Railway: set **PRIVATE_CONFIG_JSON** to the complete JSON value in Variables on both bot and scanner services. Do not set the local file path there. Railway supports multiline values; see [Using Variables](https://docs.railway.com/variables). Use the New Variable value field for the bundle; the Raw Editor expects a map of environment variable names to values.
- Large Railway bundles: if a value exceeds 32768 characters, split the serialized JSON into consecutive chunks below that limit and set **PRIVATE_CONFIG_JSON_1**, **PRIVATE_CONFIG_JSON_2**, etc. (up to four). The application joins them verbatim in numeric order before parsing; individual chunks are not standalone JSON. Remove the unsuffixed **PRIVATE_CONFIG_JSON**, leave unused trailing parts unset, and configure the same parts on both services. Do not add quotes, separators, or newlines between chunks. Numbered parts override a local file; missing parts, invalid JSON, and mixing numbered parts with the unsuffixed variable are rejected without exposing contents. Store any generated part files only in the ignored `.private/` directory.
- Environment JSON takes priority over the private file. An invalid private bundle fails; the app does not silently fall back to demo data.
- Keep API tokens and `DATABASE_URL` in separate environment variables listed in `.env.example`.

`.private/`, `.env`, local exports, database dumps, CV documents and archives are excluded from Git and Docker build context. The Dockerfile copies only application code, public examples and migrations. Do not put production exports or logs in tracked files or publish private configuration in issues, build arguments or screenshots.

Vacancies, descriptions, assessments, application snapshots, interview outcomes and reactions belong in PostgreSQL. Migrations insert no history. Tests use synthetic fixtures and require an isolated database ending in `_test` for database tests.

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock
pip install --no-deps -e .
# Create .env from .env.example only if it does not already exist.
job-monitor check-config
job-monitor doctor
```

Python 3.12 or newer is required. `check-config` validates configuration; `doctor` reports credential presence without their values. Run `docker compose up -d db` for a local development PostgreSQL and use the connection details in compose.yaml only for local development. After configuring the database and private settings:

```bash
job-monitor migrate
job-monitor scan
job-monitor bot
```

Telegram commands: `/status`, `/preferences`, `/saved`, `/apps`, `/pause`, `/resume`, `/reset_learning`. Private preferences are shown only in the authorized Telegram chat. `/reset_learning` resets rating influence, not application history.

## Railway

Connect this repository to a Railway project. Use PostgreSQL in Railway or an external PostgreSQL provider; both services must share the same database and private configuration.

1. Set `DATABASE_URL`, `PRIVATE_CONFIG_JSON`, Telegram IDs/token, and optional provider keys in Railway Variables. The repository contains no deployable personal configuration.
2. Run `job-monitor migrate` before starting the services.
3. Bot service: `job-monitor bot`; keep one running replica and disable sleeping. `railway.toml` describes this service.
4. Scanner service: override command to `job-monitor scan --scheduled`, configure cron and disable restart-on-exit for the cron service. For the example 08:00/13:00/18:00 Europe/Prague schedule, UTC cron `30 5,6,10,11,15,16 * * *` covers winter/summer preparation windows. The command checks the configured local window and exits on extra runs. Other schedules need corresponding cron changes.
5. Enable paid APIs only after setting a model and nonzero call caps. Start the Telegram conversation with `/start`; verify one scan, Notion sync and digest before relying on the service.

Notion setup: [NOTION.md](NOTION.md). No deployment is performed merely by cloning the repository. Provider cost limits should be set separately; call-count caps are not dollar budgets.

## Limitations and validation

JSON-LD collection is partial; missing structured data is not proof that a site has no jobs. Exact delivery after an ambiguous Telegram timeout is not guaranteed; uncertain sends are retained for review rather than blindly retried. Pending applications are not negative training examples. Outcome adjustments do not change CV facts or hard eligibility gates.

```bash
pip install -e '.[dev]'
pytest -q
ruff check src tests migrations
# Optional isolated PostgreSQL integration tests; this database's tables are recreated:
TEST_DATABASE_URL='postgresql+psycopg://USER:PASSWORD@HOST/monitor_test' pytest -q
```

See [VALIDATION.md](VALIDATION.md) for verification scope. Real LLM recommendation quality and production delivery require calibration after private service setup.
