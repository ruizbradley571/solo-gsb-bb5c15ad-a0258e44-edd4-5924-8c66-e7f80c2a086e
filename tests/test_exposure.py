"""开封暴露审计的单元测试与 API 集成测试。"""
from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient

from app.exposure import audit_exposure
from app.main import app
from app.models import (
    AllowedWindow,
    Event,
    EventType,
    ExposureAuditRequest,
)

client = TestClient(app)

DAY = "2026-09-18"


def dt(h: int, m: int = 0) -> datetime:
    return datetime.fromisoformat(f"{DAY}T{h:02d}:{m:02d}:00+00:00")


def ev(h: int, m: int, type_: EventType, **kw) -> Event:
    return Event(type=type_, timestamp=f"{DAY}T{h:02d}:{m:02d}:00Z", **kw)


def win(sh: int, sm: int, eh: int, em: int) -> AllowedWindow:
    return AllowedWindow(start=f"{DAY}T{sh:02d}:{sm:02d}:00Z",
                         end=f"{DAY}T{eh:02d}:{em:02d}:00Z")


def audit_until(h: int, m: int = 0) -> str:
    return f"{DAY}T{h:02d}:{m:02d}:00Z"


def request_with(events, until_h: int, until_m: int = 0, windows=None) -> ExposureAuditRequest:
    return ExposureAuditRequest(
        events=events,
        audit_until=audit_until(until_h, until_m),
        allowed_windows=windows or [],
    )


def simple_chain():
    """seal 10:00 -> unseal 12:00 -> reseal 13:00。"""
    return [
        ev(10, 0, EventType.SEAL, seal_id="S001", custodian="Alice"),
        ev(12, 0, EventType.UNSEAL),
        ev(13, 0, EventType.RESEAL, seal_id="S002", custodian="Alice"),
    ]


# ---------------------------------------------------------------------------
# 区间推导
# ---------------------------------------------------------------------------


def test_closed_interval_from_ok_unseal_reseal():
    resp = audit_exposure(request_with(simple_chain(), 14))
    assert resp.validation.valid is True
    assert len(resp.exposure_intervals) == 1
    interval = resp.exposure_intervals[0]
    assert interval.unseal_index == 1
    assert interval.reseal_index == 2
    assert interval.start == dt(12)
    assert interval.end == dt(13)
    # 无授权窗口：整段暴露
    assert resp.uncovered_seconds == 3600
    assert len(resp.covered) == 0
    assert [(s.start.hour, s.end.hour, s.unseal_index, s.reseal_index)
            for s in resp.uncovered] == [(12, 13, 1, 2)]


def test_last_unseal_without_reseal_extends_to_audit_until():
    events = [
        ev(10, 0, EventType.SEAL, seal_id="S001", custodian="Alice"),
        ev(12, 0, EventType.UNSEAL),
    ]
    resp = audit_exposure(request_with(events, 14))
    interval = resp.exposure_intervals[0]
    assert (interval.unseal_index, interval.reseal_index) == (1, None)
    assert interval.end == dt(14)
    assert resp.uncovered_seconds == 2 * 3600
    assert resp.uncovered[0].reseal_index is None


def simple_chain_dicts():
    """simple_chain 的 JSON 形态，供 API 测试使用。"""
    return [event.model_dump(mode="json") for event in simple_chain()]


def test_violating_unseal_and_reseal_are_ignored():
    # 未封装的 unseal（index 0）与未开封的 reseal（index 4）均为违规，不产生区间
    events = [
        ev(9, 0, EventType.UNSEAL),                                         # 违规：未封装开封
        ev(10, 0, EventType.SEAL, seal_id="S001", custodian="Alice"),
        ev(12, 0, EventType.UNSEAL),                                        # 有效
        ev(12, 30, EventType.RESEAL, seal_id="S002", custodian="Alice"),    # 有效（关闭区间）
        ev(13, 0, EventType.RESEAL, seal_id="S003", custodian="Alice"),     # 违规：已封装重封
    ]
    resp = audit_exposure(request_with(events, 14))
    assert resp.validation.valid is False
    assert [v.index for v in resp.validation.violations] == [0, 4]
    assert len(resp.exposure_intervals) == 1
    interval = resp.exposure_intervals[0]
    assert (interval.unseal_index, interval.reseal_index) == (2, 3)
    assert resp.uncovered_seconds == 1800


def test_multiple_intervals_in_order():
    events = [
        ev(10, 0, EventType.SEAL, seal_id="S001", custodian="Alice"),
        ev(11, 0, EventType.UNSEAL),
        ev(12, 0, EventType.RESEAL, seal_id="S002", custodian="Alice"),
        ev(13, 0, EventType.UNSEAL),
        ev(15, 0, EventType.RESEAL, seal_id="S003", custodian="Alice"),
    ]
    resp = audit_exposure(request_with(events, 16))
    assert [(i.unseal_index, i.reseal_index) for i in resp.exposure_intervals] == [(1, 2), (3, 4)]
    assert resp.uncovered_seconds == 3600 + 2 * 3600


def test_empty_events_yields_empty_audit():
    resp = audit_exposure(request_with([], 12))
    assert resp.exposure_intervals == []
    assert resp.covered == []
    assert resp.uncovered == []
    assert resp.uncovered_seconds == 0
    assert resp.validation.valid is True


# ---------------------------------------------------------------------------
# 窗口合并
# ---------------------------------------------------------------------------


def test_out_of_order_overlapping_and_nested_windows_merge_stably():
    # 乱序、重叠、嵌套、首尾相接
    windows = [
        win(14, 0, 15, 0),    # 独立
        win(12, 20, 12, 40),  # 与下者相交
        win(12, 0, 12, 30),
        win(12, 10, 12, 20),  # 完全嵌套
        win(13, 0, 14, 0),    # 与 14:00-15:00 首尾相接
    ]
    resp = audit_exposure(request_with(simple_chain(), 16, windows=windows))
    merged = [(w.start.hour, w.start.minute, w.end.hour, w.end.minute)
              for w in resp.merged_windows]
    assert merged == [(12, 0, 12, 40), (13, 0, 15, 0)]


def test_touching_windows_merge_and_cover_without_gap():
    windows = [win(11, 30, 12, 20), win(12, 20, 13, 0)]
    resp = audit_exposure(request_with(simple_chain(), 14, windows=windows))
    assert len(resp.merged_windows) == 1
    assert len(resp.covered) == 1
    assert (resp.covered[0].start.hour, resp.covered[0].end.hour) == (12, 13)
    assert resp.covered[0].window_index == 0
    assert resp.uncovered == []
    assert resp.uncovered_seconds == 0


# ---------------------------------------------------------------------------
# 区间差
# ---------------------------------------------------------------------------


def test_window_partially_covers_interval():
    resp = audit_exposure(request_with(simple_chain(), 14, windows=[win(12, 0, 12, 30)]))
    assert len(resp.covered) == 1
    covered = resp.covered[0]
    assert (covered.seconds, covered.window_index) == (1800, 0)
    assert (covered.unseal_index, covered.reseal_index) == (1, 2)
    assert len(resp.uncovered) == 1
    uncovered = resp.uncovered[0]
    assert (uncovered.start.hour, uncovered.end.hour, uncovered.seconds) == (12, 13, 1800)
    assert uncovered.window_index is None
    assert resp.uncovered_seconds == 1800


def test_one_interval_spanning_multiple_windows():
    # 开封区间 10:00-16:00；三段窗口交错，窗口均伸出区间外
    events = [
        ev(9, 0, EventType.SEAL, seal_id="S001", custodian="Alice"),
        ev(10, 0, EventType.UNSEAL),
        ev(16, 0, EventType.RESEAL, seal_id="S002", custodian="Alice"),
    ]
    windows = [win(9, 0, 11, 0), win(12, 0, 13, 0), win(15, 0, 17, 0)]
    resp = audit_exposure(request_with(events, 18, windows=windows))
    covered = [(s.start.hour, s.end.hour, s.seconds, s.window_index) for s in resp.covered]
    assert covered == [(10, 11, 3600, 0), (12, 13, 3600, 1), (15, 16, 3600, 2)]
    uncovered = [(s.start.hour, s.end.hour, s.seconds) for s in resp.uncovered]
    assert uncovered == [(11, 12, 3600), (13, 15, 7200)]
    assert resp.uncovered_seconds == 10800


def test_window_unrelated_to_interval_is_ignored():
    resp = audit_exposure(request_with(simple_chain(), 14, windows=[win(1, 0, 2, 0)]))
    assert resp.covered == []
    assert resp.uncovered_seconds == 3600


def test_window_inside_open_ended_interval():
    events = [
        ev(10, 0, EventType.SEAL, seal_id="S001", custodian="Alice"),
        ev(12, 0, EventType.UNSEAL),
    ]
    resp = audit_exposure(request_with(events, 14, windows=[win(13, 0, 14, 0)]))
    assert [(s.start.hour, s.end.hour) for s in resp.covered] == [(13, 14)]
    assert [(s.start.hour, s.end.hour, s.seconds) for s in resp.uncovered] == [(12, 13, 3600)]


def test_window_with_seconds_precision():
    events = [
        ev(10, 0, EventType.SEAL, seal_id="S001", custodian="Alice"),
        Event(type=EventType.UNSEAL, timestamp=f"{DAY}T12:00:00Z"),
        Event(type=EventType.RESEAL, timestamp=f"{DAY}T12:10:30Z",
              seal_id="S002", custodian="Alice"),
    ]
    req = ExposureAuditRequest(
        events=events,
        audit_until=f"{DAY}T13:00:00Z",
        allowed_windows=[
            AllowedWindow(start=f"{DAY}T12:00:00Z", end=f"{DAY}T12:05:30Z"),
        ],
    )
    resp = audit_exposure(req)
    assert resp.covered[0].seconds == 330
    assert resp.uncovered[0].seconds == 300
    assert resp.uncovered_seconds == 300


# ---------------------------------------------------------------------------
# audit_until 约束
# ---------------------------------------------------------------------------


def test_audit_until_earlier_than_last_valid_event_is_422():
    payload = {
        "events": [
            {"type": "seal", "timestamp": f"{DAY}T10:00:00Z",
             "seal_id": "S001", "custodian": "Alice"},
            {"type": "unseal", "timestamp": f"{DAY}T13:00:00Z"},
        ],
        "audit_until": f"{DAY}T12:00:00Z",
        "allowed_windows": [],
    }
    r = client.post("/api/v1/seal-chain/exposure-audit", json=payload)
    assert r.status_code == 422
    assert "audit_until" in r.json()["detail"]


def test_audit_until_equal_to_last_valid_event_is_ok():
    payload = {
        "events": simple_chain_dicts(),
        "audit_until": f"{DAY}T13:00:00Z",
        "allowed_windows": [],
    }
    r = client.post("/api/v1/seal-chain/exposure-audit", json=payload)
    assert r.status_code == 200
    assert r.json()["uncovered_seconds"] == 3600


def test_audit_until_only_compared_with_valid_events():
    # index 1 为违规事件（已封装状态下重复 seal，时间 15:00），最后有效事件在 10:00
    events = [
        ev(10, 0, EventType.SEAL, seal_id="S001", custodian="Alice"),
        ev(15, 0, EventType.SEAL, seal_id="S009", custodian="Bob"),
    ]
    resp = audit_exposure(request_with(events, 12))
    assert resp.validation.valid is False
    assert resp.exposure_intervals == []


# ---------------------------------------------------------------------------
# 请求校验（422）
# ---------------------------------------------------------------------------


def test_window_start_not_before_end_is_422():
    payload = {
        "events": simple_chain_dicts(),
        "audit_until": f"{DAY}T14:00:00Z",
        "allowed_windows": [
            {"start": f"{DAY}T13:00:00Z", "end": f"{DAY}T12:00:00Z"},
        ],
    }
    r = client.post("/api/v1/seal-chain/exposure-audit", json=payload)
    assert r.status_code == 422


def test_window_start_equal_end_is_422():
    payload = {
        "events": [],
        "audit_until": f"{DAY}T14:00:00Z",
        "allowed_windows": [
            {"start": f"{DAY}T13:00:00Z", "end": f"{DAY}T13:00:00Z"},
        ],
    }
    r = client.post("/api/v1/seal-chain/exposure-audit", json=payload)
    assert r.status_code == 422


def test_mixed_timezone_awareness_is_422():
    payload = {
        "events": [
            {"type": "seal", "timestamp": f"{DAY}T10:00:00Z",
             "seal_id": "S001", "custodian": "Alice"},
            {"type": "unseal", "timestamp": f"{DAY}T12:00:00Z"},
        ],
        "audit_until": f"{DAY}T14:00:00",  # naive，与带时区事件混用
        "allowed_windows": [],
    }
    r = client.post("/api/v1/seal-chain/exposure-audit", json=payload)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# API 集成
# ---------------------------------------------------------------------------


def test_api_preserves_validation_and_returns_segments():
    payload = {
        "events": simple_chain_dicts(),
        "audit_until": f"{DAY}T14:00:00Z",
        "allowed_windows": [
            {"start": f"{DAY}T11:00:00Z", "end": f"{DAY}T12:30:00Z"},
        ],
    }
    r = client.post("/api/v1/seal-chain/exposure-audit", json=payload)
    assert r.status_code == 200
    body = r.json()
    # 原 validation 完整保留
    assert body["validation"]["valid"] is True
    assert body["validation"]["sealed"] is True
    assert body["validation"]["current_seal"] == "S002"
    assert [e["status"] for e in body["validation"]["events"]] == ["ok", "ok", "ok"]
    assert body["audit_until"] == f"{DAY}T14:00:00Z"
    # 窗口伸出区间的部分被裁掉
    assert body["covered"] == [{
        "start": f"{DAY}T12:00:00Z",
        "end": f"{DAY}T12:30:00Z",
        "seconds": 1800,
        "unseal_index": 1,
        "reseal_index": 2,
        "window_index": 0,
    }]
    assert body["uncovered"][0]["seconds"] == 1800
    assert body["uncovered_seconds"] == 1800


def test_api_full_cover_open_ended_interval():
    payload = {
        "events": [
            {"type": "seal", "timestamp": f"{DAY}T10:00:00Z",
             "seal_id": "S001", "custodian": "Alice"},
            {"type": "unseal", "timestamp": f"{DAY}T12:00:00Z"},
        ],
        "audit_until": f"{DAY}T13:00:00Z",
        "allowed_windows": [
            {"start": f"{DAY}T12:00:00Z", "end": f"{DAY}T13:00:00Z"},
        ],
    }
    r = client.post("/api/v1/seal-chain/exposure-audit", json=payload)
    assert r.status_code == 200
    body = r.json()
    interval = body["exposure_intervals"][0]
    assert interval["reseal_index"] is None
    assert len(body["covered"]) == 1
    assert body["covered"][0]["seconds"] == 3600
    assert body["uncovered"] == []
    assert body["uncovered_seconds"] == 0
