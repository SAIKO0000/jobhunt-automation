# One-time local setup

1. Install Python 3.12 and the project extras: `.[dev,sheets,gemini]`.
2. Create a user-owned Google Cloud project with no billing account attached.
3. Enable only the Google Sheets API.
4. Configure the OAuth consent screen and create a Desktop OAuth client.
5. Create a blank Google workbook manually in the intended account.
6. Run `jobhunt auth-google --client-secrets <path>` interactively. The file is read only for
   this operation; refresh credentials are stored in Windows Credential Manager.
7. Bootstrap a blank workbook, or preview and explicitly apply `migrate-workbook` for an
   existing v2 workbook. Then run fixture-only write/restore tests and validate it.
8. Recheck Himalayas and Jobicy applicant-cost and API policies before enabling the task;
   Remote OK and WWR must remain disabled unless a later review proves a free route that
   does not bypass a platform gate.
9. Optionally create a Gemini key only after visibly confirming the project is on the Free
   tier and acknowledging its data treatment in `config/analysis.json`.
10. Optionally install and evaluate Ollama later. The application never installs or pulls it.
11. Preview task registration with `scripts/register-jobhunt-task.ps1 ... -WhatIf`; register
   only after the chosen gates are complete and a manual **Run now** test succeeds.

No billing account is required by this design. Do not attach one merely to follow these
instructions. Set the GitHub Actions spending limit to $0 before relying on CI for a private
repository.

Review any student subscription offer on October 1 and October 8 before its stated October
11 expiration; it is unrelated to Gemini API eligibility.
