"""A minimal stub so the integration modules can actually be imported without Home Assistant.

`py_compile` checks syntax only. **A name error in a class body** — still referencing an
import that was deleted, say — only surfaces on a real import. So just enough of HA is
imitated for every module to be loaded without installing it.

What lives here is **names and shapes only**; none of HA's behaviour is reproduced. A test
that needs to know how HA really behaves takes HA's own source instead.
"""

import sys
import types


def stub(name: str, pkg: bool = False, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    if pkg:
        module.__path__ = []
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class Base:
    """A shell that accepts anything, allowing both subclassing and generic subscripting."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __class_getitem__(cls, item):
        return cls

    def __init_subclass__(cls, **kwargs) -> None:
        pass


def enum(**kw):
    return type("E", (), kw)


stub("homeassistant", pkg=True)
stub("homeassistant.helpers", pkg=True)
stub("homeassistant.components", pkg=True)
stub("homeassistant.util", pkg=True)
stub("homeassistant.util.dt", now=lambda: None)
stub("homeassistant.core", HomeAssistant=type("H", (), {}), callback=lambda f: f)
stub("homeassistant.util.ssl", get_default_context=lambda: None)
stub(
    "homeassistant.config_entries",
    ConfigEntry=Base,
    ConfigFlow=Base,
    ConfigFlowResult=dict,
    ConfigEntryState=enum(LOADED="loaded"),
    OptionsFlow=Base,
)
stub(
    "homeassistant.const",
    EntityCategory=enum(CONFIG="config", DIAGNOSTIC="diagnostic"),
    CONF_PASSWORD="password",
    CONF_USERNAME="username",
    Platform=enum(
        CLIMATE="climate",
        SWITCH="switch",
        SELECT="select",
        SENSOR="sensor",
        BINARY_SENSOR="binary_sensor",
        NUMBER="number",
        BUTTON="button",
    ),
    ATTR_TEMPERATURE="temperature",
    PERCENTAGE="%",
    CONCENTRATION_PARTS_PER_MILLION="ppm",
    CONCENTRATION_MICROGRAMS_PER_CUBIC_METER="µg/m³",
    UnitOfTemperature=enum(CELSIUS="°C", FAHRENHEIT="°F"),
    UnitOfTime=enum(MINUTES="min", SECONDS="s"),
    UnitOfVolume=enum(CUBIC_METERS="m³"),
)
stub(
    "homeassistant.exceptions",
    ConfigEntryAuthFailed=type("A", (Exception,), {}),
    HomeAssistantError=type("B", (Exception,), {}),
    ConfigEntryNotReady=type("C", (Exception,), {}),
)
stub(
    "homeassistant.helpers.update_coordinator",
    CoordinatorEntity=Base,
    DataUpdateCoordinator=Base,
    UpdateFailed=type("U", (Exception,), {}),
)
stub("homeassistant.helpers.entity_platform", AddConfigEntryEntitiesCallback=object)
stub(
    "homeassistant.helpers.entity",
    pkg=True,
    Entity=Base,
    EntityDescription=Base,
    EntityCategory=enum(CONFIG="config", DIAGNOSTIC="diagnostic"),
)
stub("homeassistant.helpers.device_registry", DeviceInfo=dict, format_mac=lambda x: x)
stub("homeassistant.helpers.storage", Store=Base)
stub("homeassistant.helpers.recorder", get_instance=lambda hass: None)
stub("homeassistant.helpers.debounce", Debouncer=Base)
stub(
    "homeassistant.helpers.aiohttp_client",
    async_get_clientsession=lambda *a, **k: None,
    async_create_clientsession=lambda *a, **k: None,
)
stub("homeassistant.helpers.typing", ConfigType=dict)
stub(
    "homeassistant.helpers.event",
    async_track_time_interval=lambda *a, **k: (lambda: None),
    async_track_time_change=lambda *a, **k: (lambda: None),
    async_call_later=lambda *a, **k: (lambda: None),
)
stub("homeassistant.loader", async_get_integration=lambda *a, **k: None)
stub(
    "homeassistant.helpers.issue_registry",
    async_create_issue=lambda *a, **k: None,
    async_delete_issue=lambda *a, **k: None,
    IssueSeverity=enum(WARNING="warning", ERROR="error"),
)
stub("homeassistant.helpers.selector", TextSelector=Base, SelectSelector=Base)
stub(
    "homeassistant.components.switch",
    pkg=True,
    SwitchEntity=Base,
    SwitchDeviceClass=enum(SWITCH="switch", OUTLET="outlet"),
)
stub(
    "homeassistant.components.binary_sensor",
    pkg=True,
    BinarySensorEntity=Base,
    BinarySensorDeviceClass=enum(
        HEAT="heat", PROBLEM="problem", LOCK="lock", RUNNING="running"
    ),
)
stub("homeassistant.components.select", pkg=True, SelectEntity=Base)
stub(
    "homeassistant.components.sensor",
    pkg=True,
    SensorEntity=Base,
    SensorDeviceClass=enum(
        ENUM="enum",
        GAS="gas",
        TEMPERATURE="temperature",
        HUMIDITY="humidity",
        CO2="carbon_dioxide",
        PM25="pm25",
        PM10="pm10",
        PM1="pm1",
    ),
    SensorStateClass=enum(MEASUREMENT="measurement", TOTAL="total"),
)
stub("homeassistant.components.recorder", pkg=True, get_instance=lambda hass: None)
stub("homeassistant.components.recorder.models", pkg=True, StatisticData=dict, StatisticMetaData=dict)
stub(
    "homeassistant.components.recorder.models.statistics",
    StatisticMeanType=enum(NONE=0, ARITHMETIC=1, CIRCULAR=2),
)
stub(
    "homeassistant.components.recorder.statistics",
    async_add_external_statistics=lambda *a, **kw: None,
    statistics_during_period=lambda *a, **kw: {},
)
stub(
    "homeassistant.components.number",
    pkg=True,
    NumberEntity=Base,
    NumberMode=enum(BOX="box", SLIDER="slider", AUTO="auto"),
    NumberDeviceClass=enum(HUMIDITY="humidity", TEMPERATURE="temperature"),
)
stub(
    "homeassistant.components.climate",
    pkg=True,
    ClimateEntity=Base,
)
stub(
    "homeassistant.components.climate.const",
    ClimateEntityFeature=enum(TARGET_TEMPERATURE=1, TURN_ON=2, TURN_OFF=4),
    HVACMode=enum(HEAT="heat", OFF="off", COOL="cool"),
    HVACAction=enum(HEATING="heating", IDLE="idle", OFF="off", COOLING="cooling"),
)
stub("homeassistant.components.diagnostics", pkg=True, async_redact_data=lambda d, k: d)
stub("voluptuous", Schema=Base, Required=Base, Optional=Base)
stub(
    "aiohttp",
    ClientSession=type("C", (), {}),
    ClientError=type("E", (Exception,), {}),
    ClientTimeout=type("T", (), {}),
    ClientResponseError=type("R", (Exception,), {}),
)
