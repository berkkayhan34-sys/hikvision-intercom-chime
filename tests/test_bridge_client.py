"""The HTTP client maps every failure to a fixed translation key, never raw content."""

import aiohttp
import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.hikvision_intercom.bridge_client import BridgeClient, BridgeError

from .conftest import APP_HOST, STATION_NAME, STATION_URL, TOKEN


@pytest.fixture
async def client(hass, aioclient_mock):
    return BridgeClient(async_get_clientsession(hass), APP_HOST, TOKEN)


async def test_status_sends_the_bearer_token(client, aioclient_mock):
    aioclient_mock.get(STATION_URL, json={"connected": True})
    assert await client.status(STATION_NAME) == {"connected": True}
    assert aioclient_mock.mock_calls[0][3]["Authorization"] == f"Bearer {TOKEN}"


async def test_broadcast_posts_raw_pcm_and_returns_the_duration(client, aioclient_mock):
    aioclient_mock.post(f"{STATION_URL}/broadcast", status=202, json={"duration": 1.5})
    assert await client.broadcast(STATION_NAME, b"\x00\x01" * 10) == 1.5
    _, _, body, headers = aioclient_mock.mock_calls[0]
    assert (
        body == b"\x00\x01" * 10
        and headers["Content-Type"] == "application/octet-stream"
    )


@pytest.mark.parametrize(
    "status, payload, code",
    [
        (401, {}, "app_unauthorized"),
        (404, {}, "station_unknown"),
        (503, {}, "station_offline"),
        (400, {}, "invalid_audio"),
        (413, {}, "invalid_audio"),
        (500, {}, "app_error"),
        (409, {"error": "call_active"}, "call_active"),
        (409, {"error": "busy"}, "busy"),
        (409, {"error": "<script>"}, "app_error"),
        (409, [], "app_error"),
        (418, {"error": "x"}, "app_error"),
    ],
)
async def test_http_failures_map_to_fixed_codes(
    client, aioclient_mock, status, payload, code
):
    aioclient_mock.get(STATION_URL, status=status, json=payload)
    with pytest.raises(BridgeError) as error:
        await client.status(STATION_NAME)
    assert error.value.code == code
    assert TOKEN not in str(error.value) and APP_HOST not in str(error.value)


@pytest.mark.parametrize(
    "failure", [aiohttp.ClientError(), TimeoutError(), aiohttp.ClientConnectionError()]
)
async def test_transport_failures_are_unreachable(client, aioclient_mock, failure):
    aioclient_mock.get(STATION_URL, exc=failure)
    with pytest.raises(BridgeError) as error:
        await client.status(STATION_NAME)
    assert error.value.code == "app_unreachable"


async def test_non_json_reply_is_unreachable_not_a_crash(client, aioclient_mock):
    aioclient_mock.get(STATION_URL, text="<html>proxy error</html>")
    with pytest.raises(BridgeError) as error:
        await client.status(STATION_NAME)
    assert error.value.code == "app_unreachable"
