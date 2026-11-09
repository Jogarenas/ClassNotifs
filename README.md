# ClassNotifs

Phone pushes for **every section of Davidson ECO 203 and ECO 204, Spring 2027
(term 202602)**. Python 3.12+, standard library only. New sections are discovered
automatically from the [official live API](https://api.davidson.edu/api/public/v2/courses?departments=ECO&term_code=202602&limit=200).
Open the [course schedule](https://course-schedule.davidson.edu/#/schedule?departments=ECO&term_code=202602)
to register; an alert does not reserve a seat or establish registration eligibility.

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

## Behavior and persistence

The workflow requests checks at UTC minutes **02, 07, 12, …, 57** every hour.
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
- **404/missing state:** inspect that branch and restore its latest good state;
  do not replace it with empty history. Missing API courses may indicate an upstream
  outage, term change, or removed section; investigate before changing guards.
- **No recent scheduled runs:** check that Actions and this workflow are enabled;
  trigger manually and inspect the latest run. Enable GitHub Actions failure
  notifications in your GitHub notification settings.
- **Leaked topic:** generate a replacement, update `NTFY_TOPIC`, subscribe the
  phone to the replacement, and repeat the test. Delete the local topic file
  after saving it securely. Never store the topic in public repository content.
