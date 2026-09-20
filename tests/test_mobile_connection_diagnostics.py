"""连接诊断保留原协议回复，不输出配对或命令正文。"""
import logging
import pytest
from tests.test_akashic_clients_mobile_follow_scope import gateway, connected


@pytest.mark.parametrize('payload', [{'private_text':'must-not-be-logged'}, {'message_log_version':1}])
def test_sync_rejection_logs_code_without_payload(gateway,caplog,payload):
    caplog.set_level(logging.INFO)
    with connected(gateway) as (ws,epoch):
        ws.send_json({'v':1,'kind':'command','type':'session.list',
            'id':'01ARZ3NDEKTSV4RRFFQ69G5FAV','connection_epoch':epoch,'payload':payload})
        reply=ws.receive_json()
        assert reply['type']=='session.list.error'
        assert reply['payload']['code']=='unsupported_message_log_version'
    assert 'mobile.auth.accepted' in caplog.text
    assert 'mobile.sync.resume' in caplog.text
    assert 'mobile.command.rejected' in caplog.text
    assert 'code=unsupported_message_log_version' in caplog.text
    assert 'must-not-be-logged' not in caplog.text
    assert gateway[3] not in caplog.text


def test_current_sync_protocol_keeps_successful_reply(gateway,caplog):
    caplog.set_level(logging.INFO)
    with connected(gateway) as (ws,epoch):
        ws.send_json({'v':1,'kind':'command','type':'session.list',
            'id':'01ARZ3NDEKTSV4RRFFQ69G5FAV','connection_epoch':epoch,
            'payload':{'message_log_version':2,'page_size':200}})
        event=ws.receive_json();reply=ws.receive_json()
        assert event['type']=='session.list'
        assert reply['type']=='session.list.ok'
    assert 'type=session.list message_log_version=2' in caplog.text
    assert 'mobile.command.rejected' not in caplog.text
