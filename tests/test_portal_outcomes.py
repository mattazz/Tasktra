"""Focused privacy and cohort regressions for the outcomes projection."""
from __future__ import annotations

from hashlib import sha256
import json
import sqlite3
import unittest

from tasktra.portal_outcomes import MAX_PORTAL_EVIDENCE_BYTES, MAX_PORTAL_WORKFLOW_BYTES, _safe_link, _safe_path, build_outcomes
from tasktra.workflow import new_workflow, serialize_workflow, workflow_completion_token


class PortalOutcomesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:"); self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript("""CREATE TABLE work_units (id TEXT,goal_id TEXT,title TEXT,status TEXT,verification_policy TEXT,acceptance_checks TEXT,updated_at TEXT);
        CREATE TABLE work_attempts (work_unit_id TEXT,attempt_no INTEGER,status TEXT,acquired_at TEXT,ended_at TEXT,elapsed_ms INTEGER);
        CREATE TABLE workflow_evidence (work_unit_id TEXT,workflow_json TEXT,workflow_sha256 TEXT,completion_token_json TEXT,completion_evidence_json TEXT,recorded_at TEXT);""")

    def add(self, job="job", source=None, policy="deterministic-direct", evidence='{}'):
        source = source or job; flow = new_workflow({"goal_id": "goal", "work_unit_id": source}, verification_policy=policy)
        text = serialize_workflow(flow); token = json.dumps(workflow_completion_token(flow))
        self.db.execute("INSERT INTO work_units VALUES(?,?,?,?,?,?,?)", (job,"goal",job,"complete",policy,"[]","2026-01-01T00:00:00Z"))
        self.db.execute("INSERT INTO workflow_evidence VALUES(?,?,?,?,?,?)", (job,text,sha256(text.encode()).hexdigest(),token,evidence,"2026-01-01T00:01:00Z"))

    def test_valid_workflow_and_exact_agent_job_cohort(self):
        self.add(evidence='{"checks":[{"name":"test","status":"passed","exit_code":0,"elapsed_ms":4}],"deliverables":[{"kind":"link","value":"https://github.com/o/r/commit/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}]}')
        usage={"total_tokens":10,"input_tokens":7,"cached_input_tokens":2,"output_tokens":3}
        out=build_outcomes(self.db,[{"work_id":"child","job_id":"job","state":"succeeded","provenance":"host-callback","model":"m","usage":usage},{"work_id":"other","job_id":None,"state":"succeeded","provenance":"host-callback","usage":usage}],goal_id="goal")
        self.assertTrue(out["jobs"][0]["verification"]["verified"])
        self.assertEqual((out["summary"]["measured_verified_jobs"],out["summary"]["measured_tokens_for_verified_jobs"],out["summary"]["unattributed_runs"]),(1,10,1))
        self.assertEqual(out["jobs"][0]["evidence"]["checks"][0],{"name":"test","status":"passed","exit_code":0,"elapsed_ms":4})

    def test_wrong_source_and_oversized_workflow_fail_closed(self):
        self.add(source="wrong")
        out=build_outcomes(self.db,[],goal_id="goal"); self.assertFalse(out["jobs"][0]["verification"]["verified"])
        self.db.execute("UPDATE workflow_evidence SET workflow_json=?, workflow_sha256=?", ("x"*(MAX_PORTAL_WORKFLOW_BYTES+1),"a"*64))
        out=build_outcomes(self.db,[],goal_id="goal"); self.assertFalse(out["jobs"][0]["verification"]["verified"])

    def test_duplicates_leases_unknown_and_unsafe_links_are_partial(self):
        self.add(evidence='{"deliverables":[{"kind":"path","value":"C:/secret"}]}')
        out=build_outcomes(self.db,[{"work_id":"lease","job_id":"job","state":"leased","provenance":"work-lease"},{"work_id":"same","job_id":"job","state":"succeeded","provenance":"host-callback"},{"work_id":"same","job_id":"job","state":"succeeded","provenance":"host-callback"}],goal_id="goal")
        self.assertEqual(out["jobs"][0]["usage"]["attributed_runs"],0)
        self.assertEqual(out["jobs"][0]["usage"]["total_tokens"],None)
        self.assertEqual(out["jobs"][0]["evidence"]["deliverables"],[])
        self.assertTrue(out["summary"]["partial"])

    def test_timing_mixed_timezone_is_untimed_and_partial(self):
        self.add()
        self.db.execute("INSERT INTO work_attempts VALUES(?,?,?,?,?,?)",("job",1,"finished","2026-01-01T00:00:00","2026-01-01T00:00:01Z",8))
        self.db.execute("INSERT INTO work_attempts VALUES(?,?,?,?,?,?)",("job",2,"finished","2026-01-01T01:00:00+14:00","2025-12-31T10:59:59Z",9))
        out=build_outcomes(self.db,[],goal_id="goal"); job=out["jobs"][0]
        self.assertEqual((job["attempt_count"],job["timed_attempts"],job["attempt_duration_ms"]),(2,0,None))
        self.assertTrue(job["attempts_partial"])

    def test_github_links_require_strict_ascii_paths(self):
        valid="https://github.com/owner/repo/actions/runs/1"
        self.assertEqual(_safe_link(valid),valid)
        for value in ("https://github.com/../repo/commit/"+"a"*40,"https://github.com/o/r/pull/01","https://github.com/o/r/actions/runs/%31","https://github.com/o/r/commit/"+"a"*40+"/","https://github.com/o/r/commit/"+"a"*40+"?x=1","https://github.com/o/r/commit/"+"a"*40+":443"):
            self.assertIsNone(_safe_link(value))

    def test_duplicate_work_id_is_excluded_even_when_job_claims_conflict(self):
        self.add("one"); self.add("two")
        usage={"total_tokens":10,"input_tokens":7,"cached_input_tokens":0,"output_tokens":3}
        records=[{"work_id":"shared","job_id":"one","state":"succeeded","provenance":"host-callback","usage":usage},{"work_id":"shared","job_id":"two","state":"succeeded","provenance":"host-callback","usage":usage}]
        out=build_outcomes(self.db,records,goal_id="goal")
        self.assertEqual([job["usage"]["measured_runs"] for job in out["jobs"]],[0,0])
        self.assertTrue(out["partial"])
        for path in ("C:/x","dir//x","dir/../x","dir/x ","dir\nx","dir\tx"):
            self.assertIsNone(_safe_path(path))

    def test_hash_token_and_policy_mismatches_never_verify(self):
        self.add(); self.db.execute("UPDATE workflow_evidence SET workflow_sha256=?",("0"*64,))
        self.assertFalse(build_outcomes(self.db,[],goal_id="goal")["jobs"][0]["verification"]["verified"])
        text=self.db.execute("SELECT workflow_json FROM workflow_evidence").fetchone()[0]
        self.db.execute("UPDATE workflow_evidence SET workflow_sha256=?",(sha256(text.encode()).hexdigest(),))
        self.db.execute("UPDATE work_units SET verification_policy='implementation-review'")
        self.assertFalse(build_outcomes(self.db,[],goal_id="goal")["jobs"][0]["verification"]["verified"])
        self.db.execute("UPDATE work_units SET verification_policy='deterministic-direct'")
        self.db.execute("UPDATE workflow_evidence SET completion_token_json='{}'")
        self.assertFalse(build_outcomes(self.db,[],goal_id="goal")["jobs"][0]["verification"]["verified"])

    def test_deep_evidence_and_capped_receipt_coverage_are_partial_not_errors(self):
        self.add(evidence='{"x":'+("["*1100)+("0"+"]"*1100)+'}')
        out=build_outcomes(self.db,[],goal_id="goal",records_partial=True)
        self.assertTrue(out["partial"]); self.assertTrue(out["summary"]["partial"])
        self.assertTrue(out["jobs"][0]["usage"]["partial"])

    def test_goal_scope_never_projects_other_goal_records(self):
        self.add(); usage={"total_tokens":10,"input_tokens":7,"cached_input_tokens":0,"output_tokens":3}
        out=build_outcomes(self.db,[{"work_id":"other","job_id":"other-job","state":"succeeded","provenance":"host-callback","usage":usage}],goal_id="goal")
        self.assertEqual(out["summary"]["measured_verified_jobs"],0)
        self.assertEqual(out["summary"]["measured_tokens_for_verified_jobs"],None)


    def test_optional_evidence_absence_and_invalid_presence_are_distinct(self):
        self.add()
        for payload in (None, '{}'):
            with self.subTest(valid=payload):
                self.db.execute("UPDATE workflow_evidence SET completion_evidence_json=?", (payload,))
                out = build_outcomes(self.db, [], goal_id="goal")
                self.assertTrue(out["jobs"][0]["verification"]["verified"])
                self.assertFalse(out["jobs"][0]["evidence"]["partial"])
        for payload in ('{bad', '[]', 'null', json.dumps({'unicode': 'é' * 9000}, ensure_ascii=False), '{"deep":' + '[' * 1100 + '0' + ']' * 1100 + '}', '{"large":"' + 'x' * MAX_PORTAL_EVIDENCE_BYTES + '"}'):
            with self.subTest(invalid=payload[:24]):
                self.db.execute("UPDATE workflow_evidence SET completion_evidence_json=?", (payload,))
                out = build_outcomes(self.db, [], goal_id="goal")
                self.assertFalse(out["jobs"][0]["verification"]["verified"])
                self.assertTrue(out["jobs"][0]["evidence"]["partial"])
                self.assertTrue(out["partial"])
                self.assertTrue(out["summary"]["partial"])

    def test_deterministic_review_requires_the_exact_proof_and_allows_extra_safe_evidence(self):
        from tests.test_stage3_autonomy import deterministic_review_workflow, deterministic_completion_evidence
        self.add()
        flow = deterministic_review_workflow("job")
        text = serialize_workflow(flow)
        self.db.execute("UPDATE work_units SET goal_id='goal-1',verification_policy='implementation-deterministic-review',acceptance_checks=?", (json.dumps([["python", "-m", "unittest"]]),))
        self.db.execute("UPDATE workflow_evidence SET workflow_json=?,workflow_sha256=?,completion_token_json=?", (text, sha256(text.encode()).hexdigest(), json.dumps(workflow_completion_token(flow))))
        proof = deterministic_completion_evidence()
        extra = {**proof, "checks": [{"name": "Acceptance suite", "status": "passed", "exit_code": 0, "elapsed_ms": 7}], "private": "PRIVATE_SENTINEL"}
        for evidence in (proof, extra):
            with self.subTest(valid_extra=evidence is extra):
                self.db.execute("UPDATE workflow_evidence SET completion_evidence_json=?", (json.dumps(evidence),))
                out = build_outcomes(self.db, [], goal_id="goal-1")
                self.assertTrue(out["jobs"][0]["verification"]["verified"])
                self.assertFalse(out["jobs"][0]["evidence"]["partial"])
                exposed = json.dumps(out)
                self.assertNotIn('"argv"', exposed)
                self.assertNotIn('PRIVATE_SENTINEL', exposed)
                self.assertNotIn('accepted_handoffs', exposed)
        invalid = [None, '{bad', '{}', json.dumps(deterministic_completion_evidence('c' * 64)), json.dumps(deterministic_completion_evidence(argv=['different'])), json.dumps(deterministic_completion_evidence(status='failed', exit_code=1)), '{"large":"' + 'x' * MAX_PORTAL_EVIDENCE_BYTES + '"}']
        for payload in invalid:
            with self.subTest(invalid=str(payload)[:24]):
                self.db.execute("UPDATE workflow_evidence SET completion_evidence_json=?", (payload,))
                out = build_outcomes(self.db, [], goal_id="goal-1")
                self.assertFalse(out["jobs"][0]["verification"]["verified"])
                self.assertTrue(out["jobs"][0]["evidence"]["partial"])
                self.assertTrue(out["summary"]["partial"])
