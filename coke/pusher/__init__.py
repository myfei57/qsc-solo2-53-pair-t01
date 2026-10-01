"""推焦机。

推焦机负责炭化室内的推焦行程：先把推焦杆按分段速度曲线送到行程基线，炭化
室内焦炭被推出后按退杆指令返回。曲线和杆位偏离由安全监督器逐拍仲裁，越过
基线公差时进入可恢复停住；自动循环与手动控制台的速度仍经仲裁器收敛。
"""

from __future__ import annotations

from .curve import CurvePoint, StrokeSpeedProfile
from .machine import EXTENDING, IDLE, RETRACTING, STOPPED, PusherMachine
from .recovery import PushRecovery
from .schedule import PushSchedule
from .snapshot import PusherSnapshot
from .speed import SpeedArbiter, SpeedOutcome
from .stroke import StrokeBaseline
from .supervisor import DIRECT_STOP, DECELERATE_THEN_JUDGE, DeviationDecision, DeviationSupervisor

__all__ = [
    "CurvePoint",
    "DIRECT_STOP",
    "DECELERATE_THEN_JUDGE",
    "DeviationDecision",
    "DeviationSupervisor",
    "EXTENDING",
    "IDLE",
    "PusherMachine",
    "PusherSnapshot",
    "PushRecovery",
    "PushSchedule",
    "RETRACTING",
    "STOPPED",
    "SpeedArbiter",
    "SpeedOutcome",
    "StrokeBaseline",
    "StrokeSpeedProfile",
]
