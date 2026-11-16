# Facet Proof 共享资源使用培训

本目录是用 Facet Proof 实际制作的共享资源培训材料，面向需要理解"多份说明图复用同一批网格与材质时，上游变更到底影响哪些图"的操作者。全部内容由 `materials/build_atlas.py` 通过真实 HTTP 请求驱动服务生成，可重复执行、可逐图核验。

## 培训场景

三份共享网格（斜翼面板、三角支架、格栅甲板）与两种共享材质（釉青、赭橙）被多份装配复用，分五轮演示：

| 轮次 | 操作 | 展示的结论 |
| --- | --- | --- |
| 1 | 三份装配复用共享资源，冻结并渲染基线图 | 冻结快照复制可见网格与材质闭包 |
| 2 | 修订共享材质「釉青」 | 旧快照重渲逐像素一致（旧图可复现）；只有新冻结体现新材质 |
| 3 | 停用共享网格「斜翼面板」 | 引用停用网格的新冻结被拒绝（409 retired）；旧快照重渲不受影响 |
| 4 | 批量提交三份曝光，一份超限 | 失败项终态 failed(capacity)，重试仍失败；同批成功图不掩盖失败 |
| 5 | 同键重发与跨轮次抢键 | 同键同请求归并到原曝光；跨轮次抢键被 409 key_conflict 拒绝 |

## 目录结构

- `atlas/` — **培训图册**：`ATLAS.md` 含共享资源清单、事件影响矩阵与逐图说明；每图一个目录，存 color/depth/object 三个通道的 PNG；失败图以 `FAILED.md` 占位说明。
- `catalog/` — **逐图核验目录**：`CATALOG.md`（人读）与 `catalog.json`（机读）记录每图的来源（快照内容摘要、曝光键、图版 visual_hash）与核验结果（图版核验、审阅、facetreader 独立核验、比对结论）；`failures.json` 汇总失败；`evidence/` 存服务侧证据原件（曝光状态、事件流、比对、拒绝、快照闭包等）；`bundles/` 为每图交付 ZIP；`received/` 为 facetreader 离线核验输出。
- `materials/` — **操作素材**：`build_atlas.py` 制作脚本、`state.json`（跨运行的资源锚定）、`transcript-run-N.jsonl`（第 N 次运行的完整 HTTP 记录）、`server-run-N.log`、`QUERIES.md`（证据查询命令）。
- `data/` — 服务数据根（SQLite + 内容寻址图版）。属可再生成的运行产物，未纳入版本控制；删除后重跑脚本会重建全部场景（对象标识将重新随机生成，目录文件会同步刷新）。

## 如何复现与重跑

```sh
python3 training/materials/build_atlas.py
```

脚本幂等：资源由 `materials/state.json` 锚定，曝光由幂等键锚定。重复运行不会产生重复图版；每次运行结束会把本次的曝光/图版/visual_hash 与既有记录逐一比对，任何漂移都会让脚本以非零状态失败（见 `catalog.json` 的 `rerun_stability`）。完全重来：删除 `training/data/` 与 `materials/state.json` 后再运行。

## 如何查询失败与历史证据

失败与历史不是只写在文档里，而是可查询的事实：

1. 文本证据：直接检索 `catalog/failures.json`、`catalog/evidence/`（如 `events-<曝光id>.json` 记录 queued→claimed→failed→retried→failed 的完整事件流）。
2. 在线查询：重启服务后按 `materials/QUERIES.md` 中的 curl 命令查询曝光终态、事件历史、图版核验、审阅状态。
3. 离线核验：`python3 -m facetreader catalog/bundles/<图>.zip` 独立校验交付包（不依赖服务私有代码）。

## 使用边界

- 本场景为本地受信任环境设计，服务无账户与认证机制。
- 旧图可复现指"同一冻结快照 + 同一渲染请求"的重渲逐像素一致；它不证明当前审阅意见仍有效（审阅可能被后续隔离操作失效）。
- 第 4 轮的失败是教学用例：冻结合法、渲染超 40000 源面额度。它说明"已接受的曝光仍可能失败"，失败终态与成功终态一样必须进入目录。
