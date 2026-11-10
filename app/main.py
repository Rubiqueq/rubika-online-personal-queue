"""Persistent single-worker FastAPI queue, only for opt-in Rubika contacts."""
import asyncio
import csv
import io
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, UploadFile, Depends
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from openpyxl import load_workbook

from .bridge import RubikaBridge, RubikaUnavailable

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('DATA_DIR', '/data')).resolve()
DB = DATA_DIR / 'queue.sqlite3'
BRIDGE = RubikaBridge(DATA_DIR)
RUNNER = None

def connect_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA busy_timeout=10000')
    return db

def init_db():
    with connect_db() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS contacts(
            id INTEGER PRIMARY KEY, phone TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            consent INTEGER NOT NULL DEFAULT 0 CHECK(consent IN(0,1)),
            guid TEXT, created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY, contact_id INTEGER NOT NULL, message TEXT NOT NULL,
            due_at INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
            message_id TEXT, guid TEXT, allow_add INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL, FOREIGN KEY(contact_id) REFERENCES contacts(id));
        CREATE INDEX IF NOT EXISTS idx_jobs_status_due ON jobs(status, due_at, id);
        INSERT OR IGNORE INTO settings(key,value) VALUES ('running','0'),('verified','0');
        ''')
        # Crashed/incomplete sends are never blindly replayed.
        db.execute("UPDATE jobs SET status='review',last_error='عملیات ارسال هنگام خاموشی ناتمام بود؛ بررسی دستی لازم است' WHERE status='sending'")
        # Running state is durable: scheduled queue resumes after a process restart.

def _setting(db, key):
    row = db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
    return row['value'] if row else ''

def _set(db, key, value):
    db.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,str(value)))

def auth(authorization: str | None = Header(default=None)):
    expected=os.environ.get('ADMIN_TOKEN','')
    if len(expected)<20:
        raise HTTPException(status_code=503, detail='ADMIN_TOKEN با حداقل ۲۰ کاراکتر روی سرور تنظیم نشده است.')
    actual=authorization.removeprefix('Bearer ').strip() if authorization else ''
    if not secrets.compare_digest(actual,expected):
        raise HTTPException(status_code=401,detail='رمز پنل نادرست است.')

def normalize_phone(raw):
    if raw is None:return None
    x=str(raw).strip().translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩','01234567890123456789'))
    x=''.join(i for i in x if i.isascii() and i.isdigit())
    if x.startswith('0098'):x=x[2:]
    if x.startswith('0') and len(x)==11:x='98'+x[1:]
    if len(x)==10 and x.startswith('9'):x='98'+x
    return x if len(x)==12 and x.startswith('989') else None

def _rows_from_file(name, data):
    if name.lower().endswith('.xlsx'):
        workbook=load_workbook(io.BytesIO(data),read_only=True,data_only=True)
        try:
            values=workbook.active.iter_rows(values_only=True)
            rows=list(values)
        finally:
            workbook.close()
    elif name.lower().endswith('.csv'):
        t=data.decode('utf-8-sig')
        dialect=csv.Sniffer().sniff(t[:4096],delimiters=',;\t') if ',' in t[:4096] or ';' in t[:4096] or '\t' in t[:4096] else csv.excel
        rows=list(csv.reader(io.StringIO(t),dialect))
    else:
        raise HTTPException(400,'فقط CSV یا XLSX پذیرفته می‌شود.')
    if len(rows)>10001:
        raise HTTPException(400,'حداکثر ۱۰۰۰۰ ردیف در هر فایل مجاز است.')
    if not rows:return []
    head=[str(c or '').strip().lower() for c in rows[0]]
    keys={'phone':['phone','mobile','number','شماره','شماره تماس','موبایل','تلفن'],
          'name':['name','first_name','نام','نام مخاطب'],
          'consent':['consent','opt_in','allowed','اجازه','رضایت','تایید']}
    indices={k:next((i for i,h in enumerate(head) if h in v),None) for k,v in keys.items()}
    if indices['phone'] is None:
        raise HTTPException(400,'ستون phone یا شماره تماس در ردیف اول فایل لازم است.')
    out=[]
    for row in rows[1:]:
        def v(key):
            i=indices[key]
            return row[i] if i is not None and i<len(row) else ''
        p=normalize_phone(v('phone'))
        if not p:continue
        c=str(v('consent') or '').strip().lower() in ('1','yes','true','y','بله','بلی','آری','تایید','موافق')
        out.append((p,str(v('name') or 'مخاطب').strip()[:100],int(c)))
    # Deduplicate by phone; explicit last-row consent wins.
    return list({p:(p,n,c) for p,n,c in out}.values())

async def worker_once():
    with connect_db() as db:
        if _setting(db,'running')!='1' or _setting(db,'verified')!='1':return False
        now=int(time.time())
        row=db.execute('''SELECT j.*,c.phone,c.name,c.consent,c.guid AS contact_guid
            FROM jobs j JOIN contacts c ON c.id=j.contact_id
            WHERE j.status='pending' AND j.due_at<=? ORDER BY j.due_at,j.id LIMIT 1''',(now,)).fetchone()
        if not row:return False
        job=dict(row)
        if not job['consent']:
            db.execute("UPDATE jobs SET status='failed',last_error='رضایت مخاطب در فهرست ثبت نشده است' WHERE id=?",(job['id'],))
            return True
        db.execute("UPDATE jobs SET status='sending',attempts=attempts+1,updated_at=? WHERE id=? AND status='pending'",(now,job['id']))
    try:
        result=await BRIDGE.send(phone=job['phone'],name=job['name'],text=job['message'],
                                 guid=job['contact_guid'],allow_add=bool(job['allow_add']))
    except Exception as exc:
        # Provider/network exceptions may occur after submission. Manual review prevents duplicates.
        result={'state':'review','error':f'{type(exc).__name__}: {str(exc)[:350]}'}
        # Stop the queue on an ambiguous provider failure so other recipients aren't affected.
        with connect_db() as db:
            _set(db,'running','0')
    state=result.get('state','review')
    status={'server_accepted':'sent','unresolved':'unresolved','review':'review'}.get(state,'review')
    with connect_db() as db:
        db.execute('''UPDATE jobs SET status=?,last_error=?,message_id=?,guid=?,updated_at=? WHERE id=?''',
                   (status,result.get('error'),result.get('message_id'),result.get('guid'),int(time.time()),job['id']))
        if result.get('guid'):
            db.execute('UPDATE contacts SET guid=? WHERE id=?',(result['guid'],job['contact_id']))
    return True

async def worker_loop():
    while True:
        try:
            moved=await worker_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            moved=False
        await asyncio.sleep(2 if moved else 5)

@asynccontextmanager
async def lifespan(app:FastAPI):
    init_db()
    task=asyncio.create_task(worker_loop())
    try:yield
    finally:
        task.cancel()
        try:await task
        except asyncio.CancelledError:pass

app=FastAPI(title='Rubika Online Personal Queue',version='1.0',lifespan=lifespan,
    docs_url=None, redoc_url=None)

@app.get('/')
async def index():
    return FileResponse(ROOT/'static'/'index.html',headers={'Cache-Control':'no-store'})

@app.get('/health')
async def health():
    return {'app':'rubika-online','ok':True,'provider_tested':False}

@app.get('/api/status',dependencies=[Depends(auth)])
async def status():
    with connect_db() as db:
        counts={r['status']:r['n'] for r in db.execute('SELECT status,COUNT(*) AS n FROM jobs GROUP BY status')}
        return {'verified':_setting(db,'verified')=='1','running':_setting(db,'running')=='1',
                'counts':counts, 'total_contacts':db.execute('SELECT COUNT(*) FROM contacts').fetchone()[0],
                'consenting_contacts':db.execute('SELECT COUNT(*) FROM contacts WHERE consent=1').fetchone()[0]}

class PhoneRequest(BaseModel):
    phone:str
    password:str|None=None

class VerifyRequest(BaseModel):
    code:str=Field(min_length=4,max_length=12)

@app.post('/api/auth/request',dependencies=[Depends(auth)])
async def request_code(payload:PhoneRequest):
    phone=normalize_phone(payload.phone)
    if not phone:raise HTTPException(400,'شماره موبایل ایرانی معتبر وارد کنید.')
    try:return await BRIDGE.request_code(phone, payload.password)
    except Exception as exc:raise HTTPException(502,f'درخواست کد از روبیکا ناموفق بود: {type(exc).__name__}: {str(exc)[:300]}')

@app.post('/api/auth/verify',dependencies=[Depends(auth)])
async def verify_code(payload:VerifyRequest):
    try:
        result=await BRIDGE.verify_code(payload.code)
        with connect_db() as db:_set(db,'verified','1')
        return {'connected':True, **result}
    except Exception as exc:
        raise HTTPException(502,f'تایید حساب در روبیکا ناموفق بود: {type(exc).__name__}: {str(exc)[:300]}')

@app.post('/api/auth/check',dependencies=[Depends(auth)])
async def check():
    try:
        result=await BRIDGE.self_test()
        with connect_db() as db:_set(db,'verified','1')
        return result
    except Exception as exc:
        with connect_db() as db:
            _set(db,'verified','0');_set(db,'running','0')
        raise HTTPException(502,f'اتصال حساب روبیکا برقرار نیست: {type(exc).__name__}: {str(exc)[:300]}')

@app.post('/api/contacts/import',dependencies=[Depends(auth)])
async def import_contacts(file:UploadFile=File(...)):
    if not file.filename or not file.filename.lower().endswith(('.xlsx','.csv')):
        raise HTTPException(400,'فقط CSV یا XLSX مجاز است.')
    raw=await file.read(4_000_001)
    if len(raw)>4_000_000:raise HTTPException(400,'حجم فایل بیشتر از ۴ مگابایت است.')
    try: rows=_rows_from_file(file.filename, raw)
    except HTTPException:raise
    except Exception as exc:raise HTTPException(400,'فایل اکسل/CSV خوانده نشد: '+str(exc)[:160])
    now=int(time.time())
    with connect_db() as db:
        db.executemany('''INSERT INTO contacts(phone,name,consent,created_at) VALUES (?,?,?,?)
             ON CONFLICT(phone) DO UPDATE SET name=excluded.name,consent=excluded.consent''',
             [(p,n,c,now) for p,n,c in rows])
    return {'imported':len(rows),'consenting':sum(c for _,_,c in rows),
            'message':'تنها مخاطبان دارای consent=بله در صف قرار می‌گیرند.'}

@app.get('/api/contacts/template',dependencies=[Depends(auth)])
async def template():
    content='phone,name,consent\r\n09123456789,نمونه,بله\r\n'
    return Response(content='\ufeff'+content,media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition':'attachment; filename="rubika-contacts-template.csv"'})

class QueueCreate(BaseModel):
    message:str=Field(min_length=1,max_length=4200)
    interval_seconds:int=Field(ge=60,le=86400)
    start_at:str
    allow_add_contacts:bool=False

@app.post('/api/queue/build',dependencies=[Depends(auth)])
async def build_queue(payload:QueueCreate):
    try:
        parsed=datetime.fromisoformat(payload.start_at.replace('Z','+00:00'))
        if parsed.tzinfo is None:raise ValueError('timezone')
        start=int(parsed.timestamp())
    except Exception:raise HTTPException(400,'زمان‌بندی باید تاریخ ISO دارای timezone باشد.')
    if start<int(time.time())-60:raise HTTPException(400,'زمان شروع نباید در گذشته باشد.')
    with connect_db() as db:
        if db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('sending','pending')").fetchone()[0]:
            raise HTTPException(409,'ابتدا صف فعلی را کامل یا پاک کنید؛ صف موازی مجاز نیست.')
        contacts=db.execute('SELECT id FROM contacts WHERE consent=1 ORDER BY id').fetchall()
        if not contacts:raise HTTPException(400,'در Excel مخاطب دارای رضایت ثبت نشده است.')
        now=int(time.time())
        db.executemany('''INSERT INTO jobs(contact_id,message,due_at,status,allow_add,updated_at)
                          VALUES (?,?,?,'pending',?,?)''',
                       [(r['id'],payload.message,start+i*payload.interval_seconds,
                         int(payload.allow_add_contacts),now) for i,r in enumerate(contacts)])
        _set(db,'running','0')
    return {'queued':len(contacts),'first_due_at':start,'last_due_at':start+(len(contacts)-1)*payload.interval_seconds}

@app.post('/api/queue/start',dependencies=[Depends(auth)])
async def start_queue():
    with connect_db() as db:
        if _setting(db,'verified')!='1':
            raise HTTPException(409,'ابتدا با شماره و کد، حساب شخصی روبیکا را متصل کنید.')
        if not db.execute("SELECT 1 FROM jobs WHERE status='pending' LIMIT 1").fetchone():
            raise HTTPException(409,'پیامی در وضعیت انتظار وجود ندارد.')
        _set(db,'running','1')
    return {'running':True}

@app.post('/api/queue/pause',dependencies=[Depends(auth)])
async def pause_queue():
    with connect_db() as db:_set(db,'running','0')
    return {'running':False,'note':'عملیات در حال اجرا ممکن است به پایان برسد.'}

@app.post('/api/queue/clear',dependencies=[Depends(auth)])
async def clear_queue():
    with connect_db() as db:
        if _setting(db,'running')=='1':raise HTTPException(409,'اول صف را متوقف کنید.')
        if db.execute("SELECT 1 FROM jobs WHERE status='sending' LIMIT 1").fetchone():
            raise HTTPException(409,'صبر کنید تا ارسال در حال انجام تعیین تکلیف شود.')
        db.execute('DELETE FROM jobs')
    return {'cleared':True}

@app.get('/api/jobs',dependencies=[Depends(auth)])
async def jobs():
    with connect_db() as db:
        rows=db.execute('''SELECT j.id,c.phone,c.name,j.status,j.due_at,j.attempts,j.message_id,j.last_error
            FROM jobs j JOIN contacts c ON c.id=j.contact_id ORDER BY j.id DESC LIMIT 250''').fetchall()
        return {'jobs':[dict(r) for r in rows]}

@app.get('/api/jobs/export',dependencies=[Depends(auth)])
async def export_jobs():
    stream=io.StringIO()
    writer=csv.writer(stream)
    writer.writerow(['id','phone','name','status','due_at_utc','message_id','last_error'])
    with connect_db() as db:
        rows=db.execute('''SELECT j.id,c.phone,c.name,j.status,j.due_at,j.message_id,j.last_error
             FROM jobs j JOIN contacts c ON c.id=j.contact_id ORDER BY j.id''')
        for r in rows:
            writer.writerow([r['id'],r['phone'],r['name'],r['status'],
                             datetime.fromtimestamp(r['due_at'],timezone.utc).isoformat(),
                             r['message_id'] or '',r['last_error'] or ''])
    return Response(content='\ufeff'+stream.getvalue(),media_type='text/csv; charset=utf-8',
           headers={'Content-Disposition':'attachment; filename="rubika-send-results.csv"'})
