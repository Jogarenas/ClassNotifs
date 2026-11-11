import copy
import gzip
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

import monitor as m


def snapshot():
    return [{
        "subject": {"code": "ECO"}, "term": {"code": 202602},
        "course_number": "203" if crn in {"20161", "20162", "20592"} else "204",
        "crn": int(crn), "section": "A", "course_title": "Course title",
        "enrollment": {"remaining": 0}, "instructors_string": "J Jha",
        "meetings": [
            {"weekdays": "MW", "start_time": "1430", "end_time": "1545"},
            {"weekdays": "T", "start_time": "1340", "end_time": "1455"},
        ],
    } for crn in ("20161", "20162", "20163", "20164", "20592")]


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.payload = snapshot()
        self.state = {"version": 1, "sections": {}}
        self.messages = []
        self.saved = []

    def send(self, title, body):
        self.messages.append((title, body))
        return {"id": f"receipt-{len(self.messages)}", "at": "test"}

    def save(self, state):
        self.state = copy.deepcopy(state)
        self.saved.append(copy.deepcopy(state))

    def run_check(self, send=None):
        return m.check(self.payload, self.state, send or self.send, self.save)

    def test_initial_closed_then_open(self):
        self.run_check()
        self.assertEqual(self.messages, [])
        self.payload[0]["enrollment"]["remaining"] = 2
        self.run_check()
        self.assertEqual(len(self.messages), 1)
        crn = str(self.payload[0]["crn"])
        self.assertEqual(self.state["sections"][crn]["delivery"]["id"], "receipt-1")

    def test_initial_open_and_repeated_availability(self):
        self.payload[0]["enrollment"]["remaining"] = 1
        self.run_check()
        writes = len(self.saved)
        self.payload[0]["enrollment"]["remaining"] = 3
        self.run_check()
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(len(self.saved), writes)

    def test_open_closed_open_alerts_again(self):
        for remaining in (1, 0, 2):
            self.payload[0]["enrollment"]["remaining"] = remaining
            self.run_check()
        self.assertEqual(len(self.messages), 2)

    def test_negative_seats_are_closed(self):
        self.payload[0]["enrollment"]["remaining"] = -2
        self.run_check()
        self.assertFalse(self.state["sections"][str(self.payload[0]["crn"])]["open"])

    def test_new_section_discovered_and_alerted(self):
        self.run_check()
        new = copy.deepcopy(self.payload[0])
        new.update(crn=29999, section="D")
        new["enrollment"]["remaining"] = 4
        self.payload.append(new)
        self.run_check()
        self.run_check()
        self.assertEqual(len(self.messages), 1)
        self.assertTrue(self.state["sections"]["29999"]["alerted"])

    def test_missing_known_or_discovered_sections_preserves_entire_state(self):
        self.test_new_section_discovered_and_alerted()
        for crn in (20161, 29999):
            original = copy.deepcopy(self.state)
            payload = [r for r in self.payload if r["crn"] != crn]
            with self.assertRaises(m.MonitorError):
                m.check(payload, self.state, self.send, self.save)
            self.assertEqual(self.state, original)

    def test_malformed_snapshot_never_partially_alerts_or_closes(self):
        self.run_check()
        original = copy.deepcopy(self.state)
        for change in (None, "1", True, 1.5):
            payload = copy.deepcopy(self.payload)
            payload[0]["enrollment"]["remaining"] = 1
            payload[-1]["enrollment"]["remaining"] = change
            with self.assertRaises(m.MonitorError):
                m.check(payload, self.state, self.send, self.save)
            self.assertEqual(self.state, original)
            self.assertEqual(self.messages, [])

    def test_missing_fields_empty_bad_and_truncated_payloads(self):
        bad = copy.deepcopy(self.payload)
        del bad[0]["enrollment"]
        for payload in (bad, [], {}, None, self.payload * 40):
            with self.subTest(payload_type=type(payload)), self.assertRaises(m.MonitorError):
                self.run_bad(payload)
        self.assertEqual(self.saved, [])

    def run_bad(self, payload):
        m.check(payload, self.state, self.send, self.save)

    def test_subject_and_term_filter(self):
        for field, value in (("subject", {"code": "MAT"}), ("term", {"code": 202601}), ("course_number", "205")):
            payload = copy.deepcopy(self.payload)
            payload[0][field] = value
            with self.assertRaises(m.MonitorError):
                self.run_bad(payload)
        extra = copy.deepcopy(self.payload[0])
        extra.update(subject={"code": "MAT"}, crn=99999)
        self.assertEqual(len(m.parse_courses(self.payload + [extra], self.state)), 5)

    def test_failed_delivery_preserves_old_status_and_retries(self):
        self.run_check()
        before = copy.deepcopy(self.state)
        self.payload[0]["enrollment"]["remaining"] = 1
        def fail(*args):
            raise m.MonitorError("HTTP failure")
        with self.assertRaises(m.MonitorError):
            self.run_check(fail)
        self.assertEqual(self.state, before)
        self.run_check()
        self.assertEqual(len(self.messages), 1)
        self.run_check()
        self.assertEqual(len(self.messages), 1)

    def test_unacknowledged_delivery_retries(self):
        self.payload[0]["enrollment"]["remaining"] = 1
        with self.assertRaises(m.MonitorError):
            self.run_check(lambda *args: {})
        self.assertNotIn(str(self.payload[0]["crn"]), self.state["sections"])
        self.run_check()
        self.assertEqual(len(self.messages), 1)

    def test_each_receipt_saved_before_next_send(self):
        for row in self.payload[:2]:
            row["enrollment"]["remaining"] = 1
        def send(title, body):
            if self.messages:
                self.assertEqual(self.state["sections"][str(self.payload[0]["crn"])]["delivery"]["id"], "receipt-1")
            return self.send(title, body)
        self.run_check(send)
        self.assertEqual(len(self.messages), 2)

    def test_state_write_failure_stops_further_notifications(self):
        for row in self.payload:
            row["enrollment"]["remaining"] = 1
        def fail(state):
            raise m.MonitorError("Conflict saving state")
        with self.assertRaises(m.MonitorError):
            m.check(self.payload, self.state, self.send, fail)
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.state["sections"], {})

    def test_message_contains_every_meeting_and_details(self):
        self.payload[0]["enrollment"]["remaining"] = 2
        self.run_check()
        title, body = self.messages[0]
        for expected in ("Course title", "Section A", str(self.payload[0]["crn"]), "Available seats: 2", "J Jha", "MW 14:30–15:45", "T 13:40–14:55", m.SCHEDULE_URL):
            self.assertIn(expected, body)
        self.assertIn("2 seat(s)", title)

    def test_corrupt_state_rejected(self):
        for state in ({}, {"version": 1, "sections": {"20161": {"open": True, "alerted": True}}}):
            with self.assertRaises(m.MonitorError):
                m.validate_state(state)


    def test_add_department_without_repeating_existing_alerts(self):
        self.payload[0]["enrollment"]["remaining"] = 1
        self.run_check()
        receipt = copy.deepcopy(self.state["sections"]["20161"])
        new = copy.deepcopy(self.payload[0])
        new.update(subject={"code": "MAT"}, course_number="230", crn=29999)
        self.payload.append(new)
        with patch.object(m, "WATCHED_COURSES", {"ECO": {"203", "204"}, "MAT": {"230"}}):
            self.run_check()
            self.run_check()
        self.assertEqual(len(self.messages), 2)
        self.assertTrue(self.messages[-1][0].startswith("MAT 230 A:"))
        self.assertEqual(self.state["sections"]["20161"], receipt)
        self.assertTrue(self.state["sections"]["29999"]["alerted"])

    def test_non_econ_only_and_exact_department_number_pairs(self):
        row = copy.deepcopy(self.payload[0])
        row.update(subject={"code": "MAT"}, course_number="230", crn=29999)
        other = copy.deepcopy(row)
        other.update(subject={"code": "ECO"}, crn=28888)
        with patch.object(m, "WATCHED_COURSES", {"MAT": {"230"}}):
            courses = m.parse_courses([row, other], self.state)
        self.assertEqual(set(courses), {"29999"})
        self.assertEqual(courses["29999"]["subject"], "MAT")

    def test_missing_added_course_prevents_all_deliveries(self):
        self.payload[0]["enrollment"]["remaining"] = 1
        with patch.object(m, "WATCHED_COURSES", {"ECO": {"203", "204"}, "MAT": {"230"}}):
            with self.assertRaises(m.MonitorError):
                self.run_check()
        self.assertEqual(self.messages, [])
        self.assertEqual(self.saved, [])

    def test_invalid_configuration(self):
        for config in ({}, {"eco": {"203"}}, {"ECO": set()}, {"ECO": "203"}, {"ECO": {203}}):
            with self.subTest(config=config), patch.object(m, "WATCHED_COURSES", config):
                with self.assertRaises(m.MonitorError):
                    m.watched_courses()

    def test_fetch_each_department_once_and_keep_large_combined_snapshot(self):
        with patch.object(m, "WATCHED_COURSES", {"ECO": {"203", "204"}, "MAT": {"230"}}):
            with patch.object(m, "request_json", side_effect=[[{}] * 120, [{}] * 120]) as request:
                self.assertEqual(len(m.fetch_courses()), 240)
                self.assertEqual(request.call_count, 2)
                self.assertIn("departments=ECO", request.call_args_list[0].args[0])
                self.assertIn("departments=MAT", request.call_args_list[1].args[0])

    def test_empty_malformed_or_full_department_response_rejected(self):
        with patch.object(m, "WATCHED_COURSES", {"ECO": {"203"}, "MAT": {"230"}}):
            for invalid in ([], {}, [{}] * m.API_LIMIT):
                with self.subTest(response_type=type(invalid)):
                    with patch.object(m, "request_json", side_effect=[self.payload, invalid]):
                        with self.assertRaises(m.MonitorError):
                            m.fetch_courses()

    def test_cli_uses_configured_subject_in_test_notification_and_output(self):
        row = copy.deepcopy(self.payload[0])
        row.update(subject={"code": "MAT"}, course_number="230", crn=29999)
        with patch.object(m, "WATCHED_COURSES", {"MAT": {"230"}}), \
                patch("sys.argv", ["monitor.py", "--test-notification"]), \
                patch.object(m, "GitHubState") as store, \
                patch.object(m, "fetch_courses", return_value=[row]), \
                patch.object(m, "notify") as notify, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            store.return_value.load.return_value = self.state
            m.main()
            self.assertIn("Davidson MAT 230 monitor", notify.call_args.args[1])
            self.assertIn("MAT 230 A CRN 29999", output.getvalue())


class TransportTests(unittest.TestCase):
    def test_gzip_and_invalid_responses(self):
        for raw in (json.dumps(snapshot()).encode(), gzip.compress(json.dumps(snapshot()).encode())):
            response = io.BytesIO(raw)
            response.headers = {}
            with patch("urllib.request.urlopen", return_value=response):
                self.assertEqual(len(m.request_json(m.course_api_url("ECO"))), 5)
        for raw in (b"not json", b"\x1f\x8bcorrupt"):
            response = io.BytesIO(raw)
            response.headers = {}
            with patch("urllib.request.urlopen", return_value=response), self.assertRaises(m.MonitorError):
                m.request_json(m.course_api_url("ECO"))

    def test_http_failures_are_sanitized(self):
        error = urllib.error.HTTPError("https://ntfy.sh/SECRET", 503, "SECRET", {}, None)
        with patch("urllib.request.urlopen", side_effect=error), self.assertRaises(m.MonitorError) as caught:
            m.request_json("https://ntfy.sh")
        self.assertNotIn("SECRET", str(caught.exception))

    def test_ntfy_requires_receipt_and_does_not_return_topic(self):
        with patch.dict("os.environ", {"NTFY_TOPIC": "a" * 40}):
            with patch.object(m, "request_json", return_value={"event": "message", "id": "ok", "topic": "a" * 40}):
                self.assertEqual(m.notify("Title", "Body")["id"], "ok")
                self.assertNotIn("topic", m.notify("Title", "Body"))
            with patch.object(m, "request_json", return_value={}), self.assertRaises(m.MonitorError):
                m.notify("Title", "Body")

    def test_state_uses_branch_and_compare_and_swap_sha(self):
        state = {"version": 1, "sections": {}}
        content = m.base64.b64encode(json.dumps(state).encode()).decode()
        with patch.dict("os.environ", {"GITHUB_REPOSITORY": "Jogarenas/ClassNotifs", "GITHUB_TOKEN": "test"}):
            store = m.GitHubState()
            with patch.object(m, "request_json", return_value={"sha": "old", "content": content}) as request:
                self.assertEqual(store.load(), state)
                self.assertIn("ref=monitor-state", request.call_args.args[0])
            with patch.object(m, "request_json", return_value={"content": {"sha": "new"}}) as request:
                store.save(state)
                self.assertEqual(request.call_args.kwargs["data"]["sha"], "old")
                self.assertEqual(request.call_args.kwargs["data"]["branch"], "monitor-state")
                self.assertEqual(store.sha, "new")


if __name__ == "__main__":
    unittest.main()
