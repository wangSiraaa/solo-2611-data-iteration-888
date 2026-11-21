# 案卷页序目录与引用数据仓（task 726）

独立的卷内页序目录 + 摘录引用仓，输出可被只读客户端直接消费的
`citation-product/1` JSONL。不解析 PDF 或图像；不依赖旧后端。

只提供输入与只读客户端时也能完整运行：`materials/726/` 下的
`pages.json`、`references.jsonl`、`operations.jsonl` 是全部初始数据。

再版工作室扩展：在冻结目录之上提供**书帖版**（`signature-edition/1`）与
**勘误工单/开机计划**（`errata-work-order/1` → `press-plan/1`），见下文
对应章节。原 PINNED、RANGE 与 `citation-product/1` 的用法不变。

## 快速开始

```bash
# 1) 建目录 → 建引用 → 应用操作 → 导出（与 materials 的真实输入一致）
python3 -m citation_system init --db work/cli_run/catalog.db \
    --pages materials/726/pages.json --force
python3 -m citation_system add-references --db work/cli_run/catalog.db \
    --references materials/726/references.jsonl
python3 -m citation_system apply-operations --db work/cli_run/catalog.db \
    --operations materials/726/operations.jsonl
python3 -m citation_system export --db work/cli_run/catalog.db \
    --usable exports/cli_run/usable.jsonl \
    --needs-attention exports/cli_run/needs_attention.jsonl

# 2) 只读客户端只读 JSONL，不打开数据库
python3 materials/726/citation_client.py \
    exports/cli_run/usable.jsonl exports/cli_run/needs_attention.jsonl
```

一次跑完全部展示场景（含保留的旧导出）：

```bash
python3 scripts/demo_726.py     # 写 exports/scenario/，每步一个目录，互不覆盖
python3 -m unittest tests.test_citation_system -v
python3 scripts/benchmark_726.py  # 20 卷 × 200 页 × 500 引用
```

`exports/scenario/00_initial/` 与随题的 `example_usable.jsonl` **逐字节一致**；
客户端对它的输出为：

```
C_PIN usable V1/p2,V1/p3 -
C_RANGE usable V1/p2,V1/p3,V1/p4 -
```

## 目录（`citation_system/catalog.py`）

- **身份**：页的完整身份是 `(volume_id, page_id)`。`page_id` 只在卷内唯一；
  `V1/p1` 与 `V2/p1` 是不同页。`page_id` 一旦分配，插页/移位/停用都**不重发**，
  插入已存在的 `page_id` 会以 `duplicate_page_id` 整批拒绝。
- **物理次序**：每卷内 `seq` 连续 `0..n-1`，**同时**覆盖在用与停用页；
  停用只改状态，不删除、不改位置、不重排号。`UNIQUE(volume_id, seq)` 在
  任何单条 UPDATE 后都成立（右移从高到低，左移从低到高，先挪到临时槽）。
- **显示页码**：`label` 一律字符串，`"3"`、`"03"`、`"2a"` 各不相同，不做
  数值化或前缀匹配。
- **解析**：`resolve(volume, section, label)` 只在**在用**页里找，必须恰好
  一个：0 个 → `resolution_not_found`，多个 → `resolution_ambiguous`
  （附 `matching_page_ids`）。正文 `body/3` 与附录 `appendix/3` 因此不混淆。
- **查询**：当前前后页 `neighbours`（可按 `active_only` 跳过停用页，但返回
  的仍是真实页身份）；有序区间 `ordered_range`（含端，可选排除停用）。
- **操作**：`insert` / `insert_many` / `deactivate` / `activate` / `move` /
  `move_many` / `reorder`，每个请求是一个原子批次。`{"batch_id", "operations"}`
  把多个操作合成一个批次；**任一失败则整批回滚**，包括本来合法的兄弟操作。
- **同卷限制**：`move` 带 `target_volume_id` 且与源卷不同 → `cross_volume_move`；
  锚点页只存在于另一卷 → `cross_volume_anchor`。两种都在写之前拒绝。
- **最终次序**：`reorder` 必须列出该卷全部页（含停用页）且恰好一次，
  可同时改变次序与状态；位置不复用、不重复。
- **操作日志**：每个请求（含被拒绝的）都记录 `status`、`error_code`、
  `catalog_revision`、`expect`/`expectation_met`，可审计。
- **目录独立**：没有任何摘录时目录照常建、改、查。

## 引用仓（`citation_system/repository.py`）

- **创建时固定身份**：接受 `references.jsonl` 的两种写法：
  - 直接给身份：PINNED `page_ids` / RANGE `start_page_id`,`end_page_id`；
  - 给 `(section,label)`（PINNED 用 `items`，RANGE 用 `start_*`/`end_*`）：
    创建时解析一次，之后**只存实际页身份**，读取时不再重解文字。
  - 解析歧义/未找到时**什么都不写**。
- **PINNED**：固定成员集合，无顺序语义。后续插页/移位/重排都不改成员。
  成员停用后仍是成员，整体转入待处理并标明 `active:false`；**绝不静默删成员**。
- **RANGE**：固定同卷两个端页身份。查询时取**当前次序**两端之间（含端）的
  **在用页**，所以中间插页会真实进入结果、中间页停用会被排除。
  任一端停用或先后反转 → 待处理；`source` 与 `pages` 都保持原端点顺序，
  **不交换、不猜测**。
- **重开**：端点重新启用 / 移回原序后，同一引用无需重建即恢复可用。
- **反查**：`citations_referencing(volume, page)` 按实际身份返回 PINNED
  成员与当前 RANGE 覆盖（`member`/`start`/`end`/`interior`）。

## 导出（`citation_system/export.py`）

- 在**一个只读事务**里取完整已提交快照（revision、全部页、全部引用、成员、
  端点），之后只从该快照构造产品。因此一条引用的页列表**不可能拼自两个
  状态**，所有行的 `catalog_revision` 相同。
- `usable.jsonl`：完整可用引用；`reason` 为 `null`；RANGE 的 `pages` 按当前
  卷内次序。
- `needs_attention.jsonl`：端点失效/反转的 RANGE，或含停用固定页的 PINNED。
  保留 `citation_id`、`source`（原卷/原端点或原成员）、`pages`（完整固定集合
  或两个已知端页及状态）、稳定 `reason`，并附 `detail`。
  `reason` 取值：`pinned_inactive_member`、`range_endpoint_inactive`、
  `range_endpoints_reversed`。
- **每个引用恰好出现在一个文件**；PINNED 含停用页时整体进待处理文件，
  不导出其余页冒充完整引用。
- 写出前用 `validate_products` 校验合同；两文件先写临时文件再 `os.replace`，
  单个文件不会半写。若需两文件成对切换，请以**整个导出目录**为单位发布。

## 书帖版（`citation_system/imposition.py`）

书帖版把**一卷的一次完整目录修订**冻结成可开机的版面：

- 建版时取该卷**在用页**按物理次序，每 4 页或 8 页起一个独立书帖，
  末帖尾部补空位；空位是真空格位（`page_id: null`），**不伪造页**。
- 一帖 N 个位置中，第 i 张纸（从外向内，`0 ≤ i < N/4`）正面印
  `(N−2i, 1+2i)`，背面印 `(2+2i, N−1−2i)`。八页一帖时 p2、p7 因此同在
  外纸背面（`sig0/sheet0/back`）。每个**纸面**含两个**格位**，稳定名为
  `sig{k}/sheet{i}/{front|back}`。
- 位置映回原卷稳定页 ID 与所绑定的 `catalog_revision`；版成之后目录再变
  （插页/移位/停用）都不改写旧版。`--expected-revision` 与当前修订不一致
  时整版不建（`catalog_revision_mismatch`），保证绑的是一次完整修订。
- 新版可改帖长（4↔8）或绑定另一（更晚的）目录修订；旧版始终可查、可
  **复印**：`copy-edition` 复制同一冻结版面到新版号，`layout_digest` 相同。
- `locate-page` 独立给出某页的纸、面、格位；`export-edition` 产出
  自包含的 `signature-edition/1` 文档（含全部格位与空位），供离线复读。

## 勘误工单与开机计划（`citation_system/press.py`）

印厂按校样轮次开机。工单（`errata-work-order/1`）给出校样版号、架位容量、
初始在架表、各面标定装版工时、保留令和逐轮摘录组：

- 每轮的摘录组按 `citation-product/1` 的语义**整组解析**（同一快照）；
  任一摘录待处理/不存在，或用到的页不在校样版内，该轮即不可完成。
  解析成功的组把实际用到的页映成**所需纸面**；同一面只占一个架位。
- 已装的面可跨轮留架（免费）；轮后可撤下未被保留令钉住的面；保留令
  指定某面 `[from_round, to_round]` 全程在架。初装与重装都按该面标定
  工时收费。
- 精确子集 DP 求**最小总装版工时**（装早不会更省，故最优计划只在
  需要时装所需面；同费时倾向迟撤，确定性可复现）。无解时给出**首个
  不可完成轮**及原因（`capacity_exceeded` / `pinned_inactive_member` /
  `range_endpoint_inactive` / `range_endpoints_reversed` /
  `citation_not_found` / `page_not_in_edition`）。
- 单班上界：14 面、4 架位、40 轮（超出报 `shift_limits_exceeded`；
  该规模下求解约毫秒级）。
- 签署计划（`press-plan/1`）记录校样版绑定、初始在架、保留令、逐轮
  在架/装拆与费用依据，并附 sha256 `plan_digest`。配合导出的
  `signature-edition/1`，`replay-plan` **完全离线**复读：重推各轮所需
  纸面、模拟装拆与容量/保留令、重算最优费用并校验摘要——现场只带走
  当班材料即可执行；旧计划在目录继续变动后仍可按交付文件复查。

```bash
python3 -m citation_system make-edition --db work/cli_run/catalog.db \
    --volume V1 --signature-size 8 --edition-id ED1
python3 -m citation_system export-edition --db work/cli_run/catalog.db \
    --edition ED1 --out exports/press/ED1.signature-edition.json
python3 -m citation_system plan-press --db work/cli_run/catalog.db \
    --order order.json --out exports/press/WO-1.press-plan.json
python3 -m citation_system replay-plan \
    --plan exports/press/WO-1.press-plan.json \
    --edition exports/press/ED1.signature-edition.json   # 不需要数据库
```

完整演示（含题目全部算例：p2/p7 同面、p6/p7 互换后容量 1 无解/2 位费 2、
A-B-C-A 无保留令 7、加保留令 12、补空、范围端停用、复印与旧计划复查）：

```bash
python3 scripts/demo_press.py   # 交付物写入 exports/press/
```

## 依赖索引（`citation_system/dependency.py`，可选）
- 目的：加速"某实际页被哪些引用覆盖"。
- 表 `page_citation_dependency(volume_id, page_id, citation_id, role)`。
- **从不用于导出**，只用于查询，所以索引陈旧不会污染产品。
- 带 `catalog_revision` 戳；任何目录变更（含同卷移位）都会使戳不匹配，
  下次查询时惰性整表重建。PINNED 行与次序无关，RANGE 行随次序变化；
  为简单起见整表重建。
- 成本（实测，见 `scripts/benchmark_726.py`）：20 卷 × 200 页 × 500 引用时
  重建约 35 ms、约 1.7 万行；单次反查中位数：直查 ~0.25 ms，
  走索引 ~0.18 ms。规模更小时直查已足够，索引只是可选加速。
  同卷移位本身 ~0.6 ms，重导出 ~18 ms。

## 规模与性能

`scripts/benchmark_726.py` 在目标规模（20 卷、每卷 200 页、500 引用）测得：

| 操作 | 耗时 |
| --- | --- |
| 载入目录 | ~8 ms |
| 创建 500 条引用 | ~31 ms（~0.06 ms/条） |
| 一次性导出 | ~19 ms |
| 同卷移位 | ~0.6 ms |
| 移位后重导出 | ~18 ms |
| 依赖索引重建 | ~35 ms |

## 布局

```
citation_system/   实现源码（catalog / repository / export / dependency /
                   imposition / press / cli）
tests/             89 个单元与集成测试（含随机不变量与目标规模）
scripts/demo_726.py        端到端展示，逐步保留旧导出
scripts/demo_press.py      书帖版 + 勘误工单算例演示，交付物可离线复读
scripts/benchmark_726.py   目标规模基准
exports/scenario/  演示产生的各步骤导出（旧目录保留）
exports/cli_run/   按 materials 原始输入走完整 CLI 的导出
exports/press/     书帖版文档与签署开机计划（可离线复读）
work/              运行的 SQLite 数据库
```

## 不做什么

不 OCR、不做自然语言定位、不做版式编辑；不解析 PDF 图像；不读取图片内容。
