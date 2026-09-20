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


def test_snapshot_queries_work_at_receipt_capacity_without_deleting_receipts(gateway,monkeypatch):
    from datetime import datetime,timezone
    from uuid import uuid4
    from session.message import Input,ContentPart
    from tests.test_mobile_message_log import append
    from plugins.akashic_clients.mobile_realtime import storage as storage_module
    log,runtime,_client,device,_private=gateway
    session=f'akashic:{uuid4()}'
    append(log,session,'history-input',Input((ContentPart('text','retained conversation'),)))
    now=datetime.now(timezone.utc)
    receipt,_=runtime.storage.reserve_command(device_id=device,command_id='existing',
        command_type='device.update',request_hash='preserved',created_at=now)
    runtime.storage.complete_command(device_id=device,command_id='existing',reply_type='device.update.ok',
        reply_payload_json='{}',session_id=None,turn_id=None,completed_at=now)
    original=runtime.storage.read_command(device_id=device,command_id='existing')
    monkeypatch.setattr(storage_module,'_COMMAND_RECEIPT_MAX_COUNT',1)
    with connected(gateway) as (ws,epoch):
        ws.send_json({'v':1,'kind':'command','type':'session.list','id':'01ARZ3NDEKTSV4RRFFQ69G5FAV',
            'connection_epoch':epoch,'payload':{'message_log_version':2,'page_size':200}})
        assert ws.receive_json()['type']=='session.list'
        assert ws.receive_json()['type']=='session.list.ok'
        ws.send_json({'v':1,'kind':'command','type':'history.get','id':'01ARZ3NDEKTSV4RRFFQ69G5FAW',
            'connection_epoch':epoch,'session_id':session,'payload':{'message_log_version':2,'page_size':50}})
        event=ws.receive_json();reply=ws.receive_json()
        assert event['type']=='history.page'
        assert reply['type']=='history.get.ok'
        ws.send_json({'v':1,'kind':'command','type':'device.update','id':'01ARZ3NDEKTSV4RRFFQ69G5FAX',
            'connection_epoch':epoch,'payload':{'capabilities':['chat']}})
        assert ws.receive_json()['payload']['code']=='mobile_command_receipt_capacity_reached'
    assert runtime.storage.read_command(device_id=device,command_id='existing')==original
    assert runtime.storage.read_command(device_id=device,command_id='01ARZ3NDEKTSV4RRFFQ69G5FAV') is None
    assert runtime.storage.read_command(device_id=device,command_id='01ARZ3NDEKTSV4RRFFQ69G5FAW') is None
    assert log.reader(session).get('history-input') is not None


@pytest.mark.parametrize('static_active', [None,False])
def test_incompatible_optional_inspection_returns_error_and_keeps_socket(gateway,static_active):
    from copy import deepcopy
    from types import SimpleNamespace
    _log,runtime,_client,_device,_private=gateway
    fiber={'name':'plugin','parent':None,'state':'active','required':True,'dependencies':[],
           'missing_services':[],'error':None}
    if static_active is not None:fiber['static_active']=static_active
    payload={'snapshot_id':'fixed','plugins':[{'id':'plugin','revision':'r','generation_id':'g',
        'api_version':3,'composition':{'ready':True,'topology_identity':'t','composition_revision':1,
        'fibers':[fiber],'health':[],'incident_count':0,'recent_incidents':[],'incident_overflowed':False}}],
        'mcp_servers':[],'skills':[]}
    before=deepcopy(payload)
    async def capabilities():return payload
    runtime.channel.bind_runtime_inspection(SimpleNamespace(list_capabilities=capabilities))
    with connected(gateway) as (ws,epoch):
        ws.send_json({'v':1,'kind':'command','type':'runtime.capability.list',
            'id':'01ARZ3NDEKTSV4RRFFQ69G5FAV','connection_epoch':epoch,'payload':{}})
        reply=ws.receive_json()
        if static_active is None:
            assert reply['type']=='runtime.capability.list.error'
            assert reply['payload']['code']=='runtime_catalog_client_incompatible'
        else:
            assert reply['type']=='runtime.capability.list.ok'
            assert reply['payload']==payload
        ws.send_json({'v':1,'kind':'command','type':'ping',
            'id':'01ARZ3NDEKTSV4RRFFQ69G5FAW','connection_epoch':epoch,'payload':{}})
        assert ws.receive_json()['type']=='ping.ok'
    assert payload==before
