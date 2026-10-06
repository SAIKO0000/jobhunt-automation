from __future__ import annotations

from dataclasses import dataclass

SCHEMA_VERSION = "3.2.0"

# Retained verbatim so a populated v3.0 workbook can be migrated by header name
# instead of treating the reordered v3.1 queue as an unknown schema.
OPPORTUNITY_COLUMNS_V3_0 = [
    "Pipeline Stage",
    "Priority",
    "Company",
    "Role",
    "Track",
    "Fit Band",
    "Final Score",
    "Eligibility",
    "Work Arrangement",
    "Location",
    "Salary",
    "Deadline",
    "Apply URL",
    "Next Action",
    "Next Action At",
    "Source",
    "Date Found",
    "Notes",
    "Blockers",
    "Missing Requirements",
    "Match Rationale",
    "Description Snippet",
    "Requirements",
    "Employment Type",
    "Engagement Type",
    "Country",
    "Published At",
    "Source URL",
    "Attribution",
    "Draft Review",
    "AI Draft",
    "Materials Used",
    "Submitted At",
    "Response At",
    "Interview/Call At",
    "Contact Name",
    "Contact Link",
    "Closed Reason",
    "Closed At",
    "Draft Fact IDs",
    "Draft Verification",
    "Drafted At",
    "Draft Review Observed At",
    "Record ID",
    "Canonical Key",
    "Duplicate Group ID",
    "Source Record ID",
    "Last Seen At",
    "Retrieved At",
    "Source Deadline",
    "Currency",
    "Salary Min",
    "Salary Max",
    "Retention Class",
    "Location Band",
    "Processing Status",
    "Rule Score",
    "Required Skill Match Score",
    "Matched Evidence IDs",
    "AI Confidence",
    "AI Provider",
    "AI Model",
    "AI Fallback Reason",
    "Prompt Version",
    "Analyzed At",
    "AI Error",
    "User Fields Hash",
    "Updated At",
]

DAILY_OPPORTUNITY_COLUMNS_V3_1 = [
    "Pipeline Stage",
    "Priority",
    "Company",
    "Role",
    "Track",
    "Fit Band",
    "Eligibility",
    "Work Arrangement",
    "Location",
    "Salary",
    "Deadline",
    "Apply URL",
    "Next Action",
    "Next Action At",
    "Notes",
]

REVIEW_DETAIL_COLUMNS_V3_1 = [
    "Final Score",
    "Blockers",
    "Missing Requirements",
    "Match Rationale",
    "Description Snippet",
    "Requirements",
    "Employment Type",
    "Engagement Type",
    "Country",
    "Source",
    "Date Found",
    "Published At",
    "Source URL",
    "Attribution",
]

APPLICATION_DETAIL_COLUMNS = [
    "Draft Review",
    "AI Draft",
    "Materials Used",
    "Submitted At",
    "Response At",
    "Interview/Call At",
    "Contact Name",
    "Contact Link",
    "Closed Reason",
    "Closed At",
    "Draft Fact IDs",
    "Draft Verification",
    "Drafted At",
    "Draft Review Observed At",
]

SYSTEM_DETAIL_COLUMNS_V3_1 = [
    "Record ID",
    "Canonical Key",
    "Duplicate Group ID",
    "Source Record ID",
    "Last Seen At",
    "Retrieved At",
    "Source Deadline",
    "Currency",
    "Salary Min",
    "Salary Max",
    "Retention Class",
    "Location Band",
    "Processing Status",
    "Rule Score",
    "Required Skill Match Score",
    "Matched Evidence IDs",
    "AI Confidence",
    "AI Provider",
    "AI Model",
    "AI Fallback Reason",
    "Prompt Version",
    "Analyzed At",
    "AI Error",
    "User Fields Hash",
    "Updated At",
]

OPPORTUNITY_COLUMNS_V3_1 = (
    DAILY_OPPORTUNITY_COLUMNS_V3_1
    + REVIEW_DETAIL_COLUMNS_V3_1
    + APPLICATION_DETAIL_COLUMNS
    + SYSTEM_DETAIL_COLUMNS_V3_1
)

# v3.2 keeps the daily queue compact while making the three independent
# decisions visible: candidate qualification, location, and applicant cost.
DAILY_OPPORTUNITY_COLUMNS = [
    *DAILY_OPPORTUNITY_COLUMNS_V3_1[:6],
    "Qualification",
    *DAILY_OPPORTUNITY_COLUMNS_V3_1[6:8],
    "Application Cost",
    *DAILY_OPPORTUNITY_COLUMNS_V3_1[8:],
]

REVIEW_DETAIL_COLUMNS = [
    REVIEW_DETAIL_COLUMNS_V3_1[0],
    "Seniority",
    "Required Experience Years",
    "Location Eligibility",
    "Compensation",
    "Qualification Reasons",
    *REVIEW_DETAIL_COLUMNS_V3_1[1:],
]

SYSTEM_DETAIL_COLUMNS = list(SYSTEM_DETAIL_COLUMNS_V3_1)

OPPORTUNITY_COLUMNS = (
    DAILY_OPPORTUNITY_COLUMNS
    + REVIEW_DETAIL_COLUMNS
    + APPLICATION_DETAIL_COLUMNS
    + SYSTEM_DETAIL_COLUMNS
)

# Excluded records keep the same lossless shape for deduplication, later rule
# review, and preservation of any existing human notes. The tab is hidden.
EXCLUDED_COLUMNS = list(OPPORTUNITY_COLUMNS)

MANUAL_INTAKE_COLUMNS = [
    "Platform",
    "Listing URL",
    "Company",
    "Role",
    "Location",
    "Work Arrangement",
    "Application Cost",
    "Description / Requirements",
    "Date Added",
    "Import Status",
    "Import Message",
    "Imported Record ID",
]

USER_OWNED_MANUAL_INTAKE_COLUMNS = {
    "Platform",
    "Listing URL",
    "Company",
    "Role",
    "Location",
    "Work Arrangement",
    "Application Cost",
    "Description / Requirements",
    "Date Added",
}

USER_OWNED_OPPORTUNITY_COLUMNS = {
    "Pipeline Stage",
    "Priority",
    "Deadline",
    "Next Action",
    "Next Action At",
    "Notes",
    "Draft Review",
    "Materials Used",
    "Submitted At",
    "Response At",
    "Interview/Call At",
    "Contact Name",
    "Contact Link",
    "Closed Reason",
    "Closed At",
}

# Kept explicitly for safe v2 discovery and migration. Unknown populated headers
# are rejected rather than discarded.
LEGACY_OPPORTUNITY_COLUMNS_V2 = [
    "Record ID",
    "Canonical Key",
    "Duplicate Group ID",
    "Source",
    "Source Record ID",
    "Source URL",
    "Apply URL",
    "First Seen At",
    "Last Seen At",
    "Retrieved At",
    "Published At",
    "Expires At",
    "Company",
    "Company Domain",
    "Title",
    "Opportunity Type",
    "Employment Type",
    "Engagement Type",
    "Work Arrangement",
    "Location Text",
    "Country",
    "Salary Raw",
    "Currency",
    "Salary Min",
    "Salary Max",
    "Description Snippet",
    "Requirements",
    "Attribution",
    "Retention Class",
    "Location Band",
    "Location Decision",
    "Eligibility Decision",
    "Hard-Fail Reasons",
    "Required Skills",
    "Preferred Skills",
    "Processing Status",
    "Rule Score",
    "AI Score",
    "Final Score",
    "Fit Band",
    "Matched Evidence IDs",
    "Missing Requirements",
    "AI Rationale",
    "AI Confidence",
    "AI Provider",
    "AI Model",
    "AI Fallback Reason",
    "Prompt Version",
    "Analyzed At",
    "AI Error",
    "Draft Type",
    "AI Draft",
    "Draft Fact IDs",
    "Draft Verification",
    "Drafted At",
    "Review Status",
    "Priority",
    "Reviewer Notes",
    "Approved By",
    "Approved At",
    "Approval Observed At",
    "Manual Action At",
    "Outcome",
    "Response At",
    "Interview At",
    "Follow-Up At",
    "User Fields Version",
    "User Fields Hash",
    "Updated At",
]

LEGACY_OPPORTUNITY_TABS = ("Technical Roles", "VA & Freelance")
LEGACY_ARCHIVE_TABS = {
    "Technical Roles": "Technical Roles (v2 archive)",
    "VA & Freelance": "VA & Freelance (v2 archive)",
}

COLD_LEAD_COLUMNS = [
    "Lead ID",
    "Business Name",
    "Official Domain",
    "Country",
    "Area",
    "Industry",
    "Seeded By",
    "Seeded At",
    "Verification Source URL",
    "Verified At",
    "Registry ID",
    "Contact Channel",
    "Public Contact",
    "Contact Person",
    "Role",
    "Contact Provenance",
    "Personal Data",
    "Observed Problem",
    "Evidence URL",
    "Service Hypothesis",
    "AI Relevance Score",
    "AI Rationale",
    "AI Confidence",
    "AI Model",
    "Prompt Version",
    "Analyzed At",
    "Lawful Basis Status",
    "LIA Reference",
    "Privacy Notice Needed",
    "Opt-Out",
    "Suppressed At",
    "Retention Review At",
    "Draft",
    "Draft Fact IDs",
    "Review Status",
    "Approved By",
    "Approved At",
    "Manual Contact At",
    "Response",
    "Follow-Up At",
    "Outcome",
    "Notes",
    "Updated At",
]

USER_OWNED_LEAD_COLUMNS = {
    "Business Name",
    "Official Domain",
    "Country",
    "Area",
    "Industry",
    "Seeded By",
    "Seeded At",
    "Verification Source URL",
    "Verified At",
    "Registry ID",
    "Contact Channel",
    "Public Contact",
    "Contact Person",
    "Role",
    "Contact Provenance",
    "Personal Data",
    "Observed Problem",
    "Evidence URL",
    "Service Hypothesis",
    "Lawful Basis Status",
    "LIA Reference",
    "Privacy Notice Needed",
    "Opt-Out",
    "Suppressed At",
    "Retention Review At",
    "Review Status",
    "Approved By",
    "Approved At",
    "Manual Contact At",
    "Response",
    "Follow-Up At",
    "Outcome",
    "Notes",
}

SOURCE_CONFIG_COLUMNS = [
    "Adapter ID",
    "Display Name",
    "Endpoint",
    "Enabled",
    "Owner Approved",
    "Allowed Hosts",
    "Allowed Fields",
    "Attribution",
    "Retention Class",
    "Retention TTL Days",
    "AI Processing Allowed",
    "Default Applicant Cost",
    "Terms URL",
    "Terms Verified At",
    "Rate Limit Per Minute",
    "Checkpoint",
]

RUN_LOG_COLUMNS = [
    "Run ID",
    "Trigger",
    "Started At",
    "Finished At",
    "Sources Attempted",
    "Sources Succeeded",
    "Records Fetched",
    "Records Deduplicated",
    "Records Actionable",
    "Records Needs Review",
    "Records Excluded",
    "Records Written",
    "Records Quarantined",
    "AI Input Tokens",
    "AI Output Tokens",
    "AI Provider Counts",
    "AI Fallback Count",
    "Retry Count",
    "Status",
    "Errors",
    "Checkpoints",
]

DEDUPE_COLUMNS = [
    "Canonical Key",
    "Record ID",
    "Fingerprint",
    "Duplicate Group ID",
    "Primary Tab",
    "Primary Row",
    "Source Links",
    "First Seen At",
    "Last Seen At",
]

SYSTEM_EVENT_COLUMNS = [
    "Event ID",
    "Run ID",
    "Observed At",
    "Severity",
    "Event Type",
    "Record ID",
    "Message",
]

LISTS_COLUMNS = ["List Name", "Value", "Sort Order", "Active"]


@dataclass(frozen=True)
class TabSchema:
    title: str
    columns: list[str]
    hidden: bool = False


TAB_SCHEMAS = {
    "Dashboard": TabSchema("Dashboard", []),
    "Opportunities": TabSchema("Opportunities", OPPORTUNITY_COLUMNS),
    "Manual Intake": TabSchema("Manual Intake", MANUAL_INTAKE_COLUMNS),
    "Excluded": TabSchema("Excluded", EXCLUDED_COLUMNS, hidden=True),
    "Cold Outreach Leads": TabSchema("Cold Outreach Leads", COLD_LEAD_COLUMNS, hidden=True),
    "Source Config": TabSchema("Source Config", SOURCE_CONFIG_COLUMNS, hidden=True),
    "Lists & Enums": TabSchema("Lists & Enums", LISTS_COLUMNS, hidden=True),
    "Run Log": TabSchema("Run Log", RUN_LOG_COLUMNS, hidden=True),
    "Dedupe Index": TabSchema("Dedupe Index", DEDUPE_COLUMNS, hidden=True),
    "System Events": TabSchema("System Events", SYSTEM_EVENT_COLUMNS, hidden=True),
}

PIPELINE_STAGES = [
    "Inbox",
    "Shortlisted",
    "Preparing",
    "Submitted",
    "Interview/Call",
    "Offer",
    "Won",
    "Closed",
]
PRIORITIES = ["High", "Medium", "Low"]
DRAFT_REVIEW_STATUSES = ["Not needed", "Needs review", "Approved", "Discarded"]
CLOSED_REASONS = [
    "Rejected",
    "Withdrawn",
    "No response",
    "Expired",
    "Duplicate",
    "Not pursued",
    "Other",
]
TRACKS = ["Technical", "VA/Freelance"]
LOCATION_DECISIONS = ["Eligible", "Needs review", "Ineligible"]
FIT_BANDS = ["Strong Fit", "Review", "Low Priority"]
QUALIFICATION_DECISIONS = ["Eligible", "Needs review", "Ineligible"]
APPLICATION_COST_DECISIONS = [
    "Free to apply",
    "Not stated",
    "Check cost",
    "Payment required",
]


def column_index(columns: list[str], name: str) -> int:
    return columns.index(name)


def column_letter(index: int) -> str:
    result = ""
    current = index + 1
    while current:
        current, remainder = divmod(current - 1, 26)
        result = chr(65 + remainder) + result
    return result


def dashboard_values(*, opportunities_sheet_id: int = 0) -> list[list[str]]:
    stage = column_letter(column_index(OPPORTUNITY_COLUMNS, "Pipeline Stage"))
    priority = column_letter(column_index(OPPORTUNITY_COLUMNS, "Priority"))
    company = column_letter(column_index(OPPORTUNITY_COLUMNS, "Company"))
    role = column_letter(column_index(OPPORTUNITY_COLUMNS, "Role"))
    fit = column_letter(column_index(OPPORTUNITY_COLUMNS, "Fit Band"))
    deadline = column_letter(column_index(OPPORTUNITY_COLUMNS, "Deadline"))
    next_action = column_letter(column_index(OPPORTUNITY_COLUMNS, "Next Action"))
    next_action_at = column_letter(column_index(OPPORTUNITY_COLUMNS, "Next Action At"))
    submitted_at = column_letter(column_index(OPPORTUNITY_COLUMNS, "Submitted At"))
    response_at = column_letter(column_index(OPPORTUNITY_COLUMNS, "Response At"))
    run_finished = column_letter(column_index(RUN_LOG_COLUMNS, "Finished At"))
    run_status = column_letter(column_index(RUN_LOG_COLUMNS, "Status"))

    active_count = "+".join(
        f"COUNTIF('Opportunities'!{stage}:{stage},\"{value}\")"
        for value in ("Submitted", "Interview/Call", "Offer")
    )
    due_count = (
        f"=SUMPRODUCT(('Opportunities'!{stage}2:{stage}<>\"Closed\")*"
        f"((('Opportunities'!{next_action_at}2:{next_action_at}<>\"\")*"
        f"('Opportunities'!{next_action_at}2:{next_action_at}<=TODAY())+"
        f"('Opportunities'!{deadline}2:{deadline}<>\"\")*"
        f"('Opportunities'!{deadline}2:{deadline}<=TODAY()))>0))"
    )
    action_queue = (
        "=IFERROR(ARRAY_CONSTRAIN(SORT(FILTER({"
        f"'Opportunities'!{stage}2:{stage},'Opportunities'!{company}2:{company},"
        f"'Opportunities'!{role}2:{role},'Opportunities'!{next_action}2:{next_action},"
        f"'Opportunities'!{next_action_at}2:{next_action_at},"
        f"'Opportunities'!{priority}2:{priority}}},"
        f"'Opportunities'!{next_action_at}2:{next_action_at}<>\"\","
        f"'Opportunities'!{next_action_at}2:{next_action_at}<=TODAY()+7,"
        f"'Opportunities'!{stage}2:{stage}<>\"Closed\"),5,TRUE),8,6),"
        '"No actions due")'
    )
    rows: list[list[str]] = [
        ["JOB HUNT CONTROL CENTER"],
        ["Local assistant only — no automatic applications or messages."],
        [],
        [
            "Last Success (Manila)",
            (
                f"=IFERROR(MAX(FILTER('Run Log'!{run_finished}:{run_finished},"
                f'\'Run Log\'!{run_status}:{run_status}="success")),"Never")'
            ),
        ],
        [
            "Run Health",
            (
                f"=IFERROR(IF(LOOKUP(2,1/('Run Log'!{run_status}2:{run_status}<>\"\"),"
                f'\'Run Log\'!{run_status}2:{run_status})<>"success","ERROR",'
                'IF(B4="Never","NOT STARTED",IF(NOW()-B4>30/24,"STALE","OK"))),'
                '"NOT STARTED")'
            ),
        ],
        [],
        ["Inbox", "Strong Fits", "Due / Overdue", "Active Pipeline"],
        [
            f"=COUNTIF('Opportunities'!{stage}:{stage},\"Inbox\")",
            (
                f"=COUNTIFS('Opportunities'!{fit}:{fit},\"Strong Fit\","
                f"'Opportunities'!{stage}:{stage},\"<>Closed\")"
            ),
            due_count,
            f"={active_count}",
        ],
        [],
        ["QUICK VIEWS", "", "", "", "LEGEND"],
        [
            f'=HYPERLINK("#gid={opportunities_sheet_id}&fvid=310001","Technical")',
            f'=HYPERLINK("#gid={opportunities_sheet_id}&fvid=310002","VA & Freelance")',
            f'=HYPERLINK("#gid={opportunities_sheet_id}&fvid=310003","Inbox")',
            f'=HYPERLINK("#gid={opportunities_sheet_id}&fvid=310006","Active Pipeline")',
            "Blue headers = editable",
        ],
        [
            f'=HYPERLINK("#gid={opportunities_sheet_id}&fvid=310004","Strong Fits")',
            f'=HYPERLINK("#gid={opportunities_sheet_id}&fvid=310005","Deadline Soon")',
            f'=HYPERLINK("#gid={opportunities_sheet_id}&fvid=310007","Follow-Up Due")',
            f'=HYPERLINK("#gid={opportunities_sheet_id}&fvid=310008","Closed Archive")',
            "Gray headers = system-managed",
        ],
        [],
        ["NEXT 7 DAYS"],
        ["Stage", "Company", "Role", "Next Action", "Due", "Priority"],
        [action_queue],
    ]
    # Keep chart source data below the visible dashboard so the action queue and
    # charts read as one compact vertical flow at normal laptop widths.
    while len(rows) < 59:
        rows.append([])
    rows.extend(
        [
            ["Pipeline Stage", "Count", "", "Week", "Submitted", "Responses"],
            ["Inbox", f"=COUNTIF('Opportunities'!{stage}:{stage},\"Inbox\")"],
            ["Shortlisted", f"=COUNTIF('Opportunities'!{stage}:{stage},\"Shortlisted\")"],
            ["Preparing", f"=COUNTIF('Opportunities'!{stage}:{stage},\"Preparing\")"],
            ["Submitted", f"=COUNTIF('Opportunities'!{stage}:{stage},\"Submitted\")"],
            [
                "Interview/Call",
                f"=COUNTIF('Opportunities'!{stage}:{stage},\"Interview/Call\")",
            ],
            ["Offer", f"=COUNTIF('Opportunities'!{stage}:{stage},\"Offer\")"],
            ["Won", f"=COUNTIF('Opportunities'!{stage}:{stage},\"Won\")"],
            ["Closed", f"=COUNTIF('Opportunities'!{stage}:{stage},\"Closed\")"],
        ]
    )
    for offset, row_index in zip(range(7, -1, -1), range(60, 68), strict=True):
        rows[row_index].extend(
            [
                "",
                f"=TODAY()-WEEKDAY(TODAY(),2)+1-{offset * 7}",
                (
                    f"=COUNTIFS('Opportunities'!{submitted_at}:{submitted_at},"
                    f"\">=\"&D{row_index + 1},'Opportunities'!{submitted_at}:{submitted_at},"
                    f'"<"&D{row_index + 1}+7)'
                ),
                (
                    f"=COUNTIFS('Opportunities'!{response_at}:{response_at},"
                    f"\">=\"&D{row_index + 1},'Opportunities'!{response_at}:{response_at},"
                    f'"<"&D{row_index + 1}+7)'
                ),
            ]
        )
    while len(rows) < 69:
        rows.append([])
    rows.append(["System schema", SCHEMA_VERSION])
    return rows


def initial_tab_values(*, opportunities_sheet_id: int = 0) -> dict[str, list[list[str]]]:
    tabs: dict[str, list[list[str]]] = {}
    for title, schema in TAB_SCHEMAS.items():
        tabs[title] = (
            dashboard_values(opportunities_sheet_id=opportunities_sheet_id)
            if title == "Dashboard"
            else [schema.columns]
        )
    list_groups = (
        ("Pipeline Stage", PIPELINE_STAGES),
        ("Priority", PRIORITIES),
        ("Draft Review", DRAFT_REVIEW_STATUSES),
        ("Closed Reason", CLOSED_REASONS),
        ("Track", TRACKS),
        ("Eligibility", LOCATION_DECISIONS),
        ("Qualification", QUALIFICATION_DECISIONS),
        ("Application Cost", APPLICATION_COST_DECISIONS),
        ("Fit Band", FIT_BANDS),
    )
    tabs["Lists & Enums"].extend(
        [
            [list_name, value, str(index), "TRUE"]
            for list_name, values in list_groups
            for index, value in enumerate(values)
        ]
    )
    return tabs
