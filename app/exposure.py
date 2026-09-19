"""开封暴露审计。

处理流程：
1. 复用现有单链校验器（``validate_events``）得到完整 ``validation`` 结果，
   违规事件不参与后续推导；
2. 仅取 ``validation`` 中 ``status = ok`` 的 unseal / reseal 事件，按事件顺序
   配对推导半开开封区间 ``[start, end)``：unseal 开启一段，其后第一个有效
   reseal 关闭该段；末次开封未重封时终点延伸到 ``audit_until``；
3. 将 ``allowed_windows`` 按 start 排序后合并所有相交或首尾相接（``end == start``）
   的窗口，乱序 / 嵌套输入得到稳定结果；
4. 对每段开封区间与合并后窗口做区间差：落在窗口内的部分记为 covered，
   其余记为 uncovered，并累计未授权总秒数；每段保留对应 unseal / reseal
   事件在请求 ``events`` 中的下标。

``audit_until`` 早于最后一个*有效*事件的时间戳时抛 :class:`AuditParameterError`
（路由层映射为 HTTP 422）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from .models import (
    AllowedWindow,
    EventType,
    ExposureAuditRequest,
    ExposureAuditResponse,
    ExposureInterval,
    ExposureSegment,
    ValidateResponse,
)
from .validator import validate_events

_AWARENESS_ERROR = "时间戳时区信息不一致（不能混用带时区与不带时区的时间）"


class AuditParameterError(ValueError):
    """审计参数在通过结构校验之后仍不满足的语义约束（映射为 HTTP 422）。"""


@dataclass(frozen=True)
class _Range:
    """内部使用的半开区间 [start, end)。"""

    start: datetime
    end: datetime


def _cmp(a: datetime, b: datetime) -> int:
    """比较两个时间戳，混用带时区 / 不带时区时间时给出明确错误。"""
    try:
        if a < b:
            return -1
        if a > b:
            return 1
        return 0
    except TypeError:
        raise AuditParameterError(_AWARENESS_ERROR)


def _check_timestamp_awareness(
    audit_until: datetime, events, windows: List[AllowedWindow]
) -> None:
    """audit_until、全部事件与窗口的时间戳必须同为带时区或同为不带时区。"""
    aware = audit_until.tzinfo is not None and audit_until.utcoffset() is not None

    def mismatches(ts: datetime) -> bool:
        ts_aware = ts.tzinfo is not None and ts.utcoffset() is not None
        return ts_aware != aware

    for event in events:
        if mismatches(event.timestamp):
            raise AuditParameterError(_AWARENESS_ERROR)
    for window in windows:
        if mismatches(window.start) or mismatches(window.end):
            raise AuditParameterError(_AWARENESS_ERROR)


def _merge_windows(windows: List[AllowedWindow]) -> List[AllowedWindow]:
    """合并相交或首尾相接的窗口；输入乱序 / 嵌套时结果稳定（按 start 升序）。"""
    try:
        ordered = sorted(windows, key=lambda w: w.start)
    except TypeError:
        raise AuditParameterError(_AWARENESS_ERROR)

    merged: List[AllowedWindow] = []
    for window in ordered:
        if not merged or window.start > merged[-1].end:
            # 与前一段既不相交也不首尾相接，另起一段
            merged.append(window)
        elif window.end > merged[-1].end:
            # 扩展当前段（嵌套窗口 window.end <= 当前 end 时直接吞掉）
            merged[-1] = AllowedWindow(start=merged[-1].start, end=window.end)
    return merged


def _derive_intervals(
    request: ExposureAuditRequest,
) -> Tuple[List[ExposureInterval], Optional[datetime], ValidateResponse]:
    """用 validation 中 status=ok 的 unseal/reseal 配对推导半开开封区间。

    同时返回最后一个有效事件的时间戳（无有效事件时为 None）与完整校验结果。
    """
    validation = validate_events(request.events)

    last_valid_ts: Optional[datetime] = None
    open_unseal: Optional[int] = None  # 当前尚未重封的有效 unseal 事件下标
    intervals: List[ExposureInterval] = []

    for event, result in zip(request.events, validation.events):
        if result.status != "ok":
            continue
        last_valid_ts = event.timestamp
        if event.type is EventType.UNSEAL:
            # 正常状态机下 unseal 必在封装态，不可能连续出现；
            # 防御性地把未关闭的开封段按 audit_until 收口
            if open_unseal is not None:
                intervals.append(
                    ExposureInterval(
                        start=request.events[open_unseal].timestamp,
                        end=request.audit_until,
                        unseal_index=open_unseal,
                        reseal_index=None,
                    )
                )
            open_unseal = result.index
        elif event.type is EventType.RESEAL and open_unseal is not None:
            intervals.append(
                ExposureInterval(
                    start=request.events[open_unseal].timestamp,
                    end=event.timestamp,
                    unseal_index=open_unseal,
                    reseal_index=result.index,
                )
            )
            open_unseal = None

    if open_unseal is not None:
        # 末次开封未重封：延伸到 audit_until
        intervals.append(
            ExposureInterval(
                start=request.events[open_unseal].timestamp,
                end=request.audit_until,
                unseal_index=open_unseal,
                reseal_index=None,
            )
        )

    return intervals, last_valid_ts, validation


def _segment(
    start: datetime,
    end: datetime,
    interval: ExposureInterval,
    window_index: Optional[int],
) -> ExposureSegment:
    return ExposureSegment(
        start=start,
        end=end,
        seconds=max(0, int((end - start).total_seconds())),
        unseal_index=interval.unseal_index,
        reseal_index=interval.reseal_index,
        window_index=window_index,
    )


def _difference(
    interval: ExposureInterval, windows: List[_Range]
) -> Tuple[List[ExposureSegment], List[ExposureSegment]]:
    """单段开封区间 [start, end) 与有序不相交窗口做区间差。

    返回 (covered, uncovered)，分段均按时间升序。允许窗口超出开封区间的部分
    被裁掉；一段开封跨越多个窗口时逐段切分。
    """
    covered: List[ExposureSegment] = []
    uncovered: List[ExposureSegment] = []
    cursor = interval.start

    for window_index, window in enumerate(windows):
        # 窗口完全在开封区间开始之前
        if window.end <= interval.start:
            continue
        # 窗口（及其后窗口，有序保证）完全在开封区间结束之后
        if window.start >= interval.end:
            break

        overlap_start = max(window.start, interval.start)
        overlap_end = min(window.end, interval.end)
        if overlap_end <= overlap_start:
            continue

        # cursor 到窗口重叠起点之间是未授权部分
        if overlap_start > cursor:
            uncovered.append(_segment(cursor, overlap_start, interval, None))
        covered.append(_segment(overlap_start, overlap_end, interval, window_index))
        cursor = overlap_end

    if interval.end > cursor:
        uncovered.append(_segment(cursor, interval.end, interval, None))

    return covered, uncovered


def audit_exposure(request: ExposureAuditRequest) -> ExposureAuditResponse:
    """校验事件链并审计各开封区间相对授权窗口的暴露情况。"""
    _check_timestamp_awareness(request.audit_until, request.events, request.allowed_windows)

    merged = _merge_windows(request.allowed_windows)
    intervals, last_valid_ts, validation = _derive_intervals(request)

    # audit_until 早于最后一个有效事件时间：无法界定审计范围
    if last_valid_ts is not None and _cmp(request.audit_until, last_valid_ts) < 0:
        raise AuditParameterError(
            "audit_until 不能早于最后一个有效事件的时间戳："
            f"{request.audit_until.isoformat()} 早于 {last_valid_ts.isoformat()}"
        )

    window_ranges = [_Range(start=w.start, end=w.end) for w in merged]

    covered: List[ExposureSegment] = []
    uncovered: List[ExposureSegment] = []
    for interval in intervals:
        seg_covered, seg_uncovered = _difference(interval, window_ranges)
        covered.extend(seg_covered)
        uncovered.extend(seg_uncovered)

    return ExposureAuditResponse(
        validation=validation,
        audit_until=request.audit_until,
        exposure_intervals=intervals,
        merged_windows=merged,
        covered=covered,
        uncovered=uncovered,
        uncovered_seconds=sum(segment.seconds for segment in uncovered),
    )
