# Agentic Job Scanner

A personal job monitoring service built with Python, PostgreSQL, Telegram and optional LLM-assisted evaluation. It collects public vacancies, checks responsibilities against candidate evidence, explains fit, and uses only explicitly confirmed user rules for future recommendations.

## Capabilities

- Public Greenhouse, Lever and Ashby connectors, limited JobPosting JSON-LD collection, optional Brave discovery.
- Versioned vacancies, source provenance, evidence-based structured assessments and configurable salary bands.
- Telegram digests, feedback buttons, saved jobs and explanations; access restricted to a configured user and chat.
- Read-only daily Notion application sync, exact application matching, observed stage history (without automatic ranking adjustments).
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

Scheduled delivery sends vacancy cards and outbound signals only; empty slots stay silent. Technical reports are available via `/status`, not pushed three times a day.

Telegram commands: `/jobs` sends new eligible cards immediately using the existing evaluation, application exclusions, deduplication and live verification; `/scan` runs a background scan within existing API caps and sends the resulting digest. Both require the authorized user/chat and respect pause. A second manual request is refused while the first is running; the bot remains available. No separate scanner service is needed for manual tests. The bot registers its command menu at startup. `/status` remains technical diagnostics. Other commands: `/preferences`, `/saved`, `/apps`, `/pause`, `/resume`, `/reset_learning`. Private preferences are shown only in the authorized Telegram chat. `/reset_learning` resets rating influence, not application history.

## Railway

Use one always-on bot service plus PostgreSQL in the same Railway project. The bot runs collection once daily at 08:00 Europe/Prague, then drains the persisted evaluation queue and sends one digest if there are useful results. On a restart after 08:00 it catches up if today's run was missed. Daily state and locks live in PostgreSQL. No cron scanner service is required; disable any legacy scanner cron to avoid duplicate collection.

1. Set `DATABASE_URL`, private JSON (whole or numbered parts), Telegram token/IDs, and optional provider keys in Variables.
2. Pre-deploy command: `job-monitor migrate`. Start command: `job-monitor bot`. Keep one replica and disable sleeping.
3. Enable paid APIs after setting the model and daily call caps. `/scan` runs collection and evaluation manually; `/jobs` sends eligible existing cards.
4. Existing private bundles are migrated on read to the agreed 08:00 Prague schedule. No recopy of private configuration is necessary.

For a compact search-only update, set `PRIVATE_SEARCH_CONFIG_JSON` to a private JSON object with
`watchlists` (an object of named company lists), `policy_text` (the latest explicit search rules),
and optional `sources` (the same source schema as the main bundle). This replaces the watchlists,
appends the rules to both policy and clarifications, and merges sources by ID. CV evidence, salary
bands, API budgets and application history stay in their existing configuration/storage. Remove
the variable to restore the original search preferences. Never commit a populated value.

Companies without configured ATS coverage enter the existing rotating Brave search queue;
JSON-LD sources retain search fallback because a bounded crawl may miss vacancies. This does not
guarantee every company is searched every day. Existing search credentials, caps and timeouts apply.
Employer interest is not evidence of company quality, pay, culture or country eligibility.

Collection and evaluation have independent deadlines. Saved job versions form the durable evaluation queue; unchanged versions already evaluated under the same profile, rules and model are not re-evaluated merely because a week passed. Failed evaluations receive a 30-minute cooldown; daily processing resumes remaining work later that day. Budget exhaustion defers the remainder to the next daily run and sends one explanatory notice. Live vacancy checks still run before delivery. Technical reports remain available only through `/status`.

The `job-monitor collect` and `job-monitor evaluate` CLI commands can run either stage independently. `job-monitor scan` runs both stages; collection timing out does not cancel subsequent evaluation.

Feedback: use the Comment button or reply directly to a card; the original text is stored against that job. No rule is inferred from comments, reactions, missing reactions or application outcomes. `/rule text` proposes a global preference; only confirmation activates it, and `/rules` lists/removes explicit rules. Outcomes remain available as history, but do not boost recommendation ranking. Confirmed rules are included in model assessment policy and its cache fingerprint, without modifying CV facts.

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
