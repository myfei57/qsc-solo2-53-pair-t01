"""推焦顺序：导焦栅对位 → 锁定导焦栅 → 分段速度推焦 → 退杆。"""

from __future__ import annotations

import json
from typing import Callable

from ..guide.machine import GuideCar
from ..pusher.curve import SpeedCurve
from ..pusher.guard import HOLD, REDUCE_SPEED, TravelGuard
from ..pusher.machine import PusherMachine

MAX_STEPS = 2400


def _curve_audit_detail(decision) -> dict:
    detail = decision.as_dict()
    detail["speed_curve_profile"] = json.dumps(decision.profile, ensure_ascii=False, sort_keys=True)
    return detail


def _guard_decision(
    guard: TravelGuard,
    pusher: PusherMachine,
    measured_mm: float,
    target_mm: float,
    policy: str | None,
    furnace_condition: str,
) -> dict:
    decision = guard.evaluate(
        command_mm=pusher.ram_mm,
        measured_mm=measured_mm,
        target_mm=target_mm,
        state=pusher.state,
        held=pusher.is_held(),
        policy=policy or "reduce_then_judge",
        furnace_condition=furnace_condition,
    )
    return decision.as_dict()


def _extend(
    pusher: PusherMachine,
    target_mm: float,
    fallback_speed_mm_s: float,
    dt: float,
    steps: list[dict],
    speed_curve: SpeedCurve | None,
    guard: TravelGuard | None,
    deviation_policy: str | None,
    furnace_condition: str,
    on_measurement: Callable[[float, float], float] | None,
) -> tuple[bool, str | None, dict | None, dict | None]:
    """执行前进行程，返回是否停住及最近一次速度/偏离判定。"""

    last_speed: dict | None = None
    last_decision: dict | None = None
    reduced_observation = False
    last_curve_phase = ""
    guard_audited = False
    result: tuple[bool, str | None, dict | None, dict | None] | None = None
    original_guard_baseline = guard.baseline_mm if guard is not None else None
    if guard is not None:
        guard.calibrate(target_mm)

    try:
        for _ in range(MAX_STEPS):
            if pusher.at_target():
                break
            if speed_curve is None:
                pusher.set_speed(fallback_speed_mm_s)
            elif reduced_observation:
                # 上一周期已降速；本周期用低速完成观察，不能先被曲线拉高速。
                pass
            else:
                speed_decision = speed_curve.evaluate(pusher.ram_mm)
                pusher.set_speed(speed_decision.speed_mm_s)
                last_speed = speed_decision.as_dict()
                if speed_decision.phase != last_curve_phase:
                    steps.append(
                        {
                            "component": "pusher",
                            "action": "speed_curve_decision",
                            "detail": _curve_audit_detail(speed_decision),
                        }
                    )
                    last_curve_phase = speed_decision.phase

            pusher.advance(dt)
            measured = pusher.measured_mm
            if on_measurement is not None:
                measured = pusher.set_measured_position(on_measurement(measured, target_mm))

            if guard is not None:
                decision_payload = _guard_decision(
                    guard, pusher, measured, target_mm, deviation_policy, furnace_condition
                )
                last_decision = decision_payload
                if not guard_audited or decision_payload["action"] != "continue":
                    audit_detail = dict(decision_payload)
                    audit_detail["severity"] = "alarm" if decision_payload["action"] == HOLD else "warning"
                    steps.append(
                        {
                            "component": "pusher",
                            "action": "deviation_check",
                            "detail": audit_detail,
                        }
                    )
                    guard_audited = True
                if decision_payload["action"] == REDUCE_SPEED:
                    pusher.set_speed(float(decision_payload["speed_limit_mm_s"]))
                    reduced_observation = True
                    continue
                if decision_payload["action"] == HOLD:
                    pusher.hold(decision_payload["reason"], "pending")
                    result = (False, decision_payload["reason_code"], last_speed, last_decision)
                    break
                reduced_observation = False

        if result is None and pusher.at_target() and guard is not None:
            decision_payload = _guard_decision(
                guard,
                pusher,
                pusher.measured_mm,
                target_mm,
                deviation_policy,
                furnace_condition,
            )
            last_decision = decision_payload
            if decision_payload["action"] != "continue":
                audit_detail = dict(decision_payload)
                audit_detail["severity"] = "alarm" if decision_payload["action"] == HOLD else "warning"
                steps.append(
                    {
                        "component": "pusher",
                        "action": "deviation_check",
                        "detail": audit_detail,
                    }
                )
            if decision_payload["action"] == HOLD:
                pusher.hold(decision_payload["reason"], "pending")
                result = (False, decision_payload["reason_code"], last_speed, last_decision)

        if result is not None:
            return result
        if not pusher.at_target():
            return False, "stroke_incomplete", last_speed, last_decision
        pusher.complete_extension()
        return True, None, last_speed, last_decision
    finally:
        if guard is not None and original_guard_baseline is not None:
            guard.calibrate(original_guard_baseline)


def push_oven(
    pusher: PusherMachine,
    guide: GuideCar,
    oven: str,
    guide_target_mm: float,
    guide_measured_mm: float,
    stroke_target_mm: float,
    speed_mm_s: float,
    dt: float = 1.0,
    *,
    speed_curve: SpeedCurve | None = None,
    guard: TravelGuard | None = None,
    deviation_policy: str | None = None,
    furnace_condition: str = "normal",
    on_measurement: Callable[[float, float], float] | None = None,
    resume: bool = False,
) -> dict:
    """执行一炉推焦，返回带步骤与违约项的报告。"""

    steps: list[dict] = []
    violations: list[str] = []
    target_mm = float(stroke_target_mm)

    if resume:
        target_mm = pusher.snapshot().target_mm
        steps.append(
            {
                "component": "pusher",
                "action": "resume_extend",
                "detail": {"ram_mm": round(pusher.ram_mm, 2), "target_mm": round(target_mm, 2)},
            }
        )
    else:
        guide.reset()
        alignment = guide.align(oven, guide_target_mm, guide_measured_mm)
        steps.append(
            {
                "component": "guide",
                "action": "align_apron",
                "detail": {**alignment.as_dict(), "tolerance_mm": guide.align_tolerance()},
            }
        )
        if not alignment.ok:
            violations.append("guide_not_aligned")
            return {"oven": oven, "ok": False, "steps": steps, "violations": violations}

        angle = guide.lock_apron()
        steps.append(
            {
                "component": "guide",
                "action": "lock_apron",
                "detail": {"apron_deg": angle, "locked": guide.is_locked()},
            }
        )
        pusher.set_speed(speed_mm_s)
        pusher.drive_to(target_mm)

    ok, violation, last_speed, last_decision = _extend(
        pusher,
        target_mm,
        float(speed_mm_s),
        dt,
        steps,
        speed_curve,
        guard,
        deviation_policy,
        furnace_condition,
        on_measurement,
    )
    steps.append(
        {
            "component": "pusher",
            "action": "extend",
            "detail": {
                "ram_mm": round(pusher.ram_mm, 2),
                "measured_mm": round(pusher.measured_mm, 2),
                "target_mm": round(target_mm, 2),
                "held": pusher.is_held(),
                "hold_reason": pusher.hold_reason(),
                "hold_incident_id": pusher.hold_incident_id(),
            },
        }
    )
    report: dict = {
        "oven": oven,
        "ok": ok,
        "steps": steps,
        "violations": violations,
        "speed_curve_decision": last_speed,
        "deviation_decision": last_decision,
    }
    if not ok:
        violations.append(str(violation or "stroke_incomplete"))
        return report

    pusher.set_speed(speed_mm_s)
    pusher.retract()
    for _ in range(MAX_STEPS):
        if pusher.at_target():
            break
        pusher.advance(dt)
    steps.append({"component": "pusher", "action": "retract", "detail": {"ram_mm": round(pusher.ram_mm, 2)}})

    guide.unlock_apron()
    steps.append(
        {
            "component": "guide",
            "action": "unlock_apron",
            "detail": {"apron_deg": guide.snapshot().apron_deg, "locked": guide.is_locked()},
        }
    )
    return report
