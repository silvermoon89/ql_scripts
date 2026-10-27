#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
库库 AI（kuku.baidu.com）每日自动签到 —— 青龙面板版

cron: 21 8 * * *
new Env('库库AI每日签到');

--------------------------------------------------------------------------
接口说明（抓包 + 前端 bundle 逆向确认，2026-10 版）
--------------------------------------------------------------------------
活动入口：https://kuku.baidu.com/operation/activitys/kukuPcSignAct/main
活动配置：GET  /act/api/conf?conf_key=kukuSignConf
          -> activityTid(活动tid) / lotteryTid(抽奖tid) / prizeIds /
             startTime / endTime / signPoint / signTimes

签到主链路（全部 POST，Content-Type: application/json）：
  GET  /act/v2/kukuai30/homepage?tid=<activityTid>
       body {}  -> {errno, data:{signed_today, continuous_days, calendar,
                   continuous_awards, makeup_success_dates, countdown, ...}}
  POST /act/v2/kukuai30/signin?tid=<activityTid>
       body {}  -> {errno:0} 即签到成功
  POST /act/v2/kukuai30/claimreward?tid=<activityTid>
       body 不可省，至少 {"day": <连签天数>}
  POST /act/v2/kukuai30/rewardtips?tid=<activityTid>
       body {}  -> 任务奖励回执（可选）
  POST /act/v2/component/lottery/lotterycommit?tid=<lotteryTid>&cnt=1
       （GET 亦可，见活动页 bundle）-> 抽奖

鉴权：完整 BDUSS 登录 Cookie（BDUSS / BDUSS_BFUSS / STOKEN 等）。
不带 Cookie 时任何活动接口都返回 {"errno":-6,"show_msg":"账户已过期，重新登陆"}。

风控：所有活动接口可以带 jsToken 参数，值为 /operation/activitys/api/getJsToken
      下发、经百度 AntiRequest SDK(HTJ) 注入到 window.jsToken 的函数。
      失效时返回 errno 8002(JSTOKEN_ERROR)，判为作弊时 8011/8024。
      本项目未复刻该签名——服务端对纯协议请求暂未强校验；若以后签到开始
      返回 8002/8011，请改用 KUKU_HEADLESS=1（Playwright 走真浏览器）。

环境变量
  KUKU_COOKIE         必填，单账号 Cookie（直接粘贴整条 Cookie 头）
  KUKU_COOKIES        可选，多账号，用换行或 "&&" 分隔
  KUKU_TID            可选，覆盖 activityTid（默认取配置接口，兜底 291651792210）
  KUKU_LOTTERY_TID    可选，覆盖 lotteryTid（默认取配置接口，兜底 652831792323）
  KUKU_DRAW           可选，默认 1：签到后把抽奖次数抽完；0 关闭
  KUKU_CLAIM_REWARD   可选，默认 1：连签达档自动领奖；0 关闭
  KUKU_TIMEOUT        可选，单请求超时秒数，默认 20
  KUKU_PROXY          可选，http:// 或 socks5:// 代理
  KUKU_HEADLESS       可选，默认 0；1 = 用 Playwright 真浏览器执行（需装 playwright）
  KUKU_NOTIFY         可选，默认 1；0 关闭通知
  KUKU_NOTIFY_PREFIX  可选，通知标题前缀

退出码：0 成功（含今日已签） / 1 业务失败 / 2 未配置 / 3 Cookie 失效
"""

import json
import os
import random
import re
import sys
import time

try:
    import requests
except ImportError:  # 青龙旧镜像兜底
    requests = None

# ------------------------------------------------------------------ 常量

BASE = "https://kuku.baidu.com"
SIGN_PAGE = BASE + "/operation/activitys/kukuPcSignAct/main"

EP_CONF = "/act/api/conf"
EP_CONF_V2 = "/act/v2/api/conf"
EP_HOMEPAGE = "/act/v2/kukuai30/homepage"
EP_SIGNIN = "/act/v2/kukuai30/signin"
EP_CLAIM = "/act/v2/kukuai30/claimreward"
EP_REWARD_TIPS = "/act/v2/kukuai30/rewardtips"
EP_LOTTERY_INFO = "/act/v2/component/lottery/homeinfo"
EP_LOTTERY_COMMIT = "/act/v2/component/lottery/lotterycommit"

DEFAULT_TID = "291651792210"
DEFAULT_LOTTERY_TID = "652831792323"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)

EXIT_OK, EXIT_BIZ, EXIT_CONFIG, EXIT_AUTH = 0, 1, 2, 3

# errno 语义（前端错误码表 66034 + 实测）
ERRNO_NOT_LOGIN = -6
ERRNO_HTJ = 8001
ERRNO_JSTOKEN = 8002
ERRNO_CHEAT = 8011
ERRNO_CHEAT_OTHER = 8024
AUTH_ERRNOS = {ERRNO_NOT_LOGIN, ERRNO_JSTOKEN, ERRNO_CHEAT, ERRNO_CHEAT_OTHER, 110, 257}
RISK_ERRNOS = {ERRNO_HTJ, ERRNO_JSTOKEN, ERRNO_CHEAT, ERRNO_CHEAT_OTHER}


# ------------------------------------------------------------------ 基础工具

def utf8_stdout():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


utf8_stdout()


def log(msg=""):
    print(msg, flush=True)


def env(name, default=""):
    return (os.environ.get(name) or default).strip()


def env_int(name, default):
    try:
        return int(env(name, str(default)))
    except Exception:
        return default


def is_on(name, default="1"):
    return env(name, default).lower() not in ("0", "false", "no", "off")


# ------------------------------------------------------------------ 账号解析

def normalize_cookie(raw):
    """把用户粘贴的内容整理成可用的 Cookie 头。

    支持三种输入：
      1) 完整 Cookie 头：BDUSS=xx; STOKEN=yy
      2) 只给 BDUSS=xx
      3) 只给 BDUSS 值本身（无 '='）
    """
    raw = (raw or "").strip().strip('"').strip("'")
    if not raw:
        return ""
    if "=" not in raw:
        # 裸 BDUSS 值
        return "BDUSS=" + raw
    parts = []
    for chunk in re.split(r"[;\n]+", raw):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        parts.append(chunk)
    return "; ".join(parts)


def cookie_dict(cookie_header):
    out = {}
    for chunk in cookie_header.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def bduss_of(cookie_header):
    return cookie_dict(cookie_header).get("BDUSS", "")


def mask(text, keep=6):
    if not text:
        return ""
    if len(text) <= keep * 2:
        return text[:2] + "***"
    return text[:keep] + "***" + text[-4:]


def load_accounts():
    accounts = []
    for raw in re.split(r"\n|&&", env("KUKU_COOKIES")):
        ck = normalize_cookie(raw)
        if ck:
            accounts.append(ck)
    single = normalize_cookie(env("KUKU_COOKIE"))
    if single and single not in accounts:
        accounts.insert(0, single)
    return accounts


def account_tag(cookie_header, idx):
    bduss = bduss_of(cookie_header)
    if bduss:
        return "账号%d(BDUSS:%s)" % (idx, mask(bduss))
    return "账号%d" % idx


# ------------------------------------------------------------------ HTTP 会话

class Kuku:
    def __init__(self, cookie_header, timeout=20):
        self.cookie_header = cookie_header
        self.timeout = timeout
        self.tid = env("KUKU_TID", DEFAULT_TID)
        self.lottery_tid = env("KUKU_LOTTERY_TID", DEFAULT_LOTTERY_TID)
        self.uid = bduss_of(cookie_header)[:8] or "anon"
        self.session = None
        if requests is not None:
            self.session = requests.Session()
            proxy = env("KUKU_PROXY")
            if proxy:
                self.session.proxies = {"http": proxy, "https": proxy}

    # ---------------- 底层请求

    def _headers(self, form=False):
        h = {
            "Accept": "application/json, text/plain, */*",
            "Referer": SIGN_PAGE,
            "Origin": BASE,
            "User-Agent": UA,
            "Cookie": self.cookie_header,
            "X-Requested-With": "XMLHttpRequest",
        }
        h["Content-Type"] = (
            "application/x-www-form-urlencoded;charset=UTF-8" if form
            else "application/json;charset=UTF-8"
        )
        return h

    def _raw(self, method, path, params=None, body=None, form=None):
        """body=JSON 体；form=表单体（两者互斥）。"""
        url = BASE + path
        if requests is not None:
            kwargs = {
                "params": params,
                "headers": self._headers(form is not None),
                "timeout": self.timeout,
            }
            if form is not None:
                kwargs["data"] = form
            else:
                kwargs["data"] = json.dumps(body if body is not None else {})
            resp = self.session.request(method, url, **kwargs)
            return resp.status_code, resp.text

        # 无 requests 时的 urllib 兜底
        import urllib.error
        import urllib.parse
        import urllib.request

        if params:
            url += "?" + urllib.parse.urlencode(params)
        if form is not None:
            payload = urllib.parse.urlencode(form).encode("utf-8")
        elif method.upper() == "GET":
            payload = None
        else:
            payload = json.dumps(body if body is not None else {}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers=self._headers(form is not None), method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "ignore")

    def call(self, method, path, params=None, body=None, form=None, retries=2):
        """返回 (ok, data_or_err)。

        网络层失败自动重试；HTTP 200 且 errno==0 视为成功。
        """
        last_err = ""
        for attempt in range(retries + 1):
            try:
                status, text = self._raw(method, path, params, body, form)
            except Exception as e:
                last_err = "网络异常: %s" % e
                if attempt < retries:
                    time.sleep(1.5 + attempt)
                    continue
                return False, {"errno": None, "show_msg": last_err}

            if status in (500, 502, 503, 504) and attempt < retries:
                time.sleep(1.5 + attempt)
                continue

            if status == 401 or status == 302:
                return False, {"errno": ERRNO_NOT_LOGIN, "show_msg": "HTTP %s，登录态失效" % status}

            try:
                data = json.loads(text)
            except Exception:
                last_err = "响应非 JSON(HTTP %s): %s" % (status, (text or "")[:120].replace("\n", " "))
                if attempt < retries:
                    time.sleep(1.0)
                    continue
                return False, {"errno": None, "show_msg": last_err}

            errno = data.get("errno")
            if errno is None:
                return False, {"errno": None, "show_msg": "缺少 errno: %s" % json.dumps(data, ensure_ascii=False)[:160]}
            try:
                errno_i = int(errno)
            except Exception:
                errno_i = -999
            if errno_i == 0:
                return True, data
            return False, data
        return False, {"errno": None, "show_msg": last_err}

    # ---------------- 业务步骤

    def load_conf(self, conf_key="kukuSignConf"):
        ok, data = self.call("GET", EP_CONF, params={"conf_key": conf_key, "psize": 60})
        if not ok:
            return {}
        rows = data.get("data") or []
        if not rows:
            return {}
        val = rows[0].get("conf_value") or {}
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except Exception:
                return {}
        return val if isinstance(val, dict) else {}

    def sync_activity_ids(self):
        """从活动配置同步 tid；配置接口挂了就保留默认值。"""
        conf = self.load_conf()
        if not conf:
            conf = self._load_conf_v2()
        tid = str(conf.get("activityTid") or "").strip()
        lottery = str(conf.get("lotteryTid") or "").strip()
        if tid and not env("KUKU_TID"):
            self.tid = tid
        if lottery and not env("KUKU_LOTTERY_TID"):
            self.lottery_tid = lottery
        return conf

    def _load_conf_v2(self):
        ok, data = self.call("GET", EP_CONF_V2, params={"conf_key": "kukuSignConf"})
        if not ok:
            return {}
        rows = data.get("data") or []
        if not rows:
            return {}
        val = rows[0].get("conf_value") or {}
        if isinstance(val, str):
            try:
                return json.loads(val)
            except Exception:
                return {}
        return val if isinstance(val, dict) else {}

    def homepage(self):
        return self.call("POST", EP_HOMEPAGE, params={"tid": self.tid}, body={})

    def signin(self):
        return self.call("POST", EP_SIGNIN, params={"tid": self.tid}, body={})

    def claim_reward(self, day):
        """领取连签奖励。

        实测（2026-10-27，未登录态对照）：POST 带 JSON body 时服务端在鉴权之前
        就返回 errno=2『参数错误』，说明 claimreward 的参数是从 query 读的，
        与具体数值无关；改成 GET + query(tid, day) 后立刻回落到鉴权错误(-6)，
        与签到/signin 行为一致，即参数通过校验。
        """
        return self.call("GET", EP_CLAIM, params={"tid": self.tid, "day": int(day)})

    def reward_tips(self):
        return self.call("POST", EP_REWARD_TIPS, params={"tid": self.tid}, body={})

    def lottery_count(self):
        """抽奖次数。实际是 form 请求，前端固定带 comp_ver=2。"""
        ok, data = self.call(
            "POST",
            EP_LOTTERY_INFO,
            body=None,
            form={"tid": self.lottery_tid, "action": "lottery_cnt", "comp_ver": "2"},
        )
        if ok:
            return data
        return {}

    def lottery_commit(self):
        params = {"tid": self.lottery_tid, "cnt": 1}
        ok, data = self.call("GET", EP_LOTTERY_COMMIT, params=params)
        if ok:
            return True, data
        # GET 被拦时退回 POST 表单（前端 bundle 里也用表单）
        return self.call(
            "POST",
            EP_LOTTERY_COMMIT,
            form={"tid": self.lottery_tid, "cnt": 1, "action": "lottery_cnt,view_list,used_cnt"},
        )


# ------------------------------------------------------------------ 业务编排

def errno_of(data):
    try:
        return int(data.get("errno"))
    except Exception:
        return None


def describe(data):
    msg = data.get("show_msg") or data.get("errmsg") or data.get("msg") or ""
    code = errno_of(data)
    if not msg:
        msg = "errno=%s" % code
    return msg, code


def fmt_rewards(award):
    """把 '网盘SVIP天卡x1' 之类的奖励串整理一下；拿不到就返回空串。"""
    if not award:
        return ""
    if isinstance(award, (list, tuple)):
        return "、".join(str(x) for x in award if x)
    return str(award)


def parse_today(home_data):
    """从 homepage 返回里抽取签到态。"""
    data = home_data.get("data") or {}
    return {
        "signed_today": bool(data.get("signed_today")),
        "continuous_days": int(data.get("continuous_days") or 0),
        "countdown": int(data.get("countdown") or 0),
        "next_days": int(data.get("next_reward_days") or 0),
        "next_award": data.get("next_reward_award") or "",
        "awards": data.get("continuous_awards") or [],
        "calendar": data.get("calendar") or [],
        "makeup": data.get("makeup_success_dates") or [],
    }


def run_account(cookie_header, idx):
    """返回 (state, message, exit_code)。state: ok / skip / fail"""
    cli = Kuku(cookie_header, timeout=env_int("KUKU_TIMEOUT", 20))

    conf = cli.sync_activity_ids()
    end_time = str(conf.get("endTime") or "").strip()
    if end_time and end_time < time.strftime("%Y-%m-%d %H:%M:%S"):
        return "skip", "签到活动已于 %s 结束" % end_time, EXIT_OK

    # 1) 查签到态
    ok, home = cli.homepage()
    state = None
    if not ok:
        msg, code = describe(home)
        if code in AUTH_ERRNOS:
            return "fail", "登录态失效(%s)：%s —— 请重新抓 Cookie" % (code, msg), EXIT_AUTH
        return "fail", "查询签到态失败：%s" % msg, EXIT_BIZ

    info = parse_today(home)
    already = info["signed_today"]
    if already:
        state = "ok"
        msg = "今日已签到（连签 %d 天）" % info["continuous_days"]
    else:
        # 2) 签到
        time.sleep(random.uniform(0.6, 1.6))  # 轻微抖动，别把签名请求打成一模一样
        ok2, res = cli.signin()
        if ok2:
            state = "ok"
            msg = "签到成功"
        else:
            m, code = describe(res)
            if "已签" in m or "重复" in m:
                state = "ok"
                msg = "今日已签到（接口确认）"
            elif code in AUTH_ERRNOS:
                return "fail", "签到鉴权失败(%s)：%s" % (code, m), EXIT_AUTH
            elif code in RISK_ERRNOS:
                hint = "风控要求 jsToken 签名" if code == ERRNO_JSTOKEN else "账号被判异常，需换设备/账号"
                return "fail", "签到被风控拦截(%s)：%s（%s）" % (code, m, hint), EXIT_BIZ
            else:
                return "fail", "签到失败(%s)：%s" % (code, m), EXIT_BIZ

        # 3) 重查一次，拿到连签天数用于领奖
        ok3, home2 = cli.homepage()
        if ok3:
            info = parse_today(home2)

    details = []
    if info["continuous_days"] > 0:
        details.append("连签 %d 天" % info["continuous_days"])
    if info["next_days"] > 0 and info["next_award"]:
        details.append("再签 %d 天拿 %s" % (info["next_days"], fmt_rewards(info["next_award"])))
    if info["makeup"]:
        details.append("补签生效：%s" % ",".join(str(x) for x in info["makeup"]))

    # 4) 连签达档领奖
    if is_on("KUKU_CLAIM_REWARD"):
        claimed = claim_tiers(cli, info, details)
        if claimed:
            pass

    # 5) 任务奖励回执（不改变签到结果，失败只记录）
    ok_t, tips = cli.reward_tips()
    if ok_t:
        td = tips.get("data") or {}
        svip = int(td.get("svip") or 0)
        point = int(td.get("kukuai_point") or 0)
        cnt = int(td.get("lottery_cnt") or 0)
        bits = []
        if svip:
            bits.append("网盘SVIP+%d天" % svip)
        if point:
            bits.append("积分+%d" % point)
        if cnt:
            bits.append("抽奖次数+%d" % cnt)
        if bits:
            details.append("任务奖励回报：" + "、".join(bits))

    # 6) 抽奖
    if is_on("KUKU_DRAW"):
        draw_all(cli, details)

    if details:
        msg = "%s（%s）" % (msg, "；".join(details))
    return state, msg, EXIT_OK


def claim_tiers(cli, info, details):
    """连签奖励：awards 里 {day, state}，state==1 且未领时有领取按钮。"""
    awards = info.get("awards") or []
    days = info.get("continuous_days") or 0
    got = []
    for item in awards:
        if not isinstance(item, dict):
            continue
        try:
            day = int(item.get("day") or 0)
            st = int(item.get("state") or 0)
        except Exception:
            continue
        if day <= 0:
            continue
        # state: 0 未解锁 / 1 可领 / 2 已领
        if st == 2 or day > days:
            continue
        if st not in (0, 1):
            continue
        ok, res = cli.claim_reward(day)
        if ok:
            got.append("连签%d天" % day)
        time.sleep(random.uniform(0.4, 0.9))
    if got:
        details.append("领取连签奖励：" + "、".join(got))
    return got


def draw_all(cli, details, max_draw=10):
    """把可用抽奖次数抽完（默认最多 10 次，防手滑）。"""
    info = cli.lottery_count()
    cnt = None
    if info:
        data = info.get("data")
        if isinstance(data, dict):
            for key in ("lottery_cnt", "count", "left_cnt", "remain_cnt"):
                if key in data:
                    try:
                        cnt = int(data.get(key))
                    except Exception:
                        cnt = None
                    break
    if cnt is None:
        cnt = 1  # 拿不到次数时试一次
    cnt = max(0, min(cnt, max_draw))
    if cnt == 0:
        return
    won = []
    for i in range(cnt):
        ok, res = cli.lottery_commit()
        if not ok:
            m, code = describe(res)
            details.append("抽奖停止：%s" % m)
            break
        prize = extract_prize(res)
        if prize:
            won.append(prize)
        time.sleep(random.uniform(0.5, 1.2))
    if won:
        details.append("抽奖获得：" + "、".join(won))
    elif cnt:
        details.append("已完成 %d 次抽奖" % cnt)


def extract_prize(res):
    """抽奖返回结构不固定，做宽松提取。"""
    data = res.get("data")
    cand = data if isinstance(data, list) else (data or {})
    names = []

    def walk(node):
        if isinstance(node, dict):
            for k in ("prize_name", "name", "goods_name", "title"):
                v = node.get(k)
                if isinstance(v, str) and v.strip():
                    names.append(v.strip())
                    return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(cand if cand else res)
    seen = []
    for n in names:
        if n not in seen:
            seen.append(n)
    return "、".join(seen[:3])


# ------------------------------------------------------------------ 可选：真浏览器模式

def run_headless(cookie_header):
    """用 Playwright 打开活动页，走真实前端逻辑签到（需要 playwright + chromium）。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "未安装 playwright，KUKU_HEADLESS=1 无法使用"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=UA)
            cookies = []
            for k, v in cookie_dict(cookie_header).items():
                cookies.append({
                    "name": k,
                    "value": v,
                    "domain": ".baidu.com",
                    "path": "/",
                })
            ctx.add_cookies(cookies)
            page = ctx.new_page()
            page.goto(SIGN_PAGE, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            btn = page.locator(".pc-sign-btn").first
            text = btn.inner_text(timeout=8000) if btn.count() else ""
            if "今日已签" in (text or ""):
                browser.close()
                return "ok", "今日已签到（浏览器确认）"
            if btn.count():
                btn.click()
                page.wait_for_timeout(3500)
                text2 = btn.inner_text(timeout=8000)
                browser.close()
                if "今日已签" in (text2 or ""):
                    return "ok", "签到成功（浏览器）"
                return "fail", "浏览器签到后状态未变更：%s" % text2
            browser.close()
            return "fail", "页面未找到签到按钮（可能未登录）"
    except Exception as e:
        return None, "浏览器模式异常: %s" % e


# ------------------------------------------------------------------ 通知

def _module_channel_count(module):
    try:
        fn = getattr(module, "add_notify_function", None)
        if callable(fn):
            return len(fn())
    except Exception:
        pass
    return -1


def _load_send_notify():
    here = os.path.dirname(os.path.abspath(__file__))
    dirs = [
        here,
        os.getcwd(),
        os.path.join(here, os.pardir),
        "/ql/data/scripts",
        "/ql/scripts",
        "/ql/data/scripts/function",
        "/ql/scripts/function",
    ]
    for d in dirs:
        try:
            d = os.path.abspath(d)
        except Exception:
            continue
        for name in ("sendNotify", "notify"):
            if not os.path.isfile(os.path.join(d, name + ".py")):
                continue
            if d not in sys.path:
                sys.path.insert(0, d)
            try:
                mod = __import__(name)
                if _module_channel_count(mod) == 0:
                    continue
                send = getattr(mod, "send", None)
                if send is not None:
                    return send
            except Exception:
                continue
    return None


def push_notify(title, content):
    try:
        import builtins

        api = getattr(builtins, "QLAPI", None)
        if api is not None:
            try:
                res = api.systemNotify({"title": title, "content": content})
                if not (isinstance(res, dict) and res.get("code") not in (None, 200)):
                    log("📣 通知已推送（QLAPI）")
                    return True
            except Exception:
                pass
    except Exception:
        pass
    try:
        send = _load_send_notify()
        if send is not None:
            send(title, content)
            log("📣 通知已推送（notify 模块）")
            return True
    except Exception as e:
        log("⚠️ 通知发送失败: %s" % e)
    return False


# ------------------------------------------------------------------ 主流程

def main():
    accounts = load_accounts()
    if not accounts:
        log("❌ 未配置 Cookie：请设置环境变量 KUKU_COOKIE（或 KUKU_COOKIES 配多账号）")
        log("   抓法：浏览器登录 kuku.baidu.com → F12 → Network → 任一请求的")
        log("   Request Headers → Cookie 整条复制（至少含 BDUSS）")
        sys.exit(EXIT_CONFIG)

    log("🔰 库库AI 自动签到开始，共 %d 个账号" % len(accounts))

    headless = is_on("KUKU_HEADLESS", "0")
    results = []
    worst = EXIT_OK
    ok = skip = fail = 0

    for idx, cookie_header in enumerate(accounts, 1):
        tag = account_tag(cookie_header, idx)
        try:
            if headless:
                state, msg = run_headless(cookie_header)
                code = EXIT_OK if state == "ok" else EXIT_BIZ
                if state is None:
                    state, code = "fail", EXIT_BIZ
            else:
                state, msg, code = run_account(cookie_header, idx)
        except Exception as e:
            state, msg, code = "fail", "异常: %s" % e, EXIT_BIZ

        if state == "ok":
            ok += 1
        elif state == "skip":
            skip += 1
        else:
            fail += 1
            if code == EXIT_AUTH or worst == EXIT_OK:
                worst = code

        icon = {"ok": "✅", "skip": "⏭️", "fail": "❌"}[state]
        log("%s [%s] %s" % (icon, tag, msg))
        results.append((tag, state, msg))

        if idx < len(accounts):
            time.sleep(random.uniform(2.0, 4.0))

    log("🏁 完成：成功 %d / 跳过 %d / 失败 %d / 共 %d" % (ok, skip, fail, len(accounts)))

    if is_on("KUKU_NOTIFY"):
        prefix = env("KUKU_NOTIFY_PREFIX")
        title = ("%s 库库AI签到" % prefix).strip()
        body = "\n".join(
            "%s [%s] %s" % ({"ok": "✅", "skip": "⏭️", "fail": "❌"}[s], t, m)
            for t, s, m in results
        )
        body += "\n----\n成功 %d / 跳过 %d / 失败 %d" % (ok, skip, fail)
        log("")
        log(body)
        push_notify(title, body)

    sys.exit(worst)


if __name__ == "__main__":
    main()
