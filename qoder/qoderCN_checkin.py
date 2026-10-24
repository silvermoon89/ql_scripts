# -*- coding: utf8 -*-

"""
cron: 5 10 * * *
new Env('Qoder CN 每日签到');

Qoder CN「每天领 100 Credits」自动签到（青龙面板 / Python3）。

环境变量：
  QODERCN_PAT  必填。Qoder CN 个人访问令牌（PAT，pt- 开头）。
               到 https://qoder.cn/account/integrations 创建后填入。
               多账号用 & 或换行分隔，例如：QODERCN_PAT=pt-账号1&pt-账号2
  （命名与 Qoder 官方 CLI 的 QODERCN_PERSONAL_ACCESS_TOKEN 对齐）

调度说明（cron: 5 10 * * *，每一项都有理由，改动前先读）：
  活动窗口为「每日 10:00 (UTC+8) 开放新一轮，持续至次日 10:00 前」，每轮限领一次
  100 Credits、自领取起 30 天有效、错过不补。
  定在 10:05 而不是 10:00：给上游切窗口留 5 分钟缓冲，避免正好卡在切换瞬间、
  拿到上一轮的已领状态；同时离窗口开始最近，当天 10:05 失败到次日 10:00 前
  还有接近 24 小时的重试余量。
  ⚠️ 不要改到 10:00 之前：那会落到**前一天**的窗口里，领的是「再过几小时就关闭」的
     那一轮，失败后的重试余量会被压缩（极端情况下不足 2 小时）。漏领不补。
  ⚠️ 不要改成一天跑两次以上：同一窗口内多跑没有额外收益，只会多打接口。

鉴权链路（实测 2026-10-18，Qoder CN 0.4.8 / openapi.qoder.com.cn）：
  QODERCN_PAT (pt-…, 永久)
    --POST /api/v1/jobToken/exchange  {"personal_token": "<PAT>"}-->
  jobToken (jt-…, 24h)  --Authorization: Bearer-->  业务接口

  ⚠️ PAT 不能直接当 Bearer，必须先 exchange 成 jt-（每次运行重新 exchange，
     不落盘缓存：cron 日频，jt- 24h 必然过期）。
  ⚠️ Cosy-ClientType 有两处不同值，写反会静默拿不到数据：
       exchange      用 "5"
       业务接口(含 campaigns/claim/usage) 用 "10"
     campaigns 少带 Cosy-ClientType: 10 时仍返回 200，但 campaigns 数组为空。
  ⚠️ 业务接口只需最小头集：Accept / Authorization / User-Agent / Cosy-ClientType: 10。
     不需要 Cosy-Machine* 设备头，exchange 之外也不需要 Cosy-Version。

接口清单（全部本机实测）：
  POST /api/v1/jobToken/exchange                  PAT 换 jobToken
  GET  /sash/api/v1/me/campaigns                  活动列表（含每日领 100）
  POST /sash/api/v1/me/campaigns/{campaignId}/claim  领取（body {}，幂等，replayed）
  GET  /sash/api/v2/me/usage                     余额（userQuota / addOnQuota）

领取必须用 campaignId（UUID），不是 campaignKey（act-YYYYMMDD-NNN）。
签到赠送的 100 credits 落在 addOnQuota（资源包），不在 userQuota。

窗口判定（实测 2026-10-23）：活动条目带 startAt / endAt（epoch 秒），这才是
当前窗口的权威来源；campaignKey 的数字段是活动创建日，可能比开窗日早两天
（如 act-20261020-993 实际覆盖 10-22 10:00 ~ 10-23 09:59），不能拿来判日期。
"""

import os
import re
import sys
from datetime import datetime, timedelta, timezone

# 可选依赖：部分机器（企业代理 / Reqable 等本地抓包代理）会用自签根证书替换
# 证书链，此时 requests 默认的 certifi 校验会 CERTIFICATE_VERIFY_FAILED，
# 而系统证书库是可信的。truststore 让 Python 复用系统证书库；缺失时不影响运行。
try:
    import truststore

    truststore.inject_into_ssl()
except Exception:  # pragma: no cover - 取决于运行环境
    truststore = None

import requests

try:  # Python 3.9+；精简镜像缺 tzdata 时退回固定 UTC+8
    from zoneinfo import ZoneInfo

    TZ_CN = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - 仅个别精简镜像触发
    TZ_CN = timezone(timedelta(hours=8))

# Windows 控制台默认 GBK，中文/emoji 日志会花屏或抛 UnicodeEncodeError；
# 青龙容器本就是 UTF-8，这里是跨平台的无害兜底（非 Windows 专有 API）。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

API = "https://openapi.qoder.com.cn"

LOG_PREFIX = "[qoder]"
TASK_TITLE = "Qoder CN 每日签到"
USER_AGENT = "Qoder"
EXCHANGE_CLIENT_TYPE = "5"  # exchange 专用
OPENAPI_CLIENT_TYPE = "10"  # 业务接口专用（漏了 campaigns 静默为空）
COSY_VERSION = "1.0.1"  # 仅 exchange 需要
TIMEOUT = 20

PATH_EXCHANGE = "/api/v1/jobToken/exchange"
PATH_CAMPAIGNS = "/sash/api/v1/me/campaigns"
PATH_USAGE = "/sash/api/v2/me/usage"

WINDOW_RESET_HOUR = 10  # 每天 10:00 (UTC+8) 切换新一轮，10:00 前仍属前一天窗口
CAMPAIGN_KEY_RE = re.compile(r"^act-(\d{8})(?:-|$)")

MSG_AUTH_FAILED = (
    "❌ Qoder PAT 已失效，请重新登录 qoder.cn/account/integrations "
    "重新创建并更新 QODERCN_PAT"
)

__all__ = [
    "QoderError",
    "parse_pats",
    "pick_claimable",
    "ordered_claimable",
    "has_claimed_today",
    "current_campaign_key",
    "campaign_window",
    "is_campaign_active",
    "mask",
]


class QoderError(Exception):
    """站点专属错误：type 决定通知文案与下游分流。

    type: 'config' | 'auth_failed' | 'network' | 'schema_changed' | 'api_error'
    """

    def __init__(self, type_, message):
        super().__init__(message)
        self.name = "QoderError"
        self.type = type_
        self.message = message


def log(message):
    print("%s %s" % (LOG_PREFIX, message))


# --------------------------------------------------------------------------
# 纯函数（导出，便于将来加测试）
# --------------------------------------------------------------------------


def parse_pats(raw):
    """按 & / 换行切分多个 PAT，去空白、去重并保持顺序。"""
    if not raw:
        return []
    parts = str(raw).replace("\r", "\n").replace("&", "\n").split("\n")
    pats = []
    for part in parts:
        part = part.strip()
        if part and part not in pats:
            pats.append(part)
    return pats


def current_campaign_key(now=None):
    """当前领取窗口对应的 campaignKey 日期前缀（act-YYYYMMDD）。

    窗口每天 10:00 (UTC+8) 开启、次日 10:00 前关闭，所以 10:00 之前仍属于
    前一天开启的那一轮；campaignKey 的日期按窗口开启日理解。
    """
    local = (now or datetime.now(timezone.utc)).astimezone(TZ_CN)
    day = local.date()
    if local.hour < WINDOW_RESET_HOUR:
        day -= timedelta(days=1)
    return "act-%s" % day.strftime("%Y%m%d")


def pick_claimable(campaigns):
    """可领判定：actionType == CLAIM_BENEFIT 且 claimStatus == CLAIMABLE 且 campaignId 非空。

    只按 claimStatus 判会误领 VIEW_DETAILS；只按 actionType 判会误领
    ACHIEVEMENT_NOT_COMPLETED。两类实测都存在，两个条件必须同时满足。
    """
    picked = []
    for item in campaigns or []:
        if not isinstance(item, dict):
            continue
        if item.get("actionType") != "CLAIM_BENEFIT":
            continue
        if item.get("claimStatus") != "CLAIMABLE":
            continue
        if not item.get("campaignId"):
            continue
        picked.append(item)
    return picked


def _as_datetime(value):
    """把 epoch 秒（int/数字字符串）或 ISO 时间转成带时区的 datetime。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return _as_datetime(int(text))
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def campaign_window(item):
    """活动生效区间 (startAt, endAt)，UTC；缺失的一端为 None。"""
    if not isinstance(item, dict):
        return None, None
    return _as_datetime(item.get("startAt")), _as_datetime(item.get("endAt"))


def is_campaign_active(item, now=None):
    """当前是否处于活动区间内。

    实测活动条目带 startAt/endAt（epoch 秒），这才是窗口的权威来源；
    campaignKey 里的日期只是活动创建日（例如 10-22 开窗的活动 key 是
    act-20261020-993），不能用来判断当天窗口。
    """
    current = now or datetime.now(timezone.utc)
    start, end = campaign_window(item)
    if start is not None and current < start:
        return False
    if end is not None and current > end:
        return False
    return True


def _matches_window(item, now):
    """是否属于当前窗口：有时间戳按时间戳，没有才退回 campaignKey（兜底）。"""
    start, end = campaign_window(item)
    if start is not None or end is not None:
        return is_campaign_active(item, now)
    key = item.get("campaignKey") if isinstance(item, dict) else None
    if not key:
        return True
    match = CAMPAIGN_KEY_RE.match(str(key))
    if not match:
        return True
    return "act-%s" % match.group(1) == current_campaign_key(now)


def ordered_claimable(campaigns, now=None):
    """可领列表去重（按 campaignId），并把当前窗口的条目排到前面。

    接口可能对同一 campaignId 返回多条；不去重会重复请求、把 replayed 记成
    多次「已领」。当前窗口优先，是为了万一存在过期活动时先领当天那份。
    """
    current = now or datetime.now(timezone.utc)
    seen = set()
    same_window, other_window = [], []
    for item in pick_claimable(campaigns):
        cid = item["campaignId"]
        if cid in seen:
            continue
        seen.add(cid)
        if not is_campaign_active(item, current):
            continue  # 已过期或还没开始，领了也只会报错
        (same_window if _matches_window(item, current) else other_window).append(item)
    return same_window + other_window


def has_claimed_today(campaigns, now=None):
    """当前窗口是否已签到：CLAIM_BENEFIT + CLAIMED 且活动仍在生效区间内。

    只看 CLAIMED 会把昨天（或更早）的已领活动算成今天已签到，导致当天漏领；
    因此按 startAt/endAt 判断该条目是否覆盖当前时刻。
    """
    current = now or datetime.now(timezone.utc)
    for item in campaigns or []:
        if not isinstance(item, dict):
            continue
        if item.get("actionType") != "CLAIM_BENEFIT":
            continue
        if item.get("claimStatus") != "CLAIMED":
            continue
        if _matches_window(item, current):
            return True
    return False


def mask(token):
    """脱敏：只保留前缀与尾 4 位，绝不回显完整 PAT / jobToken。"""
    if not token:
        return "(空)"
    token = str(token)
    if len(token) <= 12:
        return "***"
    return "%s***%s" % (token[:6], token[-4:])


# --------------------------------------------------------------------------
# 网络层
# --------------------------------------------------------------------------


def make_session(job_token=None):
    """最小头集。exchange 阶段不挂 Authorization；换到 jt- 后再挂业务头。"""
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "User-Agent": USER_AGENT})
    if job_token:
        session.headers.update(
            {
                "Authorization": "Bearer %s" % job_token,
                "Cosy-ClientType": OPENAPI_CLIENT_TYPE,
            }
        )
    return session


def request_json(
    session,
    method,
    path,
    payload=None,
    extra_headers=None,
    where="",
    auth_statuses=(401, 403),
):
    """统一请求 + 错误分类：auth_failed / network / schema_changed / api_error。"""
    url = API + path
    try:
        resp = session.request(
            method, url, json=payload, headers=extra_headers, timeout=TIMEOUT
        )
    except requests.exceptions.SSLError as exc:
        # 与普通断网区分开：多半是代理/企业网关的根证书不在 certifi 里
        hint = "若本机开了抓包/代理软件（如 Reqable、Fiddler），请 pip install truststore 后重试"
        if truststore is None:
            hint += "，或把其根证书导入系统后重启脚本"
        raise QoderError(
            "network",
            "❌ TLS 握手失败（%s）：%s。%s" % (where, exc.__class__.__name__, hint),
        )
    except requests.RequestException as exc:
        raise QoderError(
            "network",
            "❌ 网络请求失败（连不上 openapi.qoder.com.cn）：%s %s"
            % (exc.__class__.__name__, where),
        )
    if resp.status_code in auth_statuses:
        raise QoderError("auth_failed", MSG_AUTH_FAILED)
    if resp.status_code >= 400:
        # 不回显响应体，避免把服务端的 invalid/expired 之类词带进非凭证类文案
        raise QoderError(
            "api_error",
            "❌ Qoder 接口返回异常状态 %s（%s）" % (resp.status_code, where),
        )
    try:
        return resp.json()
    except ValueError:
        raise QoderError(
            "schema_changed",
            "⚠️ Qoder 接口结构未识别（schema_changed）：%s 返回非 JSON 内容" % where,
        )


def exchange_job_token(session, pat):
    """PAT -> jobToken。响应字段为 snake_case 的 token（不是 accessToken）。"""
    doc = request_json(
        session,
        "POST",
        PATH_EXCHANGE,
        payload={"personal_token": pat},
        extra_headers={
            "Content-Type": "application/json",
            "Cosy-ClientType": EXCHANGE_CLIENT_TYPE,
            "Cosy-Version": COSY_VERSION,
        },
        where="jobToken 交换",
        # exchange 的唯一变量就是 PAT，400 一并按凭证失效处理
        auth_statuses=(400, 401, 403),
    )
    token = doc.get("token") if isinstance(doc, dict) else None
    if not token or not isinstance(token, str):
        raise QoderError(
            "schema_changed",
            "⚠️ Qoder 接口结构未识别（schema_changed）：jobToken 响应缺少 token 字段（%s）"
            % _json_keys_from(doc),
        )
    return token


def _json_keys_from(doc):
    if isinstance(doc, dict):
        return "响应键：%s" % ", ".join(sorted(doc.keys()))
    return "响应类型：%s" % type(doc).__name__


def fetch_campaigns(session):
    doc = request_json(session, "GET", PATH_CAMPAIGNS, where="活动列表")
    if not isinstance(doc, dict) or not isinstance(doc.get("campaigns"), list):
        raise QoderError(
            "schema_changed",
            "⚠️ Qoder 接口结构未识别（schema_changed）：活动列表缺少 campaigns 字段（%s）"
            % _json_keys_from(doc),
        )
    return doc


def claim(session, campaign_id):
    """POST /sash/api/v1/me/campaigns/{campaignId}/claim，body 恰好 {}。"""
    path = "%s/%s/claim" % (PATH_CAMPAIGNS, campaign_id)
    doc = request_json(
        session,
        "POST",
        path,
        payload={},
        extra_headers={"Content-Type": "application/json"},
        where="领取签到 %s" % campaign_id,
    )
    # 幂等重放（replayed）可能只回 replayed 而没有 grantId，此时权益早已发放，
    # 属于成功而不是 schema 变更。
    if isinstance(doc, dict) and (doc.get("grantId") or doc.get("replayed")):
        return doc
    raise QoderError(
        "schema_changed",
        "⚠️ Qoder 接口结构未识别（schema_changed）：领取响应既无 grantId 也无 replayed（%s）"
        % _json_keys_from(doc),
    )


def fetch_usage(session):
    doc = request_json(session, "GET", PATH_USAGE, where="余额查询")
    if not isinstance(doc, dict) or not isinstance(doc.get("qoderUsage"), dict):
        raise QoderError(
            "schema_changed",
            "⚠️ Qoder 接口结构未识别（schema_changed）：余额响应缺少 qoderUsage（%s）"
            % _json_keys_from(doc),
        )
    return doc


def _quota_text(quota):
    if not isinstance(quota, dict):
        return "不可用"
    total = quota.get("total")
    remaining = quota.get("remaining")
    unit = quota.get("unit") or "credits"
    if total is None and remaining is None:
        return "不可用"
    return "%s/%s %s" % (
        "?" if remaining is None else remaining,
        "?" if total is None else total,
        unit,
    )


def format_balance(doc):
    """余额口径：签到发的 100 进 addOnQuota（资源包），userQuota 是套餐额度，两者都要看。"""
    usage = doc.get("qoderUsage") if isinstance(doc, dict) else None
    if not isinstance(usage, dict):
        return "余额：未获取到（qoderUsage 缺失）"
    return "余额：userQuota %s（套餐额度）；addOnQuota %s（资源包，签到发放进这里）" % (
        _quota_text(usage.get("userQuota")),
        _quota_text(usage.get("addOnQuota")),
    )


def _claim_amount(doc, campaign):
    """优先取领取响应里的 benefit.amount，回落到活动条目的 benefit.amount。"""
    for source in (doc, campaign):
        benefit = source.get("benefit") if isinstance(source, dict) else None
        if isinstance(benefit, dict):
            amount = benefit.get("amount")
            if isinstance(amount, (int, float)):
                return amount
    return 0


def _claim_message(claimed, granted, replayed, errors):
    """组装领取文案：新增额度、replayed、失败条数分开说，避免把重放算成新增。"""
    if errors and not claimed:
        head = "❌ 签到失败"
    elif errors:
        head = "⚠️ 部分签到成功"
    else:
        head = "✅ 签到成功"

    notes = []
    if not claimed and errors:
        pass  # 失败明细本身就是全部信息，不再补「无新增额度」
    elif granted:
        notes.append("+%s CREDITS" % granted)
    elif claimed and replayed == claimed:
        notes.append("本次无新增额度")
    else:
        notes.append("接口未返回新增额度")
    if replayed:
        notes.append("%d 条 replayed，此前已发放、不重复计入" % replayed)
    if errors:
        notes.append("%d 条失败：%s" % (len(errors), "；".join(errors[:3])))
        if len(errors) > 3:
            notes.append("另 %d 条省略" % (len(errors) - 3))
    return "%s（%s）" % (head, "，".join(notes))


# --------------------------------------------------------------------------
# 编排
# --------------------------------------------------------------------------


def run_account(pat, index):
    """跑单个账号，内部捕获所有异常，绝不向调用方抛出。"""
    name = mask(pat)
    tag = "账号%d(%s)" % (index, name)
    result = {"name": name, "ok": False, "message": "❌ 签到失败", "balance": None}

    try:
        session = make_session()
        job_token = exchange_job_token(session, pat)
        log("%s jobToken 交换成功（有效期 24h）" % tag)

        session = make_session(job_token)
        doc = fetch_campaigns(session)
        campaigns = doc.get("campaigns") or []
        claimable = ordered_claimable(campaigns)
        log("%s 活动列表：共 %d 条，可领 %d 条" % (tag, len(campaigns), len(claimable)))

        ok = True
        if claimable:
            claimed = 0
            granted = 0
            replayed = 0
            errors = []
            for campaign in claimable:
                try:
                    res = claim(session, campaign["campaignId"])
                except QoderError as exc:
                    if exc.type == "auth_failed":
                        raise  # 凭证失效后其它请求也不会成功，直接中断本账号
                    # 单个活动失败不阻塞后面的活动：窗口内多领一条是一条
                    errors.append(exc.message)
                    log("%s 领取失败：%s" % (tag, exc.message))
                    continue
                except Exception as exc:
                    errors.append("未知错误：%s: %s" % (exc.__class__.__name__, exc))
                    log("%s 领取失败：%s" % (tag, errors[-1]))
                    continue
                claimed += 1
                if res.get("replayed"):
                    replayed += 1
                else:
                    granted += _claim_amount(res, campaign)
            message = _claim_message(claimed, granted, replayed, errors)
            ok = not errors
        elif has_claimed_today(campaigns):
            message = "✅ 今天已签到（无需操作）"
        else:
            # 一个都没领到就不能算成功：漏领不补，静默成功会让监控失灵
            message = (
                "❌ 当前没有可领取的签到活动（%s UTC+8）；活动可能已下线或接口未返回"
                % datetime.now(timezone.utc).astimezone(TZ_CN).strftime("%Y-%m-%d %H:%M")
            )
            ok = False

        result["ok"] = ok
        result["message"] = message
        log("%s %s" % (tag, message))

        try:
            balance = format_balance(fetch_usage(session))
        except QoderError as exc:
            balance = "余额查询失败：%s" % exc.message
            # 余额查询失败本身不影响已完成的签到，但**凭证类**失败必须让账号判失败。
            # 否则会出现「标题写成功、正文却带 已失效/重新登录」的自相矛盾通知：
            # 通知进失效 Bot，exit code 却错报 0。
            if exc.type == "auth_failed":
                result["ok"] = False
                result["message"] = "%s（余额查询阶段）" % exc.message
        log("%s %s" % (tag, balance))
        result["balance"] = balance
    except QoderError as exc:
        result["ok"] = False
        result["message"] = exc.message
        log("%s %s" % (tag, exc.message))
    except Exception as exc:  # 单账号未知异常隔离，不中断其余账号
        result["ok"] = False
        result["message"] = "❌ 签到失败（未知错误）：%s: %s" % (
            exc.__class__.__name__,
            exc,
        )
        log("%s %s" % (tag, result["message"]))

    return result


def build_notification(results):
    ok_count = sum(1 for item in results if item.get("ok"))
    lines = ["%s：成功 %d / 共 %d" % (TASK_TITLE, ok_count, len(results)), ""]
    for item in results:
        lines.append("【%s】%s" % (item.get("name"), item.get("message")))
        if item.get("balance"):
            lines.append("    %s" % item["balance"])
    return TASK_TITLE, "\n".join(lines)


# --------------------------------------------------------------------------
# 通知（QLAPI.systemNotify 优先，渠道探测，失败降级 print，绝不因通知中断任务）
# --------------------------------------------------------------------------


def _module_channel_count(module):
    """notify.py 系模块在渠道全空时 send() 会静默什么都不发（无异常、无输出）。

    能探测就数一下可用渠道；探测不了返回 -1，调用方按"可用"对待。
    """
    try:
        counter = getattr(module, "add_notify_function", None)
        if callable(counter):
            return len(counter())
    except Exception:
        pass
    return -1


def load_send_notify():
    """多候选路径查找青龙官方通知模块（新版 notify.py，旧版/社区 sendNotify.py）。

    渠道一个都没配的模块直接跳过——调用它只会静默空转。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidate_dirs = [
        here,
        os.getcwd(),
        os.path.join(here, os.pardir),
        os.path.join("/ql", "data", "scripts"),
        os.path.join("/ql", "scripts"),
        os.path.join("/ql", "data", "scripts", "function"),
        os.path.join("/ql", "scripts", "function"),
    ]
    for directory in candidate_dirs:
        try:
            directory = os.path.abspath(directory)
        except Exception:
            continue
        for module_name in ("sendNotify", "notify"):
            if not os.path.isfile(os.path.join(directory, module_name + ".py")):
                continue
            if directory not in sys.path:
                sys.path.insert(0, directory)
            try:
                module = __import__(module_name)
                if _module_channel_count(module) == 0:
                    continue
                send = getattr(module, "send", None)
                if send is not None:
                    return send
            except Exception:
                continue
    return None


def qlapi_send(title, content):
    """新版青龙经 sitecustomize 给 Python 注入 builtins.QLAPI。

    systemNotify 走面板「系统设置→通知设置」的渠道（失败抛异常，可判定）；
    notify 走 __ql_notify__（环境变量渠道，渠道全空会静默空转，先探测）。
    """
    import builtins

    api = getattr(builtins, "QLAPI", None)
    if api is None:
        return False
    try:
        res = api.systemNotify({"title": title, "content": content})
        if not (isinstance(res, dict) and res.get("code") not in (None, 200)):
            return True
    except Exception:
        pass
    try:
        import __ql_notify__  # noqa: F401  面板注入的环境变量渠道模块

        if _module_channel_count(__ql_notify__) == 0:
            return False
        api.notify(title, content)
        return True
    except Exception:
        return False


def send_notification(title, content):
    try:
        if qlapi_send(title, content):
            log("📣 通知已推送（QLAPI）")
            return True
    except Exception:
        pass
    try:
        send = load_send_notify()
        if send is not None:
            send(title, content)
            log("📣 通知已推送（notify 模块）")
            return True
    except Exception as exc:
        log("⚠️ 青龙通知发送失败：%s" % exc.__class__.__name__)
    print("\n%s\n%s" % (title, content))
    print("⚠️ 通知未发出：未检测到可用渠道，请在「系统设置→通知设置」配置")
    return False


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def main():
    pats = parse_pats(os.getenv("QODERCN_PAT"))
    if not pats:
        message = (
            "❌ 未配置环境变量 QODERCN_PAT\n"
            "请到 https://qoder.cn/account/integrations 创建个人访问令牌（PAT，pt- 开头），\n"
            "然后在青龙面板的「环境变量」中添加：\n"
            "  QODERCN_PAT=pt-你的令牌\n"
            "多账号用 & 或换行分隔，例如：QODERCN_PAT=pt-账号1&pt-账号2"
        )
        log(message)
        send_notification(TASK_TITLE, message)
        return 1

    log("开始签到：共 %d 个账号" % len(pats))
    results = [run_account(pat, index) for index, pat in enumerate(pats, 1)]

    title, content = build_notification(results)
    send_notification(title, content)

    return 0 if all(item.get("ok") for item in results) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        fallback = "❌ 发生异常:执行失败\n\n%s: %s" % (exc.__class__.__name__, exc)
        log(fallback)
        send_notification(TASK_TITLE, fallback)
        sys.exit(1)
