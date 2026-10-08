# Case study: a zero-budget, policy-aware job-search assistant

## Project facts for portfolio reuse

This section is the factual source for a portfolio page; keep dated results and image captions
attached to their evidence rather than presenting them as current or typical outcomes. The
tables below are editorial reference material, not a requirement to reproduce tables on the
website verbatim.

| Item | Verified project fact |
|---|---|
| Creator and role | Mark Daniel Iguban — sole designer and developer; built for personal use. |
| Status | Working local Windows workflow with daily scheduled runs. The source code is public, but the Google Sheets workbook is private. There is no hosted or self-service live demo. |
| Source code | [GitHub repository](https://github.com/SAIKO0000/jobhunt-automation), MIT licensed. A repository link is the appropriate public call to action. |
| Main stack | Python 3.12, Pydantic, `httpx`, `tenacity`, Windows Task Scheduler, Google Sheets API with desktop OAuth, Windows Credential Manager, local snapshots, and `pytest`. n8n and cloud runtime are not used. |
| Active acquisition | Himalayas and Jobicy public feeds are the only enabled automated sources as of October 8, 2026. Restricted job sites require manual intake. |
| AI status | Gemini and Ollama integrations are implemented but disabled; the documented production workflow uses deterministic rules. |
| Cost boundary | No recurring platform charge in the chosen setup; it uses an existing Windows computer, disk, electricity, and internet connection. |

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
  and personal workflow data stay out of the repository. Reviewed screenshots may show public
  listing metadata, but not private notes, account details, or workbook links.
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
explainable review queue while preserving audit and recovery information. The counts match a
successful Task Scheduler entry in the private September 3 Run Log snapshot; the private
snapshot and workbook ID are not published.

The [dashboard](assets/dashboard.png) and [Technical opportunities view](assets/opportunities.png)
are authentic captures from October 7, 2026, not images of the September 3 sample above.
The first two pictured Himalayas listings returned exact-job details on the capture date;
other visible links are not all independently verified and may expire. The screenshots show
the interface and a later queue state, not proof of applications or hiring outcomes.

As checked October 8, 2026, the automated suite has 198 passing tests and 84.50% statement
coverage. It exercises source parsing, host and response limits, formula-injection protection,
qualification boundaries, deduplication, conservative listing-availability checks, workbook
migration, human-edit conflicts, pending-run replay, credential failures, and the
no-outbound-action invariant.
Coverage is a measure of executed code statements, not a success or reliability rate.

## Evidence and image guide

| Website claim or asset | Evidence and date | What it supports—and does not |
|---|---|---|
| 119 listings classified as 15 actionable, 3 needing review, and 101 excluded | Successful scheduled Run Log entry in a private September 3, 2026 snapshot | One operational sample. It does not establish hiring outcomes, a typical daily yield, or the state pictured in the later screenshots. |
| [Dashboard screenshot](assets/dashboard.png) | Authentic private-workbook capture, October 7, 2026, after the 18:10 Manila run | The local-only safety banner, run health, and review interface at that moment. Its live totals are not the September 3 sample. |
| [Technical view screenshot](assets/opportunities.png) | Authentic capture, October 7, 2026; the first two pictured Himalayas listings returned exact-job details that day | The queue's fields and saved Technical view. Other pictured links were not all independently checked and no listing is guaranteed to remain open. |
| [Architecture diagram](assets/architecture.svg) and [narrow-screen diagram](assets/architecture-mobile.svg) | Designed illustrations of the checked-in local workflow | System relationships and the manual-action boundary. These are diagrams, not product screenshots. |
| 198 tests; 84.50% statement coverage | Local `pytest --cov=jobhunt` run, October 8, 2026 | Automated-test execution at that revision, not a success rate or proof that every integration path was exercised. Re-run before publishing a later revision. |

Use the Dashboard image for the overview and the Technical view for the filtering/review
section. Keep their capture dates and limitations in captions; make the wide Technical image
expandable rather than shrinking its row text to an unreadable thumbnail. Suggested captions:

- **Dashboard:** “Private Google Sheets workbook, October 7, 2026. The daily local run was
  healthy; no application or message was sent automatically.”
- **Technical view:** “Technical review queue, October 7, 2026. Public listings are dated
  snapshots; users check the original source before applying manually.”
- **Architecture:** “Illustrative local workflow; applications and messages remain manual.”

A private draft VA/Freelance capture is intentionally not among the published assets because
some pictured listings had not passed the link verification needed to present them as a
current queue.

## Portfolio claim boundaries

**Accurate short description:** A local Windows pipeline checks two approved public job
feeds daily, applies explicit qualification and scoring rules, and prepares a Google Sheets
review queue. The user decides whether to apply and takes that action manually.

Do not describe this as a hosted service, a public live demo, an auto-apply bot, automated
outreach, LinkedIn/Indeed/JobStreet scraping, or AI-powered production matching. Gemini and
Ollama code paths exist but are disabled in the current configuration. Do not imply that
every pictured link is still live, that the sample yields are typical, or that the project
improved callback, interview, or hiring rates. “Zero-budget” means no recurring platform
charges in the chosen setup; an existing Windows computer, disk, electricity, and internet
access are still required.

## Limitations and next steps

- The sample is evidence that the system operates, not that it improves callback or hiring
  rates.
- Two approved feeds cannot represent the entire Philippine or global entry-level market.
- Deterministic language rules can still misread a nuanced description.
- Google Sheets protections are usability guardrails, not tamper-evident authorization.
- The scheduler and secure credential backend are Windows-specific.
- AI value has not been established because providers remain disabled; owner-approved claims
  have not been used to validate AI output in production.
- Employer-specific Greenhouse, Lever, and Ashby adapters need separately reviewed board
  identifiers before activation.

A responsible next iteration would measure reviewer decisions over time, tune rules from
false-positive and false-negative examples, and activate new sources only when their public
interfaces and applicant costs remain compatible with the project’s constraints.
