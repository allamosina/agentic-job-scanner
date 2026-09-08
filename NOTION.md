# Read-only Notion application tracking

Create a Notion internal integration with **Read content**, share the application database with it, and set `NOTION_API_KEY` and `NOTION_SYNC_ENABLED=true` on bot and scanner services. Put the actual data-source ID, property names and status mappings in the private bundle's `notion` section. Public IDs are placeholders. Chat connector credentials are not server credentials.

The application uses the [Notion data-source query API](https://developers.notion.com/reference/query-a-data-source) and verifies the schema before importing all pages. It does not write to Notion. A successful snapshot is committed atomically into PostgreSQL; failed pagination preserves prior exclusions. Missing records retain previously known application exclusions and are withheld from outcome learning.

```bash
job-monitor migrate
job-monitor notion-sync --force
job-monitor applications
```

The bot checks a shared, persistent local-date marker every 30 minutes; one complete daily import runs in the configured timezone. Failed attempts retry after the configured delay. Scanner uses the same lock and marker. Once Notion integration is enabled, absent or stale successful data defers job cards until sync recovers. This state appears in the digest. Daily sync can take until the next day to observe a new application; the Telegram Applied button or a forced sync can exclude it sooner.

`Not started` remains available. Applied, interview, offered and rejected records exclude only the matched vacancy, not the employer. URLs/ATS IDs and company must agree, or a local explicit link is needed. Unknown statuses and conflicting URLs are flagged. A plain role description is not converted into an invented URL.

```bash
job-monitor link-application NOTION_PAGE_ID INTERNAL_JOB_ID
job-monitor unlink-application NOTION_PAGE_ID INTERNAL_JOB_ID
```

LinkedIn-to-ATS correspondence is not guaranteed without confirmed identity. `/apps` and the CLI report unmatched applications. The CLI output itself is private operational data and must not be committed or posted publicly.

Application snapshots and events are separate from Telegram ratings. Event timestamps mean observation time, not inferred interview dates. Maximum observed progress remains after rejection. Old rejections have an unknown preceding stage; interviews between daily checks cannot be reconstructed. Reusing a Notion page for another role does not transfer previous progress.

Outcome reporting groups by known role track and application channel. A track comes from the assessment of a linked vacancy or an explicit private `track_overrides` entry; otherwise it is UNCLASSIFIED and does not affect ranking. Duplicated/conflicting records do not amplify learning.

Small groups have zero influence. Confirmed progression gives a small positive adjustment after the configured sample threshold. Final offer/rejection conversion is compared with other known tracks within the same channel, with smoothing and minimum sample sizes on both sides. Pending applications do not enter final-outcome denominators. These are empirical signals, not causal explanations for rejection.

Adjustments only change order inside eligible groups and are bounded by `learning_max_adjustment`; set it to zero to disable this influence without deleting history or exclusions. Hard constraints, CV facts and scores remain unchanged. Comments are stored as data, never executed as instructions or automatically promoted to hard preferences.
