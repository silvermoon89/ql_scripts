#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通知模块：把签到结果推到青龙面板配置的通知渠道。

四级策略：
  1. 新版面板经 sitecustomize 注入的 QLAPI.notify（直接用「系统设置-通知设置」
     的渠道配置）
  2. 青龙官方 notify.py（新版部署在 scripts 根目录与各订阅目录）
  3. 社区通用 sendNotify.py（兼容旧面板）
  4. 都不可用（本地运行 / 渠道未配）时只提示，不重复打印报告

Glados.py 的 main() 在调用本模块前已把完整报告 print 到 stdout，
所以降级路径不重复输出正文，保证青龙日志不出现两份报告。
"""

import os
import sys


def _load_send_notify():
    """多候选路径查找青龙官方通知模块；找不到返回 None，绝不中断任务。

    新版青龙部署的是 notify.py（scripts 根目录与各订阅目录各一份），旧版/
    社区脚本约定是 sendNotify.py，两个名字都找。订阅脚本落在
    scripts/<仓库>/ 子目录下，sys.path[0] 只指向脚本自身所在目录。
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
                send = getattr(module, "send", None)
                if send is not None:
                    return send
            except Exception:
                continue
    return None


def _qlapi_send(title, content):
    """新版青龙给 Python 注入 builtins.QLAPI：notify(title, content)；
    个别版本兼容 systemNotify(dict)。注入不存在时返回 False。"""
    import builtins

    api = getattr(builtins, "QLAPI", None)
    if api is None:
        return False
    try:
        api.notify(title, content)
        return True
    except Exception:
        pass
    try:
        api.systemNotify({"title": title, "content": content})
        return True
    except Exception:
        pass
    return False


def send(title, content):
    """推送一条通知，返回是否走了真实推送渠道（QLAPI / 通知模块）。"""
    try:
        if _qlapi_send(title, content):
            return True
    except Exception:
        pass

    sender = _load_send_notify()
    if sender is not None:
        try:
            sender(title, content)
            return True
        except Exception as exc:
            print("⚠️ 通知模块推送失败: %s: %s" % (exc.__class__.__name__, exc))
            return False

    print("⚠️ 未找到可用通知渠道（QLAPI / notify.py / sendNotify.py 均不可用），结果仅输出到日志")
    return False
