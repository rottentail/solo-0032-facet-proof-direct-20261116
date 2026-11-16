# 逐图核验目录

第 3 次运行生成。机读版：`catalog.json`；失败汇总：`failures.json`；证据原件：`evidence/`。

## 核验总表

| 图 | 轮次 | 状态 | 图版核验 | 审阅 | 独立读取器 | 关键结论 |
| --- | --- | --- | --- | --- | --- | --- |
| fig-01-overview-baseline | 1 | ready | valid | accepted | valid | cmp-reseal-overview: 颜色差异 0；cmp-revised-vs-baseline: 颜色差异 1538；cmp-after-retire: 颜色差异 0；cmp-final-overview: 颜色差异 0 |
| fig-02-panel-baseline | 1 | ready | valid | accepted | valid | cmp-reseal-panel: 颜色差异 0 |
| fig-03-strut-baseline | 1 | ready | valid | accepted | valid | — |
| fig-04-overview-reseal | 2 | ready | valid | accepted | valid | cmp-reseal-overview: 颜色差异 0 |
| fig-05-panel-reseal | 2 | ready | valid | accepted | valid | cmp-reseal-panel: 颜色差异 0 |
| fig-06-overview-revised | 2 | ready | valid | accepted | valid | cmp-revised-vs-baseline: 颜色差异 1538 |
| fig-07-overview-after-retire | 3 | ready | valid | accepted | valid | cmp-after-retire: 颜色差异 0 |
| fig-08-strut-new | 3 | ready | valid | accepted | valid | cmp-final-strut: 颜色差异 0 |
| fig-09-deck-layout | 4 | **failed** | — | — | — | capacity（尝试 2 次） |
| fig-10-overview-final | 4 | ready | valid | accepted | valid | cmp-final-overview: 颜色差异 0 |
| fig-11-strut-final | 4 | ready | valid | accepted | valid | cmp-final-strut: 颜色差异 0 |

## 失败与拒绝证据

- `fig-09-deck-layout`：曝光 `exposure_67bafd0cfa9a418d87fcfc3e4a329f6b` 终态 failed（capacity），证据 catalog/evidence/exposure-train-r4-deck.json、catalog/evidence/events-exposure_67bafd0cfa9a418d87fcfc3e4a329f6b.json
- 拒绝：冻结引用已停用网格的草稿（面板新方案） → retired （HTTP 409）
- 拒绝：在新草稿中放置已停用网格 → retired （HTTP 409）

## 轮次隔离与重复运行

- 同键重发归并：`train-r1-overview` 重发返回原曝光（same_exposure=true）
- 跨轮次抢键被拒：key_conflict
- 跨运行稳定性：第 3 次运行与既有记录完全一致：曝光、图版、visual_hash 均未漂移，未另起来源。
