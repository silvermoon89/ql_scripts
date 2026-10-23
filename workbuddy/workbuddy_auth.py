#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 凭证管理（公共模块）

设计要点：
  1. 以 refreshToken(RT) 为主凭证，accessToken(AT) 只是短期票据（约 28 天）。
  2. RT 每次刷新后服务端会下发新的 RT（轮换），因此必须落盘持久化，
     否则下次仍然拿着旧 RT，青龙环境变量又无法回写，最终 12153。
  3. 首次部署可以用环境变量引导，之后以凭证文件为准；文件里 RT 更新
     （iat 更大）时不会被旧的环境变量覆盖。

环境变量：
  WB_TOKEN_FILE       凭证文件路径（默认见 default_token_file()）
  WB_REFRESH_TOKEN    单个账号的 RT
  WB_REFRESH_TOKENS   多账号 RT，逗号分隔，支持 "uid:rt" 或纯 "rt"
  WB_ACCESS_TOKEN     单个账号 AT（兼容旧配置，建议改用 RT）
  WB_ACCESS_TOKENS    多账号 AT（兼容旧配置）
  WB_USER_ID          可选，强制指定 X-User-Id（单账号时生效）
  WB_PROXY            可选，HTTP(S)/SOCKS 代理
  WB_API_BASE         可选，默认 https://copilot.tencent.com
  WB_NOTIFY_PREFIX    可选，通知前缀（青龙 NODE 名称等），默认空
"""

import base64
import json
import os
import sys
import time

try:
    import requests
    HAVE_REQUESTS = True
except ImportError:
    import urllib.error
    import urllib.request
    HAVE_REQUESTS = False

API_BASE = (os.environ.get("WB_API_BASE") or "https://copilot.tencent.com").rstrip("/")
REFRESH_EP = "/v2/plugin/auth/token/refresh"
CLIENT_UA = "CLI/2.63.2 CodeBuddy/2.63.2"

# AT 提前刷新的余量：剩不到 1 小时就换新
REFRESH_MARGIN = int(os.environ.get("WB_REFRESH_MARGIN", "3600"))

PROXY = (os.environ.get("WB_PROXY") or "").strip()
PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None

TOKEN_FILE_ENV = "WB_TOKEN_FILE"


def _utf8_stdout():
    """Windows 控制台默认 GBK，emoji 会炸；青龙是 UTF-8，不受影响。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_utf8_stdout()


def log(msg):
    print(msg)


# ---------------------------------------------------------------- JWT 工具

def decode_jwt(token):
    """解码 JWT payload，失败返回 {}。不校验签名，只取 iat/exp/sub。"""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part).decode("utf-8", "ignore"))
    except Exception:
        return {}


def jwt_uid(token):
    payload = decode_jwt(token)
    return payload.get("sub") or payload.get("uid") or ""


def jwt_exp(token):
    try:
        return int(decode_jwt(token).get("exp") or 0)
    except Exception:
        return 0


def jwt_iat(token):
    try:
        return int(decode_jwt(token).get("iat") or 0)
    except Exception:
        return 0


def to_seconds(value):
    """兼容毫秒/秒两种时间戳（桌面端存的是毫秒）。"""
    try:
        n = int(value or 0)
    except Exception:
        return 0
    return n // 1000 if n > 100000000000 else n


def looks_like_jwt(value):
    return isinstance(value, str) and value.count(".") == 2 and value.startswith("eyJ")


# ---------------------------------------------------------------- 凭证文件

def default_token_file():
    env = (os.environ.get(TOKEN_FILE_ENV) or "").strip()
    if env:
        return env
    # 青龙的持久化目录：容器重建也不会丢
    if os.path.isdir("/ql/data/config"):
        return "/ql/data/config/workbuddy_tokens.json"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "workbuddy_tokens.json")


def _atomic_write(path, data):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    try:
        os.makedirs(directory, exist_ok=True)
    except Exception:
        pass
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, path)


def read_store(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"version": 1, "accounts": []}
    except Exception as e:
        log("⚠️ 凭证文件解析失败(%s): %s" % (path, e))
        return {"version": 1, "accounts": []}
    if not isinstance(data, dict):
        return {"version": 1, "accounts": []}
    accounts = data.get("accounts")
    if not isinstance(accounts, list):
        accounts = []
    return {"version": 1, "accounts": [a for a in accounts if isinstance(a, dict)]}


def write_store(path, store):
    _atomic_write(path, store)


def find_account(store, uid, refresh_token=None):
    """先按 uid 找；没有 uid（token 不是 JWT）时退回按 RT 找，避免重复追加账号。"""
    accounts = store.get("accounts", [])
    if uid:
        for acc in accounts:
            if acc.get("uid") == uid:
                return acc
    if refresh_token:
        for acc in accounts:
            if acc.get("refreshToken") == refresh_token:
                return acc
    return None


def upsert_account(store, account, prefer_new_rt=False):
    """按 uid 合并；同 uid 时默认保留更新的 RT。

    prefer_new_rt=True 用于"刚从服务端换回来的 RT"，此时无条件覆盖，
    否则会被文件里的旧 RT 顶回去。
    """
    uid = account.get("uid")
    existing = find_account(store, uid, account.get("refreshToken"))
    if existing is None:
        if not account.get("domain"):
            account["domain"] = "www.workbuddy.cn"
        store.setdefault("accounts", []).append(account)
        return account
    # 环境变量没指定域名时沿用文件里的（可能是刷新接口下发过的）
    if not account.get("domain"):
        account["domain"] = existing.get("domain") or "www.workbuddy.cn"
    # 环境变量里的旧 RT 不要覆盖文件里已经轮换过的 RT
    new_rt, old_rt = account.get("refreshToken"), existing.get("refreshToken")
    if not prefer_new_rt and new_rt and old_rt and new_rt != old_rt:
        # 文件才是权威存储：只有环境变量里的 RT 明确更新（两边都能解出 iat 且更大）
        # 才允许覆盖，否则一律让位给文件里已轮换的 RT
        new_iat, old_iat = jwt_iat(new_rt), jwt_iat(old_rt)
        if not (new_iat and old_iat and new_iat > old_iat):
            account["refreshToken"] = old_rt
            account["refreshExpiresAt"] = existing.get("refreshExpiresAt") or account.get("refreshExpiresAt")
    # 只给了 RT、没给 AT 时，沿用文件里已有的 AT，避免每次启动都白刷一次
    if not account.get("accessToken"):
        if existing.get("accessToken"):
            account["accessToken"] = existing["accessToken"]
            account["expiresAt"] = existing.get("expiresAt") or 0
        if not account.get("refreshToken"):
            account["refreshToken"] = old_rt or ""
            account["refreshExpiresAt"] = existing.get("refreshExpiresAt") or 0
    if not account.get("nickname"):
        account["nickname"] = existing.get("nickname") or ""
    before = (existing.get("refreshToken"), existing.get("accessToken"))
    existing.update({k: v for k, v in account.items() if k != "updatedAt"})
    if (existing.get("refreshToken"), existing.get("accessToken")) != before:
        existing["updatedAt"] = account.get("updatedAt") or int(time.time())
    return existing


# ---------------------------------------------------------------- 账号装配

def _split_multi(raw):
    """按逗号拆分；支持 'uid:token'。token 本身不含逗号，可安全切分。"""
    out = []
    for chunk in (raw or "").replace("\n", ",").replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" in chunk:
            head, tail = chunk.split(":", 1)
            head, tail = head.strip(), tail.strip()
            # uid 形态固定：UUID / 纯数字 / uin；否则整体当 token
            if tail and (looks_like_jwt(tail) or tail.count(".") >= 2):
                out.append((head, tail))
                continue
        out.append(("", chunk))
    return out


def _make_account(uid, token, kind):
    # 显式配置了 WB_DOMAIN 才写死域名，否则用文件里的值（默认由 upsert 补）
    domain = (os.environ.get("WB_DOMAIN") or "").strip()
    acc = {
        "uid": uid or jwt_uid(token),
        "nickname": "",
        "domain": domain,
        "disabled": False,
        "updatedAt": int(time.time()),
    }
    if kind == "rt":
        acc["refreshToken"] = token
        acc["refreshExpiresAt"] = jwt_exp(token)
        acc["accessToken"] = ""
        acc["expiresAt"] = 0
    else:
        acc["accessToken"] = token
        acc["expiresAt"] = jwt_exp(token)
        acc["refreshToken"] = ""
        acc["refreshExpiresAt"] = 0
    return acc


def accounts_from_env():
    out = []
    forced_uid = (os.environ.get("WB_USER_ID") or "").strip()
    multi_rt = (os.environ.get("WB_REFRESH_TOKENS") or "").strip()
    single_rt = (os.environ.get("WB_REFRESH_TOKEN") or "").strip()
    multi_at = (os.environ.get("WB_ACCESS_TOKENS") or "").strip()
    single_at = (os.environ.get("WB_ACCESS_TOKEN") or "").strip()

    for uid, tok in _split_multi(multi_rt):
        out.append(_make_account(uid, tok, "rt"))
    if single_rt:
        out.append(_make_account("", single_rt, "rt"))
    for uid, tok in _split_multi(multi_at):
        out.append(_make_account(uid, tok, "at"))
    if single_at:
        out.append(_make_account("", single_at, "at"))

    # WB_USER_ID 只用于单账号场景，避免把多账号都指到同一个 uid
    if forced_uid and len(out) == 1 and not out[0].get("uid"):
        out[0]["uid"] = forced_uid
    return out


def load_accounts(auto_save=True):
    """返回凭证列表。环境变量作为引导/补充，文件作为主存储。"""
    path = default_token_file()
    store = read_store(path)
    env_accounts = accounts_from_env()

    changed = False
    if env_accounts:
        seen = set()
        for acc in env_accounts:
            key = acc.get("uid") or acc.get("refreshToken") or acc.get("accessToken")
            if key in seen:
                continue
            seen.add(key)
            before = json.dumps(
                find_account(store, acc.get("uid"), acc.get("refreshToken")),
                sort_keys=True,
            )
            merged = upsert_account(store, acc)
            if json.dumps(merged, sort_keys=True) != before:
                changed = True

    if changed and auto_save:
        try:
            write_store(path, store)
        except Exception as e:
            log("⚠️ 凭证文件写入失败(%s): %s" % (path, e))

    accounts = [a for a in store.get("accounts", []) if not a.get("disabled")]
    if not accounts:
        log("❌ 未检测到凭证：请设置 WB_REFRESH_TOKEN / WB_REFRESH_TOKENS，")
        log("   或先在本地运行 extract_tokens.py 生成凭证文件：%s" % path)
    return accounts


def account_tag(acc, idx):
    uid = acc.get("uid") or ""
    if uid:
        return uid[:8] + "…"
    nick = acc.get("nickname") or ""
    return nick or ("账号%d" % idx)


# ---------------------------------------------------------------- HTTP

def _headers(acc, with_body=True):
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Authorization": "Bearer " + (acc.get("accessToken") or ""),
        "X-Product": "SaaS",
        "User-Agent": CLIENT_UA,
    }
    if with_body:
        headers["Content-Type"] = "application/json"
    if acc.get("uid"):
        headers["X-User-Id"] = acc["uid"]
    if acc.get("enterpriseId"):
        headers["X-Enterprise-Id"] = acc["enterpriseId"]
        headers["X-Tenant-Id"] = acc["enterpriseId"]
    if acc.get("domain"):
        headers["X-Domain"] = acc["domain"]
    return headers


def raw_request(method, url, headers, body=None, timeout=20):
    """返回 (status_code, text)。不做任何重试/刷新。"""
    data = None
    if method.upper() in ("POST", "PUT", "PATCH"):
        data = json.dumps(body if body is not None else {})

    if HAVE_REQUESTS:
        resp = requests.request(
            method, url, headers=headers, data=data, timeout=timeout, proxies=PROXIES
        )
        return resp.status_code, resp.text

    payload = data.encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=payload, headers=headers, method=method.upper())
    open_fn = urllib.request.urlopen
    if PROXY:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(PROXIES))
        open_fn = opener.open
    try:
        with open_fn(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")


def parse_json(text):
    try:
        return json.loads(text)
    except Exception:
        return None


def is_gateway_401(text):
    """APISIX 网关的裸 401（HTML/纯文本），说明 Bearer 根本没被接受。"""
    if not text:
        return True
    head = text.lstrip()[:200].lower()
    return head.startswith("401") or "authorization required" in head or "<html" in head


def refresh_account(acc, path=None, force=False):
    """用 RT 换新 AT（并轮换 RT）。成功返回 True。"""
    rt = acc.get("refreshToken") or ""
    if not rt:
        acc["lastRefreshError"] = "未配置 RT，AT 到期后无法续期"
        return False

    exp = int(acc.get("expiresAt") or 0)
    if not force and exp and exp - REFRESH_MARGIN > time.time() and acc.get("accessToken"):
        return True

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Refresh-Token": rt,
        "X-Auth-Refresh-Source": "plugin",
        "X-Product": "SaaS",
        "User-Agent": CLIENT_UA,
    }
    if acc.get("domain"):
        headers["X-Domain"] = acc["domain"]
    if acc.get("enterpriseId"):
        headers["X-Enterprise-Id"] = acc["enterpriseId"]

    try:
        status, text = raw_request("POST", API_BASE + REFRESH_EP, headers, {})
    except Exception as e:
        log("  ⚠️ 刷新请求异常: %s" % e)
        return False

    body = parse_json(text) or {}
    data = body.get("data") or {}
    if status != 200 or body.get("code") not in (0, None) or not data.get("accessToken"):
        msg = body.get("msg") or (text or "")[:120].replace("\n", " ")
        if body.get("code") == 12153:
            msg = "RT 已失效(12153)，需重新登录 WorkBuddy 后重新导出"
        acc["lastRefreshError"] = msg
        log("  ⚠️ RT 刷新失败(HTTP %s): %s" % (status, msg))
        return False

    acc["accessToken"] = data["accessToken"]
    if data.get("refreshToken"):
        acc["refreshToken"] = data["refreshToken"]
    if data.get("domain"):
        acc["domain"] = data["domain"]
    if data.get("expiresIn"):
        acc["expiresAt"] = int(time.time()) + int(data["expiresIn"])
    elif jwt_exp(acc["accessToken"]):
        acc["expiresAt"] = jwt_exp(acc["accessToken"])
    if data.get("refreshExpiresIn"):
        acc["refreshExpiresAt"] = int(time.time()) + int(data["refreshExpiresIn"])
    else:
        acc["refreshExpiresAt"] = jwt_exp(acc.get("refreshToken") or "")
    if not acc.get("uid"):
        acc["uid"] = jwt_uid(acc["accessToken"])
    acc["updatedAt"] = int(time.time())
    acc.pop("lastRefreshError", None)

    if path:
        try:
            store = read_store(path)
            # 这里的 acc 带着刚换到的 RT，必须优先于文件里的旧值
            upsert_account(store, acc, prefer_new_rt=True)
            write_store(path, store)
        except Exception as e:
            log("  ⚠️ 新 RT 落盘失败(%s): %s" % (path, e))
    return True


def ensure_token(acc, path=None):
    """确保 AT 可用；优先判断过期时间，其次在 401 时兜底刷新。"""
    at = acc.get("accessToken") or ""
    exp = int(acc.get("expiresAt") or jwt_exp(at) or 0)
    need = (not at) or (exp and exp - REFRESH_MARGIN <= time.time())
    if need:
        return refresh_account(acc, path)
    return True


def call(acc, method, path, body=None, path_of_store=None, retry=1):
    """带自动刷新的 API 调用。返回 (status, text)。"""
    acc.pop("lastRefreshError", None)
    if not ensure_token(acc, path_of_store):
        # 只有"RT 明确失效"或"根本没有 RT"才值得直接判定，网络抖动时
        # 旧 AT 可能还剩几十分钟可用，继续请求反而更容易成功。
        err = acc.get("lastRefreshError") or ""
        if "12153" in err:
            return 401, json.dumps({"code": 12153, "msg": err}, ensure_ascii=False)
        if not acc.get("refreshToken"):
            return 401, json.dumps(
                {"code": 12152, "msg": "未配置 RT，AT 到期后无法续期"},
                ensure_ascii=False,
            )
    status, text = raw_request(method, API_BASE + path, _headers(acc), body)

    if retry and status in (401, 403):
        parsed = parse_json(text)
        code = (parsed or {}).get("code")
        # 网关裸 401 / 业务 12153，都值得用 RT 再换一次 AT
        if code in (None, 12153) and (is_gateway_401(text) or code == 12153):
            if refresh_account(acc, path_of_store, force=True):
                return raw_request(method, API_BASE + path, _headers(acc), body)
            # 刷新失败时把根因透出，避免用户只看到网关 401
            err = acc.get("lastRefreshError") or "凭证刷新失败"
            # 只有确实是 RT 失效能归为 12153，网络问题不能误导用户去重登
            if "12153" in err:
                biz_code = 12153
            elif not acc.get("refreshToken"):
                biz_code = 12152
            else:
                biz_code = 12151
            return 401, json.dumps({"code": biz_code, "msg": err}, ensure_ascii=False)
    return status, text


def classify_failure(status, text):
    """把失败归类成 (exit_code_hint, 人类可读原因)。"""
    body = parse_json(text) or {}
    code = body.get("code")
    msg = body.get("msg") or body.get("message") or ""

    # 只有明确的鉴权失败才算凭证问题：
    #   12153 = RT/session 失效；网关裸 401 = Bearer 未被接受。
    # 403 + 具体业务 code（活动未开启、账号不满足条件等）属于业务限制，
    # 不能误导用户去重新登录。
    if code == 12153:
        return 3, "RT 已失效(12153)，请重新登录 WorkBuddy 并重新导出凭证"
    if code == 12152:
        return 3, "未配置 RT：AT 最长 28 天且无法续期，请改用 refreshToken"
    if status == 401 and is_gateway_401(text):
        return 3, "鉴权失败(网关 401)：AT 无效，且 RT 刷新未能恢复，请检查凭证"
    if code is not None and code != 0:
        return 1, "code=%s msg=%s" % (code, msg)
    return 1, "HTTP %s: %s" % (status, (text or "")[:120].replace("\n", " "))


def emit_notify_summary(title, lines):
    """青龙会捕获 stdout；这里保持简单，直接打印。"""
    if str(os.environ.get("QINGLONG_NOTIFY", "1")).lower() in ("0", "false", "no"):
        return
    prefix = (os.environ.get("WB_NOTIFY_PREFIX") or "").strip()
    head = ("%s %s" % (prefix, title)).strip()
    log("")
    log("===== %s =====" % head)
    for line in lines:
        log(line)
    log("=" * (len(head) + 12))


def exit_with(code):
    sys.exit(code)

