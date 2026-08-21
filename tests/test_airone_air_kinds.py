"""공기질 종류가 줄어도 엔티티가 사라지지 않아야 한다.

실기기(NRT-20D)에서 나온 문제다. 8월 20일까지 CO₂·미세먼지·라돈이 정상으로
쌓이다가, 서버가 온도·습도만 주기 시작한 뒤 Home Assistant 를 재시작하자
나머지 여섯 개 센서가 통째로 `unavailable` 이 됐다. 값이 돌아와도 재시작
전까지는 살아나지 않는다.

세션 안에서는 `set_air_sensors` 가 겹쳐 써서 보호되지만, 엔티티는 시작할 때
한 번만 만들어지므로 그 보호가 재시작을 넘지 못했다.
"""

from __future__ import annotations

import sys

from harness import Report, make_airone, source

r = Report()


r.section("본 적 있는 종류를 기억한다")

device = make_airone(filters=[])
r.ok(device.known_sensor_kinds == (), "처음에는 아무것도 모른다")
r.ok(device.entity_sensor_kinds == (), "만들 엔티티도 없다")

full = [
    {"type": "temperature", "value": "25"},
    {"type": "humidity", "value": "86"},
    {"type": "co2", "value": "605"},
    {"type": "pm2Dot5", "value": "3"},
]
device.set_air_sensors(full)
r.ok(device.sensor_kinds == ("pm2Dot5", "co2", "temperature", "humidity"), "값이 온 종류")
r.ok(device.known_sensor_kinds == device.sensor_kinds, "본 적 있는 종류도 같다")

# 에어모니터가 빠졌다. 서버가 온도·습도만 준다.
device.set_air_sensors([
    {"type": "temperature", "value": "25"},
    {"type": "humidity", "value": "86"},
])
r.ok(
    device.sensor_kinds == ("pm2Dot5", "co2", "temperature", "humidity"),
    "세션 안에서는 겹쳐 써서 앞 값을 잃지 않는다",
)
r.ok("co2" in device.known_sensor_kinds, "본 적 있는 종류에서도 빠지지 않는다")


r.section("재시작을 넘어 엔티티가 살아남는다")

restarted = make_airone(filters=[])
# 저장해 둔 종류를 되살린 상태에서, 이번 조회는 온도·습도만 왔다.
restarted.remember_sensor_kinds(["temperature", "humidity", "co2", "pm2Dot5"])
restarted.set_air_sensors([
    {"type": "temperature", "value": "25"},
    {"type": "humidity", "value": "86"},
])
r.ok(
    restarted.sensor_kinds == ("temperature", "humidity"),
    "값이 온 종류는 둘뿐이다",
)
r.ok(
    restarted.entity_sensor_kinds == ("pm2Dot5", "co2", "temperature", "humidity"),
    "그래도 엔티티는 네 개를 만든다",
)
r.ok(
    "entity_sensor_kinds" in source("sensor.py"),
    "엔티티 생성이 본 적 있는 종류를 쓴다",
)


r.section("모르는 종류는 기억하지 않는다")

restarted.remember_sensor_kinds(["co2", "없는종류", 7, None])
r.ok(
    "없는종류" not in restarted.known_sensor_kinds,
    "표에 없는 이름은 넣지 않는다",
)
r.ok(
    len(restarted.known_sensor_kinds) == 4,
    "이상한 값이 섞여도 아는 종류만 남는다",
)


r.section("값이 없을 때 문자열 센서로 굳지 않는다")

sensor_source = source("sensor.py")
r.ok(
    "if raw else True" in sensor_source,
    "값이 아직 없으면 숫자로 본다",
)
r.ok(
    "단위도 device_class 도 없는 채로 굳는다" in sensor_source,
    "왜 그렇게 정했는지 적었다",
)


r.section("저장 모양이 두 가지를 모두 받는다")

coordinator_source = source("coordinator.py")
r.ok("air_kinds" in coordinator_source, "공기질 종류를 함께 저장한다")
r.ok(
    'if "reported" in entry or "air_kinds" in entry' in coordinator_source,
    "새 모양과 옛 모양을 모양으로 가린다",
)
r.ok(
    "reported, kinds = entry, None" in coordinator_source,
    "옛 모양은 통째로 reported 로 읽는다",
)
r.ok(
    "remember_sensor_kinds(kinds)" in coordinator_source,
    "되살린 종류를 기기에 넣는다",
)


r.section("진단으로 빠진 종류가 보인다")

diagnostics_source = source("diagnostics.py")
r.ok("air_sensor_kinds_known" in diagnostics_source, "본 적 있는 종류를 남긴다")
r.ok("air_sensor_kinds_missing" in diagnostics_source, "이번에 빠진 종류를 남긴다")


sys.exit(r.finish())
