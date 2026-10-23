# ql_scripts

自用的[青龙面板](https://github.com/whyour/qinglong)签到脚本合集，纯 Python，按站点分目录。

## 目录结构

```
ql_scripts/
├── qoder/                        # Qoder 每日领 100 Credits
│   ├── qoderCN_checkin.py        # 国内版（qoder.cn）
│   ├── qoderINTL_checkin.py      # 国际版（qoder.com）
│   └── test_checkin.py           # 离线单元测试（不联网）
└── workbuddy/                    # WorkBuddy 每日签到
    ├── workbuddy_checkin.py      # 签到主脚本
    ├── workbuddy_auth.py         # 凭证模块：刷新 AT / 轮换 RT / 落盘
    └── extract_tokens.py         # 本机一次性解密导出 AT + RT
```

## 青龙部署

### 1. 订阅仓库

「订阅管理」→ 新建订阅：

- 仓库地址：`https://github.com/silvermoon89/ql_scripts.git`
- 定时规则：`0 0 * * *`（仅用于拉取代码）
- 白名单：留空（默认拉取 `py` 文件）
- 黑名单：`test_.*\.py`（建议；否则 `test_checkin.py` 也会被当成任务脚本拉下来）

保存后运行一次，脚本会落到 `scripts/silvermoon89_ql_scripts/` 下。

> 仓库若是私有的，青龙订阅需要能免密拉取：把仓库设为公开，
> 或在面板里为 git 配置好凭据（`~/.git-credentials` / SSH key）。

### 2. 安装依赖

「依赖管理」→ Python3 → 新增：

```
requests
truststore
```

`truststore` 可选，仅在本地抓包代理替换证书链（`CERTIFICATE_VERIFY_FAILED`）时需要。

### 3. 建定时任务

三个脚本都在文件头写好了 `cron:` 与 `new Env(...)`，青龙订阅时勾选自动添加任务即可。
手动新建时命令为：

```bash
task silvermoon89_ql_scripts/qoder/qoderCN_checkin.py
task silvermoon89_ql_scripts/qoder/qoderINTL_checkin.py
task silvermoon89_ql_scripts/workbuddy/workbuddy_checkin.py
```

---

## qoder

每天领取 100 Credits，每轮限领一次、错过不补。

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `QODERCN_PAT` | 国内版必填 | qoder.cn 的 PAT（`pt-` 开头），多账号用 `&` 分隔 |
| `QODER_PAT` | 国际版必填 | qoder.com 的 PAT（`pt-` 开头），多账号用 `&` 分隔 |
| `QODER_MACHINE_TOKEN` | 国际版建议 | 设备身份，缺省会导致「已签到但没领到」 |
| `QODER_MACHINE_TYPE` | 国际版建议 | 同上 |
| `QODER_MACHINE_CODE` | 国际版建议 | 同上 |

PAT 在账号的 integrations 页创建：

- 国内版：https://qoder.cn/account/integrations
- 国际版：https://qoder.com/account/integrations

国际版的三项设备身份在**已登录桌面端的 Windows 机器**上生成一次即可，可跨系统复用：

```bash
set QODER_PRINT_IDENTITY=1
python qoderINTL_checkin.py
```

把打印出的三行原样填进青龙环境变量。

> 不要改到 10:00 之前执行，否则会落进前一天的窗口，重试余量会被压缩。
> 也不要一天跑多次，同一窗口内重复领取没有收益。

离线自测：

```bash
cd qoder
python -m unittest test_checkin -v
```

---

## workbuddy

WorkBuddy 每日签到。AT 约 28 天到期且无法续期，RT 每次刷新都会轮换并顺延 30 天，
所以脚本以 RT 为主凭证，并把轮换后的新 RT 落盘。

### 1. 本机导出凭证（只做一次）

新版桌面端（5.8.x）把 token 存成 AES-256-GCM 信封
（`{"$wbEncrypted":1,"envelope":"..."}`），直接复制字段必然 401。
`extract_tokens.py` 借客户端内建模块 `electron_browser_workbuddy_storage`
在本机解出明文，密钥不出本机、不联网。

在**已登录 WorkBuddy 的电脑**上：

```bash
cd workbuddy
python extract_tokens.py
```

生成 `workbuddy_tokens.json`；只想拿 RT 用 `--print-rt`。

### 2. 配置凭证

**方式一：纯环境变量（最省事）**

| 变量 | 说明 |
| --- | --- |
| `WB_REFRESH_TOKEN` | 单个账号的 RT |
| `WB_REFRESH_TOKENS` | 多账号，逗号分隔，支持 `uid:rt` |
| `WB_TOKEN_FILE` | 凭证文件路径，建议 `/ql/data/config/workbuddy_tokens.json` |

首次运行会自动用 RT 换 AT，并把轮换后的新 RT 写进 `WB_TOKEN_FILE`，之后以文件为准。

**方式二：上传凭证文件（推荐）**

把 `workbuddy_tokens.json` 上传到青龙持久化目录，并设：

```
WB_TOKEN_FILE=/ql/data/config/workbuddy_tokens.json
```

> RT 每次刷新都会轮换，旧 RT 会逐步失效，所以必须有一块可写、
> 且不会被订阅覆盖的持久化目录。不要放进 `scripts/` 下。
> 青龙的「环境变量」是只读的，脚本无法把新 RT 写回面板，这也是文件不能省的原因。

### 3. 其他可选变量

| 变量 | 说明 |
| --- | --- |
| `WB_ACCESS_TOKEN` / `WB_ACCESS_TOKENS` | 兼容旧配置的 AT（会过期，不推荐） |
| `WB_USER_ID` | 单账号时强制指定 `X-User-Id` |
| `WB_DOMAIN` | `X-Domain` 头，默认 `www.workbuddy.cn` |
| `WB_API_BASE` | 默认 `https://copilot.tencent.com` |
| `WB_PROXY` | 代理，如 `http://127.0.0.1:7897` |
| `WB_REFRESH_MARGIN` | AT 提前刷新秒数，默认 3600 |
| `WB_NOTIFY_PREFIX` | 通知标题前缀，多容器时用于区分 |
| `QINGLONG_NOTIFY` | 设 `0` 关闭通知摘要 |

### 退出码

| 码 | 含义 |
| --- | --- |
| `0` | 成功（含今日已签到） |
| `1` | 业务失败 |
| `2` | 未读到凭证 |
| `3` | 凭证失效，需重新登录并重新导出 |

---

## 安全说明

- `.pat`、`workbuddy_tokens.json` 等同账号密钥，已在 `.gitignore` 中排除，**不要提交**。
- 只与本机 / 官方接口通信，凭证不上传到任何第三方。
- 仓库是公开的话，提交前先跑一遍 `git status`，确认没有凭证文件混进来。

## 许可

MIT
