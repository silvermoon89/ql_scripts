#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 每日自动签到（青龙面板版）

cron: 13 8 * * *
new Env('WorkBuddy 每日签到');

特性：
  - 以 refreshToken(RT) 为主凭证，自动换 AT 并落盘轮换后的新 RT
  - 幂等：今日已签直接跳过，不重复请求
  - 多账号：WB_REFRESH_TOKENS 逗号分隔，支持 "uid:rt"
  - 退出码分级：0 成功 / 1 业务失败 / 2 未配置 / 3 凭证失效需重新登录

环境变量见 workbuddy_auth.py 文件头说明。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import workbuddy_auth as auth  # noqa: E402

STATUS_EP = "/v2/billing/meter/checkin-activity-status"
CHECKIN_EP = "/v2/billing/meter/daily-checkin"

# 业务上代表"今天已经签过了"，属于正常结果
ALREADY_CHECKED_CODES = {10001, 1001, 11216}

EXIT_OK, EXIT_BIZ, EXIT_CONFIG, EXIT_AUTH = 0, 1, 2, 3


def check_status(acc, store_path):
    status, text = auth.call(acc, "POST", STATUS_EP, path_of_store=store_path)
    body = auth.parse_json(text) or {}
    if status == 200 and body.get("code") == 0:
        return True, body.get("data") or {}, 0, ""
    hint, reason = auth.classify_failure(status, text)
    return False, {}, hint, reason


def do_checkin(acc, store_path):
    status, text = auth.call(acc, "POST", CHECKIN_EP, path_of_store=store_path)
    body = auth.parse_json(text) or {}
    if status == 200 and body.get("code") == 0:
        return True, body.get("data") or {}
    return False, body


def checkin_one(acc, store_path):
    """返回 (状态, 消息, 退出码)。"""
    data_ok, data, hint, reason = check_status(acc, store_path)
    if not data_ok:
        code = EXIT_AUTH if hint == EXIT_AUTH else EXIT_BIZ
        return "fail", reason, code

    if data.get("active") is False:
        return "skip", "签到活动未开启", EXIT_OK

    if data.get("today_checked_in"):
        return "ok", "今日已签到（连续 %s 天，总积分 %s）" % (
            data.get("streak_days", 0),
            data.get("total_credits", 0),
        ), EXIT_OK

    ok, res = do_checkin(acc, store_path)
    if ok:
        credit = res.get("credit") or res.get("today_credit") or 0
        streak = res.get("streak_days", 0)
        return "ok", "签到成功 +%s 积分（连续 %s 天）" % (credit, streak), EXIT_OK

    code = res.get("code")
    msg = res.get("msg") or res.get("message") or ""
    if code in ALREADY_CHECKED_CODES or "已签" in msg or "already" in msg.lower():
        return "ok", "今日已签到（接口确认）", EXIT_OK
    if code == 1002:
        return "skip", "当前账号不满足签到条件", EXIT_OK
    if code == 1003:
        return "skip", "签到活动已结束", EXIT_OK
    return "fail", "签到失败 code=%s msg=%s" % (code, msg), EXIT_BIZ


def notify_lines(results):
    lines = []
    for tag, state, msg in results:
        icon = {"ok": "✅", "skip": "⏭️", "fail": "❌"}[state]
        lines.append("%s [%s] %s" % (icon, tag, msg))
    return lines


def main():
    store_path = auth.default_token_file()
    accounts = auth.load_accounts()
    if not accounts:
        auth.exit_with(EXIT_CONFIG)

    auth.log("🔰 WorkBuddy 自动签到开始，共 %d 个账号（凭证文件：%s）" % (len(accounts), store_path))

    results = []
    worst = EXIT_OK
    ok = fail = skip = 0

    for idx, acc in enumerate(accounts, 1):
        tag = auth.account_tag(acc, idx)
        try:
            state, msg, code = checkin_one(acc, store_path)
        except Exception as e:
            state, msg, code = "fail", "异常: %s" % e, EXIT_BIZ

        if state == "ok":
            ok += 1
        elif state == "skip":
            skip += 1
        else:
            fail += 1
            # 凭证失效优先于普通业务失败
            if code == EXIT_AUTH or worst == EXIT_OK:
                worst = code

        icon = {"ok": "✅", "skip": "⏭️", "fail": "❌"}[state]
        auth.log("%s [%s] %s" % (icon, tag, msg))
        results.append((tag, state, msg))

        if idx < len(accounts):
            time.sleep(1)

    auth.log("🏁 完成：成功 %d / 跳过 %d / 失败 %d / 共 %d" % (ok, skip, fail, len(accounts)))
    auth.emit_notify_summary("WorkBuddy 签到", notify_lines(results))

    if worst:
        auth.exit_with(worst)


if __name__ == "__main__":
    main()

