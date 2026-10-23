#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本机一次性导出 WorkBuddy 凭证（AT + RT），生成青龙用的凭证文件。

为什么需要这一步：
  新版 WorkBuddy 桌面端把 auth.accessToken / auth.refreshToken 包成了
  AES-256-GCM 信封（$wbEncrypted），密钥只存在于客户端进程内。
  直接复制字段只会得到一串 envelope，贴到青龙里必然 401。

做法：
  借用 WorkBuddy 自带的内建模块 electron_browser_workbuddy_storage
  取出 atRestSecretKey，再按客户端同样的 AAD 规则解开 token。
  全程在本机完成，密钥不出本机、不联网。

用法（在已登录 WorkBuddy 的电脑上运行）：
  python extract_tokens.py                 # 生成 workbuddy_tokens.json
  python extract_tokens.py --print-rt      # 只打印 RT
  python extract_tokens.py --out /ql/data/config/workbuddy_tokens.json

refreshToken 等同账号密钥，请勿外传。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time


def _utf8_stdout():
    """Windows 控制台默认 GBK，emoji/中文容易报编码错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_utf8_stdout()


def find_workbuddy():
    """定位 WorkBuddy 主程序（解密必须用它自带的 node 运行时）。"""
    env = (os.environ.get("WORKBUDDY_EXE") or "").strip()
    if env and os.path.isfile(env):
        return env

    candidates = []
    if sys.platform == "win32":
        roots = [
            os.environ.get("LOCALAPPDATA", ""),
            os.environ.get("PROGRAMFILES", ""),
            os.environ.get("PROGRAMFILES(X86)", ""),
            r"D:\software",
            r"C:\software",
        ]
        for root in roots:
            if not root:
                continue
            candidates.append(os.path.join(root, "WorkBuddy", "WorkBuddy.exe"))
            candidates.append(os.path.join(root, "WorkBuddyAI", "WorkBuddyAI.exe"))
            candidates.append(os.path.join(root, "Programs", "WorkBuddy", "WorkBuddy.exe"))
    elif sys.platform == "darwin":
        candidates.append("/Applications/WorkBuddy.app/Contents/MacOS/WorkBuddy")
    else:
        for name in ("workbuddy", "WorkBuddy"):
            found = shutil.which(name)
            if found:
                candidates.append(found)

    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return ""


def auth_info_path():
    env = (os.environ.get("WORKBUDDY_AUTH_FILE") or "").strip()
    if env:
        return env
    if sys.platform == "win32":
        return os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            "CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info",
        )
    if sys.platform == "darwin":
        return os.path.expanduser(
            "~/Library/Application Support/CodeBuddyExtension/"
            "Data/Public/auth/workbuddy-desktop.info"
        )
    return os.path.expanduser(
        "~/.config/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info"
    )


# 在 WorkBuddy 的 node 运行时里执行：取密钥 -> 解信封 -> 写出明文 token
DECRYPT_SCRIPT = r"""
const fs = require('fs');
const crypto = require('crypto');

const authPath = process.argv[2];
const outPath = process.argv[3];

const storage = process._linkedBinding('electron_browser_workbuddy_storage');
const payload = JSON.parse(storage.loggerGet());
const key = crypto.createHash('sha256').update(payload.atRestSecretKey, 'utf8').digest();
const keyId = crypto.createHash('sha256').update(key).digest('hex').slice(0, 16);

function u32(n) { const b = Buffer.allocUnsafe(4); b.writeUInt32BE(n); return b; }
function lp(s) { const b = Buffer.from(s, 'utf8'); return Buffer.concat([u32(b.length), b]); }

// sym-v1 + field framing 的 AAD 构造，与客户端 buildAuthenticatedContextAad 一致
function aad() {
  return Buffer.concat([
    Buffer.from('WB-AAD\0', 'ascii'),
    Buffer.from([1]),
    lp('WBEV1'),
    lp('sym-v1'),
    u32(1),
    lp(keyId),
    Buffer.from([2]),
    Buffer.from([0]),
    Buffer.from([0]),
  ]);
}

function openValue(value, label) {
  if (!value) return '';
  if (typeof value === 'string') return value;
  if (!value.envelope) throw new Error(label + ': unknown shape');
  const env = JSON.parse(Buffer.from(value.envelope, 'base64').toString('utf8'));
  if (env.keyId !== keyId) throw new Error(label + ': keyId mismatch');
  const d = crypto.createDecipheriv(
    'aes-256-gcm', key, Buffer.from(env.nonce, 'base64'), { authTagLength: 16 }
  );
  d.setAAD(aad());
  d.setAuthTag(Buffer.from(env.authTag, 'base64'));
  return Buffer.concat([
    d.update(Buffer.from(env.ciphertext, 'base64')), d.final(),
  ]).toString('utf8');
}

const info = JSON.parse(fs.readFileSync(authPath, 'utf8'));
const auth = info.auth || {};
const account = info.account || {};

// 客户端存的是毫秒时间戳，统一转成秒
const toSeconds = (v) => {
  const n = Number(v) || 0;
  return n > 100000000000 ? Math.floor(n / 1000) : n;
};
let nickname = '';
try {
  nickname = account.nickname ? openValue(account.nickname, 'nickname') : '';
} catch (e) {
  nickname = '';
}

fs.writeFileSync(outPath, JSON.stringify({
  accessToken: openValue(auth.accessToken, 'accessToken'),
  refreshToken: openValue(auth.refreshToken, 'refreshToken'),
  expiresAt: toSeconds(auth.expiresAt),
  refreshExpiresAt: toSeconds(auth.refreshExpiresAt),
  domain: auth.domain || '',
  uid: account.uid || '',
  nickname: nickname,
}), 'utf8');

process.stdout.write('OK');
"""


def decrypt_with_workbuddy(exe, auth_file):
    fd, script = tempfile.mkstemp(suffix=".js")
    os.close(fd)
    fd, out_file = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        with open(script, "w", encoding="utf-8") as f:
            f.write(DECRYPT_SCRIPT)
        env = dict(os.environ)
        env["ELECTRON_RUN_AS_NODE"] = "1"
        proc = subprocess.run(
            [exe, script, auth_file, out_file],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "").strip()[:800])
        with open(out_file, "r", encoding="utf-8") as f:
            return json.load(f)
    finally:
        for path in (script, out_file):
            try:
                os.unlink(path)
            except OSError:
                pass


def atomic_write_json(path, data):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def main():
    parser = argparse.ArgumentParser(description="从本机 WorkBuddy 导出 RT/AT")
    parser.add_argument("--out", default="", help="输出凭证文件路径")
    parser.add_argument("--auth-file", default="", help="workbuddy-desktop.info 路径")
    parser.add_argument("--exe", default="", help="WorkBuddy 可执行文件路径")
    parser.add_argument("--print-rt", action="store_true", help="只打印 RT")
    args = parser.parse_args()

    auth_file = args.auth_file or auth_info_path()
    if not os.path.isfile(auth_file):
        print("❌ 找不到登录态文件：%s" % auth_file)
        print("   请先启动 WorkBuddy 并完成登录。")
        return 2

    exe = args.exe or find_workbuddy()
    if not exe:
        print("❌ 找不到 WorkBuddy 主程序，请用 --exe 指定。")
        print("   解密依赖客户端内建模块，无法用纯 Python 完成。")
        return 2

    try:
        raw = decrypt_with_workbuddy(exe, auth_file)
    except Exception as e:
        print("❌ 解密失败：%s" % e)
        return 1

    at, rt = raw.get("accessToken") or "", raw.get("refreshToken") or ""
    if not at or not rt:
        print("❌ 解密结果缺少 token，请确认客户端已登录。")
        return 1

    if args.print_rt:
        print(rt)
        return 0

    now = int(time.time())
    account = {
        "uid": raw.get("uid", ""),
        "nickname": raw.get("nickname", ""),
        "domain": raw.get("domain") or "www.workbuddy.cn",
        "accessToken": at,
        "refreshToken": rt,
        "expiresAt": int(raw.get("expiresAt") or 0),
        "refreshExpiresAt": int(raw.get("refreshExpiresAt") or 0),
        "disabled": False,
        "updatedAt": now,
    }

    target = args.out or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "workbuddy_tokens.json"
    )
    atomic_write_json(target, {"version": 1, "accounts": [account]})

    print("✅ 已导出凭证")
    print("   uid      : %s" % (account["uid"] or "-"))
    print("   账号     : %s" % (account["nickname"] or "-"))
    print("   凭证文件 : %s" % target)
    print("   AT 剩余  : %.1f 小时" % (max(0, account["expiresAt"] - now) / 3600))
    print("   RT 剩余  : %.1f 天" % (max(0, account["refreshExpiresAt"] - now) / 86400))
    print("")
    print("下一步：把该文件放进青龙持久化目录（推荐 /ql/data/config/），")
    print("        或把 RT 填进 WB_REFRESH_TOKEN 作为首次引导。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

