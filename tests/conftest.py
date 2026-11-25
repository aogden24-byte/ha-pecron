"""Pytest configuration and fixtures."""

import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.fixture
def mock_hass() -> AsyncMock:
    """Create a mock Home Assistant instance."""
    hass = AsyncMock()
    hass.data = {}
    hass.config_entries = AsyncMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda func, *args: func(*args))
    return hass


@pytest.fixture
def mock_config_entry() -> MagicMock:
    """Create a mock config entry."""
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.data = {
        "email": "test@example.com",
        "password": "test_password",
        "region": "US",
    }
    return entry


@pytest.fixture
def mock_device() -> MagicMock:
    """Create a mock Pecron device."""
    device = MagicMock()
    device.device_key = "test_device_key"
    device.device_name = "Test Device"
    return device


@pytest.fixture(autouse=True)
def _no_mqtt_network(monkeypatch):
    """Never let the cloud push stream reach the real broker in tests."""
    from custom_components.pecron.transport import cloud_stream

    monkeypatch.setattr(cloud_stream.CloudStream, "start", lambda self: None)
    monkeypatch.setattr(cloud_stream.CloudStream, "stop", lambda self: None)
