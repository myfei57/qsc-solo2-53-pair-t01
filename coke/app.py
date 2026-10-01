"""平台装配与控制循环。

``Platform`` 把各组件按真实调用链接起来，并暴露四件事：操作编排（装煤、
推焦、熄焦、换向、炉门清扫）、控制循环、并发仲裁探测和状态快照。控制循环
每个周期做三件事：把集气管压力调向目标带、按推焦联动时序驱动除尘挡板、
采集趋势；换向与推焦这类跨模块动作都由编排一次性走完并写审计。
"""

from __future__ import annotations

import dataclasses
import threading

from .audit.log import AuditLog
from .audit.record import AuditRecord
from .charger.machine import ChargingCar
from .clock import Clock, SystemClock
from .config import PUSHER_DEVIATION_POLICIES, PlatformConfig, default_config
from .door.latch import DoorLatch
from .door.machine import DoorCleaner
from .dust.damper import DustDamper
from .dust.schedule import DamperSchedule
from .dust.snapshot import DustSnapshot
from .dust.system import DustSystem
from .errors import CokeError, InterlockError
from .eventbus import Event, EventBus
from .gas.collector import CollectorMain
from .gas.flap import BridgeFlap
from .gas.latch import FlapLatch
from .guide.machine import GuideCar
from .heating.machine import HeatingReversal
from .heating.snapshot import HeatingSnapshot
from .heating.temperature import OvenTemperature
from .heating.valvetrain import ValveTrain
from .ids import IdFactory
from .ns.registry import NamespaceRegistry
from .ns.topology import Topology, build_default_topology
from .pusher.machine import PusherMachine
from .pusher.recovery import PushRecovery
from .pusher.schedule import POLICY as PUSH_ORDER_POLICY, PushSchedule
from .pusher.speed import SpeedArbiter
from .pusher.stroke import StrokeBaseline
from .quench.machine import QuenchCar
from .sequence import charge_oven, clean_door, push_oven, quench_oven, reverse_heating
from .store.journal import JsonlJournal
from .store.jsonfile import JsonStore
from .telemetry import TrendBuffer

DEFAULT_UNIT = "battery1"
DEFAULT_OVEN = "oven01"


class Platform:
    """焦炉推装熄顺序控制平台。"""

    def __init__(
        self,
        config: PlatformConfig | None = None,
        clock: Clock | None = None,
        unit: str = DEFAULT_UNIT,
    ) -> None:
        self.config = config or default_config()
        self.clock = clock or SystemClock()
        self.bus = EventBus()
        self.unit = unit

        store = JsonStore(self.config.storage_root)
        self.store = store
        self.audit = AuditLog(self.clock, self.bus, JsonlJournal(store.root, "audit", segment_limit=512))
        self.namespaces = NamespaceRegistry(unit)
        self.topology: Topology = build_default_topology()
        self.trend = TrendBuffer(period_s=5.0)
        self.ids = IdFactory("op")

        self.pusher = PusherMachine(self.config.pusher)
        self.stroke = StrokeBaseline(
            self.config.pusher.stroke_baseline_mm, self.config.pusher.stroke_tolerance_mm
        )
        self.speed = SpeedArbiter(
            self.config.pusher.speed_min_mm_s,
            self.config.pusher.speed_max_mm_s,
            self.config.pusher.speed_default_mm_s,
        )
        self.push_recovery = PushRecovery(store)
        self.push_schedule = PushSchedule(store)
        self._last_push_decision: dict = {}
        self.guide = GuideCar(self.config.guide)
        self.charger = ChargingCar(self.config.charger)
        self.quench_car = QuenchCar(self.config.quench)
        self.collector = CollectorMain(self.config.collector)
        self.flap = BridgeFlap(FlapLatch())
        self.door = DoorCleaner(self.config.door, DoorLatch(self.config.door.latch_travel_mm))
        self.reversal = HeatingReversal(self.config.heating, self.clock)
        self.valves = ValveTrain()
        self.temperature = OvenTemperature(self.config.heating)
        self.dust = DustSystem()
        self.damper = DustDamper(self.config.dust.damper_open_deg, self.config.dust.damper_rate_deg_per_s)
        self.schedule = DamperSchedule(self.config.dust.linkage_lag_s)

        self._cycles = 0
        self._operations = 0
        self._lock = threading.RLock()
        self._register_namespaces()
        self._restore()

    def _register_namespaces(self) -> None:
        """登记现场设备命名空间（控制台与审计共用同一套名字）。"""

        for area, device, kind in (
            ("battery", "pusher", "pusher-machine"),
            ("battery", "guide", "guide-car"),
            ("battery", "charger", "charging-car"),
            ("battery", "quench", "quench-car"),
            ("battery", "collector", "collector-main"),
            ("battery", "bridge_flap", "bridge-flap"),
            ("battery", "door", "door-cleaner"),
            ("battery", "reversal", "reversing-valve"),
            ("battery", "dust", "dust-damper"),
        ):
            self.namespaces.register(area, device, kind)

    def _restore(self) -> None:
        """从落盘审计回放最后一次换向，恢复换向间隔计时。"""

        try:
            records = self.audit.to_records(self.audit.replay_from_journal(limit=100_000))
        except CokeError:
            return
        reversals = [item for item in records if item.action == "reverse_complete"]
        # 只接受不晚于当前时钟的回放记录；可推进时钟重置后，上一轮留下的未来
        # 时间戳属于过期状态，不能拿它把换向间隔计时顶到当前时刻之前。
        if reversals and reversals[-1].ts <= self.clock.now():
            self.reversal.mark_reversed(reversals[-1].ts)
        state = self.store.read("platform_state")
        if isinstance(state, dict):
            self._operations = int(state.get("operations", 0))
            self._cycles = int(state.get("cycles", 0))
        recovery = self.push_recovery.state()
        if recovery.get("active"):
            self.pusher.stop_hard(str(recovery.get("stop_reason", "stroke_deviation_stop")))
            self._last_push_decision = {
                "deviation_status": "stopped",
                "deviation_reason": recovery.get("stop_reason", "stroke_deviation_stop"),
                "stop_required": True,
                "position_mm": recovery.get("stopped_position_mm"),
            }

    def _persist(self) -> None:
        """落盘平台计数，供重启后对照。"""

        self.store.write(
            "platform_state",
            {"operations": self._operations, "cycles": self._cycles, "unit": self.unit},
        )

    def _record_operation(self, op_id: str, operation: str, report: dict) -> dict:
        """把编排步骤逐条写审计，并记一条汇总事件。"""

        oven = str(report.get("oven", ""))
        for step in report.get("steps", []):
            detail = {
                key: value
                for key, value in step.get("detail", {}).items()
                if key not in ("oven", "op_id", "severity", "message")
            }
            self.bus.publish(
                Event.of(
                    step["component"],
                    step["component"],
                    step["action"],
                    oven=oven,
                    op_id=op_id,
                    severity="notice",
                    **detail,
                )
            )
        for violation in report.get("violations", []):
            self.bus.publish(
                Event.of(
                    "audit",
                    "audit",
                    "interlock_rejected",
                    oven=oven,
                    op_id=op_id,
                    severity="alarm",
                    message=f"{operation} 被拒绝：{violation}",
                    violation=violation,
                )
            )
        summary = {
            "op_id": op_id,
            "operation": operation,
            "oven": oven,
            "ok": bool(report.get("ok")),
            "violations": list(report.get("violations", [])),
        }
        self.bus.publish(
            Event.of(
                "audit",
                "audit",
                f"{operation}_complete",
                severity="notice" if report.get("ok") else "warning",
                message=f"{operation} 完成" if report.get("ok") else f"{operation} 未完成",
                **summary,
            )
        )
        return summary

    def start(self) -> dict:
        """启动平台：启动除尘风机并核对拓扑。"""

        with self._lock:
            self.dust.start()
            report = self.topology.verify(set())
            return {
                "fan_running": self.dust.is_running(),
                "topology_missing": report["missing"],
                "operations": self._operations,
            }

    def stop(self) -> dict:
        """停运平台：停除尘风机。"""

        with self._lock:
            self.dust.stop()
            return {"fan_running": self.dust.is_running(), "operations": self._operations}

    def charge(
        self,
        oven: str = DEFAULT_OVEN,
        hopper_target_mm: float = 0.0,
        hopper_measured_mm: float = 0.0,
        dt: float = 1.0,
    ) -> dict:
        """装煤编排：煤斗对位 → 调压 → 开翻板 → 给煤。"""

        with self._lock:
            op_id = self.ids.next()
            report = charge_oven(
                self.charger, self.collector, self.flap, oven, hopper_target_mm, hopper_measured_mm, dt
            )
            summary = self._record_operation(op_id, "charge", report)
            summary["op_id"] = op_id
            self._operations += 1
            self._persist()
            return {**report, **summary}

    def push(
        self,
        oven: str = DEFAULT_OVEN,
        guide_target_mm: float = 0.0,
        guide_measured_mm: float = 0.0,
        stroke_target_mm: float | None = None,
        speed_mm_s: float | None = None,
        dt: float = 1.0,
        deviation_policy: str | None = None,
        crew: str | None = None,
    ) -> dict:
        """推焦编排：导焦栅对位 → 锁定 → 曲线推焦 → 退杆；同时触发除尘联动。"""

        with self._lock:
            self.push_recovery.require_clear()
            if self.pusher.is_stopped():
                raise InterlockError("推焦杆处于停住状态，必须走人工恢复接口", component="pusher")
            op_id = self.ids.next()
            target = self.stroke.baseline_mm if stroke_target_mm is None else stroke_target_mm
            policy = deviation_policy or self.config.pusher.profile.deviation_policy
            if policy not in PUSHER_DEVIATION_POLICIES:
                raise InterlockError("推焦杆偏离策略必须是 direct_stop 或 decelerate_then_judge", component="pusher")
            self.push_schedule.begin(oven, crew=crew)
            if speed_mm_s is not None:
                self.speed.request("auto_cycle", speed_mm_s, priority=50)
                arbitration = self.speed.resolve()
                profile_speed_cap = arbitration.speed_mm_s
            else:
                profile_speed_cap = None
            self.schedule.start_link(self.clock.now())
            try:
                report = push_oven(
                    self.pusher,
                    self.guide,
                    oven,
                    guide_target_mm,
                    guide_measured_mm,
                    target,
                    profile_speed_cap,
                    dt,
                    deviation_policy=policy,
                )
            except Exception:
                self.push_schedule.fail_open(oven)
                raise
            stroke_position = report.get("final_forward_position_mm", self.pusher.ram_mm)
            stroke_check = self.stroke.check(stroke_position)
            self._last_push_decision = report.get("last_speed_decision", {})
            report["speed_arbitration"] = (
                {
                    "winner": "stroke_profile",
                    "speed_mm_s": round(self.pusher.committed_speed(), 2),
                    "priority": 100,
                    "contenders": 1,
                }
                if speed_mm_s is None
                else arbitration.as_dict()
            )
            report["stroke_check"] = stroke_check
            report["push_order_policy"] = PUSH_ORDER_POLICY
            summary = self._record_operation(op_id, "push", report)
            summary["op_id"] = op_id
            if report.get("requires_manual_ack"):
                stop_decision = report.get("last_speed_decision", {})
                reason = stop_decision.get("stop_reason") or report.get("violations", ["stroke_deviation_stop"])[-1]
                self.push_recovery.create(
                    op_id=op_id,
                    oven=oven,
                    target_mm=target,
                    dt=dt,
                    policy=policy,
                    position_mm=report.get("final_forward_position_mm", self.pusher.ram_mm),
                    reason=reason,
                )
                self.push_schedule.fail_open(oven)
                self.bus.publish(
                    Event.of(
                        "pusher",
                        "pusher",
                        "push_partial_failure",
                        oven=oven,
                        op_id=op_id,
                        severity="alarm",
                        message=f"推焦杆已停住：{reason}，需人工确认后恢复",
                        reason=report.get("violations", ["stroke_deviation_stop"])[-1],
                        stop_reason=reason,
                        position_mm=round(self.pusher.ram_mm, 2),
                        requires_manual_ack=True,
                    )
                )
            elif report["ok"]:
                self.push_schedule.complete(oven)
            elif report.get("violations") == ["guide_not_aligned"]:
                self.push_schedule.release(oven)
            else:
                self.push_schedule.fail_open(oven)
            self._operations += 1
            self._persist()
            return {**report, **summary}

    def acknowledge_push_stop(self, op_id: str, operator: str) -> dict:
        """人工确认安全停住原因，发放重新起推令牌。"""

        with self._lock:
            state = self.push_recovery.state()
            if not state.get("active"):
                raise InterlockError("没有待确认的推焦停住行程", component="pusher")
            already = bool(state.get("acknowledged")) and state.get("op_id") == op_id
            acknowledged = self.push_recovery.acknowledge(op_id, operator)
            if not already:
                self.bus.publish(
                    Event.of(
                        "pusher",
                        "pusher",
                        "push_stop_acknowledged",
                        oven=acknowledged["oven"],
                        op_id=op_id,
                        severity="warning",
                        message=f"{operator} 已确认推焦停住原因，允许重新起推",
                        operator=operator,
                        resume_token=acknowledged["resume_token"],
                    )
                )
            return {"ok": True, "idempotent": already, "recovery": acknowledged}

    def resume_push(self, op_id: str, resume_token: str) -> dict:
        """按恢复令牌重新退杆并起推；同令牌重复触发不重复执行。"""

        with self._lock:
            claim = self.push_recovery.request_resume(op_id, resume_token)
            state = claim["state"]
            if claim["idempotent"]:
                return {
                    "ok": True,
                    "idempotent": True,
                    "recovery": state,
                    **(state.get("last_report") or {}),
                }
            self.bus.publish(
                Event.of(
                    "pusher",
                    "pusher",
                    "push_resume_requested",
                    oven=state["oven"],
                    op_id=op_id,
                    severity="notice",
                    message="已按人工确认令牌重新起推",
                    resume_token=resume_token,
                )
            )
            try:
                report = push_oven(
                    self.pusher,
                    self.guide,
                    state["oven"],
                    0.0,
                    0.0,
                    float(state["target_mm"]),
                    None,
                    float(state["dt"]),
                    deviation_policy=state["deviation_policy"],
                    retry=True,
                )
            except Exception:
                failed = self.push_recovery.state()
                failed["resuming"] = False
                self.push_recovery.finish_resume(op_id, {"ok": False, "error": "resume_failed"}, False)
                self._persist()
                raise
            recovery_state = self.push_recovery.finish_resume(op_id, report, report["ok"])
            summary = self._record_operation(op_id, "push_resume", report)
            summary["op_id"] = op_id
            if report["ok"]:
                self.push_schedule.complete(state["oven"])
            else:
                self.push_schedule.fail_open(state["oven"])
            self._persist()
            return {**report, **summary, "idempotent": False, "recovery": recovery_state}

    def quench(self, oven: str = DEFAULT_OVEN, dt: float = 1.0) -> dict:
        """熄焦编排：接焦 → 走行 → 喷淋。"""

        with self._lock:
            op_id = self.ids.next()
            report = quench_oven(self.quench_car, oven, dt)
            report["travel_eta_s"] = self.quench_car.plan.eta_s()
            summary = self._record_operation(op_id, "quench", report)
            summary["op_id"] = op_id
            self._operations += 1
            self._persist()
            return {**report, **summary}

    def reverse(self, oven: str = DEFAULT_OVEN, temperature_c: float | None = None) -> dict:
        """换向编排：废气阀 → 煤气阀 → 炉温确认。"""

        with self._lock:
            op_id = self.ids.next()
            target_temp = self.config.heating.temp_target_c if temperature_c is None else temperature_c
            report = reverse_heating(self.reversal, self.valves, self.temperature, oven, target_temp)
            report["offset_c"] = self.temperature.offset_c()
            summary = self._record_operation(op_id, "reverse", report)
            summary["op_id"] = op_id
            self._operations += 1
            self._persist()
            return {**report, **summary}

    def clean(self, oven: str = DEFAULT_OVEN, dt: float = 1.0) -> dict:
        """炉门清扫编排：松开闩锁 → 开炉门 → 清扫 → 复位闩锁。"""

        with self._lock:
            op_id = self.ids.next()
            report = clean_door(self.door, oven, dt)
            summary = self._record_operation(op_id, "clean", report)
            summary["op_id"] = op_id
            self._operations += 1
            self._persist()
            return {**report, **summary}

    def cycle(self, dt: float = 1.0) -> dict:
        """推进一个控制周期，返回本周期摘要。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        with self._lock:
            self._cycles += 1
            moment = self.clock.now()
            before = self.collector.pressure_pa
            pressure = self.collector.regulate(dt)
            linkage = self.schedule.due(moment)
            target_deg = self.damper.open_deg if self.schedule.links() else 0.0
            angle = self.damper.drive_to(target_deg, dt)
            if self.damper.at_open():
                self.schedule.clear()
            self.trend.append("collector_pressure", moment, pressure)
            self.trend.append("damper_angle", moment, angle)
            self._persist()
            return {
                "cycle": self._cycles,
                "pressure_pa": round(pressure, 2),
                "pressure_delta": round(pressure - before, 3),
                "settled": self.collector.settled(),
                "damper_deg": round(angle, 2),
                "linkage_due": linkage,
                "linkage_elapsed_s": round(self.schedule.elapsed_s(moment), 2),
            }

    def concurrency_probe(self, rounds: int = 24) -> dict:
        """用真实并行调用探测推焦速度仲裁的收敛结果。

        两个线程分别代表自动循环与手动控制台，同时向推焦杆提交速度。探测只
        读取最终提交值，用来确认共享执行器始终收敛到唯一值。
        """

        if rounds < 1:
            raise ValueError("探测轮数必须为正")

        def worker(writer: str, speed: float, priority: int) -> None:
            for _ in range(rounds):
                self.speed.request(writer, speed, priority)

        threads = [
            threading.Thread(target=worker, args=("auto_cycle", self.config.pusher.speed_max_mm_s, 50)),
            threading.Thread(target=worker, args=("manual_console", self.config.pusher.speed_min_mm_s, 80)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        outcome = self.speed.resolve()
        self.pusher.set_speed(outcome.speed_mm_s)
        return {
            "rounds": rounds,
            "winner": outcome.winner,
            "committed_mm_s": round(outcome.speed_mm_s, 2),
            "contenders": outcome.contenders,
            "writers": self.speed.writers(),
            "committed_on_machine": round(self.pusher.committed_speed(), 2),
        }

    def calibrate_stroke(self, value_mm: float) -> dict:
        """重新标定推焦行程基线，返回标定前后的差值。"""

        if value_mm <= 0:
            raise CokeError("标定值必须为正", component="pusher")
        before = self.stroke.baseline_mm
        after = self.stroke.calibrate(value_mm)
        self.bus.publish(
            Event.of(
                "pusher",
                "pusher",
                "stroke_recalibrated",
                severity="notice",
                baseline_before_mm=before,
                baseline_after_mm=after,
                epoch=self.stroke.epoch,
            )
        )
        return {"before_mm": before, "after_mm": after, "epoch": self.stroke.epoch}

    def disturb_push_measurement(self, bias_mm: float, after_mm: float | None = None) -> dict:
        """注入测量杆位偏差，供演练验证超差停住。"""

        with self._lock:
            if after_mm is None:
                self.pusher.set_measurement_bias(bias_mm)
            else:
                def hook(_dt: float) -> None:
                    if self.pusher.commanded_ram_mm >= float(after_mm):
                        self.pusher.set_measurement_bias(bias_mm)

                self.pusher.set_before_advance_hook(hook)
            return {
                "bias_mm": float(bias_mm),
                "after_mm": None if after_mm is None else float(after_mm),
                "measured_ram_mm": round(self.pusher.ram_mm, 2),
            }

    def disturb_collector(self, delta_pa: float) -> dict:
        """模拟一次集气管扰动，返回扰动后压力。"""

        before = self.collector.pressure_pa
        after = self.collector.disturb(delta_pa)
        return {"before_pa": round(before, 2), "after_pa": round(after, 2), "settled": self.collector.settled()}

    def note_error(self, error: CokeError) -> dict:
        """把平台异常写入审计，返回落盘记录。"""

        payload = error.as_record()
        record = self.audit.record(
            AuditRecord.create(
                self.clock.now(),
                "",
                payload["component"],
                "error",
                severity="warning",
                message=payload["message"],
            )
        )
        return record

    def reset_state(self) -> bool:
        """清掉落盘的平台计数（交付前重新标定用）。"""

        existed = self.store.exists("platform_state")
        self.store.remove("platform_state")
        self.store.remove("push_recovery")
        self.store.remove("push_schedule")
        return existed

    def heating_snapshot(self) -> HeatingSnapshot:
        """返回加热换向状态快照。"""

        return HeatingSnapshot(
            waste_gas_open=self.valves.waste_open,
            gas_open=self.valves.gas_open,
            temperature_c=self.temperature.value_c,
            confirmed=self.temperature.confirmed(),
            reversals=self.reversal.count(),
            seconds_to_due=self.reversal.seconds_to_due(),
        )

    def dust_snapshot(self) -> DustSnapshot:
        """返回除尘状态快照。"""

        return DustSnapshot(
            fan_running=self.dust.is_running(),
            damper_deg=self.damper.angle_deg,
            damper_open=self.damper.at_open(),
            linkage_pending=self.schedule.links() > 0,
            links=self.schedule.links(),
        )

    def topology_summary(self) -> dict:
        """返回工艺链拓扑摘要。"""

        return self.topology.summary()

    def namespaces_describe(self) -> dict:
        """返回命名空间摘要。"""

        return self.namespaces.describe()

    def status(self) -> dict:
        """汇总状态快照，供控制台与命令行读取。"""

        return {
            "platform": self.config.name,
            "unit": self.unit,
            "operations": self._operations,
            "cycles": self._cycles,
            "now": round(self.clock.now(), 3),
            "utc": self.clock.utc_iso(),
            "pusher": self.pusher.snapshot().as_dict(),
            "speed": {"committed_mm_s": round(self.speed.committed(), 2)},
            "speed_curve": self.config.pusher.profile.as_dict() if self.config.pusher.profile else {},
            "last_push_decision": self._last_push_decision,
            "push_recovery": self.push_recovery.state(),
            "push_schedule": self.push_schedule.state(),
            "stroke": {
                "baseline_mm": self.stroke.baseline_mm,
                "tolerance_mm": self.stroke.tolerance_mm,
                "epoch": self.stroke.epoch,
            },
            "guide": self.guide.snapshot().as_dict(),
            "charger": self.charger.snapshot().as_dict(),
            "quench": self.quench_car.snapshot().as_dict(),
            "gas": {
                "pressure_pa": round(self.collector.pressure_pa, 2),
                "target_pa": self.collector.limits.pressure_target_pa,
                "settled": self.collector.settled(),
                "flap_deg": round(self.flap.angle_deg, 2),
                "flap_open": self.flap.is_open(),
                "latch_engaged": self.flap.latch.is_engaged(),
                "latch_cycles": self.flap.latch.cycles(),
                "latch_resets": self.flap.latch.resets(),
            },
            "door": {**self.door.snapshot().as_dict(), "latch_cycles": self.door.latch.cycles()},
            "heating": self.heating_snapshot().as_dict(),
            "dust": self.dust_snapshot().as_dict(),
            "dust_starts": self.dust.starts(),
            "audit": self.audit.summary(),
            "ids_issued": self.ids.issued(),
        }

    def snapshot(self) -> dict:
        """返回只读的整体快照（状态 + 拓扑 + 命名空间 + 趋势）。"""

        return {
            "status": self.status(),
            "topology": self.topology_summary(),
            "namespaces": self.namespaces_describe(),
            "trend": self.trend.snapshot(),
        }


def build_platform(
    config: PlatformConfig | None = None,
    clock: Clock | None = None,
    storage_root: str | None = None,
    unit: str = DEFAULT_UNIT,
) -> Platform:
    """构造一个平台；``storage_root`` 可覆盖配置里的存储目录。"""

    base = config or default_config()
    if storage_root is not None:
        base = dataclasses.replace(base, storage_root=storage_root)
    return Platform(base, clock=clock, unit=unit)
