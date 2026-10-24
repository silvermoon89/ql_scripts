# ql_scripts

自用的[青龙面板](https://github.com/whyour/qinglong)签到脚本合集，纯 Python，按站点分目录。

## 目录结构

```
ql_scripts/
├── qoder/                        # Qoder 每日领 100 Credits
│   ├── qoderCN_checkin.py        # 国内版（qoder.cn）
│   └── qoderINTL_checkin.py      # 国际版（qoder.com）
├── workbuddy/                    # WorkBuddy 每日签到
│   ├── workbuddy_checkin.py      # 签到主脚本
│   ├── workbuddy_auth.py         # 凭证模块：刷新 AT / 轮换 RT / 落盘
│   └── extract_tokens.py         # 本机导出凭证（AT / RT）
└── Glados/                       # GLaDOS / Railgun 每日签到
    ├── Glados.py                 # 签到主脚本
    ├── utils.py                  # 日志与时间工具
    └── notifier.py               # 通知推送（QLAPI / sendNotify）
```

## 青龙部署

### 1. 订阅仓库

「订阅管理」→ 新建订阅：

- 仓库地址：`https://github.com/silvermoon89/ql_scripts.git`
- 定时规则：`0 0 * * *`（仅用于拉取代码）
- 白名单：`checkin\.py|Glados\.py`（只有四个签到入口会生成定时任务）
- 依赖文件：`workbuddy_auth\.py|notifier\.py|utils\.py`（helper 模块只复制、不生成任务）
- 黑名单：留空

> 白名单留空的话，青龙会把拉下来的**所有** py 文件（包括 workbuddy_auth.py、
> utils.py 这类被 import 的辅助模块）都建成定时任务——「依赖文件」字段
> 就是为这种情况准备的：匹配它的文件会被复制到脚本目录供 import，
> 但不会出现在任务列表里。改完订阅后重新运行一次订阅，多余任务会被自动
> 删除（订阅的「自动删除任务」默认开启）；没开的话手动删掉即可。

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

四个脚本都在文件头写好了 `cron:` 与 `new Env(...)`，青龙订阅时勾选自动添加任务即可。
手动新建时命令为：

```bash
task silvermoon89_ql_scripts/qoder/qoderCN_checkin.py
task silvermoon89_ql_scripts/qoder/qoderINTL_checkin.py
task silvermoon89_ql_scripts/workbuddy/workbuddy_checkin.py
task silvermoon89_ql_scripts/Glados/Glados.py
```

### 4. 通知推送

青龙**不会**在任务成功/失败时自动推送，需要配好渠道、脚本显式调用。渠道有两种配法，
任一生效即可；注意部分版本里两者是**分开的**——面板「测试」能收到、任务却不推送时，
换另一种配法：

1. **面板设置**：「系统设置」→「通知设置」→ 选择通知方式（Telegram / 钉钉 / 飞书 /
   企业微信 / Bark / PushPlus / ntfy / 邮件等）并填好参数（脚本经 `QLAPI.systemNotify` 直达）。
2. **环境变量**：按青龙官方 notify.py 的渠道变量名在「环境变量」里配置，
   如 `TG_BOT_TOKEN` + `TG_USER_ID`、`BARK_PUSH`、`PUSH_PLUS_TOKEN` 等。

脚本每次运行结束都会推送结果摘要，成功、跳过、失败一目了然；推送有没有走通，
看任务日志末尾的 `📣` / `⚠️` 提示。全部渠道不可用时只打日志，不影响任务退出码。

不想收通知时：workbuddy 设 `QINGLONG_NOTIFY=0`；qoder / Glados 每次运行都会推送（含成功）。

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

---

## workbuddy

WorkBuddy 每日签到。AT 约 28 天到期且无法续期，RT 每次刷新都会轮换并顺延 30 天，
所以脚本以 RT 为主凭证，并把轮换后的新 RT 落盘。

### 1. 获取 refreshToken

在**已登录 WorkBuddy 的电脑**上运行：

```bash
cd workbuddy
python extract_tokens.py --print-rt
```

打印出的 refreshToken 填进下面的环境变量即可，其余用法见脚本文件头注释。

### 2. 配置环境变量

| 变量 | 说明 |
| --- | --- |
| `WB_REFRESH_TOKEN` | 单个账号的 RT |
| `WB_REFRESH_TOKENS` | 多账号，逗号分隔，支持 `uid:rt` |
| `WB_TOKEN_FILE` | 凭证落盘路径，默认青龙持久化目录，一般不用设 |

> RT 每次刷新都会轮换，旧 RT 会逐步失效。脚本会把轮换后的新 RT 自动写入
> `WB_TOKEN_FILE` 指向的持久化路径（默认青龙 config 目录），之后以落盘文件
> 为准；青龙环境变量是只读的、回写不了，所以这块持久化目录不能省。

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
| `QINGLONG_NOTIFY` | 设 `0` 关闭通知推送（含日志摘要） |

### 退出码

| 码 | 含义 |
| --- | --- |
| `0` | 成功（含今日已签到） |
| `1` | 业务失败 |
| `2` | 未读到凭证 |
| `3` | 凭证失效，需重新登录并重新导出 |

---

## Glados

GLaDOS / Railgun 每日签到，同一套 API 同时支持 glados.cloud 与 railgun.info，
先探测 Cookie 归属域名再签到，遇 device-mismatch 自动切换 User-Agent 重试。
cron `0 0 * * *`。

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `GLADOS_COOKIE` | 必填 | 网页登录后的 Cookie（含 `koa:sess…`），多账号用 `&` 或换行分隔 |
| `GLADOS_USER_AGENT` | 可选 | 自定义 UA；签到接口校验登录设备平台，默认桌面 Chrome |
| `GLADOS_DOMAINS` | 可选 | 要尝试的域名，默认 `glados.cloud` 与 `railgun.info` 都探测 |

退出码：`0` 全部成功（含今日已签到），`1` 存在失败（含未配置 Cookie、异常中断）。

---

## 免责声明

- 本仓库及脚本**仅供个人学习与技术研究使用，禁止用于任何商业用途**。
- 使用本仓库脚本所产生的一切风险与后果由使用者自行承担，与作者无关。
- 凭证、Cookie 等同账号密钥，请只配置在青龙环境变量中，切勿提交到仓库或外传。

## 许可

MIT
