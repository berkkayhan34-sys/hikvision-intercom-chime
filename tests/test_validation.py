import pytest

from custom_components.hikvision_intercom.validation import (
    SetupError,
    merge_station,
    station_name,
    validate_station,
    without_station,
)

STATION = {
    "name": "st_192_168_0_50",
    "host": "192.168.0.50",
    "port": 8000,
    "username": "admin",
    "password": "pw",
}


@pytest.mark.parametrize(
    "host", ["192.168.0.50", "10.1.2.3", "172.16.5.5", "172.31.255.254"]
)
def test_private_ipv4_is_accepted(host):
    validate_station(host, 8000, "admin", "pw")


@pytest.mark.parametrize(
    "host",
    [
        "8.8.8.8",
        "172.32.0.1",
        "127.0.0.1",
        "169.254.1.1",
        "224.0.0.1",
        "example.test",
        "http://192.168.0.1",
        "192.168.0.1:80",
        "",
        None,
        "::1",
    ],
)
def test_other_hosts_are_refused(host):
    with pytest.raises(SetupError) as error:
        validate_station(host, 8000, "admin", "pw")
    assert str(error.value) == "invalid_host"


@pytest.mark.parametrize("port", [0, -1, 65536, True, 80.5, "8000", None])
def test_invalid_ports_are_refused(port):
    with pytest.raises(SetupError) as error:
        validate_station("192.168.0.5", port, "admin", "pw")
    assert str(error.value) == "invalid_port"


@pytest.mark.parametrize(
    "user, password",
    [
        ("", "pw"),
        ("admin", ""),
        ("a" * 65, "pw"),
        ("admin", "é" * 33),
        ("admin", "a\x00b"),
        (None, "pw"),
        ("admin", 5),
    ],
)
def test_invalid_credentials_are_refused_without_echoing_them(user, password):
    with pytest.raises(SetupError) as error:
        validate_station("192.168.0.5", 8000, user, password)
    assert str(error.value) == "invalid_credentials"


def test_station_name_is_derived_from_the_address_and_is_a_valid_app_identifier():
    assert station_name("192.168.0.50") == "st_192_168_0_50"


def test_merge_adds_a_station_and_generates_a_token():
    merged = merge_station({}, STATION)
    assert merged["stations"] == [STATION]
    assert 32 <= len(merged["token"]) <= 128


def test_merge_never_overwrites_a_valid_token_but_replaces_a_malformed_one():
    keep = "k" * 40
    assert merge_station({"token": keep}, STATION)["token"] == keep
    assert merge_station({"token": "short"}, STATION)["token"] != "short"
    assert merge_station({"token": 5}, STATION)["token"] != 5


def test_merge_preserves_other_stations_and_unknown_options():
    other = {
        "name": "other",
        "host": "192.168.0.9",
        "port": 8000,
        "username": "u",
        "password": "p",
    }
    merged = merge_station(
        {"token": "k" * 40, "stations": [other], "extra": 1}, STATION
    )
    assert merged["stations"] == [other, STATION] and merged["extra"] == 1


def test_merge_updates_the_same_name_in_place_and_does_not_mutate_the_input():
    options = {"token": "k" * 40, "stations": [dict(STATION, password="old")]}
    merged = merge_station(options, STATION)
    assert merged["stations"] == [STATION]
    assert options["stations"][0]["password"] == "old"


def test_merge_refuses_an_address_already_used_by_another_name():
    other = dict(STATION, name="other")
    with pytest.raises(SetupError) as error:
        merge_station({"token": "k" * 40, "stations": [other]}, STATION)
    assert str(error.value) == "duplicate_station"


def test_without_station_removes_only_that_station():
    other = dict(STATION, name="other", host="192.168.0.9")
    result = without_station(
        {"token": "k" * 40, "stations": [STATION, other]}, STATION["name"]
    )
    assert result["stations"] == [other] and result["token"] == "k" * 40
