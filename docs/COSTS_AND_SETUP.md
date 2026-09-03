# Cost and setup checklist

The enforced platform-charge target is $0. Nothing in repository setup attaches billing,
creates an account, activates a provider, or registers a scheduled task.

| Component | Platform charge | Setup or constraint |
|---|---:|---|
| Python and packages | $0 | Install into the project `.venv`. |
| Windows Task Scheduler | $0 | Registration is a separate manual action. |
| Himalayas / Jobicy public APIs | $0 | Approved for live collection; both currently state that applying is free. |
| Remote OK / WWR feeds | Not acceptable for this project | Disabled because public discovery can lead to paid applicant access. |
| Remotive public API | $0 discovery, paid catalogue also exists | Disabled to avoid mixing public and paywalled inventory. |
| LinkedIn / Indeed / JobStreet / PhilJobNet | $0 platform charge for ordinary job-seeker use | Manual intake only; no scraping or authenticated automation. |
| Upwork | Account may be $0; proposals can require paid Connects | Manual intake only; defaults to payment-required unless a listing can be pursued with no purchase. |
| Google Sheets API | $0 | Use a user-owned project with no billing account and enable only Sheets. |
| Desktop OAuth | $0 | Create a Desktop client and authorize exactly the Sheets scope. |
| Gemini API Free tier | $0 when eligible | Optional; visibly confirm Free status and data treatment before storing a key. No paid fallback exists. |
| Local Ollama model | $0 service charge | Optional later; about 2.5 GB plus local compute. The app never installs or downloads it. |
| Local snapshots/logs | $0 service charge | Consumes local disk; snapshots need ordinary user-managed cleanup/backups. |
| Paid language-model API | $0 | Removed from the active project. |
| Hosted runtime/storage | $0 | Removed from the active project. |
| Visual workflow/orchestration service | $0 | Not used; Task Scheduler triggers the typed Python pipeline directly. |
| Make/Zapier-style automation | $0 | Not used. |

Real indirect costs are the computer’s electricity, internet connection, disk space, and the
owner’s review time. Consumer AI subscriptions do not fund API use and are not runtime
credentials for this application.

Before first scheduled use:

1. Confirm the repository checks pass locally.
2. Create the no-billing Google project, enable Sheets, create Desktop OAuth, and manually
   create the workbook.
3. Authorize and complete fixture-only bootstrap plus restore into a second blank workbook.
4. Recheck and approve sources separately.
5. Leave AI disabled, or separately complete the Free-tier/privacy/verified-claim gates.
6. Preview task registration with `-WhatIf`, use **Run now** once, inspect the sanitized log,
   and only then allow the recurring triggers.

Review any student offer on October 1 and October 8 before its stated October 11 expiration.
That offer is separate from Gemini API Free-tier eligibility.
