# ClassNotifs

Phone pushes when seats open in your configured Davidson courses, across any
department. Currently watches **every section of ECO 203 and ECO 204, Spring 2027
(term 202602)**. Python 3.12+, standard library only. New sections are discovered
automatically from Davidson's live API, with one request per watched department.
Open the [course schedule](https://course-schedule.davidson.edu/#/schedule?term_code=202602)
to register; an alert does not reserve a seat or establish registration eligibility.

## Choose courses

Edit `WATCHED_COURSES` near the top of `monitor.py`. Group course numbers by
their department code; use strings for numbers. For example:

```python
WATCHED_COURSES = {
    "ECO": {"203", "204"},
    "MAT": {"230"},
}
```

This example adds every section of MAT 230; it is not enabled by default. Use
the exact subject code and course number from Davidson's schedule. No changes
to notification logic, workflow, scheduler, or secrets are needed to add courses.
Run `python3 monitor.py --dry-run` to confirm all configured courses are found,
then deploy the change to `main` for the scheduled workflow to pick it up.

`KNOWN_SECTIONS` is an optional completeness guard, keyed by department and
number. Existing ECO entries protect the current installation; adding another
course does not require listing its CRNs. After a successful check, persisted
history guards newly discovered sections too. Keep the existing state when
adding courses so previously acknowledged openings do not alert again.

Removing a watched course with saved sections requires deliberate cleanup of
only those sections in `monitor-state:state.json`; otherwise the missing-section
guard stops the check. Do not reset the whole file. When changing `TERM`, update
the known-section guards and use a separate state branch for that term by setting
`STATE_BRANCH` and creating its initial `state.json` as described below. Verify
the dry run before enabling monitoring for that term.

The module docstring and function docstrings in `monitor.py` describe the fetch,
validation, alert, and persistence flow. The script does not automatically load
the local `.env`; normal scheduled runs receive credentials from Actions secrets.

## Setup

1. Keep this repository public for free standard GitHub-hosted Actions runs.
2. Generate a long random topic locally; never commit it or put it in an issue,
   workflow input, or log. For example, write one to a private temporary file:
   ```sh
   umask 077
   python3 -c 'import secrets; print("classnotifs-" + secrets.token_hex(24))' > /tmp/ClassNotifs-ntfy-topic.txt
   gh secret set NTFY_TOPIC -R Jogarenas/ClassNotifs < /tmp/ClassNotifs-ntfy-topic.txt
   ```
   If already configured during installation, use that same topic instead.
3. In the **ntfy phone app**, add a subscription with server **https://ntfy.sh**
   and the exact topic from that file. Enable notifications in the app and phone
   settings. An unguessable topic is **not an authenticated private channel**:
   anyone who learns it can read and publish messages. See [ntfy publishing](https://docs.ntfy.sh/publish/).
4. The `monitor-state` branch must contain `state.json`, initially
   `{"version":1,"sections":{}}`. The installer creates this branch before the
   first run. Never reset an existing state file: that can repeat alerts. If
   rebuilding elsewhere, create the branch from `main` and add this file before
   enabling the workflow. Keep it when changing code.
5. Under **Actions → Davidson course monitor → Run workflow**, select `main`,
   check **test_notification**, and run. Confirm a green run and receipt on your
   phone. The test also performs a normal course check and can send real openings.
   Only then consider monitoring active. The existing hourly ChatGPT backup is
   separate; this project does not disable it.

## External scheduler: every minute

Use [cron-job.org](https://cron-job.org/en/faq/) for the primary timer. Its free
service supports intervals down to one minute, custom headers, and POST bodies.
The workflow already accepts `workflow_dispatch`, so this setup needs no code
deployment. Leave the native GitHub schedule enabled as a backup.

1. Create a [fine-grained GitHub token](https://github.com/settings/personal-access-tokens/new)
   with resource owner `Jogarenas`, access to **only `ClassNotifs`**, and repository
   permission **Actions: Read and write**. Use a dedicated token and set a reminder
   to replace it before its expiration date.
2. Sign up or log in at the [cron-job.org console](https://console.cron-job.org/)
   and create a job named **ClassNotifs: every minute** with these settings:

   | Setting | Value |
   | --- | --- |
   | URL | `https://api.github.com/repos/Jogarenas/ClassNotifs/actions/workflows/monitor.yml/dispatches` |
   | Schedule | Every minute, all hours and days (`* * * * *`) |
   | Method | `POST` |
   | Request body | `{"ref":"main"}` |
   | `Authorization` header | `Bearer YOUR_DEDICATED_GITHUB_TOKEN` |
   | `Accept` header | `application/vnd.github+json` |
   | `Content-Type` header | `application/json` |
   | `X-GitHub-Api-Version` header | `2026-03-10` |

   Store the token only in the scheduler's authorization header. The scheduler
   needs that credential to request future runs; the ntfy topic remains solely
   in GitHub Actions secrets. Disable response-body storage and enable email
   notifications for failures, recovery, and automatic job disabling.
3. Enable the job. Verify its execution history shows successful requests at
   one-minute intervals and that corresponding **workflow_dispatch** runs in
   [GitHub Actions](https://github.com/Jogarenas/ClassNotifs/actions/workflows/monitor.yml)
   complete successfully. Dispatch acceptance alone does not mean the course
   check or push delivery succeeded; enable GitHub Actions failure notifications
   separately.

This requests **1,440 runs per day**. The scheduler is free; standard GitHub-hosted
Actions runners are free while this repository remains public. One minute is
the requested trigger interval, not a guarantee of check completion: both the
external service and GitHub runners can experience delays. To stop the external
timer, disable its job in cron-job.org; disabling the whole GitHub workflow also
blocks external dispatches. To rotate the token, update the scheduler header,
verify a successful request/run, then revoke the old token.

## Behavior and persistence

The native GitHub backup schedule requests checks at UTC minutes
**03, 08, 13, …, 58** every hour. Once configured, the external scheduler above
requests the primary checks every minute.
[GitHub scheduled runs](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
can be delayed or dropped during high load; this is not an exact five-minute
timer. Public-repository schedules can be disabled after 60 days of repository
inactivity; check Actions periodically and re-enable if necessary. State-only
updates are not a substitute for checking that the schedule remains enabled.

Davidson reportedly refreshes its data roughly every **15 minutes**, **subject
to verification**; this installation has not independently confirmed that cadence.
Polling more often cannot reveal an opening before Davidson publishes it.

An opening means exactly `enrollment.remaining > 0` (zero and negatives are
closed). Initial openings alert immediately. Continued availability does not
repeat, even if the seat count changes. A confirmed closure resets the alert,
so reopening sends another push. Each push includes title, section, CRN, seats,
instructor, all meeting days/times in America/New_York, and the schedule link.

Status and ntfy delivery receipts live in `monitor-state:state.json`, with no
topic or credentials. A shared Actions concurrency group serializes manual and
scheduled runs. SHA-checked GitHub writes reject conflicting changes. A section
is marked alerted only after ntfy acknowledges delivery, and each receipt is
persisted immediately before processing the next section. Failed deliveries
retain the section's old status and retry next run; other successful deliveries
remain saved. A failure to save state stops the run.

There is no atomic transaction spanning ntfy and GitHub: if a runner dies or a
state write fails immediately after ntfy accepts a push, the next run can repeat
that push. Serialization prevents concurrent workflow duplicates; exactly-once
delivery across that crash window is not guaranteed. ntfy acknowledgment verifies
server acceptance, not that the phone displayed the notification.

Request failures, invalid JSON/gzip, malformed target records, missing known
sections (including newly discovered ones), duplicate CRNs, and possibly truncated
responses fail the check before any course notifications/state changes. A removed
section therefore requires deliberate investigation rather than being treated as
closed. Missing/corrupt state fails instead of silently resetting alert history.
An open→closed→open transition entirely between checks cannot be detected.

## Verification

```sh
python3 -m unittest discover -s tests -v
python3 monitor.py --dry-run  # fresh API, no secret needed, no pushes/state writes
gh workflow run monitor.yml -R Jogarenas/ClassNotifs -f test_notification=true
gh run list -R Jogarenas/ClassNotifs --workflow monitor.yml --limit 5
```

Tests cover initial/recurring openings, closures and reopenings, new sections,
missing/malformed data, filtering, every meeting, gzip, failed deliveries, receipt
checkpointing, state write failures, and optimistic concurrency.

## Troubleshooting

- **No push:** confirm a green Actions run, `NTFY_TOPIC` under Settings → Secrets
  and variables → Actions, exact matching phone subscription, notification
  permission, and battery/background restrictions. Send a manual test. Do not
  paste the topic into public logs. ntfy errors fail the run and retry next check.
- **403 writing state:** allow Actions read/write contents permissions and ensure
  repository rules allow `GITHUB_TOKEN` updates to `monitor-state`.
- **Local HTTPS certificate error:** repair your Python installation's CA
  certificates or use a Python with a working system trust store. Keep HTTPS
  verification enabled; the GitHub-hosted runner has its own trust store.
- **404/missing state:** inspect that branch and restore its latest good state;
  do not replace it with empty history. Missing API courses may indicate an upstream
  outage, term change, or removed section; investigate before changing guards.
- **No recent scheduled runs:** check that Actions and this workflow are enabled;
  trigger manually and inspect the latest run. Enable GitHub Actions failure
  notifications in your GitHub notification settings.
- **Leaked topic:** generate a replacement, update `NTFY_TOPIC`, subscribe the
  phone to the replacement, and repeat the test. Delete the local topic file
  after saving it securely. Never store the topic in public repository content.
