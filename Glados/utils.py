#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Glados.py 的日志与时间工具。

原仓库的 utils 较重，这里只补 Glados.py 实际用到的五个函数，
并顺带处理 Windows 控制台 GBK 编码下的中文/emoji 输出问题。
"""

import sys
from datetime import datetime, timedelta, timezone

try:  # Python 3.9+；精简镜像缺 tzdata 时退回固定 UTC+8
    from zoneinfo import ZoneInfo

    TZ_CN = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - 仅个别精简镜像触发
    TZ_CN = timezone(timedelta(hours=8))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def log_info(message):
    print("ℹ️ %s" % message)


def log_success(message):
    print("✅ %s" % message)


def log_warning(message):
    print("⚠️ %s" % message)


def log_error(message):
    print("❌ %s" % message)


def beijing_time_str():
    return datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
