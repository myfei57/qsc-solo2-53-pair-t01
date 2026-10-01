"""平台配置。

配置来自 JSON 文件（标准库解析），覆盖推焦行程基线与分段速度曲线、导焦栅
对齐公差、集气管压力带、熄焦喷淋时长、换向间隔、炉温确认带、除尘挡板联动
参数与存储目录。任何缺失或越界取值都在装配阶段抛出 :class:`ConfigError`，
不允许带着半份配置启动控制链。
"""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import ConfigError

_MISSING = object()
PUSHER_DEVIATION_POLICIES = ("direct_stop", "decelerate_then_judge")


def _section(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = mapping.get(key)
    if value is None:
        raise ConfigError(f"配置缺少段落 {key!r}", component="config")
    if not isinstance(value, Mapping):
        raise ConfigError(f"配置段落 {key!r} 必须是映射", component="config")
    return value


def _number(section: Mapping[str, Any], key: str, *, minimum: float | None = None) -> float:
    value = section.get(key, _MISSING)
    if value is _MISSING:
        raise ConfigError(f"配置缺少字段 {key!r}", component="config")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"配置字段 {key!r} 必须是数值", component="config")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ConfigError(f"配置字段 {key!r} 不得小于 {minimum}", component="config")
    return number


def _optional_number(section: Mapping[str, Any], key: str, default: float) -> float:
    if key not in section:
        return float(default)
    return _number(section, key)


@dataclass(frozen=True)
class PusherProfile:
    """按行程分段的推焦速度曲线与超差判定策略。"""

    start_distance_mm: float
    approach_distance_mm: float
    start_speed_mm_s: float
    cruise_speed_mm_s: float
    finish_speed_mm_s: float
    creep_speed_mm_s: float
    caution_band_mm: float
    deviation_policy: str

    @classmethod
    def from_mapping(cls, limits: "PusherLimits", section: Mapping[str, Any] | None = None) -> "PusherProfile":
        values = section if isinstance(section, Mapping) else {}
        profile = cls(
            start_distance_mm=_optional_number(values, "profile_start_distance_mm", 300.0),
            approach_distance_mm=_optional_number(values, "profile_approach_distance_mm", 400.0),
            start_speed_mm_s=_optional_number(values, "profile_start_speed_mm_s", limits.speed_min_mm_s),
            cruise_speed_mm_s=_optional_number(values, "profile_cruise_speed_mm_s", limits.speed_max_mm_s),
            finish_speed_mm_s=_optional_number(values, "profile_finish_speed_mm_s", 14.0),
            creep_speed_mm_s=_optional_number(values, "profile_creep_speed_mm_s", limits.speed_min_mm_s),
            caution_band_mm=_optional_number(values, "deviation_caution_band_mm", 10.0),
            deviation_policy=str(values.get("deviation_policy", "direct_stop")),
        )
        if profile.start_distance_mm <= 0.0 or profile.approach_distance_mm <= 0.0:
            raise ConfigError("推焦起步段和收速段距离必须为正", component="config")
        if profile.start_distance_mm + profile.approach_distance_mm >= limits.stroke_baseline_mm:
            raise ConfigError("推焦速度曲线的起步段与收速段不得占满基线行程", component="config")
        for key in ("start_speed_mm_s", "cruise_speed_mm_s", "finish_speed_mm_s", "creep_speed_mm_s"):
            value = getattr(profile, key)
            if not limits.speed_min_mm_s <= value <= limits.speed_max_mm_s:
                raise ConfigError(f"推焦曲线速度 {key} 必须落在速度上下限之间", component="config")
        if not profile.start_speed_mm_s < profile.cruise_speed_mm_s:
            raise ConfigError("推焦起步速度必须低于中段速度", component="config")
        if not profile.finish_speed_mm_s < profile.cruise_speed_mm_s:
            raise ConfigError("推焦末段收速必须低于中段速度", component="config")
        if profile.creep_speed_mm_s > profile.finish_speed_mm_s:
            raise ConfigError("超差复查爬行速度不得高于末段收速", component="config")
        if profile.caution_band_mm <= 0.0 or profile.caution_band_mm > limits.stroke_tolerance_mm:
            raise ConfigError("超差预警带必须为正且不大于行程公差", component="config")
        if profile.deviation_policy not in PUSHER_DEVIATION_POLICIES:
            raise ConfigError("推焦杆超差策略必须是 direct_stop 或 decelerate_then_judge", component="config")
        return profile

    def as_dict(self) -> dict:
        return {
            "profile_start_distance_mm": self.start_distance_mm,
            "profile_approach_distance_mm": self.approach_distance_mm,
            "profile_start_speed_mm_s": self.start_speed_mm_s,
            "profile_cruise_speed_mm_s": self.cruise_speed_mm_s,
            "profile_finish_speed_mm_s": self.finish_speed_mm_s,
            "profile_creep_speed_mm_s": self.creep_speed_mm_s,
            "deviation_caution_band_mm": self.caution_band_mm,
            "deviation_policy": self.deviation_policy,
        }


@dataclass(frozen=True)
class PusherLimits:
    """推焦机行程与速度约束。"""

    stroke_baseline_mm: float
    stroke_tolerance_mm: float
    max_stroke_mm: float
    speed_min_mm_s: float
    speed_max_mm_s: float
    speed_default_mm_s: float
    profile: PusherProfile | None = None

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "PusherLimits":
        limits = cls(
            stroke_baseline_mm=_number(section, "stroke_baseline_mm", minimum=0.0),
            stroke_tolerance_mm=_number(section, "stroke_tolerance_mm", minimum=0.0),
            max_stroke_mm=_number(section, "max_stroke_mm", minimum=1.0),
            speed_min_mm_s=_number(section, "speed_min_mm_s", minimum=0.0),
            speed_max_mm_s=_number(section, "speed_max_mm_s", minimum=0.1),
            speed_default_mm_s=_number(section, "speed_default_mm_s", minimum=0.1),
        )
        if limits.stroke_baseline_mm > limits.max_stroke_mm:
            raise ConfigError("推焦行程基线不得超过最大行程", component="config")
        if not limits.speed_min_mm_s < limits.speed_max_mm_s:
            raise ConfigError("推焦速度下限必须小于上限", component="config")
        if not limits.speed_min_mm_s <= limits.speed_default_mm_s <= limits.speed_max_mm_s:
            raise ConfigError("推焦默认速度必须落在速度上下限之间", component="config")
        profile = PusherProfile.from_mapping(limits, section)
        return dataclasses.replace(limits, profile=profile)


@dataclass(frozen=True)
class GuideLimits:
    """拦焦车导焦栅对齐与锁定约束。"""

    align_tolerance_mm: float
    apron_lock_travel_mm: float
    apron_lock_timeout_s: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "GuideLimits":
        return cls(
            align_tolerance_mm=_number(section, "align_tolerance_mm", minimum=0.0),
            apron_lock_travel_mm=_number(section, "apron_lock_travel_mm", minimum=1.0),
            apron_lock_timeout_s=_number(section, "apron_lock_timeout_s", minimum=1.0),
        )


@dataclass(frozen=True)
class ChargerLimits:
    """装煤车煤斗对齐与给煤约束。"""

    align_tolerance_mm: float
    coal_target_t: float
    feed_rate_t_per_min: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "ChargerLimits":
        limits = cls(
            align_tolerance_mm=_number(section, "align_tolerance_mm", minimum=0.0),
            coal_target_t=_number(section, "coal_target_t", minimum=0.1),
            feed_rate_t_per_min=_number(section, "feed_rate_t_per_min", minimum=0.1),
        )
        if limits.align_tolerance_mm <= 0.0:
            raise ConfigError("煤斗对齐公差必须为正", component="config")
        return limits


@dataclass(frozen=True)
class QuenchLimits:
    """熄焦车走行与喷淋约束。"""

    travel_distance_m: float
    travel_speed_m_s: float
    spray_duration_s: float
    water_flow_m3_h: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "QuenchLimits":
        return cls(
            travel_distance_m=_number(section, "travel_distance_m", minimum=1.0),
            travel_speed_m_s=_number(section, "travel_speed_m_s", minimum=0.1),
            spray_duration_s=_number(section, "spray_duration_s", minimum=1.0),
            water_flow_m3_h=_number(section, "water_flow_m3_h", minimum=1.0),
        )


@dataclass(frozen=True)
class CollectorLimits:
    """集气管压力与调节约束。"""

    pressure_target_pa: float
    pressure_band_pa: float
    regulate_rate_pa_per_s: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "CollectorLimits":
        limits = cls(
            pressure_target_pa=_number(section, "pressure_target_pa"),
            pressure_band_pa=_number(section, "pressure_band_pa", minimum=1.0),
            regulate_rate_pa_per_s=_number(section, "regulate_rate_pa_per_s", minimum=0.1),
        )
        if limits.pressure_target_pa <= 0.0:
            raise ConfigError("集气管压力目标必须为正值", component="config")
        return limits

    def settled(self, pressure_pa: float) -> bool:
        """压力是否进入目标带。"""

        return abs(pressure_pa - self.pressure_target_pa) <= self.pressure_band_pa


@dataclass(frozen=True)
class DoorLimits:
    """炉门清扫闩锁约束。"""

    latch_travel_mm: float
    clean_duration_s: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "DoorLimits":
        return cls(
            latch_travel_mm=_number(section, "latch_travel_mm", minimum=1.0),
            clean_duration_s=_number(section, "clean_duration_s", minimum=1.0),
        )


@dataclass(frozen=True)
class HeatingLimits:
    """加热换向与炉温确认约束。"""

    reversal_interval_s: float
    temp_target_c: float
    temp_band_c: float
    waste_close_lead_s: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "HeatingLimits":
        limits = cls(
            reversal_interval_s=_number(section, "reversal_interval_s", minimum=1.0),
            temp_target_c=_number(section, "temp_target_c", minimum=1.0),
            temp_band_c=_number(section, "temp_band_c", minimum=1.0),
            waste_close_lead_s=_number(section, "waste_close_lead_s", minimum=0.0),
        )
        if limits.waste_close_lead_s >= limits.reversal_interval_s:
            raise ConfigError("废气阀提前关闭时间必须小于换向间隔", component="config")
        return limits

    def confirmed(self, temperature_c: float) -> bool:
        """炉温是否落在确认带内。"""

        return abs(temperature_c - self.temp_target_c) <= self.temp_band_c


@dataclass(frozen=True)
class DustLimits:
    """除尘挡板联动约束。"""

    damper_open_deg: float
    damper_rate_deg_per_s: float
    linkage_lag_s: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "DustLimits":
        limits = cls(
            damper_open_deg=_number(section, "damper_open_deg", minimum=1.0),
            damper_rate_deg_per_s=_number(section, "damper_rate_deg_per_s", minimum=0.1),
            linkage_lag_s=_number(section, "linkage_lag_s", minimum=0.0),
        )
        if limits.damper_open_deg > 90.0:
            raise ConfigError("除尘挡板开度不得超过 90 度", component="config")
        return limits


@dataclass(frozen=True)
class PlatformConfig:
    """一份完整的平台配置。"""

    name: str
    storage_root: str
    pusher: PusherLimits
    guide: GuideLimits
    charger: ChargerLimits
    quench: QuenchLimits
    collector: CollectorLimits
    door: DoorLimits
    heating: HeatingLimits
    dust: DustLimits

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "PlatformConfig":
        meta = _section(mapping, "platform")
        name = meta.get("name")
        storage_root = meta.get("storage_root")
        if not isinstance(name, str) or not name:
            raise ConfigError("platform.name 必须是非空字符串", component="config")
        if not isinstance(storage_root, str) or not storage_root:
            raise ConfigError("platform.storage_root 必须是非空字符串", component="config")

        limits = _section(mapping, "limits")
        return cls(
            name=name,
            storage_root=storage_root,
            pusher=PusherLimits.from_mapping(_section(limits, "pusher")),
            guide=GuideLimits.from_mapping(_section(limits, "guide")),
            charger=ChargerLimits.from_mapping(_section(limits, "charger")),
            quench=QuenchLimits.from_mapping(_section(limits, "quench")),
            collector=CollectorLimits.from_mapping(_section(limits, "collector")),
            door=DoorLimits.from_mapping(_section(limits, "door")),
            heating=HeatingLimits.from_mapping(_section(limits, "heating")),
            dust=DustLimits.from_mapping(_section(limits, "dust")),
        )


def load_config(path: str) -> PlatformConfig:
    """从 JSON 文件读取配置。"""

    if not os.path.isfile(path):
        raise ConfigError(f"配置文件不存在: {path}", component="config")
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, Mapping):
        raise ConfigError("配置文件顶层必须是映射", component="config")
    return PlatformConfig.from_mapping(raw)


def default_config() -> PlatformConfig:
    """内置默认配置，供未指定配置文件时装配使用。"""

    default_limits = PusherLimits(3200.0, 25.0, 3350.0, 12.0, 38.0, 24.0)
    default_profile = PusherProfile.from_mapping(default_limits, {})
    return PlatformConfig(
        name="sequence-control",
        storage_root="var/state",
        pusher=dataclasses.replace(default_limits, profile=default_profile),
        guide=GuideLimits(15.0, 120.0, 20.0),
        charger=ChargerLimits(20.0, 28.0, 4.0),
        quench=QuenchLimits(42.0, 1.6, 90.0, 320.0),
        collector=CollectorLimits(90.0, 18.0, 6.0),
        door=DoorLimits(60.0, 45.0),
        heating=HeatingLimits(1800.0, 1180.0, 35.0, 4.0),
        dust=DustLimits(70.0, 14.0, 1.5),
    )


def config_to_mapping(cfg: PlatformConfig) -> dict:
    """把配置还原成可打印的映射。"""

    return {
        "platform": {"name": cfg.name, "storage_root": cfg.storage_root},
        "limits": {
            "pusher": {
                "stroke_baseline_mm": cfg.pusher.stroke_baseline_mm,
                "stroke_tolerance_mm": cfg.pusher.stroke_tolerance_mm,
                "max_stroke_mm": cfg.pusher.max_stroke_mm,
                "speed_min_mm_s": cfg.pusher.speed_min_mm_s,
                "speed_max_mm_s": cfg.pusher.speed_max_mm_s,
                "speed_default_mm_s": cfg.pusher.speed_default_mm_s,
                **(cfg.pusher.profile.as_dict() if cfg.pusher.profile else {}),
            },
            "guide": {
                "align_tolerance_mm": cfg.guide.align_tolerance_mm,
                "apron_lock_travel_mm": cfg.guide.apron_lock_travel_mm,
                "apron_lock_timeout_s": cfg.guide.apron_lock_timeout_s,
            },
            "charger": {
                "align_tolerance_mm": cfg.charger.align_tolerance_mm,
                "coal_target_t": cfg.charger.coal_target_t,
                "feed_rate_t_per_min": cfg.charger.feed_rate_t_per_min,
            },
            "quench": {
                "travel_distance_m": cfg.quench.travel_distance_m,
                "travel_speed_m_s": cfg.quench.travel_speed_m_s,
                "spray_duration_s": cfg.quench.spray_duration_s,
                "water_flow_m3_h": cfg.quench.water_flow_m3_h,
            },
            "collector": {
                "pressure_target_pa": cfg.collector.pressure_target_pa,
                "pressure_band_pa": cfg.collector.pressure_band_pa,
                "regulate_rate_pa_per_s": cfg.collector.regulate_rate_pa_per_s,
            },
            "door": {
                "latch_travel_mm": cfg.door.latch_travel_mm,
                "clean_duration_s": cfg.door.clean_duration_s,
            },
            "heating": {
                "reversal_interval_s": cfg.heating.reversal_interval_s,
                "temp_target_c": cfg.heating.temp_target_c,
                "temp_band_c": cfg.heating.temp_band_c,
                "waste_close_lead_s": cfg.heating.waste_close_lead_s,
            },
            "dust": {
                "damper_open_deg": cfg.dust.damper_open_deg,
                "damper_rate_deg_per_s": cfg.dust.damper_rate_deg_per_s,
                "linkage_lag_s": cfg.dust.linkage_lag_s,
            },
        },
    }
