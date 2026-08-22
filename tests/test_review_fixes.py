"""전체 검토에서 나온 수정들이 되돌아가지 않게 붙잡는다.

각 절은 「무엇이 잘못됐었나」를 먼저 적는다. 고친 모양만 적으면 다음 사람이
그 줄을 왜 못 지우는지 알 수 없다.
"""

from __future__ import annotations

import sys

from harness import Report, source

r = Report()


r.section("재인증의 입구가 있다")

# `ConfigEntryAuthFailed` 를 올리면 HA 는 `SOURCE_REAUTH` 로 흐름을 열고
# `async_step_reauth` 를 찾는다. 확인 화면만 있고 입구가 없으면 흐름이 열리지
# 않아, 비밀번호를 바꾼 사용자가 통합을 지웠다 다시 까는 수밖에 없다 —
# 엔티티 ID 와 장기 통계가 통째로 끊긴다.
flow = source("config_flow.py")
r.ok("async def async_step_reauth(" in flow, "입구가 정의돼 있다")
r.ok("async def async_step_reauth_confirm(" in flow, "확인 화면도 있다")
r.ok(
    flow.index("async def async_step_reauth(")
    < flow.index("async def async_step_reauth_confirm("),
    "입구가 확인 화면보다 먼저 온다",
)

for module in ("__init__.py", "coordinator.py"):
    if "ConfigEntryAuthFailed" in source(module):
        r.ok(True, f"{module} 이 재인증을 요구할 수 있다 — 입구가 필요하다")


r.section("통계 앞 누적을 0 으로 되돌리지 않는다")

# 서버가 주는 범위가 앞으로 밀렸는데 그 자리에 행이 없으면(그 달에 사용량이
# 아예 없었으면 서버가 행을 주지 않는다) 예전에는 0 을 돌려줬다. 그러면 2년치
# 시리즈가 통째로 0 부터 다시 쌓여 경계에 거대한 음수 사용량이 그려진다 —
# HA 는 `sum` 의 차분으로 사용량을 계산하기 때문이다.
gas = source("gas_statistics.py")
r.ok("_async_last_sum_before" in gas, "앞의 마지막 누적을 찾는 길이 있다")
baseline = gas.split("async def _async_baseline")[1].split("\nasync def ")[0]
r.ok(
    "_async_last_sum_before" in baseline,
    "자리에 행이 없으면 그 길로 넘어간다",
)
r.ok(
    baseline.rfind("return 0.0") < baseline.find("_async_last_sum_before"),
    "0 을 돌려주기 전에 먼저 앞을 찾는다",
)


r.section("가스 통계 반영이 겹치지 않는다")

# 반영은 「직전 누적 읽기 → 더하기 → 쓰기」다. 두 번째가 첫 번째의 쓰기 전에
# 읽으면 같은 값에서 출발해 둘 다 잘못 쓴다. 초기 요청과 시간별 갱신이
# 겹치거나 서버가 같은 응답을 두 번 줄 때 일어날 수 있다.
coord = source("coordinator.py")
r.ok("_gas_statistics_locks" in coord, "기기별 락을 둔다")
importer = coord.split("async def _async_import_gas_statistics")[1]
importer = importer.split("\n    @callback")[0]
r.ok("async with lock" in importer, "락을 잡고 반영한다")
r.ok(
    importer.index("async with lock") < importer.index("async_import_gas_statistics("),
    "락을 잡은 다음에 부른다",
)


r.section("MQTT 백오프가 CONNACK 마다 리셋되지 않는다")

# 붙자마자 끊기는 상황에서 백오프가 영원히 첫 칸(5초)에 머물렀다. 계정당
# 세션이 하나뿐이라 사용자가 나비엔 앱을 열어두면 실제로 그렇게 된다 —
# 5초마다 재접속하면서 매번 기기 전체에 초기 상태 요청을 다시 보냈다.
mq = source("mqtt.py")
r.ok("_STABLE_CONNECTION_SECONDS" in mq, "얼마나 버텨야 「제대로 붙었다」인지 정한다")
run = mq.split("async def _async_run")[1].split("\n    async def ")[0]
r.ok("self._attempt = 0" in run, "리셋하는 자리가 있다")
r.ok(
    run.index("_STABLE_CONNECTION_SECONDS") < run.index("self._attempt = 0"),
    "버틴 시간을 확인한 뒤에 리셋한다",
)
r.ok(
    "await self._async_wait_connected()\n                self._attempt = 0" not in run,
    "CONNACK 직후에 리셋하지 않는다",
)


r.section("한 번 도는 타이머도 취소된다")

# 언로드·리로드 뒤에 깨어나면 이미 없어진 통합이 서버로 요청을 보낸다.
# 예외는 잡히지만 비공식 서버에 헛된 요청이 나가고, 관리되는 다른 타이머와
# 앞뒤가 안 맞았다.
r.ok("_oneshot_unsubs" in coord, "한 번 도는 타이머를 붙잡아 둔다")
r.ok("_track_oneshot" in coord, "붙잡는 길이 있다")
stop = coord.split("async def async_stop_mqtt")[1].split("\n    @property")[0]
r.ok("_oneshot_unsubs" in stop, "멈출 때 함께 취소한다")

# `async_call_later` 를 부르는 자리는 모두 어딘가에 결과를 남겨야 한다.
# **앞줄까지 본다** — 여러 줄로 감싸면 호출이 있는 줄에는 대입도 감싸는
# 이름도 없다. 줄 하나만 보던 판정이 멀쩡한 코드를 세 건 물었다.
lines = coord.splitlines()
loose = []
for index, line in enumerate(lines):
    if "async_call_later(" not in line or "import" in line:
        continue
    context = " ".join(lines[max(0, index - 2) : index + 1])
    if "=" in context or "_track_oneshot" in context:
        continue
    loose.append(line.strip())
r.ok(not loose, f"결과를 버리는 async_call_later 가 없다 ({len(loose)}건)")


r.section("동시에 실패해도 로그인은 한 번만 한다")

# 계정당 세션이 하나뿐인 서버라, 동시에 실패한 요청들이 각자 로그인하면
# 서로를 무효화한다.
api = source("api.py")
login = api.split("async def async_login")[1].split("\n    async def ")[0]
r.ok("seen = self._session" in login, "들어오기 전 세션을 기억한다")
r.ok(
    "self._session is not seen" in login,
    "기다리는 동안 바뀌었으면 그것을 쓴다",
)
r.ok(
    login.index("async with self._lock") < login.index("self._session is not seen"),
    "락 안에서 확인한다",
)


r.section("설정온도 범위가 지금 값을 따른다")

# 시작할 때 한 번 읽어 고정하면, 서버가 범위를 바꿨을 때 슬라이더는 옛
# 범위를 보여준다. 사용자는 움직이는데 명령은 거부된다.
num = source("number.py")
r.ok("def native_min_value" in num, "최소값을 속성으로 낸다")
r.ok("def native_max_value" in num, "최대값을 속성으로 낸다")
r.ok("_fallback_bounds" in num, "기기가 사라진 순간을 위한 값도 남긴다")


r.section("쓰지 않는 import 가 없다")

# 같은 유형이 두 번 나왔다 — `coordinator.py` 의 `_dig`, `airone.py` 의
# `LEGACY_EXTRA_FIELDS`. 둘 다 있어도 아무 일이 없어 오래 남아 있었다.
for module, name in (
    ("coordinator.py", "_dig"),
    ("airone.py", "LEGACY_EXTRA_FIELDS"),
):
    r.ok(name not in source(module), f"{module} 이 {name} 를 들이지 않는다")


sys.exit(r.finish())
