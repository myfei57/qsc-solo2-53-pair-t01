"""推焦分段速度曲线、偏离停住与人工恢复。"""

from __future__ import annotations

from coke.config import default_config
from coke.console.api import Api
from coke.pusher.curve import StrokeSpeedProfile
from coke.pusher.supervisor import DeviationSupervisor


def _inject_overtravel(platform, after_mm: float = 2900.0, bias_mm: float = 40.0) -> None:
    def hook(_dt: float) -> None:
        if platform.pusher.commanded_ram_mm >= after_mm:
            platform.pusher.set_measurement_bias(bias_mm)

    platform.pusher.set_before_advance_hook(hook)


def test_speed_curve_is_slow_fast_then_slow():
    limits = default_config().pusher
    curve = StrokeSpeedProfile(limits.profile, limits.stroke_baseline_mm)
    assert curve.evaluate(0.0).speed_mm_s == 12.0
    assert curve.evaluate(300.0).speed_mm_s == 38.0
    assert curve.evaluate(1600.0).speed_mm_s == 38.0
    assert curve.evaluate(3200.0).speed_mm_s == 14.0
    assert [point["speed_mm_s"] for point in curve.segments()] == [12.0, 38.0, 38.0, 14.0]


def test_direct_stop_policy_stops_on_over_tolerance():
    limits = default_config().pusher
    supervisor = DeviationSupervisor(limits, limits.stroke_baseline_mm, "direct_stop")
    decision = supervisor.evaluate(limits.stroke_baseline_mm + limits.stroke_tolerance_mm + 1.0, 3180.0)
    assert decision.stop is True
    assert decision.speed_mm_s == 0.0
    assert decision.reason == "over_baseline_tolerance"


def test_decelerate_policy_creeps_before_recheck_stop():
    limits = default_config().pusher
    supervisor = DeviationSupervisor(
        limits, limits.stroke_baseline_mm, "decelerate_then_judge"
    )
    first = supervisor.evaluate(limits.stroke_baseline_mm + 40.0, 3180.0)
    second = supervisor.evaluate(limits.stroke_baseline_mm + 40.0, 3180.0)
    assert first.status == "creep"
    assert first.committed_speed_mm_s == limits.profile.creep_speed_mm_s
    assert second.stop is True
    assert second.reason == "over_baseline_tolerance_after_recheck"


def test_push_partial_failure_requires_ack_and_resume_is_idempotent(platform):
    platform.start()
    _inject_overtravel(platform)
    push = platform.push(oven="oven01", deviation_policy="decelerate_then_judge")
    assert push["ok"] is False
    assert push["violations"] == ["stroke_deviation_stop"]
    assert platform.pusher.is_stopped() is True
    assert push["requires_manual_ack"] is True
    assert push["speed_curve"]["deviation_policy"] == "decelerate_then_judge"
    assert any(step["action"] == "speed_profile" for step in push["steps"])
    decisions = [
        step["detail"]
        for step in push["steps"]
        if step["action"] == "speed_decision" and step["detail"].get("deviation_status") in {"creep", "stopped"}
    ]
    assert [item["deviation_status"] for item in decisions] == ["creep", "stopped"]

    try:
        platform.push(oven="oven01")
    except Exception as error:
        assert "人工恢复" in str(error)
    else:  # pragma: no cover
        raise AssertionError("停住后不得直接重新起推")

    ack = platform.acknowledge_push_stop(push["op_id"], "operator01")
    token = ack["recovery"]["resume_token"]
    duplicate_ack = platform.acknowledge_push_stop(push["op_id"], "operator01")
    assert duplicate_ack["idempotent"] is True
    resume = platform.resume_push(push["op_id"], token)
    duplicate = platform.resume_push(push["op_id"], token)
    assert resume["ok"] is True
    assert duplicate["idempotent"] is True
    assert duplicate["ok"] is True
    assert platform.pusher.is_stopped() is False
    assert platform.guide.is_locked() is False
    assert platform.pusher.snapshot().strokes == 1

    actions = [entry["action"] for entry in platform.audit.entries(limit=500)]
    assert "speed_profile" in actions
    assert "speed_decision" in actions
    assert "stroke_deviation_stop" in actions
    assert "push_partial_failure" in actions
    assert "push_stop_acknowledged" in actions
    assert "push_resume_requested" in actions


def test_push_order_is_fixed_to_oven_number_ascending(platform):
    platform.start()
    assert platform.push(oven="oven02")["ok"] is True
    try:
        platform.push(oven="oven01")
    except Exception as error:
        assert "炉号升序" in str(error)
    else:  # pragma: no cover
        raise AssertionError("不得逆炉号起推")
    try:
        platform.push(oven="oven04", crew="A班")
    except Exception as error:
        assert "不按班组" in str(error)
    else:  # pragma: no cover
        raise AssertionError("不得按班组排队")
    assert platform.push(oven="oven04")["ok"] is True
    assert platform.status()["push_schedule"]["policy"] == "oven_number_ascending"


def test_console_exposes_stop_acknowledgement_and_resume(platform):
    platform.start()
    _inject_overtravel(platform)
    api = Api(platform)
    _, push = api.handle(
        "POST",
        "/api/push",
        {},
        {"oven": "oven01", "deviation_policy": "decelerate_then_judge"},
    )
    assert push["ok"] is False
    _, ack = api.handle(
        "POST",
        "/api/push/acknowledge-stop",
        {},
        {"op_id": push["op_id"], "operator": "operator01"},
    )
    token = ack["recovery"]["resume_token"]
    _, resume = api.handle(
        "POST",
        "/api/push/resume",
        {},
        {"op_id": push["op_id"], "resume_token": token},
    )
    assert resume["ok"] is True
    _, status = api.handle("GET", "/api/status", {}, {})
    assert status["speed_curve"]["deviation_policy"] == "direct_stop"
    assert status["push_recovery"]["finished"] is True
    assert status["last_push_decision"]["deviation_status"]
