#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通知模块：把签到结果推到青龙面板配置的通知渠道。

策略（按可判定性排序）：
  1. QLAPI.systemNotify：走面板「系统设置→通知设置」的渠道，失败抛异常可判定
  2. QLAPI.notify：走 __ql_notify__（环境变量渠道），渠道全空会静默空转，
     调用前先探测
  3. 官方 notify.py / 社区 sendNotify.py（多候选路径查找，同样先探测渠道）
  4. 都不可用时打提示，不重复打印报告

Glados.py 的 main() 在调用本模块前已把完整报告 print 到 stdout，
所以降级路径不重复输出正文，保证青龙日志不出现两份报告。
"""

import os
import sys


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


def _load_send_notify():
    """多候选路径查找青龙官方通知模块；找不到或没配渠道时返回 None。

    新版青龙部署的是 notify.py（scripts 根目录与各订阅目录各一份），旧版/
    社区脚本约定是 sendNotify.py。订阅脚本落在 scripts/<仓库>/ 子目录下，
    sys.path[0] 只指向脚本自身所在目录。渠道一个都没配的模块直接跳过。
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


def _qlapi_send(title, content):
    """新版青龙经 sitecustomize 给 Python 注入 builtins.QLAPI。

    systemNotify 走面板「系统设置→通知设置」（失败抛异常，可判定）；
    notify 走 __ql_notify__（环境变量渠道，静默空转风险，先探测）。
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


def send(title, content):
    """推送一条通知，返回是否走了真实推送渠道。"""
    try:
        if _qlapi_send(title, content):
            print("📣 通知已推送（QLAPI）")
            return True
    except Exception:
        pass

    sender = _load_send_notify()
    if sender is not None:
        try:
            sender(title, content)
            print("📣 通知已推送（notify 模块）")
            return True
        except Exception as exc:
            print("⚠️ 通知模块推送失败: %s: %s" % (exc.__class__.__name__, exc))

    print("⚠️ 通知未发出：未检测到可用渠道")
    print("   新版面板在「系统设置→通知设置」配置；旧版面板需在环境变量配置 notify.py 渠道变量")
    return False
