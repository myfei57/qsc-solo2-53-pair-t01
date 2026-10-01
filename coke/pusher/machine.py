"""推焦机状态机。

状态只在 ``idle``、``extending``（推焦杆前进）、``retracting``（退杆）之间
切换。行程不得超过配置的最大行程；一次完整"前进到目标 + 返回"记一次推焦
行程。推进是按 :meth:`advance` 显式驱动的，控制循环与验证环境都能按固定
步长观察杆位变化。

安全保持是一种可恢复的部分失败：状态机停在当前杆位并锁定前进指令，只有人工
确认后 :meth:`confirm_resume` 才允许重新起推。
"""

from __future__ import annotations

from ..config import PusherLimits
from ..errors import InterlockError
from .snapshot import PusherSnapshot

IDLE = "idle"
EXTENDING = "extending"
RETRACTING = "retracting"
HELD = "held"


class PusherMachine:
    """推焦杆状态机。"""

    def __init__(self, limits: PusherLimits) -> None:
        self._limits = limits
        self._ram_mm = 0.0
        self._measured_mm = 0.0
        self._target_mm = 0.0
        self._state = IDLE
        self._committed_mm_s = limits.speed_default_mm_s
        self._strokes = 0
        self._held = False
        self._hold_reason = ""
        self._hold_incident_id = ""

    @property
    def limits(self) -> PusherLimits:
        return self._limits

    @property
    def state(self) -> str:
        return self._state

    @property
    def ram_mm(self) -> float:
        return self._ram_mm

    @property
    def measured_mm(self) -> float:
        return self._measured_mm

    def committed_speed(self) -> float:
        """返回当前提交给执行器的速度。"""

        return self._committed_mm_s

    def set_speed(self, speed_mm_s: float) -> float:
        """把仲裁后的速度提交给执行器，正常调速夹到配置区间。"""

        clamped = max(self._limits.speed_min_mm_s, min(self._limits.speed_max_mm_s, float(speed_mm_s)))
        self._committed_mm_s = clamped
        return clamped

    def stop_for_hold(self) -> None:
        """安全保持：直接切断推进速度，不允许正常调速再把速度顶回来。"""

        self._committed_mm_s = 0.0

    def hold(self, reason: str, incident_id: str) -> None:
        """停在当前位置并锁定后续前进动作。"""

        self._held = True
        self._hold_reason = str(reason)
        self._hold_incident_id = str(incident_id)
        self._state = HELD
        self.stop_for_hold()

    def is_held(self) -> bool:
        """是否处于人工确认前的安全保持状态。"""

        return self._held

    def hold_reason(self) -> str:
        """返回最近一次安全保持原因。"""

        return self._hold_reason

    def hold_incident_id(self) -> str:
        """返回最近一次安全保持事件编号。"""

        return self._hold_incident_id

    def confirm_resume(self) -> None:
        """解除人工保持锁定；速度曲线会在重新起推时再次下发。"""

        if not self._held:
            return
        self._held = False
        self._hold_reason = ""
        self._hold_incident_id = ""
        self._state = EXTENDING if self._ram_mm < self._target_mm else IDLE

    def set_measured_position(self, position_mm: float) -> float:
        """写入外部杆位测量值，缺省模拟环境与命令位置一致。"""

        self._measured_mm = float(position_mm)
        return self._measured_mm

    def drive_to(self, target_mm: float, speed_mm_s: float | None = None) -> float:
        """下达推焦行程指令，返回生效的目标杆位。"""

        if self._held:
            raise InterlockError(
                f"推焦杆处于安全保持，必须先人工确认（事件 {self._hold_incident_id}）",
                component="pusher",
            )
        target = float(target_mm)
        if target <= self._ram_mm:
            raise InterlockError(
                f"推焦目标 {target} 必须大于当前杆位 {self._ram_mm}", component="pusher"
            )
        if target > self._limits.max_stroke_mm:
            raise InterlockError(
                f"推焦目标 {target} 超过最大行程 {self._limits.max_stroke_mm}", component="pusher"
            )
        if speed_mm_s is not None:
            self.set_speed(speed_mm_s)
        self._target_mm = target
        self._state = EXTENDING
        return target

    def retract(self) -> None:
        """下达退杆指令。"""

        if self._held:
            raise InterlockError(
                f"安全保持未确认，不能自动退杆（事件 {self._hold_incident_id}）", component="pusher"
            )
        self._target_mm = 0.0
        self._state = RETRACTING

    def advance(self, dt: float) -> bool:
        """按提交速度和步长推进杆位，返回本次是否到达目标。"""

        if self._state in (IDLE, HELD):
            return self._state == IDLE and abs(self._ram_mm - self._target_mm) < 1e-9
        if dt <= 0:
            raise ValueError("步长必须为正")
        step = self._committed_mm_s * dt
        remaining = self._target_mm - self._ram_mm
        if abs(remaining) <= step:
            self._ram_mm = self._target_mm
            self._measured_mm = self._target_mm
            reached = True
        else:
            self._ram_mm += step if remaining > 0 else -step
            self._measured_mm = self._ram_mm
            reached = False
        if reached:
            if self._state == EXTENDING:
                # 前进行程到命令终点后还要由行程保护核对实测杆位；确认无偏离后
                # :meth:`complete_extension` 才计为完整推焦。
                self._state = IDLE
            elif self._state == RETRACTING:
                self._state = IDLE
        return reached

    def complete_extension(self) -> None:
        """末端偏离核对通过后，登记一次完整推焦。"""

        if self._state == IDLE and abs(self._ram_mm - self._target_mm) < 1e-9:
            self._strokes += 1
            self._held = False
            self._hold_reason = ""
            self._hold_incident_id = ""

    def at_target(self) -> bool:
        """是否停在目标杆位。"""

        return self._state == IDLE and abs(self._ram_mm - self._target_mm) < 1e-9

    def snapshot(self) -> PusherSnapshot:
        """返回推焦机状态快照。"""

        return PusherSnapshot(
            state=self._state,
            ram_mm=self._ram_mm,
            measured_mm=self._measured_mm,
            target_mm=self._target_mm,
            committed_mm_s=self._committed_mm_s,
            strokes=self._strokes,
            held=self._held,
            hold_reason=self._hold_reason,
            hold_incident_id=self._hold_incident_id,
        )
