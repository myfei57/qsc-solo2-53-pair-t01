"""按行程分段的推焦速度曲线。

曲线固定为「起步缓行 → 线性加速 → 中段快推 → 线性减速 → 临近基线缓行收
尾」。判定只依赖当前杆位和基线，不依赖上一周期状态，因此控制循环重复计算
或审计回放都会得到同一结论。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import ConfigError

START = "start"
ACCELERATION = "acceleration"
CRUISE = "cruise"
DECELERATION = "deceleration"
CREEP_END = "creep_end"
COMPLETE = "complete"


@dataclass(frozen=True)
class SpeedCurve:
    """按基线行程比例配置的五段速度曲线。"""

    baseline_mm: float
    creep_start_ratio: float
    accel_ratio: float
    decel_ratio: float
    creep_end_ratio: float
    creep_start_speed_mm_s: float
    cruise_speed_mm_s: float
    creep_end_speed_mm_s: float
    min_speed_mm_s: float
    max_speed_mm_s: float

    def __post_init__(self) -> None:
        if self.baseline_mm <= 0:
            raise ConfigError("速度曲线基线必须为正", component="pusher")
        ratios = (
            self.creep_start_ratio,
            self.accel_ratio,
            self.decel_ratio,
            self.creep_end_ratio,
        )
        if any(value < 0.0 for value in ratios):
            raise ConfigError("速度曲线行程比例不得为负", component="pusher")
        if self.creep_start_ratio + self.accel_ratio + self.decel_ratio + self.creep_end_ratio >= 1.0:
            raise ConfigError("速度曲线分段比例之和必须小于 1，需保留中段快推行程", component="pusher")
        speeds = (
            self.creep_start_speed_mm_s,
            self.cruise_speed_mm_s,
            self.creep_end_speed_mm_s,
        )
        if any(value < 0.0 for value in speeds):
            raise ConfigError("速度曲线速度不得为负", component="pusher")
        if not self.min_speed_mm_s <= self.creep_start_speed_mm_s <= self.max_speed_mm_s:
            raise ConfigError("起步缓行速度必须落在推焦速度范围内", component="pusher")
        if not self.min_speed_mm_s <= self.cruise_speed_mm_s <= self.max_speed_mm_s:
            raise ConfigError("中段快推速度必须落在推焦速度范围内", component="pusher")
        if not self.min_speed_mm_s <= self.creep_end_speed_mm_s <= self.max_speed_mm_s:
            raise ConfigError("末端缓行速度必须落在推焦速度范围内", component="pusher")
        if self.cruise_speed_mm_s <= self.creep_start_speed_mm_s:
            raise ConfigError("中段快推速度必须高于起步缓行速度", component="pusher")
        if self.cruise_speed_mm_s <= self.creep_end_speed_mm_s:
            raise ConfigError("中段快推速度必须高于末端缓行速度", component="pusher")

    @property
    def acceleration_end_mm(self) -> float:
        return self.baseline_mm * (self.creep_start_ratio + self.accel_ratio)

    @property
    def deceleration_start_mm(self) -> float:
        return self.baseline_mm * (1.0 - self.decel_ratio - self.creep_end_ratio)

    @property
    def creep_end_start_mm(self) -> float:
        return self.baseline_mm * (1.0 - self.creep_end_ratio)

    def evaluate(self, position_mm: float) -> "SpeedDecision":
        """返回当前杆位对应的分段、速度和曲线判定原因。"""

        position = float(position_mm)
        if position <= 0.0:
            phase = START
            speed = self.creep_start_speed_mm_s
            reason = "起步段缓行，防止瞬时大推力挤碎焦炭"
        elif position < self.acceleration_end_mm:
            phase = ACCELERATION
            begin = self.baseline_mm * self.creep_start_ratio
            span = max(self.acceleration_end_mm - begin, 1e-9)
            factor = min(1.0, max(0.0, (position - begin) / span))
            speed = self.creep_start_speed_mm_s + factor * (
                self.cruise_speed_mm_s - self.creep_start_speed_mm_s
            )
            reason = "加速段，由起步速度线性升至中段快推速度"
        elif position < self.deceleration_start_mm:
            phase = CRUISE
            speed = self.cruise_speed_mm_s
            reason = "中段快推，保持受控最高效率"
        elif position < self.creep_end_start_mm:
            phase = DECELERATION
            span = max(self.creep_end_start_mm - self.deceleration_start_mm, 1e-9)
            factor = min(1.0, max(0.0, (position - self.deceleration_start_mm) / span))
            speed = self.cruise_speed_mm_s + factor * (
                self.creep_end_speed_mm_s - self.cruise_speed_mm_s
            )
            reason = "减速段，临近基线前线性收速度"
        elif position < self.baseline_mm:
            phase = CREEP_END
            speed = self.creep_end_speed_mm_s
            reason = "末端缓行，防止到位冲击拉裂炉头"
        else:
            phase = COMPLETE
            speed = self.creep_end_speed_mm_s
            reason = "已到基线，曲线不再给出前进速度"
        return SpeedDecision(
            phase=phase,
            speed_mm_s=round(max(self.min_speed_mm_s, min(self.max_speed_mm_s, speed)), 3),
            position_mm=round(position, 3),
            baseline_mm=round(self.baseline_mm, 3),
            reason=reason,
            profile=self.as_dict(),
        )

    def as_dict(self) -> dict:
        return {
            "baseline_mm": round(self.baseline_mm, 3),
            "segments": {
                START: {
                    "end_mm": round(self.baseline_mm * self.creep_start_ratio, 3),
                    "speed_mm_s": round(self.creep_start_speed_mm_s, 3),
                },
                ACCELERATION: {"end_mm": round(self.acceleration_end_mm, 3)},
                CRUISE: {"end_mm": round(self.deceleration_start_mm, 3)},
                DECELERATION: {"end_mm": round(self.creep_end_start_mm, 3)},
                CREEP_END: {
                    "end_mm": round(self.baseline_mm, 3),
                    "speed_mm_s": round(self.creep_end_speed_mm_s, 3),
                },
            },
            "cruise_speed_mm_s": round(self.cruise_speed_mm_s, 3),
        }


@dataclass(frozen=True)
class SpeedDecision:
    """一次速度曲线判定结果。"""

    phase: str
    speed_mm_s: float
    position_mm: float
    baseline_mm: float
    reason: str
    profile: dict

    def as_dict(self) -> dict:
        return {
            "phase": self.phase,
            "speed_mm_s": round(self.speed_mm_s, 2),
            "position_mm": round(self.position_mm, 2),
            "baseline_mm": round(self.baseline_mm, 2),
            "reason": self.reason,
            "curve": self.profile,
        }
