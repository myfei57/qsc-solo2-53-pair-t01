"""推焦杆行程偏离判定。

判定只在临近基线时启用，避免起步阶段把“尚未到基线”误判成欠程。越过
``基线 + 公差`` 后，按炉况配置选择直接停住，或先降到爬行速度一拍再复查；
复查仍超差才停住。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import PusherLimits
from .curve import CurvePoint, StrokeSpeedProfile

DIRECT_STOP = "direct_stop"
DECELERATE_THEN_JUDGE = "decelerate_then_judge"


@dataclass(frozen=True)
class DeviationDecision:
    """一次速度曲线与杆位偏离的联合判定。"""

    speed_mm_s: float
    segment: str
    deviation_mm: float
    tolerance_mm: float
    status: str
    reason: str
    stop: bool
    stop_reason: str = ""
    committed_speed_mm_s: float | None = None

    def as_dict(self) -> dict:
        return {
            "curve_speed_mm_s": round(self.speed_mm_s, 2),
            "committed_speed_mm_s": round(self.committed_speed_mm_s or self.speed_mm_s, 2),
            "speed_mm_s": round(self.committed_speed_mm_s or self.speed_mm_s, 2),
            "segment": self.segment,
            "deviation_mm": round(self.deviation_mm, 2),
            "tolerance_mm": round(self.tolerance_mm, 2),
            "deviation_status": self.status,
            "deviation_reason": self.reason,
            "stop_required": self.stop,
            "stop_reason": self.stop_reason,
        }


class DeviationSupervisor:
    """结合分段速度曲线和基线公差给出每一拍动作。"""

    def __init__(
        self,
        limits: PusherLimits,
        target_mm: float,
        policy: str | None = None,
        speed_cap_mm_s: float | None = None,
    ) -> None:
        if limits.profile is None:
            raise ValueError("推焦速度曲线配置缺失")
        selected = policy or limits.profile.deviation_policy
        if selected not in (DIRECT_STOP, DECELERATE_THEN_JUDGE):
            raise ValueError("未知的推焦杆偏离策略")
        self._limits = limits
        self._curve = StrokeSpeedProfile(limits.profile, target_mm)
        self._policy = selected
        self._speed_cap = None if speed_cap_mm_s is None else float(speed_cap_mm_s)
        self._creep_armed = False

    @property
    def policy(self) -> str:
        return self._policy

    @property
    def curve(self) -> StrokeSpeedProfile:
        return self._curve

    def curve_segments(self) -> list[dict]:
        return self._curve.segments()

    def evaluate(self, position_mm: float, commanded_mm: float | None = None) -> DeviationDecision:
        """评估当前杆位；重复调用代表下一控制拍。"""

        commanded = float(position_mm) if commanded_mm is None else float(commanded_mm)
        point = self._curve.evaluate(commanded)
        deviation = float(position_mm) - self._limits.stroke_baseline_mm
        tolerance = self._limits.stroke_tolerance_mm
        if deviation > tolerance:
            return self._over_tolerance(point, deviation, tolerance, commanded)
        self._creep_armed = False
        if abs(deviation) <= tolerance:
            caution = self._limits.profile.caution_band_mm
            if abs(deviation) > caution:
                return self._decision(point, deviation, tolerance, "caution", "within_tolerance_near_edge")
            return self._decision(point, deviation, tolerance, "proceed", "within_tolerance")
        if self._limits.stroke_baseline_mm - position_mm <= tolerance:
            return self._decision(point, deviation, tolerance, "caution", "approaching_baseline")
        return self._decision(point, deviation, tolerance, "proceed", "before_judgement_window")

    def _over_tolerance(
        self, point: CurvePoint, deviation: float, tolerance: float, position_mm: float
    ) -> DeviationDecision:
        reason = "over_baseline_tolerance"
        message = f"杆位偏离基线 {deviation:.2f}mm，超过公差 {tolerance:.2f}mm"
        at_commanded_target = position_mm >= self._curve.target_mm
        if self._policy == DIRECT_STOP or at_commanded_target:
            return self._decision(
                point, deviation, tolerance, "stopped", reason, True, message
            )
        if not self._creep_armed:
            self._creep_armed = True
            return self._decision(
                point,
                deviation,
                tolerance,
                "creep",
                "over_baseline_tolerance_decelerating",
                False,
                speed_mm_s=point.speed_mm_s,
                committed_speed_mm_s=self._limits.profile.creep_speed_mm_s,
            )
        return self._decision(
            point, deviation, tolerance, "stopped", "over_baseline_tolerance_after_recheck", True, message
        )

    def _decision(
        self,
        point: CurvePoint,
        deviation: float,
        tolerance: float,
        status: str,
        reason: str,
        stop: bool = False,
        stop_reason: str = "",
        speed_mm_s: float | None = None,
        committed_speed_mm_s: float | None = None,
    ) -> DeviationDecision:
        selected_speed = point.speed_mm_s if speed_mm_s is None else speed_mm_s
        committed_speed = selected_speed if committed_speed_mm_s is None else committed_speed_mm_s
        if self._speed_cap is not None:
            committed_speed = min(committed_speed, self._speed_cap)
        if stop:
            committed_speed = 0.0
        return DeviationDecision(
            speed_mm_s=selected_speed,
            committed_speed_mm_s=committed_speed,
            segment=point.segment,
            deviation_mm=deviation,
            tolerance_mm=tolerance,
            status=status,
            reason=reason,
            stop=stop,
            stop_reason=stop_reason,
        )
