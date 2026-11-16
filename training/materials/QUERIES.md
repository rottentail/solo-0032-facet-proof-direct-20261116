# 证据查询命令（QUERIES）

以下命令用于查询培训场景留下的失败与历史证据。先重启服务（数据根即培训数据）：

```sh
python3 -m facetproof --root training/data --host 127.0.0.1 --port 8796
```

下文用 `$BASE` 表示 `http://127.0.0.1:8796/facet/v1`。具体对象标识见 `../catalog/catalog.json`
（`figures[].source`）与 `../catalog/failures.json`；下面以第 4 轮失败曝光为例，记其
`exposure_id` 为 `$EXP`，任一份成功图版为 `$PLATE`，审阅为 `$PROOF`。

## 失败证据

```sh
# 失败曝光的终态、错误码与尝试次数（state=failed, error=capacity, attempt=2）
curl -s "$BASE/exposures/$EXP"

# 失败曝光的完整事件流：queued → claimed → failed → retried → claimed → failed
curl -s "$BASE/events/$EXP/history"

# 画布额度：全部终态后预留应归零（reserved=0）
curl -s "$BASE/resources"
```

## 历史与来源证据

```sh
# 任一对象（装配、网格、材质、审阅）的事件历史
curl -s "$BASE/events/<对象id>/history"

# 冻结快照的闭包：可见网格/材质及其修订号的副本
curl -s "$BASE/seals/<快照id>"

# 图版通道核验（摘要与尺寸逐一核对）
curl -s -X POST "$BASE/plates/$PLATE/verify"

# 审阅记录（含 visual_hash 与 seal_id 的固化）
curl -s "$BASE/proofs/$PROOF"

# 像素拾取：物体编号与摄像机前向距离
curl -s "$BASE/plates/$PLATE/pick?x=64&y=64"
```

## 幂等与轮次隔离证据

```sh
# 同键同请求重发：返回原曝光，不新建（对比响应 id 与 catalog.json 中一致）
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"key":"train-r1-overview","width":128,"height":128,"samples":2}' \
  "$BASE/seals/<基线快照id>/exposures"

# 跨轮次抢键：同一幂等键指向不同快照 → 409 key_conflict
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"key":"train-r1-overview","width":128,"height":128,"samples":2}' \
  "$BASE/seals/<修订快照id>/exposures"
```

## 离线核验（无需启动服务）

```sh
# 独立读取器核验交付包：有效退出 0，拒绝退出 2
python3 -m facetreader training/catalog/bundles/fig-01-overview-baseline.zip --output /tmp/received-fig01

# 直接检索文本证据
python3 -m json.tool training/catalog/failures.json
grep -l '"failed"' training/catalog/evidence/exposure-*.json
```
