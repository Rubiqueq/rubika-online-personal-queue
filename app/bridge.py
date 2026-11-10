"""Rubpy personal-account integration. No bot-token endpoints or fake successes.

Uses public rubpy 7.3.5 methods. Sessions are generated only on server, never in repo.
The provider is unofficial and may break when Rubika changes its API.
"""
import asyncio
from datetime import datetime, timezone
from pathlib import Path

class RubikaUnavailable(RuntimeError):
    pass


def _import_rubpy():
    try:
        from rubpy import Client
        from rubpy.crypto import Crypto
        from Crypto.PublicKey import RSA
        from Crypto.Signature import pkcs1_15
        return Client, Crypto, RSA, pkcs1_15
    except ImportError as exc:
        raise RubikaUnavailable("وابستگی rubpy روی سرور نصب نشده است: pip install -r requirements.txt") from exc


def _val(o, key, default=None):
    return o.get(key, default) if isinstance(o, dict) else getattr(o, key, default)


def _as_dict(result):
    if isinstance(result, dict):
        return result
    data = getattr(result, "original_update", None)
    return data if isinstance(data, dict) else {}


def _extract_contact(root, phone):
    """Never guess a GUID: only use a GUID explicitly present on provider result."""
    seen = set()
    def find(o, depth=0):
        if depth > 6 or id(o) in seen:
            return None
        seen.add(id(o))
        if isinstance(o, list):
            for it in o:
                x = find(it, depth + 1)
                if x: return x
        elif isinstance(o, dict):
            for k in ('user_guid', 'object_guid'):
                x = o.get(k)
                if isinstance(x, str) and x.startswith('u0'):
                    p = o.get('phone') or o.get('phone_number')
                    if not p or ''.join(c for c in str(p) if c.isdigit()).endswith(phone[-10:]):
                        return x
            for k,v in o.items():
                if k not in ('_client', 'metadata', 'private_key', 'auth') and isinstance(v, (dict,list)):
                    x = find(v, depth + 1)
                    if x:return x
        return None
    return find(root)

class RubikaBridge:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.session_path = str(self.data_dir / 'personal_session')
        self._pending = None
        self._lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()

    def _new_client(self):
        Client, _, _, _ = _import_rubpy()
        return Client(name=self.session_path, parse_mode='markdown', max_retries=2)

    async def _close(self, client):
        if client is not None:
            try:
                await client.stop()
            except Exception:
                pass

    async def request_code(self, phone, password=None):
        async with self._lock:
            if self._pending:
                await self._close(self._pending['client'])
                self._pending = None
            client = self._new_client()
            try:
                await client.connect()  # Avoid rubpy.start's interactive input() prompt.
                response = await client.send_code(phone_number=phone, pass_key=password, send_type='SMS')
                state = str(_val(response, 'status', 'OK'))
                if state == 'SendPassKey':
                    await self._close(client)
                    return {'state':'password_required', 'hint':str(_val(response,'hint_pass_key',''))}
                if state != 'OK':
                    raise RubikaUnavailable('Rubika sendCode response: ' + state)
                code_hash = _val(response, 'phone_code_hash')
                if not code_hash:
                    raise RubikaUnavailable('روبیکا کد هش تایید را برنگرداند؛ کد واقعاً درخواست نشده است.')
                self._pending = {'client':client,'phone':phone,'hash':code_hash,
                                 'at':datetime.now(timezone.utc).timestamp()}
                return {'state':'code_requested'}
            except Exception:
                await self._close(client)
                raise

    async def verify_code(self, code):
        async with self._lock:
            p = self._pending
            if not p:
                raise RubikaUnavailable('ابتدا درخواست کد ورود را ثبت کنید.')
            if datetime.now(timezone.utc).timestamp() - p['at'] > 600:
                self._pending = None
                await self._close(p['client'])
                raise RubikaUnavailable('مهلت کد تمام شد؛ مجدداً درخواست کد بدهید.')
            _, Crypto, RSA, pkcs1_15 = _import_rubpy()
            client = p['client']
            try:
                public_key, private_key = Crypto.create_keys()
                result = await client.sign_in(phone_code=code, phone_number=p['phone'],
                                              phone_code_hash=p['hash'], public_key=public_key)
                if _val(result,'status') != 'OK':
                    raise RubikaUnavailable('ورود توسط روبیکا تایید نشد.')
                encrypted = _val(result, 'auth')
                if not encrypted:
                    raise RubikaUnavailable('نشست تاییدشده از روبیکا دریافت نشد.')
                auth = Crypto.decrypt_RSA_OAEP(private_key, encrypted)
                user = _val(result, 'user')
                guid = _val(user,'user_guid')
                if not guid:
                    raise RubikaUnavailable('شناسه حساب تایید شده پیدا نشد.')
                client.auth = auth
                client.key = Crypto.passphrase(auth)
                client.decode_auth = Crypto.decode_auth(auth)
                client.private_key = private_key
                client.import_key = pkcs1_15.new(RSA.import_key(private_key.encode()))
                client.guid = guid
                client.session.insert(auth=auth, guid=guid, user_agent=client.user_agent,
                                      phone_number=_val(user,'phone',p['phone']), private_key=private_key)
                try:
                    await client.register_device(device_model='Rubika Online Personal Queue')
                except Exception as exc:
                    # Session exists, but don't announce verified without device registration.
                    raise RubikaUnavailable('ثبت دستگاه در روبیکا موفق نشد: '+str(exc)) from exc
                self._pending = None
                return {'guid':guid, 'phone':p['phone']}
            finally:
                # On invalid OTP retain pending code hash but close this network client only on success.
                if self._pending is None:
                    await self._close(client)

    async def _signed_in_client(self):
        client = self._new_client()
        try:
            await client.connect()
            if not client.auth or not client.private_key:
                raise RubikaUnavailable('نشست معتبر روی سرور ذخیره نشده است؛ ابتدا با کد وارد حساب شوید.')
            await client.start()
            return client
        except Exception:
            await self._close(client)
            raise

    async def self_test(self):
        async with self._send_lock:
            client = await self._signed_in_client()
            try:
                me = await client.get_me()
                guid = _val(_val(me,'user'), 'user_guid') or client.guid
                if not guid:
                    raise RubikaUnavailable('شناسه حساب در پاسخ روبیکا پیدا نشد.')
                return {'guid':guid, 'connected':True}
            finally:
                await self._close(client)

    async def send(self, phone, name, text, guid=None, allow_add=False):
        async with self._send_lock:
            client = await self._signed_in_client()
            try:
                if not client.guid:
                    raise RubikaUnavailable('ورود حساب شخصی تأیید نشده است.')
                resolved = guid if isinstance(guid, str) and guid.startswith('u0') else None
                if not resolved:
                    # First search existing contacts without modifying address book.
                    contact_results = await client.get_contacts()
                    resolved = _find_matching_phone(_as_dict(contact_results), phone)
                if not resolved and allow_add:
                    # Explicit UI opt-in: this writes a contact to the user's Rubika address book.
                    added = await client.add_address_book(phone=phone, first_name=name or 'مخاطب')
                    resolved = _extract_contact(_as_dict(added), phone)
                    if not resolved:
                        contact_results = await client.get_contacts()
                        resolved = _find_matching_phone(_as_dict(contact_results), phone)
                if not resolved:
                    return {'state':'unresolved','error':'این شماره به شناسه کاربر روبیکا تبدیل نشد. پیام ارسال نشد.'}
                response = await client.send_message(object_guid=resolved, text=text)
                message_id = _val(response, 'message_id')
                if not message_id:
                    return {'state':'review','guid':resolved,
                            'error':'پاسخ روبیکا شناسه پیام ندارد؛ نتیجه ارسال نامشخص است. از ارسال تکراری خودکار جلوگیری شد.'}
                return {'state':'server_accepted','guid':resolved,'message_id':str(message_id)}
            finally:
                await self._close(client)

def _find_matching_phone(obj, phone):
    """Only return user_guid from the same record containing an exact normalized phone."""
    if isinstance(obj, list):
        for item in obj:
            x = _find_matching_phone(item, phone)
            if x:return x
    elif isinstance(obj, dict):
        n = obj.get('phone') or obj.get('phone_number')
        if n is not None:
            digits = ''.join(str(d) for d in str(n) if d.isascii() and d.isdigit())
            guid = obj.get('user_guid') or obj.get('object_guid')
            if digits.endswith(phone[-10:]) and isinstance(guid,str) and guid.startswith('u0'):
                return guid
        for k, v in obj.items():
            if k not in ('_client', 'auth', 'private_key') and isinstance(v, (dict,list)):
                x = _find_matching_phone(v, phone)
                if x:return x
    return None
