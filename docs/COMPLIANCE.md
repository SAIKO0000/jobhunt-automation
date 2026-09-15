# Source and Outreach Compliance

Policy review baseline: September 3, 2026. Re-review a source before enabling it and at
least every 90 days afterward. A changed or inaccessible policy disables the adapter
until reviewed.

## Automated read-only adapters implemented

| Adapter | Allowed v1 operation | Required display behavior |
|---|---|---|
| Remote OK | Disabled public JSON adapter | Applicant access can be gated by Premium. Do not use its public feed to bypass that gate. |
| We Work Remotely | Disabled public RSS adapter | Free Basic access covers browsing and organization, while unrestricted applications require a paid plan. |
| Himalayas | Approved public JSON GET | Applying is explicitly free. Mention Himalayas, preserve its application/source link, and poll no more than daily because its public data refreshes every 24 hours. |
| Jobicy | Approved public JSON GET | Applying is explicitly free. Keep Jobicy as the original source, preserve its canonical job URL, and never poll more than hourly. |
| Remotive | Public JSON GET | Mention Remotive and link back to its listing; prefer a few requests daily and never exceed two requests per minute. |
| Greenhouse | Public Job Board GET | Use public posting data and link to the employer-hosted page; never POST an application. |
| Lever | Published-postings GET | Use hosted/apply URLs; never call application endpoints. |
| Ashby | Public job-board GET | Retain only records with `isListed=true`; never submit. |

The enabled Himalayas endpoints use only documented public search parameters for virtual
assistant, software-development, full-stack, and automation searches, with Philippine or
worldwide availability and entry-level seniority. These are targeted API-side queries, not
scraping. Jobicy uses documented APAC/worldwide geography and engineering-category filters.
All queries run once daily and retain the original source URL and attribution.

Availability checks are limited to previously collected Inbox records on the same approved
source host. They use bodyless `HEAD` requests and never follow a redirect outside the adapter
allowlist. Jobicy explicitly documents 404/410 checks for previously retrieved vacancies.
Himalayas source expiry dates are also honored. Missing records in targeted search results,
transient failures, and non-definitive HTTP statuses are not interpreted as closed listings.

Primary references:

- [Remote OK API](https://remoteok.com/api)
- [Remote OK applicant interface](https://remoteok.com/)
- [We Work Remotely RSS policy](https://weworkremotely.com/remote-job-rss-feed)
- [We Work Remotely job-seeker pricing](https://weworkremotely.com/frequently-asked-questions)
- [Himalayas Remote Jobs API](https://himalayas.app/api)
- [Himalayas job-seeker cost policy](https://himalayas.app/docs/what-is-himalayas)
- [Jobicy Remote Jobs API](https://jobicy.com/jobs-rss-feed)
- [Jobicy job-seeker site](https://jobicy.com/)
- [Remotive public jobs API](https://github.com/remotive-io/remote-jobs-api)
- [Greenhouse Job Board API](https://docs.greenhouse.io/job-board.html)
- [Lever Postings API](https://github.com/lever/postings-api)
- [Ashby public posting API](https://developers.ashbyhq.com/docs/public-job-posting-api)

Himalayas and Jobicy are enabled and owner-approved for zero-budget collection. Remote OK,
WWR, and Remotive remain disabled. Any future source activation requires a new applicant-cost
and automation-policy review.

## Manual-only sources

LinkedIn, JobStreet/SEEK, Indeed, Glassdoor, Kalibrr, OnlineJobs.ph, Upwork,
Freelancer, Wellfound, and generic company pages without an explicit public feed/API
remain manual-only. The system does not scrape their pages, parse their alert email
bodies, authenticate, monitor with a browser, or submit through them.

Manual entry should contain only the source URL, employer, title, location/work mode,
dates, a cost classification, and a concise user-selected excerpt. Do not copy full
restricted-platform job descriptions into the workbook. The `Manual Intake` tab is a
staging form, not an automated platform connector.

## Adapter approval checklist

Before changing a manifest to `enabled=true`:

1. Record owner approval outside the repository.
2. Confirm the official terms URL, reviewed date, endpoint hosts, and rate policy.
3. List only fields exposed by the authorized interface.
4. Choose the shortest workable retention TTL.
5. Decide separately whether downstream AI processing is authorized.
6. Add fixture-based contract tests; tests must not hit the live service.
7. Set `owner_approved=true`, then `enabled=true`.
8. Invoke a live run only with the explicit `--live` CLI flag.

If any step is uncertain, keep the adapter disabled.

## Leads and privacy

Cold leads are user-seeded; no registry enumeration or Google Maps harvesting is
implemented. Prefer business-level information. Named people and personal email
addresses require a documented lawful-basis and retention decision. `Opt-Out=true` or a
non-empty `Suppressed At` value prohibits future draft preparation for that lead.

The application contains no email or messaging connector. Philippine privacy guidance:
[Data Privacy Act](https://privacy.gov.ph/data-privacy-act/) and
[right to object](https://privacy.gov.ph/right-to-object/).
