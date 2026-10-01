"""推焦安全保持后的人工确认与幂等恢复。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class HoldIncident:
    """一次杆位偏离安全保持。"""

    incident_id: str
    oven: str
    op_id: str
    reason_code: str
    reason: str
    command_mm: float
    measured_mm: float
    target_mm: float
    policy: str
    furnace_condition: str

    def as_dict(self) -> dict:
        return {
            "incident_id": self.incident_id,
            "oven": self.oven,
            "op_id": self.op_id,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "command_mm": round(self.command_mm, 2),
            "measured_mm": round(self.measured_mm, 2),
            "target_mm": round(self.target_mm, 2),
            "policy": self.policy,
            "furnace_condition": self.furnace_condition,
        }


@dataclass(frozen=True)
class ResumeConfirmation:
    """人工确认记录。"""

    confirmation_id: str
    incident_id: str
    confirmed_by: str

    def as_dict(self) -> dict:
        return {
            "confirmation_id": self.confirmation_id,
            "incident_id": self.incident_id,
            "confirmed_by": self.confirmed_by,
        }


@dataclass
class _ResumeResult:
    confirmation: ResumeConfirmation
    report: dict


@dataclass
class PushRecovery:
    """保存活动保持事件；确认和恢复动作按事件编号幂等。"""

    _incidents: dict[str, HoldIncident] = field(default_factory=dict)
    _active_incident_id: str = ""
    _confirmations: dict[str, ResumeConfirmation] = field(default_factory=dict)
    _results: dict[str, _ResumeResult] = field(default_factory=dict)
    _seq: int = 0

    def open_incident(self, oven: str, op_id: str, decision: dict) -> HoldIncident:
        """登记新的保持事件；同一控制周期重复调用返回同一活动事件。"""

        if self._active_incident_id:
            return self._incidents[self._active_incident_id]
        self._seq += 1
        incident = HoldIncident(
            incident_id=f"hold-{self._seq:04d}",
            oven=oven,
            op_id=op_id,
            reason_code=str(decision.get("reason_code", "unknown")),
            reason=str(decision.get("reason", "杆位偏离")),
            command_mm=float(decision.get("command_mm", 0.0)),
            measured_mm=float(decision.get("measured_mm", 0.0)),
            target_mm=float(decision.get("target_mm", 0.0)),
            policy=str(decision.get("policy", "")),
            furnace_condition=str(decision.get("furnace_condition", "")),
        )
        self._incidents[incident.incident_id] = incident
        self._active_incident_id = incident.incident_id
        return incident

    def active_incident(self) -> HoldIncident | None:
        """返回尚未恢复完成的保持事件。"""

        if not self._active_incident_id:
            return None
        return self._incidents.get(self._active_incident_id)

    def confirm(self, incident_id: str, confirmed_by: str) -> ResumeConfirmation:
        """确认恢复；对同一事件和确认人的重复提交返回同一确认号。"""

        self._require_active(incident_id)
        person = str(confirmed_by).strip()
        if not person:
            raise ValueError("重新起推必须填写确认人")
        existing = self._confirmations.get(incident_id)
        if existing is not None:
            if existing.confirmed_by != person:
                raise ValueError("该保持事件已由其他人员确认，如需重新处置必须先产生新的保持事件")
            return existing
        self._seq += 1
        confirmation = ResumeConfirmation(
            confirmation_id=f"confirm-{self._seq:04d}",
            incident_id=incident_id,
            confirmed_by=person,
        )
        self._confirmations[incident_id] = confirmation
        return confirmation

    def get_confirmation(self, incident_id: str) -> ResumeConfirmation | None:
        """读取某事件的人工确认。"""

        return self._confirmations.get(incident_id)

    def start_resume(self, incident_id: str) -> bool:
        """标记开始执行恢复；重复触发返回 False，避免第二次起推。"""

        self._require_active(incident_id)
        if incident_id not in self._confirmations:
            raise RuntimeError("重新起推前必须有人确认")
        if incident_id in self._results:
            return False
        return True

    def finish_resume(self, incident_id: str, confirmation: ResumeConfirmation, report: dict) -> dict:
        """缓存成功结果；失败则要求重新人工确认后再试。"""

        if incident_id in self._results:
            return self._results[incident_id].report
        result = _ResumeResult(confirmation=confirmation, report=report)
        if report.get("ok"):
            self._results[incident_id] = result
            self._active_incident_id = ""
            return report
        # 恢复后再次停住属于新的处置循环：旧确认不能复用，避免无人确认二次硬推。
        self._confirmations.pop(incident_id, None)
        return report

    def get_result(self, incident_id: str) -> dict | None:
        """读取已执行恢复的缓存结果。"""

        result = self._results.get(incident_id)
        return None if result is None else result.report

    def status(self) -> dict:
        """供控制台展示的恢复状态。"""

        incident = self.active_incident()
        confirmation = None if incident is None else self.get_confirmation(incident.incident_id)
        return {
            "active": incident is not None,
            "incident": None if incident is None else incident.as_dict(),
            "confirmation": None if confirmation is None else confirmation.as_dict(),
            "resume_result": None if incident is None else self.get_result(incident.incident_id),
        }

    def _require_active(self, incident_id: str) -> None:
        incident = self.active_incident()
        if incident is None or incident.incident_id != incident_id:
            raise KeyError(f"未找到活动保持事件 {incident_id!r}")
