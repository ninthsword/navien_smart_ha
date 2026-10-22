# Tests

```bash
python3 tests/run.py            # everything
python3 tests/run.py filter     # only files whose name contains "filter"
```

**They run without Home Assistant installed**, and need no external packages. Python 3.11
or later is enough.

## Why they run without HA

`tests/ha_stub.py` imitates **only the names and shapes** of the HA modules the integration
uses; none of the behaviour is reproduced. So what these tests actually check is:

- whether every module really imports (a syntax check does not catch this)
- **which command gets built** when a device response goes in
- **what happens instead of a crash** when a value is missing or malformed
- whether the reasoning behind a decision is written down in the code

A test that needs to know how HA really behaves takes HA's own source instead.

## What is tested

| file | |
| --- | --- |
| `test_imports.py` | every module imports; the CLI parses |
| `test_zone_onoff.py` | turning a single zone off and on (issue #16) |
| `test_filter.py` | the filter sensor reports life remaining (PR #18) |
| `test_boiler_observation.py` | the boiler reads observed MQTT state without control, and public diagnostics carry no identifiers |
| `test_boiler_extra_status.py` | the sensors built from status fields that used to be discarded, and the error-code names |
| `test_gas_statistics.py` | writing gas history into long-term statistics, and handling overlapping months |
| `test_airone_air_kinds.py` | entities survive a shrinking set of air-quality kinds, and the save ordering |
| `test_review_fixes.py` | the fixes from the full review cannot regress |

## Writing a new one

Build devices with `make_mat` and `make_airone` from `harness.py`, and count with `Report`.

```python
from harness import Report, make_mat

r = Report()
r.section("what this looks at")
mat = make_mat(unit="0.5C", range_min=28, range_max=50, capacity=2,
               zones={"left": 33.0, "right": 30.0})
r.ok(mat.zone_is_off("left") is False, "it is on")
sys.exit(r.finish())
```

**Do not build fake state in combinations that cannot occur on a real device.** That is why
`make_mat` derives `enable` from the value: an off value with `enable: true` does not exist
on real hardware, and a test that passes on such data guarantees nothing.

**Write down why a decision was made.** Comments in this repository record the **why** rather
than the what, so the reasoning can be followed without tearing the app apart again.
