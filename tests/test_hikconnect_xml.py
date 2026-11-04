"""The station's answers are XML that arrives over the network: it is read without accepting a DTD or an entity."""

import pytest

from custom_components.hikvision_intercom.hikconnect_api import HikConnectClient, HikConnectError

# A number through an entity: the standard library would read it as 30, so an answer of {} proves the entity was refused.
INTERNAL_ENTITY = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY n "30">]><OperationTime><ring>&n;</ring></OperationTime>'
EXTERNAL = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY secret SYSTEM "file:///etc/passwd">]><OperationTime><ring>&secret;</ring></OperationTime>'


@pytest.fixture
def client(monkeypatch):
    client = HikConnectClient("account", "password")
    answer = {}
    monkeypatch.setattr(client, "isapi", lambda serial, method, path, body="": {"data": answer["data"]})
    client.answer = answer
    return client


def test_a_normal_answer_is_read(client):
    client.answer["data"] = "<OperationTime><ring>30</ring><call>45</call></OperationTime>"
    assert client.get_operation_time("S1") == {"ring": 30, "call": 45}


@pytest.mark.parametrize("hostile", [INTERNAL_ENTITY, EXTERNAL])
def test_an_answer_with_a_dtd_or_entities_is_refused_and_reads_as_nothing(client, hostile):
    client.answer["data"] = hostile
    assert client.get_operation_time("S1") == {}


def test_a_broken_answer_reads_as_nothing(client):
    client.answer["data"] = "<OperationTime><ring>"
    assert client.get_operation_time("S1") == {}


def test_the_video_key_is_found_and_a_masked_one_is_not_taken(client):
    client.answer["data"] = "<EZVIZ><verificationCode>ABCDEF</verificationCode></EZVIZ>"
    assert client.get_video_key("S1") == "ABCDEF"
    client.answer["data"] = "<EZVIZ><verificationCode>******</verificationCode></EZVIZ>"
    with pytest.raises(HikConnectError):
        client.get_video_key("S1")


def test_a_hostile_answer_never_yields_a_video_key(client):
    client.answer["data"] = EXTERNAL.replace("OperationTime", "EZVIZ").replace("<ring>", "<verificationCode>").replace("</ring>", "</verificationCode>")
    with pytest.raises(Exception) as error:  # noqa: B017 - what matters is that it does not return the entity's value
        client.get_video_key("S1")
    assert "root:" not in str(error.value)
