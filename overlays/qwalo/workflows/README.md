# Why this directory is empty

`WORKFLOW_DIRS` in a Qwalo client release points here, not at the base repo's
`workflows/` tree.

That base tree ships eleven server-side workflows, among them `slack_backfill.py`
and `slack_sync.py`, which call `upsert_messages` and write Slack conversation history
into Postgres on a schedule. A Qwalo client release must not register them: the owner's
decision of 2026-10-21 is that Centaur stores no lead or seller data, and a backfill is
exactly a writer. Showing a reviewer their own client's seller text is a different act
from a job that copies every conversation into a database unattended, and the second
one is not part of the first. The others (`google_calendar_sync`, `linear_sync`,
`github_issue_triage`, `google_drive_sync`) would be registered against secrets this
vault does not hold, which produces failures that look like broken integrations rather
than missing grants.

An empty directory is the control. `deploy-client.sh` renders
`apiRs.extraEnv.WORKFLOW_DIRS` to this path, and the release registers zero workflows.

To give a client a workflow, add one file here following the pattern in the base
repo's `workflows/` directory, and keep it inside this client's `account_scope`.
