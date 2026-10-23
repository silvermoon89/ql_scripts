# -*- coding: utf8 -*-

"""
cron: 5 10 * * *
new Env('Qoder 国际版每日签到');

Qoder 国际版「每天领 100 Credits」自动签到（青龙面板 / Python3）。

环境变量：
  QODER_PAT  必填。Qoder 国际版个人访问令牌（PAT，pt- 开头）。
             到 https://qoder.com/account/integrations 创建后填入。
             多账号用 & 或换行分隔，例如：QODER_PAT=pt-账号1&pt-账号2
  （命名与 Qoder 官方 CLI 的 QODER_PERSONAL_ACCESS_TOKEN 对齐）

  QODER_MACHINE_TOKEN / QODER_MACHINE_TYPE / QODER_MACHINE_CODE
             设备身份，强烈建议配置。国际版活动列表按设备定向下发：缺这三项时
             接口仍返回 200，但「每日领 100 Credits」会被静默过滤，只剩其他
             成长活动，结果是天天报「已签到」却一分没领。
             取值：在已登录桌面端的 Windows 机器上执行
               QODER_PRINT_IDENTITY=1 python3 qoderINTL_checkin.py
             把打印出的三行原样填入青龙环境变量即可（可跨系统使用，之后不再
             依赖桌面端）。不配置时脚本会尝试调用本机 runtime-info.exe。

调度说明（cron: 5 10 * * *，每一项都有理由，改动前先读）：
  活动窗口为「每日 10:00 (UTC+8) 开放新一轮，持续至次日 10:00 前」，每轮限领一次
  100 Credits、自领取起 30 天有效、错过不补。
  定在 10:05 而不是 10:00：给上游切窗口留 5 分钟缓冲，避免正好卡在切换瞬间、
  拿到上一轮的已领状态；同时离窗口开始最近，当天 10:05 失败到次日 10:00 前
  还有接近 24 小时的重试余量。
  ⚠️ 不要改到 10:00 之前：那会落到**前一天**的窗口里，领的是「再过几小时就关闭」的
     那一轮，失败后的重试余量会被压缩（极端情况下不足 2 小时）。漏领不补。
  ⚠️ 不要改成一天跑两次以上：同一窗口内多跑没有额外收益，只会多打接口。

鉴权链路（国际版网关 openapi.qoder.sh，路径与国内版一致）：
  QODER_PAT (pt-…, 永久)
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
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from glob import glob

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

API = "https://openapi.qoder.sh"

LOG_PREFIX = "[qoder-intl]"
TASK_TITLE = "Qoder 国际版每日签到"
USER_AGENT = "Qoder"
EXCHANGE_CLIENT_TYPE = "5"  # exchange 专用
OPENAPI_CLIENT_TYPE = "10"  # 业务接口专用（漏了 campaigns 静默为空）
COSY_VERSION = "1.0.1"  # 仅 exchange 需要；实测服务端不校验该值
TIMEOUT = 20

PATH_EXCHANGE = "/api/v1/jobToken/exchange"
PATH_CAMPAIGNS = "/sash/api/v1/me/campaigns"
PATH_USAGE = "/sash/api/v2/me/usage"

WINDOW_RESET_HOUR = 10  # 每天 10:00 (UTC+8) 切换新一轮，10:00 前仍属前一天窗口
CAMPAIGN_KEY_RE = re.compile(r"^act-(\d{8})(?:-|$)")
DAILY_BENEFIT_AMOUNT = 100  # 每日签到活动的 Credits 数量，用于与其他活动区分

DEVICE_ENV_VARS = ("QODER_MACHINE_TOKEN", "QODER_MACHINE_TYPE", "QODER_MACHINE_CODE")
_IDENTITY_CACHE = {}

MSG_AUTH_FAILED = (
    "❌ Qoder 国际版 PAT 已失效，请重新登录 qoder.com/account/integrations "
    "重新创建并更新 QODER_PAT"
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
    "is_daily_campaign",
    "device_identity",
    "identity_headers",
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
    """可领取的每日签到活动：去重（按 campaignId），当前窗口的排到前面。

    接口可能对同一 campaignId 返回多条；不去重会重复请求、把 replayed 记成
    多次「已领」。当前窗口优先，是为了万一存在过期活动时先领当天那份。
    国际版列表里还混有其他成长活动（如 1024/2048 Credits 礼包），只处理
    每日 100 这一条，避免脚本去领与签到无关的权益。
    """
    current = now or datetime.now(timezone.utc)
    seen = set()
    same_window, other_window = [], []
    for item in pick_claimable(campaigns):
        if not is_daily_campaign(item):
            continue
        cid = item["campaignId"]
        if cid in seen:
            continue
        seen.add(cid)
        if not is_campaign_active(item, current):
            continue  # 已过期或还没开始，领了也只会报错
        (same_window if _matches_window(item, current) else other_window).append(item)
    return same_window + other_window


def has_claimed_today(campaigns, now=None):
    """当前窗口的每日签到是否已领：只认每日 100 活动，且仍在生效区间内。

    国际版列表同时返回其他成长活动（1024/2048 Credits），它们长期 CLAIMED；
    不加金额过滤会把「已领过别的礼包」误判成「今天已签到」，直接跳过当天领取。
    """
    current = now or datetime.now(timezone.utc)
    for item in campaigns or []:
        if not isinstance(item, dict):
            continue
        if not is_daily_campaign(item):
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
# 设备身份（国际版必需）
# --------------------------------------------------------------------------


def _find_runtime_info():
    """定位桌面端自带的风控桥 runtime-info.exe（按 Qoder 安装目录约定查找）。"""
    patterns = [
        os.path.join(os.path.expanduser("~"), ".qoder", ".bin", "umid-*", "runtime-info.exe"),
        os.path.join(os.path.expanduser("~"), ".qoder-cn", ".bin", "umid-*", "runtime-info.exe"),
    ]
    for pattern in patterns:
        for path in sorted(glob(pattern), reverse=True):
            if os.path.isfile(path):
                return path
    return None


def device_identity():
    """取真实设备身份（machineToken/Type/Code）。

    国际版的活动列表按设备身份定向下发：不带这些头时接口仍返回 200，但
    「每日领取 100 Credits」会被静默过滤掉（只留其他成长活动）。因此国际版
    不能用空身份；优先用环境变量，其次调用桌面端自带的 runtime-info。
    """
    if _IDENTITY_CACHE:
        return _IDENTITY_CACHE

    env = [os.getenv(name, "").strip() for name in DEVICE_ENV_VARS]
    if all(env):
        _IDENTITY_CACHE.update(dict(zip(("machineToken", "machineType", "machineCode"), env)))
        return _IDENTITY_CACHE

    exe = _find_runtime_info()
    if exe:
        try:
            proc = subprocess.run(
                [exe, "prod"],
                capture_output=True,
                text=True,
                timeout=25,
            )
            doc = json.loads(proc.stdout or "{}")
        except Exception:
            doc = {}
        token = doc.get("machineToken")
        if token and doc.get("machineType") and doc.get("machineCode"):
            _IDENTITY_CACHE.update(
                {
                    "machineToken": token,
                    "machineType": doc["machineType"],
                    "machineCode": doc["machineCode"],
                }
            )
            return _IDENTITY_CACHE
    return {}


def identity_headers(identity):
    """把设备身份转成请求头；缺失时返回空字典（活动列表会被上游过滤）。"""
    if not identity:
        return {}
    return {
        "Cosy-MachineToken": identity["machineToken"],
        "Cosy-MachineType": identity["machineType"],
        "Cosy-MachineCode": identity["machineCode"],
    }


def is_daily_campaign(item):
    """是否「每日领 100 Credits」活动；靠 benefit.amount 区分，避免误判其他活动。"""
    if not isinstance(item, dict):
        return False
    benefit = item.get("benefit")
    return isinstance(benefit, dict) and benefit.get("amount") == DAILY_BENEFIT_AMOUNT


# --------------------------------------------------------------------------
# 网络层
# --------------------------------------------------------------------------


def make_session(job_token=None, extra_headers=None):
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
    if extra_headers:
        session.headers.update(extra_headers)
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
            "❌ 网络请求失败（连不上 openapi.qoder.sh）：%s %s"
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
    # 被风控拦截时 HTTP 仍是 200，且响应里照样带 grantId，只有 status /
    # failureCode 说明没发：同设备多账号会命中 SAME_PERSON_ALREADY_CLAIMED。
    if isinstance(doc, dict) and doc.get("status") == "BLOCKED":
        reason = doc.get("failureCode") or "BLOCKED"
        hint = ""
        if reason == "SAME_PERSON_ALREADY_CLAIMED":
            hint = "；该设备/同一人本轮已领过，官方按人去重"
        raise QoderError(
            "api_error",
            "❌ 领取被拒绝（%s）%s" % (reason, hint),
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

        identity = device_identity()
        if identity:
            log("%s 设备身份已就绪（%s…）" % (tag, identity["machineCode"][:6]))
        else:
            log(
                "%s ⚠️ 未取得设备身份：每日 100 活动会被上游静默过滤，"
                "请配置 QODER_MACHINE_TOKEN/TYPE/CODE" % tag
            )
        session = make_session(job_token, identity_headers(identity))
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
            if identity:
                message = (
                    "❌ 当前没有可领取的每日 100 活动（%s UTC+8）；"
                    "活动可能已下线或本轮已结束"
                    % datetime.now(timezone.utc).astimezone(TZ_CN).strftime("%Y-%m-%d %H:%M")
                )
            else:
                message = (
                    "❌ 活动列表未包含每日 100（%s UTC+8）：缺少设备身份，"
                    "上游会静默过滤该活动；请配置 QODER_MACHINE_TOKEN/TYPE/CODE"
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
# 通知（多候选路径查找 sendNotify，失败降级 print，绝不因通知中断任务）
# --------------------------------------------------------------------------


def load_send_notify():
    try:
        from sendNotify import send

        return send
    except Exception:
        pass

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
        if not os.path.isfile(os.path.join(directory, "sendNotify.py")):
            continue
        if directory not in sys.path:
            sys.path.insert(0, directory)
        try:
            from sendNotify import send

            return send
        except Exception:
            continue
    return None


def send_notification(title, content):
    try:
        send = load_send_notify()
        if send is not None:
            send(title, content)
            return True
    except Exception as exc:
        log("⚠️ 青龙通知发送失败：%s" % exc.__class__.__name__)
    print("\n%s\n%s" % (title, content))
    return False


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def main():
    if os.getenv("QODER_PRINT_IDENTITY", "").strip() in ("1", "true", "yes"):
        identity = device_identity()
        if not identity:
            log("❌ 未取得设备身份：请在 Windows 桌面端安装目录下运行，或手动设置三个 QODER_MACHINE_* 变量")
            return 1
        log("设备身份（填入青龙环境变量，可跨系统运行）：")
        for name, key in zip(DEVICE_ENV_VARS, ("machineToken", "machineType", "machineCode")):
            print("%s=%s" % (name, identity[key]))
        return 0

    pats = parse_pats(os.getenv("QODER_PAT"))
    if not pats:
        message = (
            "❌ 未配置环境变量 QODER_PAT\n"
            "请到 https://qoder.com/account/integrations 创建个人访问令牌（PAT，pt- 开头），\n"
            "然后在青龙面板的「环境变量」中添加：\n"
            "  QODER_PAT=pt-你的令牌\n"
            "多账号用 & 或换行分隔，例如：QODER_PAT=pt-账号1&pt-账号2"
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
