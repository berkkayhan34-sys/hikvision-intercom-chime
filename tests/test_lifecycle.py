"""Removal, diagnostics and resilience when parts of the setup are broken."""

import json
from pathlib import Path

from homeassistant.config_entries import ConfigEntryState, ConfigSubentry
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.hikvision_intercom.const import DOMAIN
from custom_components.hikvision_intercom.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.hikvision_intercom.hikconnect_api import HikConnectAuthError

from .conftest import PASSWORD, SERIAL, STATION_NAME, TOKEN
from .test_chime import ONLINE, PLAY, STATUS, mock_bridge, setup


def audio_dir(hass, subentry_id):
    return Path(hass.config.path(".storage", DOMAIN)) / subentry_id


async def test_removing_a_station_drops_its_entities_audio_and_app_credentials(
    hass, cloud, supervisor, entry, aioclient_mock
):
    mock_bridge(aioclient_mock)
    await setup(hass, entry)
    subentry_id = next(iter(entry.subentries))
    assert audio_dir(hass, subentry_id).is_dir()
    (audio_dir(hass, subentry_id) / ("a" * 64 + ".wav")).write_bytes(b"owned")
    (audio_dir(hass, subentry_id) / "user-note.txt").write_text("keep")

    assert hass.config_entries.async_remove_subentry(entry, subentry_id)
    await hass.async_block_till_done()

    assert supervisor.removed == [STATION_NAME]
    assert hass.states.get(PLAY) is None and hass.states.get(STATUS) is None
    assert (
        hass.states.get("button.ds_kh6320_wte1_answer_call") is not None
    )  # cloud side stays
    assert not (audio_dir(hass, subentry_id) / ("a" * 64 + ".wav")).exists()
    assert (audio_dir(hass, subentry_id) / "user-note.txt").read_text() == "keep"
    assert entry.state is ConfigEntryState.LOADED


async def test_removing_the_entry_removes_every_station_from_the_app(
    hass, cloud, supervisor, entry, aioclient_mock
):
    mock_bridge(aioclient_mock)
    await setup(hass, entry)
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert supervisor.removed == [STATION_NAME]


async def test_editing_a_station_sound_does_not_reload_the_entry(
    hass, cloud, supervisor, entry, aioclient_mock
):
    mock_bridge(aioclient_mock)
    await setup(hass, entry)
    runtime = hass.data[DOMAIN][entry.entry_id]["chimes"][next(iter(entry.subentries))]
    hass.config_entries.async_update_subentry(
        entry,
        next(iter(entry.subentries.values())),
        data={**next(iter(entry.subentries.values())).data, "repeats": 3},
    )
    await hass.async_block_till_done()
    assert (
        hass.data[DOMAIN][entry.entry_id]["chimes"][next(iter(entry.subentries))]
        is runtime
    )


async def test_diagnostics_contain_no_address_credentials_or_token(
    hass, cloud, supervisor, entry, aioclient_mock
):
    mock_bridge(aioclient_mock)
    await setup(hass, entry)
    report = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(report, default=str)
    for secret in (
        "192.168.0.50",
        PASSWORD,
        TOKEN,
        "cloud-password",
        "owner@example.test",
        STATION_NAME,
    ):
        assert secret not in text, secret
    (chime,) = report["chimes"]
    assert chime["station_connected"] is True and chime["request_status"] == "idle"
    assert chime["repeats"] == 4 and chime["custom_audio"] is False


async def test_a_broken_audio_file_does_not_break_the_cloud_features(
    hass, cloud, supervisor, entry, aioclient_mock
):
    subentry = next(iter(entry.subentries.values()))
    entry_data = dict(subentry.data, source_digest="f" * 64)
    mock_bridge(aioclient_mock)
    hass.config_entries.async_update_subentry(entry, subentry, data=entry_data)
    await setup(hass, entry)
    state = hass.states.get(STATUS)
    assert (
        state.state == "error"
        and state.attributes["last_error"] == "storage_unavailable"
    )
    assert hass.states.get("button.ds_kh6320_wte1_answer_call") is not None


async def test_unusable_audio_storage_skips_only_the_chime(
    hass, cloud, supervisor, entry, aioclient_mock, tmp_path
):
    subentry_id = next(iter(entry.subentries))
    root = Path(hass.config.path(".storage", DOMAIN))
    root.mkdir(parents=True, exist_ok=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / subentry_id).symlink_to(
        elsewhere, target_is_directory=True
    )  # a symlinked store is refused
    mock_bridge(aioclient_mock)
    await setup(hass, entry)
    assert hass.states.get(PLAY) is None
    assert hass.states.get("button.ds_kh6320_wte1_answer_call") is not None
    assert list(elsewhere.iterdir()) == []  # nothing was written through the link


async def test_cloud_auth_failure_still_reports_the_entry_as_failed(
    hass, cloud, supervisor, entry, aioclient_mock
):
    cloud.login_error = HikConnectAuthError("bad credentials")
    mock_bridge(aioclient_mock)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_entity_unique_ids_are_per_station_and_stable(
    hass, cloud, supervisor, entry, aioclient_mock
):
    mock_bridge(aioclient_mock)
    await setup(hass, entry)
    subentry_id = next(iter(entry.subentries))
    registry = er.async_get(hass)
    assert registry.async_get(PLAY).unique_id == f"{subentry_id}_play"
    # The entry owns the entities so the shared Doorbell device is not also listed under the station.
    assert registry.async_get(PLAY).config_subentry_id is None


async def test_an_earlier_separate_chime_device_moves_into_the_doorbell_keeping_entity_ids(
    hass, cloud, supervisor, entry, aioclient_mock
):
    """What the box looks like after the previous version: a device of its own, and
    entities attached to the station subentry."""
    subentry_id = next(iter(entry.subentries))
    devices, registry = dr.async_get(hass), er.async_get(hass)
    old = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        config_subentry_id=subentry_id,
        identifiers={(DOMAIN, f"station_{subentry_id}")},
        name="Doorbell chime",
    )
    seeded = {}
    for domain, key in (
        ("button", "play"),
        ("button", "stop"),
        ("number", "gain_db"),
        ("number", "repeats"),
        ("sensor", "status"),
    ):
        seeded[key] = registry.async_get_or_create(
            domain,
            DOMAIN,
            f"{subentry_id}_{key}",
            suggested_object_id=f"doorbell_chime_{key}",
            config_entry=entry,
            config_subentry_id=subentry_id,
            device_id=old.id,
        ).entity_id
    mock_bridge(aioclient_mock)
    await setup(hass, entry)

    assert {
        registry.async_get(entity_id).entity_id for entity_id in seeded.values()
    } == set(seeded.values())
    shared = devices.async_get_device_by_identifier((DOMAIN, SERIAL), entry.entry_id)
    assert shared is not None
    for entity_id in seeded.values():
        assert registry.async_get(entity_id).device_id == shared.id
        assert registry.async_get(entity_id).config_subentry_id is None
    assert (
        devices.async_get_device_by_identifier(
            (DOMAIN, f"station_{subentry_id}"), entry.entry_id
        )
        is None
    )
    assert len(dr.async_entries_for_config_entry(devices, entry.entry_id)) == 1


async def test_two_stations_keep_separate_devices(
    hass, cloud, supervisor, entry, aioclient_mock
):
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            subentry_type="station",
            title="Hall chime",
            unique_id="192.168.0.60:8000",
            data={
                "name": "st_192_168_0_60",
                "host": "192.168.0.60",
                "port": 8000,
                "username": "admin",
                "gain_db": 0,
                "repeats": 4,
            },
        ),
    )
    aioclient_mock.get(
        "http://a1b2c3d4-hikvision-sdk:8099/v1/stations/st_192_168_0_60", json=ONLINE
    )
    mock_bridge(aioclient_mock)
    await setup(hass, entry)
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 3  # the doorbell and one device per station
