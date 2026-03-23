"""
PKU Treehole 论坛监控工具
========================
实时监控 https://treehole.pku.edu.cn/web/ 的最新帖子，
当帖子内容匹配用户指定的关键词时，通过邮件/系统提示音发出提醒。
"""

from __future__ import annotations

import json
import logging
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

import requests
import yaml

# ---------------------------------------------------------------------------
# 日志配置：文件记录全部日志，终端仅显示重要信息
# ---------------------------------------------------------------------------
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
LOG_FILE = Path(__file__).parent / "monitor.log"

logger = logging.getLogger("treehole_monitor")
logger.setLevel(logging.DEBUG)

# 文件 Handler：记录所有 INFO 及以上日志
_file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
logger.addHandler(_file_handler)

# 终端 Handler：仅显示 WARNING 及以上
_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.WARNING)
_console_handler.setFormatter(logging.Formatter(LOG_FORMAT))
logger.addHandler(_console_handler)


def _console_print(msg: str):
    """直接打印到终端（用于启动信息和匹配提醒等需要用户看到的消息）"""
    print(msg)

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

    def login(self) -> tuple[str, dict]:
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

    def __init__(self, token: str, cookies: dict | None = None):
        self.token = token
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
        self._structure_logged = False

    def _handle_sms_verification(self):
        """处理手机短信验证（首次使用时需要）"""
        logger.warning("⚠️  树洞要求手机短信验证（仅需验证一次）")

        # 请求发送短信验证码
        try:
            resp = self.session.post(self.SEND_SMS_URL, timeout=15)
            result = resp.json() if resp.text else {}
            logger.info(f"短信发送结果: {result.get('message', resp.status_code)}")
        except Exception as e:
            logger.error(f"发送短信验证码失败: {e}")

        # 提示用户输入验证码
        print("\n" + "=" * 50)
        print("  📱 请查看手机短信，输入收到的验证码")
        print("=" * 50)
        code = input("  验证码: ").strip()

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
            logger.info("✅ 短信验证成功")
            self._verified = True
        else:
            msg = result.get("message", "验证失败")
            logger.error(f"短信验证失败: {msg}")
            raise RuntimeError(f"短信验证失败: {msg}")

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

    logger.info(f"🔔 发现匹配帖子 #{post_id}: {summary[:60]}...")
    _console_print(f"🔔 发现匹配帖子 #{post_id}: {summary[:60]}")

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
            self.client = TreeholeClient(token, cookies)
            return

        # 测试 token 是否有效
        posts = self.client.get_latest_posts(page=1, limit=1)
        if posts is None:
            logger.warning("Token 可能已失效，正在重新登录...")
            token, cookies = self.auth.login()
            self.client = TreeholeClient(token, cookies)

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
                    self.client = TreeholeClient(token, cookies)
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

        # 启动信息 — 同时输出到终端和日志
        banner = [
            "=" * 50,
            "  PKU Treehole 监控已启动",
            f"  关键词: {self.keywords}",
            f"  匹配模式: {self.match_mode}",
            f"  检查间隔: {interval} 秒",
            f"  日志文件: {LOG_FILE}",
            "=" * 50,
        ]
        for line in banner:
            _console_print(line)
            logger.info(line)

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
                logger.info(f"等待 {interval} 秒后进行下一轮检查...")
                time.sleep(interval)
            except KeyboardInterrupt:
                _console_print("\n🛑 监控已停止")
                logger.info("🛑 监控已停止")
                break


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main():
    config = load_config()

    # 基本配置验证
    if not config.get("pku", {}).get("username") or not config.get("pku", {}).get("password"):
        logger.error("❌ 请先在 config.yaml 中填写 PKU 用户名和密码")
        sys.exit(1)

    if not config.get("keywords", {}).get("list"):
        logger.error("❌ 请先在 config.yaml 中设置要监控的关键词列表")
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
