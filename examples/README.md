# 示例

先启动服务（首次建议清空数据卷以获得确定性输出）：

```bash
docker compose down -v
docker compose up -d --build
```

所有脚本用 `BASE` 环境变量定位 API（默认 `http://localhost:8000`），依赖 `curl` 与 `python3`。

> 确定性说明：示例按 01 → 02 → 03 的顺序在**清空数据卷后**运行时，文中期望值（1300、2299、100、255、7、77……）严格成立。重复运行或跨示例混跑时，"同一运行内金额一致"的断言始终成立，但具体数值可能因既有批次而不同。

## 01 · 迟到更正（`01_late_corrections.sh`）

时间线（`T = 2026-11-15T12:00:00Z`）：

1. 导入基础修订 `data/revisions_base.csv`（T 之前已可见），提交样本 `s1(A@T)`、`s2(B@T)`，创建运行 1：
   - `s1 = 1300`（p1 的 1000 + p4 的 300；p2 发生在 T-25h，在窗口外）
   - `s2 = 700`
2. 导入迟到修订 `data/revisions_late.csv`（多数 `visible_at = T+1h`）：
   - `p1#2`：更正把账户 A→C
   - `p4#2`：撤销（`revoked=true`）
   - `p2#2`：更正把发生时刻从 T-25h 移入窗口（T-5h）
   - `p5#1`：晚到的新支付
   - `p6#1`：**回填**型修订——`visible_at = T-5h`（T 时本可知，只是迟到入库）
3. 提交 `s3(A@T+2h)`、`s4(C@T+2h)`、`s5(A@T)`，创建运行 2（其修订水位线已包含迟到批次）：
   - `s3 = 6249`（5000 + 250 + 999；p1 已归 C、p4 已撤销）
   - `s4 = 1000`（p1 更正后属于 C）
   - `s5 = 2299`：与 s1 同一预测时刻，仍**不包含** T+1h 才可见的更正，但包含回填的 p6 → 1300 + 999
4. 运行 1 的结果集不可变：`s1` 在其中永远是 1300。

## 02 · 并发补传 / 修订水位线 / 幂等 / 冲突（`02_concurrent_uploads.sh`）

1. 先导入 `data/revisions_ex2_base.csv`（ex2-p1 = 100，T 前可见），上传 5000 个相同样本（同账户、同预测时刻）并创建运行 1。
2. **运行进行中**再上传 50 个样本——受理成功（201）但不计入本次运行（样本水位线固定）。
3. **运行进行中**上传 `data/revisions_ex2_backdated.csv`：`ex2-p1#2`（金额更正为 250）与 `ex2-p2`（5），两者的 `visible_at` 都在样本预测时刻**之前**（回填过去可见）。受理成功（201）——**补传不必等训练结束**——但运行 1 的修订范围在创建时已冻结，这 5000 个相同样本的金额**全部为 100**，无一被中途改写（这正是被修复的缺陷：此前同一运行中相同（账户， 预测时刻）的样本会因中途补传而得到不同金额）。
4. 原样重传 `revisions_base.csv` → 全部 `rows_reused`（幂等复用）。
5. 上传 `revisions_conflict.csv`（`p1#1` 金额被改）→ HTTP 409，**整批拒绝**，其中的新支付 `p9` 也不落库。
6. 运行 1 完成后的数据集不含 `ex2-extra-*`；创建运行 2 后包含这 50 个样本，且 ex2 样本金额统一变为 **255**（250 + 5）——后来更正不改变旧任务结果，新任务可纳入更正。

提示：想放大可观察窗口，用 `RUN_CHUNK_DELAY_MS=50 docker compose up -d` 放慢 worker。

## 03 · 重启接续（`03_restart_resume.sh`）

1. 先导入 `data/revisions_ex3_base.csv`（ex3-p1 = 7），上传 20000 个样本并创建运行，轮询到部分完成（`done_samples` 介于 0 与 total 之间）。
2. `docker compose restart app`。
3. 重启后 `done_samples` **不回退**（从已完成样本接续，已完成样本不重算）。
4. 重启后立即上传回填更正 `data/revisions_ex3_backdated.csv`（ex3-p1#2 = 77，`visible_at` 在预测时刻之前）：续算的运行**仍沿用原修订范围**，全部 20000 个样本金额一致为 **7**——重启不混用口径。
5. 新建运行纳入该更正：ex3 样本统一为 **77**。

提示：worker 默认很快，20000 样本可能几秒内跑完；用 `RUN_CHUNK_DELAY_MS=100 docker compose up -d` 让中断窗口清晰可见。若脚本提示"run completed before restart"，按此调整即可。

## 数据文件

| 文件 | 内容 |
|---|---|
| `data/revisions_base.csv` | 4 条基础修订（p1–p4，T 前可见） |
| `data/revisions_late.csv` | 5 条迟到修订（更正账户/时刻、撤销、晚到、回填） |
| `data/revisions_conflict.csv` | 与既有 `p1#1` 内容冲突的批次（含一条新行 p9，用于证明整批回滚） |
| `data/revisions_ex2_base.csv` / `data/revisions_ex2_backdated.csv` | 示例 02：基础修订（100）与回填批次（更正 250 + 新支付 5，均过去可见） |
| `data/revisions_ex3_base.csv` / `data/revisions_ex3_backdated.csv` | 示例 03：基础修订（7）与重启后上传的回填更正（77，过去可见） |
| `data/samples_1.csv` / `data/samples_2.csv` | 示例 01 的样本 |
