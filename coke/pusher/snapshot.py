"""推焦机状态快照。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PusherSnapshot:
    """推焦机某一时刻的状态。"""

    state: str
    ram_mm: float
    measured_ram_mm: float
    target_mm: float
    committed_mm_s: float
    strokes: int
    stop_reason: str

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "ram_mm": round(self.ram_mm, 2),
            "measured_ram_mm": round(self.measured_ram_mm, 2),
            "target_mm": round(self.target_mm, 2),
            "committed_mm_s": round(self.committed_mm_s, 2),
            "strokes": self.strokes,
            "stop_reason": self.stop_reason,
        }
