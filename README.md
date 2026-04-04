# PKU Treehole Monitor 🔔

PKU 树洞论坛关键词监控工具。实时监控 [PKU Treehole](https://treehole.pku.edu.cn/web/) 最新帖子，当帖子内容匹配指定关键词时，通过 **邮件** 和 **macOS 系统通知** 发出提醒。

## 功能特性

- ✅ 自动通过 PKU IAAA 系统登录（支持 Token 自动续期）
- ✅ **TOTP 自动验证** — 配置 TOTP 密钥后，App 手机令牌验证全自动完成
- ✅ 定时轮询最新帖子
- ✅ 支持多关键词 AND/OR 匹配模式
- ✅ 邮件提醒（支持 HTML 美化邮件）
- ✅ macOS 系统通知 + 提示音
- ✅ 去重机制（避免重复提醒）
- ✅ 状态持久化（重启后不会重复提醒已通知帖子）
- ✅ **Rich 终端** — 使用 rich 库美化终端输出

## 快速开始

### 1. 安装依赖

```bash
uv sync
```

### 2. 编辑配置文件
将 `config.exp.yaml` 重命名为 `config.yaml`，然后编辑 `config.yaml` 填写你的信息：

```yaml
# PKU 登录凭据
pku:
  username: "你的学号"
  password: "你的密码"

# 关键词设置
keywords:
  mode: "AND"          # AND: 全部匹配 | OR: 任一匹配
  list:
    - "关键词1"
    - "关键词2"

# 邮件设置（以 QQ 邮箱为例）
email:
  enabled: true
  smtp_server: "smtp.qq.com"
  smtp_port: 465
  use_ssl: true
  sender: "你的QQ邮箱@qq.com"
  password: "QQ邮箱授权码"
  receiver: "接收提醒的邮箱"
```

### 3. 运行

```bash
uv run python monitor.py
```

或者使用命令行入口：

```bash
uv run treehole-monitor
```

## 配置说明

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `monitor.interval_seconds` | 轮询间隔（秒） | `60` |
| `monitor.max_pages` | 每次检查的最大页数 | `2` |
| `keywords.mode` | 匹配模式：`AND` / `OR` | `AND` |
| `email.enabled` | 是否启用邮件提醒 | `true` |
| `sound.enabled` | 是否启用系统提示音 | `true` |
| `totp_secret` | TOTP 密钥（配置后 App 令牌验证自动完成） | 空 |

## TOTP 自动验证设置（推荐）

配置后，当树洞要求 App 手机令牌验证时会自动计算并提交，无需手动操作。

1. 打开北京大学 App → `我的 → 手机令牌` → 点击 `解绑`
2. 电脑打开 [我的门户](https://portal.pku.edu.cn/portal2017/#/setting) → `绑定手机令牌App`
3. 截图二维码，用二维码解析工具提取内容，得到形如：
   ```
   otpauth://totp/iaaa.pku.edu.cn:2110000000?secret=K42CH3M2GADU6W8T&issuer=iaaa.pku.edu.cn
   ```
4. 其中 `secret=` 后的值（如 `K42CH3M2GADU6W8T`）即为 TOTP 密钥，填入 `config.yaml` 的 `totp_secret`
5. 用 FreeOTP / Google Authenticator 等 App 扫码完成绑定

> 如未配置 `totp_secret`，程序会在需要时暂停并提示手动输入令牌。

## 邮箱授权码获取

### QQ 邮箱
1. 登录 QQ 邮箱 → 设置 → 账户
2. 开启 SMTP 服务
3. 生成授权码

### 163 邮箱
```yaml
email:
  smtp_server: "smtp.163.com"
  smtp_port: 465
  use_ssl: true
```

### Gmail
```yaml
email:
  smtp_server: "smtp.gmail.com"
  smtp_port: 465
  use_ssl: true
  password: "应用专用密码"
```

## 注意事项
- 💾 运行状态保存在 `.monitor_state.json` 中
- 🔄 Token 过期后会自动重新登录
- 按 `Ctrl+C` 优雅停止监控
