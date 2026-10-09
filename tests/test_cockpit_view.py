from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import unittest

from tasktra.cockpit_view import CSS, JAVASCRIPT, render


def _snapshot() -> dict[str, object]:
    return {
        "kind": "tasktra.operator-cockpit.snapshot",
        "version": 1,
        "read_only": True,
        "claimability_evaluated": False,
        "capture": {"captured_at": "2030-01-01T00:00:00Z", "schema_version": 12,
                    "audit_sequence": 4, "audit_head_sha256": "a" * 64,
                    "state_manifest_sha256": "b" * 64},
        "project": {"name": "Project", "root": "/project"},
        "source_provenance": {"package_kind": "source-checkout", "source_matches_project": True,
                              "foreign_source_checkout": False},
        "guidance_context": {"cwd": "/project", "env": {"PYTHONPATH": "/project/src"},
                             "argv_prefix": ["/python", "-m", "tasktra"], "shell": "posix"},
        "guidance_templates": {
            "goal-overview": {"label": "Overview", "read_only": True,
                              "argv_suffix": ["overview", "--root", "{root}", "--goal-id", "{goal_id}", "--limit", "{limit}", "--offset", "{offset}"]},
            "goal-status": {"label": "Status", "read_only": True,
                            "argv_suffix": ["status", "--root", "{root}", "--goal-id", "{goal_id}", "--detail-limit", "{limit}"]},
            "work-dependencies": {"label": "Dependencies", "read_only": True,
                                  "argv_suffix": ["work", "--root", "{root}", "dependencies", "{goal_id}", "--work-unit-id", "{work_unit_id}", "--limit", "{limit}", "--offset", "{offset}"]},
            "work-impact": {"label": "Impact", "read_only": True,
                            "argv_suffix": ["work", "--root", "{root}", "impact", "{goal_id}", "{work_unit_id}", "--direction", "both", "--limit", "{limit}", "--offset", "{offset}"]},
            "doctor": {"label": "Doctor", "read_only": True, "argv_suffix": ["doctor", "--root", "{root}"]},
        },
        "bounds": {"page_size": 2},
        "completeness": {"goals_captured": 1, "goals_total": 1, "normal_goals_omitted": 0},
        "runtime": {"emergency_stop": {"active": False}},
        "aggregates": {"goals": {"total": 1}, "work_units": {"total": 3}, "leases": {"live": 0}, "budgets": {"exhausted_goals": 0}, "provider_effects": {"failed": 2, "succeeded": 1}},
        "goals": [{
            "id": "goal-one", "title": "Goal <script>alert(1)</script>", "status": "active", "priority": 1,
            "progress": {"work": {"complete": 0, "total": 3, "by_status": {"planned": 2, "complete": 1}}, "acceptance": {"evidence": 1, "criteria": 3}},
            "budget": {"present": True, "tokens": {"total": 100, "consumed": 10, "reserved": 5, "remaining": 85}, "attempts": {"total": 4, "consumed": 1, "remaining": 3}, "elapsed_ms": {"total": 1000, "consumed": 100, "reserved_held": 20, "remaining": 880}, "concurrency": {"maximum": 2, "occupied": 1, "available": 1}, "exhausted": False},
            "dependencies": [{"id": "upstream-2", "status": "active"}], "checkpoints": [{"id": "review", "position": 1, "status": "reached", "reached_at": "2030-01-01T00:00:00Z"}], "provider_effects": {"failed": 2}, "intake": {"accepting_claims": True, "draining": False},
            "leases": {"stored_leased": 0, "live": 0, "expired": 0}, "attention": [{"code": "work-blocked", "count": 1, "detail": "Stored status"}],
            "completeness": {"work_units_total": 3, "work_units_captured": 3, "work_units_omitted": 0, "dependency_edges_total": 2, "dependency_edges_captured": 2, "dependency_edges_omitted": 0, "graph_complete": True},
            "work_units": [
                {"id": "base", "title": "Base", "status": "complete", "checkpoint_id": None, "attempt_count": 0, "last_outcome_class": None, "lease": None, "updated_at": "2030-01-01T00:00:00Z", "structural_ready": True, "prerequisite_ids": []},
                {"id": "anchor-10", "title": "Anchor", "status": "planned", "checkpoint_id": None, "attempt_count": 2, "last_outcome_class": "blocked", "lease": {"state": "expired", "expires_at": "2030-01-01T01:00:00Z"}, "updated_at": "2030-01-01T00:00:00Z", "structural_ready": False, "prerequisite_ids": ["base"]},
                {"id": "dependent", "title": "Dependent", "status": "planned", "checkpoint_id": None, "attempt_count": 0, "last_outcome_class": None, "lease": None, "updated_at": "2030-01-01T00:00:00Z", "structural_ready": False, "prerequisite_ids": ["anchor-10"]},
            ],
        }],
    }


class CockpitViewTests(unittest.TestCase):
    def test_render_is_deterministic_safe_and_hash_bound(self) -> None:
        snapshot = _snapshot()
        first = render(snapshot)
        self.assertEqual(first, render(snapshot))
        self.assertTrue(first.endswith(b"\n"))
        self.assertIn(b'"captured_at":"2030-01-01T00:00:00Z"', first)
        self.assertIn("Static capture; regenerate to refresh.", JAVASCRIPT)
        self.assertIn(b"Goal \\u003cscript\\u003ealert(1)\\u003c/script\\u003e", first)
        self.assertNotIn(b"Goal <script>alert(1)</script>", first)
        for source in (CSS, JAVASCRIPT):
            digest = base64.b64encode(hashlib.sha256(source.encode("utf-8")).digest())
            self.assertIn(b"sha256-" + digest, first)
        self.assertNotIn(b"unsafe-inline", first)
        self.assertNotIn(b"nonce", first)

    def test_render_rejects_wrong_view_identity(self) -> None:
        invalid = _snapshot()
        invalid["version"] = 2
        with self.assertRaisesRegex(ValueError, "unsupported"):
            render(invalid)
        invalid = _snapshot()
        invalid["claimability_evaluated"] = True
        with self.assertRaisesRegex(ValueError, "read-only"):
            render(invalid)

    def test_node_helpers_match_bounded_graph_and_guidance_contract(self) -> None:
        snapshot = _snapshot()
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable for cockpit JS verification")
        program = r"""
let source = '';
process.stdin
  .on('data', (x) => (source += x))
  .on('end', () => {
    global.window = {};
    eval(source);
    const s = JSON.parse(process.argv[1]);
    const h = window.TasktraCockpit;
    const v = { goal_id: 'goal-one', work_unit_id: 'anchor-10', limit: 2, offset: 0 };
    const ps = { ...s, guidance_context: { ...s.guidance_context, shell: 'powershell' } };
    const forged = { ...s, guidance_context: { ...s.guidance_context, env: { PATH: 'bad' } } };
    console.log(
      JSON.stringify({
        impact: h.dependencyImpact(s.goals[0], 'anchor-10', 'both', 2, 0),
        partial: h.dependencyImpact(
          { ...s.goals[0], completeness: { graph_complete: false } },
          'anchor-10',
        ),
        command: h.expandGuidance(s, 'work-impact', v),
        powershell: h.expandGuidance(ps, 'work-impact', v),
        bad: h.expandGuidance(s, 'unknown', {}),
        badEnv: h.expandGuidance(forged, 'doctor', {}),
        model: h.goalViewModel(s.goals[0]),
        unknownCriteria: h.goalViewModel({
          ...s.goals[0],
          progress: { acceptance: { evidence: 0, criteria: null } },
        }).acceptanceSummary,
        first: h.goalWorkPage(s.goals[0], '', '', 0, 2),
        second: h.goalWorkPage(s.goals[0], '', '', 1, 2),
        filtered: h.goalWorkPage(s.goals[0], 'anchor', 'planned', 0, 2),
        ordered: ['unit-2', 'unit-10', 'unit-1'].sort(h.compare),
      }),
    );
  });
        """
        result = subprocess.run([str(node), "-e", program, json.dumps(snapshot)], input=JAVASCRIPT,
                                text=True, encoding="utf-8", capture_output=True, check=True)
        output = json.loads(result.stdout)
        impact = output["impact"]
        self.assertTrue(impact["available"])
        self.assertEqual(impact["summary"]["all_prerequisites_total"], 1)
        self.assertEqual(impact["summary"]["all_dependents_total"], 1)
        self.assertEqual([(row["relation"], row["work_unit_id"]) for row in impact["relations"]], [("prerequisite", "base"), ("dependent", "dependent")])
        self.assertFalse(output["partial"]["available"])
        self.assertEqual(output["command"]["argv"][-5:], ["anchor-10", "--direction", "both", "--limit", "2", "--offset", "0"][-5:])
        self.assertTrue(output["command"]["command_text"].startswith("cd -- '/project' && PYTHONPATH='/project/src'"))
        self.assertTrue(output["powershell"]["command_text"].startswith("Set-Location -LiteralPath '/project'; $env:PYTHONPATH='/project/src'; & '/python'"))
        self.assertIsNone(output["bad"])
        self.assertIsNone(output["badEnv"])
        self.assertEqual(output["model"]["acceptance"], {"evidence": 1, "criteria": 3})
        self.assertEqual(output["model"]["acceptanceSummary"], "1 evidence / 3 criteria")
        self.assertEqual(output["unknownCriteria"], "0 evidence / Unavailable criteria")
        self.assertEqual(output["model"]["budget"]["tokens"]["remaining"], 85)
        self.assertEqual(output["model"]["dependencies"][0]["id"], "upstream-2")
        self.assertEqual(output["model"]["checkpoints"][0]["id"], "review")
        self.assertEqual(output["model"]["providerEffects"], {"failed": 2})
        self.assertEqual([unit["id"] for unit in output["first"]["rows"]], ["base", "anchor-10"])
        self.assertEqual([unit["id"] for unit in output["second"]["rows"]], ["dependent"])
        self.assertEqual([unit["id"] for unit in output["filtered"]["rows"]], ["anchor-10"])
        self.assertEqual(output["ordered"], ["unit-1", "unit-10", "unit-2"])

    def test_source_uses_safe_dynamic_rendering_and_bounded_attention(self) -> None:
        self.assertNotIn("innerHTML", JAVASCRIPT)
        self.assertNotIn("fetch(", JAVASCRIPT)
        self.assertIn("slice(0, pageSize())", JAVASCRIPT)
        self.assertIn("routeLabel = 'Portfolio view'", JAVASCRIPT)
        self.assertIn("replacement.focus()", JAVASCRIPT)
        self.assertIn("routeFocusPending", JAVASCRIPT)


if __name__ == "__main__":
    unittest.main()
