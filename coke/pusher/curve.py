"""按行程分段的推焦速度曲线。

曲线按「起步段线性缓启 → 中段快速推进 → 末段线性收速」计算，速度只依赖
当前杆位与目标基线，不允许控制循环根据上一拍速度自行加速，保证每一拍都可
独立复核。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import PusherProfile


@dataclass(frozen=True)
class CurvePoint:
    """某一杆位对应的速度曲线判定。"""

    position_mm: float
    target_mm: float
    segment: str
    speed_mm_s: float
    remaining_mm: float
    progress_ratio: float

    def as_dict(self) -> dict:
        return {
            "position_mm": round(self.position_mm, 2),
            "target_mm": round(self.target_mm, 2),
            "segment": self.segment,
            "curve_speed_mm_s": round(self.speed_mm_s, 2),
            "remaining_mm": round(self.remaining_mm, 2),
            "progress_ratio": round(self.progress_ratio, 4),
        }


class StrokeSpeedProfile:
    """按杆位返回分段速度。"""

    def __init__(self, profile: PusherProfile, target_mm: float) -> None:
        if target_mm <= 0.0:
            raise ValueError("速度曲线目标必须为正")
        self._profile = profile
        self._target = float(target_mm)
        self._start_end = min(profile.start_distance_mm, target_mm / 2.0)
        self._approach_start = max(0.0, target_mm - profile.approach_distance_mm)
        if self._start_end >= self._approach_start:
            raise ValueError("起步段与收速段重叠")

    @property
    def target_mm(self) -> float:
        return self._target

    def evaluate(self, position_mm: float) -> CurvePoint:
        """计算指定杆位的速度与所在分段。"""

        position = max(0.0, min(float(position_mm), self._target))
        remaining = self._target - position
        ratio = 0.0 if self._target == 0.0 else position / self._target
        if position < self._start_end:
            factor = 0.0 if self._start_end == 0.0 else position / self._start_end
            speed = self._lerp(self._profile.start_speed_mm_s, self._profile.cruise_speed_mm_s, factor)
            segment = "ramp_start"
        elif position < self._approach_start:
            speed = self._profile.cruise_speed_mm_s
            segment = "cruise"
        else:
            span = max(0.0, self._target - self._approach_start)
            factor = 1.0 if span == 0.0 else (position - self._approach_start) / span
            speed = self._lerp(self._profile.cruise_speed_mm_s, self._profile.finish_speed_mm_s, factor)
            segment = "ramp_finish"
        return CurvePoint(position, self._target, segment, speed, remaining, ratio)

    def segments(self) -> list[dict]:
        """返回曲线关键点，供控制台展示和审计。"""

        points = (
            (0.0, self._profile.start_speed_mm_s),
            (self._start_end, self._profile.cruise_speed_mm_s),
            (self._approach_start, self._profile.cruise_speed_mm_s),
            (self._target, self._profile.finish_speed_mm_s),
        )
        return [
            {"position_mm": round(position, 2), "speed_mm_s": round(speed, 2)}
            for position, speed in points
        ]

    @staticmethod
    def _lerp(start: float, end: float, factor: float) -> float:
        return start + (end - start) * max(0.0, min(1.0, factor))
