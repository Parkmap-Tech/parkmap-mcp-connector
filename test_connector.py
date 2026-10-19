# -*- coding: utf-8 -*-
"""MCP 连接器端到端验证（v0.4.0）：真实 stdio 握手 + 版本跟随 + 五类引导/提醒。

覆盖（评审 2026-10-19 老板需求）：
A. initialize 握手版本号 == VERSION 文件（版本号必须跟随发版）
B. 未配 Key：tools/call 返回 isError 引导（含注册/商城/工作台链接），不是协议错误
C. 有效 Key：search_parks 正常返回数据 + 配额（admin 直通门禁）
D. 会员已过期：isError 提醒含「已过期」与续费链接（membership_expired）
E. 配额耗尽：isError 提醒含今日/本月用量与升级链接（quota_exceeded）

前置：本地后端运行中（127.0.0.1:8000），开发模式（dev_code/dev_answer 可用）。
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

BASE = 'http://127.0.0.1:8000'
HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, '..', 'server', 'parkmap.db')
TS = int(time.time())
PWD = 'McpTest12345'

ok, fail = 0, 0


def check(name, cond, extra=''):
    global ok, fail
    if cond:
        ok += 1
        print(f'  PASS {name}')
    else:
        fail += 1
        print(f'  FAIL {name} {extra}')


def req(method, path, body=None, token=None):
    h = {'Content-Type': 'application/json'}
    if token:
        h['Authorization'] = f'Bearer {token}'
    r = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                               headers=h, method=method)
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        d = e.read()
        try:
            return e.code, json.loads(d)
        except Exception:
            return e.code, d.decode(errors='replace')


def get_captcha():
    _, cap = req('GET', '/api/captcha')
    cap['dev_answer'] = str(eval(cap['question'].replace('=', '').replace('?', '').strip()))
    return cap


def register(email, tag):
    cap = get_captcha()
    _, ec = req('POST', '/api/auth/send-code', {'email': email})
    st, r = req('POST', '/api/auth/register', {
        'role': '开发者', 'account_type': 'personal', 'email': email, 'password': PWD,
        'email_code': ec.get('dev_code'), 'captcha_id': cap['captcha_id'],
        'captcha_text': cap['dev_answer'], 'agree': True,
        'username': f'mcp{tag}{TS % 100000}', 'name': 'MCP连接器测试',
    })
    assert st == 200, (st, r)
    con = sqlite3.connect(DB, timeout=10)
    uid = con.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()[0]
    con.close()
    return uid


def insert_key_direct(user_id, raw_key):
    """绕过「试用/无会员不能建 key」的前置门禁，直接落库（测试已过期/无会员场景的 key）。"""
    con = sqlite3.connect(DB, timeout=10)
    con.execute(
        "INSERT INTO api_keys (user_id, name, prefix, key_hash, status, used_today, today, used_month, month, created_at)"
        " VALUES (?,?,?,?, 'active', 0, ?, 0, ?, ?)",
        (user_id, 'MCP测试直插', raw_key[:8], hashlib.sha256(raw_key.encode()).hexdigest(),
         datetime.utcnow().strftime('%Y-%m-%d'), datetime.utcnow().strftime('%Y-%m'),
         datetime.utcnow().isoformat()))
    con.commit()
    key_id = con.execute('SELECT max(id) FROM api_keys').fetchone()[0]
    con.close()
    return key_id


def set_membership(user_id, level, expires_at, api_tier='free'):
    con = sqlite3.connect(DB, timeout=10)
    con.execute(
        "INSERT INTO memberships (user_id, level, api_tier, starts_at, expires_at, status)"
        " VALUES (?,?,?,?,?, 'active')"
        " ON CONFLICT(user_id) DO UPDATE SET level=excluded.level, expires_at=excluded.expires_at,"
        " api_tier=excluded.api_tier, status='active'",
        (user_id, level, api_tier, datetime.utcnow().isoformat(), expires_at.isoformat()))
    con.commit()
    con.close()


def rpc_session(env_extra, messages):
    """起连接器子进程跑一轮 JSON-RPC 会话，返回 {id: reply} 与进程对象。"""
    env = dict(os.environ)
    env['PARKMAP_API_BASE'] = BASE
    env['PARKMAP_NO_BROWSER'] = '1'  # 测试机不真弹浏览器；弹窗路径已单测覆盖（无异常即过）
    env.pop('PARKMAP_API_KEY', None)
    env.update(env_extra)
    proc = subprocess.Popen(
        [sys.executable, 'server.py'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=HERE, env=env, text=True, encoding='utf-8',
    )
    proc.stdin.write('\n'.join(json.dumps(m, ensure_ascii=False) for m in messages) + '\n')
    proc.stdin.close()
    replies = {}
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if 'id' in msg:
            replies[msg['id']] = msg
    proc.wait(timeout=30)
    return replies


def tool_text(reply_msg):
    r = reply_msg.get('result', {})
    content = r.get('content') or []
    return (r.get('isError'), content[0]['text'] if content else '')


print('== MCP 连接器 v0.4.0 端到端验证 ==')

with open(os.path.join(HERE, 'VERSION'), encoding='utf-8') as f:
    version = f.read().strip()

# ---------- A. 版本跟随：握手版本号 == VERSION 文件 ----------
print('-- A. 版本号跟随')
replies = rpc_session({}, [
    {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
    {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
])
info = replies.get(1, {}).get('result', {}).get('serverInfo', {})
check('A1. initialize 握手', info.get('name') == 'parkmap-mcp')
check('A2. 握手版本号 == VERSION 文件', info.get('version') == version,
      f"-> {info.get('version')} vs {version}")
check('A3. tools/list 七工具', len(replies.get(2, {}).get('result', {}).get('tools', [])) == 7)

# ---------- B. 未配 Key：isError 引导（非协议错误） ----------
print('-- B. 未绑 Key 引导')
replies = rpc_session({}, [
    {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
    {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
    {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
     'params': {'name': 'search_parks', 'arguments': {'city': '北京'}}},
])
r3 = replies.get(3, {})
is_err, text = tool_text(r3)
check('B1. 缺 Key 走 isError 结果（非协议错误）', is_err is True and 'error' not in r3, f'-> {str(r3)[:120]}')
check('B2. 引导含「绑定 API Key」与三步路径', 'API Key' in text and '绑定' in text)
check('B3. 引导含注册/商城/工作台链接',
      '/register/personal' in text and '/mall' in text and 'tab=workbench' in text, f'-> {text[:150]}')

# ---------- C. 有效 Key（admin 直通）：正常返回 + 配额 ----------
print('-- C. 有效 Key 正常调用')
_, login = req('POST', '/api/auth/login', {'identifier': 'admin', 'password': 'Admin12345'})
st, key_resp = req('POST', '/api/apikeys', {'name': f'MCP验证{TS % 10000}'}, token=login['token'])
assert st == 200 and key_resp.get('key'), (st, key_resp)
admin_raw_key = key_resp['key']
replies = rpc_session({'PARKMAP_API_KEY': admin_raw_key}, [
    {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
    {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
    {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
     'params': {'name': 'search_parks', 'arguments': {'city': '北京', 'limit': 2}}},
])
is_err, text = tool_text(replies.get(3, {}))
payload = {}
try:
    payload = json.loads(text)
except ValueError:
    pass
check('C1. search_parks 返回数据', payload.get('data', {}).get('total', 0) > 0, f'-> {text[:150]}')
check('C2. 带配额提示', payload.get('quota', {}).get('remaining_day') is not None)

# ---------- D. 会员已过期：isError 提醒含「已过期」+ 续费链接 ----------
print('-- D. 会员过期提醒')
email_d = f'mcp-exp-{TS % 1000000}@test.cn'
uid_d = register(email_d, 'd')
raw_d = f'pmk_test_d_{TS}'
key_id_d = insert_key_direct(uid_d, raw_d)
set_membership(uid_d, 'pro', datetime.utcnow() - timedelta(days=1))  # 昨天到期
replies = rpc_session({'PARKMAP_API_KEY': raw_d}, [
    {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
    {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
    {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
     'params': {'name': 'search_parks', 'arguments': {'city': '北京'}}},
])
r3 = replies.get(3, {})
is_err, text = tool_text(r3)
check('D1. 过期调走 isError 结果', is_err is True and 'error' not in r3, f'-> {str(r3)[:120]}')
check('D2. 提醒含「已过期」', '已过期' in text, f'-> {text[:150]}')
check('D3. 提醒含续费入口链接', 'tab=membership' in text or '/mall' in text)

# ---------- E. 配额耗尽：isError 提醒含用量 + 升级链接 ----------
print('-- E. 配额耗尽提醒')
email_e = f'mcp-quota-{TS % 1000000}@test.cn'
uid_e = register(email_e, 'e')
raw_e = f'pmk_test_e_{TS}'
key_id_e = insert_key_direct(uid_e, raw_e)
set_membership(uid_e, 'pro', datetime.utcnow() + timedelta(days=30))  # 在期付费，api_tier=free（100/日）
con = sqlite3.connect(DB, timeout=10)
con.execute("UPDATE api_keys SET used_today=100, today=? WHERE id=?",
            (datetime.utcnow().strftime('%Y-%m-%d'), key_id_e))
con.commit()
con.close()
replies = rpc_session({'PARKMAP_API_KEY': raw_e}, [
    {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
    {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
    {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
     'params': {'name': 'search_parks', 'arguments': {'city': '北京'}}},
])
r3 = replies.get(3, {})
is_err, text = tool_text(r3)
check('E1. 超配额走 isError 结果', is_err is True and 'error' not in r3, f'-> {str(r3)[:120]}')
check('E2. 提醒含今日/本月用量', '100/100' in text and '本月' in text, f'-> {text[:180]}')
check('E3. 提醒含提升配额链接', '/mall' in text)

# ---------- 清理 ----------
con = sqlite3.connect(DB, timeout=10)
con.execute('DELETE FROM api_keys WHERE id IN (?, ?)', (key_id_d, key_id_e))
con.execute('DELETE FROM memberships WHERE user_id IN (?, ?)', (uid_d, uid_e))
con.execute("DELETE FROM api_call_logs WHERE key_id IN (?, ?)", (key_id_d, key_id_e))
con.execute("DELETE FROM user_profiles WHERE user_id IN (?, ?)", (uid_d, uid_e))
con.execute("DELETE FROM users WHERE id IN (?, ?)", (uid_d, uid_e))
con.commit()
con.close()
# admin 测试 key 吊销（DELETE 为软吊销）
req('DELETE', f'/api/apikeys/{key_resp["item"]["id"]}', token=login['token'])
print('  清理完成')

print(f'\n连接器验证：{ok} 通过，{fail} 失败')
sys.exit(1 if fail else 0)
