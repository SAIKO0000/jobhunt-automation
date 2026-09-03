# Case study: a zero-budget, policy-aware job-search assistant

## The problem

Searching for early-career technical and virtual-assistant work involves repeatedly checking
job boards, removing obviously unsuitable roles, and recording enough context to decide what
to do next. A conventional “auto-apply” bot would reduce clicks, but it would also create
platform-policy, privacy, quality, and accountability risks.

The goal was narrower: automate opportunity discovery and organization while keeping every
application, proposal, and message under human control.

## Constraints

- **No platform budget.** The recurring system could not depend on paid hosting, paid
  orchestration, or paid AI requests.
- **Permission before reach.** Restricted job platforms could not be scraped or accessed
  through browser automation. A source had to expose a reviewed public feed or API before it
  could be enabled.
- **No autonomous outreach.** Approval in a spreadsheet could not submit a form or send a
  message.
- **Data minimization.** OAuth credentials, workbook snapshots, logs, résumé contact details,
  and live job-search activity could not enter the repository.
- **Early-career realism.** Explicit senior roles, excessive experience requirements, unpaid
  work, and applicant fees needed to be removed without hiding uncertain cases from review.

## Design decisions

### Local Python instead of hosted orchestration

Windows Task Scheduler invokes a typed Python CLI once per day. This avoids a cloud bill and
keeps credentials on the user’s computer. n8n was evaluated but rejected: it would add a
server, credential store, upgrades, and another failure surface while the source policy,
qualification, deduplication, and Sheets recovery logic would still need custom code.

### Deny-by-default acquisition

Each adapter has an explicit endpoint and host allowlist, field policy, rate limit, retention
rule, and owner-approval gate. A live run requires both configuration approval and the
`--live` flag. Source failures remain partial failures and cannot activate a scraper or a
different source automatically.

Himalayas and Jobicy are the only currently enabled feeds. LinkedIn, Indeed, JobStreet,
Upwork, and similar platforms remain manual intake sources.

### Explainable qualification and scoring

The pipeline normalizes records into typed models, strips recognized tracking parameters,
and deduplicates by source ID, canonical URL, and a cross-source fingerprint. Hard gates
handle applicant cost, location, seniority, and mandatory experience before a bounded rule
score is calculated.

Definite exclusions are retained in a protected archive instead of cluttering the daily
queue. Uncertain evidence becomes `Needs Manual Review`; it is never silently treated as
eligible.

### Sheets as an interface, not a database

The workbook exposes a compact daily queue and collapses review, application, and system
metadata. Human-owned fields—stage, priority, next action, dates, and notes—remain editable.
Machine writes resolve rows by immutable IDs and compare hashes of human fields immediately
before updating.

Before each mutating run, the system creates a local snapshot and persists a pending result.
Ambiguous Sheets failures are reconciled by run ID before replay, preventing blind duplicate
writes.

### Optional AI behind separate gates

Gemini and Ollama share a provider-neutral structured-output contract, but both are disabled.
AI requires a command-line switch, source permission, verified profile claims, privacy
acknowledgement, and a secure credential. There is no paid fallback and AI failure can only
send a record to manual review.

## Result

The local scheduled workflow runs successfully from acquisition through workbook update. In
a September 3, 2026 operational sample, it processed 119 listings from two sources:

| Classification | Count |
|---|---:|
| Actionable | 15 |
| Needs manual review | 3 |
| Excluded from the daily queue | 101 |
| Quarantined as malformed or unsafe | 0 |

This demonstrates the purpose of the pipeline: reduce a large discovery batch to a small,
explainable review queue while preserving audit and recovery information.

The automated suite currently has 166 passing tests and 84.41% coverage. It exercises source
parsing, host and response limits, formula-injection protection, qualification boundaries,
deduplication, workbook migration, human-edit conflicts, pending-run replay, credential
failures, and the no-outbound-action invariant.

## Limitations and next steps

- The sample is evidence that the system operates, not that it improves callback or hiring
  rates.
- Two approved feeds cannot represent the entire Philippine or global entry-level market.
- Deterministic language rules can still misread a nuanced description.
- Google Sheets protections are usability guardrails, not tamper-evident authorization.
- The scheduler and secure credential backend are Windows-specific.
- AI value has not been established because profile claims and providers remain disabled.
- Employer-specific Greenhouse, Lever, and Ashby adapters need separately reviewed board
  identifiers before activation.

A responsible next iteration would measure reviewer decisions over time, tune rules from
false-positive and false-negative examples, and activate new sources only when their public
interfaces and applicant costs remain compatible with the project’s constraints.
