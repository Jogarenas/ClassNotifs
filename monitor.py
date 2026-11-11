"""Watch Davidson course sections and send ntfy pushes when seats open.

Configuration lives below: set TERM and add department/course-number pairs to
WATCHED_COURSES. Every section of each listed course is discovered automatically.
For example, adding "MAT": {"230"} watches MAT 230 alongside the existing courses.
Course numbers are strings so suffixes such as "101L" can be represented.

Run `python monitor.py --dry-run` to validate a new course list against the live
API without sending pushes or writing state. Normal runs require GITHUB_TOKEN,
GITHUB_REPOSITORY, and NTFY_TOPIC in the environment; this script does not load
.env files. The workflow supplies these values through GitHub Actions.

The flow is fetch all departments → validate the entire snapshot → compare with
saved availability → send alerts → checkpoint receipts. No notification or state
change happens if the snapshot is incomplete. State lives on STATE_BRANCH and
must be preserved when adding courses to avoid repeating acknowledged alerts.
Only positive remaining enrollment counts as open. Continued availability sends
one alert; a confirmed closure followed by reopening permits another alert.

Python standard library only. GitHub Actions serializes concurrent invocations.
"""

import argparse
import base64
import copy
import gzip
import json
import os
import re
import sys
import urllib.error
import urllib.request
from urllib.parse import urlencode
from datetime import datetime, timezone

# Edit this block to choose courses. Numbers apply only to their own department.
TERM = "202602"
WATCHED_COURSES = {
    "ECO": {"203", "204", "211"},
    "HIS": {"163"}
    # "MAT": {"230"},  # Uncomment or add another department to watch it too.
}
# Optional completeness guards for sections already verified in this term.
# New courses do not need CRNs here: their sections are discovered from the API.
# When changing TERM, verify/update these guards and use state for the new term.
KNOWN_SECTIONS = {
    ("ECO", "203"): {"20161", "20162", "20592"},
    ("ECO", "204"): {"20163", "20164"},
    ("ECO", "211"): {"20167", "20168"},
    ("HIS", "163"): {"20260"}

}
API_LIMIT = 200
SCHEDULE_URL = f"https://course-schedule.davidson.edu/#/schedule?term_code={TERM}"
STATE_BRANCH = "monitor-state"


class MonitorError(Exception):
    pass


def request_json(url, *, data=None, headers=None, method=None):
    """Never include URLs, response bodies, or exception text in errors (secrets)."""
    request_headers = {"Accept-Encoding": "gzip", "User-Agent": "ClassNotifs/1.0"}
    request_headers.update(headers or {})
    if data is not None:
        data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            if response.headers.get("Content-Encoding", "").lower() == "gzip" or raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            return json.loads(raw)
    except urllib.error.HTTPError as error:
        raise MonitorError(f"HTTP request failed (status {error.code})") from None
    except Exception:
        raise MonitorError("Request failed or response was not valid JSON/gzip") from None


def text(value, name):
    """Require a nonempty display/identity field instead of guessing a value."""
    if not isinstance(value, str) or not value.strip():
        raise MonitorError(f"Missing or invalid {name}")
    return value.strip()


def watched_courses():
    """Validate configuration and return exact (department, number) identities."""
    if not isinstance(WATCHED_COURSES, dict) or not WATCHED_COURSES:
        raise MonitorError("WATCHED_COURSES must list at least one department and course")
    targets = set()
    for subject, numbers in WATCHED_COURSES.items():
        if not isinstance(subject, str) or not re.fullmatch(r"[A-Z]+", subject):
            raise MonitorError("Course departments must be uppercase subject codes")
        if not isinstance(numbers, (set, list, tuple)) or not numbers:
            raise MonitorError("Each department must list at least one course number")
        for number in numbers:
            if not isinstance(number, str) or not re.fullmatch(r"[0-9]+[A-Za-z]*", number):
                raise MonitorError("Course numbers must be strings such as '203' or '101L'")
            targets.add((subject, number))
    return targets


def course_api_url(subject):
    """Fetch departments separately so a larger watch list cannot truncate a union."""
    query = urlencode({"departments": subject, "term_code": TERM, "limit": API_LIMIT})
    return f"https://api.davidson.edu/api/public/v2/courses?{query}"


def fetch_courses():
    """Collect all configured departments before allowing any deliveries.

    A full API page may indicate more results than we received. Refuse it rather
    than silently missing sections; pagination would be needed for that department.
    """
    targets = watched_courses()
    payload = []
    for subject in sorted({subject for subject, _ in targets}):
        rows = request_json(course_api_url(subject))
        if not isinstance(rows, list) or not rows or len(rows) >= API_LIMIT:
            raise MonitorError(f"Empty, malformed, or possibly truncated {subject} response")
        payload.extend(rows)
    return payload


def parse_courses(payload, previous):
    """Validate target sections, including known CRNs, before changing anything.

    Every configured course must appear. Previously discovered sections must also
    remain present; a missing section is not evidence that its enrollment closed.
    Removing a course with saved history therefore requires deliberate state
    cleanup, not just deleting its configuration entry.
    """
    targets = watched_courses()
    if not isinstance(payload, list) or not payload:
        raise MonitorError("Empty, malformed, or possibly truncated course response")
    courses = {}
    for row in payload:
        if not isinstance(row, dict):
            raise MonitorError("Malformed course record")
        try:
            subject = text(row["subject"]["code"], "subject")
            number = str(row["course_number"])
            term = str(row["term"]["code"])
        except (KeyError, TypeError):
            raise MonitorError("Missing course identity") from None
        if term != TERM or (subject, number) not in targets:
            continue
        try:
            crn = str(row["crn"])
            if not re.fullmatch(r"[0-9]+", crn) or crn in courses:
                raise MonitorError("Invalid or duplicate CRN")
            remaining = row["enrollment"]["remaining"]
            # Davidson sometimes sends counts as JSON strings for individual
            # sections. Normalize only whole-number strings; do not round floats
            # or treat missing values/booleans as enrollment counts.
            if isinstance(remaining, str) and re.fullmatch(r"-?[0-9]+", remaining):
                try:
                    remaining = int(remaining)
                except ValueError:
                    raise MonitorError("Remaining enrollment is outside the supported integer format") from None
            if type(remaining) is not int:
                raise MonitorError(f"Missing or invalid remaining enrollment for {subject} {number} CRN {crn}")
            meetings = row["meetings"]
            if not isinstance(meetings, list):
                raise MonitorError("Missing meetings")
            times = []
            for meeting in meetings:
                days = text(meeting["weekdays"], "meeting days")
                start = text(meeting["start_time"], "start time")
                end = text(meeting["end_time"], "end time")
                if not all(re.fullmatch(r"(?:[01][0-9]|2[0-3])[0-5][0-9]", t) for t in (start, end)):
                    raise MonitorError("Invalid meeting time")
                times.append(f"{days} {start[:2]}:{start[2:]}–{end[:2]}:{end[2:]}")
            courses[crn] = {
                "subject": subject, "number": number, "section": text(row["section"], "section"),
                "title": text(row["course_title"], "course title"),
                "remaining": remaining,
                "instructor": text(row["instructors_string"], "instructor"),
                "meetings": times,
            }
        except (KeyError, TypeError):
            raise MonitorError("Missing or malformed target course data") from None
    baseline = {crn for identity in targets for crn in KNOWN_SECTIONS.get(identity, set())}
    missing = (baseline | set(previous["sections"])) - set(courses)
    if missing or {(c["subject"], c["number"]) for c in courses.values()} != targets:
        raise MonitorError("Missing expected courses/sections; prior state preserved")
    return courses


def validate_state(state):
    """Reject corrupt history rather than resetting it and repeating old alerts."""
    if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("sections"), dict):
        raise MonitorError("Invalid persisted state; refusing to reset it")
    for crn, section in state["sections"].items():
        if not re.fullmatch(r"[0-9]+", crn) or not isinstance(section, dict):
            raise MonitorError("Invalid section state")
        if type(section.get("open")) is not bool or type(section.get("alerted")) is not bool:
            raise MonitorError("Invalid section status")
        if section["alerted"] and (not section["open"] or not isinstance(section.get("delivery"), dict) or not section["delivery"].get("id")):
            raise MonitorError("Alerted state lacks a delivery receipt")
    return state


class GitHubState:
    """Durable state on a dedicated branch; Contents API SHA guards each write."""

    def __init__(self):
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        token = os.environ.get("GITHUB_TOKEN", "")
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or not token:
            raise MonitorError("GITHUB_REPOSITORY and GITHUB_TOKEN are required")
        self.url = f"https://api.github.com/repos/{repo}/contents/state.json"
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        self.sha = None

    def load(self):
        result = request_json(self.url + f"?ref={STATE_BRANCH}", headers=self.headers)
        try:
            self.sha = result["sha"]
            return validate_state(json.loads(base64.b64decode(result["content"])))
        except (KeyError, TypeError, ValueError):
            raise MonitorError("Cannot decode persisted state; refusing to reset it") from None

    def save(self, state):
        content = base64.b64encode((json.dumps(state, indent=2, sort_keys=True) + "\n").encode()).decode()
        result = request_json(self.url, method="PUT", headers=self.headers, data={
            "message": "Update course monitor state", "branch": STATE_BRANCH,
            "sha": self.sha, "content": content,
        })
        try:
            self.sha = result["content"]["sha"]
        except (KeyError, TypeError):
            raise MonitorError("State write was not acknowledged") from None


def notify(title, message):
    """Publish a push and return its receipt; server acceptance is not phone delivery."""
    topic = os.environ.get("NTFY_TOPIC", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,200}", topic):
        raise MonitorError("NTFY_TOPIC must be configured as a long unguessable topic")
    # JSON publish keeps the secret out of the URL and error messages.
    receipt = request_json("https://ntfy.sh", data={
        "topic": topic, "title": title, "message": message,
        "click": SCHEDULE_URL, "priority": 4,
    })
    if not isinstance(receipt, dict) or receipt.get("event") != "message" or not receipt.get("id"):
        raise MonitorError("ntfy did not acknowledge delivery")
    return {"id": receipt["id"], "at": datetime.now(timezone.utc).isoformat()}


def opening_message(crn, course):
    """Format one section's identity, enrollment, and all scheduled meetings."""
    title = f"{course['subject']} {course['number']} {course['section']}: {course['remaining']} seat(s) open"
    message = "\n".join([
        course["title"], f"Section {course['section']} | CRN {crn}",
        f"Available seats: {course['remaining']}", f"Instructor: {course['instructor']}",
        "Meetings (America/New_York):",
        *(course["meetings"] or ["Not scheduled / TBA"]), SCHEDULE_URL,
    ])
    return title, message


def check(payload, state, send, save):
    """Apply a validated snapshot, preserving each successful delivery immediately.

    `send(title, body)` returns an ntfy-style receipt; `save(state)` persists it.
    Injected callbacks let tests exercise transitions without network requests.
    Failed sends retain old state for a later retry. A save failure stops the run
    because continuing could deliver more alerts without durable receipts.
    """
    courses = parse_courses(payload, state)
    state = copy.deepcopy(state)
    failures = 0
    for crn, course in sorted(courses.items()):
        old = state["sections"].get(crn)
        is_open = course["remaining"] > 0
        if is_open and old and old["open"] and old["alerted"]:
            continue
        new = {"open": is_open, "alerted": False}
        if is_open:
            try:
                receipt = send(*opening_message(crn, course))
                if not isinstance(receipt, dict) or not receipt.get("id"):
                    raise MonitorError("No successful delivery receipt")
            except MonitorError:
                # This section's old state remains intact; retry on the next run.
                print(f"Notification failed for CRN {crn}; will retry", file=sys.stderr)
                failures += 1
                continue
            new.update(alerted=True, delivery=receipt)
        if new != old:
            state["sections"][crn] = new
            # Checkpoint each delivery immediately, before sending another alert.
            save(state)
    if failures:
        raise MonitorError(f"{failures} notification delivery failure(s)")
    return courses


def main():
    """Run a read-only live check or the normal persisted notification cycle."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Live validation only; no alerts or state changes")
    parser.add_argument("--test-notification", action="store_true", help="Also send a test before checking courses")
    args = parser.parse_args()
    if args.dry_run:
        courses = parse_courses(fetch_courses(), {"sections": {}})
    else:
        store = GitHubState()
        state = store.load()  # Missing or unreadable state fails closed.
        payload = fetch_courses()
        parse_courses(payload, state)
        if args.test_notification:
            labels = ", ".join(f"{subject} {number}" for subject, number in sorted(watched_courses()))
            notify("ClassNotifs test", f"Test notification: Davidson {labels} monitor (term {TERM}).\n" + SCHEDULE_URL)
            print("Test notification accepted by ntfy")
        courses = check(payload, state, notify, store.save)
    for crn, course in sorted(courses.items()):
        print(f"{course['subject']} {course['number']} {course['section']} CRN {crn}: {course['remaining']} remaining")
    print("Live validation succeeded; no alerts or state changes" if args.dry_run else "Course check succeeded")


if __name__ == "__main__":
    try:
        main()
    except MonitorError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
