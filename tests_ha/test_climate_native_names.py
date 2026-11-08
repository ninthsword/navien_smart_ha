"""The thermostat gives Home Assistant both the old and the native temperature names.

Home Assistant 2026.11 reads the native names and deprecates the old ones; 2026.8 to 2026.10
read only the old ones. The native names exist on the entity at runtime only, so they are
read through getattr.
"""

from __future__ import annotations

from homeassistant.components.climate import ClimateEntity
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_component import EntityComponent

from .conftest import entity_id_for_unique_id


async def test_native_names_match_old_names(hass: HomeAssistant, loaded_entry) -> None:
    # The climate component's key; its exported name differs between 2026.8 and 2026.11.
    component: EntityComponent[ClimateEntity] = hass.data["climate"]
    expected = {"left": (34.5, 35.0), "right": (35.5, 36.0)}
    for zone, (current, target) in expected.items():
        entity_id = entity_id_for_unique_id(
            hass, "climate", f"AABBCCDDEEFF_{zone}_thermostat"
        )
        entity = component.get_entity(entity_id)
        assert entity is not None

        assert entity.current_temperature == current
        assert entity.target_temperature == target
        assert entity.temperature_unit == UnitOfTemperature.CELSIUS
        old_values = {
            "native_current_temperature": entity.current_temperature,
            "native_target_temperature": entity.target_temperature,
            "_attr_native_temperature_unit": entity.temperature_unit,
        }
        for native_name, old_value in old_values.items():
            assert getattr(entity, native_name) == old_value, native_name
