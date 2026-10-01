"""推焦速度曲线、偏离保护、人工恢复和固定起推顺序。"""

from __future__ import annotations

import pytest

from coke.config import default_config
from coke.errors import CokeError
from coke.pusher.curve import ACCELERATION, CREEP_END, CRUISE, START, SpeedCurve
from coke.pusher.guard import DIRECT_STOP, HOLD, REDUCE_SPEED, REDUCE_THEN_JUDGE, TravelGuard
from coke.pusher.machine import PusherMachine
from coke.sequence import push_oven
from coke.guide.machine import GuideCar


def test_speed_curve_is_slow_fast_slow_by_stroke():
    limits = default_config().pusher
    curve = SpeedCurve(
        baseline_mm=limits.stroke_baseline_mm,
        creep_start_ratio=limits.creep_start_ratio,
        accel_ratio=limits.accel_ratio,
        decel_ratio=limits.decel_ratio,
        creep_end_ratio=limits.creep_end_ratio,
        creep_start_speed_mm_s=limits.creep_start_speed_mm_s,
        cruise_speed_mm_s=limits.cruise_speed_mm_s,
        creep_end_speed_mm_s=limits.creep_end_speed_mm_s,
        min_speed_mm_s=limits.speed_min_mm_s,
        max_speed_mm_s=limits.speed_max_mm_s,
    )
    assert curve.evaluate(0).phase == START
    assert curve.evaluate(0).speed_mm_s < curve.evaluate(1600).speed_mm_s
    assert curve.evaluate(1600).phase == CRUISE
    assert curve.evaluate(1600).speed_mm_s == limits.cruise_speed_mm_s
    assert curve.evaluate(3100).phase == CREEP_END
    assert curve.evaluate(3100).speed_mm_s < curve.evaluate(1600).speed_mm_s
    assert curve.evaluate(200).phase == ACCELERATION


def test_travel_guard_can_reduce_before_second_judgement():
    guard = TravelGuard(baseline_mm=3200, baseline_tolerance_mm=25, warning_deviation_mm=15, hard_deviation_mm=30, reduced_speed_mm_s=12)
    first = guard.evaluate(
        command_mm=100,
        measured_mm=120,
        target_mm=3200,
        state="extending",
        held=False,
        policy=REDUCE_THEN_JUDGE,
    )
    assert first.action == REDUCE_SPEED
    assert first.speed_limit_mm_s == 12
    second = guard.evaluate(
        command_mm=112,
        measured_mm=132,
        target_mm=3200,
        state="extending",
        held=False,
        policy=REDUCE_THEN_JUDGE,
    )
    assert second.action == HOLD
    assert second.reason_code == "measurement_deviation"


def test_beyond_calibrated_baseline_holds_even_without_hard_slip():
    guard = TravelGuard(3200, 25, 15, 30, 12)
    decision = guard.evaluate(
        command_mm=3200,
        measured_mm=3226,
        target_mm=3200,
        state="extending",
        held=False,
        policy=REDUCE_THEN_JUDGE,
    )
    assert decision.action == HOLD
    assert decision.reason_code == "beyond_baseline"


def test_direct_stop_policy_holds_on_first_warning():
    guard = TravelGuard(3200, 25, 15, 30, 12)
    decision = guard.evaluate(
        command_mm=100,
        measured_mm=120,
        target_mm=3200,
        state="extending",
        held=False,
        policy=DIRECT_STOP,
        furnace_condition="sticky",
    )
    assert decision.action == HOLD
    assert decision.policy == DIRECT_STOP


def test_normal_push_uses_speed_curve_and_completes():
    platform = None
    from coke.app import build_platform
    from coke.clock import ManualClock

    import tempfile

    with tempfile.TemporaryDirectory() as path:
        platform = build_platform(default_config(), clock=ManualClock(), storage_root=path)
        platform.start()
        report = platform.push()
        assert report["ok"] is True
        assert report["speed_curve_decision"]["phase"] == CREEP_END
        assert report["deviation_decision"]["action"] == "continue"
        status = platform.status()
        assert status["speed"]["curve"]["segments"]["start"]["speed_mm_s"] == 14.0


def test_platform_hold_requires_confirmation_and_resume_is_idempotent(platform):
    platform.start()
    held = platform.push(measurement_offset_mm=40)
    assert held["ok"] is False
    assert held["violations"] == ["measurement_deviation"]
    assert platform.pusher.is_held()
    assert platform.pusher.committed_speed() == 0.0

    with pytest.raises(CokeError):
        platform.push()
    with pytest.raises(CokeError):
        platform.resume_push(held["hold_incident"]["incident_id"])

    confirmation = platform.confirm_push_resume(held["hold_incident"]["incident_id"], "operator-a")
    repeated_confirmation = platform.confirm_push_resume(
        held["hold_incident"]["incident_id"], "operator-a"
    )
    assert repeated_confirmation["confirmation_id"] == confirmation["confirmation_id"]
    assert confirmation["confirmed_by"] == "operator-a"
    recovered = platform.resume_push(held["hold_incident"]["incident_id"], measurement_offset_mm=0)
    assert recovered["ok"] is True
    repeated = platform.resume_push(held["hold_incident"]["incident_id"])
    assert repeated["op_id"] == recovered["op_id"]
    assert platform.pusher.snapshot().strokes == 1

    actions = {entry["action"] for entry in platform.audit.entries(1000)}
    assert "speed_curve_decision" in actions
    assert "deviation_check" in actions
    assert "push_held" in actions
    deviation_checks = [
        entry for entry in platform.audit.entries(1000) if entry["action"] == "deviation_check"
    ]
    assert deviation_checks and deviation_checks[0]["severity"] == "alarm"
    assert "push_resume_confirmed" in actions
    assert "push_resume_complete" in actions


def test_furnace_condition_selects_direct_stop(platform):
    platform.start()
    report = platform.push(furnace_condition="sticky", measurement_offset_mm=20)
    assert report["deviation_policy"] == DIRECT_STOP
    assert report["deviation_decision"]["action"] == HOLD


def test_push_order_is_fixed_by_oven_number_and_ignores_crew(platform):
    plan = platform.push_order_plan(["oven10", "oven2", "oven03", "oven2"], crew="crew-b")
    assert plan["ovens"] == ["oven2", "oven03", "oven10"]
    assert plan["start_push_order"] == "oven_number_ascending"
    assert plan["crew_order_ignored"] is True
    assert any(entry["action"] == "push_order_planned" for entry in platform.audit.entries(100))


def test_push_batch_executes_each_unique_oven_in_ascending_order(platform):
    platform.start()
    batch = platform.push_batch(["oven10", "oven02", "oven2"], crew="crew-b", dt=1.0)
    assert batch["ok"] is True
    assert [item["oven"] for item in batch["results"]] == ["oven02", "oven2", "oven10"]
    assert platform.status()["push_order"]["start_push_order"] == "oven_number_ascending"


def test_sequence_keeps_backward_constant_speed_interface():
    config = default_config()
    pusher = PusherMachine(config.pusher)
    guide = GuideCar(config.guide)
    report = push_oven(pusher, guide, "oven01", 0.0, 0.0, 3200.0, 24.0)
    assert report["ok"] is True
    assert report["speed_curve_decision"] is None
    assert report["deviation_decision"] is None
