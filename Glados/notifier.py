#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通知模块：把签到结果推到青龙面板配置的通知渠道。

三级策略：
  1. 新版面板内置 QLAPI.systemNotify（直接用「系统设置-通知设置」的渠道配置）
  2. 社区通用 sendNotify.py（多候选路径查找，兼容老面板）
  3. 都不可用（本地运行 / 渠道未配）时只提示，不重复打印报告

Glados.py 的 main() 在调用本模块前已把完整报告 print 到 stdout，
所以降级路径不重复输出正文，保证青龙日志不出现两份报告。
"""

import os
import sys


def _load_send_notify():
    """多候选路径查找青龙的 sendNotify.py；找不到返回 None，绝不中断任务。"""
    try:
        from sendNotify import send  # type: ignore

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
            from sendNotify import send  # type: ignore

            return send
        except Exception:
            continue
    return None


def send(title, content):
    """推送一条通知，返回是否走了真实推送渠道（QLAPI / sendNotify）。"""
    try:
        QLAPI.systemNotify({"title": title, "content": content})  # noqa: F821
        return True
    except Exception:
        pass

    sender = _load_send_notify()
    if sender is not None:
        try:
            sender(title, content)
            return True
        except Exception as exc:
            print("⚠️ sendNotify 推送失败: %s: %s" % (exc.__class__.__name__, exc))
            return False

    print("⚠️ 未找到可用通知渠道（QLAPI / sendNotify.py 均不可用），结果仅输出到日志")
    return False
