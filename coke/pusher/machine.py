"""推焦机状态机。

状态只在 ``idle``、``extending``（推焦杆前进）、``retracting``（退杆）和
``stopped``（安全联锁停住，等待人工恢复）之间切换。行程不得超过配置的最大
行程；一次完整“前进到目标 + 返回”记一次推焦行程。推进是按
:meth:`advance` 显式驱动的，控制循环与验证环境都能按固定步长观察杆位变化。
"""

from __future__ import annotations

from typing import Callable

from ..config import PusherLimits
from ..errors import InterlockError
from .snapshot import PusherSnapshot

IDLE = "idle"
EXTENDING = "extending"
RETRACTING = "retracting"
STOPPED = "stopped"


class PusherMachine:
    """推焦杆状态机。"""

    def __init__(self, limits: PusherLimits) -> None:
        self._limits = limits
        self._ram_mm = 0.0
        self._measured_ram_mm = 0.0
        self._measurement_bias_mm = 0.0
        self._target_mm = 0.0
        self._state = IDLE
        self._committed_mm_s = limits.speed_default_mm_s
        self._strokes = 0
        self._stop_reason = ""
        self._before_advance: Callable[[float], None] | None = None

    @property
    def limits(self) -> PusherLimits:
        return self._limits

    @property
    def state(self) -> str:
        return self._state

    @property
    def ram_mm(self) -> float:
        return self._measured_ram_mm

    @property
    def commanded_ram_mm(self) -> float:
        return self._ram_mm

    @property
    def stop_reason(self) -> str:
        return self._stop_reason

    def committed_speed(self) -> float:
        """返回当前提交给执行器的速度。"""

        return self._committed_mm_s

    def set_speed(self, speed_mm_s: float) -> float:
        """把仲裁后的速度提交给执行器，越界即夹到配置区间。"""

        clamped = max(self._limits.speed_min_mm_s, min(self._limits.speed_max_mm_s, float(speed_mm_s)))
        self._committed_mm_s = clamped
        return clamped

    def set_measurement_bias(self, bias_mm: float) -> None:
        """设置测量杆位相对指令杆位的偏差（验证和故障注入用）。"""

        self._measurement_bias_mm = float(bias_mm)
        self._measured_ram_mm = max(0.0, self._ram_mm + self._measurement_bias_mm)

    def set_before_advance_hook(self, hook: Callable[[float], None] | None) -> None:
        """登记每个推进拍之前触发的演练钩子。"""

        self._before_advance = hook

    def drive_to(self, target_mm: float, speed_mm_s: float | None = None) -> float:
        """下达推焦行程指令，返回生效的目标杆位。"""

        if self._state == STOPPED:
            raise InterlockError("推焦杆处于人工确认停住状态，不能直接重新起推", component="pusher")
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
        self._stop_reason = ""
        return target

    def retract(self) -> None:
        """下达退杆指令。"""

        if self._state == STOPPED:
            raise InterlockError("推焦杆处于人工确认停住状态，需先完成恢复流程", component="pusher")
        self._target_mm = 0.0
        self._state = RETRACTING
        self._committed_mm_s = self._limits.speed_default_mm_s

    def advance(self, dt: float) -> bool:
        """按提交速度和步长推进杆位，返回本次是否到达目标。"""

        if self._state == IDLE:
            return True
        if self._state == STOPPED:
            return False
        if dt <= 0:
            raise ValueError("步长必须为正")
        step = self._committed_mm_s * dt
        if self._before_advance is not None:
            self._before_advance(dt)
        remaining = self._target_mm - self._ram_mm
        if abs(remaining) <= step:
            self._ram_mm = self._target_mm
            reached = True
        else:
            self._ram_mm += step if remaining > 0 else -step
            reached = False
        self._measured_ram_mm = max(0.0, self._ram_mm + self._measurement_bias_mm)
        if reached:
            self._state = IDLE
            self._committed_mm_s = 0.0
        return reached

    def stop_hard(self, reason: str) -> None:
        """安全停住并保留杆位、目标和原因。"""

        self._state = STOPPED
        self._committed_mm_s = 0.0
        self._stop_reason = str(reason)

    def complete_stroke(self) -> None:
        """在无超差地停在基线后记一次完整行程。"""

        if self._state != IDLE:
            raise InterlockError("推焦杆未停稳，不能确认完整行程", component="pusher")
        self._strokes += 1
        self._stop_reason = ""

    def reset_for_retry(self, clear_hook: bool = False) -> None:
        """清掉安全停住状态，允许恢复流程重新进退杆。"""

        self._state = IDLE
        self._stop_reason = ""
        self._committed_mm_s = 0.0
        self._measurement_bias_mm = 0.0
        self._measured_ram_mm = self._ram_mm
        if clear_hook:
            self._before_advance = None

    def at_target(self) -> bool:
        """是否停在目标杆位。"""

        return self._state == IDLE and abs(self._ram_mm - self._target_mm) < 1e-9

    def is_stopped(self) -> bool:
        """是否处于需人工确认的安全停住状态。"""

        return self._state == STOPPED

    def snapshot(self) -> PusherSnapshot:
        """返回推焦机状态快照。"""

        return PusherSnapshot(
            state=self._state,
            ram_mm=self._ram_mm,
            measured_ram_mm=self._measured_ram_mm,
            target_mm=self._target_mm,
            committed_mm_s=self._committed_mm_s,
            strokes=self._strokes,
            stop_reason=self._stop_reason,
        )
