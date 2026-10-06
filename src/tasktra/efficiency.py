"""Pure, conservative token-usage summaries and trial comparisons.

External JSON is caller-attested. It may organize trial results, but it never
becomes a measured savings claim. The receipt resolver is an explicit trusted
integration dependency; its data is labelled resolver-attested, not authenticated.
"""
from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import Any, Callable, Iterable, Iterator, Mapping

_COUNTERS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
_OPTIONAL = frozenset({"cached_input_tokens", "cache_write_input_tokens", "reasoning_output_tokens"})
_MEASURED = frozenset({"host-callback", "rollout-verified"})
_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
_GROUP_LIMIT = 128


class EfficiencyError(ValueError):
    """Raised when a bounded comparison contract is malformed."""


def _count(value: Any, field: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 1_000_000_000_000:
        raise EfficiencyError(f"{field} must be a non-negative bounded integer")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise EfficiencyError(f"{field} must be a non-empty bounded string")
    return value


def _usage(value: Any) -> dict[str, int | None] | None:
    if not isinstance(value, Mapping) or set(value) != set(_COUNTERS):
        return None
    try:
        result = {name: _count(value[name], name, optional=name in _OPTIONAL) for name in _COUNTERS}
    except EfficiencyError:
        return None
    if result["total_tokens"] != result["input_tokens"] + result["output_tokens"]:
        return None
    if any(result[name] is not None and result[name] > result[parent] for name, parent in (
        ("cached_input_tokens", "input_tokens"), ("cache_write_input_tokens", "input_tokens"),
        ("reasoning_output_tokens", "output_tokens"),
    )):
        return None
    return result


def _iter_records(value: Mapping[str, Any] | Iterable[Mapping[str, Any]]) -> Iterator[Mapping[str, Any]]:
    source = value.get("executions", ()) if isinstance(value, Mapping) else value
    if isinstance(source, (str, bytes)):
        raise EfficiencyError("execution records must be an iterable of objects")
    try:
        for record in source:
            if not isinstance(record, Mapping):
                raise EfficiencyError("execution records must contain only objects")
            yield record
    except TypeError as error:
        raise EfficiencyError("execution records must be an iterable of objects") from error


def _provenance(record: Mapping[str, Any]) -> str | None:
    value = record.get("usage_provenance")
    if value is None and isinstance(record.get("verified"), Mapping):
        value = record["verified"].get("usage_provenance")
    return value if value in _MEASURED else None


def _record_usage(record: Mapping[str, Any]) -> dict[str, int | None] | None:
    value = record.get("usage")
    if value is None and isinstance(record.get("verified"), Mapping):
        value = record["verified"].get("usage")
    return _usage(value)


def _identity(record: Mapping[str, Any], field: str, fallback: str = "unknown") -> str:
    value = record.get(field)
    if value is None and isinstance(record.get("verified"), Mapping):
        value = record["verified"].get(field)
    return value if isinstance(value, str) and value else fallback


def _receipt_identity(record: Mapping[str, Any], usage: Mapping[str, int | None]) -> tuple[tuple[str, ...], tuple[Any, ...], tuple[str, ...]] | None:
    """Exact immutable identity only; arbitrary thread scopes are not receipts."""
    fingerprints = record.get("response_fingerprints")
    if not isinstance(fingerprints, (list, tuple)) or not fingerprints or not all(isinstance(item, str) and item for item in fingerprints):
        return None
    owner = tuple(str(record.get(field, "")) for field in
                  ("work_id", "source_sha256", "usage_provenance", "state", "role", "observed_model", "parent_work_id"))
    return tuple(sorted(set(fingerprints))), tuple(usage[name] for name in _COUNTERS), owner


def _attribution_anchor(record: Mapping[str, Any]) -> bool:
    return (record.get("role") == "coordinator" and record.get("state") == "planned"
            and record.get("attribution_reason") == "run-supervisor"
            and all(record.get(field) is None for field in
                    ("provider", "thread_id", "start_provenance", "finish_provenance", "usage", "usage_provenance")))


def summarize_executions(records: Mapping[str, Any] | Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Stream a full ledger into bounded conservative output.

    Exact duplicate receipts require matching fingerprint set, counters, and
    ownership. Partial overlap or conflicting receipt data withholds both
    sides. Records without immutable response fingerprints are never guessed
    to be duplicates.
    """
    coverage = {"records": 0, "planned": 0, "started": 0, "terminal": 0, "terminal_with_actual_usage": 0,
                "terminal_unknown_usage": 0, "actual_usage_records": 0, "untrusted_usage": 0,
                "deduplicated_records": 0, "ambiguous_receipts": 0, "invalid_usage": 0,
                "groups_omitted": 0, "observations_omitted": 0}
    totals = {name: 0 for name in ("input_tokens", "output_tokens", "total_tokens")}
    subset_totals = {name: 0 for name in _OPTIONAL}; subset_known = {name: 0 for name in _OPTIONAL}
    groups: dict[tuple[str, str], dict[str, int]] = {}; group_overflow = False; omitted_groups: set[tuple[str, str]] = set()
    accepted: dict[object, tuple[Mapping[str, Any], dict[str, int | None], tuple[str, str]]] = {}
    fingerprint_owner: dict[str, object] = {}
    ambiguous_fingerprints: set[str] = set()
    all_work_ids: set[str] = set(); parent_links: list[str] = []; sequence = 0
    execution_count = 0

    def group_for(key: tuple[str, str]) -> dict[str, int]:
        nonlocal group_overflow
        if key not in groups and len(groups) >= _GROUP_LIMIT:
            omitted_groups.add(key); key = ("other", "other"); group_overflow = True
        return groups.setdefault(key, {"records": 0, "actual_usage_records": 0, "total_tokens": 0})

    def add(record: Mapping[str, Any], usage: dict[str, int | None], key: tuple[str, str], sign: int) -> None:
        coverage["actual_usage_records"] += sign
        if record.get("state") in _TERMINAL:
            coverage["terminal_with_actual_usage"] += sign
        for name in totals: totals[name] += sign * int(usage[name])
        for name in _OPTIONAL:
            if usage[name] is not None:
                subset_totals[name] += sign * int(usage[name]); subset_known[name] += sign
        group = group_for(key); group["actual_usage_records"] += sign; group["total_tokens"] += sign * int(usage["total_tokens"])

    for record in _iter_records(records):
        sequence += 1; coverage["records"] += 1; state = record.get("state")
        execution_count += int(not _attribution_anchor(record))
        if state == "planned": coverage["planned"] += 1
        elif state == "started": coverage["started"] += 1
        elif state in _TERMINAL: coverage["terminal"] += 1
        if isinstance(record.get("work_id"), str) and record["work_id"]: all_work_ids.add(record["work_id"])
        if isinstance(record.get("parent_work_id"), str) and record["parent_work_id"]: parent_links.append(record["parent_work_id"])
        group_key = (_identity(record, "role"), _identity(record, "observed_model")); group_for(group_key)["records"] += 1
        usage, provenance = _record_usage(record), _provenance(record)
        if usage is None:
            if record.get("usage") is not None: coverage["invalid_usage"] += 1
            if state in _TERMINAL: coverage["terminal_unknown_usage"] += 1
            continue
        if provenance is None:
            coverage["untrusted_usage"] += 1; continue
        identity = _receipt_identity(record, usage) or ("unfingerprinted", sequence)
        fingerprints = identity[0] if isinstance(identity, tuple) and isinstance(identity[0], tuple) else ()
        if ambiguous_fingerprints.intersection(fingerprints):
            ambiguous_fingerprints.update(fingerprints)
            coverage["ambiguous_receipts"] += 1
            continue
        if identity in accepted:
            coverage["deduplicated_records"] += 1; continue
        overlaps = {fingerprint_owner[item] for item in fingerprints if item in fingerprint_owner and fingerprint_owner[item] != identity}
        if overlaps:
            coverage["ambiguous_receipts"] += 1
            ambiguous_fingerprints.update(fingerprints)
            for old_key in overlaps:
                ambiguous_fingerprints.update(old_key[0])
                old = accepted.pop(old_key, None)
                if old is not None:
                    add(old[0], old[1], old[2], -1); coverage["ambiguous_receipts"] += 1
            continue
        accepted[identity] = (record, usage, group_key)
        for item in fingerprints: fingerprint_owner[item] = identity
        add(record, usage, group_key, 1)

    outcomes = {"succeeded": 0, "failed": 0, "cancelled": 0}
    for record, usage, _ in accepted.values():
        if record.get("state") in outcomes: outcomes[record["state"]] += int(usage["total_tokens"])
    outcomes["non_success"] = outcomes["failed"] + outcomes["cancelled"]
    subsets = {name: subset_totals[name] if subset_known[name] == coverage["actual_usage_records"] else None for name in _OPTIONAL}
    subset_coverage = {name: {"known_records": subset_known[name], "unknown_records": coverage["actual_usage_records"] - subset_known[name], "known_total": subset_totals[name]} for name in _OPTIONAL}
    lineage = "not-observed" if not parent_links else ("complete" if all(parent in all_work_ids for parent in parent_links) else "incomplete")
    observations = []
    for kind, count, detail in (("unknown-terminal-usage", coverage["terminal_unknown_usage"], "terminal executions without actual verified usage are excluded"),
                                ("untrusted-usage-excluded", coverage["untrusted_usage"], "usage without supported receipt provenance is excluded"),
                                ("ambiguous-shared-receipt", coverage["ambiguous_receipts"], "overlapping or conflicting response receipts are withheld")):
        if count: observations.append({"kind": kind, "records": count, "detail": detail})
    coverage["groups_omitted"] = len(omitted_groups)
    if group_overflow: observations.append({"kind": "role-model-group-overflow", "groups_omitted": len(omitted_groups), "detail": "additional groups were folded into other"})
    unknown_records = max(0, execution_count - coverage["actual_usage_records"] - coverage["deduplicated_records"])
    return {"kind": "tasktra.token-efficiency-summary", "schema_version": 2, "coverage": coverage,
            "totals": {"known": totals, "known_records": coverage["actual_usage_records"],
                       "unknown_records": unknown_records,
                       "complete": unknown_records == 0 and coverage["started"] == 0 and not coverage["ambiguous_receipts"] and lineage != "incomplete",
                       "subsets": subsets, "subset_coverage": subset_coverage, "outcome_total_tokens": outcomes,
                       "non_success_terminal_tokens": outcomes["non_success"],
                       "lineage": {"status": lineage, "known_parent_records": len(parent_links), "missing_parent_records": sum(parent not in all_work_ids for parent in parent_links)},
                       "note": "subsets overlap parent totals and are not additive"},
            "by_role_model": [{"role": role, "model": model, "known_records": values["actual_usage_records"], **values} for (role, model), values in sorted(groups.items())], "observations": observations}


def _trial(value: Mapping[str, Any], *, verified: bool) -> dict[str, Any]:
    if not isinstance(value, Mapping): raise EfficiencyError("trial must be an object")
    allowed = {"scenario", "replicate", "mode", "config_fingerprint", "acceptance_fingerprint", "success", "validation_outcome", "retry_count", "elapsed_ms", "receipt_ids"}
    unknown = set(value) - allowed
    if unknown: raise EfficiencyError(f"trial has unsupported field(s): {', '.join(sorted(map(str, unknown)))}")
    required = {"scenario", "replicate", "mode", "config_fingerprint", "acceptance_fingerprint", "success", "validation_outcome"}
    if verified: required.add("receipt_ids")
    missing = required - set(value)
    if missing: raise EfficiencyError(f"trial is missing field(s): {', '.join(sorted(missing))}")
    mode = _text(value["mode"], "mode")
    if mode not in {"direct", "current", "optimized"}: raise EfficiencyError("mode must be direct, current, or optimized")
    if not isinstance(value["success"], bool): raise EfficiencyError("success must be boolean")
    validation = _text(value["validation_outcome"], "validation_outcome")
    if validation not in {"passed", "failed", "unknown", "not-run"}: raise EfficiencyError("validation_outcome is not supported")
    replicate = value["replicate"]
    if isinstance(replicate, bool) or not isinstance(replicate, (str, int)): raise EfficiencyError("replicate must be a bounded string or integer")
    ids = value.get("receipt_ids", ())
    if verified and (not isinstance(ids, (list, tuple)) or len(ids) > 512
                     or not all(isinstance(item, str) and 0 < len(item) <= 128 for item in ids)
                     or len(set(ids)) != len(ids) or (not ids and value["success"])):
        raise EfficiencyError("receipt_ids must be distinct bounded identifiers")
    return {"scenario": _text(value["scenario"], "scenario"), "replicate": _text(str(replicate), "replicate"), "mode": mode,
            "config_fingerprint": _text(value["config_fingerprint"], "config_fingerprint"), "acceptance_fingerprint": _text(value["acceptance_fingerprint"], "acceptance_fingerprint"),
            "success": value["success"], "validation_outcome": validation, "receipt_ids": tuple(ids) if isinstance(ids, (list, tuple)) else (),
            "retry_count": _count(value.get("retry_count", 0), "retry_count"), "elapsed_ms": _count(value.get("elapsed_ms", 0), "elapsed_ms")}


def _public_trial(trial: Mapping[str, Any]) -> dict[str, Any]:
    return {name: trial[name] for name in ("mode", "success", "validation_outcome", "elapsed_ms")} | {
        "receipt_ids": list(trial["receipt_ids"]), "caller_attested_retry_count": trial["retry_count"]}


def _report(trials: list[dict[str, Any]], *, level: str, usage: Mapping[int, Mapping[str, Any]] | None, baseline_mode: str | None) -> dict[str, Any]:
    if baseline_mode not in {None, "direct", "current"}: raise EfficiencyError("baseline_mode must be direct or current")
    groups: dict[tuple[str, str, str, str], list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for index, trial in enumerate(trials): groups[(trial["scenario"], trial["replicate"], trial["config_fingerprint"], trial["acceptance_fingerprint"])].append((index, trial))
    pairs = []; observations = []; deltas: dict[str, list[int]] = defaultdict(list); percents: dict[str, list[float]] = defaultdict(list); before_totals: dict[str, int] = defaultdict(int); after_totals: dict[str, int] = defaultdict(int)
    for (scenario, replicate, config, acceptance), items in sorted(groups.items()):
        modes: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
        for item in items: modes[item[1]["mode"]].append(item)
        controls = (baseline_mode,) if baseline_mode else tuple(mode for mode in ("direct", "current") if modes[mode])
        for control in controls or ("current",):
            reasons = []; baseline = modes[control][0] if len(modes[control]) == 1 else None; optimized = modes["optimized"][0] if len(modes["optimized"]) == 1 else None
            if baseline is None: reasons.append(f"missing-{control}-baseline" if not modes[control] else f"duplicate-{control}-baseline")
            if optimized is None: reasons.append("missing-optimized" if not modes["optimized"] else "duplicate-optimized")
            if baseline and optimized and (not baseline[1]["success"] or not optimized[1]["success"] or baseline[1]["validation_outcome"] != "passed" or optimized[1]["validation_outcome"] != "passed"): reasons.append("quality-not-equivalent")
            pair = {"scenario": scenario, "replicate": replicate, "config_fingerprint": config, "acceptance_fingerprint": acceptance, "baseline_mode": control, "evidence_level": level, "quality_evidence_level": "caller-attested", "reasons": reasons}
            if baseline: pair["baseline"] = _public_trial(baseline[1])
            if optimized: pair["optimized"] = _public_trial(optimized[1])
            if level == "caller-attested":
                pair.update({"eligible": False, "claim": "attested-comparison-only"})
            elif baseline and optimized and not reasons and usage is not None:
                before, after = usage[baseline[0]], usage[optimized[0]]
                if not (before["complete"] and after["complete"]): reasons.append("incomplete-receipt-coverage")
                else:
                    first, second = before["total_tokens"], after["total_tokens"]; delta = second - first
                    pair.update({"eligible": True, "baseline_total_tokens": first, "optimized_total_tokens": second, "total_token_delta": delta,
                                 "total_token_percent_delta": (delta / first * 100) if first else None,
                                 "derived_retry_count": {"baseline": before["retry_count"], "optimized": after["retry_count"]},
                                 "retry_evidence": "unavailable", "claim": "resolver-attested-usage-and-caller-attested-quality"})
                    deltas[control].append(delta)
                    if first: percents[control].append(delta / first * 100)
                    before_totals[control] += first; after_totals[control] += second
            if "eligible" not in pair: pair["eligible"] = False
            if not pair["eligible"]: observations.append({"kind": "ineligible-trial-pair", "scenario": scenario, "replicate": replicate, "baseline_mode": control, "reasons": reasons})
            pairs.append(pair)
    scenario_replicates: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    for scenario, replicate, config, acceptance in groups:
        scenario_replicates[(scenario, replicate)].add((config, acceptance))
    for (scenario, replicate), fingerprints in sorted(scenario_replicates.items()):
        if len(fingerprints) > 1:
            observations.append({"kind": "mismatched-fingerprint", "scenario": scenario, "replicate": replicate,
                                 "detail": "different configuration or acceptance fingerprints were not paired"})
    aggregates = {}
    for control in ("direct", "current"):
        first, second = before_totals[control], after_totals[control]
        pair_count = sum(pair["baseline_mode"] == control for pair in pairs)
        aggregates[f"optimized_vs_{control}"] = {"pair_count": pair_count, "ineligible_pairs": pair_count - len(deltas[control]),
                                                  "scope": "complete-case-only", "overall_savings_claim": None,
                                                  "eligible_pairs": len(deltas[control]), "baseline_total_tokens": first, "optimized_total_tokens": second,
                                                  "total_token_delta": second - first if deltas[control] else None,
                                                  "total_token_percent_delta": ((second - first) / first * 100) if first else None,
                                                  "median_total_token_delta": median(deltas[control]) if deltas[control] else None,
                                                  "median_total_token_percent_delta": median(percents[control]) if percents[control] else None,
                                                  "percent_sample_count": len(percents[control])}
    return {"kind": "tasktra.token-efficiency-trial-report", "schema_version": 2, "evidence_level": level, "pairs": pairs, "aggregates": aggregates, "observations": observations}


def compare_trials(trials: Iterable[Mapping[str, Any]] | Mapping[str, Any], *, baseline_mode: str | None = None) -> dict[str, Any]:
    """Organize caller-attested JSON. It never reports measured savings."""
    source = trials.get("trials", ()) if isinstance(trials, Mapping) else trials
    if isinstance(source, (str, bytes)): raise EfficiencyError("trials must be an iterable of objects")
    try: materialized = [_trial(item, verified=False) for item in source]
    except TypeError as error: raise EfficiencyError("trials must be an iterable of objects") from error
    if not materialized: raise EfficiencyError("trials must contain observations")
    return _report(materialized, level="caller-attested", usage=None, baseline_mode=baseline_mode)


def compare_verified_trials(trials: Iterable[Mapping[str, Any]], *, receipt_resolver: Callable[[str], Mapping[str, Any]],
                            execution_records: Iterable[Mapping[str, Any]], baseline_mode: str | None = None) -> dict[str, Any]:
    """Compare resolver-attested receipts and their full declared lineage.

    Integration owns ``receipt_resolver`` (normally ``store.get``) and supplies
    ``store.iter_records()``. This function does not authenticate arbitrary
    resolvers. Every sibling under a selected root is included, exposing failed
    attempts; the execution ledger cannot distinguish stages from retries.
    """
    if not callable(receipt_resolver): raise EfficiencyError("receipt_resolver must be callable")
    materialized = [_trial(item, verified=True) for item in trials]
    all_records: dict[str, Mapping[str, Any]] = {}
    for record in _iter_records(execution_records):
        work_id = record.get("work_id")
        if not isinstance(work_id, str) or not work_id or work_id in all_records:
            raise EfficiencyError("execution_records must contain distinct work_id values")
        all_records[work_id] = record

    def root_id(record: Mapping[str, Any]) -> tuple[str, bool]:
        current = record; seen: set[str] = set(); complete = True
        while isinstance(current.get("parent_work_id"), str) and current["parent_work_id"]:
            parent = current["parent_work_id"]
            if parent in seen or parent not in all_records:
                return parent, False
            seen.add(parent); current = all_records[parent]
        return str(current.get("work_id", "")), complete

    assigned_roots: set[str] = set(); usage: dict[int, dict[str, Any]] = {}
    for index, trial in enumerate(materialized):
        selected: list[Mapping[str, Any]] = []
        for receipt_id in trial["receipt_ids"]:
            try:
                receipt = receipt_resolver(receipt_id)
            except (LookupError, ValueError) as error:
                raise EfficiencyError("receipt_ids must resolve to matching execution receipts") from error
            if not isinstance(receipt, Mapping) or receipt.get("work_id") != receipt_id:
                raise EfficiencyError("receipt_ids must resolve to matching execution receipts")
            selected.append(receipt)
        roots = {root_id(record)[0] for record in selected}
        if assigned_roots & roots: raise EfficiencyError("receipt lineages must be disjoint across trial arms")
        assigned_roots |= roots
        lineage = [(record, root_id(record)[1]) for record in all_records.values() if root_id(record)[0] in roots]
        executable = [(record, valid) for record, valid in lineage if not _attribution_anchor(record)]
        resolved = [(record, _record_usage(record)) for record, _ in executable]
        complete = bool(executable) and all(lineage_ok and record.get("state") in _TERMINAL and token_usage is not None and _provenance(record) is not None
                                         for (record, lineage_ok), (_, token_usage) in zip(executable, resolved))
        summary = summarize_executions(record for record, _ in lineage)
        complete = complete and not summary["coverage"]["ambiguous_receipts"]
        measured = [token_usage for (record, token_usage) in resolved if token_usage is not None and _provenance(record) is not None]
        usage[index] = {"total_tokens": sum(int(token_usage["total_tokens"]) for token_usage in measured),
                        "retry_count": None, "complete": complete}
    return _report(materialized, level="resolver-attested", usage=usage, baseline_mode=baseline_mode)
