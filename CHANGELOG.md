# Changelog

## Unreleased — NR-67D boiler support

- Reads the `smarttok` MQTT status confirmed on an `NR-67D` and surfaces it as temperature
  and humidity sensors.
- Applies the observed scale factors: indoor temperature and humidity in 0.1 units, every
  other temperature in 0.5C units.
- Corrected `operationBusy=1` to idle and `2` to heating, by cross-checking the app code, the
  official manual and temperature changes on a real device. The selected `operationMode` is
  shown as a human-readable name on its own sensor, while the raw mode and error codes stay
  as disabled diagnostic sensors.
- Strips identifiers — MQTT topics, sessions, MAC addresses, controller serial numbers —
  from diagnostics.
- Controls the hot-water and heating-water setpoints with the commands confirmed in the
  `modelCode=20` branch of Navien Smart 2.10.4. It follows the server's range and accepts
  only 0.5C steps.
- Sends a setpoint command regardless of whether the boiler is heating, and re-reads the real
  state afterwards rather than updating the value optimistically.
- Requests status once when five minutes pass with no traffic in either direction. A status
  arriving first pushes the timer five minutes out again, so nothing is polled needlessly.
- Adds the power, fast-hot-water, fast-hot-water smart-operation and turbo-hot-water switches
  confirmed on the app screen and in the `modelCode=20` calls. They are created only on
  devices whose server data declares the feature.
- Adds this month's gas usage as an m3 sensor, with the heating and hot-water figures as
  attributes. The monthly total refreshes hourly through a separate gas query.
- Selects Home Assistant's local date from the app's daily array and shows it as a
  "오늘 가스 사용량" sensor. At midnight the date basis rolls without waiting for a network
  query.
- **Both gas sensors export `last_reset`.** The `TOTAL` state class treats a cycle as
  finished only when `last_reset` changes (HA's `sensor/recorder.py`), so without it the
  month-end drop to zero read as a decrease rather than a reset and that month fell out of
  the long-term statistics entirely. The monthly sensor's cycle start is the first of **the
  month the server named**, not the wall clock.
- **Keeps gas usage history as long-term statistics.** One gas query returns everything the
  app's gas-usage screen draws — two months of daily figures (`gasMeterLastMonth`,
  `gasMeterThisMonth`) and two years of monthly ones (`gasMeterLastYear`,
  `gasMeterThisYear`). The four arrays merge into one series and go in as external statistics
  under three headings: total, heating and hot water. An overlapping month keeps only its
  daily figures so the same usage is never counted twice, and a future date whose values are
  `null` means no data rather than zero usage.
- Every query rewrites that whole span. Rows at the same timestamp are overwritten, so Home
  Assistant can be off for days and repair itself on the next query, and it follows along
  when Navien corrects a figure later. An older span that has fallen outside the range the
  server returns is spliced onto the cumulative total already stored, so nothing goes out of
  line.

- **Opens up status fields that used to be read and discarded.** Of the 44 status fields an
  `NR-67D` sends, 25 were parsed and never used. Only those whose meaning is certain are
  surfaced.
  - **Outside temperature** — in 0.1C units, and **not measured by the boiler but a regional
    weather observation**. When this read 27.0C the KMA observation for Seoul at the same
    moment was exactly 27.0C while the neighbourhood estimate was 25.8C, and every value seen
    so far has been a whole degree. `outsideTemperatureDisplayUse` is not used as a
    condition: it governs whether the room controller displays the value, not whether the
    data arrives, and this device has it at 1 while the temperature arrives normally.
  - **Hot water in use** — `DHWUse`, on the same 1=off, 2=on scheme as the hot-water feature
    switches. It turns on when a tap opens, so it can detect a shower or the washing-up.
  - **Hot-water flow, heating flow, Wi-Fi signal and fault status** — diagnostic items. The
    Wi-Fi signal keeps only its number because the unit was never confirmed, and the fault
    bits only report whether they are non-zero, because what each bit means is unknown.
- **Heating intensity is read only.** Neither the app nor the manual names the steps, and the
  range the server sends has `heatingIntensityMin: 3` above `heatingIntensityMax: 1`, so even
  the direction cannot be settled. Only the raw step value is shown.
- **Schedules are read only too.** It shows whether the weekly and fast-hot-water schedules
  are on, what the repeat interval is, and the raw 24-hour schedule. What each position of
  that table means is not interpreted, and schedules are never written — that is a separate
  protocol which overwrites the whole timetable, so a wrong command erases the real one.
- **The running state `히팅` becomes `연소`,** the word the official `NCB753` manual uses.
  `히팅` appeared in neither the manual nor the app.
  > **Check your automations.** Any automation comparing the running state against `히팅`
  > has to be changed to `연소`.
- **Error codes carry the manual's names.** The "12. 자가 진단 조치 방법" table of the
  official `NCB753` manual was transcribed verbatim, giving an `E001`-style label and a fault
  description such as "열교환기 과열" as attributes. This reads the integer the server sends
  as the manual's three-digit number rather than reproducing errors on a real device, so **a
  number absent from the table is left unnamed and shown as a number.**
- An exhaustive read of the manual shows **there is no "heating intensity" setting.** The
  only things adjusted in steps are the screen brightness and the proximity sensor. The
  manual's "gas saving" is an icon on the display rather than a setting, and the status value
  that lights it has not been found.
- The room controller echoes the last command code it processed back in its status, so every
  code seen is collected into diagnostics. It is a way to learn a command whose meaning is
  still unknown — an operating mode, say — **without guessing**: pressing that feature once
  in the app reveals the code.

### Air-quality sensors disappearing on restart

**While the server was sending only some of the air-quality kinds, restarting Home Assistant
turned the remaining sensors unavailable wholesale.** Even when the values came back, they
did not revive until another restart.

On a real `NRT-20D` this cut off five of them: CO2, particulates, radon, volatile organic
compounds and the overall index. When the air monitor drops out, the server sends only
temperature and humidity.

Within a session an empty response was already prevented from erasing values received
earlier, but **entities are created only once at startup, so that protection did not survive
a restart.**

Now **every kind that has ever produced a value is stored and restored on the next start.**
The values are not restored — showing a days-old number as if it were current is worse than
unknown. With the entity in place, the history resumes as soon as values return.

Diagnostics gained `air_sensor_kinds_known` and `air_sensor_kinds_missing`, so which kinds
were absent from a given poll is visible from outside.

**A "공기질 자료 끊김" sensor now reports the loss.** The problem was that nothing signalled
it at all: no error code was sent and the query still succeeded, so it showed up nowhere. It
turns on when an item that used to arrive stops arriving, and names what is missing in its
attributes. A loss during a session is logged once as well.

The device never claims to have no sensor, so this does not call it a fault — it reports only
the observation that **we used to receive it and now do not**.

**A newly seen kind is persisted during the air-quality query itself.** Kinds are only
updated by that query while saving happened on an MQTT report, so a kind that returned never
reached disk; every restart then restored the old set and the returned item stayed without an
entity. But **nothing is written before the restore runs**: the first poll happens before it,
and writing then would erase the last stored state with our own hands.

**Radon permanently reading `0` means that device has no radon sensor.** The server sends `0`
whether or not the sensor exists and never says it is absent, so this cannot be told apart
here either. Confirmed on a real `NRT-20D`. The item is not removed — another unit may have
the sensor, and removing it would throw that device's value away.

**A case was confirmed where the away operating mode takes effect from neither the official
app nor the room controller's physical button.** In that installation the room controller and
the boiler are wired through a contact, so the mode may simply not be conveyable over that
wiring, or the device may be faulty; the evidence at hand cannot separate the two and a
question to the manufacturer is needed. The conclusion, not to open the command, is unchanged.

### Fixes from the full review

**Restored air-quality kinds were disappearing five minutes later.** Device objects are
rebuilt on every poll with the values to carry over listed by hand, and "kinds ever seen" was
missing from that list. What the restore read from storage was narrowed back down on the next
poll to whatever was arriving, and that narrowed set overwrote the stored copy. The restore
above was therefore only effective while the server was sending every kind.

**Changing the password never opened the re-authentication screen.** The confirmation step
existed but the flow's entry point (`async_step_reauth`) did not. The only remedy was to
delete and reinstall the integration, which severs every entity id and all long-term
statistics.

**Two problems in the long-term gas statistics were fixed.** Overlapping writes could
corrupt the running total, so only one now runs at a time per device. And when the range the
server returns moved forward with no stored row at that point, two years of data restarted
from zero and drew a **huge negative usage** at that boundary — the last preceding total is
now found and spliced on.

**Leaving the Navien app open could produce a reconnect every five seconds.** The retry
interval was reset as soon as the connection was accepted, so when the link dropped
immediately after connecting the interval never grew, and each time an initial status request
went out to every device. It now resets only after the connection has held for 60 seconds.

Also: the device identifier is redacted from the warning that asks users to attach it to an
issue (the diagnostics export redacts the same value, so the two disagreed); three one-shot
timers left running are cancelled when the integration is unloaded; requests that fail at the
same time no longer each log in (one session per account means they invalidate each other);
and the setpoint slider follows the range the server reports now.

## v0.18.0 — 2026-08-10

**The filter sensor name was corrected, and the tests were added to the repository.**

### It is "필터 잔량" (life remaining), not "필터 사용률" (usage) — report and PR #18

A value of `87` means **87% remaining**. Under the old name it read as "87% used", which is
**exactly the opposite meaning** — and this is the value people use to judge when to replace
a filter, so the judgement flipped.

The reporter compared it against the app on a real device: sensor `87`, app "필터 87% 남음",
actual usage 13%.

We confirmed it independently by taking the app apart. Its filter-management screen decides
like this:

| value | app text |
| --- | --- |
| 76 and above | 필터가 충분하네요! |
| 41-75 | 아직은 여유있네요! |
| 11-40 | 곧 필터를 교체해야 해요! |
| 10 and below | 필터를 교체해 주세요! |

**A higher value means "plenty left".** For a usage figure it would be the other way around.

**The value itself is unchanged.** It was the remaining life from the start, so everything
recorded so far is already correct. Inverting it here would make the same number mean the
opposite on either side of this version.

**The entity ids are unchanged too**, since only the display name moves, so dashboards and
automations keep working.

### The tests were added to the repository

There is now a `tests/` directory.

```bash
python3 tests/run.py
```

**It runs without Home Assistant installed**, and needs no external packages.

When you send a report or a code change, you can check for yourself that what changed did not
disturb anything else.

## v0.17.2 — 2026-08-05

**A zone that had been turned off still sometimes failed to come back on.** Taking the app
apart again settled two places.

### Off was being decided from the wrong field

Our code was deciding **the same question differently in two places**.

| | decided from |
| --- | --- |
| showing "off" on screen | the **enable flag** the device sends |
| whether to raise the temperature when turning on | the **temperature value** |

The Navien app reads **only the enable flag** in both places, and it does so deliberately: it
does not trust the temperature a powered-off zone reports and overwrites the display with
"off".

**We now match the app**, so the display and the command rest on the same evidence.

### A zone could drop out of the command even when asked to turn on

When the device sent no temperature for a zone, we **left that zone out of the command
entirely**. Pressing "turn the right side on" produced a command with no right side in it, so
the device powered on and the zone stayed as it was.

**A zone you asked to turn on is now always in the command.** With no value known, it turns
on at the minimum temperature.

### Does this affect what you already use?

| | |
| --- | --- |
| **carbon (stepped) mats** | **no effect** — both readings always give the same answer |
| setting a temperature, turning off | unchanged |
| a zone that is already on | its value is untouched: 33C stays 33C |
| Airone, the power switch, the control lock | unrelated |

The only thing that changes is **turning a powered-off zone on**, and the direction of the
change is "something that did not work now does".

## v0.17.1 — 2026-08-04

**Fixed a zone that had been turned off failing to come back on** — the half that v0.17.0
left behind.

### Turning on did not raise the temperature

v0.17.0 fixed "turning off has to lower the temperature below the minimum", but **the same
calculation never went into turning on**.

Turning one side off drops that zone's temperature to `27.5`. Turning it on again, we
**resent `27.5` unchanged**. Since 27.5 means "stay off", the device powered on while that
zone remained off.

**A zone that was off is now raised to the minimum temperature when it is turned on** — 28C
while heating, 20C while cooling. It matches pressing `+` from the off state in the app,
which lands on 28.

**A zone that was already on is left alone.** A side set to 33C is not dragged back to 28.

### More confirmed models

On an `EME-520/521` (temperature mat, single and queen) power, temperature and turning the
two sides off independently were all confirmed, thanks to a report.

The server reports **both `EME-520` and `EME-521` as `EME-520`**. We follow the control
scheme the server sends rather than the model name, so both behave identically.

## v0.17.0 — 2026-08-03

**Fixed being unable to turn a mat off.** Report #16 revealed two problems layered on top of
each other.

### Some models never got a power switch

Whether to create the power switch was decided from the `powerCtrl` value the server sends.
**That rule had no evidence behind it.**

The Navien app does not even **read** that value — nothing anywhere in it pulls that field
out. We alone consulted it and withheld the switch, which left `EME-520` owners with **no way
at all to turn the device off**.

**A mat now always gets a power switch.**

Every device reported so far had `powerCtrl: true`. The `EME-520` was the first model to
arrive with `false`, so nobody had hit it before.

### Turning the left or right side off did nothing

On a temperature mat, turning one side off had no effect. **The way to turn it off is
different.**

The app turns a side off by lowering the temperature **one step below the lowest settable
value**.

| mat | minimum | one step | off value |
| --- | --- | --- | --- |
| carbon (stepped) | level 1 | 1 | **0 = 운전 대기** |
| four-season and temperature | 28C | 0.5 | **27.5** |

**This is why stepped mats worked all along.** The `level 0` we were sending was not luck but
the same rule. Only temperature mats were missing the calculation.

### The last remaining side cannot be turned off, and now says why

**The device refuses this.** With one of the two sides already off, the other one will not
turn off.

Until now, pressing it **did nothing at all**. It now shows the same message as the app.

> 이미 다른 편측이 운전대기 상태입니다. 난방을 끄시려면 매트 전원을 종료해 주세요.

**It does not power the device down on your behalf.** The app does not either, and it is not
what was asked for. With the first fix above, every mat now has a power switch to use instead.

### The Airone mode 자동운전 becomes 자동

Corrected as reported: the app's control screen says **`자동`**.

The app carries two sets of names — the mode picker says `자동운전` while the control screen
shows `자동`. Every other name (환기, 청정, 제습, 요리, 환기제습, 바이패스) already came from
the control screen; only this one had been taken from the picker and broke the pattern.

> **Check your automations.** Any automation naming `자동운전` has to be changed to `자동`.
> That covers automations selecting an operating mode or comparing the state.

### Does this affect what you already use?

| | |
| --- | --- |
| dropping one side of a carbon (stepped) mat to standby | **completely unchanged** |
| raising and lowering steps | unchanged |
| anyone who already had a power switch | unchanged |
| `EME-520` owners | a power switch **appears** |
| turning a temperature mat side off | **it now actually turns off** (previously nothing happened) |
| the Airone `자동운전` mode | **becomes `자동`** — check your automations |

## v0.16.0 — 2026-08-01

**Power moves to the top of the list, fixing the tangled ordering on split left/right mats.**

### Power was wedged between the left and right sides

The device page sorts by name in **Korean dictionary order**, and because `ㅇ` sorts before
`ㅈ` the result looked like this:

```
○○ 우측난방 단계     ㅇ
○○ 전원              ㅈ + ㅓ      <- right in the middle
○○ 좌측난방 단계     ㅈ + ㅗ
```

**Power is now the device's primary entity.** Its name becomes the device name itself (`○○`),
so it always sorts first.

```
○○                    <- power
○○ 우측난방 단계
○○ 좌측난방 단계
```

This is the approach Home Assistant prescribes for "the single main feature of a device".
Every model has exactly one power control per device — on a split mat, no amount of operating
the two sides moves the power value (confirmed on a real device).

### Button sound volume moved under configuration

It is set once and left alone, so there is no reason for it to sit among the main controls.
It now lives in the configuration section of the device page.

**The control lock stays where it is.** A household with children toggles it daily, so it must
not drop off the default Overview dashboard.

### Does this affect what you already use?

| | |
| --- | --- |
| entity ids | **unchanged** — automations and scripts keep working |
| the power entity's display name | `○○ 전원` becomes `○○` |
| **voice assistant phrasing** | **`○○ 전원 켜줘` becomes `○○ 켜줘`** |
| a name you set yourself | your name is kept |
| button sound volume | drops off the default Overview; it is in the configuration section of the device page |

### Left-then-right ordering is still not possible

In Korean `우` sorts before `좌`, so the order cannot be fixed through names (the same holds
for 왼쪽/오른쪽, where `오 < 왼`). To see them in the order you want, build a dashboard card
and list them explicitly.

## v0.15.0 — 2026-08-01

**Entity names now match the Navien app's own wording, and the server can no longer be called
too often.**

### The README explains why air quality is on five minutes

To answer "why is it not real-time like the app" (#1), the Navien app was taken apart again.

**The app is not push-based either. It polls in exactly the same way.**

```java
while (the screen is open) {
    RealtimeAirSensor(...);
    delay(60000);          // 60 seconds
}
```

| | Navien app | this integration |
| --- | --- | --- |
| interval | 1 minute | 5 minutes |
| when | only while the air-quality screen is open | around the clock |
| requests per day | 10 minutes on screen means 10 | 288 |

**The app reacts five times faster in the moment, and we make thirty times the daily
traffic.** Pulling it in to one minute would mean 1,440 requests a day, a load the app never
creates. So the default stays at five minutes, and the README explains how to pull it in with
an automation for anyone who needs to.

### Repeated requests now have a floor

`homeassistant.update_entity` can wake the integration at any time, and **HA's default floor
is 10 seconds**. A single line of automation would have one account hitting Navien's servers
more than 8,000 times a day. When an unofficial integration is blocked, it is not blocked for
that person alone but for everyone using it.

**Repeated requests are now coalesced to the polling interval** — five minutes with an Airone
present, fifteen minutes with mats alone.

| with a 10-second automation | up to v0.14.7 | v0.15.0 |
| --- | --- | --- |
| Airone present | 8,352 a day | **576 a day** |
| mats only | 8,352 a day | **192 a day** |
| a single manual refresh | immediate | **immediate** (unchanged) |

**What is throttled is repetition, not a single call.** Refreshing to see the current value
still works instantly, whenever you want.

Rather than picking a number, this was **tied to the polling interval**, which means nothing
outside can wake us faster than we poll ourselves.

**The polling interval itself is unchanged.**

### Entity names match the app

Corrected as reported
([#1](https://github.com/ripe-avocado/navien_smart_ha/issues/1)), checked one by one against
the app resources.

| until now | now | evidence |
| --- | --- | --- |
| 종합 공기질 | **통합공기질** | the app's air-quality screen title, verbatim |
| 목표 습도 | **희망습도** | the app's control screen title, verbatim |
| 운전 모드 | **운전모드** | the app's spelling |
| 운전 상태 | **운전상태** | the app uses no space here |

"종합 공기질" and "목표 습도" were **phrases the app never uses** — we invented them, and
side by side with the app they looked like different features.

**Automations do not break.** Only the display name changes; the entity ids are unchanged. A
name you set yourself is kept.

### Nothing else changed

Neither the polling interval nor the entity layout moved.

## v0.14.7 — 2026-08-01

**Found why ventilation/purifier air quality stalled: we were starving our
own refresh.**

Three reports (#1, #12, #13) all showed the same shape.

```
last_poll_seconds_ago   600 ~ 1113s   ← refresh interval is 300s
poll_failures           0
polling_disabled_in_ha  false
mqtt_connected          true
```

**It wasn't failing — it wasn't running at all.** Nothing in the logs
either.

### Every realtime message pushed the next refresh five minutes further out

When a device sends a state, we apply it to the UI. The function that did
that was **cancelling the scheduled next refresh and restarting the count
from zero.**

| | Message interval | Result |
| --- | --- | --- |
| ventilation/purifier | **46s** | the 300s timer **never fires** |
| sleep mat | 3.2 hours | fine |

**That's why only ventilation/purifier users hit this.** Mats are quiet
enough that the timer fired normally.

State application and refresh scheduling are now separate. **Air quality
refreshes correctly every 5 minutes.**

### The particulate-matter unit didn't match the spec

A reporter's log pointed it out.

```
using native unit '㎍/㎥' which is not a valid unit for device class 'pm1';
expected one of ['µg/m³']
```

We'd used the app's own notation, but Home Assistant requires a different
character. **As it stood, statistics for all three particulate sensors
couldn't be built.** Matched it to spec.

> ⚠️ Because the unit changes, you'll see one statistics "repair" notice for
> the three particulate sensors. Clear it under Settings → System → Repairs.

### Closed two holes that failed silently

- A server response that wasn't an object (`null`, an array) stalled the
  refresh with no further signal
- Same for an unexpected entry in the device list

Neither was classified as our error, so **neither showed up in the failure
count or the logs.**

### Made the next stall visible immediately

Two new fields in the diagnostics.

```
poll_attempts     how many refresh attempts were made  ← separates "never called" from "failed"
poll_last_error   what the last failure was
```

## v0.14.6 — 2026-08-01

**Reads the auto-dry progress.**

v0.14.5 noted that "the app also shows a progress value, but we couldn't find
where it lives." **We hadn't looked hard enough.** Thirty more lines of app
code turned it up.

```java
do { ... } while (additionalDataStatusPrevious.getType() != 4);
value = additionalDataStatus.getValue();
```

**The state string stays "Auto Dry"; the progress moved to an attribute.**
Mixing a number into the state, like "Auto Dry 47%", breaks any automation
that compares the string.

```
Operation status sensor
  state       Auto Dry
  attribute   auto_dry_percent: 47
```

The attribute is absent entirely outside auto-dry — the app only reads this
value under the same condition.

> **Confirmed on a real device.** `moKorean`, who sent the PR, reported that
> "the auto-dry progress displays correctly on a real device."

## v0.14.5 — 2026-08-01

### Fixed the "Auto Dry" operation status showing with no name

Turning off dehumidify makes the device dry itself out internally, and that
state was showing as **"Unknown (4)"**.

[PR #15](https://github.com/ripe-avocado/navien_smart_ha/pull/15) — `moKorean`
confirmed this on a real `NRT-530Z3` and sent it in. Cross-checked against the
app code too.

```java
if (... roomController.getRunning() != 4) { ... }
   → "자동건조 중 %02d%%"
```

Named it **"Auto Dry"**, matching the app's label. **The power-state
determination doesn't change** — auto-dry is cleanup after power-off, so it
still counts as "off".

> The app also shows a progress value (`47%`). It lives elsewhere and isn't
> read yet.

### Surfaced why polling stops, in the diagnostics

A report (#1) turned up this data.

```
last_poll_seconds_ago   69110.5   ← 19.2 hours
update_interval_seconds 300       ← should be every 5 minutes
poll_failures           0         ← not even counted as a failure
```

**It wasn't failing — it wasn't running at all.** Home Assistant has a setting
that causes exactly this.

> Settings → Devices & services → Navien Smart → ⋮ → System options
> → **"Enable polling for updates"**

Turning it off stops HA's periodic refresh entirely. But the realtime
connection is one we maintain ourselves, so it stays alive — which means
**operation mode and power stay current while only air quality stalls.**

**That setting is now in the diagnostics** (`polling_disabled_in_ha`). No more
guessing — one file answers it.

## v0.14.4 — 2026-07-31

**An unrecognized legacy model no longer gets the bypass mode.**

v0.14.3 had it backwards — it excluded only models *confirmed* to lack
bypass, so anything not on the list still showed it. Flipped that.

| Model | Bypass |
| --- | --- |
| `NRT-20DS` · `NRT-20DSW` · `NRT-21DS` | has it |
| `NRT-30` · `NTR-10PW` | has it |
| `NRT-20D` · `NRT-21D` | no |
| **everything else** | **no** ← changed from v0.14.3 |

**Showing a mode that isn't there is worse than hiding one that is.**
Pressing it stops the outdoor unit, and the user has to undo it through the
app. A missing mode that does exist is fixed with **one report.**

Legacy models come in only six families, small enough to manage as a list;
new models get their mode straight from the server, so they were never
affected.

**If you're on a model not in the list and your app shows bypass, let us
know** — it goes in right away.

## v0.14.3 — 2026-07-31

**Removed the bypass mode from models that don't have it.** Report (#13).

An `NRT-20D` user reported it — **the app has no bypass mode, but we were
showing one.** Pressing it stopped the outdoor unit and power dropped to 3W
(ventilation/low-fan draws 51W).

The app decides whether to offer this mode from a value the device reports,
and **that value lives somewhere we can't read.** So since v0.11.0 we'd been
adding it to every legacy model.

### Excluding only what's confirmed absent

| Model | Bypass |
| --- | --- |
| `NRT-20D` · `NRT-21D` | **no** — excluded as of this version |
| `NRT-20DS` · `NRT-20DSW` · `NRT-21DS` | has it |
| `NRT-30` · `NTR-10PW` | has it |
| everything else | **unchanged** |

The first attempt was "only show it for models with an `S` suffix," and we
**reverted that.** `NRT-30` and `NTR-10PW` have no `S` but do have bypass —
that approach would have **broken a working feature** for that family.

**Excluding broadly breaks working devices; excluding narrowly can always be
widened by one more report.** We picked the reversible option. If your model
isn't listed and has no bypass, let us know.

### Impact

**Only `NRT-20D` and `NRT-21D` drop from 6 modes to 5.** Every other legacy
model is unchanged, and new models get their mode from the server, so they
were never affected.

## v0.14.2 — 2026-07-31

**Gave TVOC the `ppb` unit.** Requested in a report (#13).

Checked every app resource string (APK 2.10.4). **The app only prints a unit
for four things.**

| Metric | Does the app print a unit? |
| --- | --- |
| PM10 · PM2.5 · PM1 | **yes** — `µg/m³` |
| CO2 | **yes** — `ppm` |
| TVOC · radon | **no** |
| overall air quality | it's a score, no unit applies |

TVOC and radon's info-screen titles end in "Volatile Organic Compounds TVOC"
and "Radon RADON" respectively. Neither `ppb` nor `Bq` appears anywhere in
the resources or code.

**We add it anyway.** A number with no unit can't be read meaningfully.
Diagnostics carries `inferred: true` alongside it, so **an externally
visible flag marks it as an inferred value, not a confirmed one.** Radon was
already handled this way — leaving TVOC without a unit was the inconsistent
choice.

### ⚠️ You'll see one statistics "repair" notice

Since the unit changes from **none → `ppb`**, Home Assistant flags the
existing history as mismatched. Clear it once under Settings → System →
Repairs.

## v0.14.1 — 2026-07-31

**Fixed the fan-speed entity disappearing entirely on single-speed modes.**

Report (#12): switching from ventilation/low to **sleep mode makes the
fan-speed entity vanish.**

> In the app it shows as "Sleep + Auto (disabled)".

Our rule read:

```python
return super().available and len(self._choices) > 1
```

Sleep mode has exactly **one** candidate, "auto". `1 > 1` is false, so the
entity dropped out.

**Having only one choice is not the same as having none.** The app shows the
value and just disables pressing it — it doesn't hide it. For the user,
hiding it means losing "what fan speed is it on right now," which is worse.
When there's only one value, show it.

### The blast radius was wider than expected

| Device generation | Modes that disappeared |
| --- | --- |
| new | sleep |
| legacy | **auto-run · cooking · sleep** |

Legacy auto-run and cooking were **widened in v0.13.1.** Correcting them back
to a single choice, per the app's own file, triggered this same rule and made
them vanish too.

### Confirmed by a user

The same reporter also confirmed that v0.13.2's **"Unknown" operation-mode
issue was resolved.** Their own trace through the integration code pinned
down the root cause both times.

## v0.14.0 — 2026-07-31

**State exists the moment a device is added.**

Until now, adding a mat left it **with no state until the device sent
something on its own.** Mats are quiet — one report's data showed 6 realtime
messages and 1 usable state over 19 hours. Add a winter mat in summer and a
day could pass with nothing.

Pressing the temperature control in that window hit the error that v0.13.3
had only reworded.

### The server was holding the last state

The AWS shadow stores the document a device last reported **on the server
side.** There's a way to request it, and **Navien's server honors the
request.**

**Confirmed on two real devices** (`EME-500` single and double zone).

| | REST connection | Shadow response |
| --- | --- | --- |
| single | **offline** | came back — level 1, connected false |
| double | online | came back — left off / right level 3 on |

**It answers even when the device is off**, because the server responds, not
the device.

### What changes

```
before: add mat → no state → pressing temperature control errors
        (waiting for the device to send something on its own)

after:  add mat → state arrives immediately → controllable right away
```

### Kept it on the safe side

- **It's a read.** It doesn't change device settings and leaves no trace in
  the shadow
- **Once per device**, only when it's added — not on every poll
- A failure just **leaves things at v0.13.x behavior**. Nothing is lost
- The accompanying "desired" value that comes with it is **not read** — it's
  not a value the device has confirmed

## v0.13.3 — 2026-07-31

**Tell the user what's actually wrong when a device has dropped off Wi-Fi.**

Report: a mat kept dropping off Wi-Fi, and turning it on from HA in that state
produced this.

```
climate/set_hvac_mode 동작을 수행하지 못했습니다. 보낼 구역 값이 없습니다.
```

**That's our own message ("no zone value to send"), and there's nothing the
user can do with it.**

### What was happening

1. The device list (server-side) is fine, so **the model gets recognized**
2. The device isn't sending state — **unplugged for summer, dropped off
   Wi-Fi, or the integration was just added**, all three look the same
3. But the server's `connected` field **tracks registration more than
   liveness**, so it stays "normal" for a while
4. So **the entity looks healthy**, and pressing it fails

The app sends **the current setting along with power-on.** We follow the same
shape, so if we've never received a state, we can't build that value.

The power switch (`switch`) only sends `operationMode`, so it doesn't hit
this. Only `climate` does.

### Reworded the message

```
before: 보낼 구역 값이 없습니다.

after:  기기가 아직 상태를 보내오지 않아 온도를 함께 실을 수 없습니다.
        「전원」 스위치로는 켤 수 있습니다 — 켜면 기기가 상태를 보내고
        그 뒤로 온도 조절도 됩니다. 그래도 안 되면 매트가 전원·Wi-Fi 에
        연결돼 있는지 확인해 주세요.
```

**The behavior itself is unchanged.** If the device is truly offline, no
command will get through either way. All this does is explain why.

## v0.13.2 — 2026-07-31

**We were erasing state we had just received ourselves. This is why the
ventilation-purifier entity kept falling into `unavailable`.**

A reporter read the code and put together the evidence ([#12]). The
**timestamps** in that record pinpointed the cause.

```
command_log   change-mode  at 2038928.0
              status       at 2038931.3

humidity_log  mode: 4      at 2038930.9   ← change-mode response
              mode: null   at 2038939.9   ← overwritten by the status response
```

The device answers a `status` request with the **entire DID document**. The
`roomController.mode` field inside it isn't the current operating mode (an
integer) but a **14-entry array of supported combinations**. We overwrote
blindly with it, and the array clobbered the mode number we'd just received.

### And that's why the device looked dead

```
mode = None
  → the fan-speed candidates for "the current mode" become an empty list
  → the fan-speed select decides "nothing to choose from" and goes unavailable
```

The device was fine the whole time. This is why diagnostics showed
`available: true`, `poll_failures: 0`, and `mqtt_connected: true` all at once
while only the entity was missing.

### One more instance of the same trap

`additionalData` also has a different shape depending on whether it arrives
as state or as capability.

| where it comes from | shape |
| --- | --- |
| state | `{"type": 3, "value": 40}` — a value |
| DID | `{"type": 1, "min": 0, "max": 4}` — a range table |

When the range table overwrites it, **the target humidity we'd stored
disappears.** A list with no values at all isn't state, so it's now discarded.

**A real state value still overwrites as normal.** The integer mode switches
correctly, and `additionalData` that does carry a value is applied.

## v0.13.1 — 2026-07-31

**Older models were being offered fan speeds that auto-run and cooking don't have.**

A reporter's older-model (`NRT20DS`) screen showed **auto-run with four fan
speeds — low, medium, high, auto** — when it should have been auto alone.

The mode list for older models comes from a file bundled in the app. That
file **records whether fan speed can be chosen in each mode, and that got
lost in translation on our side.** We guessed instead that "the default
combination means it can be chosen," and that guess was wrong for two modes.

| mode | app file | through v0.13.0 | v0.13.1 |
| --- | --- | --- | --- |
| auto-run | fixed | low/medium/high/auto | **auto** |
| cooking | fixed | low/medium/high/auto | **high** |
| ventilation/purification | selectable | six | six (unchanged) |
| sleep | fixed | auto | auto (unchanged) |

**We now follow the file even when the server says otherwise**, because on
older models the app never even looks at the server's list. A combination
missing from the file still falls back to whatever the server says.

**Nothing changes on newer models.**

### Also blocked while we were in there

Every entry in the file carries `{type:1, min:0, max:4}`, which means **a
fan-speed range**. On newer models the same numbers mean a humidity range,
and passing it through unchanged would have produced a "target humidity
0-4%." We stopped it from being applied and added a check for it.

## v0.13.0 — 2026-07-31

**Control lock can now be switched on and off. v0.12.0's conclusion was
wrong.**

v0.12.0 stated that "the app has no way to lock over Wi-Fi either" and shipped
it as a read-only sensor. A reporter sent **a screenshot of a lock button on
the app's control screen**, and digging again turned it up.

```java
String str = !mateInfoData1.getLockState() ? "lock-on" : "lock-off";
r11 = Boolean.valueOf(areEqual(r35, "lock-on"));   // childLock
r4  = new Desired(r11, new Event(modelCode), null × 12);
```

Same shape as the seasonal switch, and the topic is one we already use.

### Why we missed it

Two searches missed it in the same direction.

- We only scanned calls that pass the command name **as a literal string**.
  Lock passes `lock-on`/`lock-off` **through a variable**
- We searched for `new Desired(`, but the actual code uses the **fully
  qualified name** (`new kr.co.kdnavien...Desired(`), so it didn't match

**Never conclude "it doesn't exist" from one search alone.** A button visible
on the app screen is evidence; failing to find it is our problem.

### The entity changes

```
binary_sensor.<device>_조작_잠금   →   switch.<device>_조작_잠금
```

If you were on v0.12.0 or v0.12.1, **the old `binary_sensor` is left behind
in a "restored" state.** Remove it from Settings → Devices & services →
Navien Smart.

### Confirmed

A reporter confirmed that **changing the volume worked correctly** on
v0.12.0. Reading the lock state also came back correct.

## v0.12.1 — 2026-07-31

**Stopped asking devices with no sensor for air quality.**

Both Airone reports were on an `NRT-530Z3`. Not a coincidence.

### Having air quality means hitting the server 6x harder

| | mat only | one Airone |
| --- | --- | --- |
| poll interval | 15 min | **5 min** (air quality only arrives over REST) |
| requests per cycle | 1 | **2** (device list + air quality) |
| **per hour** | **4** | **24** |

This is why only Airone users ran into the problem. It wasn't that things
were broken — **it was being hit harder and taking more damage.**

### But some devices don't need that request at all

We had failed to wire up something the spec already told us.

```
NRT-530S3 (no monitor)  sensor table on roomController.sensor    ← built into the room controller
NRT-530Z3 (has monitor) roomController.sensor is an empty array
                         sensor table on airMonitor[].sensor     ← a separate device
```

**There's also a case where neither has one** — an energy-recovery
ventilator without an air monitor purchased. Asking that device every five
minutes only gets an empty response back. And before v0.12.0, if that call
ran slow, **the entire poll would die and every entity went
`unavailable`.**

Now, once a device declares that "nobody is carrying a sensor," we stop
asking. That device's requests drop **from 24 per hour to 12**, and there's
half as much surface for the poll to fail on.

**We only stop asking once we're sure there's none.**

- If an air monitor is attached, we ask — even if its table is empty
- If the room controller offers a sensor table, we ask
- **If the table itself never arrived, we ask** — that's "unknown," not
  "none"
- If an air monitor gets added later, it shows up in the device list and
  gets asked again automatically

A skip is logged once. If the judgment turns out wrong, that log line comes
back as a report. Diagnostics also carries `wants_air_sensors` and
`declared_sensor_count`.

## v0.12.0 — 2026-07-31

**Handled three reports at once. Two were bugs, one was a new feature.**

### Ventilation-purifier fan speed only showed 3 options — the app has 6 (#9)

A reporter sent a screenshot of the app's "fan speed" screen. **Six of
them: auto, eco, low, medium, high, turbo.** We were only creating three.

We trusted only the `supportedAirVolumes` list field, but **that field
doesn't exist at all in app 2.10.4.** The server added it later, and
devices on old firmware still don't send it. What the app actually looks
at is `configurable`.

Widening it as-is would also revive the list a device had narrowed down on
the server side, so we blocked it in two layers — a device that gives a
list gets only that list; a device that enumerates the same combination as
multiple entries gets only that enumeration; a device with no fan-speed
value at all (an energy-recovery ventilator) still shows only turbo/eco.

### Air quality hadn't changed in ten hours (#1)

The report's data didn't add up.

```
seconds since last value changed   35395   ← unchanged for 9.8 hours
empty responses 0 · query failures 0 · reads with no change 0
```

If it had genuinely stayed unchanged for 9.8 hours, that last line should
have climbed on every read. At 0, **the read never finished at all**, yet
the failure count was also 0. There was one path out that left no record
behind.

```python
except aiohttp.ClientError    # ← doesn't catch a timeout
```

`TimeoutError` is in the `OSError` family, so it slips past this line.
**The intervals also happened to line up** — the default timeout is 5
minutes, and the ventilation-purifier poll interval is also 5 minutes. One
late call pushes the entire next cycle back.

MQTT stayed alive the whole time, so power and mode looked fine. That's why
only air quality looked stuck.

- Timeouts are now caught and counted as a failure, logged after 3 in a row
- Set the request timeout explicitly to **30 seconds**
- Diagnostics now carries **the poll's own timing** — `last_poll_seconds_ago`
  and `poll_failures`

That last line is the key. When failures, empty responses, and no-change
reads are all 0 yet the value is stale, that one field will separate the
two cases next time.

### Mats gained key-tone volume and control lock

A reporter's `EMF520` state showed both `volume` and `childLock`. **They're
different in nature.**

| | |
| --- | --- |
| key-tone volume | **changeable.** Mute · 1 · 2 · 3 (`select`) |
| control lock | **display only** (`binary_sensor`) |

The app's volume screen has four slots and the value is sent to the command
as-is. This isn't a guessed range.

Lock is different. **The app has no path to lock over Wi-Fi** — the
lock/unlock constants exist, but nothing in the whole app calls them, and
the field doesn't even have a setter. Control is Bluetooth-only, and only
on some models. As a switch, pressing it would do nothing, so it's shown as
state only.

A feature the device doesn't have is still not created — volume only
appears when `functions.beep` is present, lock only when
`functions.lockMode` is.

### Confirmed that cooling controls left and right independently

An `EMF520` report showed cooling mode holding **left at 25.0 and right at
25.5**, different from each other. This confirms, on a real device, the
judgment behind removing "tying left and right together" in v0.11.1. The
README's "depends on the model" wording was corrected to match the fact.

## v0.11.1 — 2026-07-31

**A four-season mat report led to three fixes. Two of them were bugs we
introduced.**

An `EMF520` user confirmed v0.10.0's season switching on a real device —
**heating↔cooling switching works.** The temperature range shifting by
season is also working as intended.

### Cooling showed the running state as "heating"

`operationMode` **carries the same value whether heating or cooling, as
long as it's running.** What it's actually doing is decided by `season`,
but we used the table as-is, so cooling also showed "heating."

While fixing it we found one more — **a mat with no cooling feature at all
was still being read as cooling** whenever it sent a `season` value.
`is_cooling` wasn't checking `coolControl`.

### Removed the code that tied left and right together in cooling — it had
### baked model-specific behavior into the code

Since v0.9.0, changing one side while cooling with independent left/right
sent **the same value to both sides.** The basis for that was the app's
help text.

```
COOL mode — the mat's left and right run at the same temperature
```

**That wording left out which model it applied to.** Navien's product page
says:

```
0.5°C independent heating/cooling technology lets you set left and right
to the temperature you want — this feature only applies to the four-season
Pro model
```

The Pro model does control left and right independently even in cooling.
**The server doesn't tell us Pro versus Air.** Tying them together amounted
to baking model-specific behavior into the code, which is exactly what this
integration decided not to do.

A SmartThings screenshot the reporter posted showed cooling state with
**left at 25.5 and right at 24.5.** That was the answer, and we didn't
recognize it at the time.

Now **we only send the zone you pressed.** On a model that runs tied
together, the device itself returns both values equal and we just show
that. **We don't get ahead of the device and decide what it does.**

### Error code names

Added 15 codes a reporter transcribed from the device manual.

**The state value stays a raw number; the name only shows up as the
`error_text` attribute** — turning it into text would break any automation
built on the numeric value. And **it's only attached to the temperature-mat
type.** The presence of water tank, circulation pump, UV lamp and leak
codes marks this as a hot-water/four-season-line manual, and there's no
basis for assuming the same numbers mean the same thing on a carbon
(stepped) mat.

### The same mistake, for the third time today

- Diagnostics recording — confirmed "it accumulates" **from the code
  alone** (v0.9.5)
- Mode list — concluded where it came from **from a single function**
  (v0.11.0)
- Tying left/right together — concluded device behavior **from a single
  line of text**

All three **reached a conclusion from a single piece of evidence.**

## v0.11.0 — 2026-07-30

**Adds support for older-generation ventilation-purifiers (`modelCode`
below 1000).** These devices had produced zero entities up to now.

### Opened up by a contribution

A user with a real `NRT-20DSW` confirmed and sent this in
([#7](https://github.com/ripe-avocado/navien_smart_ha/pull/7)). **This was
a section we had no way to open without an older-generation device on
hand.**

The design is good — the generation difference is confined to **exactly two
places, where state arrives and where commands are sent**, and the
model/entity layer only knows one, current-generation vocabulary. The
verified current-generation path isn't disturbed.

| | current generation | older generation |
| --- | --- | --- |
| state path | `did.reported` | `did.state.reported` |
| control topic | `cmd/rc/v2/…` | `cmd/rc/…` (no `v2`) |
| command envelope | `state.desired` | `request` |
| power | running = 1 | **running = 2** (reversed) |

The reversed power value was separately confirmed in the app's source too —
in `powercontrol()`, the `modelCode < 1000` branch sends 1 when turning on
and 2 when turning off.

### Mode list — I got this wrong twice

**At first I read the DID as a capability list.** A contributor sent in a
list containing modes absent from the DID, and I had it narrowed down by
citing other report data as a counterexample.

I then saw a single app function (`makeModeList()`) build the list from the
DID and **concluded "the narrowed version matches the app." That was also
wrong** — I never traced who fills in the variable that function reads.

Rechecking after **both users'** app screens turned out to show six modes
each, the answer came out: on older-generation devices the app **doesn't
look at the DID at all.**

```java
if (modelCode < 1000) { loadlegacyModeDataFromFile(); ... }
// assets/jsons/airone_legacy_mode_did.json
```

We now use that file's contents directly. **It's one table per generation,
not per model**, and the app uses the same one for every older-generation
device.

```
자동운전 · 환기 · 청정 · 요리 · 숙면 · 바이패스
풍량: 미풍 · 약풍 · 강풍 · 자동 · 터보 · 절전
```

### Bypass is the one exception — a flag we can't read

Reading the branch all the way through, the app does filter by capability
too — **just not through `mode[]`.**

```java
if (did.supportByPass == 2)  modeDidList.add(ModeDid(17, 1, …))   // bypass
```

`supportByPass` lives in **the response to a DID request made separately
over MQTT**, and it's absent from the device list. Neither of the two
devices we checked had it. **So this condition can't be evaluated.**

We include bypass anyway — it's present on both real devices' app screens,
and a contributor confirmed command 17 is accepted by the device. Leaving
it out would remove a feature both devices actually have.

Two items in the same spot — basal ventilation (`basalairUse`) and
cooking-mode auto fan speed (`kitchenMode.autoUse`) — were **left out.**
Neither was confirmed on either device.

**Doing this correctly would mean requesting the DID over MQTT on
older-generation devices too.** Noted as remaining work.

### The device accepts modes absent from the DID

A contributor's real measurement: sending 12, 6, or 17, none of which are
in `mode[]`, the device applies them anyway. **"What the device accepts"
and "what the app shows" are different, and the app is the narrower one.**

**Lesson:** we concluded where something came from by looking at a single
function, without tracing **who fills in** the value that function reads.
This is the same mistake for the second time — the first was diagnostics
being erased on every poll (v0.9.5), where we also confirmed "it
accumulates" from the code alone.

### Also

- **State restored after a restart** — older-generation devices don't
  answer a state request. We restore the last value received, and
  diagnostics marks it with `state_restored` **to flag it as a restored
  value.** The flag clears once the device reports on its own
- **Older-generation-only diagnostic sensors** — the configured value and
  the operating value arrive separately under names that invite confusion
  (`supportedOperationMode` isn't a capability list, it's **the configured
  mode**). Only named the ones whose meaning was confirmed
- **Filter** — on older-generation devices this arrives as usage time, not
  a usage percentage. **We leave it as a bare number since the unit is
  unknown**

### Still not done

**This opened up a path we can't verify ourselves.** We don't have an
older-generation device. It means there will be a blind spot the next time
the Airone side gets fixed.

## v0.10.0 — 2026-07-30

**Heating↔cooling switching now works from HA.** Removed the note that
said "switch it from the device."

### Solved by a single community comment

A report came in that a SmartThings custom component displays the season
mode as `coolPlus`.

**`coolPlus` isn't a Navien value** — a full-text search of the app's
strings turns up zero matches. It's a SmartThings-side label, and the
spec sheet's `Cool+` is a model name too.

**What mattered was that the report sent us digging again.** The app does
have a path that uses season.

| what we confirmed | |
| --- | --- |
| values | **only 0 (heating) / 2 (cooling)** — the app's season screen has
two buttons |
| envelope | `Desired.season` (integer) — **the same envelope we already
use** |
| topic | the shadow update — **the same topic we already use** |
| `event.modelCode` | `async_control` already attaches this to every
command |

**Nothing new had to be built.** It was just one more field on top of an
existing path. The reason we hadn't opened this before was "we don't know
where to send it," and that was the only thing missing.

### Modeled as a "season" list, not a climate mode

We didn't build this as heating/cooling buttons on `climate`. **Season
isn't what the device is doing right now, it's which side the device is
configured for.** The app puts it on the device settings screen rather
than the control screen too, and switching it shifts the whole temperature
range (heating 28-45, cooling 20-35).

Putting it on the `climate` mode buttons would overlap "currently heating"
and "configured for heating" in the same spot.

**We only send values we recognize.** Anything other than 0 or 2 is
rejected — there's no slot for something like `coolPlus` to land in. If
the server sends a value we don't know, the list state is left empty.

### Not yet confirmed

**We have no four-season mat, so we couldn't press it ourselves.** Sending
the same value over the same path the app uses is not the same thing as
the device actually responding to it — we got that wrong twice today
already.

Two `EMF520` users had requested cooling control. We're closing this out on
their confirmation.

**The cooling step range for the stepped four-season models is still
unknown.** The step list stays untouched in cooling.

## v0.9.5 — 2026-07-30

**Diagnostics records were being erased on every poll. This meant the
air-quality-detection warning added in v0.9.3 could never fire.**

### Left out of the carry-over list

Device objects are rebuilt fresh on every poll. That rebuild lists what
should carry over, and the records added in v0.9.0-v0.9.3 were never added
to that list.

```python
device.reported = old.reported          # only these carried over
device.air_sensors = old.air_sensors
device.sensor_kinds = old.sensor_kinds
device.last_humidity = old.last_humidity
# command_log · state_log · air_sensor_errors · air_sensor_unchanged … absent
```

| what we built | what it actually did |
| --- | --- |
| `WARNING` after 3 consecutive air-quality failures | **could never fire**
— the counter reset to 0 and never reached 3 |
| `air_sensor_unchanged_reads` | always 0 |
| mat `command_log` / `state_log` | erased on every poll |
| Airone `command_log` / `humidity_log` | erased on every poll |

**The only reason this had seemed to work at all is that reporters operated
the device and got a response within a few minutes** — inside a single poll
window. Pure luck.

### Why we didn't catch it

**We confirmed "the record persists" from the code alone, never along the
time axis.** The tests only checked that pushing several values into one
object made them accumulate. A poll replacing the whole object was a spot
those tests couldn't see.

Carrying values over is now covered by a check.

### What this will help tell apart

A report: "the HA value hasn't changed in over 15 minutes" — the history
sat flat for three and a half hours. There are two possible causes and we
still can't tell them apart.

| | how it will show up now |
| --- | --- |
| the server keeps returning the same value | `air_sensor_unchanged_reads`
climbs into the dozens |
| queries aren't running on schedule | that value stays 0 while only
`air_sensor_changed_seconds_ago` grows |

## v0.9.4 — 2026-07-30

**Device IDs were leaking through diagnostics. What's embedded inside a
value wasn't being redacted.**

### An identifier embedded inside a value just went out as-is

An issue report's attached file had this line, unredacted.

```
"requestTopic": "dt/rc/7/1097BD3F5CACB84E/did"
```

`requestTopic` wasn't in the list of keys to redact, and **the device ID is
embedded inside its value.** The reporter posted this line on a public
issue.

**This is the second incident of the same kind.** The first was missing
redaction on a table we built ourselves (`air_monitors`). That time we only
added redaction for that one table. **Stopping there was the mistake** —
the approach itself was "redact if the key name is on the list," and an
identifier embedded inside a value was never in scope to begin with.

### Didn't stop at adding one more key

Adding `requestTopic` to the list only blocks this one case. The next leak
happens the same way.

**Instead, identifier values are now collected first, and that exact
string is stripped from the entire result** — no matter what the key is
named or how deep it sits.

```
"requestTopic": "dt/rc/7/**REDACTED**/did"
```

**The topic's shape survives.** What's needed to widen support is the
shape, not the identifier.

Anything under 8 characters isn't collected — a short value could
coincidentally appear inside some other string, and stripping it would
corrupt a perfectly good value. Longer values are stripped first.

### The README listed models as supported when they aren't

The ventilation-purifier model list had been **the list from the app's
"add device" screen.** That's what the app supports, not what we support.
"Newer models only" was noted below it, but seeing `NRT-20D series` in the
table reads as "this works."

The same report confirmed `NRT-20DS` is an older-generation model
(`modelCode 7`). The table now separates entries into **confirmed / does
not work / not yet known.**

"Not yet known" doesn't mean it doesn't work. Since we don't hard-code a
per-model table, a newer model is likely to work — that note was added
alongside it.

Also made explicit that the `TAC` line (commercial SCADA) is out of scope.

## v0.9.3 — 2026-07-30

**We were logging a warning about a situation that had no problem at all.
And air quality could stop updating with no trace of it.**

### "Not verified on a real device" was being logged as a `WARNING`

A reporter **took this line as an error and attached it to an issue.**

```
환기청정을 찾았습니다 (modelCode=1900). 지원을 켰지만 실기기로 검증하지
않았습니다 — 동작하지 않거나 값이 이상할 수 있습니다.
```

Two things were wrong with it.

1. **It wasn't even true anymore.** State, control, and target humidity
   had already been confirmed by reports
2. **`WARNING` should only be used when something is actually wrong.** The
   HA log screen shows warnings and above by default, so this raised
   concern over nothing

Dropped it to `INFO` and reworded it — it now only states what was found,
and asks to be told if something looks off. **We don't hard-code a list of
verified models in the code** — models keep getting added.

### Air quality could stop with no trace left behind

A report: "the values are different" — HA showed CO2 527ppm / humidity
49%, the app showed 479ppm / 52%. Temperature, the overall air-quality
index, and particulates matched exactly, so **the path the values travel
through is correct.**

Most likely a difference in measurement timing, but **there was no way to
tell.**

v0.8.2 had fixed this by "an empty response no longer erases a value
already received." The cost of that fix was that **a stale value stays on
screen indefinitely if updates stop.** Both query failures and empty
responses were logged at `DEBUG`. **This left exactly the same gap that
v0.8.3 had pointed out, in this spot.**

| diagnostic field | what it shows |
| --- | --- |
| `air_sensor_changed_seconds_ago` | seconds since the value **last
changed** |
| `air_sensor_read_errors` | number of failed queries |
| `air_sensor_empty_responses` | number of empty responses received |
| `air_sensor_unchanged_reads` | number of reads that came back the same as
before |

A query failing **3 times in a row (roughly 15 minutes) now raises a
`WARNING`** — stating explicitly that the value on screen is from an
earlier read.

### Docs

- Noted that both ventilation-purifier form factors are confirmed
  (separate `NRT-530Z3`, all-in-one `NRT-530S3`)
- **The all-in-one room controller reports air quality even without an air
  monitor** — the sensor is built into the room controller
- Fixed a code comment claiming "Airone has never been sent a command on a
  real device." **We now distinguish what was pressed ourselves from what
  we only heard through a report**

## v0.9.2 — 2026-07-30

**Target dehumidification humidity is now visible. The cause was looking
in the wrong place for it.**

### The device reports it back as `type 3`

The diagnostics added in v0.9.1 **gave us the answer on the very first
report.**

```
"reported_additional_data": [
  {"type": 1, "value": 1},
  {"type": 3, "value": 60}     ← target humidity
]
```

The server's capability info gives the range as `{"type": 1, "min": 40,
"max": 65}`. So **we assumed the reported value would use the same
number.** It didn't. State reports the value as `type: 3`. `type: 1` in
the same list is a different item with a range of 0-4, and searching only
that one meant we never found it, so the display stayed empty forever.

**We stopped hard-coding the number as a condition.** Only one device has
been observed. The rule now is **a value that falls inside the range the
server reported**, and only when there are multiple candidates does the
confirmed number (`3`) get tried first.

### The command we send had been working all along

A reporter confirmed via the app — setting 55 from HA also showed 55 in
the app, setting 60 showed 60.

**The device had never actually lost the value.** "It resets to 40% when
returning to dehumidify mode" was actually **our display sitting empty and
its slider handle resting at the far left.** 40 was never actually
reported.

v0.8.3 and v0.9.0 had both tried to fix this on the assumption that "the
device reverts it," and both were wrong. **We built a fix on top of an
unverified assumption.** That's why v0.9.0's reapply logic ended up
overwriting the user's own input too (removed in v0.9.1).

### Docs

Split the ventilation-purifier notes by room-controller form factor —
separate units work, the all-in-one is still being investigated for a
state-value issue. Also noted that target humidity shows as "auto" in
turbo and eco.

## v0.9.1 — 2026-07-30

**v0.9.0 was reverting the user's own actions. Removed.**

### Changing fan speed snapped back after 8 seconds — a bug I introduced in
### v0.9.0

A reporter sent their v0.9.0 diagnostics. **Lining up the commands sent
and the states observed in time order made it obvious immediately.**

```
610528.4  we sent    dehumidify · default fan speed   humidity 50
610530.4  we sent    dehumidify · turbo                ← the user selected turbo
610531.9  we sent    dehumidify · default fan speed    ← reapply overwrote turbo
610533.2  device replied   dehumidify · turbo
610534.8  device replied   dehumidify · default fan speed  ← dragged back
```

The comparison only checked `mode` and left out `option`. Changing only
the fan speed within the same dehumidify mode wasn't seen as "moved to a
different mode," so it got overwritten.

### Removed the reapply mechanism entirely

We didn't stop at fixing the guard condition. **The whole mechanism could
never have worked.**

All eight of the `humidity_log` observations **came back empty.** The
device never reports target humidity back as state. Without that, there's
no way to judge whether a value "got reverted," so the resend would be
treated as having failed forever, firing once more every time the mode
changed.

**We stop firing a command with no evidence to justify it.** Humidity is
now sent exactly once, riding along with the mode change.

### Stopped inventing a humidity range for turbo and eco

The server only gives a range (40-65) for dehumidify with default fan
speed. v0.9.0 had assumed "the range is a property of the mode" and
carried it over to other fan-speed combinations in the same mode. **That
was a guess, and it contradicted the app** — the app's resources contain
`humidityAutoText` and `humiditySeekbarNone`. In turbo and eco, the app
hides the slider and shows "auto" instead.

We now only accept it when the exact combination matches. Inventing a
range that doesn't exist makes an uncontrollable value look adjustable,
and that value then rides along in the command.

### A place to look for where target humidity actually lives

We now know the device doesn't report humidity back, but **we don't know
where it puts it.** Just logging key names won't get us any further.

Diagnostics now carries **only numbers and booleans.** Stripping out
entire strings is the redaction mechanism here — device IDs, SSIDs,
nicknames, and MAC addresses are all strings, so none of them slip
through. **A method that lists keys to redact leaks the moment a new key
appears**; this method doesn't leak even when a new string-valued key
shows up.

### The whole integration crashed if the nickname arrived as a plain string

We had assumed `nickName` always arrives shaped as `{"mainItem": ...}`. On
an account where it arrives as a plain string, parsing throws and **not a
single device shows up.** Diagnostics redacts this value, so its actual
shape wasn't visible — there was never a basis for that assumption.

No report of this actually happening has come in. It's a cheap enough
defense to add regardless.

## v0.9.0 — 2026-07-30

**Four-season mat cooling can now be controlled, along with fixes for a
bug that erased mat values and a bug that reverted the dehumidify target
humidity.**

### Four-season mat cooling control

Until now, cooling was **read-only.** The temperature showed correctly,
but it couldn't be adjusted. It had been blocked because the cooling value
scheme hadn't been confirmed.

We confirmed that the `coolControl` field the server sends matches
Navien's published spec sheet exactly — 0.5-degree steps, per-model
ranges, safety limits. **We don't hard-code the spec sheet.** We use
whatever range the device itself reports.

| | heating | cooling |
| --- | --- | --- |
| temperature control | works | **works** (new) |
| slider range | `heatControl` | `coolControl` |
| left/right independence | independent | **same value** |

**Left and right ending up at the same value in cooling is device
behavior**, stated as such in the app's help text. Changing one side moves
both.

Not yet opened up:

- **Heating ↔ cooling switching** — we don't know how to write the season
  value yet. Switch it on the device or in the app
- **Cooling on stepped four-season models** — only the temperature-based
  models have been confirmed. Step selection stays blocked
- **Cooling high-temperature warning** — for heating, "too hot" is the
  dangerous direction, but cooling is the opposite. We didn't add a safety
  limit since we don't know which direction applies

### Mat values vanished into "unknown" — a bug on our side

An `EMF520` user reported: the left/right controllers went "one side
showing only off, the other alternating between off and the current
temperature, then both ended up off."

**The cause was the same kind of bug already fixed in v0.8.1** — but that
fix only covered ventilation-purifiers. We had left mats alone, believing
"state always arrives as a complete set."

**That belief rested on nothing but my own two mats.** Four-season models
only send the one zone that changed. Replacing the whole object wholesale
erased power, season, and the other zone along with it.

Mats now **overlay only what arrived**, the same as everything else,
regardless of depth — so even a response carrying just one temperature no
longer loses the step level or on/off state.

### Dehumidify target humidity kept reverting to 40% — v0.8.3's fix was
### wrong

v0.8.3 had fixed this by **sending humidity along with the mode-change
message.** A reporter confirmed **the symptom was unchanged.**

The device doesn't accept that humidity value there. It appears to need
to be sent **after** entering the mode, not alongside the switch into it.

We now re-read the state after changing mode, and send it once more
**only if the value has actually reverted** at that point.

- If the device already accepted the first value, it matches and nothing
  is resent
- If the user deliberately left it at that value, it's also left alone
- If the mode changed again in the meantime, someone else's action isn't
  overwritten
- **No repeated resending.** At most one corrective send

**If this still doesn't work, we won't guess again next time.** The
records below are there to pin down exactly what the device is ignoring.

### Turned the three remaining open cases into "reports," not "guesses"

Instead of guessing a value and hard-coding it, we built **a place to
collect evidence.** Diagnostics alone hides the cause.

| diagnostic field | what it shows |
| --- | --- |
| `command_log` | **the value sent.** Whether sending 26.0 gets 26.0 back
from the device |
| `state_log` | **the order of observed values.** What the season value
actually changes to |
| `humidity_log` | the order humidity was sent in, and what the device
reported back |
| `mqtt_messages` | **counts received versus discarded.** Separates "it
never arrived" from "it arrived but couldn't be used" |
| `airone_last_unknown_shape_keys` | what shape a discarded message had |

Records are capped at 8 lines. **Values are limited to temperatures,
steps, counts, and key names** — no identifiers are stored.

## v0.8.3 — 2026-07-30

**Fixed a bug where the target dehumidification humidity kept resetting,
and made the integration surface, on its own, why state isn't arriving.**

### Target humidity kept reverting to 40% — a bug on our side

Report: 「제습모드에서 목표습도를 설정해놓고 다른모드로 변경하였다가 다시 제습모드로
전환하면 세팅한게 다시 초기화(40%)로 바뀌어있습니다」

```python
target = humidity                       # None (only changing mode)
bounds = self.humidity_bounds(mode, option)   # dehumidify → (40, 65)
if target is None and bounds is not None:
    current = self.target_humidity      # ← the problem is here
```

`target_humidity` looks up its range based on **"the current mode."** At
the exact moment of switching from ventilation into dehumidify, the mode
is still ventilation, so this is **always `None`.** So humidity never got
included in the command at all, and the device fell back to its own
minimum.

We now look at the range for **the mode being entered**, and send along
**the last humidity we remembered.**

A remembered value outside that range is **discarded rather than
clamped** — we don't manufacture a value the user never actually chose. A
value the user set directly is clamped instead (the intent there is
unambiguous).

### We were silently swallowing the reason state wasn't arriving

A report came in from an `NRT-530S3` (all-in-one room controller) user:
operating mode, power, and fan speed stayed empty indefinitely. The same
line's `NRT-530Z3` (separate room controller) works fine.

**We had no way to pin down the cause — every discard path logged at
`DEBUG`.** A default install only shows `WARNING` and above, so nothing
was left behind at all.

What was fixed:

- **We now also receive `idu` (indoor unit) state.** On the all-in-one room
  controller, the room controller and indoor unit are one physical unit,
  so state may be reported under this field instead — it does exist in the
  `did`. **We don't interpret the value**, just capture it into
  diagnostics
- **A state message of unrecognized shape is now logged at `WARNING`.**
  Only the top-level key names are kept; values are not
- **`WARNING` if no state arrives within 45 seconds.** Logs the topic sent
  and the topic being listened on together. **A silent failure is the
  hardest kind to catch** — when the request itself succeeds (HTTP 200)
  but no response follows, the user assumes the integration itself is
  broken

### What was added to diagnostics

Rather than values, this carries only **relationships and presence/absence.**

| field | what it shows |
| --- | --- |
| `reported_received` | whether state has arrived even once |
| `reported_keys` | which groups arrived (`roomController` / `odu` /
`airMonitor` / `idu`) |
| `reported_room_controller_keys` | whether it arrived but only `running`
and `mode` were missing |
| `physical_id_same_as_device_id` | whether the all-in-one and separate
units differ in ID structure — **redaction means the two IDs can't be
compared by eye** |
| `last_humidity_remembered` | whether a remembered humidity value is
still alive |

**Lesson:** we had kept to "don't build on a guess," but **we hadn't been
telling anyone what we failed to use.** Every discard path needs to leave a
trace, or a report can't lead anywhere.

307 synthetic test cases pass. 89 of them were built from real reported
responses.

## v0.8.2 — 2026-07-30

**Fixed a bug where the air-quality sensors fell into "unknown" every five
minutes.** The same mistake as v0.8.1, repeated in a different spot.

### Cause

Air quality doesn't arrive over MQTT — it's **re-read from `/air-sensor`
every five minutes.** `set_air_sensors` was **replacing the whole thing
wholesale** with that response.

If the response arrives empty once, or with only some items, everything
else vanishes at that point. If the air monitor drops out briefly, or the
server skips a beat, **the sensors fall into "unknown" on a five-minute
cycle.**

We now **overlay it.** An empty response erases nothing, and a partial one
only updates what it contains.

### The same mistake, twice

| location | fixed in |
| --- | --- |
| MQTT state response (`apply_reported`) | v0.8.1 |
| air-quality REST response (`set_air_sensors`) | **v0.8.2** |

Both were "replace with whatever the server gave us," and both never
considered **the case where the server gives only part of it.** Mats never
hit this trap because the shadow always sends the whole thing, and that
habit carried straight over onto Airone, which is what caused this.

**When there's no guarantee that what's arriving is the complete set, we
don't replace wholesale.** Confirmed these are the only two remaining
update paths on the Airone side.

### Confirmed by reports

A reporter confirmed that **an air monitor now gets added correctly**
with `v0.8.0`. The `/air-sensor` path fix
(`data.sensorList[].airs[]`) actually worked.

A report saying "it loads fine, then the state value goes missing, then
switching modes makes it work again" matches v0.8.1's symptom description
exactly — it was arriving, just not staying.

290 synthetic test cases pass. 77 of them were built from real reported
responses.

## v0.8.1 — 2026-07-30

**Fixed a bug where entities fell into "unknown" whenever power was
operated.** Caught through a community report.

### Cause — a partial response was overwriting the whole thing

```python
device.reported = reported     # ← replaces the whole thing
```

On Airone, **each command gets its own separate response, and that
response is a partial payload.** Turning power off might send only what
changed, like `{"roomController": {"running": 2}}`, or sometimes just
`odu` with no `roomController` at all.

Replacing wholesale erased `mode`, `option`, and `airVolume` at that
point, and when only `odu` arrived, even `running` disappeared — **making
the power switch go "unknown."**

We now **overlay it.** Only the items that arrived get replaced; the rest
stay as they were. Lists (`airMonitor`, `filter`) are still replaced
wholesale — merging a partial list item by item would misalign positions.

We accept the risk of a stale value lingering. If the device stops sending
some item, its last value stays. **Better than everything going
"unknown."**

**Mats were left untouched.** The shadow always sends the complete set, so
this handling isn't needed there, and preserving an already-verified path
takes priority.

### Also confirmed from the same report

**A filter usage rate showing "unknown" in some spots is normal.** The
reporter's device has an outdoor unit declaring 4 filters, but **only 2 of
them report a usage rate.** Filling the missing ones in as 0% would be a
lie.

That device's firmware is indoor unit `10.1` / outdoor unit `16.00`, older
than an earlier report's (`13.0` / `17.00`). **Even the same model sends
different items depending on firmware.** The design of using only what the
server actually sends turned out to be the right call.

### README — split confirmed from unconfirmed

Ventilation-purifiers had simply been marked "works (unverified)." Reports
have now confirmed **that state display works** (running state, operating
mode, fan speed, filter, air quality), while **whether an action taken
from HA actually reaches the device is still unconfirmed by anyone.** That
distinction was added.

Blurring it into "works" loses trust the moment control doesn't work;
blurring it into "unverified" undersells what's already confirmed.

281 synthetic test cases pass. 68 of them were built from real reported
responses.

## v0.8.0 — 2026-07-30

**Fixed five Airone bugs from two real-device reports.** Received
diagnostics from an `NRT-530S3` (1900) and an `NRT-530Z3` (1901, with an
air monitor). None of these could have surfaced from synthetic data.

### 1. Fan speed only ever showed "auto"

`ModeDid` had **a field that doesn't appear in the APK.**

```json
{"name":4, "option":1, "airVolume":4, "supportedAirVolumes":[1,2,3,4]}
```

`airVolume` is **the current value**, and `supportedAirVolumes` is **the
selectable list.** We only read the former, so the entity had just one
option.

**This is the cause behind the community's "fan speed never shows
low/medium/high."**

### 2. Only half the operating modes showed up — `configurable` was
### misread

We had read it as "can this mode be selected." **Wrong.** Entries with
`configurable: true` are exactly the ones carrying `supportedAirVolumes`
or `additionalData` — it means **"can fan speed or humidity be adjusted
within this mode,"** not whether the mode itself is selectable.

Auto-run, ventilation+dehumidify, cooking, sleep, turbo, and eco all come
back `false`, yet every one of them is selectable in the app.

| | before | after |
| --- | --- | --- |
| operating modes | ventilation · dehumidify · purify · bypass (4) |
**auto-run · ventilation+dehumidify · ventilation · dehumidify · purify ·
cooking · sleep · bypass (8)** |
| fan speed (ventilation) | auto (1) | **low · medium · high · auto ·
turbo · eco (6)** |

### 3. Not a single air-monitor air-quality sensor was created

The `/air-sensor` response path was wrong.

```
assumed: data.airs[]
actual:  data.sensorList[].airs[]
```

An air monitor was attached, yet zero sensors were created. Since there
can be multiple zones, we now scan the whole list, and kept the earlier
assumed path as a fallback.

### 4. Target humidity showed as `1`

`type: 1` inside `additionalData` **means something different depending on
where it appears.**

| location | type 1 range | meaning |
| --- | --- | --- |
| dehumidify inside `mode[]` | 40-65 | target humidity |
| at the `roomController` level | 0-4 | not humidity |

On a device in ventilation mode, the controller-level `1` was being read
as humidity, pinning the slider to the far left. We now only accept it as
humidity **when it falls inside the range the server reported for that
mode.** In modes that don't provide a range (ventilation, purify), the
slider is now disabled — matching the app.

### 5. The air monitor's `deviceId` wasn't being redacted in diagnostics

`supported_devices` passes through `async_redact_data`, but the
`airone_entities` table we assemble ourselves went out unredacted. **The
air monitor's device ID appeared as-is, and a reporter posted it on a
public issue.**

Redaction is now applied to our own hand-built table too. **Redacting the
source doesn't automatically make a derived table safe.**

### Verification

Both real-device responses were transcribed directly into tests — 51 cases
(28 + 23). 264 total.

**Synthetic data could not have caught a single one of these five.**
`supportedAirVolumes` doesn't appear in the APK, so we had no way to know
it existed; `configurable` was understood backwards; the `/air-sensor`
shape was a guess; and the per-location meaning of `additionalData`
couldn't have been imagined.

### Also confirmed along the way

The `Properties.data.did.reported` path was correct. The `idu` field does
exist (an empty array on both devices). Outdoor unit `modelCode
2000`/firmware `17.00`, indoor unit `13.0`, air monitor `35`/`12.00`. 4
filters (`type` 1, 2, 3, 6). Got a lead for inferring `sensor[]`'s integer
`type` meaning from its range (`type 6` = 400-9999 → CO2). Details are in
the dev notes.

## v0.7.7 — 2026-07-30

**Fixed a bug where not a single ventilation-purifier entity was ever
created.** The decisive clue was a report that every other prior
integration showed the device, ours alone didn't.

### Cause — we abandoned the whole device

`AironeDevice.parse` looked like this.

```python
controller = did.get("roomController")
if not isinstance(controller, dict):
    return None          # ← the device disappears right here
```

When capability metadata (`did`) was missing, we **returned `None` and
built nothing at all.** There's a window — right after registration, for
instance — where a device hasn't uploaded its `did` yet, and a user in
that window saw zero entities.

Prior integrations, at the same point, **create the device unconditionally.**

```python
modes = self._extract_modes(raw_device)   # empty tuple if absent
return NavienDevice(...)                  # created anyway
```

**They had it right.** "We don't know what can be selected" and "there is
no device" are different statements. Power, running state, and errors come
from the state response, so they're usable even without metadata.

### The fix's effect

| | before | after |
| --- | --- | --- |
| no `did` | **0 entities** | power · running state · error code · error |
| only `roomController` missing | **0 entities** | the above + filter usage
(from outdoor-unit info) |
| operating mode / fan speed / target humidity | — | only when the server
provides it (unchanged) |
| air quality | **never created** | created based on the `/air-sensor`
response |

There's now exactly **one** condition for giving up on registering a
device: missing `deviceId` or `deviceSeq`. That genuinely can't be
created, since the device can't be identified.

### Separated "older generation" from "couldn't read the value"

When `modelCode` couldn't be read as a number, this had been blurred into
"using older-generation communication." **Not knowing and being
older-generation are different things.** These are now logged separately.

### Why we missed it

When comparing against prior integrations, **we only looked at the entity
list and control features.** "When does the device not get created at
all" never made it into that comparison table. Even a fully filled-out
feature comparison is **meaningless if the device disappears one step
earlier.**

213 synthetic test cases pass. 17 regression tests were added for this
path.

## v0.7.6 — 2026-07-30

**A report came in that no ventilation-purifier entities were being
created.** Fixed it so the cause can be pinned down in one pass next time.

### Now accepts `serviceCode` even when it arrives as a string

```python
service_code = raw.get("serviceCode")
if service_code not in SUPPORTED_SERVICE_CODES:   # (200, 300)
```

A mat's response carries `"serviceCode": 200` as an integer. **Assuming
Airone would do the same was the mistake.** If it arrives as the string
`"300"`, this comparison silently fails and the whole device drops into
"unsupported." Now compared as an integer instead.

We'd already seen `modelCode` arrive as a string from mats
(`"257"`) and were handling it. Since types are mixed within the very same
response, `serviceCode` should have been suspected too.

### Logs **what was actually seen** on a skip

The existing log had just said "no capability metadata." Even with a
report in hand, **there was no way to know where to look.** It now
includes the response's key structure alongside it.

```
능력 메타데이터를 찾지 못해 엔티티를 만들지 않습니다
(찾은 곳: Properties.data.did.reported / 실제 키: {'nickName': {...},
 'registry': {'attributes': {...}}})
```

A different path shows up right there. **Only key names are captured,
never values** — a nickname could contain a family member's name, and a
device ID must never leak through here either. Confirmed by a test.

### There are only three paths where an entity fails to get created

| path | log |
| --- | --- |
| `serviceCode` is neither 200 nor 300 | "…skipping" |
| capability metadata not found | the new log above (with key structure) |
| `modelCode < 1000` (older generation) | "using older-generation
communication" |

**All three now leave a reason behind.** No path disappears silently.

194 synthetic test cases pass. Real-device verification is still at 0.

## v0.7.5 — 2026-07-30

**Compared against the prior integration again, this time at its current
version (0.1.8).** The earlier comparison had been against 0.1.2, six
versions behind. That comparison was stale.

### Widened sensor name aliases

That project added a large number of aliases in 0.1.8. Some names we
weren't accepting:

`air_quality` · `air_quality_score` · `airquality` · `totalairquality` →
overall air quality
`radonStageValue` · `radonConcentration` (and `_`-separated forms) → radon
`voc` · `t_voc` → volatile organic compounds
`pm1_0` · `pm2_5` · standard names differing only in case → each
particulate sensor
`co2Value` · `carbonDioxide` → carbon dioxide

**Keys are not normalized mechanically.** Stripping separators would turn
`pm1.0` into `pm10`, **colliding with a different sensor** — PM1.0 and
PM10 are separate readings. Kept as an explicit table instead of a rule,
with a regression test attached.

### `running` is now also read as a boolean or a string

We'd already confirmed it arrives as an integer. But that project changed
to probing three spots across four keys (`"running", "isRun", "state",
"power"`) — read as **a signal that the value's shape can vary.**

- boolean → running/stopped
- `on` `run` `running` `true` `y` `yes` / `off` `stop` `stopped` `false`
  `n` `no` / `away` `out`
- `0` → stopped (the server itself uses 1/2/3, but even if 0 arrives,
  "stopped" is a better read than "unknown")

**Only reading was widened.** Sending still uses integers exclusively.

The `isRun`, `state`, and `power` keys that project added were **not
followed.** They're fields absent from the APK's
`RoomControllerStatus`. Probing an unfounded location is different from
being lenient about a value's shape.

### What didn't need to be followed

| their 0.1.8 | ours |
| --- | --- |
| added `1900` to the `modelCode` whitelist + a model-name regex | **a
`>= 1000` range check.** Already broader from the start |
| hard-coded air monitor `"34": "NAA-30DM"` | model names aren't hard-coded
— shows the `modelCode` the server sent, as-is |
| added `all_devices` to diagnostics | we already carry the full raw
payload |
| added an `idu` field to diagnostics | absent from the APK. Our
diagnostics carries the raw payload wholesale, so if it does arrive it
already shows up as-is |

Having to add `1900` after the fact is the cost of a whitelist approach.
In the meantime, an `NRT-530S3` user hit "it doesn't load at all."

194 synthetic test cases pass. Real-device verification is still at 0.

## v0.7.4 — 2026-07-30

**When `running` can't be read from the room controller, it's now read
from the outdoor unit.**

**Both** `RoomControllerStatus` and `OduStatus` carry a `running` field.
Only ever looking at the room controller meant a device that reports this
field empty there left the power switch permanently `unknown` — **the
value existed, we just weren't reading it.**

The room controller is still checked first — that's the one the user
actually touches.

### Why fix this now

Went back and looked at how the prior integration handles `running`. It
probes three spots.

```python
running = status.get("running")
if running is None: running = room_controller.get("running")
if running is None: running = room_controller.get("state")
```

**That's a signal they weren't confident where it lives either.**
`room_controller.state` is a field absent from the APK, so it wasn't
followed, but the outdoor unit's `running` genuinely exists in
`OduStatus`. Only the fallback with APK grounding was added.

### Left the power-value mapping unchanged

That project also uses `1 = running` (`"running": 1 if power else 2`,
`state["power"] = int(running) == 1`) — the same as us.

**But this isn't treated as verification.** Both likely came from the same
APK constant (`OPERATION_STATUS_ON_V2_1MODEL = 1`), so all this confirms
is that the two don't contradict each other. **We could both be wrong
together.**

A user of that project reporting 「전원은 상태반영이 잘 안되고 있는것 같습니다」
is more likely caused by **the 120-second optimistic-update window**
(`OPTIMISTIC_STATE_TTL`) than by the mapping. If the device rejects the
command, that produces "it worked, then reverted." We instead re-read the
real state 3 seconds later (v0.7.0).

179 synthetic test cases pass. Real-device verification is still at 0.

## v0.7.3 — 2026-07-30

**Cut the README roughly in half and corrected three inaccuracies.** No
code changes.

It had grown to 286 lines, past the point anyone would read to the end.
Trimmed to 204 lines, with a divider added under every top-level heading.

### Inaccuracies

| location | wrong | corrected |
| --- | --- | --- |
| developer CLI | **"read-only"** | `control`, `airone-control`, and
`airone-status` **send commands to the real device.** Without `--yes`
they only print the body |
| when it applies | "periodic polling is 15 minutes" | it's 5 minutes if
an Airone device is present. Removed the number and kept only "applies
immediately" |
| what works | ventilation-purifier "`NRT-530S3`/`NRT-530Z3` **confirmed**"
| what's actually confirmed is only **that it's a current-generation
model**; whether it works on that specific device is unknown |

"Read-only" was a dangerous error. Believing it and running `control`
actually turns a mat on.

### What was missing

- **Ventilation-purifier model list** — `NRT530 (3W)`, `NRT/NRZ530`,
  `NRT-30`, `NRT-21D`, `NRT-20D`, `NTR-10PW` from the app's add-device
  screen
- **v0.4.x migration steps** — added, then **removed again.** The domain
  changed within the same day the repository went public, and install
  count was 0, so there's no one left to migrate. A paragraph explaining a
  situation that doesn't exist only confuses the reader

  What real measurement confirmed is kept — **HACS doesn't delete the old
  folder when the domain changes.** Diagnostics' `custom_components`
  showed both `navien_smart 0.4.6` and `navien_smarthome 0.7.2` at once.
  Leaving both around shows the same integration twice in the add list,
  and the old folder still has a `config_flow`, so picking the wrong one
  pulls up the old version. **The next time the domain changes, this
  needs to be documented up front**

### What was trimmed

Sections with long design rationale were cut to a line or two — why
stepped models don't use a slider, why four-season cooling is disabled,
family accounts, no local control. **The reasoning stays in this
document; the README states only the conclusion.**

## v0.7.2 — 2026-07-30

**Restored the radon unit `Bq/㎥`. Removing it alongside TVOC in v0.7.1 was
a mistake.**

Two items with different levels of evidence had been handled by one rule.

| | candidate units | decision |
| --- | --- | --- |
| **radon** | `Bq/㎥` only | **attach it.** Every domestic indoor-air-quality
standard uses this unit (148 Bq/㎥ for multi-use facilities and schools).
`pCi/L` is US-only. If a device sold domestically reports radon as a
number, it's Bq/㎥ |
| **TVOC** | `㎍/㎥` (standard 500), `ppb` (common sensor convention), or an
index (0-500) | **leave it blank.** An observed value of 70 fits all
three, a coin flip |

The absence of a unit string in the app is **because the app displays a
grade**, not because the value itself has no unit. Radon has exactly one
candidate, so this is closer to "known" than "unknown."

### Made units attached without hard evidence visible from outside

A unit set by judgment rather than pulled from the app is now flagged.

- `unit_inferred: true` on the radon sensor's attributes
- `air_sensor_units` in diagnostics — per-item unit, whether it was
  inferred, the raw value, and the grade

A report that it's wrong can now be fixed immediately. **A value entered
by judgment is not hidden behind the appearance of certainty.**

## v0.7.1 — 2026-07-30

**Removed the radon unit `Bq/㎥` added in v0.7.0. It had no basis.**

We'd followed the prior integration's use of `Bq/m3`, backed by the
international standard using the same unit. But **the app itself gives no
grounding for it.**

A full search of the dex found the app uses exactly **two** air-quality
unit strings: **`"ppm"` and `"㎍/㎥"`.** Radon, TVOC, and overall air
quality carry no unit at all. The device's own wall display shows only a
grade too, and radon settings are handled purely as a relative intensity
called "radon management level."

**Radon is a carcinogen, and the domestic indoor standard is 148 Bq/㎥.**
Attaching the wrong unit can lead a user to a wrong health judgment.
Leaving it blank is the right call here, over a plausible-sounding guess.

The grade for all three sensors is still kept in the `grade` attribute —
matching what the device's own screen shows.

### Confirmed from the device's own screen

Looked at the wall room-controller's screen. It showed `통합공기질 78`,
`PM1.0 좋음`, `PM2.5 좋음`, `CO2 좋음`, `TVOC 좋음`, `자동운전-자동`.

- **`총합 78` is a number** — confirming that fixing it to a score in
  v0.7.0 was correct
- **`자동운전-자동` shows mode and fan speed joined together** — confirming
  that splitting them into separate axes was correct
- **This screen showing TVOC as a grade has nothing to do with whether the
  value itself exists.** The same screen also shows only a grade for
  PM1.0 and PM2.5, and those two are confirmed to carry numeric values
  (the air monitor's own screen shows `5.0 ug/m3`). **It's purely a
  display choice**

## v0.7.0 — 2026-07-30

**Compared the prior Airone integration against ours down to the code
level.** Checked every point where its users had complained, and fixed
four things we ourselves had gotten wrong.

### Re-split operating mode and fan speed along the same axes as the app —
### a design mistake on our side

Turbo, eco, and basal had been placed **in the operating-mode list.** That
grew the list to 14 entries, forcing anyone who just wanted to change fan
speed to dig through the mode list.

The app splits these into two axes (columns one and two of
`AironeModeCode.labelFor`).

| axis | contains |
| --- | --- |
| operating mode | 자동운전 · 환기 · 제습 · 청정 · 요리 · 바이패스 · 환기제습 ·
**숙면** |
| fan speed | 미풍 · 약풍 · 강풍 · 자동 · **터보** · **절전** · **기저** |

On a real device (NRT-530Z3) the mode list shrank **from 14 to 8**, with
turbo and eco moving to fan speed. Only sleep stays on the mode side, like
the app — it's a separate button there too.

Even if the server doesn't give `option 1` for a given mode and only gives
turbo, that mode still stays in the list.

### The air monitor is now a separate device

The air monitor that carries the air-quality sensors (confirmed on a real
`NAA-21DM`) is now created as **a separate device**, hung off the main
unit via `via_device`. Cramming 9 sensors onto the main unit's card made
it hard to read.

The model name isn't hard-coded — it shows the `modelCode` the server
sends.

### Built the air-quality sensors properly

- **Attached `device_class`** — PM1/PM2.5/PM10/CO2/temperature/humidity.
  HA handles the icon, history graph, and unit conversion on its own
- **Radon unit `Bq/㎥`** — the app has no string for it, so we used the
  international standard. Display-only, never used in control
- **Sensor key aliases** — the same sensor is grouped together even when
  the server names it differently, e.g. `radonValue`, `PM2.5`,
  `airQualityScore`
- **No unit attached for TVOC.** It could be ppb, ㎍/㎥, or an index — no
  way to tell. **A wrong unit is worse than no unit**

### Target humidity moves in steps of 5

The app's −/+ buttons move by 5 (`setProgress(getProgress() ± 5)`). The
server doesn't specify a step, so we follow the app. If an automation
sends an arbitrary value, it's rounded to a multiple of 5 and then clamped
to the server's range.

### "Power state doesn't reflect" — re-query instead of an optimistic update

The prior integration updates the UI first, right after a command, and
holds that for 120 seconds. If the device rejects it, the user experiences
**"it worked, then reverted"** — a failure that looked like success.

We don't update the UI first. Instead, **we ask for the real state again
3 seconds later.** If the device reports on its own first, that arrives
sooner; if not, this single follow-up catches up. Mats skip optimistic
updates for the same reason.

### Comparison results

| item | prior integration | ours |
| --- | --- | --- |
| supported models | whitelist of just `1901` | **all of `modelCode >=
1000`** |
| operating mode | hard-coded 8 combinations | **the server's `mode[]`** |
| fan-speed axis | airVolume + option | same |
| target humidity | steps of 5 | steps of 5 + validated against the
server's min/max |
| air quality | 9 kinds | 9 kinds + aliases + `device_class` |
| air monitor | separate device | same |
| running state / error / filter | absent | **present** |
| after a command | optimistic for 120s | **real re-query 3 seconds
later** |
| real-time | `cloud_polling` | **`cloud_push`** |
| CLI verification tool | absent | **present** |

**None of their 5 control features (power, mode, fan speed, humidity, mat
temperature) were missed.**

The `1901` whitelist caused real harm — an `NRT-530S3` (1900) user hit
"it doesn't load at all." We catch this because ours is a range check.

### Verification

175 synthetic test cases pass (147 prior + 30 for the axis redesign,
duplicates cleaned up). **Real-device verification is still at 0.**

### Confirmed through community comments

**A few real-device facts were confirmed through community comments.**
Not our own devices, but the values shown on screen there are still
evidence.

#### Air-quality values had been stored as strings — switched to numeric —
#### a mistake on our side

We had concluded `tvoc`, `radon`, and `total` "only ever arrive as a
grade." **Wrong.**

A real device's screen showed `Air Quality Score 82.0` and `TVOC 70.0` as
numbers, and another user wrote "라돈수치도 잘 뜹니다" (radon numbers also show
up fine).

The `getValueText` function I'd relied on was **a display function.** The
app chooses to show it as a grade — the value itself isn't absent.
**Inferring whether data exists from UI code leads to exactly this kind of
mistake.**

Now decided from the first value seen — if it reads as a number, it
becomes a numeric sensor (with a graph); otherwise a string. The grade is
kept as a `grade` attribute in both cases.

The unit is still unknown (radon is guessed at Bq/㎥, tvoc at ppb only).
**We don't guess and attach one — we output the bare number.** `total` is
a 0-100 score, not a grade.

#### Matched operating-mode names to the app's control-screen wording

We had used the hard-coded strings from `AironeModeCode`, which turned out
to be the short labels used for quick mode. The control screen uses
different entries from `strings.xml`.

| before | after |
| --- | --- |
| 자동 | **자동운전** |
| 환기 · 터보 | **환기 터보** |
| 제습 · 절전 | **제습 절전** |

Also filled in names for exhaust (5) and negative-pressure ventilation
(18). Any device whose server sends those modes now shows a name instead
of a bare number.

#### Confirmed — nothing needed fixing here

| community report | our side |
| --- | --- |
| `NRT-530S3 (1900)` doesn't load at all | **caught.** Not a model
whitelist — a `modelCode >= 1000` range check |
| mode list "seems to be missing a few entries" | **can't happen.** We use
the server's `mode[]` |
| fan speed only shows turbo/eco | an energy-recovery ventilator has no
fan-speed steps. Turbo and eco are `(mode, option)` combinations, so they
land **in the operating-mode list** — matching the app |
| local ports are all closed | matches what we confirmed by real
measurement |

Confirmed three real-device modelCodes — `NRT-530Z3` 1901, `NRT-530S3`
1900, air monitor `NAA-21DM` 35.

## v0.6.0 — 2026-07-30

**Added ventilation-purifier (Airone) support. Could not verify against a
real device.**

We don't have an Airone device at home. The protocol was pulled entirely
from the app, but **it has never actually been sent to one.** That fact is
stated openly in three places — the README table, an install-time log
warning, and the `verified_on_hardware: false` attribute on the running
state entity.

### Entities created

| entity | domain | created when |
| --- | --- | --- |
| power | `switch` | always |
| operating mode | `select` | when the server tells us which combinations
are selectable |
| fan speed | `select` | when two or more options exist for the current
combination |
| target humidity | `number` | when the server tells us a humidity range |
| running state / error code | `sensor` | always |
| air quality | `sensor` | for every item the server provides a value for |
| filter usage | `sensor` | for every filter the outdoor unit reports |
| error | `binary_sensor` | always |

### No model table hard-coded into the code

We reused the approach that worked for mats. In the device-list response,
`Properties.data.did.reported.roomController.mode` reports **the complete
set of operating combinations that specific device supports.** Selectable
options are built only from this.

- A combination with `configurable: false` isn't added to the list
- The humidity range also comes from `additionalData`'s `min`/`max` —
  never a hard-coded number
- If the server doesn't send `mode`, the mode selector simply **isn't
  created**, with the reason logged

### Older-generation models are skipped

The app's code has a `modelCode < 1000` branch. That single check changes
the topic and the payload envelope entirely.

| | older generation | current generation |
| --- | --- | --- |
| topic | `cmd/rc/{modelCode}/…` | `cmd/rc/v2/{modelCode}/…` |
| payload location | `request` | `state.desired` |
| `running` value | running is **2** | running is **1** |

**Sending both through the same code path would flip power on and off
backwards.** We only handle current-generation devices; an older one is
recognized and skipped, with the reason logged.

### We don't send a value we don't recognize

We couldn't confirm whether `ModeDid.airVolume` is a single value or a
bitmask. So **any value absent from the confirmed table (1-6) is
discarded.** If it turns out to be a bitmask, a value like `15` could
arrive, and sending that through unchanged risks an unknown device
reaction.

`tvoc` and `radon` aren't shown as numbers by the app either — only a
grade arrives. We don't manufacture a number and attach it; the grade
stays a string.

### Three CLI commands

For a report to be useful, someone needs to be able to check things one at
a time before ever touching HA.

```bash
python3 tools/navien_cli.py airone-modes   --device-seq N   # supported combination table. sends nothing
python3 tools/navien_cli.py airone-status  --device-seq N   # state request + air quality
python3 tools/navien_cli.py airone-control --device-seq N --mode 9 --humidity 55
```

`airone-control` **rejects a value absent from the server-reported
combinations.** Without `--yes` it only prints the body it would send and
stops there.

### What's verified and what isn't

92 cases confirmed with synthetic data — 41 for model parsing, combination
derivation, and payload generation; 10 for the envelope; 16 for the MQTT
parser and mat regressions; 25 for CLI guards.

**Real-device verification: 0.** For that reason, the issue template was
changed from "send a report and we'll build it" to "please tell us whether
this worked or not."

### Changes on the mat side

- Changed the MQTT subscription wildcard from `+` to `#`. This matches
  what the app uses (`HomeViewModel`'s `/{prefix}/#`), and an Airone
  response can arrive one level deeper. `#` is a superset of `+`, so mat
  behavior is unchanged
- Nothing else on the mat side was touched. Preserving an already-verified
  path takes priority

## v0.5.0 — 2026-07-30

**Changed the integration identifier (domain) from `navien_smart` to
`navien_smarthome`.**

When the identifier changes, HA can't carry over the previous config
entry. **Existing users must follow these steps exactly.**

1. 설정 → 기기 및 서비스 → 「Navien Smart」 → ⋮ → **삭제**
2. **Reinstall** this integration through HACS (reinstall, not update)
3. **Restart HA**
4. 설정 → 기기 및 서비스 → 통합 추가 → 「Navien Smart」 → enter your ID and
   password

After step 3, delete the `custom_components/navien_smart/` folder if it's
still there. HACS cleans it up automatically most of the time, but it can
be left behind.

Devices and entities are recreated. **Entity IDs are derived from the
device name, so they stay the same as before**, but if you had automations
depending on them, it's safer to check once after re-registering.

No functional changes.

## v0.4.6 — 2026-07-30

**Raised the minimum Home Assistant version to 2025.2.** Caught during a
pre-release check.

`hacs.json` declared a requirement of `2024.12.0`, but the code uses newer
APIs than that.

| API | used at | introduced in |
| --- | --- | --- |
| `AddConfigEntryEntitiesCallback` | 10 places | HA 2025.2 |
| `_get_reauth_entry`, `data_updates` | config flow | HA 2024.11 |
| `model_id` (DeviceInfo) | entity | HA 2024.8 |

A user on HA 2024.12 installing this would hit an `ImportError` that
prevents the integration from loading at all, seeing only an
unexplainable traceback.

**Having HACS filter this out before install is the right kind of
failure.** It now shows "requires HA 2025.2 or later" and blocks
installation. Also stated explicitly in the README's install section.

`2025.2.0` is a conservative choice. We couldn't pin down the exact
version `AddConfigEntryEntitiesCallback` was introduced in, so we chose
to set the floor high and block rather than set it low and break.

## v0.4.5 — 2026-07-30

**Removed the word "진단" (diagnostics) everywhere it was visible to
users.**

v0.4.3 had renamed the menu item to `통계정보 다운로드` and added a note
alongside it — "the original label is Download diagnostics, and depending
on your HA version it may show as 진단 정보 다운로드 instead." That was
meant to cover a possible translation mismatch, but **the note itself was
what caused the confusion.**

A user only needs to know the name that appears on the HA screen. Showing
"통계정보" and "진단" together makes them wonder whether these are the same
thing or different things.

- README — changed "진단 파일" to "통계정보 파일." Removed the translation
  note paragraph
- 4 issue forms — field name changed from `진단 정보` to `통계정보 파일`.
  Removed the translation note
- `coordinator.py` log — removed the parenthetical `(진단 파일)` from
  `'통계정보 다운로드'(진단 파일)`

"진단" was left as-is in code comments — it's never shown to users there,
and it's a developer-facing term referring to HA's diagnostics feature.

## v0.4.4 — 2026-07-30

**Reworded the reporting instructions into plain user language.** They had
been demanding developer vocabulary.

### What was wrong

The four-season instructions read like this.

> If you use a four-season mat, please attach **the `운전 상태` sensor's
> attributes** to your issue. It contains `season` and `cool_control`.

This made users **dig through entity attributes.** But **the diagnostics
file already contains that same value** (`diagnostics.py`'s `_entity_view`
already carries `season`, `cool_control`, and `is_cooling`).

Downloading a single diagnostics file was all that was actually needed,
yet the instructions demanded an unnecessary extra step. Diagnostics
exists precisely to lower the bar for reporting, and the instructions were
making that pointless.

### What was fixed

- README's four-season section — reworded to "please leave it running in
  cooling and send us the diagnostics file." Kept only the reason for
  leaving it in cooling and dropped the rest
- `coordinator.py`'s four-season log — replaced showing the raw
  `coolControl=...` value to the user with instructions on how to report
- 3 issue forms — replaced mentions of JSON paths like
  `report_wanted_devices`, `entities[].season` with "it's all in the
  file. There's nothing you need to look up separately"

### The principle

**The one thing a user needs to know is where to click to get the file.**
The structure inside it is for whoever reads the report to work out.

## v0.4.3 — 2026-07-30

**The menu name for downloading the diagnostics file was wrong.** Fixed
the instructions.

We had written `진단 정보 다운로드`, but HA's Korean-language screen
actually shows **`통계정보 다운로드`.** The original English label is
`Download diagnostics`, and the translation renders it as "통계" (statistics).

A user looking for "진단" (diagnostics) couldn't find it. Instructions
meant to make reporting easier were instead blocking reports.

Fixed in 7 places — README, 4 issue forms, the `coordinator.py` log, and a
`diagnostics.py` comment.

**Both names are now shown together.** HA's translation can change from
version to version, so relying on only one risks drifting out of sync
again. `통계정보 다운로드` is listed first, alongside a note that the
wording may differ from the original English label.

## v0.4.2 — 2026-07-30

Added a **per-model support table** to the README. No code changes.

Users search by their own model name, so having a full list helps. Copied
the entire heated-mat list from the app's `기기 추가` (add device) screen.

### Bluetooth-only models are confirmed unsupported

The app catalog's `숙면매트 온수 (Bluetooth)` line (`EQM530`-`EQM571`,
`EQH20/40DN`) **talks directly to the phone over BLE and never goes
through the cloud.**

Evidence from the APK:

```
STX_BLE = 178                          BLE frame start byte
MATE_BT_DEVICE_CONTROL_POWER_ON = 1    BLE payload value
HOT_WATER_MAT_PREFIX = "KDO"           older-generation BLE prefix
```

HA isn't a phone, so there's no way to connect to these. **No plan to
support them going forward.**

### Split the table into three confidence levels

- **Confirmed** — EME-500 single and double zone. Confirmed on a real
  device down to querying, receiving, controlling, and switching to step 0
- **Expected to work** — the integration doesn't hard-code a per-model
  table and runs off server-provided values, so this should work
  structurally, but no real-device confirmation exists
- **Doesn't work** — Bluetooth-only

We didn't write "any similar device works," because one entire line (BLE)
doesn't work at all.

### One ambiguity left unresolved

`EQM551` appears in **both** the app's Wi-Fi list and its Bluetooth list.
We couldn't confirm whether this is two different devices sharing a number
or the app listing it under both for convenience. Noted as-is in the
README with a request for reports.

## v0.4.1 — 2026-07-30

**The device page now shows firmware, model, and serial number.**

Wired server-provided values into HA's device info.

| field | source | observed value (EME-500) |
| --- | --- | --- |
| model | `model` + `modelType` | `EME-500 (카본)` |
| model ID | `modelCode` | `257` |
| firmware | `mcu.version` + `wifi.version` | `14.0.0 (Wi-Fi 5.1.100)` |
| serial number | `deviceId` | unique per device |

**There are two firmware versions, but HA has only one field for it.** The
mat's main body (MCU) and the Wi-Fi module each carry their own firmware.
Combined into a single line.

**Wi-Fi firmware was not placed in the "hardware" field.** HA displays
that field as a hardware revision, so putting firmware there would be
misleading. The server doesn't provide a hardware revision, so that field
is left blank.

For a response that doesn't include version info, the firmware field is
blank while the rest still display normally.

### What wasn't added

The first 12 characters of `deviceId` appear to be the device's MAC
address (`AABBCCDDEEFF` + `0001` matching the ARP entry
`aa:bb:cc:dd:ee:ff`). Registering it as a MAC in HA would link it to
router/DHCP integrations, but **it was not added.**

This correspondence was only confirmed on one device, and registering a
wrong MAC would cause HA to **merge it with a different device** in the
device list. That's an annoying side effect to undo, so one piece of
evidence isn't enough to justify adding it.

## v0.4.0 — 2026-07-30

**Changed step control from a slider (`number`) to a selection list
(`select`).** This is a breaking change since the entity itself is
replaced.

### Why

The `number` slider was wrong in three ways.

- **It requires precision.** Dragging across 8 positions in a narrow card
  row means aiming for 3 and landing on 4. On a heating device, a
  one-step overshoot isn't something to shrug off
- **A tick mark can't carry a name.** The app calls the leftmost position
  `운전 대기` (standby), but it showed as `0 단계` (step 0)
- **0 couldn't actually be set.** The server sends `rangeMin: 1`, so the
  minimum was 1. Since the device does report 0, this left it **visible
  but unsettable**

Same logic as the reason step control was never modeled as `climate`
("a step isn't a temperature"). **A step isn't a continuous quantity
either.** It's 9 discrete states, and 0 among them is a state, not a
number.

### Step 0 = standby — confirmed by real measurement

Lowered the right side to step 0 in the app and watched the shadow.

```
12:11:34  right = {level: 1, enable: True}
12:16:26  right = {level: 0, enable: False}
```

**`level 0` and `enable false` move together.** So selecting `운전 대기`
now sends both at once. No need for a separate switch entity.

`select` options: `운전 대기`, `1단계` … `8단계`

**One side alone can be set to standby.** Unlike the app, there's no need
to turn the whole device off.

### A bug caught during implementation

The first implementation applied the `level 0 → enable false` rule to
**every zone.** Lowering the left side to standby also overwrote the right
side's `enable`, **turning it off too.**

Fixed by adding a `zone in changes` condition so it only applies **to the
zone actually being changed.** Verified the other side stays untouched
across three combinations.

### Numeric history is lost

`select` state is a string, so no graphs or long-term statistics. The
`level` value is instead kept as an entity attribute, so automations and
templates can still use it as a number.

If a graph turns out to be needed later, a separate read-only numeric
`sensor` will be added then — not preemptively.

### After updating

The `number.*_단계` entities disappear and `select.*_단계` ones appear. If
you had these on a dashboard, the cards will need to be reassigned. Any
leftover old entities can be cleaned up from Settings → Devices.

## v0.3.3 — 2026-07-30

**The wait-time note in v0.3.2 was wrong. Corrected.** No code changes.

We had written "usually under a minute, observed max 1 minute 30 seconds,"
but the actual figure is **1.4 seconds.** Measured twice in HA.

| run | subscription started | `reported` received | elapsed |
| --- | --- | --- | --- |
| 1st | 11:55:30.838 | 11:55:32.211 | 1.373s |
| 2nd | 11:56:50.804 | 11:56:52.152 | 1.348s |

### Where did the 93 seconds come from

That figure was observed right after turning on v0.3.0, but **it had
attributed a different event to the wrong cause.**

The initial state was requested at 11:40:58, and a value arrived at
11:42:31 — so it was read as 93 seconds. But in between, the same device
had been tested three times over the CLI, and what arrived at 11:42:31
was the response to one of those. **The 11:40:58 request either never got
a response, or it was lost.**

At the time, `clientId` used `homeSeq`; it now uses `userSeq`. An A/B test
showed both subscribed successfully, so that can't be pinned down as the
cause either. Since it couldn't be reproduced, this stays an unconfirmed
cause.

### What we decided not to do, as a result

- **Not adding `RestoreEntity`.** There's no reason to show a stale value
  to paper over 1.4 seconds. If someone operated the device while HA was
  off, that comes with the cost of showing a wrong value
- **Not investigating `devices/{deviceSeq}/log`.** The goal had been to
  get initial state faster, and there's no longer a problem left to solve

Building a countermeasure without measuring first just adds unnecessary
code.

## v0.3.2 — 2026-07-30

Documented why values are empty right after a restart, and added logging
to help debug it. No behavior changed.

### Why it's empty right after restart — investigation results

Values weren't failing to arrive — this was **the time spent waiting for
the device to respond.** One observation in HA measured **93 seconds**
from request to arrival, while CLI tests got a response within 30
seconds. Given the variance, the README says "usually under a minute,
observed max 1 minute 30 seconds."

Tested whether a faster path exists — it's blocked.

```
$aws/things/{deviceId}/shadow/name/status/get
→ rejected: {"code":404,"message":"No shadow exists with name: 'status'"}
```

**There is no shadow on the user's account.** The device reports to its
own dedicated AWS account, and Navien relays that onto the user account's
topic. There's no way to read the stored state directly — waking the
device and waiting for its report is the only path.

Confirmed that repeating the same request gets a response every time — a
fresh request on every restart is fine.

### Matched `clientId` to the app's format

We had been sending `{uuid}-U{homeSeq}`. The app uses
`{uuid}-U{userSeq}`. An A/B check showed both subscribe successfully for
now, but if the server ever starts validating clientId, the one that
diverges from the app would be the first to break.

### Added receive logging

Both received events and **discarded events** are now logged at debug
level. Without this, there was no way to tell "state never arrives" apart
from "it arrives but gets discarded," which had been dragging out root
cause investigations.

## v0.3.1 — 2026-07-30

Fixed the `운전 상태` sensor crashing with an exception.

```
ValueError: Sensor sensor.xxx_unjeon_sangtae is providing enum options,
but is missing the enum device class
```

We had set `_attr_options` without attaching `device_class = ENUM`. HA
threw an exception at the step where it turns state into a string, so
only this sensor ended up with an empty value.

**Fixed by removing `options` instead.** Attaching `ENUM` was also an
option, but ENUM requires the value to always be within the declared list,
and `운전 상태` returns `알 수 없음(N)` for an unrecognized mode. This
sensor would crash again the day Navien adds a new mode. ENUM isn't used
for a value whose list of possibilities can't be closed.

The rest of v0.3.0's fixes were confirmed on a real device — left step 2 /
right step 4 came through into HA exactly as-is, matching the mat's actual
values.

## v0.3.0 — 2026-07-30

**Fixed three bugs found the first time this was actually run in HA.**
Through v0.2.3, entities were created but their state stayed permanently
`상태 알 수 없음` (state unknown).

### MQTT disconnected itself and reconnected in a loop right after connecting

`_async_run` looked like this.

```python
await self._async_connect_once()               # connect() returns before CONNACK
while not self._stopping and self.connected:   # connected is still False
```

`paho`'s `connect()` doesn't wait for the CONNACK. So the watch loop
exited immediately, tearing down the connection just made, reconnecting 5
seconds later, and disconnecting again. There was never a window for
state to arrive.

Now waits for the CONNACK before entering the watch loop (up to 15
seconds, retrying if it times out).

### Initial state was never fetched

**Shadow events only arrive when something changes.** Subscribing alone
leaves state empty forever as long as nothing is operated.

Once the subscription is up, we now send **only `event.modelCode`, with
no control field**, to any device that's on. The device then reports its
current state as `reported`. The app uses the same approach. This doesn't
change any setting — there's no value being sent to change.

Nothing is sent to a device that's off. It won't respond; it just queues
in the shadow.

### Blocked the event loop

`ssl.create_default_context()` reads certificates from disk, which
blocked HA's event loop. HA logged a warning about it. Switched to
`homeassistant.util.ssl.get_default_context()`, which HA builds and
caches at boot.

## v0.2.3 — 2026-07-30

Fixed the session-limitation instructions. No behavior change.

v0.2.1 had led with **"create a second, HA-only account"** as the
recommendation. But Navien requires **identity verification** to create
an account (`auth/start-self-auth`, `auth/self-auth`) — tied to a real
name and phone number, meaning **a single user genuinely can't create a
second account.** The recommendation had been a path blocked for most
people.

Reversed the order.

1. **State the actual behavior first** — since there's only one session,
   the app and HA push each other out. The integration automatically
   re-logs-in, so this doesn't get in the way much in practice; the only
   noticeable inconvenience is that HA updates pause while the app is open
2. **Demoted the family account to an option for when that's
   inconvenient**, with the identity-verification requirement stated
   explicitly

## v0.2.2 — 2026-07-30

Changed the icon's shape. No behavior change.

v0.2.1's icon reused the Navien app icon's diagonal wedge and navy color
directly, which **made it look like an official Navien asset.** Removed.

| | v0.2.1 | v0.2.2 |
| --- | --- | --- |
| shape | rounded square + navy diagonal wedge | **house silhouette**
(matching the Miro integration) |
| color | orange + navy | **orange alone** |
| wordmark | navy | ink (`#111827`) |

A community integration's mark needs to read as such from its shape
alone. Only the Navien orange was kept as the color, and the composition
was matched to the existing Miro integration — which also makes it
visible that the same person made both.

The navy `#0F2F6E` is kept in the script for reference only, never used in
the mark itself.

## v0.2.1 — 2026-07-30

Icon and documentation. No behavior change.

### Icon

Added 8 files under `custom_components/navien_smart/brand/`. HA 2026.3+
serves this directly from the integration — `home-assistant/brands` no
longer accepts custom-integration icons.

The colors weren't guessed. **They were pulled from the app icon's actual
pixels** — orange `#F48400`, navy `#0F2F6E`. Navien's logo wasn't copied;
an original mark was built using those same two colors.

The dark variant adjusts the colors. The navy wedge got lost against a
dark background, so it was brightened (`#2A5AAA`); the orange stood out
too aggressively, so it was toned down one notch (`#FF9412`).

### Documentation — local control and session limits

Confirmed two things by real measurement and documented them in the
README.

**Local (LAN) control is impossible.** No open port anywhere across the
mat's full TCP 1-65535 range. The app itself has no direct-connect code
either — a frame protocol constant (`STX_WIFI`) still exists but is
unused, and the UDP code (port 48899) is dedicated to
ventilation-purifier device registration only. The device is a pure
client that only ever connects out to its own dedicated cloud endpoint.

**There's exactly one session per account.** Confirmed by logging in
twice — once a later token is issued, the earlier one dies with a `404`.

There is, however, **a workaround.** Navien offers family sharing
(`/home/{homeSeq}/invite`, etc.). Creating a separate HA-only account and
inviting it avoids conflicting with the phone app. Whether an invited
account can actually control the device wasn't confirmed, and the README
states that plainly.

## v0.2.0 — 2026-07-30

**Added a thermostat (`climate`) entity for mats that operate on
temperature.**

v0.1.0 had a mismatch between documentation and code. The README stated
temperature-based mats were supported, but the `climate` platform didn't
exist at all — a temperature-based device only got a power switch and
sensors, with **no way to actually adjust the temperature.**

### What changed

| control scheme the device uses | v0.1.0 | v0.2.0 |
| --- | --- | --- |
| temperature (0.5-degree steps) | no control | **`climate` thermostat** |
| step (1-8) | `number` slider | unchanged |

For temperature-based devices, the server also sends
`temperature.current`, so the thermostat card's current-temperature field
is populated. Step-based devices don't report a current value, so they
keep the `number` entity.

`climate`'s on/off uses the per-zone `heater.<zone>.enable`. Device power
is handled by a separate switch, so only one side of a double mat can be
turned off.

### Still unverified

**No temperature-based mat was on hand, so `climate` couldn't be confirmed
on a real device.** In particular, sending `enable: false` wasn't
verified — only `enable: true` was confirmed, and only on a step-based
model. If you use a temperature-based mat, a report would help.

### Documentation

- The README's entity table now states the branching by control axis
  explicitly, fixing wording that had read as "always a step slider"
- Changed the support table from grouping by model line
  (carbon/hot-water) to **grouping by control scheme (step/temperature)**.
  Carbon models are usually step-based and hot-water models
  temperature-based, but this can't be assumed from the model name — the
  server's value decides it

## v0.1.0 — 2026-07-30

First release. **Only heated mats are supported.**

### What works

- Carbon (step-based) heated mats — confirmed on 2 real devices, EME-500
  single and double
- Hot-water (temperature-based) heated mats — the code path is settled,
  but unconfirmed on a real device
- Four-season heated mats — **heating only.** Control is left disabled
  while in cooling mode

Entities: power (`switch`), heating step (`number`), running state and
error code (`sensor`), high-temperature warning and error
(`binary_sensor`).

### Why it was built this way

**Step wasn't modeled as `climate`.** Carbon mats operate on steps 1-8,
and the server doesn't provide a current temperature. Modeling it as
`climate` would show "step 3" as "3 degrees," and the current-temperature
field would sit permanently empty. Exposed as a `number` slider instead.

**The poll interval is 15 minutes.** No need for it to be shorter. Real
measurement confirmed the device reports state to the server on its own —
all 12 remote-control operations tested arrived immediately. Periodic
polling exists only to sync up after a reconnect.

**Only features the server declares are created.** A feature absent from
`functions` gets no entity. The real EME-500 device has no `lockMode`, so
no lock entity is created for it. Prior reference material treated this
field as mandatory, which doesn't work on this model.

**No command is sent for a value we don't recognize.** The control axis
is determined by the server's `heatControl.unit`. Only two values are
confirmed: `1.0L` (step, increments of 1) and `0.5C` (temperature,
increments of 0.5 degrees). Any other value skips creating the control
entity, with the case logged.

**Values aren't clamped by the high-temperature warning.** The server's
`safeValue` (confirmed as step 4 on carbon) is only a warning indicator in
the app too, not an upper bound. The full 1-8 range stays open, and only a
`binary_sensor` flags it.

### What doesn't work

- Ventilation-purifiers (Airone), boilers — **reports are welcome.** The
  diagnostics file carries the raw data
- Wall pads/lobby phones, commercial SCADA — out of scope, different
  server architecture
- Mat sleep mode, scheduling, ion care, UV sterilization, quick heating —
  command names and value tables were gathered, but sending them was
  never verified, so they weren't added

### Known limitations

**There's exactly one session per account.** Opening the Navien app drops
the HA session. The integration automatically logs back in, but at that
moment the app gets pushed out instead. This is a server-side constraint
with no workaround.

**Real-time state only arrives over MQTT.** REST only returns
registration-time information, so state stops updating if MQTT
disconnects. Reconnection is retried with a backoff starting at 5 seconds
and growing to a maximum of 5 minutes.
