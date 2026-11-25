"""Token-refresh behaviour of the cloud account (replaces the old coordinator tests)."""

from unittest.mock import MagicMock, patch

import pytest

from custom_components.pecron.transport.cloud import CloudAccount, is_auth_error


@pytest.mark.parametrize(
    "message",
    [
        "Error 5032",
        "token expired",
        "Authentication failed",
        "HTTP 401",
        "Unauthorized",
        "TOKEN EXPIRED",
        "AuThEnTiCaTiOn failed",
    ],
)
def test_auth_errors_detected(message: str) -> None:
    assert is_auth_error(Exception(message))


@pytest.mark.parametrize("message", ["Connection timeout", "Something went wrong", "500 server"])
def test_non_auth_errors_not_detected(message: str) -> None:
    assert not is_auth_error(Exception(message))


def _account() -> CloudAccount:
    return CloudAccount(MagicMock(), "a@b.c", "pw", "US")


def test_auth_error_triggers_relogin_and_retry() -> None:
    account = _account()
    first, second = MagicMock(), MagicMock()
    first.get_device_properties.side_effect = Exception("5032 token invalid")
    second.get_device_properties.return_value = "props"
    second.get_devices.return_value = []
    account.api = first
    with patch("custom_components.pecron.transport.cloud.PecronAPI", return_value=second):
        assert account._run("get_device_properties", "dev") == "props"
    assert account.api is second
    second.login.assert_called_once_with("a@b.c", "pw")


def test_auth_error_twice_raises() -> None:
    account = _account()
    api = MagicMock()
    api.get_device_properties.side_effect = Exception("401 Unauthorized")
    api.get_devices.return_value = []
    account.api = api
    with patch("custom_components.pecron.transport.cloud.PecronAPI", return_value=api):
        with pytest.raises(Exception, match="401"):
            account._run("get_device_properties", "dev")


def test_non_auth_error_keeps_session() -> None:
    account = _account()
    api = MagicMock()
    api.get_device_properties.side_effect = Exception("Connection timeout")
    account.api = api
    with pytest.raises(Exception, match="timeout"):
        account._run("get_device_properties", "dev")
    assert account.api is api


def test_auth_key_never_regenerates() -> None:
    account = _account()
    api = MagicMock()
    api._request.return_value = {"authKey": "abc"}
    account.api = api
    assert account._auth_key("pk", "dk") == "abc"
    paths = [c.args[1] for c in api._request.call_args_list]
    assert paths == ["/v2/binding/enduserapi/getAuthKey"]
    assert not any("regenerate" in p for p in paths)
