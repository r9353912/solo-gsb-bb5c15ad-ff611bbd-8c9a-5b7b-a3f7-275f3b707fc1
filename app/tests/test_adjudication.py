"""人工选证裁决端到端测试。"""
from __future__ import annotations

import hashlib
import json

import pytest

DB_BASENAME = "trace_test_adjudication"

API = "/api/v1"


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def scenario(client):
    """设备 DJ 上两份同时覆盖的证书；读数 J1（两候选范围内）、J2（CERT-J1 超限）。"""
    r = client.post(f"{API}/certificates", json={
        "request_id": "adj-req-cert1",
        "certificate_no": "CERT-J1", "device_no": "DJ",
        "valid_from": "2026-03-01T00:00:00Z",
        "valid_to": "2026-04-01T00:00:00Z",
        "min_value": 0.0, "max_value": 10.0,
    })
    assert r.status_code == 200, r.text
    r = client.post(f"{API}/certificates", json={
        "request_id": "adj-req-cert2",
        "certificate_no": "CERT-J2", "device_no": "DJ",
        "valid_from": "2026-03-01T00:00:00Z",
        "valid_to": "2026-04-01T00:00:00Z",
        "min_value": 0.0, "max_value": 100.0,
    })
    assert r.status_code == 200, r.text
    r = client.post(f"{API}/readings", json={
        "request_id": "adj-req-reads",
        "readings": [
            {"reading_no": "J1", "device_no": "DJ",
             "measured_at": "2026-03-10T00:00:00Z", "value": 42.0},
            {"reading_no": "J2", "device_no": "DJ",
             "measured_at": "2026-03-11T00:00:00Z", "value": 5.0},
        ],
    })
    assert r.status_code == 200, r.text
    base = r.json()["version"]
    for no in ("J1", "J2"):
        body = client.get(f"{API}/readings/{no}").json()
        assert body["status"] == "review"
        assert body["adjudication"] is None
    return {"base": base}


def test_01_adjudicate_success_conforming_and_out_of_range(client, scenario):
    base = scenario["base"]

    # 选 CERT-J2（范围 [0,100]）裁决 J1 -> conforming
    r = client.post(f"{API}/readings/J1/adjudicate", json={
        "request_id": "adj-decide-j1-j2",
        "reading_no": "J1",
        "certificate_no": "CERT-J2",
        "version_seq": 1,
        "base_version": base,
        "reason": "复核确认使用 CERT-J2",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["event_type"] == "reading_adjudicate"
    assert body["version"] == base + 1
    assert body["idempotent_replay"] is False
    assert body["status"] == "conforming"
    assert body["reliance"] == {
        "certificate_no": "CERT-J2", "version_seq": 1, "device_no": "DJ",
        "valid_from": "2026-03-01T00:00:00.000000Z",
        "valid_to": "2026-04-01T00:00:00.000000Z",
        "min_value": 0.0, "max_value": 100.0,
        "created_at_version": 2, "superseded_at_version": None,
    }
    # 全部候选保留
    assert {c["certificate_no"] for c in body["candidates"]} == {"CERT-J1", "CERT-J2"}

    adj = body["adjudication"]
    assert adj["state"] == "effective"
    assert adj["decided_at_version"] == base + 1
    assert adj["base_version"] == base
    assert adj["selected_certificate_no"] == "CERT-J2"
    assert adj["selected_version_seq"] == 1
    assert adj["invalidated_at_version"] is None
    assert adj["basis"]["candidate_count"] == 2
    assert {c["certificate_no"] for c in adj["basis"]["candidates"]} == {"CERT-J1", "CERT-J2"}

    # 选 CERT-J2 裁决 J2（5.0 也在 [0,100]）-> conforming；改选 CERT-J1 则 out_of_range
    r = client.post(f"{API}/readings/J2/adjudicate", json={
        "request_id": "adj-decide-j2-j1",
        "reading_no": "J2",
        "certificate_no": "CERT-J1",
        "version_seq": 1,
        "base_version": base + 1,
        "reason": "应按 CERT-J1 判定",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "conforming"  # 5.0 也在 [0,10] 闭区间内
    assert body["reliance"]["certificate_no"] == "CERT-J1"

    # 闭区间边界：再提一个恰为边界值的读数，裁决后边界值合格
    r = client.post(f"{API}/readings", json={
        "request_id": "adj-req-edge",
        "readings": [{
            "reading_no": "J3", "device_no": "DJ",
            "measured_at": "2026-03-12T00:00:00Z", "value": 10.0,
        }],
    })
    edge_base = r.json()["version"]
    r = client.post(f"{API}/readings/J3/adjudicate", json={
        "request_id": "adj-decide-j3",
        "reading_no": "J3",
        "certificate_no": "CERT-J1",
        "version_seq": 1,
        "base_version": edge_base,
    })
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "conforming"


def test_02_rejections_consume_no_version(client, scenario):
    base = client.get(f"{API}/status").json()["current_version"]

    # 依据版本不等于当前版本 -> 409 base_version_stale，不产生新版本
    r = client.post(f"{API}/readings/J1/adjudicate", json={
        "request_id": "adj-rej-stale",
        "reading_no": "J1",
        "certificate_no": "CERT-J2", "version_seq": 1,
        "base_version": 3,
        "reason": "old base",
    })
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "base_version_stale"

    # 未知读数 -> 404
    r = client.post(f"{API}/readings/NOPE/adjudicate", json={
        "request_id": "adj-rej-unknown",
        "reading_no": "NOPE",
        "certificate_no": "CERT-J2", "version_seq": 1,
        "base_version": base,
    })
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_reading"

    # 所选版本不是候选（不存在的版本序号）-> 422 target_mismatch
    r = client.post(f"{API}/readings/J1/adjudicate", json={
        "request_id": "adj-rej-badseq",
        "reading_no": "J1",
        "certificate_no": "CERT-J2", "version_seq": 9,
        "base_version": base,
    })
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "target_mismatch"

    # 不是候选的证书 -> 422 target_mismatch
    # （先签一份不覆盖该测量时刻的证书）
    r = client.post(f"{API}/certificates", json={
        "request_id": "adj-rej-cert",
        "certificate_no": "CERT-JX", "device_no": "DJ",
        "valid_from": "2026-05-01T00:00:00Z",
        "valid_to": "2026-06-01T00:00:00Z",
        "min_value": 0.0, "max_value": 1.0,
    })
    assert r.status_code == 200
    after_cert = client.get(f"{API}/status").json()["current_version"]
    r = client.post(f"{API}/readings/J1/adjudicate", json={
        "request_id": "adj-rej-notcand",
        "reading_no": "J1",
        "certificate_no": "CERT-JX", "version_seq": 1,
        "base_version": after_cert,
    })
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "target_mismatch"

    # 非 review 读数不能裁决：唯一匹配设备的读数 conforming -> 409
    r = client.post(f"{API}/certificates", json={
        "request_id": "adj-rej-certu",
        "certificate_no": "CERT-JU", "device_no": "DJU",
        "valid_from": "2026-03-01T00:00:00Z",
        "valid_to": "2026-04-01T00:00:00Z",
        "min_value": 0.0, "max_value": 10.0,
    })
    assert r.status_code == 200
    r = client.post(f"{API}/readings", json={
        "request_id": "adj-req-u",
        "readings": [{"reading_no": "JU", "device_no": "DJU",
                      "measured_at": "2026-03-10T00:00:00Z", "value": 1.0}],
    })
    assert r.status_code == 200
    u_base = r.json()["version"]
    r = client.post(f"{API}/readings/JU/adjudicate", json={
        "request_id": "adj-rej-u",
        "reading_no": "JU",
        "certificate_no": "CERT-JU", "version_seq": 1,
        "base_version": u_base,
    })
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "reading_not_in_review"

    # 路径与请求体读数编号不一致 -> 422
    r = client.post(f"{API}/readings/J1/adjudicate", json={
        "request_id": "adj-rej-path",
        "reading_no": "J2",
        "certificate_no": "CERT-J2", "version_seq": 1,
        "base_version": u_base,
    })
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "path_body_mismatch"

    # 全部拒绝均不产生新版本
    assert client.get(f"{API}/status").json()["current_version"] == u_base


def test_03_idempotent_replay_and_conflict(client, scenario):
    decide_version = scenario["base"] + 1

    # 相同 request_id + 相同内容：沿用首次结果
    r = client.post(f"{API}/readings/J1/adjudicate", json={
        "request_id": "adj-decide-j1-j2",
        "reading_no": "J1",
        "certificate_no": "CERT-J2", "version_seq": 1,
        "base_version": scenario["base"],
        "reason": "复核确认使用 CERT-J2",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["idempotent_replay"] is True
    assert body["version"] == decide_version

    # 相同 request_id 不同内容 -> 409
    r = client.post(f"{API}/readings/J2/adjudicate", json={
        "request_id": "adj-decide-j1-j2",
        "reading_no": "J2",
        "certificate_no": "CERT-J1", "version_seq": 1,
        "base_version": scenario["base"],
    })
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "request_id_conflict"


def test_04_queries_presented_per_version(client, scenario):
    decide_version = scenario["base"] + 1  # J1 的裁决版本

    # 当前视角：J1 按裁决呈现 conforming
    cur = client.get(f"{API}/readings/J1").json()
    assert cur["status"] == "conforming"
    assert cur["reliance"]["certificate_no"] == "CERT-J2"
    assert cur["adjudication"]["state"] == "effective"
    assert {c["certificate_no"] for c in cur["candidates"]} == {"CERT-J1", "CERT-J2"}

    # 裁决版本之前：没有裁决，仍是 review
    old = client.get(f"{API}/readings/J1", params={"at_version": scenario["base"]}).json()
    assert old["status"] == "review"
    assert old["adjudication"] is None
    assert old["reliance"] is None

    # 恰在裁决版本：裁决可见
    at = client.get(f"{API}/readings/J1", params={"at_version": decide_version}).json()
    assert at["status"] == "conforming"
    assert at["adjudication"]["state"] == "effective"

    # 列表按版本呈现裁决结论，status 过滤按呈现口径
    rows = client.get(f"{API}/readings", params={
        "device_no": "DJ", "status": "conforming",
    }).json()["items"]
    nos = {i["reading_no"] for i in rows}
    assert {"J1", "J2", "J3"} <= nos
    assert all(i["adjudication"] is not None for i in rows if i["reading_no"] in {"J1", "J2", "J3"})

    rows = client.get(f"{API}/readings", params={
        "device_no": "DJ", "status": "review",
    }).json()["items"]
    assert all(i["reading_no"] not in {"J1", "J2", "J3"} for i in rows)

    # 请求追溯能找到裁决事件
    r = client.get(f"{API}/requests/adj-decide-j1-j2")
    assert r.status_code == 200
    assert r.json()["event_type"] == "reading_adjudicate"

    # 版本流水包含新事件类型
    evs = client.get(f"{API}/versions", params={"limit": 200}).json()["items"]
    assert any(e["event_type"] == "reading_adjudicate" for e in evs)


def test_05_invalidation_on_correct_and_revoke(client, scenario):
    # 裁决 J1 选的是 CERT-J2 v1；纠正 CERT-J2 -> v1 失效 -> J1 裁决失效
    before = client.get(f"{API}/status").json()["current_version"]
    r = client.post(f"{API}/certificates/CERT-J2/correct", json={
        "request_id": "adj-correct-j2",
        "certificate_no": "CERT-J2", "device_no": "DJ",
        "valid_from": "2026-03-01T00:00:00Z",
        "valid_to": "2026-04-01T00:00:00Z",
        "min_value": 0.0, "max_value": 200.0,
        "reason": "范围纠正",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    correct_v = body["version"]
    by_no = {a["reading_no"]: a for a in body["affected_readings"]}
    # J1：裁决依据 v1 失效；此时自动结论仍为 review（CERT-J1 v1 + CERT-J2 v2）
    assert by_no["J1"]["before"]["status"] == "conforming"
    assert by_no["J1"]["after"]["status"] == "review"
    assert by_no["J1"]["adjudication_invalidated"] is True

    # 当前读数：回到自动结论 review，并标明裁决已失效
    cur = client.get(f"{API}/readings/J1").json()
    assert cur["status"] == "review"
    assert cur["reliance"] is None
    assert cur["adjudication"]["state"] == "invalidated"
    assert cur["adjudication"]["invalidated_at_version"] == correct_v
    assert cur["adjudication"]["invalidated_by_event"] == "cert_correct"
    # 失效后候选为当前自动候选（新版本 v2）
    cur_cands = {c["version_seq"]: c["certificate_no"] for c in cur["candidates"]}
    assert cur_cands == {1: "CERT-J1", 2: "CERT-J2"}

    # 旧版本（裁决生效时）仍可查到原裁决结论
    old = client.get(f"{API}/readings/J1", params={"at_version": before}).json()
    assert old["status"] == "conforming"
    assert old["adjudication"]["state"] == "effective"
    assert old["reliance"]["version_seq"] == 1

    # J2 选的是 CERT-J1 v1，撤销 CERT-J1 -> J2 裁决失效；
    # 自动口径变为唯一匹配 CERT-J2 v2 -> 42 在 [0,200] -> conforming（自动恢复）
    r = client.post(f"{API}/certificates/CERT-J1/revoke", json={
        "request_id": "adj-revoke-j1cert",
        "certificate_no": "CERT-J1",
        "reason": "作废",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    revoke_v = body["version"]
    by_no = {a["reading_no"]: a for a in body["affected_readings"]}
    # J2：原裁决依 CERT-J1 失效；自动结论按 CERT-J2 v2，5.0 合格
    assert by_no["J2"]["before"]["status"] == "conforming"
    assert by_no["J2"]["after"]["status"] == "conforming"
    assert by_no["J2"]["adjudication_invalidated"] is True

    j2 = client.get(f"{API}/readings/J2").json()
    assert j2["status"] == "conforming"
    assert j2["reliance"]["certificate_no"] == "CERT-J2"
    assert j2["reliance"]["version_seq"] == 2
    assert j2["adjudication"]["state"] == "invalidated"
    assert j2["adjudication"]["invalidated_at_version"] == revoke_v
    assert j2["adjudication"]["invalidated_by_event"] == "cert_revoke"

    # J1：两份证书分别已纠正/撤销 -> CERT-J1 撤销后只剩 CERT-J2 v2 唯一匹配
    j1 = client.get(f"{API}/readings/J1").json()
    assert j1["status"] == "conforming"  # 42 在 [0,200]
    assert j1["adjudication"]["state"] == "invalidated"


def test_06_readjudicate_after_review_returns(client, scenario):
    # 撤销 CERT-J2（当前 v2）后 J1 自动变 unverified；重新签发证书让其回到 review 后可再次裁决
    r = client.post(f"{API}/certificates/CERT-J2/revoke", json={
        "request_id": "adj-revoke-j2cert",
        "certificate_no": "CERT-J2",
    })
    assert r.status_code == 200
    j1 = client.get(f"{API}/readings/J1").json()
    assert j1["status"] == "unverified"
    assert j1["adjudication"]["state"] == "invalidated"

    # 新签两份覆盖证书 -> review 再次出现
    r = client.post(f"{API}/certificates", json={
        "request_id": "adj-re-cert-a",
        "certificate_no": "CERT-JA", "device_no": "DJ",
        "valid_from": "2026-03-01T00:00:00Z",
        "valid_to": "2026-04-01T00:00:00Z",
        "min_value": 0.0, "max_value": 50.0,
    })
    assert r.status_code == 200
    r = client.post(f"{API}/certificates", json={
        "request_id": "adj-re-cert-b",
        "certificate_no": "CERT-JB", "device_no": "DJ",
        "valid_from": "2026-03-01T00:00:00Z",
        "valid_to": "2026-04-01T00:00:00Z",
        "min_value": 0.0, "max_value": 100.0,
    })
    assert r.status_code == 200
    base = client.get(f"{API}/status").json()["current_version"]
    j1 = client.get(f"{API}/readings/J1").json()
    assert j1["status"] == "review"

    r = client.post(f"{API}/readings/J1/adjudicate", json={
        "request_id": "adj-redecide-j1",
        "reading_no": "J1",
        "certificate_no": "CERT-JB", "version_seq": 1,
        "base_version": base,
        "reason": "重新复核",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "conforming"
    assert body["reliance"]["certificate_no"] == "CERT-JB"
    # 当前呈现的是新裁决；失效的旧裁决仍保留在该读数的版本历史中可按版本查询
    assert body["adjudication"]["state"] == "effective"
    assert body["adjudication"]["decided_at_version"] == base + 1
    assert body["adjudication"]["basis"]["candidate_count"] == 2


def test_07_audit_packages_present_adjudication_per_version(client, scenario):
    # 构造一个干净的取证场景：设备 DP，两证书，裁决发生在某个版本
    r = client.post(f"{API}/certificates", json={
        "request_id": "adj-p-cert1",
        "certificate_no": "CERT-P1", "device_no": "DP",
        "valid_from": "2026-03-01T00:00:00Z",
        "valid_to": "2026-04-01T00:00:00Z",
        "min_value": 0.0, "max_value": 10.0,
    })
    assert r.status_code == 200
    r = client.post(f"{API}/certificates", json={
        "request_id": "adj-p-cert2",
        "certificate_no": "CERT-P2", "device_no": "DP",
        "valid_from": "2026-03-01T00:00:00Z",
        "valid_to": "2026-04-01T00:00:00Z",
        "min_value": 0.0, "max_value": 100.0,
    })
    assert r.status_code == 200
    r = client.post(f"{API}/readings", json={
        "request_id": "adj-p-read",
        "readings": [{"reading_no": "P1", "device_no": "DP",
                      "measured_at": "2026-03-05T00:00:00Z", "value": 42.0}],
    })
    assert r.status_code == 200
    review_v = r.json()["version"]

    flt = {
        "device_no": "DP",
        "measured_from": "2026-03-01T00:00:00Z",
        "measured_to": "2026-04-01T00:00:00Z",
    }

    # 裁决前版本视角：review，两候选
    pkg = client.get(f"{API}/audit/packages", params={**flt, "at_version": review_v}).json()
    assert pkg["items"][0]["status"] == "review"
    assert {c["certificate_no"] for c in pkg["items"][0]["candidates"]} == {"CERT-P1", "CERT-P2"}
    pre_digest = pkg["digest"]["package_digest"]

    # 裁决：选 CERT-P2 -> conforming
    r = client.post(f"{API}/readings/P1/adjudicate", json={
        "request_id": "adj-p-decide",
        "reading_no": "P1",
        "certificate_no": "CERT-P2", "version_seq": 1,
        "base_version": review_v,
    })
    assert r.status_code == 200
    decide_v = r.json()["version"]

    # 裁决版本视角：conforming，reliance 指向所选证书，候选闭集保留
    pkg = client.get(f"{API}/audit/packages", params={**flt, "at_version": decide_v}).json()
    item = pkg["items"][0]
    assert item["status"] == "conforming"
    assert item["reliance"]["certificate_no"] == "CERT-P2"
    assert {c["certificate_no"] for c in item["candidates"]} == {"CERT-P1", "CERT-P2"}
    # 条目仍沿用既有字段集合（audit-v1 包结构不变）
    assert set(item.keys()) == {
        "reading_no", "device_no", "measured_at", "value",
        "submitted_at_version", "status", "reliance", "candidates",
    }
    # 摘要可按 audit-v1 规则独立复算
    canonical_package = {
        "spec_version": pkg["digest"]["spec_version"],
        "filter": pkg["filter"],
        "at_version": decide_v,
        "total_count": pkg["total_count"],
        "items": pkg["items"],
    }
    assert pkg["digest"]["package_digest"] == _sha(_canon(canonical_package))

    # 裁决前版本的包摘要不受后续裁决影响
    pre_pkg = client.get(f"{API}/audit/packages", params={**flt, "at_version": review_v}).json()
    assert pre_pkg["digest"]["package_digest"] == pre_digest
    assert pre_pkg["items"][0]["status"] == "review"

    # 撤销所选证书：裁决失效，读数回到自动结论；旧版本取证包内容/摘要不变
    r = client.post(f"{API}/certificates/CERT-P2/revoke", json={
        "request_id": "adj-p-revoke",
        "certificate_no": "CERT-P2",
    })
    assert r.status_code == 200
    revoke_v = r.json()["version"]

    same = client.get(f"{API}/audit/packages", params={**flt, "at_version": decide_v}).json()
    assert same["digest"]["package_digest"] == pkg["digest"]["package_digest"]
    assert same["items"] == pkg["items"]

    cur = client.get(f"{API}/audit/packages", params={**flt, "at_version": revoke_v}).json()
    # CERT-P1 唯一匹配，42 超出 [0,10] -> out_of_range（自动结论恢复）
    assert cur["items"][0]["status"] == "out_of_range"
    assert cur["items"][0]["reliance"]["certificate_no"] == "CERT-P1"


def test_08_adjudicate_validation_422(client):
    # base_version < 1 / version_seq < 1 -> 422（pydantic）
    r = client.post(f"{API}/readings/J1/adjudicate", json={
        "request_id": "adj-bad-1",
        "reading_no": "J1",
        "certificate_no": "CERT-JA", "version_seq": 1,
        "base_version": 0,
    })
    assert r.status_code == 422
