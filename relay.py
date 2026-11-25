import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import threading
import time
from contextlib import contextmanager
from email.message import EmailMessage
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet
from flask import Flask, Response, abort, g, jsonify, render_template, request, session
import requests
from werkzeug.middleware.proxy_fix import ProxyFix

import egress
import protocols


STATE = Path(os.getenv('RELAY_STATE', './state'))
STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
MASTER = STATE / 'master.key'
try:
    with MASTER.open('xb') as handle:
        handle.write(Fernet.generate_key())
except FileExistsError:
    pass
MASTER.chmod(0o600)
SECRET = MASTER.read_bytes()
CIPHER = Fernet(SECRET)
CORE = egress.Core(os.getenv('MIHOMO_STATE', str(STATE / 'mihomo')), os.getenv('MIHOMO_SECRET', 'development'))
PASSWORDS = PasswordHasher()
ADMIN_PATH = '/' + os.getenv('ADMIN_PATH', 'admin').strip('/')
ADMIN_PORT = os.getenv('ADMIN_PORT', '')
app = Flask(__name__)
if ADMIN_PORT:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
app.secret_key = HMAC_KEY = hmac.digest(SECRET, b'session', 'sha256')
app.config.update(MAX_CONTENT_LENGTH=2 * 1024 * 1024, SESSION_COOKIE_SECURE=bool(ADMIN_PORT),
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict')
NETWORK_ERRORS = (requests.RequestException, OSError)
PROVIDERS = {
    'OpenAI': ('responses', 'https://api.openai.com'),
    'Anthropic': ('messages', 'https://api.anthropic.com'),
    'Google Gemini': ('gemini', 'https://generativelanguage.googleapis.com'),
    'DeepSeek': ('chat', 'https://api.deepseek.com'),
    'xAI/Grok': ('chat', 'https://api.x.ai'),
    'Kimi': ('chat', 'https://api.moonshot.ai'),
}


def now():
    return int(time.time())


def digest(value):
    return hmac.digest(HMAC_KEY, value.encode(), 'sha256').hex()


def encrypt(value):
    return CIPHER.encrypt(value.encode()).decode()


def decrypt(value):
    return CIPHER.decrypt(value.encode()).decode()


@contextmanager
def database(write=False):
    db = sqlite3.connect(STATE / 'relay.sqlite3', timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        if write:
            db.execute('BEGIN IMMEDIATE')
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


with database() as db:
    db.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT COLLATE NOCASE UNIQUE, display TEXT,
      email TEXT COLLATE NOCASE UNIQUE, password TEXT NOT NULL, admin INTEGER DEFAULT 0, change_password INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS invites(id INTEGER PRIMARY KEY, code TEXT UNIQUE, lookup TEXT UNIQUE, remaining INTEGER,
      expires INTEGER, enabled INTEGER, note TEXT);
    CREATE TABLE IF NOT EXISTS otp(email TEXT PRIMARY KEY, code TEXT, invite TEXT, expires INTEGER, attempts INTEGER);
    CREATE TABLE IF NOT EXISTS limits(bucket TEXT PRIMARY KEY, start INTEGER, count INTEGER);
    CREATE TABLE IF NOT EXISTS keys(id INTEGER PRIMARY KEY, owner INTEGER REFERENCES users(id), label TEXT, prefix TEXT,
      lookup TEXT UNIQUE, secret TEXT, mode TEXT DEFAULT 'requests', quota INTEGER DEFAULT 0,
      expires INTEGER DEFAULT 0, models TEXT DEFAULT '[]', enabled INTEGER DEFAULT 1, created INTEGER);
    CREATE TABLE IF NOT EXISTS upstreams(id INTEGER PRIMARY KEY, name TEXT UNIQUE, protocol TEXT, base TEXT, secret TEXT,
      nodes TEXT, enabled INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS routes(alias TEXT, upstream INTEGER REFERENCES upstreams(id), model TEXT, priority INTEGER,
      PRIMARY KEY(alias, upstream, model));
    CREATE TABLE IF NOT EXISTS discovered(upstream INTEGER REFERENCES upstreams(id), model TEXT, PRIMARY KEY(upstream, model));
    CREATE TABLE IF NOT EXISTS ledger(id TEXT PRIMARY KEY, key_id INTEGER REFERENCES keys(id), model TEXT, started INTEGER,
      status TEXT, reserved INTEGER, input INTEGER DEFAULT 0, output INTEGER DEFAULT 0, requests INTEGER DEFAULT 0);
    CREATE INDEX IF NOT EXISTS ledger_key ON ledger(key_id);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, actor INTEGER, action TEXT, target TEXT, created INTEGER);
    """)
    if not db.execute("SELECT 1 FROM users WHERE admin=1").fetchone():
        initial = os.getenv('INITIAL_PASSWORD')
        if not initial and (STATE / 'bootstrap-password').is_file():
            initial = (STATE / 'bootstrap-password').read_text().strip()
        if not initial:
            initial = secrets.token_urlsafe(24)
            with (STATE / 'bootstrap-password').open('x', encoding='utf-8') as handle:
                handle.write(initial + '\n')
            (STATE / 'bootstrap-password').chmod(0o600)
        db.execute("INSERT INTO users(username,display,password,admin,change_password) VALUES('root','管理员',?,1,1)",
                   (PASSWORDS.hash(initial),))
    # Calls interrupted by a process restart may have reached the provider.
    db.execute("UPDATE ledger SET status='unknown',requests=1,input=reserved WHERE status='pending'")


class Problem(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


@app.errorhandler(Problem)
def problem(error):
    if request.path.startswith('/v1beta/'):
        statuses = {400:'INVALID_ARGUMENT', 401:'UNAUTHENTICATED', 403:'PERMISSION_DENIED', 429:'RESOURCE_EXHAUSTED', 503:'UNAVAILABLE'}
        return jsonify(error={'code': error.status, 'message': error.message, 'status': statuses.get(error.status, 'UNKNOWN')}), error.status
    if request.path == '/v1/messages':
        return jsonify(type='error', error={'type': 'api_error', 'message': error.message}), error.status
    return jsonify(error={'message': error.message}), error.status


@app.errorhandler(ValueError)
def bad_value(error):
    return problem(Problem(str(error)))


@app.errorhandler(sqlite3.IntegrityError)
def duplicate(_):
    return problem(Problem('记录重复或仍在使用中', 409))


def settings(name, default=None):
    with database() as db:
        row = db.execute('SELECT value FROM settings WHERE key=?', (name,)).fetchone()
    return json.loads(decrypt(row[0])) if row else default


def setting(db, name, value):
    db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (name, encrypt(json.dumps(value))))


def audit(db, action, target):
    db.execute('INSERT INTO audit(actor,action,target,created) VALUES(?,?,?,?)', (g.user['id'], action, str(target), now()))


def payload():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise Problem('需要 JSON 对象')
    return data


def rate(name, ceiling, window):
    marker = now()
    with database(True) as db:
        db.execute('DELETE FROM limits WHERE start<?', (marker - 86400,))
        row = db.execute('SELECT start,count FROM limits WHERE bucket=?', (name,)).fetchone()
        if not row or row['start'] + window <= marker:
            db.execute('INSERT OR REPLACE INTO limits VALUES(?,?,1)', (name, marker))
        elif row['count'] >= ceiling:
            raise Problem('操作过于频繁，请稍后再试', 429)
        else:
            db.execute('UPDATE limits SET count=count+1 WHERE bucket=?', (name,))


def account(db, data, *, temporary=False):
    username = str(data.get('username', '')).strip()
    display = str(data.get('display', '')).strip()
    email = str(data.get('email', '')).strip().lower() or None
    password = str(data.get('password', ''))
    if not re.fullmatch(r'[A-Za-z0-9_]{3,32}', username) or not 1 <= len(display) <= 80:
        raise Problem('请填写 3–32 位字母、数字或下划线的用户名和显示名称')
    if email and (len(email) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email)):
        raise Problem('邮箱格式错误')
    check_password(password)
    db.execute('INSERT INTO users(username,display,email,password,change_password) VALUES(?,?,?,?,?)',
               (username, display, email, PASSWORDS.hash(password), int(temporary)))


def check_password(password):
    if not 12 <= len(password) <= 128:
        raise Problem('密码长度须为 12–128 个字符')


def valid_password(user, password):
    if not user or len(password) > 128:
        return False
    try:
        return PASSWORDS.verify(user['password'], password)
    except (VerificationError, InvalidHashError):
        return False


def admin_request():
    return not ADMIN_PORT or request.environ.get('HTTP_X_LUNARELAY_ENTRY') == 'admin'


@app.before_request
def session_guard():
    if request.path.startswith(('/v1/', '/v1beta/')):
        return
    g.user = None
    if session.get('uid'):
        with database() as db:
            g.user = db.execute('SELECT * FROM users WHERE id=?', (session['uid'],)).fetchone()
        if g.user and session.get('password_version') != digest(g.user['password']):
            g.user = None
        if g.user and bool(g.user['admin']) != admin_request() and ADMIN_PORT:
            g.user = None
    if request.method == 'POST' and not hmac.compare_digest(
        str(session.get('csrf', '-')), request.headers.get('X-CSRF-Token', '')
    ):
        raise Problem('请刷新网页后重试', 403)
    if request.path.startswith(ADMIN_PATH + '/') and not admin_request():
        abort(404)


@app.after_request
def secure(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Frame-Options'] = 'DENY'
    if not request.path.startswith(('/v1/', '/v1beta/')):
        response.headers['Cache-Control'] = 'no-store'
    return response


def member(admin=False):
    if not g.user or (admin and not g.user['admin']):
        raise Problem('请先登录', 401)
    if g.user['change_password']:
        raise Problem('请先修改初始密码', 403)
    return g.user


@app.get('/')
@app.get(ADMIN_PATH)
def home():
    administrator = request.path == ADMIN_PATH
    if administrator != admin_request() and ADMIN_PORT:
        abort(404)
    session.setdefault('csrf', secrets.token_urlsafe(32))
    config = settings('turnstile', {})
    return render_template('panel.html', admin=administrator, admin_path=ADMIN_PATH, csrf=session['csrf'],
                           user=dict(g.user) if g.user else None, sitekey=config.get('sitekey', ''),
                           signup=bool(settings('smtp', {}).get('verified')), providers=PROVIDERS)


def challenge(data, action):
    config = settings('turnstile', {})
    if not config.get('secret'):
        return
    token = str(data.get('turnstile', ''))
    if not token or len(token) > 2048:
        raise Problem('请完成人机验证')
    url = 'https://challenges.cloudflare.com/turnstile/v0/siteverify'
    try:
        with egress.direct() as client:
            result = client.post(url, data={'secret': config['secret'], 'response': token}, timeout=8).json()
    except (requests.RequestException, ValueError):
        raise Problem('人机验证服务暂时不可用', 503) from None
    if not result.get('success') or result.get('action') != action or result.get('hostname') != request.host.split(':')[0]:
        raise Problem('人机验证失败，请重试')


@app.post('/session/<action>')
def authentication(action):
    data = payload()
    if action == 'logout':
        session.clear()
        return jsonify(ok=True)
    if action == 'password':
        if not g.user or not valid_password(g.user, str(data.get('old', ''))):
            raise Problem('当前密码错误', 403)
        password = str(data.get('password', ''))
        check_password(password)
        password_hash = PASSWORDS.hash(password)
        with database(True) as db:
            db.execute('UPDATE users SET password=?,change_password=0 WHERE id=?', (password_hash, g.user['id']))
        session['password_version'] = digest(password_hash)
        if g.user['admin']:
            (STATE / 'bootstrap-password').unlink(missing_ok=True)
        return jsonify(ok=True)
    if action != 'login':
        abort(404)
    identity = str(data.get('username', '')).strip()
    rate('login:' + request.remote_addr, 15, 300)
    rate('identity:' + digest(identity.lower()), 8, 300)
    challenge(data, 'login')
    with database() as db:
        user = db.execute('SELECT * FROM users WHERE username=?', (identity,)).fetchone()
    if not valid_password(user, str(data.get('password', ''))) or (ADMIN_PORT and bool(user['admin']) != admin_request()):
        raise Problem('用户名或密码错误', 401)
    csrf = session['csrf']
    session.clear()
    session.update(uid=user['id'], csrf=csrf, password_version=digest(user['password']))
    return jsonify(ok=True)


def email_message(config, recipient, subject, body):
    message = EmailMessage()
    message['From'], message['To'], message['Subject'] = config['from'], recipient, subject
    message.set_content(body)
    try:
        context = ssl.create_default_context()
        if config['port'] == 465:
            connection = smtplib.SMTP_SSL(config['host'], config['port'], timeout=10, context=context)
        else:
            connection = smtplib.SMTP(config['host'], config['port'], timeout=10)
        with connection as smtp:
            if config['port'] != 465:
                smtp.starttls(context=context)
            if config.get('username'):
                smtp.login(config['username'], config['password'])
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException, ValueError):
        raise Problem('SMTP 发送失败，请检查连接、账号和发件人', 503) from None


@app.post('/register/<action>')
def register(action):
    if (ADMIN_PORT and admin_request()) or action not in ('send', 'create'):
        abort(404)
    config = settings('smtp', {})
    if not config.get('verified'):
        raise Problem('暂未开放邮箱注册', 503)
    data = payload()
    email = str(data.get('email', '')).strip().lower()
    code = str(data.get('invite', '')).strip().upper()
    if len(email) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email) or not re.fullmatch(r'[A-Z0-9]{6}', code):
        raise Problem('请检查邮箱和六位授权码')
    rate('registration:' + request.remote_addr, 15, 300)
    challenge(data, 'send' if action == 'send' else 'register')
    invite = digest(code)
    if action == 'send':
        rate('mail:' + digest(email), 2, 120)
        otp = f'{secrets.randbelow(1000000):06d}'
        with database(True) as db:
            check_invite(db, invite)
            db.execute('INSERT OR REPLACE INTO otp VALUES(?,?,?,?,0)', (email, digest(otp), invite, now() + 600))
        email_message(config, email, 'LunaRelay 注册验证码', f'你的注册验证码为 {otp}，10 分钟内有效。')
        return jsonify(ok=True)
    invalid = False
    with database(True) as db:
        row = check_invite(db, invite)
        otp = db.execute('SELECT * FROM otp WHERE email=?', (email,)).fetchone()
        if not otp or otp['invite'] != invite or otp['expires'] <= now() or otp['attempts'] >= 5:
            raise Problem('邮箱验证码无效或已到期')
        if not hmac.compare_digest(otp['code'], digest(str(data.get('otp', '')))):
            db.execute('UPDATE otp SET attempts=attempts+1 WHERE email=?', (email,))
            invalid = True
        else:
            account(db, data)
            db.execute('DELETE FROM otp WHERE email=?', (email,))
            db.execute('UPDATE invites SET remaining=remaining-1 WHERE id=?', (row['id'],))
    if invalid:
        raise Problem('邮箱验证码错误')
    return jsonify(ok=True)


def check_invite(db, lookup):
    row = db.execute('SELECT * FROM invites WHERE lookup=? AND enabled=1 AND remaining>0 AND expires>?',
                     (lookup, now())).fetchone()
    if not row:
        raise Problem('授权码不可用')
    return row


def key_rows(db, owner=None):
    query = """SELECT keys.id,keys.owner,users.username,label,prefix,mode,quota,expires,models,enabled,created,
               COALESCE(SUM(input+output),0) AS tokens,COALESCE(SUM(requests),0) AS requests
               FROM keys JOIN users ON keys.owner=users.id LEFT JOIN ledger ON ledger.key_id=keys.id"""
    if owner is not None:
        query += ' WHERE owner=?'
    return [dict(row) for row in db.execute(query + ' GROUP BY keys.id ORDER BY keys.id DESC', (owner,) if owner else ())]


@app.route('/account/keys', methods=['GET', 'POST'])
def my_keys():
    user = member()
    if request.method == 'GET':
        with database() as db:
            return jsonify(key_rows(db, user['id']))
    data = payload()
    label = str(data.get('label', '')).strip()
    if not 1 <= len(label) <= 80:
        raise Problem('输入 1–80 个字符的 Key 名称')
    token = 'lr_' + secrets.token_urlsafe(32)
    with database(True) as db:
        if db.execute('SELECT COUNT(*) FROM keys WHERE owner=?', (user['id'],)).fetchone()[0] >= 50:
            raise Problem('已达到 Key 数量上限')
        db.execute('INSERT INTO keys(owner,label,prefix,lookup,secret,created) VALUES(?,?,?,?,?,?)',
                   (user['id'], label, token[:12], digest(token), encrypt(token), now()))
    return jsonify(key=token), 201


CONFIG_LOCK = threading.Lock()


def refresh_proxy(url=None):
    with CONFIG_LOCK:
        url = url or settings('subscription', '')
        if not url:
            raise Problem('请先添加代理订阅', 503)
        try:
            candidates = egress.subscription(egress.download(url))
            revision = settings('proxy_revision', 0)
            with database() as db:
                exits = {'19000': settings('default_nodes', [])}
                exits.update({str(19000 + row['id']): json.loads(row['nodes'])
                             for row in db.execute('SELECT id,nodes FROM upstreams') if json.loads(row['nodes'])})
            nodes = CORE.configure(candidates, exits)
        except (NETWORK_ERRORS, UnicodeError):
            raise Problem('代理订阅或 Mihomo 连接失败', 503) from None
        with database(True) as db:
            setting(db, 'subscription', url)
            setting(db, 'proxy_updated', now())
            setting(db, 'proxy_nodes', nodes)
            setting(db, 'proxy_active_revision', revision)
        return nodes


def periodic():
    while True:
        time.sleep(60)
        if settings('subscription') and (now() - settings('proxy_updated', 0) >= 21600
                                          or settings('proxy_active_revision') != settings('proxy_revision', 0)):
            try:
                refresh_proxy()
            except (Problem, ValueError):
                time.sleep(900)


if os.getenv('MIHOMO_STATE'):
    threading.Thread(target=periodic, daemon=True).start()


def names(value):
    if isinstance(value, str):
        value = value.split(',')
    return list(dict.fromkeys(str(item).strip() for item in value or [] if str(item).strip()))


def upstream_call(upstream, method, path, data=None, stream=False):
    port = 19000 + upstream['id'] if json.loads(upstream['nodes']) else 19000
    client = CORE.client(port)
    try:
        response = client.request(method, upstream['base'] + path, json=data, stream=stream,
                                  headers=protocols.headers(upstream['protocol'], decrypt(upstream['secret'])),
                                  timeout=(8, 120), allow_redirects=False)
    except BaseException:
        client.close()
        raise
    return client, response


def discover(upstream):
    protocol = upstream['protocol']
    found = []
    page = '/v1beta/models' if protocol == 'gemini' else '/v1/models'
    for _ in range(10):
        client, response = upstream_call(upstream, 'GET', page)
        with client, response:
            if response.status_code != 200:
                raise Problem(f'上游模型目录返回 HTTP {response.status_code}', 502)
            obj = response.json()
        if protocol == 'gemini':
            found.extend(item['name'].removeprefix('models/') for item in obj.get('models', [])
                         if 'generateContent' in item.get('supportedGenerationMethods', []))
            token = obj.get('nextPageToken')
            page = '/v1beta/models?pageToken='
        else:
            found.extend(item['id'] for item in obj.get('data', []))
            token = obj.get('last_id') if obj.get('has_more') else None
            page = '/v1/models?after_id='
        if not token:
            break
        from urllib.parse import quote
        page += quote(token, safe='')
    with database(True) as db:
        db.execute('DELETE FROM discovered WHERE upstream=?', (upstream['id'],))
        db.executemany('INSERT OR IGNORE INTO discovered VALUES(?,?)', [(upstream['id'], model) for model in found])
    return found


@app.get(ADMIN_PATH + '/data')
def admin_data():
    member(True)
    with database() as db:
        result = {
            'users': [dict(r) for r in db.execute('SELECT id,username,display,email FROM users WHERE admin=0')],
            'keys': key_rows(db),
            'invites': [dict(r) | {'code': decrypt(r['code'])} for r in db.execute(
                'SELECT id,code,remaining,expires,enabled,note FROM invites ORDER BY id DESC')],
            'upstreams': [dict(r) | {'quota': '上游未提供'} for r in db.execute(
                'SELECT id,name,protocol,base,nodes,enabled FROM upstreams')],
            'routes': [dict(r) for r in db.execute('SELECT * FROM routes ORDER BY alias,priority')],
            'models': [dict(r) for r in db.execute('SELECT * FROM discovered ORDER BY upstream,model')],
            'audit': [dict(r) for r in db.execute('SELECT action,target,created FROM audit ORDER BY id DESC LIMIT 30')],
        }
    core_status = {}
    try:
        core_status = CORE.status()
    except (NETWORK_ERRORS, ValueError):
        pass
    result.update(proxy_updated=settings('proxy_updated'), default_nodes=settings('default_nodes', []),
                  smtp_ready=bool(settings('smtp', {}).get('verified')), turnstile_ready=bool(settings('turnstile', {})),
                  nodes=[{'id': n, 'name': next((k.split(' ', 1)[1] for k in core_status if k.startswith(n + ' ')), n),
                          'alive': any(v.get('alive', False) for k, v in core_status.items() if k.startswith(n + ' '))}
                         for n in settings('proxy_nodes', [])],
                  exits={k: v.get('now') for k, v in core_status.items() if k.startswith('exit-')})
    return jsonify(result)


@app.post(ADMIN_PATH + '/<action>')
def admin_update(action):
    user = member(True)
    data = payload()
    record = int(data.get('id') or 0)
    if action == 'proxy':
        nodes = refresh_proxy(str(data.get('url', '')).strip() or None)
        return jsonify(nodes=nodes)
    if action == 'smtp':
        config = {'host': data['host'], 'port': int(data['port']), 'from': data['from'],
                  'username': data.get('username', ''), 'password': data.get('password', ''), 'verified': True}
        email_message(config, data['recipient'], 'LunaRelay SMTP 测试', '这是一封测试邮件。')
        with database(True) as db:
            setting(db, 'smtp', config)
            audit(db, action, 'configuration')
        return jsonify(ok=True)
    if action in ('discover', 'probe'):
        with database() as db:
            upstream = db.execute('SELECT * FROM upstreams WHERE id=? AND enabled=1', (record,)).fetchone()
        if not upstream:
            raise Problem('请选择已启用的上游')
        try:
            if action == 'discover':
                return jsonify(models=discover(upstream))
            model = str(data.get('model', '')).strip()
            if not model:
                raise Problem('请输入要测试的上游模型')
            protocol = upstream['protocol']
            body = protocols.convert('chat', protocol, {'messages': [{'role':'user','content':'Reply OK'}], 'max_tokens': 32}, model, False)
            client, response = upstream_call(upstream, 'POST', protocols.endpoint(protocol, model), body)
            with client, response:
                if response.status_code >= 300:
                    raise Problem(f'上游返回 HTTP {response.status_code}', 502)
                return jsonify(output=protocols.output_text(protocol, response.json()))
        except NETWORK_ERRORS:
            raise Problem('代理或上游连接失败', 503) from None
    with database(True) as db:
        if action == 'user':
            account(db, data, temporary=True)
        elif action == 'invite':
            remaining, expires = int(data['remaining']), int(data['expires'])
            if not 0 <= remaining <= 10000:
                raise Problem('可用次数须在 0 至 10000 之间')
            enabled = int(bool(data.get('enabled', True)))
            note = str(data.get('note', ''))[:150]
            if record:
                db.execute('UPDATE invites SET remaining=?,expires=?,enabled=?,note=? WHERE id=?',
                           (remaining, expires, enabled, note, record))
            else:
                while True:
                    code = ''.join(secrets.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(6))
                    if not db.execute('SELECT 1 FROM invites WHERE lookup=?', (digest(code),)).fetchone():
                        break
                db.execute('INSERT INTO invites(code,lookup,remaining,expires,enabled,note) VALUES(?,?,?,?,?,?)',
                           (encrypt(code), digest(code), remaining, expires, enabled, note))
        elif action == 'key':
            mode, quantity = data['mode'], int(data.get('quota') or 0)
            selected = names(data.get('models'))
            if mode not in ('requests', 'tokens', 'unlimited') or not 0 <= quantity <= 10**15:
                raise Problem('额度配置无效')
            if set(selected) - {r[0] for r in db.execute('SELECT alias FROM routes')}:
                raise Problem('存在尚未配置路由的模型')
            db.execute('UPDATE keys SET mode=?,quota=?,expires=?,models=?,enabled=? WHERE id=?',
                       (mode, quantity, int(data.get('expires') or 0), json.dumps(selected), int(bool(data.get('enabled', True))), record))
        elif action == 'reveal':
            rate_limit = db.execute('SELECT COUNT(*) FROM audit WHERE actor=? AND action=? AND created>?',
                                    (user['id'], action, now() - 60)).fetchone()[0]
            if rate_limit >= 10 or not valid_password(user, str(data.get('password', ''))):
                raise Problem('无法完成密码验证', 403)
            row = db.execute('SELECT secret FROM keys WHERE id=?', (record,)).fetchone()
            if not row:
                raise Problem('Key 不存在', 404)
            audit(db, action, record)
            return jsonify(key=decrypt(row[0]))
        elif action == 'turnstile':
            sitekey, secret = str(data.get('sitekey', '')).strip(), str(data.get('secret', '')).strip()
            if bool(sitekey) != bool(secret):
                raise Problem('请同时填写站点密钥和服务器密钥')
            setting(db, 'turnstile', {'sitekey': sitekey, 'secret': secret} if sitekey else {})
        elif action == 'defaults':
            selected = names(data.get('nodes'))
            if not set(selected) <= set(settings('proxy_nodes', [])):
                raise Problem('节点不存在')
            setting(db, 'default_nodes', selected)
            setting(db, 'proxy_revision', settings('proxy_revision', 0) + 1)
        elif action == 'upstream':
            name = str(data.get('name', '')).strip()
            protocol, base = data.get('protocol'), str(data.get('base', '')).strip().rstrip('/')
            selected = names(data.get('nodes'))
            if protocol not in ('chat', 'responses', 'messages', 'gemini') or not name or not base:
                raise Problem('请填写完整的上游名称、协议和地址')
            parsed = egress.public_https(base)
            if parsed.query:
                raise Problem('上游地址不能含查询参数')
            if not set(selected) <= set(settings('proxy_nodes', [])):
                raise Problem('节点不存在')
            cred = str(data.get('secret', '')).strip()
            if record:
                db.execute('UPDATE upstreams SET name=?,protocol=?,base=?,secret=COALESCE(?,secret),nodes=?,enabled=? WHERE id=?',
                           (name, protocol, base, encrypt(cred) if cred else None, json.dumps(selected),
                            int(bool(data.get('enabled', True))), record))
            else:
                if not cred or db.execute('SELECT COUNT(*) FROM upstreams').fetchone()[0] >= 128:
                    raise Problem('缺少 API 凭据或已达到上游数量上限')
                db.execute('INSERT INTO upstreams(name,protocol,base,secret,nodes) VALUES(?,?,?,?,?)',
                           (name, protocol, base, encrypt(cred), json.dumps(selected)))
            if settings('subscription'):
                setting(db, 'proxy_revision', settings('proxy_revision', 0) + 1)
        elif action == 'route':
            alias, model = str(data.get('alias', '')).strip(), str(data.get('model', '')).strip()
            if not re.fullmatch(r'[\w./:-]{1,128}', alias) or not model or len(model) > 200:
                raise Problem('请输入有效的模型别名和上游模型')
            if data.get('remove'):
                db.execute('DELETE FROM routes WHERE alias=? AND upstream=? AND model=?', (alias, record, model))
            else:
                db.execute('INSERT OR REPLACE INTO routes VALUES(?,?,?,?)', (alias, record, model, int(data.get('priority', 0))))
        else:
            abort(404)
        audit(db, action, record or 'new')
    if action in ('upstream', 'defaults') and settings('subscription'):
        try:
            refresh_proxy()
        except (Problem, ValueError):
            return jsonify(ok=True, warning='已保存，代理出口待刷新')
    return jsonify(ok=True)


def api_key():
    authorization = request.headers.get('Authorization', '')
    bearer = authorization[7:] if authorization.startswith('Bearer ') else ''
    token = bearer or request.headers.get('x-api-key') or request.headers.get('x-goog-api-key') or request.args.get('key', '')
    if not token or len(token) > 120:
        raise Problem('需要有效的 API Key', 401)
    with database() as db:
        key = db.execute('SELECT * FROM keys WHERE lookup=? AND enabled=1', (digest(token),)).fetchone()
    if not key:
        raise Problem('需要有效的 API Key', 401)
    return key


@app.get('/v1/models')
@app.get('/v1beta/models')
def models():
    key = api_key()
    allowed = json.loads(key['models'])
    with database() as db:
        available = {r[0] for r in db.execute('SELECT DISTINCT alias FROM routes JOIN upstreams ON upstream=upstreams.id WHERE enabled=1')}
    selected = sorted(set(allowed) & available)
    if request.path.endswith('beta/models'):
        return jsonify(models=[{'name': 'models/' + m, 'displayName': m,
                                'supportedGenerationMethods': ['generateContent', 'streamGenerateContent']} for m in selected])
    return jsonify(object='list', data=[{'id': model, 'object': 'model', 'owned_by': 'LunaRelay', 'created': 0,
                                       'type': 'model', 'display_name': model} for model in selected], has_more=False)


def reserve(key_id, alias, request_id, estimate):
    with database(True) as db:
        key = db.execute('SELECT * FROM keys WHERE id=? AND enabled=1', (key_id,)).fetchone()
        if not key or alias not in json.loads(key['models']):
            raise Problem('此 Key 未获准调用该模型', 403)
        if key['expires'] and key['expires'] <= now():
            raise Problem('此 Key 的使用期限已到', 403)
        if key['mode'] != 'unlimited':
            is_tokens = key['mode'] == 'tokens'
            used = db.execute(
                'SELECT COALESCE(SUM(CASE WHEN status=? THEN reserved ELSE input+output END),0) FROM ledger WHERE key_id=?'
                if is_tokens else
                'SELECT COALESCE(SUM(CASE WHEN status=? THEN 1 ELSE requests END),0) FROM ledger WHERE key_id=?',
                ('pending', key_id),
            ).fetchone()[0]
            if used + (estimate if is_tokens else 1) > key['quota']:
                raise Problem('此 Key 的可用额度不足', 429)
        db.execute('INSERT INTO ledger(id,key_id,model,started,status,reserved) VALUES(?,?,?,?,?,?)',
                   (request_id, key_id, alias, now(), 'pending', estimate))


def settle(request_id, status, counts=(0, 0)):
    with database(True) as db:
        db.execute('UPDATE ledger SET status=?,input=?,output=?,requests=? WHERE id=? AND status=?',
                   (status, *counts, int(status != 'rejected'), request_id, 'pending'))


@app.post('/v1/chat/completions')
@app.post('/v1/responses')
@app.post('/v1/messages')
@app.post('/v1beta/models/<path:gemini>')
def infer(gemini=None):
    key = api_key()
    data = payload()
    if gemini:
        alias, _, action = gemini.rpartition(':')
        if action not in ('generateContent', 'streamGenerateContent'):
            raise Problem('不支持的 Gemini 模型操作', 404)
        source, stream = 'gemini', action == 'streamGenerateContent'
    else:
        source = {'/v1/chat/completions': 'chat', '/v1/responses': 'responses', '/v1/messages': 'messages'}[request.path]
        alias, stream = str(data.get('model', '')), bool(data.get('stream'))
    if alias not in json.loads(key['models']):
        raise Problem('此 Key 未获准调用该模型', 403)
    with database() as db:
        routes = db.execute("""SELECT upstreams.*,routes.model FROM routes JOIN upstreams ON upstream=upstreams.id
                           WHERE alias=? AND enabled=1 ORDER BY priority,upstreams.id""", (alias,)).fetchall()
    if not routes:
        raise Problem('模型暂无可用上游', 503)
    candidates = []
    errors = []
    for upstream in routes:
        try:
            candidates.append((upstream, protocols.convert(source, upstream['protocol'], data, upstream['model'], stream)))
        except ValueError as exc:
            errors.append(str(exc))
    if not candidates:
        raise Problem(errors[0])
    max_output = data.get('max_completion_tokens') or data.get('max_tokens') or data.get('max_output_tokens')
    if source == 'gemini':
        max_output = data.get('generationConfig', {}).get('maxOutputTokens')
    if key['mode'] == 'tokens' and max_output is None:
        max_output = 1024
        for upstream, body in candidates:
            if upstream['protocol'] == 'gemini':
                body.setdefault('generationConfig', {})['maxOutputTokens'] = max_output
            else:
                body[{'chat':'max_completion_tokens','responses':'max_output_tokens','messages':'max_tokens'}[upstream['protocol']]] = max_output
    if max_output is not None and not 1 <= int(max_output) <= 1048576:
        raise Problem('最大输出 Token 数超出范围')
    estimate = len(request.get_data()) + int(max_output or 1024)
    request_id = 'lr_' + secrets.token_hex(12)
    if not settings('proxy_updated'):
        raise Problem('请等待管理员配置或刷新上游代理', 503)
    reserve(key['id'], alias, request_id, estimate)
    for upstream, body in candidates:
        target = upstream['protocol']
        try:
            client, response = upstream_call(upstream, 'POST', protocols.endpoint(target, upstream['model'], stream), body, True)
        except requests.ConnectTimeout:
            continue
        except requests.ConnectionError as exc:
            if 'ProxyError' == type(exc).__name__:
                continue
            settle(request_id, 'unknown', (estimate, 0))
            raise Problem('上游连接中断，调用结果未知', 503) from None
        except NETWORK_ERRORS:
            settle(request_id, 'unknown', (estimate, 0))
            raise Problem('上游响应中断，调用结果未知', 503) from None
        if response.status_code >= 300:
            status = response.status_code
            response.close()
            client.close()
            if status in (401, 403, 404, 429):
                continue
            settle(request_id, 'rejected' if status < 500 else 'unknown', (0 if status < 500 else estimate, 0))
            raise Problem(f'上游返回 HTTP {status}', 400 if status in (400, 422) else 502)
        if stream:
            return Response(wire_stream(client, response, source, target, alias, request_id, estimate),
                            mimetype='text/event-stream', headers={'X-Accel-Buffering': 'no'})
        try:
            with client, response:
                obj = response.json()
            counts = protocols.usage(obj)
            settle(request_id, 'complete', counts if sum(counts) else (estimate, 0))
            if source != target:
                obj = protocols.result(source, request_id, alias, protocols.output_text(target, obj), counts)
            elif 'model' in obj:
                obj['model'] = alias
            return jsonify(obj)
        except (NETWORK_ERRORS, ValueError):
            settle(request_id, 'unknown', (estimate, 0))
            raise Problem('上游返回无效或不完整的数据', 502) from None
    settle(request_id, 'rejected')
    raise Problem('当前模型的上游或代理暂时不可用', 503)


def wire_stream(client, response, source, target, alias, request_id, estimate):
    counts = (0, 0)
    completed = False
    rolling_text = []
    event = None
    try:
        if source != target:
            yield from protocols.begin_stream(source, request_id, alias)
        for raw in response.iter_lines(chunk_size=1):
            line = raw.decode('utf-8')
            if line.startswith('event:'):
                event = line[6:].strip()
            if not line.startswith('data:'):
                continue
            body = line[5:].strip()
            if body == '[DONE]':
                completed = True
                if source == target:
                    yield protocols.sse(body, event)
                break
            obj = json.loads(body)
            counts = tuple(max(a, b) for a, b in zip(counts, protocols.usage(obj)))
            kind = obj.get('type') or event
            completed |= kind in ('response.completed', 'response.incomplete', 'message_stop') or bool(
                obj.get('choices') and obj['choices'][0].get('finish_reason')) or bool(
                obj.get('candidates') and obj['candidates'][0].get('finishReason'))
            if source == target:
                if 'model' in obj:
                    obj['model'] = alias
                for slot in ('response', 'message'):
                    if isinstance(obj.get(slot), dict) and 'model' in obj[slot]:
                        obj[slot]['model'] = alias
                yield protocols.sse(obj, event)
            else:
                delta = protocols.output_text(target, obj)
                if delta:
                    rolling_text.append(delta)
                    yield protocols.delta_stream(source, request_id, alias, delta)
            event = None
        if completed and source != target:
            yield from protocols.end_stream(source, request_id, alias, ''.join(rolling_text), counts)
        if not completed:
            yield protocols.sse({'type': 'error', 'error': {'message': '上游流式请求中断'}}, 'error')
    except (NETWORK_ERRORS, ValueError):
        yield protocols.sse({'type': 'error', 'error': {'message': '上游流式请求中断'}}, 'error')
    finally:
        response.close()
        client.close()
        settle(request_id, 'complete' if completed else 'unknown', counts if sum(counts) else (estimate, 0))


@app.get('/healthz')
def health():
    with database() as db:
        db.execute('SELECT 1').fetchone()
    return jsonify(ok=True)
