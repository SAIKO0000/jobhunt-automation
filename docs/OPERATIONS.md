# Local operations

## Safe sequence

Run fixture-only validation first:

```powershell
jobhunt validate-config
jobhunt run --dry-run --fixture-dir tests/fixtures
jobhunt bootstrap-workbook --backend local
jobhunt run --write --backend local --fixture-dir tests/fixtures
jobhunt validate-workbook --backend local
```

For an existing schema-v2, v3.0, or v3.1 workbook, use the explicit migration path instead
of bootstrap:

```powershell
jobhunt migrate-workbook --backend google --spreadsheet-id <id>
jobhunt migrate-workbook --backend google --spreadsheet-id <id> --write
jobhunt validate-workbook --backend google --spreadsheet-id <id>
```

The first command is a read-only preview. The write command creates a local snapshot before
changing the workbook and leaves both old opportunity tabs as hidden read-only archives.
Warnings reported by validation do not make the command fail; schema, ownership, or draft
evidence errors do.

The checked-in live search divides responsibilities deliberately. Himalayas targets
entry-level virtual-assistant, software-development, full-stack, and automation listings
available to Philippine or worldwide applicants. Jobicy requests APAC and worldwide
engineering listings. Each source may contain several approved endpoints; successful query
results are retained when a sibling query fails, while a source with no successful query
fails closed for that run. Hard exclusions for location, seniority, experience, unpaid work,
and applicant charges apply equally to both tracks.

## Manual-only job boards

For LinkedIn, JobStreet, Indeed, PhilJobNet, OnlineJobs.ph, Wellfound, Upwork, Remote OK,
and We Work Remotely:

1. Use the platform's own search or alert.
2. Manually decide which listing is worth importing.
3. Confirm there is a direct, genuinely free application route, then add its minimal facts
   to `Manual Intake`; do not paste a full restricted-platform page or bypass a paywall.
4. Run the normal pipeline with `--write` (no `--live` is needed for manual intake).
5. Review the resulting `Opportunities` row or, when excluded, the recorded import message.

This path never opens the listing, signs in, parses an alert mailbox, or sends an application.

Google setup is a separate user action. Use `credentials status` to check only presence and
backend metadata; secret values are never displayed. `credentials delete-google` deletes the
local copy but does not revoke access at Google, so revoke server-side access separately when
needed.

## Recovery

Every write creates local pre-schema and pre-run snapshots. If the Sheets commit fails,
retain `runtime/pending/<run-id>.json` and use:

```powershell
jobhunt replay-pending --backend google --spreadsheet-id <id> --run-id <uuid>
```

Replay first checks whether the run is already logged. It snapshots the current workbook and
then applies the normal idempotency and human-edit conflict rules.

## Scheduling

The registration script creates one logged-in-user task with a 07:17 local trigger,
StartWhenAvailable, IgnoreNew overlap handling, a 15-minute limit, one retry after five
minutes, and a network requirement. It stores no Windows password. Both registration and
removal support `-WhatIf`; neither script is run automatically by repository setup.

Scheduled output uses `--summary-only`, so logs contain run metadata and sanitized errors,
not listing descriptions or credentials.

## Provider behavior

Gemini permits at most 20 rows per run and 40 per local calendar day. Ollama may be used only
as a fallback after explicit evaluation approval and only for quota, connection, timeout, or
HTTP 502/503/504 failures. Authentication, authorization, configuration, model, schema,
evidence, and contradiction failures never fall back. Any provider failure sends the record
to manual review.
