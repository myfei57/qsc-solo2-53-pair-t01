"""推焦曲线与恢复控制台接口。"""

from __future__ import annotations

from coke.console.api import Api


def test_console_status_exposes_curve_and_recovery(platform):
    api = Api(platform)
    status_code, status = api.handle("GET", "/api/status", {}, {})
    assert status_code == 200
    assert "curve" in status["speed"]
    assert status["push_recovery"]["active"] is False


def test_console_recovery_flow_and_idempotent_resume(platform):
    platform.start()
    api = Api(platform)
    held = platform.push(oven="oven05", measurement_offset_mm=40)
    assert held["ok"] is False
    incident_id = held["hold_incident"]["incident_id"]

    code, missing = api.handle(
        "POST", "/api/push/resume", {}, {"incident_id": incident_id}
    )
    assert code == 409
    assert missing["component"] == "pusher"

    code, confirmation = api.handle(
        "POST",
        "/api/push/confirm-resume",
        {},
        {"incident_id": incident_id, "confirmed_by": "operator-b"},
    )
    assert code == 200
    assert confirmation["confirmed_by"] == "operator-b"

    platform._measurement_offset_mm = 0.0
    code, resumed = api.handle("POST", "/api/push/resume", {}, {"incident_id": incident_id})
    assert code == 200 and resumed["ok"] is True
    code, repeated = api.handle("POST", "/api/push/resume", {}, {"incident_id": incident_id})
    assert code == 200
    assert repeated["op_id"] == resumed["op_id"]


def test_console_push_order_is_oven_number_ascending(platform):
    api = Api(platform)
    code, payload = api.handle(
        "POST",
        "/api/push/order",
        {},
        {"ovens": ["oven11", "oven1", "oven02"], "crew": "crew-c"},
    )
    assert code == 200
    assert payload["ovens"] == ["oven1", "oven02", "oven11"]
    assert payload["start_push_order"] == "oven_number_ascending"
    assert payload["crew"] == "crew-c"
