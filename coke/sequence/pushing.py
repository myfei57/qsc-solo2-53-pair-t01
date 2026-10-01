"""推焦顺序：导焦栅对位 → 锁定导焦栅 → 按曲线推焦 → 偏离停住/完成后退杆。"""

from __future__ import annotations

from ..config import PusherLimits
from ..guide.machine import GuideCar
from ..pusher.machine import PusherMachine
from ..pusher.supervisor import DeviationSupervisor

MAX_STEPS = 2400


def _run_extension(
    pusher: PusherMachine,
    supervisor: DeviationSupervisor,
    oven: str,
    target_mm: float,
    dt: float,
) -> tuple[list[dict], list[str], dict]:
    steps: list[dict] = []
    speed_trace: list[dict] = []
    for index in range(MAX_STEPS):
        decision = supervisor.evaluate(pusher.ram_mm, pusher.commanded_ram_mm)
        detail = {
            "oven": oven,
            "tick": index,
            "ram_mm": round(pusher.commanded_ram_mm, 2),
            "measured_ram_mm": round(pusher.ram_mm, 2),
            "target_mm": round(target_mm, 2),
            **decision.as_dict(),
        }
        speed_trace.append(detail)
        steps.append({"component": "pusher", "action": "speed_decision", "detail": detail})
        if decision.stop:
            pusher.stop_hard(decision.stop_reason)
            steps.append(
                {
                    "component": "pusher",
                    "action": "stroke_deviation_stop",
                    "detail": {
                        "oven": oven,
                        "ram_mm": round(pusher.commanded_ram_mm, 2),
                        "measured_ram_mm": round(pusher.ram_mm, 2),
                        "target_mm": round(target_mm, 2),
                        "deviation_mm": round(decision.deviation_mm, 2),
                        "tolerance_mm": round(decision.tolerance_mm, 2),
                        "reason": decision.stop_reason,
                        "requires_manual_ack": True,
                        "partial_failure": True,
                    },
                }
            )
            return steps, ["stroke_deviation_stop"], {"stopped": True, "decision": decision.as_dict()}

        pusher.set_speed(decision.speed_mm_s)
        reached = pusher.advance(dt)
        post = supervisor.evaluate(pusher.ram_mm, pusher.commanded_ram_mm)
        post_detail = {
            "oven": oven,
            "tick": index,
            "ram_mm": round(pusher.commanded_ram_mm, 2),
            "measured_ram_mm": round(pusher.ram_mm, 2),
            "target_mm": round(target_mm, 2),
            **post.as_dict(),
        }
        speed_trace.append(post_detail)
        steps.append({"component": "pusher", "action": "speed_decision", "detail": post_detail})
        if post.stop:
            pusher.stop_hard(post.stop_reason)
            steps.append(
                {
                    "component": "pusher",
                    "action": "stroke_deviation_stop",
                    "detail": {
                        "oven": oven,
                        "measured_ram_mm": round(pusher.ram_mm, 2),
                        "reason": post.stop_reason,
                        "requires_manual_ack": True,
                        "partial_failure": True,
                    },
                }
            )
            return steps, ["stroke_deviation_stop"], {"stopped": True, "decision": post.as_dict()}
        if reached:
            pusher.complete_stroke()
            return steps, [], {"stopped": False, "decision": post.as_dict()}
    return steps, ["stroke_incomplete"], {"stopped": False, "decision": speed_trace[-1] if speed_trace else {}}


def _build_supervisor(
    limits: PusherLimits, target_mm: float, policy: str | None, speed_cap_mm_s: float | None
) -> DeviationSupervisor:
    return DeviationSupervisor(limits, target_mm, policy, speed_cap_mm_s)


def push_oven(
    pusher: PusherMachine,
    guide: GuideCar,
    oven: str,
    guide_target_mm: float,
    guide_measured_mm: float,
    stroke_target_mm: float,
    speed_mm_s: float | None = None,
    dt: float = 1.0,
    *,
    deviation_policy: str | None = None,
    retry: bool = False,
) -> dict:
    """执行一炉推焦，返回带步骤、速度曲线与违约项的报告。"""

    if speed_mm_s is not None:
        # 保留旧调用的速度上限入口：安全曲线速度可以更低，但不得被外部顶高。
        pusher.set_speed(speed_mm_s)
    steps: list[dict] = []
    violations: list[str] = []

    if not retry:
        guide.reset()
        pusher.reset_for_retry()
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
    else:
        pusher.reset_for_retry(clear_hook=True)
        pusher.set_measurement_bias(0.0)

    supervisor = _build_supervisor(pusher.limits, stroke_target_mm, deviation_policy, speed_mm_s)
    curve = {
        "target_mm": round(stroke_target_mm, 2),
        "deviation_policy": supervisor.policy,
        "segments": supervisor.curve_segments(),
    }
    steps.append({"component": "pusher", "action": "speed_profile", "detail": {"oven": oven, **curve}})

    if retry and pusher.commanded_ram_mm > 0.0:
        pusher.set_speed(pusher.limits.speed_default_mm_s)
        pusher.retract()
        for _ in range(MAX_STEPS):
            if pusher.at_target():
                break
            pusher.advance(dt)
        steps.append({"component": "pusher", "action": "recovery_retract", "detail": {"oven": oven, "ram_mm": 0.0}})

    pusher.drive_to(stroke_target_mm)
    extension_steps, extension_violations, extension = _run_extension(
        pusher, supervisor, oven, stroke_target_mm, dt
    )
    steps.extend(extension_steps)
    violations.extend(extension_violations)
    if extension.get("stopped") or "stroke_incomplete" in violations:
        steps.append(
            {
                "component": "pusher",
                "action": "extend",
                "detail": {
                    "ram_mm": round(pusher.commanded_ram_mm, 2),
                    "measured_ram_mm": round(pusher.ram_mm, 2),
                    "target_mm": stroke_target_mm,
                    "stopped": bool(extension.get("stopped")),
                },
            }
        )
        return {
            "oven": oven,
            "ok": False,
            "steps": steps,
            "violations": violations,
            "speed_curve": curve,
            "final_forward_position_mm": pusher.ram_mm,
            "final_commanded_position_mm": pusher.commanded_ram_mm,
            "last_speed_decision": extension.get("decision", {}),
            "requires_manual_ack": bool(extension.get("stopped")),
        }

    final_forward_position_mm = pusher.commanded_ram_mm
    steps.append(
        {
            "component": "pusher",
            "action": "extend",
            "detail": {
                "ram_mm": round(pusher.commanded_ram_mm, 2),
                "measured_ram_mm": round(pusher.ram_mm, 2),
                "target_mm": stroke_target_mm,
                "final_forward_position_mm": round(pusher.commanded_ram_mm, 2),
            },
        }
    )

    pusher.set_speed(pusher.limits.speed_default_mm_s)
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
    return {
        "oven": oven,
        "ok": True,
        "steps": steps,
        "violations": violations,
        "speed_curve": curve,
        "final_forward_position_mm": final_forward_position_mm,
        "final_commanded_position_mm": final_forward_position_mm,
        "last_speed_decision": extension.get("decision", {}),
    }
