"""推焦杆偏离基线保护。

保护同时核对两类偏差：外部实测杆位与命令杆位的差值（打滑、卡阻、测量异常），
以及实测杆位与标定基线的差值（过推或未到基线）。炉况策略决定预警时是立即
停，还是先降到低速观察一个周期再判定；硬偏差一律立即停。
"""

from __future__ import annotations

from dataclasses import dataclass

DIRECT_STOP = "direct_stop"
REDUCE_THEN_JUDGE = "reduce_then_judge"
POLICIES = (DIRECT_STOP, REDUCE_THEN_JUDGE)

CONTINUE = "continue"
REDUCE_SPEED = "reduce_speed"
HOLD = "hold"

MEASUREMENT_DEVIATION = "measurement_deviation"
BEYOND_BASELINE = "beyond_baseline"
ENDPOINT_DEVIATION = "endpoint_deviation"


@dataclass(frozen=True)
class DeviationDecision:
    """一次偏离判定结论。"""

    action: str
    reason_code: str
    reason: str
    command_mm: float
    measured_mm: float
    baseline_mm: float
    position_deviation_mm: float
    endpoint_deviation_mm: float
    warning_limit_mm: float
    hard_limit_mm: float
    baseline_tolerance_mm: float
    policy: str
    furnace_condition: str
    speed_limit_mm_s: float | None = None

    @property
    def held(self) -> bool:
        return self.action == HOLD

    def as_dict(self) -> dict:
        payload = {
            "action": self.action,
            "held": self.held,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "command_mm": round(self.command_mm, 2),
            "measured_mm": round(self.measured_mm, 2),
            "baseline_mm": round(self.baseline_mm, 2),
            "position_deviation_mm": round(self.position_deviation_mm, 2),
            "endpoint_deviation_mm": round(self.endpoint_deviation_mm, 2),
            "warning_limit_mm": round(self.warning_limit_mm, 2),
            "hard_limit_mm": round(self.hard_limit_mm, 2),
            "baseline_tolerance_mm": round(self.baseline_tolerance_mm, 2),
            "policy": self.policy,
            "furnace_condition": self.furnace_condition,
        }
        if self.speed_limit_mm_s is not None:
            payload["speed_limit_mm_s"] = round(self.speed_limit_mm_s, 2)
        return payload


class TravelGuard:
    """带炉况策略的杆位偏离仲裁。"""

    def __init__(
        self,
        baseline_mm: float,
        baseline_tolerance_mm: float,
        warning_deviation_mm: float,
        hard_deviation_mm: float,
        reduced_speed_mm_s: float,
    ) -> None:
        if warning_deviation_mm <= 0 or hard_deviation_mm <= 0:
            raise ValueError("偏离限值必须为正")
        if warning_deviation_mm >= hard_deviation_mm:
            raise ValueError("预警偏离限值必须小于硬停限值")
        if reduced_speed_mm_s < 0:
            raise ValueError("观察速度不得为负")
        self._baseline_mm = float(baseline_mm)
        self._tolerance_mm = float(baseline_tolerance_mm)
        self._warning_mm = float(warning_deviation_mm)
        self._hard_mm = float(hard_deviation_mm)
        self._reduced_speed = float(reduced_speed_mm_s)
        self._reduced_pending = False

    @property
    def baseline_mm(self) -> float:
        return self._baseline_mm

    def calibrate(self, baseline_mm: float) -> None:
        """基线重新标定后同步保护基线。"""

        self._baseline_mm = float(baseline_mm)

    def evaluate(
        self,
        *,
        command_mm: float,
        measured_mm: float,
        target_mm: float,
        state: str,
        held: bool,
        policy: str = REDUCE_THEN_JUDGE,
        furnace_condition: str = "normal",
    ) -> DeviationDecision:
        """给出继续、降速观察或安全保持结论。"""

        if policy not in POLICIES:
            raise ValueError(f"未知偏离策略 {policy!r}")
        position_deviation = float(measured_mm) - float(command_mm)
        endpoint_deviation = float(measured_mm) - self._baseline_mm
        common = {
            "command_mm": command_mm,
            "measured_mm": measured_mm,
            "baseline_mm": self._baseline_mm,
            "position_deviation_mm": position_deviation,
            "endpoint_deviation_mm": endpoint_deviation,
            "warning_limit_mm": self._warning_mm,
            "hard_limit_mm": self._hard_mm,
            "baseline_tolerance_mm": self._tolerance_mm,
            "policy": policy,
            "furnace_condition": furnace_condition,
        }

        if held or state == "held":
            self._reduced_pending = False
            return DeviationDecision(
                HOLD,
                "safety_hold_latched",
                "杆位偏离保护已停住，等待人工确认后才能重新起推",
                **common,
            )

        if abs(endpoint_deviation) > self._tolerance_mm and state == "idle" and abs(
            float(target_mm) - self._baseline_mm
        ) <= 1e-9:
            return DeviationDecision(
                HOLD,
                ENDPOINT_DEVIATION,
                f"停住后杆位偏离基线 {endpoint_deviation:.2f}mm，超出 {self._tolerance_mm:.2f}mm，停止并等待处置",
                **common,
            )

        if endpoint_deviation > self._tolerance_mm and measured_mm >= self._baseline_mm:
            return DeviationDecision(
                HOLD,
                BEYOND_BASELINE,
                f"实测杆位已越过基线 {endpoint_deviation:.2f}mm，禁止继续硬推，立即停住",
                **common,
            )

        if abs(position_deviation) > self._hard_mm:
            return DeviationDecision(
                HOLD,
                MEASUREMENT_DEVIATION,
                f"实测杆位与命令位置偏差 {position_deviation:.2f}mm，超过硬停限值 {self._hard_mm:.2f}mm，立即停住",
                **common,
            )

        if abs(position_deviation) > self._warning_mm:
            if policy == DIRECT_STOP:
                return DeviationDecision(
                    HOLD,
                    MEASUREMENT_DEVIATION,
                    f"炉况策略要求直接停：杆位偏差 {position_deviation:.2f}mm 超过预警限值 {self._warning_mm:.2f}mm",
                    **common,
                )
            if not self._reduced_pending:
                self._reduced_pending = True
                return DeviationDecision(
                    REDUCE_SPEED,
                    MEASUREMENT_DEVIATION,
                    f"杆位偏差 {position_deviation:.2f}mm 超预警，先降到 {self._reduced_speed:.2f}mm/s 观察",
                    speed_limit_mm_s=self._reduced_speed,
                    **common,
                )
            return DeviationDecision(
                HOLD,
                MEASUREMENT_DEVIATION,
                f"降速观察后杆位偏差仍为 {position_deviation:.2f}mm，超过预警限值 {self._warning_mm:.2f}mm，停住待确认",
                **common,
            )

        self._reduced_pending = False
        return DeviationDecision(
            CONTINUE,
            "within_tolerance",
            "实测杆位、命令位置和基线均在允许偏差内",
            **common,
        )
