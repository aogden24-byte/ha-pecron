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


def _key_api(get_ok: bool):
    from unofficial_pecron_api.exceptions import PecronAPIError

    api = MagicMock()

    def request(method, path, **kw):
        if path.endswith("getAuthKey"):
            if get_ok:
                return {"authKey": "readkey"}
            raise PecronAPIError("not supported", code=5000)
        if path.endswith("regenerateAuthKey"):
            return {"authKey": "newkey"}
        raise AssertionError(path)

    api._request.side_effect = request
    return api


def test_auth_key_read_only_by_default() -> None:
    from unofficial_pecron_api.exceptions import PecronAPIError

    account = _account()
    account.api = _key_api(get_ok=False)
    with pytest.raises(PecronAPIError):
        account._auth_key("pk", "dk")
    paths = [c.args[1] for c in account.api._request.call_args_list]
    assert not any("regenerate" in p for p in paths)


def test_auth_key_prefers_get() -> None:
    account = _account()
    account.api = _key_api(get_ok=True)
    assert account._auth_key("pk", "dk", allow_regenerate=True) == "readkey"


def test_auth_key_regenerates_only_when_allowed() -> None:
    account = _account()
    account.api = _key_api(get_ok=False)
    assert account._auth_key("pk", "dk", allow_regenerate=True) == "newkey"
