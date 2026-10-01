"""推焦机。

推焦机负责炭化室内的推焦行程：先把推焦杆按速度曲线送到行程基线，炭化室内
焦炭被推出后按退杆指令返回。推焦杆速度由自动曲线、安全保护与手动控制台
仲裁；偏离基线时安全保护可降速观察或直接停住，并要求人工确认后恢复。
"""

from __future__ import annotations

from .curve import SpeedCurve, SpeedDecision
from .guard import (
    DIRECT_STOP,
    REDUCE_THEN_JUDGE,
    DeviationDecision,
    TravelGuard,
)
from .machine import EXTENDING, HELD, IDLE, RETRACTING, PusherMachine
from .recovery import HoldIncident, PushRecovery, ResumeConfirmation
from .schedule import OVEN_ORDER, PushSchedule
from .snapshot import PusherSnapshot
from .speed import SpeedArbiter, SpeedOutcome
from .stroke import StrokeBaseline

__all__ = [
    "DIRECT_STOP",
    "EXTENDING",
    "HELD",
    "IDLE",
    "OVEN_ORDER",
    "RETRACTING",
    "REDUCE_THEN_JUDGE",
    "DeviationDecision",
    "HoldIncident",
    "PushRecovery",
    "PushSchedule",
    "PusherMachine",
    "PusherSnapshot",
    "ResumeConfirmation",
    "SpeedArbiter",
    "SpeedCurve",
    "SpeedDecision",
    "SpeedOutcome",
    "StrokeBaseline",
]
