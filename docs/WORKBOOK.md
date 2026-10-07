# Workbook Design

Schema v3.2 is a single-queue design. It keeps the daily job-hunting workspace compact while
retaining provenance, scoring, AI, and conflict metadata in collapsed groups.

## Tabs

Visible:

- `Dashboard`
- `Opportunities`
- `Manual Intake`

Hidden until needed:

- `Cold Outreach Leads`
- `Excluded` (definite seniority, cost, location, or relevance exclusions)

Hidden/protected support tabs:

- `Source Config`
- `Lists & Enums`
- `Run Log`
- `Dedupe Index`
- `System Events`

`Technical Roles` and `VA & Freelance` are saved filter views of `Opportunities`, not
separate datasets. The workbook also includes Inbox, Strong Fits, Deadline Soon, Follow-Up
Due, Active Pipeline, and Closed Archive views. Filter views do not reorder the underlying
machine-managed rows.

`jobhunt bootstrap-workbook --backend google` creates a new v3.2 workbook structure,
validations, protected edit zones, formatting, saved views, collapsed detail groups,
dashboard formulas, and two charts. It does not create the spreadsheet itself.

The Google workbook must use the `Asia/Manila` time zone. UTC timestamps remain UTC in
internal logs and are converted to Manila wall-clock time when written as Sheets date-time
serials. The dashboard marks a successful daily run stale after 30 hours, allowing six hours
past the next expected daily run before showing the warning. A latest non-successful run
still displays `ERROR` immediately.

## Daily queue

The daily queue shows 16 columns by default:

1. Pipeline Stage
2. Priority
3. Company
4. Role
5. Track
6. Fit Band
7. Qualification
8. Eligibility
9. Work Arrangement
10. Location
11. Salary
12. Deadline
13. Apply URL
14. Next Action
15. Next Action At
16. Notes

`Application Cost` is still a protected machine-owned column between Work Arrangement
and Location, but is hidden from the everyday view. The free-to-apply check continues to
exclude or flag paywalled and uncertain listings; hiding it does not assume that every
future source will be free. You can unhide the column if you need to inspect a decision.

Final Score, Source, and Date Found remain available in the collapsed Review Details group.
That group also shows seniority, required experience, location eligibility, compensation,
and the exact qualification reasons. `Eligibility` is the combined result; a definite
location or qualification failure wins, followed by manual review, then eligible.

`Skip` is no longer a visible queue option. Definite exclusions are written to the hidden,
protected `Excluded` tab and still indexed for deduplication. If later source facts change,
the record can move between tables while preserving human-owned notes and workflow fields.

Scheduled live runs also health-check existing Inbox listings. A Himalayas source expiry,
two exact "Job not found" results from separate runs at least 20 hours apart, or Jobicy's
two consecutive 404/410 results move a stale listing to `Excluded`; it is never deleted.
Ambiguous checks do nothing to the row. The hidden `System Events` tab records successful,
inconclusive, unavailable, deferred, and archived observations for troubleshooting.

## Manual intake

Use one row per listing from a manual-only platform. Required fields are Platform, Listing
URL, Company, and Role. Add Work Arrangement, Location, Application Cost, and only a short
user-selected description/requirements excerpt when available. The runtime never opens or
scrapes the URL. On a successful write it records `Imported`, the destination table, and the
immutable Record ID in protected status columns.

The header and columns A-C are frozen. Review Details, Application Details, and System
Details are collapsed initially and can be expanded using the column-group controls.
Pale-blue headers identify human-editable fields; light-gray headers identify machine
fields. Protection is a usability guardrail, not a security boundary.

## Independent workflow states

`Pipeline Stage` tracks the user's real-world progress:

`Inbox -> Shortlisted -> Preparing -> Submitted -> Interview/Call -> Offer -> Won`

`Closed` is terminal and should have a `Closed Reason` and `Closed At` date. Missing dates
for Submitted, Interview/Call, or Closed are warnings, not edit blockers. Changing a stage
never applies, submits, sends, or contacts anyone.

`Draft Review` separately tracks optional draft quality:

`Not needed | Needs review | Approved | Discarded`

A role can move to Submitted without AI or a draft. A draft can be marked Approved only
when a draft exists and its evidence IDs validate.

## Ownership and conflict handling

Human-owned opportunity fields are Pipeline Stage, Priority, Deadline, Next Action, Next
Action At, Notes, Draft Review, Materials Used, the submitted/response/interview dates,
contact fields, Closed Reason, and Closed At. The runtime initializes these on new rows but
does not overwrite them on refresh.

Before a machine update, the runtime resolves the row by immutable Record ID and compares a
canonical hash of human-owned fields. Concurrent human edits are preserved and the machine
update is sent to conflict review. Dates are normalized before hashing so Google date serials
and equivalent ISO/Python dates do not create false conflicts.

## Dashboard

The dashboard contains:

- A no-automatic-action safety banner and run-health warning
- Inbox, Strong Fits, Due/Overdue, and Active Pipeline KPI cards
- Links to the primary saved views
- A next-seven-days action queue
- One pipeline chart
- One weekly submissions/responses chart

Before the first run, health reads `NOT STARTED`. It becomes stale only after a successful
run is more than 18 hours old. All eight saved views are linked in a two-row Quick Views
area. Charts sit below the action queue so they remain visible at ordinary laptop widths;
their helper data and the schema marker are kept below the presentation area.

## Migrating a v2, v3.0, or v3.1 workbook

Migration is always explicit. Preview first; this makes no workbook changes:

```powershell
jobhunt migrate-workbook --backend google --spreadsheet-id <id>
```

Review the JSON report, then apply only when ready:

```powershell
jobhunt migrate-workbook --backend google --spreadsheet-id <id> --write
jobhunt validate-workbook --backend google --spreadsheet-id <id>
```

The write command creates a local pre-migration snapshot. For v3.0/v3.1 it reorders populated
rows by header name, preserves all values, recomputes human-field hashes, and reconciles the
generated views, charts, formatting, protection, and collapsed detail group. For v2 it also
merges rows by immutable Record ID, creates and verifies `Opportunities`, then renames and
hides the old sheets as `Technical Roles (v2 archive)` and `VA & Freelance (v2 archive)`.
It never deletes the archives automatically.

The v3.2 migration writes and verifies `Opportunities` and `Excluded` together before it
changes the dashboard or legacy archives. Existing rows already marked Skip/Skipped move to
`Excluded`; other existing rows remain visible until refreshed through the new rules.

Migration stops before writing when it detects populated unknown columns, duplicate record
IDs, conflicting duplicate data, an unrecognized `Opportunities` header, or pre-existing
archive names. Notes, priorities, workflow dates, outcomes, IDs, and other recognized human
data are mapped by header name rather than column position.

## Restore

Snapshots contain schema version, timestamp, workbook ID, and normalized values for all
tabs. The safe default restores only into a blank workbook:

```powershell
jobhunt restore-backup --backend google --spreadsheet-id <id> --input snapshot.json --blank-only
```

Use `--no-blank-only` only as a separately reviewed recovery action because it can
overwrite existing workbook content.
