"""Tests for signature editions (书帖版) and errata work orders (勘误工单)."""

from __future__ import annotations

import itertools
import json
import random
import shutil
import tempfile
import unittest
from pathlib import Path

from citation_system import cli
from citation_system.catalog import Catalog
from citation_system.errors import EditionError, WorkOrderError
from citation_system.imposition import (
    ImpositionStore,
    impose_signature,
    parse_side_id,
    side_id,
    verify_edition_document,
)
from citation_system.press import (
    PressPlanner,
    replay_plan,
    solve_schedule,
)
from citation_system.repository import CitationRepository


def pages_doc(volume_id, page_ids, *, revision=1):
    return {
        "format": "volume-pages/1",
        "revision": revision,
        "volumes": [
            {
                "volume_id": volume_id,
                "pages": [
                    {
                        "page_id": pid,
                        "section_id": "body",
                        "label": pid.lstrip("p"),
                        "active": True,
                    }
                    for pid in page_ids
                ],
            }
        ],
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cat = Catalog.create(str(self.tmp / "cat.db"))
        self.repo = CitationRepository(self.cat)
        self.store = ImpositionStore(self.cat)
        self.planner = PressPlanner(self.cat)

    def tearDown(self):
        self.cat.close()
        shutil.rmtree(self.tmp)

    def load_book(self, n=8):
        self.cat.load_pages_document(pages_doc("BOOK", [f"p{i}" for i in range(1, n + 1)]))

    def order(self, edition_id, rounds, *, capacity=2, costs=None, initial=(),
              retentions=(), order_id="WO"):
        if costs is None:
            costs = {s["side_id"]: 1 for s in self.store.iter_sides(edition_id)}
        return {
            "format": "errata-work-order/1",
            "order_id": order_id,
            "edition_id": edition_id,
            "capacity": capacity,
            "initial_rack": sorted(initial),
            "mount_costs": costs,
            "retentions": list(retentions),
            "rounds": [
                {"round": i, "citations": list(group)}
                for i, group in enumerate(rounds, 1)
            ],
        }


class ImpositionMathTests(unittest.TestCase):
    def test_signature_8_layout(self):
        # (sheet, side, slot) -> position, from the imposition rule.
        self.assertEqual(
            impose_signature(8),
            [
                (0, "front", 0, 8), (0, "front", 1, 1),
                (0, "back", 0, 2), (0, "back", 1, 7),
                (1, "front", 0, 6), (1, "front", 1, 3),
                (1, "back", 0, 4), (1, "back", 1, 5),
            ],
        )

    def test_signature_4_layout(self):
        self.assertEqual(
            impose_signature(4),
            [
                (0, "front", 0, 4), (0, "front", 1, 1),
                (0, "back", 0, 2), (0, "back", 1, 3),
            ],
        )

    def test_side_id_roundtrip_and_malformed(self):
        self.assertEqual(parse_side_id("sig2/sheet1/back"), (2, 1, "back"))
        self.assertEqual(side_id(2, 1, "back"), "sig2/sheet1/back")
        for bad in ("", "sig0", "sig0/sheet0", "x0/sheet0/front",
                    "sig0/sheet0/top", "sig-1/sheet0/front"):
            with self.assertRaises(EditionError) as ctx:
                parse_side_id(bad)
            self.assertEqual(ctx.exception.code, "invalid_side_id")


class SignatureEditionTests(Base):
    def test_example_p2_p7_share_outer_sheet_back(self):
        self.load_book(8)
        self.store.create_edition("BOOK", 8, edition_id="ED1")
        p2 = self.store.locate_page("ED1", "p2")
        p7 = self.store.locate_page("ED1", "p7")
        self.assertEqual(p2.side_id, "sig0/sheet0/back")
        self.assertEqual(p7.side_id, "sig0/sheet0/back")
        # Independently queryable: sheet, side and slot (纸、面、格位).
        self.assertEqual((p2.sheet_index, p2.side, p2.slot, p2.position), (0, "back", 0, 2))
        self.assertEqual((p7.sheet_index, p7.side, p7.slot, p7.position), (0, "back", 1, 7))
        p1 = self.store.locate_page("ED1", "p1")
        self.assertEqual((p1.sheet_index, p1.side, p1.slot, p1.position), (0, "front", 1, 1))
        p8 = self.store.locate_page("ED1", "p8")
        self.assertEqual((p8.sheet_index, p8.side, p8.slot, p8.position), (0, "front", 0, 8))

    def test_blank_padding_never_fakes_pages(self):
        self.load_book(5)  # 5 in-use pages, size 4 -> sig1 padded with 3 blanks
        ed = self.store.create_edition("BOOK", 4, edition_id="ED1")
        self.assertEqual(ed.signature_count, 2)
        slots = self.store.slots("ED1")
        blanks = [s for s in slots if s.blank]
        self.assertEqual(len(blanks), 3)
        self.assertTrue(all(s.page_id is None for s in blanks))
        self.assertEqual([s.position for s in blanks], [2, 3, 4])
        self.assertTrue(all(s.signature_index == 1 for s in blanks))
        # Every non-blank slot points at a real page of the volume.
        real = {f"p{i}" for i in range(1, 6)}
        self.assertEqual({s.page_id for s in slots if not s.blank}, real)
        # The single real page of the last signature sits at position 1.
        p5 = self.store.locate_page("ED1", "p5")
        self.assertEqual((p5.signature_index, p5.position), (1, 1))
        self.assertEqual(p5.side_id, "sig1/sheet0/front")
        # A blank has no page identity, so nothing locates to it.
        with self.assertRaises(EditionError) as ctx:
            self.store.locate_page("ED1", "p6")
        self.assertEqual(ctx.exception.code, "page_not_in_edition")

    def test_in_use_order_skips_inactive_pages(self):
        self.load_book(5)
        self.cat.apply_operations(
            [{"operation": "deactivate", "volume_id": "BOOK", "page_id": "p2"}]
        )
        ed = self.store.create_edition("BOOK", 4, edition_id="ED1")
        self.assertEqual(ed.page_count, 4)
        self.assertEqual(ed.signature_count, 1)  # p1,p3,p4,p5 fill one signature
        with self.assertRaises(EditionError):
            self.store.locate_page("ED1", "p2")
        self.assertEqual(self.store.locate_page("ED1", "p3").position, 2)

    def test_edition_is_frozen_and_revision_checked(self):
        self.load_book(8)
        ed = self.store.create_edition("BOOK", 8, edition_id="ED1")
        self.cat.apply_operations(
            [{"operation": "move", "volume_id": "BOOK", "page_id": "p7",
              "before_page_id": "p6"}]
        )
        # The old edition still maps p7 to the outer sheet back.
        self.assertEqual(self.store.locate_page("ED1", "p7").side_id,
                         "sig0/sheet0/back")
        # Binding with a stale expected revision writes nothing.
        with self.assertRaises(EditionError) as ctx:
            self.store.create_edition(
                "BOOK", 8, edition_id="ED2", expected_revision=ed.catalog_revision
            )
        self.assertEqual(ctx.exception.code, "catalog_revision_mismatch")
        with self.assertRaises(EditionError):
            self.store.get_edition("ED2")
        # Binding the new current revision works and sees the new order.
        ed2 = self.store.create_edition(
            "BOOK", 8, edition_id="ED2",
            expected_revision=self.cat.revision(),
        )
        self.assertEqual(ed2.catalog_revision, ed.catalog_revision + 1)
        self.assertEqual(self.store.locate_page("ED2", "p7").side_id,
                         "sig0/sheet1/front")

    def test_new_edition_may_change_signature_size(self):
        self.load_book(8)
        self.store.create_edition("BOOK", 8, edition_id="ED8")
        self.store.create_edition("BOOK", 4, edition_id="ED4")
        self.assertEqual(self.store.locate_page("ED8", "p7").side_id,
                         "sig0/sheet0/back")
        # In a 4-up edition p7 is at position 3 of signature 1: back slot 1.
        self.assertEqual(self.store.locate_page("ED4", "p7").side_id,
                         "sig1/sheet0/back")
        self.assertEqual(self.store.get_edition("ED4").signature_count, 2)

    def test_copy_edition_is_a_faithful_reprint(self):
        self.load_book(8)
        ed = self.store.create_edition("BOOK", 8, edition_id="ED1")
        self.cat.apply_operations(
            [{"operation": "deactivate", "volume_id": "BOOK", "page_id": "p1"}]
        )
        copy = self.store.copy_edition("ED1", edition_id="ED1-reprint")
        self.assertEqual(copy.source_edition_id, "ED1")
        self.assertEqual(copy.layout_digest, ed.layout_digest)
        self.assertEqual(copy.catalog_revision, ed.catalog_revision)
        self.assertEqual(self.store.locate_page("ED1-reprint", "p7").side_id,
                         "sig0/sheet0/back")
        with self.assertRaises(EditionError) as ctx:
            self.store.copy_edition("ED1", edition_id="ED1-reprint")
        self.assertEqual(ctx.exception.code, "edition_exists")
        with self.assertRaises(EditionError) as ctx:
            self.store.copy_edition("NOPE", edition_id="EDX")
        self.assertEqual(ctx.exception.code, "edition_not_found")

    def test_invalid_signature_size(self):
        self.load_book(8)
        for bad in (0, 2, 6, 16):
            with self.assertRaises(EditionError) as ctx:
                self.store.create_edition("BOOK", bad)
            self.assertEqual(ctx.exception.code, "invalid_signature_size")

    def test_export_document_verifies_and_detects_tampering(self):
        self.load_book(5)
        self.store.create_edition("BOOK", 4, edition_id="ED1")
        doc = self.store.export_edition("ED1")
        self.assertEqual(verify_edition_document(doc), [])
        self.assertEqual(doc["format"], "signature-edition/1")
        tampered = json.loads(json.dumps(doc))
        tampered["slots"][0]["page_id"] = "ghost"
        tampered["slots"][0]["blank"] = False
        self.assertTrue(verify_edition_document(tampered))


class PressExampleTests(Base):
    """The worked examples from the task, on real stable pages."""

    def setUp(self):
        super().setUp()
        self.load_book(8)
        # A = sig0/sheet0/back {p2,p7}; B = sig0/sheet0/front {p8,p1};
        # C = sig0/sheet1/front {p6,p3}.
        self.repo.create_pinned("C_OUTER", "p2 and p7", "BOOK", ["p2", "p7"])
        self.repo.create_pinned("C_A1", "need A", "BOOK", ["p2"])
        self.repo.create_pinned("C_B", "need B", "BOOK", ["p8"])
        self.repo.create_pinned("C_C", "need C", "BOOK", ["p6"])
        self.repo.create_pinned("C_A2", "need A again", "BOOK", ["p7"])
        self.store.create_edition("BOOK", 8, edition_id="ED1")
        self.A = "sig0/sheet0/back"
        self.B = "sig0/sheet0/front"
        self.C = "sig0/sheet1/front"

    def test_single_round_one_side_costs_one(self):
        plan = self.planner.plan(
            self.order("ED1", [["C_OUTER"]], capacity=1)
        )
        self.assertEqual(plan["status"], "feasible")
        self.assertEqual(plan["total_cost"], 1)
        rnd = plan["rounds"][0]
        self.assertEqual(rnd["resolution"]["required_sides"], [self.A])
        self.assertEqual(rnd["on_rack"], [self.A])
        self.assertEqual([m["side"] for m in rnd["mounted"]], [self.A])

    def test_swap_p6_p7_breaks_capacity_one(self):
        self.cat.apply_operations(
            [{"operation": "move", "volume_id": "BOOK", "page_id": "p7",
              "before_page_id": "p6"}]
        )
        self.store.create_edition("BOOK", 8, edition_id="ED2")
        # The PINNED members are unchanged, but p7 moved to another side.
        plan = self.planner.plan(
            self.order("ED2", [["C_OUTER"]], capacity=1, order_id="WO-swap")
        )
        self.assertEqual(plan["status"], "infeasible")
        self.assertEqual(plan["first_infeasible_round"], 1)
        self.assertEqual(plan["infeasible"]["reason"], "capacity_exceeded")
        self.assertEqual(
            plan["rounds"][0]["resolution"]["required_sides"],
            [self.A, self.C],
        )
        # Two rack slots fit both sides; each side mounts once.
        plan2 = self.planner.plan(
            self.order("ED2", [["C_OUTER"]], capacity=2, order_id="WO-swap2")
        )
        self.assertEqual(plan2["status"], "feasible")
        self.assertEqual(plan2["total_cost"], 2)
        self.assertEqual(sorted(plan2["rounds"][0]["on_rack"]), [self.A, self.C])

    def test_abca_minimum_seven(self):
        costs = {s["side_id"]: 1 for s in self.store.iter_sides("ED1")}
        costs[self.A] = 5
        plan = self.planner.plan(
            self.order("ED1", [["C_A1"], ["C_B"], ["C_C"], ["C_A2"]],
                       capacity=2, costs=costs)
        )
        self.assertEqual(plan["status"], "feasible")
        self.assertEqual(plan["total_cost"], 7)
        # A is expensive, so it stays on the rack across all four rounds.
        for rnd in plan["rounds"]:
            self.assertIn(self.A, rnd["on_rack"])
        self.assertEqual([m["side"] for m in plan["rounds"][3]["mounted"]], [])

    def test_abca_with_retention_costs_twelve(self):
        costs = {s["side_id"]: 1 for s in self.store.iter_sides("ED1")}
        costs[self.A] = 5
        plan = self.planner.plan(
            self.order(
                "ED1", [["C_A1"], ["C_B"], ["C_C"], ["C_A2"]],
                capacity=2, costs=costs,
                retentions=[{"side": self.B, "from_round": 2, "to_round": 3}],
            )
        )
        self.assertEqual(plan["status"], "feasible")
        self.assertEqual(plan["total_cost"], 12)
        rounds = plan["rounds"]
        # B is pinned during rounds 2 and 3.
        self.assertIn(self.B, rounds[1]["on_rack"])
        self.assertIn(self.B, rounds[2]["on_rack"])
        # Round 3 needs B and C together, so A must come down beforehand...
        self.assertIn(self.A, rounds[1]["unmounted_after"])
        self.assertNotIn(self.A, rounds[2]["on_rack"])
        # ...and the last round remounts A at full price.
        self.assertEqual([m["side"] for m in rounds[3]["mounted"]], [self.A])
        self.assertEqual(rounds[3]["round_cost"], 5)


class PressResolutionTests(Base):
    def setUp(self):
        super().setUp()
        self.load_book(8)
        self.repo.create_pinned("C_PIN", "pair", "BOOK", ["p2", "p3"])
        self.repo.create_range("C_SPAN", "span", "BOOK", "p2", "p4")
        self.store.create_edition("BOOK", 8, edition_id="ED1")

    def test_range_endpoint_deactivated_blocks_its_round(self):
        self.cat.apply_operations(
            [{"operation": "deactivate", "volume_id": "BOOK", "page_id": "p4"}]
        )
        plan = self.planner.plan(
            self.order("ED1", [["C_PIN"], ["C_SPAN"]], capacity=2)
        )
        self.assertEqual(plan["status"], "infeasible")
        self.assertEqual(plan["first_infeasible_round"], 2)
        self.assertEqual(plan["infeasible"]["reason"], "range_endpoint_inactive")
        # The first round still resolved cleanly.
        self.assertEqual(plan["rounds"][0]["resolution"]["status"], "ok")
        self.assertEqual(plan["rounds"][1]["resolution"]["status"], "failed")

    def test_pinned_inactive_member_blocks_round(self):
        self.cat.apply_operations(
            [{"operation": "deactivate", "volume_id": "BOOK", "page_id": "p3"}]
        )
        plan = self.planner.plan(self.order("ED1", [["C_PIN"]], capacity=2))
        self.assertEqual(plan["status"], "infeasible")
        self.assertEqual(plan["infeasible"]["reason"], "pinned_inactive_member")

    def test_unknown_citation_blocks_round(self):
        plan = self.planner.plan(self.order("ED1", [["C_GHOST"]], capacity=2))
        self.assertEqual(plan["status"], "infeasible")
        self.assertEqual(plan["first_infeasible_round"], 1)
        self.assertEqual(plan["infeasible"]["reason"], "citation_not_found")

    def test_page_outside_edition_blocks_round(self):
        # p9 is inserted after the edition was frozen; a RANGE now spans it.
        self.repo.create_range("C_WIDE", "wide", "BOOK", "p1", "p8")
        self.cat.apply_operations(
            [{"operation": "insert", "volume_id": "BOOK", "after_page_id": "p8",
              "page": {"page_id": "p9", "section_id": "body", "label": "9",
                       "active": True}}]
        )
        self.repo.create_range("C_ALL", "all", "BOOK", "p1", "p9")
        plan = self.planner.plan(self.order("ED1", [["C_ALL"]], capacity=4))
        self.assertEqual(plan["status"], "infeasible")
        self.assertEqual(plan["infeasible"]["reason"], "page_not_in_edition")
        self.assertEqual(plan["infeasible"]["detail"]["page_id"], "p9")

    def test_cross_volume_citation_cannot_locate(self):
        self.cat.load_pages_document(pages_doc("OTHER", ["q1"]))
        self.repo.create_pinned("C_OTHER", "elsewhere", "OTHER", ["q1"])
        plan = self.planner.plan(self.order("ED1", [["C_OTHER"]], capacity=2))
        self.assertEqual(plan["status"], "infeasible")
        self.assertEqual(plan["infeasible"]["reason"], "page_not_in_edition")


class PressSchedulingTests(Base):
    def setUp(self):
        super().setUp()
        self.load_book(8)
        self.repo.create_pinned("C_A", "a", "BOOK", ["p2"])  # sig0/sheet0/back
        self.repo.create_pinned("C_B", "b", "BOOK", ["p8"])  # sig0/sheet0/front
        self.store.create_edition("BOOK", 8, edition_id="ED1")
        self.A = "sig0/sheet0/back"
        self.B = "sig0/sheet0/front"

    def test_initial_rack_is_free(self):
        plan = self.planner.plan(
            self.order("ED1", [["C_A"]], capacity=1, initial=[self.A])
        )
        self.assertEqual(plan["status"], "feasible")
        self.assertEqual(plan["total_cost"], 0)
        self.assertEqual(plan["rounds"][0]["mounted"], [])

    def test_retention_satisfied_from_initial_rack(self):
        plan = self.planner.plan(
            self.order(
                "ED1", [["C_A"], ["C_A"]], capacity=2, initial=[self.B],
                retentions=[{"side": self.B, "from_round": 1, "to_round": 2}],
            )
        )
        self.assertEqual(plan["status"], "feasible")
        self.assertEqual(plan["total_cost"], 1)  # only A mounts, B was free
        for rnd in plan["rounds"]:
            self.assertIn(self.B, rnd["on_rack"])

    def test_idle_round_with_empty_group(self):
        plan = self.planner.plan(self.order("ED1", [["C_A"], []], capacity=1))
        self.assertEqual(plan["status"], "feasible")
        self.assertEqual(plan["rounds"][1]["resolution"]["required_sides"], [])

    def test_plan_is_deterministic(self):
        order = self.order("ED1", [["C_A"], ["C_B"], ["C_A"]], capacity=2)
        self.assertEqual(self.planner.plan(order)["plan_digest"],
                         self.planner.plan(order)["plan_digest"])

    def test_missing_mount_cost_rejected(self):
        order = self.order("ED1", [["C_A"]], capacity=1, costs={self.B: 1})
        with self.assertRaises(WorkOrderError) as ctx:
            self.planner.plan(order)
        self.assertEqual(ctx.exception.code, "missing_mount_cost")
        self.assertEqual(ctx.exception.details["sides"], [self.A])

    def test_unknown_side_rejected(self):
        order = self.order("ED1", [["C_A"]], capacity=2, initial=["sig9/sheet0/front"])
        with self.assertRaises(WorkOrderError) as ctx:
            self.planner.plan(order)
        self.assertEqual(ctx.exception.code, "invalid_side_id")

    def test_retention_window_validated(self):
        base = self.order("ED1", [["C_A"]], capacity=1)
        for bad in ({"side": self.A, "from_round": 2, "to_round": 1},
                    {"side": self.A, "from_round": 1, "to_round": 2},
                    {"side": self.A, "from_round": 0, "to_round": 1}):
            with self.assertRaises(WorkOrderError) as ctx:
                self.planner.plan({**base, "retentions": [bad]})
            self.assertEqual(ctx.exception.code, "invalid_work_order")

    def test_shift_limits(self):
        order = self.order("ED1", [["C_A"]], capacity=5)
        with self.assertRaises(WorkOrderError) as ctx:
            self.planner.plan(order)
        self.assertEqual(ctx.exception.code, "shift_limits_exceeded")
        order = self.order("ED1", [["C_A"]] * 41, capacity=1)
        with self.assertRaises(WorkOrderError) as ctx:
            self.planner.plan(order)
        self.assertEqual(ctx.exception.code, "shift_limits_exceeded")

    def test_side_limit_exceeded(self):
        # 40 pages, 4-up: 10 signatures x 2 sides = 20 sides.
        self.cat.load_pages_document(pages_doc("WIDE", [f"w{i}" for i in range(1, 41)]))
        self.store.create_edition("WIDE", 4, edition_id="EDW")
        # Position 1 of each signature lands on its front, position 2 on its
        # back: these 15 pages sit on 15 distinct sides.
        picked = [f"w{4 * k + 1}" for k in range(10)] + [f"w{4 * k + 2}" for k in range(5)]
        sids = {self.store.locate_page("EDW", p).side_id for p in picked}
        self.assertEqual(len(sids), 15)
        self.repo.create_pinned("C_WIDE", "wide", "WIDE", picked)
        order = self.order("EDW", [["C_WIDE"]], capacity=4)
        with self.assertRaises(WorkOrderError) as ctx:
            self.planner.plan(order)
        self.assertEqual(ctx.exception.code, "shift_limits_exceeded")


class SolverInvariantTests(unittest.TestCase):
    """Cross-check the exact DP against an exhaustive brute force."""

    def brute_force(self, required, retained, costs, capacity, initial):
        universe = sorted(set().union(initial, *required, *retained))
        states = [frozenset(initial)]
        best = None
        first_bad = None
        for i, (req, ret) in enumerate(zip(required, retained)):
            must = req | ret
            if len(must) > capacity:
                first_bad = i + 1
                break
        if first_bad is not None:
            return False, first_bad, None
        choices = []
        for req, ret in zip(required, retained):
            must = frozenset(req | ret)
            options = [
                frozenset(s)
                for r in range(len(must), capacity + 1)
                for s in itertools.combinations(universe, r)
                if frozenset(s) >= must
            ]
            choices.append(options)
        for seq in itertools.product(*choices):
            cost, prev = 0, frozenset(initial)
            for state in seq:
                cost += sum(costs[s] for s in state - prev)
                prev = state
            if best is None or cost < best:
                best = cost
        return True, None, best

    def test_against_brute_force(self):
        rng = random.Random(20261121)
        for trial in range(300):
            n_sides = rng.randint(1, 5)
            sides = [f"s{i}" for i in range(n_sides)]
            costs = {s: rng.randint(1, 5) for s in sides}
            capacity = rng.randint(1, 2)
            rounds = rng.randint(1, 6)
            required = [
                {s for s in sides if rng.random() < 0.4} for _ in range(rounds)
            ]
            retained = [
                {s for s in sides if rng.random() < 0.15} for _ in range(rounds)
            ]
            initial = {s for s in sides if rng.random() < 0.2}
            if len(initial) > capacity:
                initial = set(sorted(initial)[:capacity])
            solved = solve_schedule(required, retained, costs, capacity, initial)
            ok, first_bad, best = self.brute_force(
                required, retained, costs, capacity, initial
            )
            self.assertEqual(solved["feasible"], ok, f"trial {trial}")
            if not ok:
                self.assertEqual(solved["first_infeasible_round"], first_bad)
            else:
                self.assertEqual(solved["total_cost"], best, f"trial {trial}")
                # The reported states are a valid schedule attaining the cost.
                prev, total = frozenset(initial), 0
                for i, state in enumerate(solved["states"]):
                    self.assertLessEqual(len(state), capacity)
                    self.assertTrue(state >= frozenset(required[i] | retained[i]))
                    total += sum(costs[s] for s in state - prev)
                    prev = state
                self.assertEqual(total, solved["total_cost"])


class ReplayTests(Base):
    def setUp(self):
        super().setUp()
        self.load_book(8)
        self.repo.create_pinned("C_A1", "need A", "BOOK", ["p2"])
        self.repo.create_pinned("C_B", "need B", "BOOK", ["p8"])
        self.repo.create_pinned("C_C", "need C", "BOOK", ["p6"])
        self.repo.create_pinned("C_A2", "need A again", "BOOK", ["p7"])
        self.store.create_edition("BOOK", 8, edition_id="ED1")
        self.A = "sig0/sheet0/back"
        self.B = "sig0/sheet0/front"
        self.costs = {s["side_id"]: 1 for s in self.store.iter_sides("ED1")}
        self.costs[self.A] = 5
        self.order_doc = self.order(
            "ED1", [["C_A1"], ["C_B"], ["C_C"], ["C_A2"]],
            capacity=2, costs=self.costs,
            retentions=[{"side": self.B, "from_round": 2, "to_round": 3}],
        )
        self.plan = self.planner.plan(self.order_doc)
        self.edition_doc = self.store.export_edition("ED1")

    def test_offline_replay_valid(self):
        verdict = replay_plan(self.plan, self.edition_doc)
        self.assertTrue(verdict["valid"], verdict["problems"])
        self.assertEqual(verdict["total_cost"], 12)

    def test_replay_detects_tampered_schedule(self):
        bad = json.loads(json.dumps(self.plan))
        bad["rounds"][3]["mounted"] = []  # hide the remount of A
        verdict = replay_plan(bad, self.edition_doc)
        self.assertFalse(verdict["valid"])

    def test_replay_detects_resigned_fraud(self):
        # Even with a recomputed digest, a cheaper-than-optimal plan fails.
        from citation_system.imposition import content_digest

        bad = json.loads(json.dumps(self.plan))
        bad["total_cost"] = 11
        bad["plan_digest"] = content_digest(
            {k: v for k, v in bad.items() if k != "plan_digest"}
        )
        verdict = replay_plan(bad, self.edition_doc)
        self.assertFalse(verdict["valid"])
        self.assertTrue(any("optimality" in p or "cost_basis" in p
                            for p in verdict["problems"]))

    def test_replay_detects_wrong_edition(self):
        self.store.create_edition("BOOK", 4, edition_id="ED4")
        other = self.store.export_edition("ED4")
        verdict = replay_plan(self.plan, other)
        self.assertFalse(verdict["valid"])

    def test_old_plan_replays_after_catalog_moves_on(self):
        # The catalog moves on; the delivered plan + edition still replay.
        self.cat.apply_operations(
            [{"operation": "deactivate", "volume_id": "BOOK", "page_id": "p3"}]
        )
        self.store.create_edition("BOOK", 8, edition_id="ED2")
        verdict = replay_plan(self.plan, self.edition_doc)
        self.assertTrue(verdict["valid"], verdict["problems"])

    def test_infeasible_plan_replays(self):
        self.cat.apply_operations(
            [{"operation": "deactivate", "volume_id": "BOOK", "page_id": "p6"}]
        )
        plan = self.planner.plan(
            self.order("ED1", [["C_A1"], ["C_C"]], capacity=2, costs=self.costs)
        )
        self.assertEqual(plan["status"], "infeasible")
        verdict = replay_plan(plan, self.edition_doc)
        self.assertTrue(verdict["valid"], verdict["problems"])
        self.assertEqual(verdict["status"], "infeasible")


class CliSmokeTests(Base):
    def test_edition_plan_replay_roundtrip(self):
        self.load_book(8)
        self.repo.create_pinned("C_OUTER", "pair", "BOOK", ["p2", "p7"])
        db = str(self.tmp / "cat.db")
        edition_json = self.tmp / "edition.json"
        order_json = self.tmp / "order.json"
        plan_json = self.tmp / "plan.json"

        self.assertEqual(cli.main([
            "make-edition", "--db", db, "--volume", "BOOK",
            "--signature-size", "8", "--edition-id", "ED1",
        ]), 0)
        self.assertEqual(cli.main([
            "export-edition", "--db", db, "--edition", "ED1",
            "--out", str(edition_json),
        ]), 0)
        order = self.order("ED1", [["C_OUTER"]], capacity=1)
        order_json.write_text(json.dumps(order), encoding="utf-8")
        self.assertEqual(cli.main([
            "plan-press", "--db", db, "--order", str(order_json),
            "--out", str(plan_json),
        ]), 0)
        # Replay is offline: it only reads the two JSON documents.
        self.assertEqual(cli.main([
            "replay-plan", "--plan", str(plan_json), "--edition", str(edition_json),
        ]), 0)
        plan = json.loads(plan_json.read_text(encoding="utf-8"))
        self.assertEqual(plan["status"], "feasible")
        self.assertEqual(plan["total_cost"], 1)


if __name__ == "__main__":
    unittest.main()
