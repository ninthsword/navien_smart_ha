"""공기질 종류가 줄어도 엔티티가 사라지지 않아야 한다.

실기기(NRT-20D)에서 나온 문제다. 8월 20일까지 CO₂·미세먼지·라돈이 정상으로
쌓이다가, 서버가 온도·습도만 주기 시작한 뒤 Home Assistant 를 재시작하자
나머지 여섯 개 센서가 통째로 `unavailable` 이 됐다. 값이 돌아와도 재시작
전까지는 살아나지 않는다.

세션 안에서는 `set_air_sensors` 가 겹쳐 써서 보호되지만, 엔티티는 시작할 때
한 번만 만들어지므로 그 보호가 재시작을 넘지 못했다.
"""

from __future__ import annotations

import re
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


r.section("끊긴 것을 사용자에게 알린다")

binary_source = source("binary_sensor.py")
r.ok("AironeAirDataMissing" in binary_source, "공기질 자료 끊김 센서를 만든다")
r.ok("공기질 자료 끊김" in binary_source, "이름을 붙였다")
r.ok(
    "기기 고장이라고 하지 않고" in binary_source,
    "관찰일 뿐 고장 판정이 아니라고 적었다",
)
r.ok(
    "AironeAirDataMissing(coordinator, airone)" in binary_source
    and "wants_air_sensors" in binary_source,
    "공기질을 묻는 기기에만 만든다",
)

r.ok(
    "_log_air_sensors_missing" in coordinator_source,
    "세션 중에 빠지면 로그로도 남긴다",
)
r.ok(
    "AIRONE_SENSOR_KINDS," in coordinator_source,
    "로그에 쓰는 이름표를 import 한다",
)


r.section("돌아온 종류가 디스크에 남는다")

# 실기기에서 자료가 돌아왔는데도 센서 여섯 개가 `사용할 수 없음` 으로 남았다.
# 종류는 공기질 조회로만 갱신되는데 저장은 MQTT 보고 때만 일어나서, 늘어난
# 종류가 디스크에 닿지 않았다. 재시작하면 다시 옛 종류만 되살아난다.
body = coordinator_source.split("async def _async_update_air_sensors")[1]
body = body.split("\n    def ")[0]
r.ok("set_air_sensors" in body, "공기질 조회가 종류를 갱신한다")
r.ok(
    "_async_remember_state()" in body,
    "같은 자리에서 저장까지 한다",
)
r.ok(
    body.index("set_air_sensors") < body.index("_async_remember_state()"),
    "갱신한 뒤에 저장한다",
)
r.ok(
    "!= before" in body,
    "늘지 않았으면 저장하지 않는다",
)

grown = make_airone(filters=[])
grown.remember_sensor_kinds(["temperature", "humidity"])
was = grown.known_sensor_kinds
grown.set_air_sensors([
    {"type": "temperature", "value": "25"},
    {"type": "humidity", "value": "59"},
    {"type": "co2", "value": "605"},
])
r.ok(grown.known_sensor_kinds != was, "종류가 돌아오면 달라진다 — 저장이 걸린다")

same = make_airone(filters=[])
same.set_air_sensors([{"type": "temperature", "value": "25"}])
was = same.known_sensor_kinds
same.set_air_sensors([{"type": "temperature", "value": "26"}])
r.ok(same.known_sensor_kinds == was, "값만 바뀐 조회는 저장을 부르지 않는다")


r.section("되살리기 전에는 저장하지 않는다")

# 위 저장을 넣자마자 룸콘 상태 17개가 「알 수 없음」이 됐다. 첫 조회가
# 되살리기보다 먼저 도는데, 그때 쓴 스냅숏에는 아직 안 읽은 `reported` 가
# 없었다. 통째로 갈아끼우는 저장이라 디스크의 `reported` 가 날아갔고,
# 뒤이은 되살리기는 우리가 지운 것을 읽었다.
save = coordinator_source.split("def _async_remember_state")[1]
save = save.split("\n    async def ")[0]
r.ok("_state_restored" in save, "되살렸는지 먼저 본다")
r.ok(
    save.index("_state_restored") < save.index("snapshot: dict"),
    "스냅숏을 만들기 전에 막는다",
)
r.ok(
    "async_delay_save" in save and save.index("_state_restored") < save.index("async_delay_save"),
    "쓰기보다 먼저 막는다",
)

restore = coordinator_source.split("async def async_restore_state")[1]
restore = restore.split("\n    # -- ")[0]
r.ok(
    restore.count("self._state_restored = True") == 3,
    "읽기에 실패해도 플래그를 켠다 — 안 켜면 영영 못 쓴다",
)
r.ok(
    "_async_remember_state()" in restore,
    "되살린 뒤 한 번 남긴다 — 첫 조회에서 안 남긴 종류가 여기서 남는다",
)
r.ok(
    restore.rindex("self._state_restored = True")
    < restore.index("self._async_remember_state()"),
    "플래그를 켠 다음에 부른다",
)


r.section("폴링이 되살린 종류를 버리지 않는다")

# **여기가 앞 절을 무력화하고 있었다.** 기기 객체는 폴링마다 새로 만들고
# 이어받을 필드를 손으로 나열하는데, `known_sensor_kinds` 가 그 목록에서
# 빠져 있었다. 되살린 종류가 5분 뒤 「지금 오는 것」만으로 다시 좁혀지고,
# 그 좁혀진 값이 디스크까지 덮어써 재시작 때 센서가 또 사라졌다.
carry = coordinator_source.split("def _parse_airone")[1].split("\n    def ")[0]
r.ok(
    "device.known_sensor_kinds = old.known_sensor_kinds" in carry,
    "본 적 있는 종류를 이어받는다",
)

FULL = ("pm2Dot5", "co2", "temperature", "humidity")


# **목록을 손으로 적지 않는다.** 여기에 이름을 나열하면 시험이 구현과 같은
# 가정을 공유하게 된다 — 코드가 이어받지 않는 필드를 시험만 이어받으면,
# 버그가 있어도 통과한다. 실제로 그렇게 써 봤더니 이 절이 전부 통과했다.
# 그래서 `_parse_airone` 이 쓰는 대입문을 소스에서 뽑아 그대로 흉내 낸다.
CARRIED = re.findall(r"device\.(\w+) = old\.\1", carry)
assert CARRIED, "_parse_airone 에서 이어받는 필드를 찾지 못했다"


def _carry_over(old_device):
    """`_parse_airone` 이 실제로 이어받는 필드만 옮긴 새 객체."""
    fresh = make_airone(filters=[])
    for name in CARRIED:
        setattr(fresh, name, getattr(old_device, name))
    return fresh


def _airs(kinds):
    return [{"type": k, "value": "1"} for k in kinds]


# 재시작 직후: 첫 조회는 온도·습도만 받고, 그 뒤 되살리기가 네 종류를 채운다.
poll1 = make_airone(filters=[])
poll1.set_air_sensors(_airs(["temperature", "humidity"]))
poll1.remember_sensor_kinds(FULL)
r.ok(len(poll1.known_sensor_kinds) == 4, "되살린 직후 네 종류")

# 5분 뒤 폴링. 서버는 여전히 온도·습도만 준다.
poll2 = _carry_over(poll1)
before = poll2.known_sensor_kinds
poll2.set_air_sensors(_airs(["temperature", "humidity"]))
r.ok(
    len(poll2.known_sensor_kinds) == 4,
    "다음 폴링에서도 네 종류가 남는다",
)
r.ok(
    poll2.entity_sensor_kinds == FULL,
    "만들 엔티티도 그대로 넷이다",
)
r.ok(
    poll2.known_sensor_kinds == before,
    "달라진 것이 없으니 저장을 부르지 않는다 — 디스크를 좁히지 않는다",
)

# 값이 돌아오면 늘어나고, 그때는 저장이 걸려야 한다.
poll3 = _carry_over(poll2)
before = poll3.known_sensor_kinds
poll3.set_air_sensors(_airs(["temperature", "humidity", "pm10"]))
r.ok(
    "pm10" in poll3.known_sensor_kinds and poll3.known_sensor_kinds != before,
    "새 종류가 오면 늘어나고 저장이 걸린다",
)


sys.exit(r.finish())
