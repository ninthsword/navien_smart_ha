"""Constants for the Navien Smart integration.

**Nothing here was guessed.** Every value was extracted from the app. The level of
verification differs, though.

- Mat (200) values — verified on real devices: stepped mats directly, temperature and
  four-season mats through reports
- Airone (300) values — extracted from the app and then **confirmed by reports**. The
  confirmed models are the split room controller (1901) and the all-in-one room
  controller (1900)

**Those two carry different weight.** What was pressed in person and what was heard in a
report are never blended, and each entry says which it is.
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "navien_smarthome"

# --- endpoints -------------------------------------------------------------

API_URL: Final = "https://nskr.naviensmartcontrol.com/api/v2.0"
LOGIN_URL: Final = "https://member.naviensmartcontrol.com"

# The value from the app's `Constants.getIotEndPoint()` — Navien's own domain in front of
# AWS IoT. The `network.server.endpoint` in a device response is different, because it is
# **the endpoint the device connects to**; connecting there gives a 403 on an SNI mismatch.
IOT_ENDPOINT: Final = "nskr-iot.naviensmartcontrol.com"
IOT_REGION: Final = "ap-northeast-2"
IOT_SERVICE: Final = "iotdevicegateway"

USER_AGENT: Final = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2_1 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 APP_NAVIENSMART_IOS"
)

# **Stops one request from eating an entire polling interval.**
#
# The `aiohttp` default is five minutes, exactly the Airone polling interval. If the server
# accepts the connection and never answers, that single request consumes the whole interval.
REQUEST_TIMEOUT_SECONDS: Final = 30

# --- server response codes -------------------------------------------------

CODE_SUCCESS: Final = 200
CODE_BAD_REQUEST: Final = 400
# One session per account. Opening the app is what produces this code.
CODE_NOT_AUTHORIZED: Final = 404
CODE_TOKEN_EXPIRED: Final = 407

# --- device kinds ----------------------------------------------------------

# The app calls this `SERVICE_SMARTTOK` internally — SmartTok is the brand of the boiler's
# communication module.
SERVICE_BOILER: Final = 100
SERVICE_MATE: Final = 200
SERVICE_AIRONE: Final = 300
SERVICE_SCADA: Final = 400
SERVICE_HOMEAUTO: Final = 500

# Boilers (100) support only the observed status sensors and the hot-water and
# heating-water setpoints of modelCode=20 (NR-67D). Power, operating mode and schedules stay
# closed. Commands and temperature encodings differ per controller, so casting the model net
# wider could send a nonsensical value to a real device.
SUPPORTED_SERVICE_CODES: Final = (SERVICE_BOILER, SERVICE_MATE, SERVICE_AIRONE)

# Kinds that are worth a report but get no entities. Currently none.
REPORT_WANTED_SERVICE_CODES: Final = ()

# Out of scope: no report is requested and diagnostics keeps only a summary.
OUT_OF_SCOPE_REASONS: Final = {
    SERVICE_SCADA: "상업용 장비는 이 통합의 범위가 아닙니다",
    SERVICE_HOMEAUTO: "월패드·로비폰은 서버 체계가 완전히 달라 범위가 아닙니다",
}

# The status to state alongside a request for a report.
REPORT_WANTED_NOTES: Final = {}

SERVICE_NAMES: Final = {
    SERVICE_BOILER: "보일러",
    SERVICE_MATE: "숙면매트",
    SERVICE_AIRONE: "환기청정",
    SERVICE_SCADA: "상업용 SCADA",
    SERVICE_HOMEAUTO: "스마트홈",
}

# MQTT subscription topic prefixes. The app's `HomeViewModel` subscribes to `/{prefix}/#`,
# and there are exactly five: `smarttok` (boiler), `mate`, `airone`, `scada`, `homeauto`.
# State is parsed for three of them. On the boiler ``smarttok`` path only the two
# modelCode=20 setpoint commands are opened; every other mode and flag leaves nothing but a
# raw number in diagnostics.
#
# Airone **control** does not use this scheme but `AIRONE_TOPIC_FMT`. Only the subscription
# lives here.
TOPIC_PREFIX: Final = {
    SERVICE_BOILER: "smarttok",
    SERVICE_MATE: "mate",
    SERVICE_AIRONE: "airone",
}

# --- Airone (ventilation and purification) ---------------------------------
#
# Every value below was confirmed directly in the app (`AironeConstants`, `PubSubData`,
# `AironeModeCode`). **None of them has ever been sent to a real device, though.**

# The `Integer.parseInt(modelCode) < 1000` branch in
# `AironeConstants.aironeMqttPayload()`. That single test splits both the topic and the
# envelope. The legacy envelope differs enough that the same code cannot address it, so it
# is skipped.
AIRONE_V2_MIN_MODEL_CODE: Final = 1000

AIRONE_TOPIC_FMT: Final = "cmd/rc/v2/{model_code}/{device_id}/remote/{command}"

# The older generation has no `v2` segment (see the topic table in the CHANGELOG entry about
# skipping older units).
AIRONE_LEGACY_TOPIC_FMT: Final = "cmd/rc/{model_code}/{device_id}/remote/{command}"

AIRONE_CMD_STATUS: Final = "status"
AIRONE_CMD_POWER: Final = "power"
AIRONE_CMD_CHANGE_MODE: Final = "change-mode"

# How long to wait after a command before asking for the state again. When the device pushes
# it by itself that arrives first; when it does not, this single request catches up.
# **This is what stands in for an optimistic update** — a failure is never dressed up as a
# success.
AIRONE_READBACK_DELAY_SECONDS: Final = 3

# After this many consecutive air-quality query failures, a `WARNING` is emitted. At a
# five-minute interval, three failures is about fifteen minutes. Logging every time is noisy;
# logging never means nobody notices the values have frozen.
AIRONE_AIR_ERROR_LOG_EVERY: Final = 3

# If no response arrives this long after a status request, it is logged. An observed mat
# round trip was 1.4 seconds, so this is generous — a silent failure has to be visible.
AIRONE_SILENCE_CHECK_SECONDS: Final = 45

# `running` for the V2.1 generation. Legacy inverts it (running = 2). Ignoring the
# generation flips the power state.
AIRONE_RUN_ON: Final = 1
AIRONE_RUN_OFF: Final = 2
AIRONE_RUN_AWAY: Final = 3
# 4 is the auto-dry state, in which the unit dries itself out after dehumidification stops.
# It was observed on a real `NRT-530Z3` (model code 1901) when the power was switched off
# (PR #15, moKorean). Without it the running state shows as "알 수 없음(4)".
#
# **Confirmed in the app code as well.**
#
#     if (... roomController.getRunning() != 4) { ... }
#     getString(R.string.STR_FRAGMENT_AIRONE_CONTROL_AUTO_DEHUMIDITY)
#     → "자동건조 중 %02d%%"
#
# The label follows the app's 자동건조 with the trailing 중 dropped, so it matches the
# nouns used by the others (운전, 정지, 외출). The progress the app shows alongside
# (`%02d%%`) is read through `AIRONE_AUTO_DRY_TYPE` and put in an **attribute**: mixing a
# number into the state text breaks every automation that compares the string.
AIRONE_RUN_AUTO_DRY: Final = 4
AIRONE_RUN_NAMES: Final = {
    AIRONE_RUN_ON: "운전",
    AIRONE_RUN_OFF: "정지",
    AIRONE_RUN_AWAY: "외출",
    AIRONE_RUN_AUTO_DRY: "자동건조",
}

# The values are `ROOM_OPERATION_MODE_*` and `OPERATION_MODE_*`.
#
# **The app carries two families of names.** Not knowing that at first mixed one of them in.
#
#   list form     STR_FRAGMENT_AIRONE_CONTROL_MODE_*_TITLE   환기모드 · 청정모드 · 자동운전
#                 (`AironeModeListAdapter` — the mode picker screen)
#   display form  airone_*                                    환기 · 청정 · **자동**
#                 (`AirOneControlFragment` — the current mode on the control screen)
#
# **The display form is used**, because that is what the user sees on a dashboard. It has no
# "mode" suffix and reads naturally next to an option (환기 터보, 청정 절전).
#
# Only `12` had been taken from the list form (자동운전) and broke the pattern. A community
# report confirmed it and it was corrected to 자동; the rest matched the display form from
# the start.
#
# Modes the server never sends still get a label. It is only for display, and control comes
# solely from the combinations the server declared, so it is harmless — and an unrecognised
# value never shows up as a bare number.
AIRONE_MODE_NAMES: Final = {
    0: "없음",
    4: "환기",  # 환기모드
    5: "배기",  # 배기모드
    6: "요리",  # 요리모드
    8: "청정",  # 청정모드
    9: "제습",  # 제습모드
    10: "환기제습",
    12: "자동",  # airone_auto. 목록용은 `자동운전` 이지만 표시용을 쓴다
    15: "환기(외기)",  # OPERATION_MODE_AERATION — 앱 명칭 미확인
    17: "바이패스",
    18: "음압환기",
}

# --- Older-generation Airone mode list -------------------------------------
#
# **For the older generation the DID is not the capability list.** The app never reads it.
#
#     AirOneControlViewModel:
#         if (modelCode < 1000) { loadlegacyModeDataFromFile(); ... }
#     -> reads assets/jsons/airone_legacy_mode_did.json and uses it as modeDidList
#
# What follows is that file (APK 2.10.4) transcribed verbatim. Nothing is guessed.
#
# **Bypass (17) is not in that file; the app adds it after checking device capabilities.**
#
#     if (did.supportByPass == 2)          modeDidList.add(ModeDid(17, 1, …))
#     if (did.ventiMode.basalairUse == 2)  modeDidList.add(ModeDid(4, 6, …))   baseline
#     if (did.kitchenMode.autoUse == 2)    sets the cooking fan speed to auto (4)
#
# **Those flags are not readable here.** They live in `DidPacket`, which comes back from a
# separate MQTT DID request and is absent from the REST device list, so the conditions cannot
# be evaluated.
#
# There are three reasons 17 is included anyway.
#
# 1. Bypass appears on the app screen of **two** real devices (`NRT-20DS`, `NRT-20DSW`)
# 2. A contributor confirmed the 17 command takes effect — even on a device whose DID never
#    declared it
# 3. Leaving it out removes a feature both devices actually have
#
# **Doing this properly means requesting the DID over MQTT and reading `supportByPass`.**
# That is the outstanding work, and until then baseline ventilation (4, 6) and auto cooking
# fan speed stay out.
#
# This is **one table per generation**, not per model — the app uses the same one for every
# older unit.
#
# **The `additionalData` in that file is not humidity.** Every entry is
# `{type:1, min:0, max:4}`, which is the fan-speed range (0-4). It collides with `type:1`
# meaning the humidity range in the newer protocol, so passing it through would produce a
# "target humidity 0-4%". It is not carried here.
#
# **`configurable` was dropped at first (corrected in v0.13.1).** The value is in the file
# and was lost in transcription, replaced by a guess that `option == 1` meant selectable.
# That guess was wrong for auto and cooking — both are `false`, yet gentle, low and high were
# being offered. It is why an older reporter saw four fan speeds under auto.
#
# (mode, option, default air volume, configurable)
LEGACY_MODE_DID: Final = (
    (12, 1, 4, False),   # auto — fan speed fixed
    (4, 1, 1, True),     # ventilate
    (6, 1, 3, False),    # cooking — fixed at high
    (4, 2, 3, False),    # ventilate turbo
    (4, 3, 4, False),    # ventilate saving
    (4, 4, 4, False),    # sleep — the app shows (4,4) as its own mode
    (8, 1, 1, True),     # purify
    (8, 2, 3, False),    # purify turbo
    (8, 3, 4, False),    # purify saving
    # Bypass is absent from the bundled file. Three newer units all report
    # `configurable: true`, and that is the only evidence there is, so it is followed.
    # **Not attached to every older model** — see `LEGACY_BYPASS_MODEL_PREFIXES`.
    (17, 1, 4, True),
)

# --- Bypass on older models — **only where it is confirmed to exist** ------
#
# The app decides from a flag the device declares.
#
#     if (did.supportByPass == 2)  modeDidList.add(ModeDid(17, 1, …))
#
# **That flag exists only in the MQTT DID, which is unreadable here.** So v0.11.0 through
# v0.14.2 attached bypass to every older model, and report #13 for the `NRT-20D` revealed
# that some devices do not have it.
#
# | model | bypass | evidence |
# | --- | --- | --- |
# | `NRT-20DS`, `NRT-20DSW` | yes | app screen; command 17 confirmed to apply (PR #7) |
# | `NRT-21DS` | yes | the `S` grade of the same family |
# | `NRT-30` | yes | product literature — higher specification |
# | `NTR-10PW` | yes | product literature — a different family |
# | `NRT-20D`, `NRT-21D` | **no** | report #13 and product literature — base model |
#
# **An "ends in S" rule does not work.** `NTR-10PW` is the counterexample: no `S` and bypass
# present. So the names are listed explicitly.
#
# **An unknown model does not get it.** v0.14.3 tried the opposite — excluding only what was
# confirmed absent — and was reverted. There are only six older families, so the list is
# manageable, and **showing one that does not exist is the worse failure**: pressing it stops
# the outdoor unit and the user has to undo it from the app. A missing one only needs a line
# in a report to add.
#
# A prefix match is enough — `NRT20DS` also catches `NRT20DSW`. `[^A-Z0-9]` is stripped
# before comparing, because the server may send `NRT20D`.
LEGACY_BYPASS_MODEL_PREFIXES: Final = ("NRT20DS", "NRT21DS", "NRT30", "NTR10PW")

AIRONE_MODE_BYPASS: Final = 17

# An older DID carries no `supportedAirVolumes`. For a `configurable` combination the app's
# own table is used — a contributor confirmed on a real device that gentle, low, high and
# auto all work.
LEGACY_DEFAULT_AIR_VOLUMES: Final = (1, 2, 3, 4)


# The older status frame carries values the newer one lacks. They are never interpreted for
# control, only surfaced as diagnostic sensors, and only the ones whose meaning is confirmed
# get a name. Names follow the app; where the meaning is unconfirmed the original field name
# stays in parentheses, so an odd value can be traced to its field at a glance.
# The tuple is (field, display name, value map). With a map, a name is shown instead of a
# number — only where the table came from the app, and **items whose 1/2 flag scheme is
# unconfirmed get no map.** Guessing at on and off could show the exact opposite.
LEGACY_EXTRA_FIELDS: Final = {
    "radonStageValue": ("radon_stage", "라돈 단계", "level"),
    "freeFilterUsedTime": ("filter_used_time", "필터 사용 시간", None),
    "freeFilterCleanAlarmFlag": ("filter_clean_alarm", "필터 청소 알림", None),
    "hepaFilterCleanAlarmFlag": ("hepa_filter_clean_alarm", "헤파필터 청소 알림", None),
    "deepSleepMode": ("deep_sleep_mode", "숙면 동작", None),
    "bypassOperation": ("bypass_operation", "바이패스 동작", None),
    "connectedSensingBox": ("connected_sensing_box", "센싱박스 연결", None),
    "supportedOperationMode": ("set_operation_mode", "설정 운전모드", "mode"),
    "oduOperationMode": ("odu_operation_mode", "실외기 동작모드", "mode"),
    "desiredAirVolume": ("set_air_volume", "설정 풍량", "wind"),
    "airVolume": ("actual_air_volume", "실제 풍량", "wind"),
    "errorState": ("error_state", "오류 상태", "error"),
}


# --- Older-generation status frame (Airone `modelCode < 1000`) -------------
#
# The envelope is `{topic, payload: {...}, serviceCode}` — **a flat structure with neither
# `reported` nor `roomController`**. The field names differ from the newer protocol and are
# actively **misleading**, as confirmed by setting bypass from the app on a real device
# (NRT-20DSW, modelCode 8) and comparing all 45 fields.
#
# | older field | what it actually means |
# | --- | --- |
# | `supportedOperationMode` | the **selected** operating mode (not a capability list) |
# | `oduOperationMode` | the mode the outdoor unit is **actually running now** (1 = stopped) |
# | `desiredAirVolume` | the **selected** fan speed |
# | `airVolume` | the **actual** airflow (0 when it is not blowing) |
#
# Reading the actual values makes bypass look like a phantom mode and auto look
# unobservable. So **the selected values are read**, and the actual ones stay in diagnostics.
LEGACY_STATUS_TO_CONTROLLER: Final = {
    "supportedOperationMode": "mode",
    "optionFunction": "option",
    "desiredAirVolume": "airVolume",
}

# The actual values. Never used as control state; diagnostics only.
LEGACY_ACTUAL_FIELDS: Final = ("oduOperationMode", "airVolume")

# The `running` value is **inverted** relative to the newer protocol (see the CHANGELOG entry
# about skipping older units). Ignoring the generation flips the power state, so the older
# `isRunning` 2 = running maps to the newer `running` 1 = running.
LEGACY_RUNNING_TO_V2: Final = {2: AIRONE_RUN_ON, 1: AIRONE_RUN_OFF}

# The older command envelope. The newer protocol uses `payload.state.desired`, the older one
# `payload.request`.
LEGACY_CONTROLLER_TO_REQUEST: Final = {
    "mode": "operationMode",
    "option": "optionMode",
    "airVolume": "windLevel",
}
LEGACY_RUNNING_TO_REQUEST: Final = {AIRONE_RUN_ON: 2, AIRONE_RUN_OFF: 1}

# `ROOM_OPERATION_OPTION_*`. The names come from the app's control-screen strings
# (`STR_FRAGMENT_AIRONE_CONTROL_OPTION_*`). 1 means "no option" and so has no label.
AIRONE_OPTION_NONE: Final = 1
AIRONE_OPTION_SLEEP: Final = 4
AIRONE_OPTION_NAMES: Final = {
    2: "터보",
    3: "절전",
    AIRONE_OPTION_SLEEP: "숙면",
    5: "기저",
    6: "기저",
}

# `ROOM_OPERATION_WIND_*` plus the app strings `..._CONTROL_WIND_1~3`.
# **Nothing outside this table is ever sent** — `ModeDid.airVolume` may yet turn out to be a
# bitmask (see spec 6-5).
#
# SCADA (commercial) uses a different table in the same position (5 = sleep airflow,
# 6 = saving, 7 = turbo). The two must never be mixed — this is the residential
# room-controller table.
AIRONE_WIND_NAMES: Final = {
    1: "미풍",
    2: "약풍",
    3: "강풍",
    4: "자동",
    5: "기저",
    6: "기저",
}

# **The fan speeds selectable when the server sends no list.**
#
# A field called `supportedAirVolumes` carries that list, but **it does not exist in APK
# 2.10.4**. The server added it later, and devices on older firmware still do not send it.
# What the app consults in that case is `configurable`
# (`AirOneControlFragment.allowedWindChoicesFromDids`).
#
#     z10 = any entry of that mode has configurable true
#     if (z10) { show gentle, low and high }
#     else     { only those whose airVolume is 1, 2 or 3 }
AIRONE_SELECTABLE_AIR_VOLUMES: Final = (1, 2, 3, 4)

# With `option != 1` the app shows the option label instead of the fan speed. Sleep (4) is
# the one exception that uses both (`AironeModeCode.labelFor`).
AIRONE_OPTIONS_WITH_WIND: Final = frozenset({AIRONE_OPTION_NONE, AIRONE_OPTION_SLEEP})

# The app gives sleep **its own mode button** (`STR_..._MODE_SLEEP_TITLE` = 숙면모드, and
# `AironeModeCode.rawToUi` hides the underlying mode and returns 1001 when option is 4). So
# sleep goes in the operating-mode list, not the fan-speed list.
AIRONE_MODE_SLEEP_LABEL: Final = "숙면"

# Target humidity is shown only in dehumidify and ventilating-dehumidify. The range comes
# from the server's `additionalData` min/max — no numbers are written here.
AIRONE_MODES_WITH_HUMIDITY: Final = frozenset({9, 10})

# **The `type` number differs between sending and receiving.** Not a guess — confirmed by a
# real-device report.
#
# Sending uses `1`. The server's capability range arrives as
# `{"type": 1, "min": 40, "max": 65}`, and sending with that number **leaves the value
# visible in the app as well** (the reporter verified it there).
#
# Receiving uses `3`. Device state carries it in `additionalData` as
# `{"type": 3, "value": 60}`. The `type: 1` entry in that same list is **a different item**,
# with a value of `1` and a range of 0-4. Up to v0.9.1 that was searched for the humidity,
# never found, and the field stayed empty.
#
# The number alone is not relied on — a value inside the range the server declared is
# checked alongside it.
AIRONE_HUMIDITY_TYPE: Final = 1
AIRONE_HUMIDITY_REPORT_TYPE: Final = 3

# Auto-dry progress. **Found in the app code and settled on a real device.**
#
# > Contributor (moKorean, PR #15): "실기기에서 자동 건조 진행도 잘 표시됨을
# > 확인했습니다" (2026-08-01)
#
#     do { ... } while (additionalDataStatusPrevious.getType() != 4);
#     value = additionalDataStatus.getValue();
#     textView8.setText(Util.format("자동건조 중 %02d%%", value));
#
# **It scans from the end and takes the last one** (`listIterator(size())` plus
# `previous()`), treating the later of two identical numbers as the current value. This does
# the same.
AIRONE_AUTO_DRY_TYPE: Final = 4

# The app's target-humidity -/+ buttons move in steps of 5
# (`AirOneControlFragment`: `setProgress(getProgress() ± 5)`). The server sends only min and
# max and never a step, so the app is followed.
AIRONE_HUMIDITY_STEP: Final = 5

# `SENSOR_LEVEL_*`
AIRONE_LEVEL_NAMES: Final = {
    0: "알 수 없음",
    1: "좋음",
    2: "보통",
    3: "나쁨",
    4: "매우나쁨",
}

# The server may name the same sensor differently. Names are lower-cased and then normalised
# through this table.
#
# **Keys must not be normalised mechanically.** Stripping separators turns `pm1.0` into
# `pm10` and **collides with a different sensor** — PM1.0 and PM10 are separate items. So
# there is no rule, only an explicit table.
AIRONE_SENSOR_ALIASES: Final = {
    # standard names differing only in case
    "pm1dot0": "pm1Dot0",
    "pm2dot5": "pm2Dot5",
    # particulates — listed one by one so the `pm1` family never merges with `pm10`
    "pm1": "pm1Dot0",
    "pm1.0": "pm1Dot0",
    "pm1_0": "pm1Dot0",
    "pm25": "pm2Dot5",
    "pm2.5": "pm2Dot5",
    "pm2_5": "pm2Dot5",
    # radon
    "radonvalue": "radon",
    "radon_value": "radon",
    "radonbq": "radon",
    "radonbqm3": "radon",
    "radonstagevalue": "radon",
    "radon_stage_value": "radon",
    "radonconcentration": "radon",
    "radon_concentration": "radon",
    # volatile organic compounds
    "voc": "tvoc",
    "t_voc": "tvoc",
    "tvocvalue": "tvoc",
    # overall air quality
    "airquality": "total",
    "air_quality": "total",
    "airqualityscore": "total",
    "air_quality_score": "total",
    "totalairquality": "total",
    # everything else
    "co2value": "co2",
    "carbondioxide": "co2",
}

# The `airs[].type` of `/air-sensor` — a string, on a different scheme from the integer
# `SensorDid.type`. A kind absent from this table gets no entity, only a log line.
#
# A `unit` of None does not mean "there is no value" but **the unit was never confirmed.**
# Because the app **displays** `tvoc` and `radon` as grades, they were taken to have no
# value, until user reports confirmed numbers do arrive (a radon reading, TVOC 70.0, overall
# 82.0). `getValueText` is only a display function; `Air.value` holds the number.
#
# Units are decided by how strong the evidence is. **The only unit strings the app uses are
# `"ppm"` and `"㎍/㎥"`** (exhaustive dex search). The rest follow the reasoning below.
#
# `radon` uses `Bq/㎥`. **Not a guess — this market has only one unit.** Korean indoor air
#   quality standards are all in Bq/㎥ (148 Bq/㎥ for public facilities and schools), and
#   `pCi/L` is used only in the United States. A device sold in Korea reporting radon as a
#   number is reporting Bq/㎥. The app shows only a grade, so no string exists — but that
#   does not mean the value has no unit.
#
# `tvoc` is left empty. Three candidates compete — `㎍/㎥` (Korean standard 500), `ppb`
#   (consumer sensor convention) and an index (0-500) — and the observed value of 70 fits all
#   three. **A coin toss is left empty.** Radon is a different situation.
#
# `total` has no unit. The device screen shows only a score, as "통합공기질 78".
#
# The grades for both live in the `grade` attribute — the same values the device screen shows.
#
# HA uses `device_class` for icons, history graphs and unit conversion. Radon has no standard
# HA device_class, so only the unit is attached.
#
# **Radon stays in the table, but its value is not trusted.** A reporter's NRT-20D was
# confirmed to have no radon sensor, and the server still sends `radon` and `radonStageValue`
# as 0 — it does not distinguish "no sensor" from "0 Bq/㎥". The entry is not removed: another
# unit may well have the sensor, and removing it would throw that device's value away. A
# reading that is always 0 means there is no sensor, and the entity can simply be hidden in HA.
AIRONE_SENSOR_KINDS: Final = {
    "pm1Dot0": ("극초미세먼지", "\u00b5g/m\u00b3", "pm1"),
    "pm2Dot5": ("초미세먼지", "\u00b5g/m\u00b3", "pm25"),
    "pm10": ("미세먼지", "\u00b5g/m\u00b3", "pm10"),
    "co2": ("이산화탄소", "ppm", "carbon_dioxide"),
    "tvoc": ("휘발성유기화합물", "ppb", None),
    "radon": ("라돈", "Bq/㎥", None),
    "temperature": ("온도", "°C", "temperature"),
    "humidity": ("습도", "%", "humidity"),
    "total": ("통합공기질", None, None),
}

# Items whose unit was a judgement call rather than something extracted from the app. A
# report showing one is wrong is what corrects it. They are listed in diagnostics so it is
# visible from outside which units rest on no direct evidence.
#
# **The app prints only four units on screen** (exhaustive resource check, APK 2.10.4):
#
#     극초미세먼지 PM1.0 (㎍/㎥)   초미세먼지 PM2.5 (㎍/㎥)
#     미세먼지    PM10  (㎍/㎥)   이산화탄소 CO2   (ppm)
#
# For TVOC and radon **the app shows no unit either.** Its explanation screen titles end at
# "휘발성 유기화합물 TVOC" and "라돈 RADON". Neither `ppb` nor `Bq` appears anywhere in the
# resources or the code.
#
# They are attached anyway because **a number with no unit cannot be read.** For both, the
# field has effectively one unit in practice. A report showing otherwise corrects it.
AIRONE_INFERRED_UNITS: Final = frozenset({"radon", "tvoc"})


def airone_mode_label(mode: int, option: int) -> str:
    """운전 모드 조합을 앱과 같은 말로 옮긴다.

    앱은 `숙면` 일 때 원래 모드를 감추고 「숙면」만 보여주지만
    (`AironeModeCode.rawToUi` 가 1001 을 반환한다), 여기서는 감추지 않는다.
    `(9,4)` 와 `(4,4)` 가 둘 다 있으면 목록에 같은 이름이 두 개 생겨서
    사용자가 고를 수 없게 된다.
    """
    base = AIRONE_MODE_NAMES.get(mode, f"알 수 없음({mode})")
    suffix = AIRONE_OPTION_NAMES.get(option)
    return f"{base} {suffix}" if suffix else base

# --- control axis (heatControl.unit) --------------------------------------

# An exhaustive dex search finds only these two values, in `<step><axis>` form.
UNIT_LEVEL: Final = "1.0L"  # heater.*.level.set — integer steps
UNIT_CELSIUS: Final = "0.5C"  # heater.*.temperature.set — 0.5-degree steps
KNOWN_UNITS: Final = (UNIT_LEVEL, UNIT_CELSIUS)

# --- operationMode (the KDMode.WIFI map) ----------------------------------

MODE_POWER_OFF: Final = 0
MODE_HEAT: Final = 1
MODE_RESERVE: Final = 2
MODE_SLEEP: Final = 3
MODE_STERILIZE: Final = 4
MODE_DISCHARGE: Final = 5
MODE_ERROR: Final = 6
MODE_CUSTOM_SLEEP: Final = 7
MODE_AISLEEP: Final = 8
MODE_CHANGE_HEAT: Final = 99
MODE_BED_DRYING: Final = 129
MODE_HYPER: Final = 130

MODE_NAMES: Final = {
    MODE_POWER_OFF: "전원 꺼짐",
    MODE_HEAT: "난방",
    MODE_RESERVE: "예약",
    MODE_SLEEP: "수면모드",
    MODE_STERILIZE: "살균",
    MODE_DISCHARGE: "배수",
    MODE_ERROR: "오류",
    MODE_CUSTOM_SLEEP: "개인맞춤 수면",
    MODE_AISLEEP: "AI 수면",
    MODE_CHANGE_HEAT: "난방 전환",
    MODE_BED_DRYING: "침대 건조",
    MODE_HYPER: "하이퍼",
}

# Mat error-code names, **transcribed from the device manual by a reporter** (2026-07-31).
#
# **Applied only to temperature mats (`0.5C`).** Its mentions of water tanks, circulation
# pumps, UV lamps, cooling units and leaks mark it as a hot-water or four-season manual. There
# is no evidence the same numbers mean the same things on a carbon (stepped) mat, so stepped
# mats show only the number.
#
# The state stays a number and the name appears **only as an attribute**. Turning the state
# into text would break automations and templates already using the value.
MAT_ERROR_NAMES: Final = {
    0: "정상",
    2: "물 부족 (Er 02)",
    5: "물탱크 공급 온도 센서 (Er 05)",
    7: "외기 온도 센서 (Er 07)",
    8: "순환펌프 동작 (Er 08)",
    9: "팬 이상 (Er 09)",
    11: "수위 감지 (Er 11)",
    15: "UV램프 (Er 15)",
    16: "물탱크 과열 (Er 16)",
    17: "난방 이상 (Er 17)",
    18: "온도센서 (Er 18)",
    26: "냉각장치 과냉 (Er 26)",
    27: "냉방 이상 (Er 27)",
    28: "누수 (Er 28)",
}

# The modes that count as powered on. `MODE_ERROR` is excluded.
MODES_ON: Final = frozenset(
    {
        MODE_HEAT,
        MODE_RESERVE,
        MODE_SLEEP,
        MODE_STERILIZE,
        MODE_CUSTOM_SLEEP,
        MODE_AISLEEP,
        MODE_CHANGE_HEAT,
        MODE_BED_DRYING,
        MODE_HYPER,
    }
)

# --- heating zones ---------------------------------------------------------

ZONE_SINGLE: Final = "single"
ZONE_LEFT: Final = "left"
ZONE_RIGHT: Final = "right"

ZONE_NAMES: Final = {ZONE_SINGLE: "난방", ZONE_LEFT: "좌측", ZONE_RIGHT: "우측"}

# `modelType`. Inferring from the prefix is wrong — the EMW750 starts with EMW yet is
# four-season.
MODEL_TYPE_LABELS: Final = {"em": "카본", "wm": "온수", "fm": "사계절"}

# --- season (four-season models) -------------------------------------------

# `Constants.SUMMER_SEASON` and `WINTER_SEASON`. What the value 1 is for is unconfirmed.
SEASON_WINTER: Final = 0
SEASON_SUMMER: Final = 2
SEASON_NAMES: Final = {SEASON_WINTER: "난방", SEASON_SUMMER: "냉방"}

# --- button sound volume ---------------------------------------------------
#
# **The app's volume screen has four steps** — mute plus three levels.
# `MateDeviceSettingSoundVolumeFragment` sets `selectedIndex` to one of 0, 1, 2 or 3 and
# passes it straight to `setSoundVolume(int)`. Reading splits on the same values
# (`initializeCurrentVolumeLevel`: the mute icon at 0, one each for 1 and 2, and 3 for
# everything else).
#
# The field sent is `Desired.volume` (`Integer`) and the command is `control-volume`. The
# labels follow the icon names (`mute`, `volumeLevel1~3`) rather than the app's screen text.
MAT_VOLUME_NAMES: Final = {0: "음소거", 1: "1단계", 2: "2단계", 3: "3단계"}

# On a four-season model, `season` selects which control descriptor applies: `coolControl` in
# summer and `heatControl` otherwise. Both share the `heater` setpoint path, and there is no
# cooling-specific operationMode in the `KDMode.WIFI` map.
#
# v1 creates no control entities for four-season devices: the value scheme is unconfirmed,
# and showing a cooling device against the heating range misleads the user. A real-device
# report is what opens it.

# Single versus double comes from `mcu.capacity`, not `matType` (observed: both read 1).
CAPACITY_SINGLE: Final = 1
CAPACITY_DOUBLE: Final = 2

# --- stepped control labels ------------------------------------------------

# 0 is a state, not a number. It is the leftmost position of the app's slider and sits below
# the `heatControl.rangeMin` the server reports (observed: 1). It arrives as `level 0`
# together with `enable false`.
LEVEL_STANDBY: Final = 0
LABEL_STANDBY: Final = "운전 대기"


def level_label(level: int) -> str:
    """Render a step value with the same wording as the app."""
    return LABEL_STANDBY if level == LEVEL_STANDBY else f"{level}단계"

# --- config keys -----------------------------------------------------------

CONF_HOME_SEQ: Final = "home_seq"

# For the initial sync after a reconnect. State arrives by MQTT push, so there is no reason
# to poll frequently.
UPDATE_INTERVAL_SECONDS: Final = 900

# Shorter when an Airone is present. Air-quality values never arrive over MQTT and have to be
# read from `/air-sensor`, and a particulate reading refreshed every fifteen minutes is
# useless.
AIRONE_UPDATE_INTERVAL_SECONDS: Final = 300

# **The floor on refresh requests from outside — kept equal to the polling interval.**
#
# `homeassistant.update_entity` can wake the coordinator at any time. HA's default floor is
# 10 seconds, so an automation firing every 10 seconds has one account hitting Navien's
# servers 8,640 times a day. This is an unofficial client: being blocked would not stop with
# that one person, it would stop the integration for everyone.
#
# **This was 60 seconds at first**, on the reasoning that the app reads once a minute and
# could serve as the ceiling. That was wrong: the app's minute lasts **only while someone is
# looking at the screen**, while this runs around the clock. As a floor it would permit five
# times our own default (five minutes), making the floor looser than the default.
#
# So this is a rule rather than a chosen number: **nothing outside can wake us faster than
# our own polling interval.** `_tune_interval()` moves this along with the interval (900
# seconds for mats alone, 300 when an Airone is present).
#
# The constant here is the **starting value**, matching the interval at the moment the
# coordinator is built. If the first poll finds an Airone, `_tune_interval()` brings this down
# to 300 seconds with it.
#
# The first request is still served immediately (`immediate=True`) — "refresh now" must not
# stop working. What is throttled is **repetition**, not a single call.
MIN_REFRESH_COOLDOWN_SECONDS: Final = UPDATE_INTERVAL_SECONDS

# The table that renders diagnostic values as human-readable names. It sits after the tables
# above, which it depends on.
LEGACY_VALUE_TABLES: Final = {
    "mode": AIRONE_MODE_NAMES,
    "wind": AIRONE_WIND_NAMES,
    "level": AIRONE_LEVEL_NAMES,
    "error": {0: "정상"},
}
