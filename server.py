#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""园图 parkmap MCP 连接器（stdio 版，零框架依赖）。

一个自包含的 MCP server：通过 JSON-RPC over stdio 与 MCP 客户端通信，
把工具调用转发到园图 parkmap 开放数据接口（X-API-Key 认证）。

仅提供只读工具，不会对平台数据做任何写操作。
每次调用后自动附带当日/当月剩余配额，方便 Agent 自我管理调用节奏。

v0.4.0 行为约定（技术规程见 docs/mcp-connector-spec.md）：
- 未绑定 Key：首次工具调用时弹出官网注册页（每进程一次），并以 isError 结果
  返回图文引导（注册 → 办会员 → 生成 Key → 配置重启），Agent 会在会话中转述
- Key 无效/无权限/会员过期/配额耗尽：同样以 isError 结果返回带官网链接的
  行动引导（会话内消息提醒），不用协议级错误——协议错误多数客户端只显示失败
  徽章，Agent 拿不到可读内容，无法提醒用户
- 服务端 403/429 的 detail 为 {'code', 'message', ...} 字典，本文件按 code 映射
"""
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

SERVER_VERSION = '0.4.1'

API_BASE = os.environ.get('PARKMAP_API_BASE', 'https://parkmap.cn').rstrip('/')
API_KEY = os.environ.get('PARKMAP_API_KEY', '')
SITE_BASE = os.environ.get('PARKMAP_SITE_BASE', 'https://parkmap.cn').rstrip('/')

# 用户行动入口（与官网路由一致；官网改版时同步修改）
URL_REGISTER = SITE_BASE + '/register/personal'      # 注册
URL_LOGIN = SITE_BASE + '/login'                     # 登录
URL_KEY = SITE_BASE + '/account?tab=workbench'       # 生成/管理 API Key（工作台）
URL_MEMBER = SITE_BASE + '/account?tab=membership'   # 会员状态/续费
URL_MALL = SITE_BASE + '/mall'                       # 会员卡商城（办理/升级）

TOOLS = [
    {
        'name': 'search_parks',
        'description': '按城市/区县/产业方向/租金区间/关键词检索园区，返回园区列表（免费档可用）',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'city': {'type': 'string', 'description': '城市名，如 北京、上海'},
                'district': {'type': 'string', 'description': '区县名，如 海淀区'},
                'industry': {'type': 'string', 'description': '产业方向，如 人工智能、生物医药'},
                'q': {'type': 'string', 'description': '关键词（匹配园区名与简介）'},
                'rent_min': {'type': 'number', 'description': '租金下限（元/㎡/天）'},
                'rent_max': {'type': 'number', 'description': '租金上限（元/㎡/天）'},
                'limit': {'type': 'integer', 'description': '返回条数，默认 50，最大 200'},
                'offset': {'type': 'integer', 'description': '分页偏移'},
            },
        },
    },
    {
        'name': 'get_park',
        'description': '获取单个园区档案：租金、产业方向、级别标识（个人档以上含政策与活动，企业档含租户名单与十维评估）',
        'inputSchema': {
            'type': 'object',
            'properties': {'park_id': {'type': 'string', 'description': '园区 ID，如 zgc-software'}},
            'required': ['park_id'],
        },
    },
    {
        'name': 'list_news',
        'description': '园区资讯：新园区立项/园区活动/园区新政/领导考察/综合动态（免费档可用）',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'category': {'type': 'string', 'enum': ['立项', '活动', '新政', '考察', '动态']},
                'city': {'type': 'string', 'description': '相关城市，如 北京'},
                'limit': {'type': 'integer', 'description': '返回条数，默认 20，最大 100'},
                'offset': {'type': 'integer', 'description': '分页偏移'},
            },
        },
    },
    {
        'name': 'list_policies',
        'description': '扶持政策检索（个人 API 会员及以上）：按园区/级别/标签/关键词筛选，结果带园区归属',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'park_id': {'type': 'string', 'description': '限定某园区的政策'},
                'level': {'type': 'string', 'enum': ['国家级', '市级', '区级', '园区级']},
                'tag': {'type': 'string', 'description': '标签，如 租金补贴、税收优惠、人才政策'},
                'q': {'type': 'string', 'description': '关键词（匹配标题与摘要）'},
                'limit': {'type': 'integer', 'description': '返回条数，默认 50，最大 200'},
                'offset': {'type': 'integer', 'description': '分页偏移'},
            },
        },
    },
    {
        'name': 'get_policy',
        'description': '政策全文详情（个人 API 会员及以上）',
        'inputSchema': {
            'type': 'object',
            'properties': {'policy_id': {'type': 'string', 'description': '政策 ID'}},
            'required': ['policy_id'],
        },
    },
    {
        'name': 'get_region_stats',
        'description': '区域统计（个人 API 会员及以上）：各区园区数量、平均租金、产业分布 TOP30',
        'inputSchema': {
            'type': 'object',
            'properties': {'city': {'type': 'string', 'description': '城市名，不传为全部'}},
        },
    },
    {
        'name': 'get_evaluations',
        'description': '十维选址评估档案（企业 API 会员）：政策/成本/硬件/交通/人才/生态/服务/合规/生活/发展十维评分与关键事实',
        'inputSchema': {
            'type': 'object',
            'properties': {'park_id': {'type': 'string', 'description': '园区 ID，不传返回全部档案'}},
        },
    },
]


class UserActionError(Exception):
    """需要用户行动（绑 Key/办会员/续费/等配额恢复）的错误。
    message 面向终端用户，必须带可点击的官网入口链接。"""


# ---------------- 缺 Key 引导：弹官网注册页（每进程一次） ----------------

_pop_state = {'done': False}


def _pop_page_once(url: str) -> None:
    """后台线程弹浏览器，不阻塞 stdio；弹不出不报错（消息里仍带网址）。
    每进程只弹一次：MCP 客户端常驻连接器进程，反复弹窗是骚扰。
    PARKMAP_NO_BROWSER=1 可禁用弹窗（无桌面环境/自动化测试）。"""
    if _pop_state['done']:
        return
    _pop_state['done'] = True
    if os.environ.get('PARKMAP_NO_BROWSER'):
        return
    try:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    except Exception:
        pass


def _no_key_error() -> 'UserActionError':
    _pop_page_once(URL_REGISTER)
    return UserActionError(
        '⚠️ 还未绑定园图 parkmap API Key，暂时无法查询。\n'
        '已为您打开官网注册页，请按以下步骤绑定：\n'
        f'1. 注册或登录：{URL_REGISTER}（已有账号：{URL_LOGIN}）\n'
        f'2. 办理正式会员（MCP 数据服务为正式会员权益）：{URL_MALL}\n'
        f'3. 生成 API Key：{URL_KEY}（账号管理 → 工作台，Key 以 pmk_ 开头，仅显示一次）\n'
        '4. 把 Key 写入 MCP 客户端配置的 env.PARKMAP_API_KEY，保存后重启客户端即可使用'
    )


def _map_http_error(e: urllib.error.HTTPError) -> 'UserActionError':
    body = e.read().decode('utf-8', errors='replace')
    try:
        detail = json.loads(body).get('detail', body)
    except ValueError:
        detail = body
    code = detail.get('code', '') if isinstance(detail, dict) else ''
    msg = detail.get('message', '') if isinstance(detail, dict) else str(detail)

    if e.code == 401:
        return UserActionError(
            '⚠️ API Key 无效或已吊销。\n'
            f'请到 {URL_KEY} 重新生成 Key，并更新 MCP 客户端配置里的 PARKMAP_API_KEY（无需重装连接器）。')
    if e.code == 403:
        if code == 'membership_trial':
            return UserActionError(
                f'⚠️ {msg}\n办理入口：{URL_MALL}（会员卡商城）或 {URL_MEMBER}（账号 → 会员）。')
        if code == 'membership_expired':
            return UserActionError(
                f'⚠️ {msg}\n续费入口：{URL_MEMBER} 或 {URL_MALL}。')
        if code == 'membership_required':
            return UserActionError(
                f'⚠️ {msg}\n办理入口：{URL_MALL}（会员卡商城）。')
        if code == 'scope_insufficient':
            return UserActionError(
                f'⚠️ {msg}\n升级入口：{URL_MALL}（兑换对应 API 会员卡，即时提档，无需更换 Key）。')
        return UserActionError(f'⚠️ 当前账号无权访问（403）：{msg}\n会员与档位管理：{URL_MEMBER}')
    if e.code == 429:
        if isinstance(detail, dict) and 'quota_day' in detail:
            return UserActionError(
                '⚠️ 调用额度已用完：今日 '
                f"{detail.get('used_today')}/{detail.get('quota_day')} 次，"
                f"本月 {detail.get('used_month')}/{detail.get('quota_month')} 次。\n"
                f'次日/次月自动恢复；高频使用可升级会员提升配额：{URL_MALL}，'
                '或联系平台定制企业配额。')
        return UserActionError(f'⚠️ 调用次数已达上限（429），次日/次月自动恢复。提升配额：{URL_MALL}')
    return UserActionError(f'接口错误（{e.code}）：{msg}')


def api_get(path, params=None):
    if not API_KEY:
        raise _no_key_error()
    url = API_BASE + path
    if params:
        url += '?' + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, '')})
    req = urllib.request.Request(url)
    req.add_header('X-API-Key', API_KEY)
    req.add_header('Accept', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            quota = {
                'tier': resp.headers.get('X-API-Tier'),
                'remaining_day': resp.headers.get('X-Quota-Remaining-Day'),
                'remaining_month': resp.headers.get('X-Quota-Remaining-Month'),
            }
            # 包装一层：数据 + 配额提示（Agent 可据此控制调用节奏）
            return {'data': data, 'quota': quota}
    except urllib.error.HTTPError as e:
        raise _map_http_error(e)


def call_tool(name, args):
    args = args or {}
    if name == 'search_parks':
        return api_get('/api/open/parks', args)
    if name == 'get_park':
        return api_get('/api/open/parks/' + urllib.parse.quote(str(args.get('park_id', ''))))
    if name == 'list_news':
        return api_get('/api/open/news', args)
    if name == 'list_policies':
        return api_get('/api/open/policies', args)
    if name == 'get_policy':
        return api_get('/api/open/policies/' + urllib.parse.quote(str(args.get('policy_id', ''))))
    if name == 'get_region_stats':
        return api_get('/api/open/stats', args)
    if name == 'get_evaluations':
        return api_get('/api/open/evaluations', args)
    raise RuntimeError(f'未知工具: {name}')


def reply(msg_id, result=None, error=None):
    out = {'jsonrpc': '2.0', 'id': msg_id}
    if error is not None:
        out['error'] = {'code': -32000, 'message': str(error)}
    else:
        out['result'] = result
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + '\n')
    sys.stdout.flush()


def tool_error_result(text):
    """用户可行动的错误以 isError 工具结果返回（非协议错误）：
    Agent 能读到内容并在会话里提醒用户，形成「会话内消息提醒」。"""
    return {'content': [{'type': 'text', 'text': text}], 'isError': True}


def main():
    if not API_KEY:
        # stderr 不影响 JSON-RPC；客户端日志可见，首次工具调用还会弹页 + isError 引导
        sys.stderr.write(f'[parkmap-mcp] 未配置 PARKMAP_API_KEY，调用工具时将引导绑定（{URL_REGISTER}）\n')
        sys.stderr.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method = msg.get('method', '')
        msg_id = msg.get('id')
        if method == 'initialize':
            reply(msg_id, {
                'protocolVersion': '2024-11-05',
                'capabilities': {'tools': {}},
                'serverInfo': {'name': 'parkmap-mcp', 'version': SERVER_VERSION},
            })
        elif method == 'notifications/initialized':
            pass
        elif method == 'tools/list':
            reply(msg_id, {'tools': TOOLS})
        elif method == 'tools/call':
            params = msg.get('params', {})
            try:
                data = call_tool(params.get('name'), params.get('arguments'))
                reply(msg_id, {
                    'content': [{'type': 'text', 'text': json.dumps(data, ensure_ascii=False, indent=2)}],
                })
            except UserActionError as e:
                reply(msg_id, tool_error_result(str(e)))
            except Exception as e:  # noqa: BLE001 — 其余异常走协议错误而非崩溃
                reply(msg_id, error=e)
        elif msg_id is not None:
            reply(msg_id, error=f'不支持的方法: {method}')


if __name__ == '__main__':
    main()
