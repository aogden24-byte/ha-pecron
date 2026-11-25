# Pecron Home Assistant Integration

A Home Assistant community integration for Pecron portable power stations. Monitor and control your devices with real-time data and automation capabilities.

## Features

- **Device Control** - Turn AC and DC outputs on/off directly from Home Assistant
- **Real-time Monitoring** - Battery percentage, voltage, current, temperature, input/output power, and device status
- **Multi-device Support** - Manage multiple Pecron stations from one account
- **Smart Entity Discovery** - Automatically creates only the entities your device supports
- **Advanced Control Service** - `pecron.set_property` for controlling any writable device property
- **Local first** - Bluetooth and Wi-Fi with automatic failover to the cloud, in the order you choose
- **Configurable Poll Rates** - Local (from 5 s) and cloud (from 60 s) set separately
- **Multi-region Support** - US, EU, and CN regions
- **Automation Ready** - Create complex automations based on battery level, power usage, etc.

## How It Works

Each station is reached over up to three connections, in an order you choose:

| Connection | Needs | Notes |
|---|---|---|
| **Bluetooth** | A Bluetooth adapter or an ESPHome Bluetooth proxy in range | Fully local. Holds the station's single BLE session, so the Pecron app falls back to Wi-Fi/cloud while it is in use. |
| **Wi-Fi** (TCP port 6607) | Station and Home Assistant on the same network | Fully local and fast. Found automatically by scanning the subnet, or set a fixed IP. |
| **Cloud** | Internet | Same backend as the Pecron app. Polled slowly: the account has a request quota. |

The integration uses the highest-priority connection that works. A connection
that fails is skipped for that poll (data keeps flowing from the next one);
after 3 consecutive failures it is marked down and re-tried with increasing
back-off (1 → 15 min), then promoted back automatically when it answers. Any
connection can be set to **Not used**. Every connection's health is exposed as
a diagnostic entity, plus a **Connection** sensor showing which one is live.

**Cloud login is needed once.** Local connections need each station's data
model and local key, which only Pecron's cloud provides. They are cached in the
config entry, so after setup Bluetooth and Wi-Fi keep working with no internet.
The integration only ever *reads* the local key; it never regenerates it
(regenerating breaks every other local tool using the station).

**One client per connection.** A station accepts one Wi-Fi TCP client and one
BLE client at a time. Do not run other local tools (pecron-monitor,
other Pecron integrations) against the same station.

**Regional endpoints:**
- **US** (default): `iot-api.landecia.com`
- **EU**: `iot-api.acceleronix.io`
- **CN**: `iot-api.quectelcn.com`

## Installation

### Via HACS

1. Go to **HACS** → **Integrations** → **Custom repositories**
2. Add this repository: `https://github.com/aogden24-byte/ha-pecron`
3. Select **Pecron** and click **Install**
4. Restart Home Assistant
5. Go to **Settings** → **Devices & Services** → **Create Integration**
6. Search for **Pecron**

### Manual

```bash
git clone https://github.com/aogden24-byte/ha-pecron.git
cp -r ha-pecron/custom_components/pecron ~/.homeassistant/custom_components/
# Restart Home Assistant
```

## Configuration

Add via the UI:
1. **Settings** → **Devices & Services** → **Create Integration**
2. Select **Pecron**
3. Enter:
   - Email
   - Password
   - Region (US, EU, or CN)
   - (Optional) Custom refresh interval

## Entities

The integration creates the following entities for each device:

### Switches
- **AC Output** - Control AC power output (on/off)
- **DC Output** - Control DC power output (on/off)

### Selects
- **AC Charge Speed** - Control AC charging power level (0%, 25%, 50%, 75%, 100%)

### Sensors
- **Battery Percentage** - Current battery level (%)
- **Battery Voltage** - Current battery pack voltage (V)
- **Battery Current** - Signed battery pack charge/discharge current (A)
- **Battery Power** - Current battery power (W), positive while charging and negative while discharging
- **Battery Temperature** - Current battery pack temperature (°C)
- **Input Power** - Total power being drawn from all input sources (W)
- **AC Input Power** - Power from grid/AC charging (W)
- **DC Input Power** - Power from solar/DC input (W)
- **Output Power** - Total power being output (W)
- **Time to Full** - Estimated time until battery is fully charged (minutes)
- **Time to Empty** - Estimated time until battery is depleted (minutes)

### Binary Sensors
- **UPS Mode** - Whether UPS mode is active
- **Online** - Device connectivity status

*Note: Actual entities vary by device model. The integration uses TSL (Thing Specification Language) to discover and create only the entities your specific device supports.*

## Services

### `pecron.set_property`

Advanced service for controlling any writable device property.

**Parameters:**
- `device_id` (required) - The Pecron device to control
- `property_code` (required) - The property code to set (e.g., `ac_switch`, `dc_switch`)
- `value` (required) - The value to set (type depends on property)

**Example:**
```yaml
service: pecron.set_property
data:
  device_id: 1234567890abcdef
  property_code: "ac_switch"
  value: true
```

**Automation Example:**
```yaml
automation:
  - alias: "Turn off AC when battery low"
    trigger:
      - platform: numeric_state
        entity_id: sensor.pecron_battery_percentage
        below: 20
    action:
      - service: switch.turn_off
        target:
          entity_id: switch.pecron_ac_output
```

## Requirements

- Home Assistant 2024.1 or later
- Python 3.11+

## Issues & Support

For bugs, feature requests, or questions, please open an issue on GitHub.

## Disclaimer

This integration is not affiliated with or endorsed by Pecron. It uses the unofficial API which was reverse-engineered from the Pecron Android app. Use at your own risk.

## License

MIT

## Credits

- Cloud API: [unofficial-pecron-api](https://github.com/jsightler/unofficial-pecron-api) (MIT) and the original integration by Jesse Sightler.
- Local protocol (TTLV framing, handshake, encryption): documented by
  [pecron-monitor](https://github.com/attractify-logan/pecron-monitor) (MIT).
