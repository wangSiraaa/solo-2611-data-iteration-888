"""Errata work orders (勘误工单) and press scheduling.

The print shop runs a shift in proof rounds (校样轮次).  Each round names a
group of excerpts (citations) that must be reviewed *simultaneously*.  The
whole group is resolved against the live citation repository with exactly
the semantics of the ``citation-product/1`` export; only when the whole
group resolves does the round know its **required paper sides**: the sides
of the bound signature edition (校样版) that carry the actually used pages.

Scheduling rules
----------------
* The rack has ``capacity`` slots; one side occupies exactly one slot, no
  matter how many rounds or citations use it.
* Mounted sides may stay on the rack across rounds; staying is free.
* After a round, any side not pinned by a retention order may be taken
  down; unmounting is free.
* A retention order (保留令) pins one side on the rack for every round in
  ``[from_round, to_round]``.
* Mounting a side -- first mount or remount -- costs that side's rated
  work-hours.  The initial rack is already mounted and costs nothing.

The optimizer minimizes total mount work-hours.  Mounting a side earlier
than its first need can never help (mounts cost the same whenever they
happen and a kept side only occupies a slot), so an optimal plan mounts
exactly the sides of ``required ∪ retained`` when they are not already up,
and keeps some subset of the rest.  With at most 14 sides, 4 slots and 40
rounds per shift, an exact subset DP is small and fully deterministic.

A round is **uncompletable** when its group fails to resolve (inactive
PINNED member, inactive/reversed RANGE endpoints, unknown citation, or a
used page the edition does not carry), or when ``required ∪ retained``
exceeds the rack capacity.  An infeasible order reports the *first* such
round.

The signed plan (``press-plan/1``) records the inputs (edition binding,
initial rack, mount costs, retentions), the per-round resolution and
schedule with the cost basis, and a sha256 digest.  Together with the
exported ``signature-edition/1`` document it replays **offline**: no
database is needed on site.
"""

from __future__ import annotations

import itertools
import json
import os
from pathlib import Path
from typing import Any

from . import export
from .catalog import Catalog
from .errors import (
    CITATION_NOT_FOUND,
    INVALID_WORK_ORDER,
    MISSING_MOUNT_COST,
    PAGE_NOT_IN_EDITION,
    SHIFT_LIMITS_EXCEEDED,
    WorkOrderError,
)
from .imposition import (
    ImpositionStore,
    content_digest,
    edition_page_side_map,
    parse_side_id,
    verify_edition_document,
)

ORDER_FORMAT = "errata-work-order/1"
PLAN_FORMAT = "press-plan/1"

# Single-shift limits (单班): 14 sides, 4 rack slots, 40 rounds.
MAX_SIDES = 14
MAX_CAPACITY = 4
MAX_ROUNDS = 40

REASON_CAPACITY_EXCEEDED = "capacity_exceeded"


# ----------------------------------------------------------------------
# work order parsing
# ----------------------------------------------------------------------
class WorkOrder:
    def __init__(
        self,
        order_id: str,
        edition_id: str,
        capacity: int,
        initial_rack: list[str],
        mount_costs: dict[str, int],
        retentions: list[dict[str, Any]],
        rounds: list[list[str]],
    ) -> None:
        self.order_id = order_id
        self.edition_id = edition_id
        self.capacity = capacity
        self.initial_rack = initial_rack
        self.mount_costs = mount_costs
        self.retentions = retentions  # normalized, merged per side
        self.rounds = rounds


def _fail(code: str, message: str, **details: Any) -> None:
    raise WorkOrderError(code, message, details=details)


def load_work_order(document: dict[str, Any]) -> WorkOrder:
    """Validate an ``errata-work-order/1`` document.  Raises WorkOrderError."""

    if not isinstance(document, dict):
        _fail(INVALID_WORK_ORDER, "work order must be an object")
    if document.get("format") != ORDER_FORMAT:
        _fail(INVALID_WORK_ORDER, "unsupported work order format",
              format=document.get("format"))
    order_id = document.get("order_id")
    if not isinstance(order_id, str) or not order_id:
        _fail(INVALID_WORK_ORDER, "order_id must be a non-empty string")
    edition_id = document.get("edition_id")
    if not isinstance(edition_id, str) or not edition_id:
        _fail(INVALID_WORK_ORDER, "edition_id must be a non-empty string")
    capacity = document.get("capacity")
    if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
        _fail(INVALID_WORK_ORDER, "capacity must be a positive integer")
    if capacity > MAX_CAPACITY:
        _fail(SHIFT_LIMITS_EXCEEDED,
              f"capacity {capacity} exceeds the single-shift limit {MAX_CAPACITY}",
              capacity=capacity, max_capacity=MAX_CAPACITY)

    initial_rack = document.get("initial_rack", [])
    if not isinstance(initial_rack, list) or not all(
        isinstance(s, str) for s in initial_rack
    ):
        _fail(INVALID_WORK_ORDER, "initial_rack must be a list of side ids")
    if len(set(initial_rack)) != len(initial_rack):
        _fail(INVALID_WORK_ORDER, "initial_rack lists a side twice")
    if len(initial_rack) > capacity:
        _fail(INVALID_WORK_ORDER, "initial rack does not fit the capacity",
              initial_rack=initial_rack, capacity=capacity)

    mount_costs = document.get("mount_costs")
    if not isinstance(mount_costs, dict):
        _fail(INVALID_WORK_ORDER, "mount_costs must be an object side -> hours")
    for side, cost in mount_costs.items():
        if not isinstance(cost, int) or isinstance(cost, bool) or cost <= 0:
            _fail(INVALID_WORK_ORDER,
                  f"mount cost for {side!r} must be a positive integer",
                  side=side, cost=cost)

    rounds_doc = document.get("rounds")
    if not isinstance(rounds_doc, list) or not rounds_doc:
        _fail(INVALID_WORK_ORDER, "rounds must be a non-empty list")
    if len(rounds_doc) > MAX_ROUNDS:
        _fail(SHIFT_LIMITS_EXCEEDED,
              f"{len(rounds_doc)} rounds exceed the single-shift limit {MAX_ROUNDS}",
              rounds=len(rounds_doc), max_rounds=MAX_ROUNDS)
    rounds: list[list[str]] = []
    for i, entry in enumerate(rounds_doc, 1):
        if not isinstance(entry, dict):
            _fail(INVALID_WORK_ORDER, f"round {i} must be an object")
        if "round" in entry and entry["round"] != i:
            _fail(INVALID_WORK_ORDER,
                  f"round numbers must be sequential; expected {i}",
                  round=i, given=entry["round"])
        citations = entry.get("citations")
        if not isinstance(citations, list) or not all(
            isinstance(c, str) and c for c in citations
        ):
            _fail(INVALID_WORK_ORDER,
                  f"round {i} citations must be a list of citation ids", round=i)
        rounds.append(list(citations))

    retentions: list[dict[str, Any]] = []
    for entry in document.get("retentions", []):
        if not isinstance(entry, dict):
            _fail(INVALID_WORK_ORDER, "retention orders must be objects")
        side = entry.get("side")
        frm, to = entry.get("from_round"), entry.get("to_round")
        if not isinstance(side, str) or not side:
            _fail(INVALID_WORK_ORDER, "retention needs a side id")
        if not all(isinstance(x, int) and not isinstance(x, bool) for x in (frm, to)):
            _fail(INVALID_WORK_ORDER, "retention rounds must be integers", side=side)
        if not (1 <= frm <= to <= len(rounds_doc)):
            _fail(INVALID_WORK_ORDER,
                  "retention window must satisfy 1 <= from <= to <= rounds",
                  side=side, from_round=frm, to_round=to, rounds=len(rounds_doc))
        retentions.append({"side": side, "from_round": frm, "to_round": to})
    retentions = _merge_retentions(retentions)

    return WorkOrder(
        order_id=order_id,
        edition_id=edition_id,
        capacity=capacity,
        initial_rack=sorted(initial_rack),
        mount_costs=dict(mount_costs),
        retentions=retentions,
        rounds=rounds,
    )


def _merge_retentions(retentions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Union overlapping/adjacent windows per side; deterministic order."""

    by_side: dict[str, list[list[int]]] = {}
    for r in retentions:
        by_side.setdefault(r["side"], []).append([r["from_round"], r["to_round"]])
    merged: list[dict[str, Any]] = []
    for side, windows in by_side.items():
        windows.sort()
        cur = windows[0]
        for w in windows[1:]:
            if w[0] <= cur[1] + 1:
                cur[1] = max(cur[1], w[1])
            else:
                merged.append({"side": side, "from_round": cur[0], "to_round": cur[1]})
                cur = w
        merged.append({"side": side, "from_round": cur[0], "to_round": cur[1]})
    merged.sort(key=lambda r: (r["from_round"], r["to_round"], r["side"]))
    return merged


def retained_per_round(retentions: list[dict[str, Any]], rounds: int) -> list[set[str]]:
    """retained[i] = sides pinned on the rack during round ``i + 1``."""

    per_round: list[set[str]] = [set() for _ in range(rounds)]
    for r in retentions:
        for rnd in range(r["from_round"], r["to_round"] + 1):
            per_round[rnd - 1].add(r["side"])
    return per_round


# ----------------------------------------------------------------------
# exact scheduler
# ----------------------------------------------------------------------
def solve_schedule(
    required: list[set[str]],
    retained: list[set[str]],
    costs: dict[str, int],
    capacity: int,
    initial: set[str],
) -> dict[str, Any]:
    """Minimum total mount work-hours; fully deterministic.

    Returns ``{"feasible": True, "states": [...], "total_cost": n}`` where
    ``states[i]`` is the frozenset on the rack during round ``i + 1``, or
    ``{"feasible": False, "first_infeasible_round": r, ...}``.
    """

    for i, (req, ret) in enumerate(zip(required, retained)):
        must = req | ret
        if len(must) > capacity:
            return {
                "feasible": False,
                "first_infeasible_round": i + 1,
                "reason": REASON_CAPACITY_EXCEEDED,
                "detail": {"needed": sorted(must), "capacity": capacity},
            }

    # dp: rack state -> min cost to be in that state after the round.
    dp: dict[frozenset[str], int] = {frozenset(initial): 0}
    parents: list[dict[frozenset[str], frozenset[str]]] = []
    for i in range(len(required)):
        must = frozenset(required[i] | retained[i])
        nxt: dict[frozenset[str], int] = {}
        parent: dict[frozenset[str], frozenset[str]] = {}

        def offer(state: frozenset[str], cost: int, prev: frozenset[str]) -> None:
            # Ties prefer the *larger* previous state (lazy unmount: sides
            # stay up while free), then the lexicographically smaller one.
            key = (cost, -len(prev), tuple(sorted(prev)))
            best = nxt.get(state)
            if best is None or key < (
                best, -len(parent[state]), tuple(sorted(parent[state]))
            ):
                nxt[state] = cost
                parent[state] = prev

        for state, cost in dp.items():
            mounts = must - state
            mount_cost = sum(costs[s] for s in mounts)
            keepable = sorted(state - must)
            for take in range(len(keepable) + 1):
                for keep in itertools.combinations(keepable, take):
                    new_state = must.union(keep)
                    if len(new_state) <= capacity:
                        offer(frozenset(new_state), cost + mount_cost, state)
        dp = nxt
        parents.append(parent)

    final_state = min(dp, key=lambda s: (dp[s], tuple(sorted(s))))
    total = dp[final_state]
    states: list[frozenset[str]] = [frozenset()] * len(required)
    cursor = final_state
    for i in range(len(required) - 1, -1, -1):
        states[i] = cursor
        cursor = parents[i][cursor]
    return {"feasible": True, "states": states, "total_cost": total}


# ----------------------------------------------------------------------
# planner
# ----------------------------------------------------------------------
class PressPlanner:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.conn = catalog.conn
        self.imposition = ImpositionStore(catalog)

    def plan(self, order_document: dict[str, Any]) -> dict[str, Any]:
        order = load_work_order(order_document)
        edition = self.imposition.get_edition(order.edition_id)
        edition_sides = {s["side_id"] for s in self.imposition.iter_sides(edition.edition_id)}
        self._check_sides(order, edition_sides)

        # Resolve every round's group against one committed snapshot, with
        # exactly the citation-product/1 semantics.
        revision, usable, attention = export.collect_products(self.conn)
        usable_by_id = {p["citation_id"]: p for p in usable}
        attention_by_id = {p["citation_id"]: p for p in attention}
        page_side = self.imposition.page_side_map(edition.edition_id)

        resolutions: list[dict[str, Any]] = []
        required: list[set[str]] = []
        first_failure: dict[str, Any] | None = None
        for i, group in enumerate(order.rounds, 1):
            resolution, sides = self._resolve_round(
                group, usable_by_id, attention_by_id, page_side, edition.volume_id
            )
            resolutions.append(resolution)
            required.append(sides or set())
            if first_failure is None and resolution["status"] == "failed":
                first_failure = {"round": i, "reason": resolution["reason"],
                                 "detail": resolution.get("detail", {})}

        retained = retained_per_round(order.retentions, len(order.rounds))
        universe = set(order.initial_rack) | set().union(*required, *retained)
        if len(universe) > MAX_SIDES:
            _fail(SHIFT_LIMITS_EXCEEDED,
                  f"{len(universe)} distinct sides exceed the single-shift limit "
                  f"{MAX_SIDES}",
                  sides=len(universe), max_sides=MAX_SIDES)
        missing_costs = sorted(
            s for s in set().union(*required, *retained)
            if s not in order.mount_costs
        )
        if missing_costs:
            _fail(MISSING_MOUNT_COST,
                  "no rated mount hours for sides " + ", ".join(missing_costs),
                  sides=missing_costs)

        body = self._plan_body(order, edition.edition_id, edition.layout_digest,
                               edition.volume_id, edition.catalog_revision,
                               revision, resolutions, retained)
        if first_failure is not None:
            body["status"] = "infeasible"
            body["first_infeasible_round"] = first_failure["round"]
            body["infeasible"] = first_failure
            body["total_cost"] = None
        else:
            solved = solve_schedule(
                required, retained, order.mount_costs, order.capacity,
                set(order.initial_rack),
            )
            if not solved["feasible"]:
                body["status"] = "infeasible"
                body["first_infeasible_round"] = solved["first_infeasible_round"]
                body["infeasible"] = {
                    "round": solved["first_infeasible_round"],
                    "reason": solved["reason"],
                    "detail": solved["detail"],
                }
                body["total_cost"] = None
            else:
                body["status"] = "feasible"
                self._fill_schedule(body, order, solved, retained)
                body["first_infeasible_round"] = None
                body["infeasible"] = None
        body["plan_digest"] = content_digest(body)
        return body

    # ------------------------------------------------------------------
    def _check_sides(self, order: WorkOrder, edition_sides: set[str]) -> None:
        referenced = (
            set(order.initial_rack)
            | set(order.mount_costs)
            | {r["side"] for r in order.retentions}
        )
        for side in sorted(referenced):
            parse_side_id(side)  # malformed -> invalid_side_id
            if side not in edition_sides:
                _fail("invalid_side_id",
                      f"side {side!r} does not exist in edition "
                      f"{order.edition_id!r}",
                      side=side, edition_id=order.edition_id)

    @staticmethod
    def _resolve_round(
        group: list[str],
        usable_by_id: dict[str, Any],
        attention_by_id: dict[str, Any],
        page_side: dict[str, str],
        edition_volume: str,
    ) -> tuple[dict[str, Any], set[str] | None]:
        """Resolve one round's group; the whole group must parse together."""

        pages: list[dict[str, str]] = []
        seen_pages: set[tuple[str, str]] = set()
        for cid in group:
            if cid in usable_by_id:
                for p in usable_by_id[cid]["pages"]:
                    key = (p["volume_id"], p["page_id"])
                    if key not in seen_pages:
                        seen_pages.add(key)
                        pages.append({"volume_id": p["volume_id"],
                                      "page_id": p["page_id"]})
            elif cid in attention_by_id:
                return {
                    "status": "failed",
                    "reason": attention_by_id[cid]["reason"],
                    "detail": {"citation_id": cid},
                }, None
            else:
                return {
                    "status": "failed",
                    "reason": CITATION_NOT_FOUND,
                    "detail": {"citation_id": cid},
                }, None
        sides: set[str] = set()
        for p in pages:
            side = page_side.get(p["page_id"]) if p["volume_id"] == edition_volume else None
            if side is None:
                return {
                    "status": "failed",
                    "reason": PAGE_NOT_IN_EDITION,
                    "detail": {"citation_id": None, "page_id": p["page_id"],
                               "volume_id": p["volume_id"]},
                }, None
            sides.add(side)
        return {
            "status": "ok",
            "pages": pages,
            "required_sides": sorted(sides),
        }, sides

    @staticmethod
    def _plan_body(
        order: WorkOrder,
        edition_id: str,
        layout_digest: str,
        volume_id: str,
        edition_revision: int,
        planned_revision: int,
        resolutions: list[dict[str, Any]],
        retained: list[set[str]],
    ) -> dict[str, Any]:
        return {
            "format": PLAN_FORMAT,
            "order_id": order.order_id,
            "status": None,  # filled by caller
            "edition_id": edition_id,
            "edition_layout_digest": layout_digest,
            "volume_id": volume_id,
            "edition_catalog_revision": edition_revision,
            "planned_at_catalog_revision": planned_revision,
            "capacity": order.capacity,
            "initial_rack": list(order.initial_rack),
            "mount_costs": {s: order.mount_costs[s] for s in sorted(order.mount_costs)},
            "retentions": [dict(r) for r in order.retentions],
            "rounds": [
                {
                    "round": i,
                    "citations": list(order.rounds[i - 1]),
                    "resolution": resolutions[i - 1],
                    "retained_sides": sorted(retained[i - 1]),
                }
                for i in range(1, len(order.rounds) + 1)
            ],
        }

    @staticmethod
    def _fill_schedule(
        body: dict[str, Any],
        order: WorkOrder,
        solved: dict[str, Any],
        retained: list[set[str]],
    ) -> None:
        states: list[frozenset[str]] = solved["states"]
        cumulative = 0
        for i, state in enumerate(states):
            prev = frozenset(order.initial_rack) if i == 0 else states[i - 1]
            nxt = states[i + 1] if i + 1 < len(states) else frozenset()
            mounted = sorted(state - prev)
            cost = sum(order.mount_costs[s] for s in mounted)
            cumulative += cost
            body["rounds"][i].update(
                {
                    "mounted": [{"side": s, "cost": order.mount_costs[s]}
                                for s in mounted],
                    "on_rack": sorted(state),
                    # Sides coming down after this round's run (after the
                    # last round the rack is cleared for the shift end).
                    "unmounted_after": sorted(state - nxt),
                    "round_cost": cost,
                    "cumulative_cost": cumulative,
                }
            )
        body["total_cost"] = cumulative


# ----------------------------------------------------------------------
# offline replay
# ----------------------------------------------------------------------
def replay_plan(plan: dict[str, Any], edition_document: dict[str, Any]) -> dict[str, Any]:
    """Re-execute a signed plan against the exported edition, offline.

    Checks the edition binding and digest, re-derives every round's required
    sides from the recorded pages, simulates the recorded schedule (capacity,
    retentions, mounts/unmounts, cost basis), re-runs the optimizer to
    confirm the recorded total is minimal, and verifies the plan digest.
    """

    problems: list[str] = []

    def check(name: str, ok: bool, detail: Any = None) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})
        if not ok:
            problems.append(f"{name}: {detail}")

    checks: list[dict[str, Any]] = []
    if plan.get("format") != PLAN_FORMAT:
        return {
            "valid": False,
            "checks": [],
            "problems": [f"bad plan format {plan.get('format')!r}"],
        }

    edition_problems = verify_edition_document(edition_document)
    check("edition_document", not edition_problems, edition_problems or "ok")

    bound = (
        plan.get("edition_id") == edition_document.get("edition_id")
        and plan.get("edition_layout_digest") == edition_document.get("layout_digest")
    )
    check("edition_binding", bound, {
        "plan_edition": plan.get("edition_id"),
        "document_edition": edition_document.get("edition_id"),
    })

    recorded_digest = plan.get("plan_digest")
    recomputed = content_digest({k: v for k, v in plan.items() if k != "plan_digest"})
    check("plan_digest", recorded_digest == recomputed, recorded_digest)

    rounds = plan.get("rounds", [])
    capacity = plan.get("capacity")
    check("shift_limits",
          isinstance(capacity, int) and 1 <= capacity <= MAX_CAPACITY
          and len(rounds) <= MAX_ROUNDS,
          {"capacity": capacity, "rounds": len(rounds)})

    # Re-derive required sides from the recorded pages through the edition.
    page_side = edition_page_side_map(edition_document)
    edition_volume = edition_document.get("volume_id")
    required: list[set[str]] = []
    resolution_ok = True
    for entry in rounds:
        resolution = entry.get("resolution", {})
        if resolution.get("status") != "ok":
            required.append(set())
            continue
        sides: set[str] = set()
        for p in resolution.get("pages", []):
            side = page_side.get(p.get("page_id")) \
                if p.get("volume_id") == edition_volume else None
            if side is None:
                check("round_pages", False,
                      {"round": entry.get("round"), "page_id": p.get("page_id")})
                resolution_ok = False
                continue
            sides.add(side)
        if sorted(sides) != resolution.get("required_sides"):
            check("required_sides", False, {
                "round": entry.get("round"),
                "recorded": resolution.get("required_sides"),
                "recomputed": sorted(sides),
            })
            resolution_ok = False
        required.append(sides)
    if resolution_ok:
        check("round_resolution", True, "ok")

    retained = retained_per_round(plan.get("retentions", []), len(rounds))
    costs = plan.get("mount_costs", {})
    initial = set(plan.get("initial_rack", []))

    if plan.get("status") == "feasible":
        _replay_schedule(plan, rounds, retained, costs, capacity, initial, check)
    elif plan.get("status") == "infeasible":
        _replay_infeasible(plan, rounds, required, retained, capacity, check)
    else:
        check("status", False, plan.get("status"))

    # Re-run the optimizer on the recorded inputs; the recorded outcome must
    # match the recomputed optimum exactly.  Only meaningful when every side
    # the schedule could mount has a rated cost in the plan.
    needed = set().union(initial, *required, *retained) if rounds else set(initial)
    costed = needed <= set(costs)
    check("mount_costs_cover_schedule", costed,
          sorted(needed - set(costs)) or "ok")
    solvable = all(
        r.get("status") == "ok" for r in (e.get("resolution", {}) for e in rounds)
    )
    if solvable and costed and isinstance(capacity, int) and capacity >= 1:
        solved = solve_schedule(required, retained, costs, capacity, initial)
        if solved["feasible"]:
            check("optimality",
                  plan.get("status") == "feasible"
                  and plan.get("total_cost") == solved["total_cost"],
                  {"recorded": plan.get("total_cost"),
                   "recomputed": solved["total_cost"]})
        else:
            check("optimality",
                  plan.get("status") == "infeasible"
                  and plan.get("first_infeasible_round")
                  == solved["first_infeasible_round"],
                  {"recorded": plan.get("first_infeasible_round"),
                   "recomputed": solved["first_infeasible_round"]})

    return {
        "valid": not problems,
        "order_id": plan.get("order_id"),
        "status": plan.get("status"),
        "total_cost": plan.get("total_cost"),
        "checks": checks,
        "problems": problems,
    }


def _replay_schedule(plan, rounds, retained, costs, capacity, initial, check) -> None:
    rack = set(initial)
    cumulative = 0
    schedule_ok = True
    for i, entry in enumerate(rounds):
        mounted = [m["side"] for m in entry.get("mounted", [])]
        if any(m in rack for m in mounted):
            check("schedule", False, {"round": entry.get("round"),
                                    "remount_of_mounted": mounted})
            schedule_ok = False
        rack |= set(mounted)
        on_rack = set(entry.get("on_rack", []))
        if rack != on_rack:
            check("schedule", False, {"round": entry.get("round"),
                                      "expected_rack": sorted(rack),
                                      "recorded": sorted(on_rack)})
            schedule_ok = False
        must = set(entry.get("resolution", {}).get("required_sides") or []) | retained[i]
        if not must <= on_rack:
            check("schedule", False, {"round": entry.get("round"),
                                      "missing": sorted(must - on_rack)})
            schedule_ok = False
        if len(on_rack) > capacity:
            check("schedule", False, {"round": entry.get("round"),
                                      "over_capacity": sorted(on_rack)})
            schedule_ok = False
        round_cost = sum(costs.get(m, 0) for m in mounted)
        cumulative += round_cost
        if entry.get("round_cost") != round_cost \
                or entry.get("cumulative_cost") != cumulative:
            check("cost_basis", False, {
                "round": entry.get("round"),
                "recorded": (entry.get("round_cost"), entry.get("cumulative_cost")),
                "recomputed": (round_cost, cumulative),
            })
            schedule_ok = False
        unmounted = set(entry.get("unmounted_after", []))
        if not unmounted <= on_rack:
            check("schedule", False, {"round": entry.get("round"),
                                      "unmounted_not_on_rack": sorted(unmounted)})
            schedule_ok = False
        # Note: unmounting after round i is only constrained by round i+1's
        # needs, which the next iteration's ``must <= on_rack`` verifies --
        # a side may come down right after the last round of its window.
        rack = on_rack - unmounted
    if plan.get("total_cost") != cumulative:
        check("cost_basis", False, {"recorded_total": plan.get("total_cost"),
                                    "recomputed_total": cumulative})
        schedule_ok = False
    # Retention orders hold for their whole windows.
    for r in plan.get("retentions", []):
        for rnd in range(r["from_round"], r["to_round"] + 1):
            on_rack = set(rounds[rnd - 1].get("on_rack", []))
            if r["side"] not in on_rack:
                check("retention", False, {"side": r["side"], "round": rnd})
                schedule_ok = False
    if schedule_ok:
        check("schedule", True, "ok")
        check("cost_basis", True, "ok")
        check("retention", True, "ok")


def _replay_infeasible(plan, rounds, required, retained, capacity, check) -> None:
    first = None
    for i, entry in enumerate(rounds, 1):
        resolution = entry.get("resolution", {})
        if resolution.get("status") == "failed":
            first = {"round": i, "reason": resolution.get("reason")}
            break
        must = required[i - 1] | retained[i - 1]
        if len(must) > capacity:
            first = {"round": i, "reason": REASON_CAPACITY_EXCEEDED}
            break
    ok = first is not None \
        and plan.get("first_infeasible_round") == first["round"] \
        and (plan.get("infeasible") or {}).get("reason") == first["reason"]
    check("first_infeasible_round", ok,
          {"recorded": plan.get("first_infeasible_round"),
           "recomputed": first})


# ----------------------------------------------------------------------
# file helpers
# ----------------------------------------------------------------------
def write_plan_atomic(plan: dict[str, Any], path: str | Path) -> None:
    """Write one plan JSON file via a sibling temp file + rename."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(plan, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def write_edition_atomic(document: dict[str, Any], path: str | Path) -> None:
    write_plan_atomic(document, path)  # same staging discipline
