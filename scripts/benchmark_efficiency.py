"""Opt-in live, paired coding experiment; retained evidence, no estimated tokens.

Run from a Tasktra source checkout with saved Codex CLI authentication:
  python scripts/benchmark_efficiency.py --execute --output .tasktra/runtime/efficiency-benchmark
Each arm has its own Git repository. No generated patch reaches the source
checkout. The external acceptance check is identical across arms. This small
experiment measures execution overhead, not general coding effectiveness.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from time import monotonic

from tasktra.authority import authority_envelope_sha256
from tasktra.autonomy import AutonomyStore
from tasktra.cli import _execution_profile
from tasktra.efficiency import compare_verified_trials
from tasktra.execution import ExecutionStore
from tasktra.host import CodexHostAdapter
from tasktra.supervisor import run_work
from tasktra.validation import run_validations
from tasktra.worker_profiles import WorkerContext


SOURCE = '''def unique_names(values):
    """Trim names, drop empty names, and deduplicate case-insensitively.

    Keep the first spelling and input order. Accept any iterable of strings.
    """
    return list(set(values))
'''
CHECK = '''from names import unique_names
assert unique_names([]) == []
assert unique_names([" Amy ", "amy", "", "BOB", " bob ", "  ", "Amy"]) == ["Amy", "BOB"]
assert unique_names(iter(["Élodie", "ÉLODIE", " Zoe "])) == ["Élodie", "Zoe"]
assert unique_names(["Straße", "STRASSE"]) == ["Straße"]
assert unique_names([" z ", "a", "Z", " b "]) == ["z", "a", "b"]
'''
TASK = ("Fix unique_names in names.py to trim whitespace, drop empty strings, deduplicate with str.casefold(), "
        "and preserve the first spelling and input order. Accept any iterable of strings. "
        "Only names.py may change; check.py is the acceptance check. Run it and inspect the result. "
        "Do not delegate this stage or use network services. Keep the stage response brief.")


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def fixture(root):
    root.mkdir(parents=True, exist_ok=False)
    (root / ".tasktra").mkdir()
    commands = [[sys.executable, "check.py"]]
    (root / ".tasktra/project.toml").write_text(
        "[project]\nname='efficiency-fixture'\nconfig_version=1\n[packs]\nenabled=['core']\n"
        "[validation]\ninclude_pack_defaults=false\ncommands=" + json.dumps(commands) + "\n", encoding="utf-8")
    (root / ".gitignore").write_text(".tasktra/runtime/\n__pycache__/\n", encoding="utf-8")
    (root / "names.py").write_text(SOURCE, encoding="utf-8")
    (root / "check.py").write_text(CHECK, encoding="utf-8")
    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=Tasktra benchmark", "-c", "user.email=benchmark@example.invalid", "commit", "-qm", "Controlled fixture"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return commands


def assess_scope(root, base_commit):
    """Check the complete final delta, including changes committed by an arm."""
    changed = subprocess.run(
        ["git", "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", base_commit, "--"],
        cwd=root, check=True, capture_output=True, timeout=30,
    ).stdout.decode("utf-8").split("\0")
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=root, check=True, capture_output=True, timeout=30,
    ).stdout.decode("utf-8").split("\0")
    changed, untracked = sorted(filter(None, changed)), sorted(filter(None, untracked))
    return {"base_commit": base_commit, "changed_paths": changed, "untracked_paths": untracked,
            "scope_valid": changed == ["names.py"] and not untracked}


class ObservedHost(CodexHostAdapter):
    def run(self, **kwargs):
        print(json.dumps({"event": "stage-start", "model": kwargs["model"], "sandbox": kwargs["sandbox"]}), flush=True)
        result = super().run(**kwargs)
        print(json.dumps({"event": "stage-finished", "tokens": result.usage.get("total_tokens") if result.usage else None}), flush=True)
        return result


def direct(root, profile, timeout):
    ledger = ExecutionStore(root)
    work_id = "direct-" + root.name
    ledger.plan(work_id, "implementer", *profile)
    def started(thread):
        ledger.start(work_id, provider="codex", host="local", thread_id=thread, provenance="host-callback")
    try:
        result = ObservedHost().run(prompt=TASK, workspace=root, model=profile[0], effort=profile[1],
            sandbox="workspace-write", timeout_seconds=timeout, on_started=started, on_tick=lambda: None)
        if result.usage:
            ledger.record_host_usage(work_id, thread_id=result.thread_id, usage=dict(result.usage))
        success = result.response["status"] == "completed" and not result.response["findings"]
        ledger.finish(work_id, "succeeded" if success else "failed", provenance="host-callback",
                      unknown_reason=None if result.usage else "host-usage-unavailable")
        return {"ok": success, "executions": [work_id], "response": result.response}
    except Exception as error:
        record = ledger.get(work_id)
        if record["state"] == "started":
            ledger.finish(work_id, "failed", unknown_reason="host-result-incomplete", provenance="host-callback")
        return {"ok": False, "executions": [work_id], "error": str(error)[:500]}


def supervised(root, mode, commands, profiles, timeout, worker_context=None):
    store = AutonomyStore(root / ".tasktra/runtime/tasktra.sqlite")
    goal, unit = "benchmark", "bench-" + root.name + "-unit"
    scope = {"paths": ["."], "exclusions": []}
    store.create_goal(goal_id=goal, title="Repair name normalization", description=TASK, acceptance=["Unmodified check.py passes."])
    contract = {"kind": "tasktra.authority-envelope", "version": 1, "goal_id": goal,
        "outcome": TASK, "motivation": "User-authorized local token-efficiency experiment.", "author_id": "coordinator",
        "acceptance_criteria": [{"id": "checks", "statement": "Unmodified check.py passes."}], "scope": scope,
        "allowed_actions": ["goal-activate", "work-claim", "work-complete", "verify-implementation-deterministic-review"],
        "allowed_effects": ["local-reversible-write"], "prohibited_actions": ["push", "deploy"],
        "quality_requirements": ["Independent review and the same deterministic acceptance check."],
        "budgets": {"tokens": None, "attempts": 1, "elapsed_seconds": timeout, "concurrency": 1},
        "dependencies": [], "checkpoints": [], "stop_conditions": ["Unresolved defect."], "escalation_conditions": ["Host unavailable."]}
    store.define_goal_contract(goal, contract, actor_id="coordinator")
    envelope = authority_envelope_sha256(contract)
    expiry = datetime.now(timezone.utc) + timedelta(hours=4)
    store.record_transition_approval(goal_id=goal, action="goal-activate", effect="local-reversible-write",
        envelope_sha256=envelope, approver_id="benchmark-operator", performer_id="coordinator", valid_until=expiry,
        authority_clause="Explicit --execute opts into this disposable local experiment.")
    store.activate_goal(goal, actor_id="coordinator", envelope_sha256=envelope)
    policy = "implementation-review" if mode == "current" else "implementation-deterministic-review"
    store.create_work_unit(goal_id=goal, work_unit_id=unit, title=TASK, scope=scope, verification_policy=policy,
                          acceptance_checks=commands if mode == "optimized" else None)
    for action in ("work-claim", "work-complete"):
        store.record_transition_approval(goal_id=goal, work_unit_id=unit, action=action, effect="local-reversible-write",
            envelope_sha256=envelope, approver_id="benchmark-steward", approver_kind="steward", performer_id="coordinator", valid_until=expiry)
    return run_work(root, goal_id=goal, work_unit_id=unit, performer_id="coordinator", envelope_sha256=envelope,
        profile_for_role=lambda role: profiles[role], token_reservation=1_000_000, timeout_seconds=timeout, apply=True,
        host=ObservedHost(worker_context=worker_context if mode == "optimized" else None),
        context_mode="legacy" if mode == "current" else "compact")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, choices=range(1, 11), default=1)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--worker-context", type=Path, help="Explicit bounded tool selection for the optimized arm only")
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({"mutation": "none", "arms": ["direct", "current", "optimized"], "repetitions": args.repetitions,
            "scenario": "name-normalization", "note": "Use --execute to spend model tokens in isolated fixtures."})); return
    original = Path.cwd()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    context = None
    if args.worker_context is not None:
        if args.worker_context.stat().st_size > 64 * 1024:
            raise ValueError("worker context exceeds 64 KiB")
        context = WorkerContext.from_mapping(json.loads(args.worker_context.read_text(encoding="utf-8")))
    profiles = {role: _execution_profile(original, role) for role in ("implementer", "tester", "reviewer")}
    config_fingerprint = digest({"profiles": profiles, "source": SOURCE, "task": TASK, "timeout": args.timeout})
    trials, receipts = [], {}
    for replicate in range(args.repetitions):
        modes = ["direct", "current", "optimized"]
        modes = modes[replicate % 3:] + modes[:replicate % 3]
        for mode in modes:
            print(json.dumps({"event": "arm-start", "mode": mode, "replicate": replicate}), flush=True)
            root = output / f"{replicate}-{mode}"
            commands = fixture(root)
            base_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
            start = monotonic()
            try:
                result = direct(root, profiles["implementer"], args.timeout) if mode == "direct" else supervised(root, mode, commands, profiles, args.timeout, context)
            except Exception as error:
                result = {"ok": False, "error": str(error)[:500], "executions": [row["work_id"] for row in ExecutionStore(root).iter_records() if row["role"] != "coordinator"]}
            validation = run_validations(root, commands, timeout_seconds=60)
            unchanged_check = (root / "check.py").read_text(encoding="utf-8") == CHECK
            scope = assess_scope(root, base_commit)
            passed = unchanged_check and scope["scope_valid"] and all(item.status == "passed" for item in validation)
            rows = list(ExecutionStore(root).iter_records())
            receipts.update({row["work_id"]: row for row in rows})
            trial = {"scenario": "name-normalization", "replicate": replicate, "mode": mode,
                "config_fingerprint": config_fingerprint, "acceptance_fingerprint": digest(CHECK),
                "success": bool(result["ok"] and passed), "validation_outcome": "passed" if passed else "failed",
                "elapsed_ms": round((monotonic() - start) * 1000), "receipt_ids": result["executions"]}
            trials.append(trial)
            save(root / ".tasktra/runtime/experiment.json", {"result": result, "trial": trial, "receipts": rows,
                "check_unchanged": unchanged_check, "scope": scope,
                "final_source": (root / "names.py").read_text(encoding="utf-8")})
            save(output / "trials.json", trials)
            save(output / "receipts.json", receipts)
            print(json.dumps({"event": "arm-finished", "mode": mode, "success": trial["success"]}), flush=True)
    report = compare_verified_trials(trials, receipt_resolver=lambda identifier: receipts[identifier], execution_records=receipts.values())
    save(output / "comparison.json", report)
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
