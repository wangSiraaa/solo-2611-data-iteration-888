"""End-to-end demonstration of 书帖版 (signature editions) + 勘误工单 (press plans).

Builds a real 8-page volume, binds signature editions, works through every
example from the task statement with real stable page ids, and delivers the
shift materials (edition document + signed plan) under ``exports/press/``.
The final step replays the signed plans **offline** through the CLI, the way
the print shop would on site: no database involved.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from citation_system.catalog import Catalog  # noqa: E402
from citation_system.imposition import ImpositionStore  # noqa: E402
from citation_system.press import (  # noqa: E402
    PressPlanner,
    replay_plan,
    write_edition_atomic,
    write_plan_atomic,
)
from citation_system.repository import CitationRepository  # noqa: E402

WORK = ROOT / "work" / "press"
OUT = ROOT / "exports" / "press"

A = "sig0/sheet0/back"    # positions (2, 7) of an 8-up signature
B = "sig0/sheet0/front"   # positions (8, 1)
C = "sig0/sheet1/front"   # positions (6, 3)


def reset(path: Path) -> None:
    if path.exists():
        path.unlink()
    for suffix in ("-wal", "-shm"):
        extra = Path(str(path) + suffix)
        if extra.exists():
            extra.unlink()


def show_plan(title: str, plan: dict) -> None:
    print(f"\n=== {title} ===")
    print(f"  status={plan['status']} total_cost={plan['total_cost']} "
          f"digest={plan['plan_digest'][:23]}…")
    if plan["status"] == "infeasible":
        print(f"  first uncompletable round: {plan['first_infeasible_round']} "
              f"reason={plan['infeasible']['reason']} {plan['infeasible']['detail']}")
    for rnd in plan["rounds"]:
        line = f"  round {rnd['round']}: citations={rnd['citations']}"
        if rnd["resolution"]["status"] == "ok":
            line += f" required={rnd['resolution']['required_sides']}"
        else:
            line += f" FAILED({rnd['resolution']['reason']})"
        if "on_rack" in rnd:
            mounted = [f"{m['side']}(+{m['cost']})" for m in rnd["mounted"]]
            line += (f"\n      mounted={mounted} on_rack={rnd['on_rack']} "
                     f"unmounted_after={rnd['unmounted_after']} "
                     f"round_cost={rnd['round_cost']}")
        print(line)


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    db_path = WORK / "press.db"
    reset(db_path)

    cat = Catalog.create(str(db_path))
    cat.load_pages_document({
        "format": "volume-pages/1",
        "revision": 1,
        "volumes": [{
            "volume_id": "BOOK",
            "pages": [
                {"page_id": f"p{i}", "section_id": "body", "label": str(i),
                 "active": True}
                for i in range(1, 9)
            ],
        }],
    })
    repo = CitationRepository(cat)
    repo.create_pinned("C_OUTER", "p2 与 p7 同版复核", "BOOK", ["p2", "p7"])
    repo.create_pinned("C_A1", "首轮用 A 面", "BOOK", ["p2"])
    repo.create_pinned("C_B", "次轮用 B 面", "BOOK", ["p8"])
    repo.create_pinned("C_C", "三轮用 C 面", "BOOK", ["p6"])
    repo.create_pinned("C_A2", "末轮再用 A 面", "BOOK", ["p7"])
    repo.create_range("C_SPAN", "p2 至 p4 连续摘录", "BOOK", "p2", "p4")

    store = ImpositionStore(cat)
    planner = PressPlanner(cat)

    # ------------------------------------------------------------------
    print("=== 书帖版 ED1: BOOK, 8页一帖, 绑定目录修订 1 ===")
    ed1 = store.create_edition("BOOK", 8, edition_id="ED1")
    print(" ", json.dumps(ed1.as_dict(), ensure_ascii=False))
    for pid in ("p2", "p7"):
        slot = store.locate_page("ED1", pid)
        print(f"  {pid} -> 纸 sheet{slot.sheet_index} 面 {slot.side} "
              f"格位 {slot.slot} (位置 {slot.position}, {slot.side_id})")
    write_edition_atomic(store.export_edition("ED1"), OUT / "ED1.signature-edition.json")

    # ------------------------------------------------------------------
    # Example 1: one round, p2+p7 share the outer sheet back.
    order1 = {
        "format": "errata-work-order/1", "order_id": "WO-1",
        "edition_id": "ED1", "capacity": 1, "initial_rack": [],
        "mount_costs": {s["side_id"]: 1 for s in store.iter_sides("ED1")},
        "retentions": [],
        "rounds": [{"round": 1, "citations": ["C_OUTER"]}],
    }
    plan1 = planner.plan(order1)
    show_plan("例1: p2、p7 同在外纸背面, 容量1, 各面装版1小时", plan1)
    assert plan1["total_cost"] == 1

    # ------------------------------------------------------------------
    # Example 2: swap p6/p7 in the volume, bind a new edition.
    print("\n=== 同卷移位互换 p6/p7, 新书帖版 ED2 绑定修订 2 ===")
    cat.apply_operations([
        {"operation": "move", "volume_id": "BOOK", "page_id": "p7",
         "before_page_id": "p6"},
    ])
    ed2 = store.create_edition("BOOK", 8, edition_id="ED2")
    print(f"  ED2 catalog_revision={ed2.catalog_revision}; "
          f"p7 -> {store.locate_page('ED2', 'p7').side_id}, "
          f"p2 -> {store.locate_page('ED2', 'p2').side_id}")
    print(f"  旧版 ED1 仍可查: p7 -> {store.locate_page('ED1', 'p7').side_id}")
    write_edition_atomic(store.export_edition("ED2"), OUT / "ED2.signature-edition.json")

    costs2 = {s["side_id"]: 1 for s in store.iter_sides("ED2")}
    order2 = {**order1, "order_id": "WO-2", "edition_id": "ED2",
              "mount_costs": costs2}
    plan2 = planner.plan(order2)
    show_plan("例2: 固定引用成员不变, 但需两面同架, 容量1", plan2)
    assert plan2["status"] == "infeasible" and plan2["first_infeasible_round"] == 1

    order3 = {**order2, "order_id": "WO-3", "capacity": 2}
    plan3 = planner.plan(order3)
    show_plan("例2续: 架位增至2", plan3)
    assert plan3["total_cost"] == 2

    # ------------------------------------------------------------------
    # Example 3: A,B,C,A rounds with mount costs 5,1,1.
    abca_rounds = [{"round": i, "citations": [c]} for i, c in
                   enumerate(["C_A1", "C_B", "C_C", "C_A2"], 1)]
    costs_abca = {s["side_id"]: 1 for s in store.iter_sides("ED1")}
    costs_abca[A] = 5
    order4 = {
        "format": "errata-work-order/1", "order_id": "WO-4",
        "edition_id": "ED1", "capacity": 2, "initial_rack": [],
        "mount_costs": costs_abca, "retentions": [], "rounds": abca_rounds,
    }
    plan4 = planner.plan(order4)
    show_plan(f"例3: 四轮依次需要 A={A}, B={B}, C={C}, A; 装版 5/1/1", plan4)
    assert plan4["total_cost"] == 7

    order5 = {**order4, "order_id": "WO-5",
              "retentions": [{"side": B, "from_round": 2, "to_round": 3}]}
    plan5 = planner.plan(order5)
    show_plan(f"例3续: 保留令要求 B={B} 第2至3轮留架", plan5)
    assert plan5["total_cost"] == 12
    assert A in plan5["rounds"][1]["unmounted_after"]  # 第3轮前必须撤A
    assert [m["side"] for m in plan5["rounds"][3]["mounted"]] == [A]  # 末轮重装

    # ------------------------------------------------------------------
    # Blank padding: 5 in-use pages in a 4-up edition.
    print("\n=== 补空: PAD 卷 5 张在用页, 4页一帖 ===")
    cat.load_pages_document({
        "format": "volume-pages/1",
        "revision": 1,
        "volumes": [{
            "volume_id": "PAD",
            "pages": [
                {"page_id": f"q{i}", "section_id": "body", "label": str(i),
                 "active": True}
                for i in range(1, 6)
            ],
        }],
    })
    ed_pad = store.create_edition("PAD", 4, edition_id="ED-PAD")
    for side in store.iter_sides("ED-PAD"):
        cells = [(s["position"], s["page_id"] if not s["blank"] else "空位")
                 for s in side["slots"]]
        print(f"  {side['side_id']}: {cells}")
    blanks = [s for s in store.slots("ED-PAD") if s.blank]
    print(f"  末帖补空 {len(blanks)} 格, 空位不伪造页: "
          f"{all(s.page_id is None for s in blanks)}")

    # ------------------------------------------------------------------
    # Range endpoint deactivated: the round cannot be completed.
    print("\n=== 范围端停用: 停用 C_SPAN 的端页 p4 ===")
    cat.apply_operations([
        {"operation": "deactivate", "volume_id": "BOOK", "page_id": "p4"},
    ])
    order6 = {**order1, "order_id": "WO-6",
              "rounds": [{"round": 1, "citations": ["C_A1"]},
                         {"round": 2, "citations": ["C_SPAN"]}]}
    plan6 = planner.plan(order6)
    show_plan("例4: 第2轮的 RANGE 端页已停用", plan6)
    assert plan6["status"] == "infeasible"
    assert plan6["infeasible"]["reason"] == "range_endpoint_inactive"
    assert plan6["first_infeasible_round"] == 2
    cat.apply_operations([
        {"operation": "activate", "volume_id": "BOOK", "page_id": "p4"},
    ])

    # ------------------------------------------------------------------
    # Reprint an old edition (复印).
    print("\n=== 复印: ED1 -> ED1-reprint ===")
    reprint = store.copy_edition("ED1", edition_id="ED1-reprint")
    print(f"  source={reprint.source_edition_id} "
          f"layout_digest 相同: {reprint.layout_digest == ed1.layout_digest}")

    # ------------------------------------------------------------------
    # Deliver and replay offline (旧计划复查): the catalog moves on, the
    # delivered files still replay exactly.
    print("\n=== 交付与离线复读 ===")
    plans = {
        "WO-1": (plan1, "ED1"), "WO-3": (plan3, "ED2"),
        "WO-4": (plan4, "ED1"), "WO-5": (plan5, "ED1"), "WO-6": (plan6, "ED1"),
    }
    for name, (plan, _ed) in plans.items():
        write_plan_atomic(plan, OUT / f"{name}.press-plan.json")
    # The catalog moves on after delivery; old plans must still replay.
    cat.apply_operations([
        {"operation": "move", "volume_id": "BOOK", "page_id": "p5", "at_start": True},
    ])
    store.create_edition("BOOK", 8, edition_id="ED3")
    cat.close()

    for name, (plan, ed) in plans.items():
        edition_doc = json.loads(
            (OUT / f"{ed}.signature-edition.json").read_text(encoding="utf-8")
        )
        verdict = replay_plan(plan, edition_doc)
        assert verdict["valid"], verdict["problems"]
        print(f"  库内复读 {name}: valid={verdict['valid']} "
              f"total_cost={verdict['total_cost']}")

    print("\n  通过 CLI 离线复读 (现场只带走当班材料):")
    for name in ("WO-1", "WO-4", "WO-5", "WO-6"):
        ed = plans[name][1]
        out = subprocess.run(
            [sys.executable, "-m", "citation_system", "replay-plan",
             "--plan", str(OUT / f"{name}.press-plan.json"),
             "--edition", str(OUT / f"{ed}.signature-edition.json")],
            capture_output=True, text=True, cwd=ROOT,
        )
        verdict = json.loads(out.stdout)
        print(f"    {name}: exit={out.returncode} valid={verdict['valid']}")
        assert out.returncode == 0 and verdict["valid"]

    print(f"\n交付材料已写入 {OUT.relative_to(ROOT)}/")
    for path in sorted(OUT.iterdir()):
        print(f"  {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
