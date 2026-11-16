#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Facet Proof 共享资源培训图册制作脚本。

仅使用 Python 标准库，通过真实 HTTP 请求驱动本仓库的 Facet Proof 服务，
实际建立多份复用网格与材质的装配，分五轮演示共享资源的影响面：

  第 1 轮  基线：三份装配复用同一批共享网格/材质，冻结并渲染基线说明图；
  第 2 轮  材质修订：旧快照重渲逐像素一致（旧图可复现），新冻结才体现新材质；
  第 3 轮  网格停用：引用停用网格的新冻结被拒绝，旧快照重渲不受影响；
  第 4 轮  批量制作：一份超限曝光失败（capacity），同批成功图不掩盖失败；
  第 5 轮  轮次隔离：同键重发返回原曝光，跨轮次抢键被 key_conflict 拒绝。

脚本可重复执行：资源由 materials/state.json 锚定，曝光由幂等键锚定；
重复运行不产生重复图版，并在目录中记录跨运行稳定性核验结果。

用法：python3 training/materials/build_atlas.py [--port 8876]
"""
import argparse
import hashlib
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TRAINING = REPO / "training"
MATERIALS = TRAINING / "materials"
ATLAS = TRAINING / "atlas"
CATALOG = TRAINING / "catalog"
EVIDENCE = CATALOG / "evidence"
BUNDLES = CATALOG / "bundles"
RECEIVED = CATALOG / "received"
DATA = TRAINING / "data"
STATE_PATH = MATERIALS / "state.json"

# 全部说明图使用同一渲染请求，保证跨轮次比对的前提一致。
REQUEST = {"width": 128, "height": 128, "samples": 2}


class BuildFailure(Exception):
    """制作脚本断言失败：立即中止并以非零状态退出，不允许被后续成功掩盖。"""


def check(condition, message):
    if not condition:
        raise BuildFailure(message)


# ---------------------------------------------------------------------------
# HTTP 客户端：逐条请求/响应记入本轮 transcript，二进制与大响应只记摘要。
# ---------------------------------------------------------------------------
class FaultError(Exception):
    def __init__(self, status, code, detail):
        super().__init__(f"{status} {code}: {detail}")
        self.status, self.code, self.detail = status, code, detail


class Client:
    def __init__(self, base, transcript):
        self.base = base
        self.transcript = transcript
        self.seq = 0
        self.round_tag = "setup"

    @staticmethod
    def _shrunk(value):
        text = json.dumps(value, ensure_ascii=False)
        if len(text) > 2000:
            raw = text.encode("utf-8")
            return {"truncated": True, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        return value

    def _record(self, method, path, body, status, payload):
        if isinstance(payload, bytes):
            shown = {"binary": {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}}
        else:
            shown = self._shrunk(payload)
        self.transcript.write(json.dumps({
            "seq": self.seq, "round": self.round_tag, "method": method, "path": path,
            "request": self._shrunk(body) if body is not None else None,
            "status": status, "response": shown}, ensure_ascii=False, sort_keys=True) + "\n")
        self.transcript.flush()

    def call(self, method, path, body=None):
        self.seq += 1
        raw_body = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=raw_body, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                status = response.status
                mime = response.headers.get_content_type()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status, mime = exc.code, "application/json"
        payload = raw if mime != "application/json" else json.loads(raw.decode("utf-8"))
        self._record(method, path, body, status, payload)
        if status >= 400:
            raise FaultError(status, payload.get("error", "unknown"), payload.get("detail", ""))
        return payload

    def expect_fault(self, method, path, body, code):
        """断言一次操作被指定错误码拒绝，并把拒绝事实作为证据返回。"""
        try:
            self.call(method, path, body)
        except FaultError as exc:
            check(exc.code == code, f"期望拒绝码 {code}，实际 {exc.code}（{method} {path}）")
            return {"code": exc.code, "detail": exc.detail, "http_status": exc.status}
        raise BuildFailure(f"期望 {method} {path} 被拒绝（{code}），但实际成功了")


# ---------------------------------------------------------------------------
# 持久状态：跨运行锚定资源标识，保证重复运行不另起来源。
# ---------------------------------------------------------------------------
class State:
    def __init__(self, path):
        self.path = path
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.data = {"run_seq": 0, "resources": {}, "assemblies": {}, "seals": {},
                         "exposures": {}, "plates": {}, "proofs": {}, "comparisons": {},
                         "flags": {}, "fingerprints": {}}

    def save(self):
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2,
                                        sort_keys=True) + "\n", encoding="utf-8")


CLIENT = None
STATE = None


# ---------------------------------------------------------------------------
# 共享资源与装配的几何定义。
# ---------------------------------------------------------------------------
PANEL_MESH = {"name": "斜翼面板",
              "vertices": [[-1, -1, 0], [1, -1, 0], [1, 1, 0], [-1, 1, 0], [0, 0, 1.2]],
              "faces": [[0, 1, 4], [1, 2, 4], [2, 3, 4], [3, 0, 4], [0, 3, 2], [0, 2, 1]]}
STRUT_MESH = {"name": "三角支架",
              "vertices": [[0, 0, 0], [1.4, 0, 0], [0, 1.4, 0], [0, 0, 0.9], [1.4, 0, 0.9], [0, 1.4, 0.9]],
              "faces": [[0, 2, 1], [3, 4, 5], [0, 1, 4], [0, 4, 3], [1, 2, 5], [1, 5, 4], [2, 0, 3], [2, 3, 5]]}


def deck_mesh():
    """33×33 格栅：2048 个三角面。单份合法，21 份布置合计 43008 面，超出 40000 面渲染上限。"""
    side = 33
    vertices = [[round(-2.2 + 4.4 * i / (side - 1), 6), round(-2.2 + 4.4 * j / (side - 1), 6), 0]
                for j in range(side) for i in range(side)]
    faces = []
    for j in range(side - 1):
        for i in range(side - 1):
            a = j * side + i
            faces.append([a, a + 1, a + side + 1])
            faces.append([a, a + side + 1, a + side])
    return {"name": "格栅甲板", "vertices": vertices, "faces": faces}


TEAL = {"name": "釉青", "color": [0.12, 0.62, 0.61], "two_sided": True}
TEAL_REVISED = {"name": "釉青", "color": [0.16, 0.32, 0.85], "two_sided": True}
AMBER = {"name": "赭橙", "color": [0.85, 0.45, 0.15], "two_sided": True}


def deck_placements(mesh_id, pigment_id):
    return [{"key": f"deck-{n:02d}", "mesh_id": mesh_id, "pigment_id": pigment_id,
             "transform": {"translation": [(n % 7 - 3) * 4.6, (n // 7 - 1) * 4.6, 0]}}
            for n in range(21)]


def assembly_specs(ids):
    panel, strut, deck = ids["mesh.panel"], ids["mesh.strut"], ids["mesh.deck"]
    teal, amber = ids["pigment.teal"], ids["pigment.amber"]
    return {
        "overview": {
            "create": {"name": "总装说明图", "lens": {"eye": [3.4, 2.6, 5.4], "target": [0, 0, 0.3]}},
            "placements": [
                {"key": "panel", "mesh_id": panel, "pigment_id": teal,
                 "transform": {"translation": [-1.3, -0.2, 0], "euler": [0, 25, 0]}},
                {"key": "strut", "mesh_id": strut, "pigment_id": amber,
                 "transform": {"translation": [0.8, -0.7, 0]}}]},
        "panel": {
            "create": {"name": "面板特写", "lens": {"eye": [0.4, 0.6, 4.5], "target": [0, 0, 0.4],
                                                    "projection": "orthographic", "span": 3.4}},
            "placements": [{"key": "panel", "mesh_id": panel, "pigment_id": teal,
                            "transform": {"euler": [0, 25, 0]}}]},
        "strut": {
            "create": {"name": "支架特写", "lens": {"eye": [2.8, 2.2, 3.4], "target": [0.5, 0.5, 0.3]}},
            "placements": [{"key": "strut", "mesh_id": strut, "pigment_id": amber, "transform": {}}]},
        "panel-v2": {
            "create": {"name": "面板新方案", "lens": {"eye": [3, 2, 4]}},
            "placements": [{"key": "panel", "mesh_id": panel, "pigment_id": teal,
                            "transform": {"euler": [0, -15, 0]}}]},
        "strut-v2": {
            "create": {"name": "支架说明图（新）", "lens": {"eye": [2.6, 2.0, 3.0], "target": [0.4, 0.4, 0.3]}},
            "placements": [{"key": "strut", "mesh_id": strut, "pigment_id": amber,
                            "transform": {"translation": [-0.7, -0.7, 0]}}]},
        "deck": {
            "create": {"name": "整舰布局图", "lens": {"eye": [0, -26, 16], "target": [0, 0, 0], "fov": 50}},
            "placements": deck_placements(deck, amber)},
        "scratch": {
            "create": {"name": "停用网格验证用草稿", "lens": {"eye": [3, 2, 4]}},
            "placements": None},
    }


# ---------------------------------------------------------------------------
# 说明图计划：slug、轮次、快照、幂等键与教学要点。
# ---------------------------------------------------------------------------
FIGURES = [
    {"slug": "fig-01-overview-baseline", "round": 1, "seal": "overview", "key": "train-r1-overview",
     "title": "总装说明图（基线）",
     "lesson": "装配复用共享网格「斜翼面板」（釉青）与「三角支架」（赭橙）的首次冻结渲染。"},
    {"slug": "fig-02-panel-baseline", "round": 1, "seal": "panel", "key": "train-r1-panel",
     "title": "面板特写（基线）",
     "lesson": "同一共享网格「斜翼面板」与材质「釉青」在第二份装配（正交特写）中的复用。"},
    {"slug": "fig-03-strut-baseline", "round": 1, "seal": "strut", "key": "train-r1-strut",
     "title": "支架特写（基线）",
     "lesson": "共享网格「三角支架」与材质「赭橙」在第二份装配中的复用。"},
    {"slug": "fig-04-overview-reseal", "round": 2, "seal": "overview", "key": "train-r2-overview-reseal",
     "title": "总装说明图（材质修订后·旧快照重渲）", "identical_to": "fig-01-overview-baseline",
     "lesson": "材质「釉青」修订后重渲旧快照：与 fig-01 逐像素一致——旧图可复现。"},
    {"slug": "fig-05-panel-reseal", "round": 2, "seal": "panel", "key": "train-r2-panel-reseal",
     "title": "面板特写（材质修订后·旧快照重渲）", "identical_to": "fig-02-panel-baseline",
     "lesson": "旧快照的冻结闭包保留了材质副本，上游修订不影响已冻结说明图。"},
    {"slug": "fig-06-overview-revised", "round": 2, "seal": "overview-revised", "key": "train-r2-overview-revised",
     "title": "总装说明图（材质修订后·新冻结）", "differs_from": "fig-01-overview-baseline",
     "lesson": "派生装配在材质修订后重新冻结：几何不变、面板颜色改变——材质修订只影响新冻结。"},
    {"slug": "fig-07-overview-after-retire", "round": 3, "seal": "overview", "key": "train-r3-overview-after-retire",
     "title": "总装说明图（网格停用后·旧快照重渲）", "identical_to": "fig-01-overview-baseline",
     "lesson": "「斜翼面板」停用后旧快照仍可重渲且逐像素一致——停用只限制新冻结。"},
    {"slug": "fig-08-strut-new", "round": 3, "seal": "strut-v2", "key": "train-r3-strut-new",
     "title": "支架说明图（网格停用后·新冻结）",
     "lesson": "不引用停用网格的新装配照常冻结渲染——停用限制仅及于引用它的新冻结。"},
    {"slug": "fig-09-deck-layout", "round": 4, "seal": "deck", "key": "train-r4-deck",
     "title": "整舰布局图（批量·失败）", "expect_failure": "capacity",
     "lesson": "冻结成功但渲染超出 40000 源面额度：曝光终态 failed，重试仍失败，证据可查询。"},
    {"slug": "fig-10-overview-final", "round": 4, "seal": "overview", "key": "train-r4-overview-final",
     "title": "总装说明图（批量终版）", "identical_to": "fig-01-overview-baseline",
     "lesson": "与 fig-09 同批的成功项：旧快照再次重渲，仍与基线一致。"},
    {"slug": "fig-11-strut-final", "round": 4, "seal": "strut-v2", "key": "train-r4-strut-final",
     "title": "支架说明图（批量终版）", "identical_to": "fig-08-strut-new",
     "lesson": "同批成功项：新冻结快照的重渲，与 fig-08 一致。"},
]

COMPARISONS = [
    ("cmp-reseal-overview", "fig-04-overview-reseal", "fig-01-overview-baseline", "identical"),
    ("cmp-reseal-panel", "fig-05-panel-reseal", "fig-02-panel-baseline", "identical"),
    ("cmp-revised-vs-baseline", "fig-06-overview-revised", "fig-01-overview-baseline", "different"),
    ("cmp-after-retire", "fig-07-overview-after-retire", "fig-01-overview-baseline", "identical"),
    ("cmp-final-overview", "fig-10-overview-final", "fig-01-overview-baseline", "identical"),
    ("cmp-final-strut", "fig-11-strut-final", "fig-08-strut-new", "identical"),
]


# ---------------------------------------------------------------------------
# 资源、装配、快照、曝光的幂等确保操作。
# ---------------------------------------------------------------------------
def ensure_resource(kind, logical, spec):
    if logical in STATE.data["resources"]:
        obj = CLIENT.call("GET", f"/{kind}/{STATE.data['resources'][logical]}")
        check(obj["payload"]["name"] == spec["name"], f"{logical} 名称发生漂移")
        return obj
    obj = CLIENT.call("POST", f"/{kind}", spec)
    STATE.data["resources"][logical] = obj["id"]
    STATE.save()
    return obj


def ensure_assembly(name, spec):
    if name not in STATE.data["assemblies"]:
        created = CLIENT.call("POST", "/assemblies", spec["create"])
        STATE.data["assemblies"][name] = created["id"]
        STATE.save()
    assembly = CLIENT.call("GET", f"/assemblies/{STATE.data['assemblies'][name]}")
    if spec.get("placements") and assembly["state"] == "draft" and not assembly["payload"]["placements"]:
        CLIENT.call("PUT", f"/assemblies/{assembly['id']}",
                    {"revision": assembly["revision"], "placements": spec["placements"]})
        assembly = CLIENT.call("GET", f"/assemblies/{assembly['id']}")
    return assembly


def ensure_sealed(name):
    assembly = CLIENT.call("GET", f"/assemblies/{STATE.data['assemblies'][name]}")
    if name in STATE.data["seals"]:
        seal = CLIENT.call("GET", f"/seals/{STATE.data['seals'][name]['id']}")
        check(assembly["state"] == "sealed", f"装配 {name} 应已冻结")
        check(seal["payload"]["content_hash"] == STATE.data["seals"][name]["content_hash"],
              f"快照 {name} 的内容摘要跨运行漂移")
        return seal
    check(assembly["state"] == "draft", f"装配 {name} 缺少快照记录却已冻结")
    seal = CLIENT.call("POST", f"/assemblies/{assembly['id']}/seal", {"revision": assembly["revision"]})
    STATE.data["seals"][name] = {"id": seal["id"], "content_hash": seal["payload"]["content_hash"]}
    STATE.save()
    return seal


def ensure_exposure(key, seal_id):
    """提交曝光。幂等键锚定来源：同键同请求返回原曝光，重复运行不会另起曝光。"""
    response = CLIENT.call("POST", f"/seals/{seal_id}/exposures", {"key": key, **REQUEST})
    known = STATE.data["exposures"].get(key)
    if known is not None:
        check(response["id"] == known, f"幂等键 {key} 在重复运行中返回了不同曝光")
    else:
        STATE.data["exposures"][key] = response["id"]
        STATE.save()
    return response


def drain_worker():
    """同步执行工作器直到队列清空，返回每一步的真实终态。"""
    steps = []
    while True:
        step = CLIENT.call("POST", "/worker/step")
        if step["state"] == "idle":
            return steps
        steps.append(step)
        check(len(steps) <= 60, "工作器未在预期步数内排空队列")


def exposure_of(fig):
    return CLIENT.call("GET", f"/exposures/{STATE.data['exposures'][fig['key']]}")


def plate_of(fig):
    exposure = exposure_of(fig)
    check(exposure["state"] == "succeeded", f"{fig['slug']} 的曝光未成功：{exposure['state']}")
    plate_id = exposure["plate_id"]
    known = STATE.data["plates"].get(fig["slug"])
    if known is not None:
        check(plate_id == known, f"{fig['slug']} 的图版标识跨运行漂移")
    else:
        STATE.data["plates"][fig["slug"]] = plate_id
        STATE.save()
    return CLIENT.call("GET", f"/plates/{plate_id}")


def ensure_proof(fig, plate):
    if fig["slug"] in STATE.data["proofs"]:
        proof = CLIENT.call("GET", f"/proofs/{STATE.data['proofs'][fig['slug']]}")
        check(proof["state"] == "accepted", f"{fig['slug']} 的审阅记录失效")
        return proof
    proof = CLIENT.call("POST", f"/plates/{plate['id']}/proofs",
                        {"note": f"培训核验：{fig['title']}"})
    proof = CLIENT.call("POST", f"/proofs/{proof['id']}/decide",
                        {"state": "accepted", "note": "通道核验通过，纳入培训图册"})
    STATE.data["proofs"][fig["slug"]] = proof["id"]
    STATE.save()
    return proof


def ensure_comparison(name, left_slug, right_slug, expectation):
    if name in STATE.data["comparisons"]:
        comparison = CLIENT.call("GET", f"/comparisons/{STATE.data['comparisons'][name]}")
    else:
        comparison = CLIENT.call("POST", "/comparisons",
                                 {"left": STATE.data["plates"][left_slug],
                                  "right": STATE.data["plates"][right_slug]})
        STATE.data["comparisons"][name] = comparison["id"]
        STATE.save()
    payload = comparison["payload"]
    if expectation == "identical":
        check(payload["color_changed"] == 0 and payload["silhouette_changed"] == 0
              and payload["depth_changed"] == 0,
              f"{name} 期望逐像素一致，实际 {payload}")
    else:
        check(payload["color_changed"] > 0, f"{name} 期望颜色差异，实际 {payload}")
        check(payload["silhouette_changed"] == 0, f"{name} 不应有几何轮廓差异")
    return payload


# ---------------------------------------------------------------------------
# 五轮制作流程。
# ---------------------------------------------------------------------------
def round2_material_revision():
    CLIENT.round_tag = "round-2"
    print("第 2 轮：修订共享材质「釉青」，对比旧快照重渲与新冻结")
    teal_id = STATE.data["resources"]["pigment.teal"]
    pigment = CLIENT.call("GET", f"/pigments/{teal_id}")
    if pigment["revision"] == 1 and pigment["payload"]["color"] == TEAL["color"]:
        pigment = CLIENT.call("PUT", f"/pigments/{teal_id}", {"revision": 1, **TEAL_REVISED})
    check(pigment["payload"]["color"] == TEAL_REVISED["color"], "釉青未处于修订后颜色")
    # 旧快照重渲：S1、S2 的冻结闭包仍持有修订前的材质副本。
    ensure_exposure("train-r2-overview-reseal", STATE.data["seals"]["overview"]["id"])
    ensure_exposure("train-r2-panel-reseal", STATE.data["seals"]["panel"]["id"])
    # 新冻结：派生装配在修订后冻结，闭包捕获修订后的材质。
    if "overview-revised" not in STATE.data["assemblies"]:
        forked = CLIENT.call("POST", f"/assemblies/{STATE.data['assemblies']['overview']}/fork",
                             {"name": "总装说明图（修订）"})
        STATE.data["assemblies"]["overview-revised"] = forked["id"]
        STATE.save()
    ensure_sealed("overview-revised")
    ensure_exposure("train-r2-overview-revised", STATE.data["seals"]["overview-revised"]["id"])
    steps = drain_worker()
    check(all(step["state"] == "succeeded" for step in steps) or not steps,
          f"第 2 轮出现意外失败：{steps}")


def round3_mesh_retirement(specs):
    CLIENT.round_tag = "round-3"
    print("第 3 轮：停用共享网格「斜翼面板」，观察新冻结受限与旧快照免疫")
    # 停用前先备好一份引用面板的草稿（此时放置合法），停用后它的冻结必须被拒绝。
    ensure_assembly("panel-v2", specs["panel-v2"])
    ensure_assembly("scratch", specs["scratch"])
    panel_id = STATE.data["resources"]["mesh.panel"]
    mesh = CLIENT.call("GET", f"/meshes/{panel_id}")
    if mesh["state"] == "usable":
        mesh = CLIENT.call("POST", f"/meshes/{panel_id}/retire", {"revision": mesh["revision"]})
    check(mesh["state"] == "retired", "斜翼面板未停用")
    rejections = []
    draft = CLIENT.call("GET", f"/assemblies/{STATE.data['assemblies']['panel-v2']}")
    rejections.append({"operation": "冻结引用已停用网格的草稿（面板新方案）",
                       "expected": "retired",
                       "observed": CLIENT.expect_fault(
                           "POST", f"/assemblies/{draft['id']}/seal",
                           {"revision": draft["revision"]}, "retired")})
    scratch = CLIENT.call("GET", f"/assemblies/{STATE.data['assemblies']['scratch']}")
    rejections.append({"operation": "在新草稿中放置已停用网格",
                       "expected": "retired",
                       "observed": CLIENT.expect_fault(
                           "PUT", f"/assemblies/{scratch['id']}",
                           {"revision": scratch["revision"],
                            "placements": [{"key": "panel", "mesh_id": panel_id,
                                            "pigment_id": STATE.data["resources"]["pigment.teal"]}]},
                           "retired")})
    # 旧快照重渲不受停用影响；不引用停用网格的新装配照常冻结渲染。
    ensure_exposure("train-r3-overview-after-retire", STATE.data["seals"]["overview"]["id"])
    ensure_assembly("strut-v2", specs["strut-v2"])
    ensure_sealed("strut-v2")
    ensure_exposure("train-r3-strut-new", STATE.data["seals"]["strut-v2"]["id"])
    steps = drain_worker()
    check(all(step["state"] == "succeeded" for step in steps) or not steps,
          f"第 3 轮出现意外失败：{steps}")
    return rejections


def round4_batch_with_failure(specs):
    CLIENT.round_tag = "round-4"
    print("第 4 轮：批量制作——一份超限曝光失败，同批成功图不掩盖失败")
    ensure_assembly("deck", specs["deck"])
    ensure_sealed("deck")
    keys = {"train-r4-deck": STATE.data["seals"]["deck"]["id"],
            "train-r4-overview-final": STATE.data["seals"]["overview"]["id"],
            "train-r4-strut-final": STATE.data["seals"]["strut-v2"]["id"]}
    for key, seal_id in keys.items():
        ensure_exposure(key, seal_id)
    drain_worker()
    outcomes = {key: CLIENT.call("GET", f"/exposures/{STATE.data['exposures'][key]}") for key in keys}
    deck = outcomes["train-r4-deck"]
    check(deck["state"] == "failed" and deck["error"] == "capacity",
          f"超限曝光未按预期失败：{deck['state']} {deck['error']}")
    check(outcomes["train-r4-overview-final"]["state"] == "succeeded", "同批成功项缺失（总装）")
    check(outcomes["train-r4-strut-final"]["state"] == "succeeded", "同批成功项缺失（支架）")
    # 失败处置：显式重试一次，证明失败是确定性的，且完整历史可查询。
    if not STATE.data["flags"].get("deck_retried"):
        CLIENT.call("POST", f"/exposures/{deck['id']}/retry")
        drain_worker()
        STATE.data["flags"]["deck_retried"] = True
        STATE.save()
    deck = CLIENT.call("GET", f"/exposures/{deck['id']}")
    check(deck["state"] == "failed" and deck["error"] == "capacity" and deck["attempt"] == 2,
          f"重试后超限曝光未保持失败终态：{deck['state']} {deck['error']} attempt={deck['attempt']}")
    items = []
    for key in keys:
        exposure = CLIENT.call("GET", f"/exposures/{STATE.data['exposures'][key]}")
        items.append({"key": key, "exposure_id": exposure["id"], "state": exposure["state"],
                      "error": exposure["error"], "attempt": exposure["attempt"],
                      "plate_id": exposure["plate_id"]})
    manifest = {"round": 4, "status": "failed",
                "note": "批量中一份失败即整体标记失败；成功项不掩盖失败项。",
                "submitted": len(items),
                "succeeded": sum(1 for item in items if item["state"] == "succeeded"),
                "failed": sum(1 for item in items if item["state"] == "failed"),
                "items": items}
    (EVIDENCE / "batch-round4.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"  批量结果：{manifest['succeeded']} 成功 / {manifest['failed']} 失败"
          f"（train-r4-deck: capacity，已记录，未被成功图掩盖）")
    return manifest


def round5_round_isolation():
    CLIENT.round_tag = "round-5"
    print("第 5 轮：幂等与轮次隔离——同键重发归并，跨轮次抢键被拒")
    first = STATE.data["exposures"]["train-r1-overview"]
    replay = CLIENT.call("POST", f"/seals/{STATE.data['seals']['overview']['id']}/exposures",
                         {"key": "train-r1-overview", **REQUEST})
    check(replay["id"] == first, "同键同请求的重发未归并到原曝光")
    conflict = CLIENT.expect_fault(
        "POST", f"/seals/{STATE.data['seals']['overview-revised']['id']}/exposures",
        {"key": "train-r1-overview", **REQUEST}, "key_conflict")
    return {"replay_same_request": {"key": "train-r1-overview", "exposure_id": first,
                                    "replayed_id": replay["id"], "same_exposure": True},
            "cross_round_key_reuse": {"key": "train-r1-overview",
                                      "attempted_seal": STATE.data["seals"]["overview-revised"]["id"],
                                      "expected": "key_conflict", "observed": conflict}}


# ---------------------------------------------------------------------------
# 核验与导出：逐图核验、独立读取器、比对、证据落盘。
# ---------------------------------------------------------------------------
def export_evidence(name, value):
    (EVIDENCE / name).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                             sort_keys=True) + "\n", encoding="utf-8")


def seal_summary(name):
    seal = CLIENT.call("GET", f"/seals/{STATE.data['seals'][name]['id']}")
    payload = seal["payload"]
    closure = [{"key": item["placement"]["key"],
                "mesh_id": item["placement"]["mesh_id"], "mesh_revision": item["mesh_revision"],
                "mesh_name": item["mesh"]["name"],
                "pigment_id": item["placement"]["pigment_id"],
                "pigment_revision": item["pigment_revision"],
                "pigment_name": item["pigment"]["name"],
                "pigment_color": item["pigment"]["color"]}
               for item in sorted(payload["closure"], key=lambda item: item["placement"]["key"])]
    summary = {"seal_id": seal["id"], "content_hash": payload["content_hash"],
               "assembly_id": payload["assembly_id"], "assembly_revision": payload["assembly_revision"],
               "name": payload["name"], "frozen_resources": closure,
               "lens": payload["lens"], "illumination": payload["illumination"]}
    export_evidence(f"seal-{name}.json", summary)
    return summary


def finalize_ready_figure(fig, plate):
    """逐图核验：服务核验、审阅、拾取、文件完整性、独立读取器，并导出证据。"""
    slug = fig["slug"]
    verify = CLIENT.call("POST", f"/plates/{plate['id']}/verify")
    check(verify["valid"], f"{slug} 图版核验失败")
    export_evidence(f"verify-{slug}.json", verify)
    proof = ensure_proof(fig, plate)
    export_evidence(f"events-{proof['id']}.json",
                    CLIENT.call("GET", f"/events/{proof['id']}/history")["items"])
    pick = CLIENT.call("GET", f"/plates/{plate['id']}/pick?x=64&y=64")
    export_evidence(f"pick-{slug}.json", pick)
    # 图版文件完整性：下载的 PNG 字节必须等于图版登记的通道摘要。
    figure_dir = ATLAS / slug
    figure_dir.mkdir(parents=True, exist_ok=True)
    files = {}
    for pass_name in ("color.png", "depth.png", "object.png"):
        data = CLIENT.call("GET", f"/plates/{plate['id']}/passes/{pass_name}")
        check(hashlib.sha256(data).hexdigest() == plate["payload"]["passes"][pass_name]["sha256"],
              f"{slug} 的 {pass_name} 与登记摘要不符")
        (figure_dir / pass_name).write_bytes(data)
        files[pass_name] = f"atlas/{slug}/{pass_name}"
    # 交付包与独立读取器核验。
    bundle = CLIENT.call("GET", f"/plates/{plate['id']}/bundle")
    bundle_path = BUNDLES / f"{slug}.zip"
    bundle_path.write_bytes(bundle)
    files["bundle"] = f"catalog/bundles/{slug}.zip"
    received_dir = RECEIVED / slug
    received_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([sys.executable, "-m", "facetreader", str(bundle_path),
                           "--output", str(received_dir)],
                          cwd=REPO, capture_output=True, text=True)
    check(proc.returncode == 0, f"{slug} 的交付包被独立读取器拒绝：{proc.stdout}{proc.stderr}")
    reader = json.loads(proc.stdout)
    check(reader["valid"], f"{slug} 的交付包核验无效")
    export_evidence(f"facetreader-{slug}.json", reader)
    exposure = exposure_of(fig)
    export_evidence(f"exposure-{fig['key']}.json", exposure)
    export_evidence(f"events-{exposure['id']}.json",
                    CLIENT.call("GET", f"/events/{exposure['id']}/history")["items"])
    seal_info = STATE.data["seals"][fig["seal"]]
    record = {"slug": slug, "title": fig["title"], "round": fig["round"], "status": "ready",
              "lesson": fig["lesson"],
              "source": {"seal_id": seal_info["id"], "seal_content_hash": seal_info["content_hash"],
                         "exposure_key": fig["key"], "exposure_id": exposure["id"],
                         "plate_id": plate["id"], "visual_hash": plate["payload"]["visual_hash"],
                         "request": plate["payload"]["request"],
                         "statistics": plate["payload"]["statistics"]},
              "verification": {
                  "plate_verify": "valid",
                  "proof": {"id": proof["id"], "state": proof["state"]},
                  "pick": pick,
                  "facetreader": {"valid": reader["valid"], "summary": reader["summary"]},
                  "file_integrity": "下载的 PNG 字节与图版登记摘要逐一相等"},
              "files": files,
              "evidence": {"exposure": f"catalog/evidence/exposure-{fig['key']}.json",
                           "events": f"catalog/evidence/events-{exposure['id']}.json",
                           "verify": f"catalog/evidence/verify-{slug}.json",
                           "facetreader": f"catalog/evidence/facetreader-{slug}.json"}}
    if "identical_to" in fig:
        record["verification"]["identical_to"] = fig["identical_to"]
    if "differs_from" in fig:
        record["verification"]["differs_from"] = fig["differs_from"]
    return record


def finalize_failed_figure(fig):
    slug = fig["slug"]
    exposure = exposure_of(fig)
    check(exposure["state"] == "failed" and exposure["error"] == fig["expect_failure"],
          f"{slug} 未保持预期失败终态：{exposure['state']} {exposure['error']}")
    export_evidence(f"exposure-{fig['key']}.json", exposure)
    export_evidence(f"events-{exposure['id']}.json",
                    CLIENT.call("GET", f"/events/{exposure['id']}/history")["items"])
    figure_dir = ATLAS / slug
    figure_dir.mkdir(parents=True, exist_ok=True)
    (figure_dir / "FAILED.md").write_text(
        f"# {slug} — 制作失败\n\n"
        f"本图在批量制作中失败，未产生图版；失败未被同批成功图掩盖。\n\n"
        f"- 曝光：`{exposure['id']}`（幂等键 `{fig['key']}`）\n"
        f"- 终态：`failed` · 错误：`{exposure['error']}` · 尝试次数：{exposure['attempt']}\n"
        f"- 证据：`catalog/failures.json`、`catalog/evidence/exposure-{fig['key']}.json`、"
        f"`catalog/evidence/events-{exposure['id']}.json`\n",
        encoding="utf-8")
    seal_info = STATE.data["seals"][fig["seal"]]
    return {"slug": slug, "title": fig["title"], "round": fig["round"], "status": "failed",
            "lesson": fig["lesson"],
            "source": {"seal_id": seal_info["id"], "seal_content_hash": seal_info["content_hash"],
                       "exposure_key": fig["key"], "exposure_id": exposure["id"]},
            "failure": {"state": exposure["state"], "error": exposure["error"],
                        "attempt": exposure["attempt"], "retried": True,
                        "note": "冻结成功、渲染超 40000 源面额度；重试后仍以相同错误失败，属确定性失败。"},
            "evidence": {"exposure": f"catalog/evidence/exposure-{fig['key']}.json",
                         "events": f"catalog/evidence/events-{exposure['id']}.json",
                         "batch": "catalog/evidence/batch-round4.json"},
            "files": {"failure_notice": f"atlas/{slug}/FAILED.md"}}


# ---------------------------------------------------------------------------
# 目录与图册文档生成。
# ---------------------------------------------------------------------------
def impact_matrix(comparisons):
    revised = comparisons["cmp-revised-vs-baseline"]
    return [
        {"event": "材质修订：釉青 [0.12, 0.62, 0.61] → [0.16, 0.32, 0.85]",
         "affected": ["fig-06-overview-revised（修订后的新冻结）"],
         "unaffected": ["fig-01/02/04/05/07/10（全部旧快照及其重渲）"],
         "evidence": f"cmp-revised-vs-baseline 颜色差异 {revised['color_changed']} 像素、"
                     f"轮廓差异 {revised['silhouette_changed']}；cmp-reseal-overview、"
                     "cmp-reseal-panel 三项差异全 0"},
        {"event": "网格停用：斜翼面板 retire",
         "affected": ["新冻结被拒 2 起：冻结引用停用网格的草稿、在新草稿放置停用网格（均 409 retired）"],
         "unaffected": ["fig-01/04/07/10（旧快照 S1 及其重渲）"],
         "evidence": "catalog/evidence/rejections.json；cmp-after-retire 三项差异全 0"},
        {"event": "旧快照重渲",
         "affected": ["产生与基线逐像素一致的新图版：fig-04、fig-05、fig-07、fig-10"],
         "unaffected": ["—"],
         "evidence": "重渲图版 visual_hash 与基线完全相等；对应 comparison 三项差异全 0"},
        {"event": "批量制作中的单份失败",
         "affected": ["fig-09-deck-layout：failed（capacity），重试仍失败"],
         "unaffected": ["同批 fig-10、fig-11 正常成功"],
         "evidence": "catalog/evidence/batch-round4.json；catalog/failures.json；曝光事件流"}]


def write_atlas(records, comparisons, extra, run_seq):
    lines = ["# 培训图册：共享资源使用", "",
             f"> 由 `training/materials/build_atlas.py` 第 {run_seq} 次运行生成；渲染器 facet-raster-1，"
             "全部图版为 128×128、2×2 超采样。",
             "> 服务数据根：`training/data/`（`python3 -m facetproof --root training/data` 可重启查询）。",
             "", "## 共享资源", "",
             "| 资源 | 标识 | 状态 | 修订 |", "| --- | --- | --- | --- |"]
    for item in extra["shared_resources"]:
        lines.append(f"| {item['logical']}（{item['name']}） | `{item['id']}` | {item['state']} | "
                     f"{item['revision']} |")
    lines += ["", "## 事件影响矩阵", "",
              "| 事件 | 影响的说明图 | 不受影响的说明图 | 证据 |", "| --- | --- | --- | --- |"]
    for row in impact_matrix(comparisons):
        lines.append("| {event} | {affected} | {unaffected} | {evidence} |".format(
            event=row["event"], affected="；".join(row["affected"]),
            unaffected="；".join(row["unaffected"]), evidence=row["evidence"]))
    lines += ["", "## 逐图", ""]
    for record in records:
        slug = record["slug"]
        lines.append(f"### {slug} — {record['title']}")
        lines.append("")
        lines.append(f"- 轮次：{record['round']} · 状态：**{record['status']}** · 教学要点：{record['lesson']}")
        if record["status"] == "ready":
            src = record["source"]
            lines.append(f"- 快照 `{src['seal_id']}`（内容摘要 `{src['seal_content_hash'][:16]}…`）"
                         f"· 图版 `{src['plate_id']}`（visual_hash `{src['visual_hash'][:16]}…`）")
            ver = record["verification"]
            lines.append(f"- 核验：图版核验 {ver['plate_verify']} · 审阅 {ver['proof']['state']} "
                         f"· 独立读取器 valid={ver['facetreader']['valid']} · "
                         f"覆盖像素 {ver['facetreader']['summary']['covered_pixels']}")
            lines.append("")
            lines.append(f"![color]({slug}/color.png) ![depth]({slug}/depth.png) "
                         f"![object]({slug}/object.png)")
        else:
            failure = record["failure"]
            lines.append(f"- 失败：`{failure['error']}`（终态 {failure['state']}，尝试 {failure['attempt']} 次，"
                         "含一次显式重试）——无图版，证据见 `catalog/failures.json`")
        lines.append("")
    (ATLAS / "ATLAS.md").write_text("\n".join(lines), encoding="utf-8")


def write_catalog(records, comparisons, extra, run_seq):
    catalog = {"schema": "facet-training-catalog-1", "run_seq": run_seq,
               "service": extra["health"], "request": REQUEST,
               "shared_resources": extra["shared_resources"], "seals": extra["seals"],
               "capacity": extra["capacity"],
               "figures": records, "comparisons": comparisons,
               "impact_matrix": impact_matrix(comparisons),
               "rejections": extra["rejections"], "idempotency": extra["idempotency"],
               "batch_round4": extra["batch"], "rerun_stability": extra["stability"]}
    (CATALOG / "catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    failures = [record for record in records if record["status"] == "failed"]
    (CATALOG / "failures.json").write_text(
        json.dumps({"schema": "facet-training-failures-1", "run_seq": run_seq,
                    "query": "python3 -m facetproof --root training/data 重启服务后，"
                             "GET /facet/v1/exposures/{exposure_id} 与 /events/{exposure_id}/history",
                    "failures": failures}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    by_slug = {}
    for name, left, right, _expectation in COMPARISONS:
        for slug in (left, right):
            by_slug.setdefault(slug, []).append(name)
    lines = ["# 逐图核验目录", "",
             f"第 {run_seq} 次运行生成。机读版：`catalog.json`；失败汇总：`failures.json`；"
             "证据原件：`evidence/`。", "",
             "## 核验总表", "",
             "| 图 | 轮次 | 状态 | 图版核验 | 审阅 | 独立读取器 | 关键结论 |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for record in records:
        if record["status"] == "ready":
            conclusion = "；".join(f"{name}: 颜色差异 {comparisons[name]['color_changed']}"
                                   for name in by_slug.get(record["slug"], []))
            lines.append(f"| {record['slug']} | {record['round']} | ready | valid | "
                         f"{record['verification']['proof']['state']} | valid | {conclusion or '—'} |")
        else:
            lines.append(f"| {record['slug']} | {record['round']} | **failed** | — | — | — | "
                         f"{record['failure']['error']}（尝试 {record['failure']['attempt']} 次） |")
    lines += ["", "## 失败与拒绝证据", ""]
    for record in failures:
        lines.append(f"- `{record['slug']}`：曝光 `{record['source']['exposure_id']}` 终态 failed"
                     f"（{record['failure']['error']}），证据 {record['evidence']['exposure']}、"
                     f"{record['evidence']['events']}")
    for rejection in extra["rejections"]:
        lines.append(f"- 拒绝：{rejection['operation']} → {rejection['observed']['code']} "
                     f"（HTTP {rejection['observed']['http_status']}）")
    lines += ["", "## 轮次隔离与重复运行", "",
              f"- 同键重发归并：`{extra['idempotency']['replay_same_request']['key']}` "
              "重发返回原曝光（same_exposure=true）",
              f"- 跨轮次抢键被拒：{extra['idempotency']['cross_round_key_reuse']['observed']['code']}",
              f"- 跨运行稳定性：{extra['stability']['summary']}", ""]
    (CATALOG / "CATALOG.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# 主流程。
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8876)
    args = parser.parse_args()

    global CLIENT, STATE
    for directory in (MATERIALS, ATLAS, CATALOG, EVIDENCE, BUNDLES, RECEIVED):
        directory.mkdir(parents=True, exist_ok=True)
    STATE = State(STATE_PATH)
    STATE.data["run_seq"] += 1
    run_seq = STATE.data["run_seq"]
    STATE.save()
    transcript_path = MATERIALS / f"transcript-run-{run_seq}.jsonl"
    server_log = open(MATERIALS / f"server-run-{run_seq}.log", "wb")
    server = subprocess.Popen([sys.executable, "-m", "facetproof", "--root", str(DATA),
                               "--host", "127.0.0.1", "--port", str(args.port)],
                              cwd=REPO, stdout=server_log, stderr=subprocess.STDOUT)
    try:
        with open(transcript_path, "w", encoding="utf-8") as transcript:
            CLIENT = Client(f"http://127.0.0.1:{args.port}/facet/v1", transcript)
            health = None
            for _ in range(100):
                try:
                    health = CLIENT.call("GET", "/health")
                    break
                except (urllib.error.URLError, ConnectionError, OSError):
                    time.sleep(0.1)
            check(health is not None, "服务未能启动")
            # 第 1 轮（资源确保完成后 specs 才有真实标识，故在轮次内重建）。
            CLIENT.round_tag = "round-1"
            ensure_resource("meshes", "mesh.panel", PANEL_MESH)
            ensure_resource("meshes", "mesh.strut", STRUT_MESH)
            ensure_resource("meshes", "mesh.deck", deck_mesh())
            ensure_resource("pigments", "pigment.teal", TEAL)
            ensure_resource("pigments", "pigment.amber", AMBER)
            specs = assembly_specs(STATE.data["resources"])
            for name in ("overview", "panel", "strut"):
                ensure_assembly(name, specs[name])
                ensure_sealed(name)
            for fig in FIGURES:
                if fig["round"] == 1:
                    ensure_exposure(fig["key"], STATE.data["seals"][fig["seal"]]["id"])
            steps = drain_worker()
            check(all(step["state"] == "succeeded" for step in steps) or not steps,
                  f"第 1 轮出现意外失败：{steps}")
            print("第 1 轮：共享资源与三份复用装配已冻结渲染")
            round2_material_revision()
            rejections = round3_mesh_retirement(specs)
            export_evidence("rejections.json", rejections)
            batch = round4_batch_with_failure(specs)
            idempotency = round5_round_isolation()
            export_evidence("idempotency.json", idempotency)

            CLIENT.round_tag = "finalize"
            print("汇总：逐图核验、比对与证据导出")
            plates = {}
            for fig in FIGURES:
                if not fig.get("expect_failure"):
                    plates[fig["slug"]] = plate_of(fig)
            for fig in FIGURES:
                if "identical_to" in fig:
                    check(plates[fig["slug"]]["payload"]["visual_hash"]
                          == plates[fig["identical_to"]]["payload"]["visual_hash"],
                          f"{fig['slug']} 与 {fig['identical_to']} 的 visual_hash 不等")
                if "differs_from" in fig:
                    check(plates[fig["slug"]]["payload"]["visual_hash"]
                          != plates[fig["differs_from"]]["payload"]["visual_hash"],
                          f"{fig['slug']} 与 {fig['differs_from']} 应当不同")
            records = []
            for fig in FIGURES:
                if fig.get("expect_failure"):
                    records.append(finalize_failed_figure(fig))
                else:
                    records.append(finalize_ready_figure(fig, plates[fig["slug"]]))
            comparisons = {}
            for name, left, right, expectation in COMPARISONS:
                comparisons[name] = ensure_comparison(name, left, right, expectation)
                export_evidence(f"comparison-{name}.json", comparisons[name])
            seals = {name: seal_summary(name) for name in
                     ("overview", "panel", "strut", "overview-revised", "strut-v2", "deck")}
            capacity = CLIENT.call("GET", "/resources")
            export_evidence("resources.json", capacity)
            check(capacity["reserved"] == 0, "终态后画布额度未全部归还")
            shared_resources = []
            for logical, object_id in sorted(STATE.data["resources"].items()):
                kind = "meshes" if logical.startswith("mesh") else "pigments"
                obj = CLIENT.call("GET", f"/{kind}/{object_id}")
                export_evidence(f"resource-{logical}.json", obj)
                export_evidence(f"events-{object_id}.json",
                                CLIENT.call("GET", f"/events/{object_id}/history")["items"])
                shared_resources.append({"logical": logical, "id": object_id, "kind": kind,
                                         "state": obj["state"], "revision": obj["revision"],
                                         "name": obj["payload"]["name"]})
            for name, assembly_id in sorted(STATE.data["assemblies"].items()):
                export_evidence(f"events-{assembly_id}.json",
                                CLIENT.call("GET", f"/events/{assembly_id}/history")["items"])
            # 跨运行稳定性：同一批图版的来源标识不得漂移。
            fingerprints = {record["slug"]: {
                "exposure_id": record["source"]["exposure_id"],
                "plate_id": record["source"].get("plate_id"),
                "visual_hash": record["source"].get("visual_hash"),
                "status": record["status"]} for record in records}
            previous = STATE.data["fingerprints"]
            if previous:
                drift = {slug: {"was": previous.get(slug), "now": current}
                         for slug, current in fingerprints.items() if previous.get(slug) != current}
                check(not drift, f"重复运行出现来源漂移：{drift}")
                stability = {"stable": True, "checked_against_run": run_seq - 1,
                           "summary": f"第 {run_seq} 次运行与既有记录完全一致："
                                    "曝光、图版、visual_hash 均未漂移，未另起来源。"}
            else:
                stability = {"stable": True, "baseline_recorded": True,
                             "summary": "首次运行，已记录基线指纹供后续运行比对。"}
            STATE.data["fingerprints"] = fingerprints
            STATE.save()
            extra = {"health": health, "capacity": capacity, "shared_resources": shared_resources,
                     "seals": seals, "rejections": rejections, "idempotency": idempotency,
                     "batch": batch, "stability": stability}
            write_atlas(records, comparisons, extra, run_seq)
            write_catalog(records, comparisons, extra, run_seq)
            ready = sum(1 for record in records if record["status"] == "ready")
            failed = sum(1 for record in records if record["status"] == "failed")
            print(f"完成：{ready} 份图版就绪，{failed} 份失败（已如实记录）；"
                  f"图册 atlas/ATLAS.md，目录 catalog/CATALOG.md")
    finally:
        server.terminate()
        server.wait(timeout=10)
        server_log.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BuildFailure as exc:
        print(f"制作失败：{exc}", file=sys.stderr)
        sys.exit(1)
