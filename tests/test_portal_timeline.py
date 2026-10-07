import unittest

from tasktra.portal_timeline import build_timeline


class TimelineTests(unittest.TestCase):
    def test_unknown_timing_clears_invalid_or_contradictory_marks(self):
        attempts = [{"id": "closed", "work_unit_id": "j", "goal_id": "g", "status": "finished",
                     "acquired_at": "2026-01-01T00:00:00Z", "ended_at": None, "elapsed_ms": 0},
                    {"id": "overflow", "work_unit_id": "j", "goal_id": "g", "status": "finished",
                     "acquired_at": "0001-01-01T00:00:00+23:59", "ended_at": None}]
        rows = build_timeline({"agents": [{"work_id": "a", "started_at": "2026-01-02T00:00:00Z",
                                           "last_observed_at": "2026-01-01T00:00:00Z"}]}, attempts)["rows"]
        for row in rows:
            self.assertEqual(row["timing"], "unknown")
            self.assertIsNone(row["started_at"])
            self.assertIsNone(row["ended_at"])
            self.assertIsNone(row["last_observed_at"])
            self.assertIsNone(row["duration_ms"])

    def test_attempt_duration_and_agent_window_are_distinct(self):
        snapshot = {"warnings": [], "agents": [
            {"id": "reported", "work_id": "run-a", "goal_id": "g", "job_id": "j", "state": "started", "role": "worker", "model": "m", "started_at": "2026-01-01T00:00:00Z", "last_observed_at": "2026-01-01T00:01:00Z"},
            {"id": "reported", "work_id": "run-b", "state": "started", "role": "worker", "model": None, "started_at": None, "last_observed_at": None},
        ]}
        attempts = [{"id": "a", "work_unit_id": "j", "goal_id": "g", "job_title": "Job", "status": "complete", "outcome_class": "success", "acquired_at": "2026-01-01T00:00:00Z", "ended_at": "2026-01-01T00:00:03Z", "elapsed_ms": 3000}]
        rows = {row["id"]: row for row in build_timeline(snapshot, attempts)["rows"]}
        self.assertEqual((rows["job_attempt:a"]["timing"], rows["job_attempt:a"]["duration_ms"]), ("recorded_duration", 3000))
        self.assertEqual((rows["agent:run-a"]["timing"], rows["agent:run-a"]["ended_at"]), ("observation_window", None))
        self.assertEqual(rows["agent:run-b"]["timing"], "unknown")


if __name__ == "__main__":
    unittest.main()
