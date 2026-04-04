"""
PKU Treehole 论坛监控工具
========================
实时监控 https://treehole.pku.edu.cn/web/ 的最新帖子，
当帖子内容匹配用户指定的关键词时，通过邮件/系统提示音发出提醒。
"""

from __future__ import annotations

import json
import logging
import random
import re
import uuid
import smtplib
import subprocess
import sys
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import pyotp
import requests
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# ---------------------------------------------------------------------------
# Rich 控制台 & 日志
# ---------------------------------------------------------------------------
console = Console()

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
LOG_FILE = Path(__file__).parent / "monitor.log"

logger = logging.getLogger("treehole_monitor")
logger.setLevel(logging.DEBUG)

# 文件 Handler：记录所有 INFO 及以上日志
_file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
logger.addHandler(_file_handler)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
IAAA_LOGIN_URL = "https://iaaa.pku.edu.cn/iaaa/oauthlogin.do"
TREEHOLE_LOGIN_URL = "https://treehole.pku.edu.cn/api/login"
TREEHOLE_HOLES_URL = "https://treehole.pku.edu.cn/api/pku_hole"

STATE_FILE = Path(__file__).parent / ".monitor_state.json"


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def load_config(path: str = "config.yaml") -> dict:
    """加载 YAML 配置文件"""
    config_path = Path(__file__).parent / path
    if not config_path.exists():
        logger.error(f"配置文件 {config_path} 不存在，请根据 config.yaml 模板填写配置")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state() -> dict:
    """加载已提醒帖子的状态（避免重复提醒）"""
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"notified_ids": []}


def save_state(state: dict):
    """保存状态到文件"""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# PKU IAAA 认证
# ---------------------------------------------------------------------------

class PKUAuth:
    """处理 PKU IAAA 认证，获取树洞 API Token"""

    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.token: str | None = None
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        })

    def login(self, max_retries: int = 3, retry_delay: int = 5) -> tuple[str, dict]:
        """
        带重试机制的登录方法
        """
        for attempt in range(1, max_retries + 1):
            try:
                return self._do_login()
            except Exception as e:
                logger.error(f"第 {attempt}/{max_retries} 次登录获取 Token 失败: {e}")
                if attempt < max_retries:
                    logger.info(f"等待 {retry_delay} 秒后重试...")
                    time.sleep(retry_delay)
                else:
                    raise

    def _do_login(self) -> tuple[str, dict]:
        """
        通过 IAAA 登录获取树洞 token。
        完整流程：
          1. GET /redirect_iaaa_login → 获取 _session / XSRF-TOKEN cookies
          2. POST IAAA oauthlogin.do → 获取 IAAA token
          3. GET /cas_iaaa_login?token=... → 用 cookies + IAAA token 换取树洞 JWT
        返回 token 字符串。
        """
        logger.info("正在通过 IAAA 登录 PKU 树洞...")

        # 每次登录使用全新 session，确保 cookie 干净
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        })

        # ----- Step 1: 访问 redirect_iaaa_login 建立会话 -----
        full_guid = str(uuid.uuid4())
        short_uuid = full_guid.replace("-", "")[-12:]  # 后 12 位
        redirect_init_url = (
            f"https://treehole.pku.edu.cn/redirect_iaaa_login"
            f"?uuid=Web_PKUHOLE_2.0.0_WEB_UUID_{full_guid}"
        )

        try:
            # allow_redirects=False 只是获取 cookies，不跟随到 IAAA 页面
            resp = self.session.get(redirect_init_url, timeout=15, allow_redirects=False)
            logger.debug(f"redirect_iaaa_login status={resp.status_code}, cookies={dict(self.session.cookies)}")
        except requests.RequestException as e:
            logger.error(f"初始化会话失败: {e}")
            raise

        logger.info("会话 cookies 已获取")

        # ----- Step 2: IAAA 认证 -----
        redirect_url = f"https://treehole.pku.edu.cn/cas_iaaa_login?uuid={short_uuid}&plat=web"

        iaaa_payload = {
            "appid": "PKU Helper",
            "userName": self.username,
            "password": self.password,
            "randCode": "",
            "smsCode": "",
            "otpCode": "",
            "redirUrl": redirect_url,
        }

        try:
            resp = self.session.post(IAAA_LOGIN_URL, data=iaaa_payload, timeout=15)
            resp.raise_for_status()
            result = resp.json()
        except requests.RequestException as e:
            logger.error(f"IAAA 请求失败: {e}")
            raise

        if not result.get("success"):
            error_msg = result.get("errors", {}).get("msg", "未知错误")
            logger.error(f"IAAA 登录失败: {error_msg}")
            raise RuntimeError(f"IAAA 登录失败: {error_msg}")

        iaaa_token = result["token"]
        logger.info("IAAA 认证成功，正在获取树洞 Token...")

        # ----- Step 3: 用 IAAA token + session cookies 换取树洞 token -----
        # cas_iaaa_login 返回 302 重定向，JWT 在 Location URL 的 token 参数中
        cas_login_url = (
            f"https://treehole.pku.edu.cn/cas_iaaa_login"
            f"?uuid={short_uuid}&plat=web&token={iaaa_token}"
        )

        try:
            resp = self.session.get(cas_login_url, timeout=15, allow_redirects=False)
        except requests.RequestException as e:
            logger.error(f"树洞 token 交换失败: {e}")
            raise

        # 从 302 重定向的 Location header 中提取 JWT token
        if resp.status_code in (301, 302):
            location = resp.headers.get("Location", "")
            # Location 格式: https://treehole.pku.edu.cn/web/iaaa_success?token=eyJ...
            parsed = urlparse(location)
            qs = parse_qs(parsed.query)
            token_list = qs.get("token", [])
            if token_list:
                self.token = token_list[0]
            else:
                logger.error(f"重定向 URL 中未找到 token 参数: {location[:200]}")
                raise RuntimeError("重定向 URL 中未找到 token")
        else:
            # 非重定向，尝试从 JSON 响应提取
            try:
                result = resp.json()
                self.token = (
                    result.get("token")
                    or result.get("access_token")
                    or result.get("jwt")
                    or (result.get("data") or {}).get("token")
                )
            except ValueError:
                pass

        if not self.token:
            logger.error(
                f"无法获取树洞 Token。"
                f"\n  响应状态: {resp.status_code}"
                f"\n  Location: {resp.headers.get('Location', 'N/A')[:200]}"
                f"\n  响应体: {resp.text[:200]}"
            )
            raise RuntimeError("无法获取树洞 Token")

        logger.info("✅ 树洞 Token 获取成功")
        assert self.token is not None
        return self.token, dict(self.session.cookies)


# ---------------------------------------------------------------------------
# 树洞 API 客户端
# ---------------------------------------------------------------------------

class TreeholeClient:
    """树洞 API 调用封装"""

    SEND_SMS_URL = "https://treehole.pku.edu.cn/api/jwt_send_msg"
    VERIFY_SMS_URL = "https://treehole.pku.edu.cn/api/jwt_msg_verify"
    VERIFY_OTP_URL = "https://treehole.pku.edu.cn/api/check_otp"

    def __init__(self, token: str, cookies: dict | None = None, config: dict | None = None):
        self.token = token
        self.config = config
        self.session = requests.Session()
        if cookies:
            self.session.cookies.update(cookies)
        self.session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        })
        self._verified = False
        self._otp_verified = False
        self._structure_logged = False

    def _handle_sms_verification(self):
        """处理手机短信验证（首次使用时需要）— 必须手动输入"""
        logger.warning("树洞要求手机短信验证（仅需验证一次）")

        if self.config:
            notify_system_event(self.config, "短信验证", "检测到树洞需要手机短信验证，程序已暂停，请前往控制台并输入验证码。")

        # 请求发送短信验证码
        try:
            resp = self.session.post(self.SEND_SMS_URL, timeout=15)
            result = resp.json() if resp.text else {}
            logger.info(f"短信发送结果: {result.get('message', resp.status_code)}")
        except Exception as e:
            logger.error(f"发送短信验证码失败: {e}")

        console.print(
            Panel(
                "[bold yellow]请查看手机短信，输入收到的验证码[/bold yellow]",
                title="[bold red]📱 短信验证[/bold red]",
            )
        )
        code = console.input("  [bold yellow]验证码: [/bold yellow]").strip()

        if not code:
            raise RuntimeError("未输入验证码")

        # 提交验证码
        try:
            resp = self.session.post(
                self.VERIFY_SMS_URL,
                json={"valid_code": code},
                headers={"Accept": "application/json"},
                timeout=15,
            )
            result = resp.json() if resp.text else {}
            logger.info(f"验证结果: {result}")
        except Exception as e:
            logger.error(f"验证码提交失败: {e}")
            raise

        if result.get("success"):
            console.print("[bold green]  ✓ 短信验证成功[/bold green]")
            logger.info("短信验证成功")
            self._verified = True
        else:
            msg = result.get("message", "验证失败")
            logger.error(f"短信验证失败: {msg}")
            raise RuntimeError(f"短信验证失败: {msg}")

    def _get_otp_code(self) -> str:
        """获取 OTP 验证码：优先使用 TOTP 自动计算，否则手动输入"""
        totp_secret = None
        if self.config:
            totp_secret = self.config.get("totp_secret")

        if totp_secret:
            totp = pyotp.TOTP(totp_secret)
            code = totp.now()
            console.print(f"  [dim]TOTP 令牌已自动计算[/dim]")
            logger.info(f"TOTP 令牌已计算: {code}")
            return code
        else:
            # 无 TOTP 密钥，通知用户手动输入
            if self.config:
                notify_system_event(self.config, "App手机令牌", "检测到树洞需要App手机动态令牌验证，程序已暂停，请前往控制台查看并输入。")

            console.print(
                Panel(
                    "[bold yellow]请打开北京大学App，查看并输入手机令牌 (6位数字)[/bold yellow]\n"
                    "[dim]提示: 配置 totp_secret 可实现自动验证[/dim]",
                    title="[bold red]📱 手机令牌验证[/bold red]",
                )
            )
            code = console.input("  [bold yellow]手机令牌: [/bold yellow]").strip()
            match = re.search(r"\d{6}", code)
            if not match:
                raise RuntimeError("无效的手机令牌")
            return match.group()

    def _handle_otp_verification(self):
        """处理北京大学App手机令牌验证（支持 TOTP 自动 / 手动输入）"""
        logger.warning("树洞要求输入北京大学App手机令牌")

        code = self._get_otp_code()

        # 提交手机令牌
        try:
            resp = self.session.post(
                self.VERIFY_OTP_URL,
                json={"code": code},
                headers={"Accept": "application/json"},
                timeout=15,
            )
            result = resp.json() if resp.text else {}
            logger.info(f"App令牌验证结果: {result}")
        except Exception as e:
            logger.error(f"App令牌提交失败: {e}")
            raise

        if result.get("success"):
            console.print("[bold green]  ✓ App手机令牌验证成功[/bold green]")
            logger.info("App手机令牌验证成功")
            self._otp_verified = True
        else:
            msg = result.get("message", "验证失败")
            logger.error(f"App手机令牌验证失败: {msg}")
            raise RuntimeError(f"App手机令牌验证失败: {msg}")

    def get_latest_posts(self, page: int = 1, limit: int = 25) -> list[dict]:
        """获取最新帖子列表"""
        params = {"page": page, "limit": limit}
        try:
            resp = self.session.get(TREEHOLE_HOLES_URL, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            logger.error(f"获取帖子失败 (page={page}): {e}")
            return []
        except ValueError as e:
            logger.error(f"API 响应非 JSON (page={page}): {e}")
            return []


        # 检查是否需要短信验证 (code 40002)
        if isinstance(data, dict) and data.get("code") == 40002:
            if not self._verified:
                self._handle_sms_verification()
                # 验证后重试
                return self.get_latest_posts(page=page, limit=limit)
            else:
                logger.error("短信验证已完成但仍被拒绝，可能需要重新登录")
                return []

        # 检查是否需要 App 手机令牌验证 (code 40008)
        if isinstance(data, dict) and data.get("code") == 40008:
            if not getattr(self, "_otp_verified", False):
                self._handle_otp_verification()
                return self.get_latest_posts(page=page, limit=limit)
            else:
                logger.error("App令牌验证已完成但仍被拒绝，可能需要重新登录")
                return []

        # API 可能返回多种格式:
        #   直接列表: [{...}, ...]
        #   分页: {"data": {"data": [...], "total": N, ...}}
        #   简单包装: {"data": [...]}
        posts: list[dict] = []

        if isinstance(data, list):
            posts = data
        elif isinstance(data, dict):
            inner = data.get("data", [])
            if isinstance(inner, list):
                posts = inner
            elif isinstance(inner, dict):
                # 分页格式: {"data": {"data": [...], ...}}
                posts = inner.get("data", [])
                if not isinstance(posts, list):
                    posts = []

        # 过滤掉非 dict 项（安全处理）
        posts = [p for p in posts if isinstance(p, dict)]

        if not posts and data:
            logger.debug(f"API 返回非空但无法解析帖子, type={type(data).__name__}, "
                        f"keys={list(data.keys()) if isinstance(data, dict) else 'N/A'}")

        return posts


# ---------------------------------------------------------------------------
# 关键词匹配
# ---------------------------------------------------------------------------

def match_keywords(text: str, keywords: list[str], mode: str = "AND") -> bool:
    """
    检查文本是否匹配关键词列表。
    mode="AND": 所有关键词都必须出现
    mode="OR":  任一关键词出现即可
    """
    if not keywords or not text:
        return False

    text_lower = text.lower()
    if mode.upper() == "AND":
        return all(kw.lower() in text_lower for kw in keywords)
    else:  # OR
        return any(kw.lower() in text_lower for kw in keywords)


# ---------------------------------------------------------------------------
# 提醒方式
# ---------------------------------------------------------------------------

def send_email(config: dict, subject: str, body: str):
    """通过邮件发送提醒"""
    email_cfg = config["email"]
    if not email_cfg.get("enabled"):
        return

    msg = MIMEMultipart("alternative")
    msg["From"] = email_cfg["sender"]
    msg["To"] = email_cfg["receiver"]
    msg["Subject"] = subject

    # HTML 邮件内容
    html_body = f"""
    <html>
    <body style="font-family: 'Segoe UI', sans-serif; padding: 20px; background: #f5f5f5;">
      <div style="max-width: 600px; margin: 0 auto; background: white; border-radius: 12px;
                  box-shadow: 0 2px 12px rgba(0,0,0,0.1); padding: 24px;">
        <h2 style="color: #1a73e8; margin-top: 0;">🔔 树洞关键词提醒</h2>
        <div style="white-space: pre-wrap; line-height: 1.6; color: #333;">
{body}
        </div>
        <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
        <p style="color: #999; font-size: 12px;">
          此邮件由 PKU Treehole Monitor 自动发送 · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        </p>
      </div>
    </body>
    </html>
    """

    msg.attach(MIMEText(body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        if email_cfg.get("use_ssl"):
            server = smtplib.SMTP_SSL(email_cfg["smtp_server"], email_cfg["smtp_port"], timeout=15)
        else:
            server = smtplib.SMTP(email_cfg["smtp_server"], email_cfg["smtp_port"], timeout=15)
            server.starttls()

        server.login(email_cfg["sender"], email_cfg["password"])
        server.sendmail(email_cfg["sender"], email_cfg["receiver"], msg.as_string())
        server.quit()
        logger.info("📧 提醒邮件发送成功")
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")


def play_sound():
    """在 macOS 上播放系统提示音"""
    try:
        subprocess.run(
            ["afplay", "/System/Library/Sounds/Glass.aiff"],
            check=False,
            timeout=5,
        )
    except Exception:
        pass


def send_notification(title: str, message: str):
    """发送 macOS 系统通知"""
    try:
        script = f'display notification "{message}" with title "{title}" sound name "Glass"'
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            timeout=5,
        )
    except Exception:
        pass


def notify(config: dict, post: dict, keywords: list[str]):
    """统一的提醒入口"""
    post_id = post.get("pid", "未知")
    content = post.get("text", "")
    ts = post.get("timestamp", "")

    # 格式化 Unix 时间戳
    if isinstance(ts, (int, float)) and ts > 0:
        created_at = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    else:
        created_at = str(ts)

    # 截取内容摘要
    summary = content[:200] + ("..." if len(content) > 200 else "")

    subject = f"{config['email'].get('subject_prefix', '[树洞监控]')} 匹配帖子 #{post_id}"

    body = (
        f"帖子编号: #{post_id}\n"
        f"发布时间: {created_at}\n"
        f"匹配关键词: {', '.join(keywords)}\n"
        f"{'─' * 40}\n"
        f"内容:\n{summary}\n"
        f"{'─' * 40}\n"
        f"链接: https://treehole.pku.edu.cn/web/#/hole/{post_id}\n"
    )

    logger.info(f"发现匹配帖子 #{post_id}: {summary[:60]}...")
    console.print(f"  [bold green]✔[/bold green] 发现匹配帖子 [cyan]#{post_id}[/cyan]: {summary[:60]}")

    # 邮件提醒
    send_email(config, subject, body)

    # 系统通知（macOS）
    send_notification("树洞关键词提醒", f"帖子 #{post_id} 匹配关键词: {', '.join(keywords)}")

    # 系统提示音
    if config.get("sound", {}).get("enabled"):
        play_sound()


# ---------------------------------------------------------------------------
# 主监控逻辑
# ---------------------------------------------------------------------------

def notify_system_event(config: dict, event_type: str, msg: str):
    """发送系统级别的通知（如需要验证码等）"""
    subject = f"{config.get('email', {}).get('subject_prefix', '[树洞监控]')} ⚠️ 需要手动干预：{event_type}"
    body = (
        f"发生事件: 需要{event_type}\n"
        f"{'─' * 40}\n"
        f"详情: {msg}\n"
        f"{'─' * 40}\n"
        f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    )

    logger.info(f"🔔 发送系统事件提醒: {msg}")

    # 邮件提醒
    send_email(config, subject, body)

    # 系统通知（macOS）
    send_notification("树洞监控警告", msg)

    # 系统提示音
    if config.get("sound", {}).get("enabled"):
        play_sound()


class TreeholeMonitor:
    """主监控类"""

    def __init__(self, config: dict):
        self.config = config
        self.state = load_state()
        self.auth = PKUAuth(
            config["pku"]["username"],
            config["pku"]["password"],
        )
        self.client: TreeholeClient | None = None
        self.keywords = config["keywords"]["list"]
        self.match_mode = config["keywords"].get("mode", "AND")

    def ensure_login(self):
        """确保已登录，如果 token 失效则重新登录"""
        if self.client is None:
            token, cookies = self.auth.login()
            self.client = TreeholeClient(token, cookies, config=self.config)
            return

        # 测试 token 是否有效
        posts = self.client.get_latest_posts(page=1, limit=1)
        if posts is None:
            logger.warning("Token 可能已失效，正在重新登录...")
            token, cookies = self.auth.login()
            self.client = TreeholeClient(token, cookies, config=self.config)

    def check_new_posts(self):
        """检查新帖子并触发提醒"""
        self.ensure_login()
        assert self.client is not None

        max_pages = self.config["monitor"].get("max_pages", 2)
        per_page = self.config["monitor"].get("posts_per_page", 25)
        notified = set(self.state.get("notified_ids", []))

        new_matches = 0

        for page in range(1, max_pages + 1):
            posts = self.client.get_latest_posts(page=page, limit=per_page)
            if not posts:
                logger.warning(f"第 {page} 页无数据（可能需要重新登录）")
                # 尝试重新登录
                try:
                    token, cookies = self.auth.login()
                    self.client = TreeholeClient(token, cookies, config=self.config)
                    posts = self.client.get_latest_posts(page=page, limit=per_page)
                except Exception as e:
                    logger.error(f"重新登录失败: {e}")
                    break

                if not posts:
                    break

            for post in posts:
                post_id = str(post.get("pid", ""))
                if not post_id or post_id in notified:
                    continue

                content = post.get("text") or ""

                if match_keywords(content, self.keywords, self.match_mode):
                    notify(self.config, post, self.keywords)
                    notified.add(post_id)
                    new_matches += 1

        # 只保留最近的 5000 条记录，避免状态文件无限增长
        notified_list = list(notified)
        if len(notified_list) > 5000:
            notified_list = notified_list[-5000:]

        self.state["notified_ids"] = notified_list
        self.state["last_check"] = datetime.now().isoformat()
        save_state(self.state)

        return new_matches

    def run(self):
        """主循环"""
        interval = self.config["monitor"].get("interval_seconds", 60)

        # 启动标题
        console.print(
            Panel(
                "[bold blue]🔔 PKU Treehole 监控[/bold blue]",
                subtitle=f"[dim]{datetime.now():%Y-%m-%d %H:%M:%S}[/dim]",
            )
        )

        # 系统概览
        info = Table(show_header=False, box=None, padding=(0, 1))
        info.add_column(style="cyan")
        info.add_column(style="white")
        info.add_row("关键词", ", ".join(self.keywords))
        info.add_row("匹配模式", self.match_mode)
        info.add_row("轮询间隔", f"{interval} 秒")
        totp_status = "自动 (TOTP)" if self.config.get("totp_secret") else "手动输入"
        info.add_row("令牌验证", totp_status)
        info.add_row("日志文件", str(LOG_FILE))
        console.print(Panel(info, title="[bold magenta]📋 系统概览[/bold magenta]"))

        logger.info(f"监控已启动 - 关键词: {self.keywords}, 模式: {self.match_mode}, 间隔: {interval}s")

        while True:
            try:
                matches = self.check_new_posts()
                if matches > 0:
                    logger.info(f"本轮发现 {matches} 条匹配帖子")
                else:
                    logger.info("本轮未发现匹配帖子")
            except KeyboardInterrupt:
                raise
            except Exception as e:
                logger.error(f"检查过程出错: {e}")

            try:
                # 添加随机偏差防止因为固定规律请求被识别，偏差在 1 秒 到 interval 的 30% 之间
                deviation = random.uniform(1.0, max(3.0, interval * 0.3))
                actual_interval = interval + deviation
                logger.info(f"等待 {actual_interval:.1f} 秒后进行下一轮检查...")
                time.sleep(actual_interval)
            except KeyboardInterrupt:
                console.print(
                    Panel(
                        "[yellow]监控已停止[/yellow]",
                        title="[bold red]🛑 已停止[/bold red]",
                    )
                )
                logger.info("监控已停止")
                break


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main():
    config = load_config()

    # 基本配置验证
    if not config.get("pku", {}).get("username") or not config.get("pku", {}).get("password"):
        console.print("[bold red]✗ 请先在 config.yaml 中填写 PKU 用户名和密码[/bold red]")
        sys.exit(1)

    if not config.get("keywords", {}).get("list"):
        console.print("[bold red]✗ 请先在 config.yaml 中设置要监控的关键词列表[/bold red]")
        sys.exit(1)

    if config.get("email", {}).get("enabled"):
        email_cfg = config["email"]
        missing = []
        for field in ["smtp_server", "sender", "password", "receiver"]:
            if not email_cfg.get(field):
                missing.append(field)
        if missing:
            logger.warning(f"⚠️  邮件配置缺少以下字段: {', '.join(missing)}，邮件提醒将不可用")
            config["email"]["enabled"] = False

    monitor = TreeholeMonitor(config)
    monitor.run()


if __name__ == "__main__":
    main()
