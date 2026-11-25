"""Raw field sensors."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.pecron.raw_entities import RawFieldSensor, flatten, raw_sensor_tracker


def test_flatten_structs_and_arrays() -> None:
    assert flatten({"a": {"b": 1}, "c": [2, {"d": 3}], "e": True}) == {
        "a.b": 1,
        "c.0": 2,
        "c.1.d": 3,
        "e": True,
    }


def test_tracker_adds_each_field_once_and_reads_numbers() -> None:
    device = SimpleNamespace(device_name="F5000LFP_B639")
    coordinator = MagicMock()
    coordinator.data = {
        "DK": {
            "device": device,
            "values": {"id_66": {"field_67": 0}, "ac_data_input_hm": {"ac_input_voltage": "117.2"}},
        }
    }
    coordinator.links = {}
    added: list = []
    track = raw_sensor_tracker(coordinator, added.extend)
    track()
    track()
    assert sorted(e._path for e in added) == ["ac_data_input_hm.ac_input_voltage", "id_66.field_67"]
    volt = next(e for e in added if e._path.endswith("voltage"))
    assert isinstance(volt, RawFieldSensor)
    assert volt.native_value == 117.2
    coordinator.data["DK"]["values"]["ac_data_input_hm"]["ac_input_voltage"] = "118"
    assert volt.native_value == 118
