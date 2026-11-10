import asyncio
import io
import os
import time
from datetime import datetime, timezone, timedelta

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

os.environ.setdefault('ADMIN_TOKEN', 'test_secret_123456789012345678901234567890')
from app import main
from app.bridge import _find_matching_phone

HEADERS={'Authorization':'Bearer test_secret_123456789012345678901234567890'}

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(main,'DATA_DIR',tmp_path)
    monkeypatch.setattr(main,'DB',tmp_path/'queue.sqlite3')
    monkeypatch.setattr(main,'BRIDGE',FakeBridge())
    with TestClient(main.app) as c:
        yield c

class FakeBridge:
    def __init__(self):self.sent=[]
    async def request_code(self,phone,password=None):return {'state':'code_requested'}
    async def verify_code(self,code):return {'guid':'u0PersonalMock','phone':'989121234567'}
    async def self_test(self):return {'guid':'u0PersonalMock','connected':True}
    async def send(self,phone,name,text,guid=None,allow_add=False):
        self.sent.append((phone,text,allow_add))
        return {'state':'server_accepted','guid':'u0MockRecipient','message_id':'m987'}

def post(c,url,obj):
    return c.post(url,headers=HEADERS,json=obj)

def test_requires_admin_token(client):
    assert client.get('/api/status').status_code==401
    assert client.get('/api/status',headers=HEADERS).status_code==200


def test_import_csv_and_queue_opt_in(client):
    csv_data='phone,name,consent\n09123456789,First,بله\n+989123456789,Final,بله\n09120000000,Other,خیر\n'.encode('utf-8')
    r=client.post('/api/contacts/import',headers=HEADERS,files={'file':('contacts.csv',csv_data,'text/csv')})
    assert r.status_code==200,r.text
    assert r.json()['imported']==2
    assert r.json()['consenting']==1
    assert client.get('/api/status',headers=HEADERS).json()['total_contacts']==2
    scheduled=datetime.now(timezone.utc).isoformat()
    r=post(client,'/api/queue/build',{'message':'سلام','interval_seconds':120,'start_at':scheduled})
    assert r.status_code==200,r.text
    assert r.json()['queued']==1
    assert post(client,'/api/queue/start',{}).status_code==409  # must authenticate account
    assert post(client,'/api/auth/verify',{'code':'12345'}).status_code==200
    assert post(client,'/api/queue/start',{}).status_code==200
    assert asyncio.run(main.worker_once()) is True
    status=client.get('/api/status',headers=HEADERS).json()['counts']
    assert status['sent']==1
    assert len(main.BRIDGE.sent)==1
    assert main.BRIDGE.sent[0][0]=='989123456789'
    jobs=client.get('/api/jobs',headers=HEADERS).json()['jobs']
    assert jobs[0]['message_id']=='m987'
    r=client.get('/api/jobs/export',headers=HEADERS)
    assert b'm987' in r.content


def test_import_xlsx_and_robust_phone(client):
    wb=Workbook(); ws=wb.active;ws.append(['شماره تماس','نام','رضایت']);ws.append([9121234567,'M','بله']);ws.append(['۰۹۱۲۱۲۳۴۵۶۷','M2','بلی'])
    output=io.BytesIO();wb.save(output)
    r=client.post('/api/contacts/import',headers=HEADERS,
                  files={'file':('data.xlsx',output.getvalue(),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
    assert r.status_code==200,r.text
    assert r.json()['imported']==1
    assert main.normalize_phone('۰۹۱۲۱۲۳۴۵۶۷')=='989121234567'


def test_no_opt_in_no_queue(client):
    r=client.post('/api/contacts/import',headers=HEADERS,files={'file':('only.csv',b'phone,name,consent\n09123456789,X,no','text/csv')})
    assert r.status_code==200
    r=post(client,'/api/queue/build',{'message':'سلام','interval_seconds':60,
            'start_at':datetime.now(timezone.utc).isoformat()})
    assert r.status_code==400


def test_contact_resolution_never_uses_wrong_guid():
    result={'contacts':[{'phone':'989121111111','user_guid':'u0Wrong'},
                        {'phone':'989121234567','user_guid':'u0Correct'}]}
    assert _find_matching_phone(result,'989121234567')=='u0Correct'
    assert _find_matching_phone(result,'989129999999') is None


def test_crash_recovery_manual_review(client):
    with main.connect_db() as db:
        db.execute("INSERT INTO contacts(phone,name,consent,created_at) VALUES ('989121234567','A',1,?)",(int(time.time()),))
        db.execute("INSERT INTO jobs(contact_id,message,due_at,status,updated_at) VALUES (1,'hi',0,'sending',0)")
    main.init_db()
    with main.connect_db() as db:
        assert db.execute('SELECT status FROM jobs').fetchone()[0]=='review'
        assert main._setting(db,'running')=='0'  # no START was issued


def test_unknown_provider_outcome_is_not_marked_sent(client,monkeypatch):
    class Uncertain(FakeBridge):
        async def send(self,**kwargs):raise OSError('server disconnected after submission')
    monkeypatch.setattr(main,'BRIDGE',Uncertain())
    data='phone,name,consent\n09123456789,M,yes\n'.encode()
    assert client.post('/api/contacts/import',headers=HEADERS,files={'file':('a.csv',data,'text/csv')}).status_code==200
    assert post(client,'/api/auth/verify',{'code':'12345'}).status_code==200
    assert post(client,'/api/queue/build',{'message':'x','interval_seconds':60,'start_at':datetime.now(timezone.utc).isoformat()}).status_code==200
    assert post(client,'/api/queue/start',{}).status_code==200
    asyncio.run(main.worker_once())
    assert client.get('/api/status',headers=HEADERS).json()['counts'].get('review')==1
