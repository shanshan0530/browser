import asyncio
import io
import ipaddress
import json
import os
import socket
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP, Image
from PIL import Image as PILImage
from playwright.async_api import BrowserContext, Page, async_playwright


PORT = int(os.environ.get("PORT", 8080))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
PROFILE_DIR = DATA_DIR / "browser-profile"
READ_ONLY_MODE = os.environ.get("READ_ONLY_MODE", "true").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
ALLOWED_DOMAINS = tuple(
    domain.strip().lower().lstrip(".")
    for domain in os.environ.get("ALLOWED_DOMAINS", "").split(",")
    if domain.strip()
)

DATA_DIR.mkdir(parents=True, exist_ok=True)
PROFILE_DIR.mkdir(parents=True, exist_ok=True)

mcp = FastMCP("browser", host="0.0.0.0", port=PORT)

_playwright = None
_context: Optional[BrowserContext] = None
_page: Optional[Page] = None
_lock: Optional[asyncio.Lock] = None


def get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def write_blocked(action: str) -> str:
    return (
        f"[已阻止] {action}：当前 READ_ONLY_MODE=true。"
        "如需允许写入操作，请在确认风险后将 READ_ONLY_MODE 设置为 false 并重新部署。"
    )


def hostname_matches(hostname: str, domain: str) -> bool:
    return hostname == domain or hostname.endswith(f".{domain}")


def validate_public_url(url: str, required_domain: Optional[str] = None) -> Optional[str]:
    """返回错误信息；URL 安全时返回 None。"""
    try:
        parsed = urlparse(url)
    except Exception:
        return "URL 格式无效"

    if parsed.scheme not in {"http", "https"}:
        return "只允许 http 或 https URL"
    if parsed.username or parsed.password:
        return "URL 中不能携带用户名或密码"

    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    if not hostname:
        return "URL 缺少有效域名"

    if required_domain and not hostname_matches(hostname, required_domain):
        return f"只允许访问 {required_domain} 及其子域名"

    if ALLOWED_DOMAINS and not any(
        hostname_matches(hostname, domain) for domain in ALLOWED_DOMAINS
    ):
        return f"域名 {hostname} 不在 ALLOWED_DOMAINS 白名单中"

    if hostname == "localhost" or hostname.endswith(".local"):
        return "禁止访问本机或局域网地址"

    try:
        addr_info = socket.getaddrinfo(
            hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror:
        return f"无法解析域名 {hostname}"

    for item in addr_info:
        raw_ip = item[4][0].split("%", 1)[0]
        try:
            address = ipaddress.ip_address(raw_ip)
        except ValueError:
            return f"无法验证地址 {raw_ip}"
        if not address.is_global:
            return f"禁止访问非公网地址 {address}"

    return None


async def _cleanup():
    """清理所有浏览器资源。"""
    global _playwright, _context, _page
    _page = None
    if _context is not None:
        try:
            await _context.close()
        except Exception:
            pass
        _context = None
    if _playwright is not None:
        try:
            await _playwright.stop()
        except Exception:
            pass
        _playwright = None


async def ensure_page() -> Page:
    """获取可用页面，自动处理崩溃恢复。"""
    global _playwright, _context, _page

    if _page is not None and not _page.is_closed():
        try:
            await _page.evaluate("1")
            return _page
        except Exception:
            pass

    if _context is not None:
        try:
            pages = _context.pages
            for page in pages:
                if not page.is_closed():
                    _page = page
                    await _page.evaluate("1")
                    return _page
            _page = await _context.new_page()
            return _page
        except Exception:
            pass

    await _cleanup()

    _playwright = await async_playwright().start()
    _context = await _playwright.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=False,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
            "--disable-crash-reporter",
            "--no-first-run",
            "--disable-default-apps",
        ],
        viewport={"width": 1280, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    )
    await _context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    )

    pages = _context.pages
    _page = pages[0] if pages else await _context.new_page()
    return _page


# ── 状态与通用浏览器工具 ──────────────────────────────────────


@mcp.tool()
async def security_status() -> str:
    """查看浏览器 MCP 当前的安全模式和域名白名单。"""
    return json.dumps(
        {
            "read_only_mode": READ_ONLY_MODE,
            "allowed_domains": list(ALLOWED_DOMAINS),
            "profile_dir": str(PROFILE_DIR),
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
async def navigate(url: str) -> str:
    """打开一个公网网页。"""
    error = validate_public_url(url)
    if error:
        return f"[已阻止] navigate：{error}"

    async with get_lock():
        try:
            page = await ensure_page()
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            status = response.status if response else "unknown"
            return f"已打开: {page.url}  状态码: {status}"
        except Exception as exc:
            return f"[错误] navigate 失败: {exc}"


@mcp.tool()
async def screenshot(quality: int = 60):
    """
    截取当前页面。quality: 1-100，默认 60，扫码建议 90。
    """
    quality = max(1, min(int(quality), 100))
    async with get_lock():
        try:
            page = await ensure_page()
            png_data = await page.screenshot(type="png", full_page=False)
            image = PILImage.open(io.BytesIO(png_data))
            if image.width > 1000:
                ratio = 1000 / image.width
                image = image.resize(
                    (1000, int(image.height * ratio)),
                    PILImage.Resampling.LANCZOS,
                )
            buffer = io.BytesIO()
            image.convert("RGB").save(
                buffer,
                format="JPEG",
                quality=quality,
                optimize=True,
            )
            return Image(data=buffer.getvalue(), format="jpeg")
        except Exception as exc:
            return f"[错误] screenshot 失败: {exc}"


@mcp.tool()
async def execute_js(script: str) -> str:
    """在当前页面执行 JavaScript。只读模式下禁用。"""
    if READ_ONLY_MODE:
        return write_blocked("execute_js")

    async with get_lock():
        try:
            page = await ensure_page()
            result = await page.evaluate(script)
            if result is None:
                return "null"
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as exc:
            return f"[错误] execute_js 失败: {exc}"


@mcp.tool()
async def click(selector: str) -> str:
    """点击页面元素。只读模式下禁用。"""
    if READ_ONLY_MODE:
        return write_blocked("click")

    async with get_lock():
        try:
            page = await ensure_page()
            await page.click(selector, timeout=10000)
            await page.wait_for_timeout(500)
            return f"已点击: {selector}"
        except Exception as exc:
            return f"[错误] click 失败: {exc}"


@mcp.tool()
async def type_text(selector: str, text: str, human_like: bool = False) -> str:
    """向输入框写入文字。只读模式下禁用。"""
    if READ_ONLY_MODE:
        return write_blocked("type_text")

    async with get_lock():
        try:
            page = await ensure_page()
            if human_like:
                await page.click(selector, timeout=10000)
                await page.fill(selector, "")
                await page.keyboard.type(text, delay=40)
            else:
                await page.fill(selector, text, timeout=10000)
            return f"已输入到 {selector}"
        except Exception as exc:
            return f"[错误] type_text 失败: {exc}"


@mcp.tool()
async def scroll(direction: str = "down", amount: int = 600) -> str:
    """
    向上或向下滚动页面。direction: up/down，amount: 像素数。
    """
    normalized_direction = direction.strip().lower()
    if normalized_direction not in {"up", "down"}:
        return "[错误] direction 只能是 up 或 down"

    amount = max(1, min(abs(int(amount)), 5000))
    async with get_lock():
        try:
            page = await ensure_page()
            delta = amount if normalized_direction == "down" else -amount
            await page.evaluate("(delta) => window.scrollBy(0, delta)", delta)
            await page.wait_for_timeout(400)
            scroll_y = await page.evaluate("window.scrollY")
            return (
                f"已滚动 {normalized_direction} {amount}px，"
                f"当前 scrollY: {scroll_y}"
            )
        except Exception as exc:
            return f"[错误] scroll 失败: {exc}"


@mcp.tool()
async def wait_for(selector: str, timeout: int = 10000) -> str:
    """等待页面元素出现。"""
    timeout = max(100, min(int(timeout), 60000))
    async with get_lock():
        try:
            page = await ensure_page()
            await page.wait_for_selector(selector, timeout=timeout)
            return f"元素已出现: {selector}"
        except Exception as exc:
            return f"[错误] wait_for 失败: {exc}"


@mcp.tool()
async def get_url() -> str:
    """查看当前页面 URL。"""
    async with get_lock():
        try:
            page = await ensure_page()
            return page.url
        except Exception as exc:
            return f"[错误] get_url 失败: {exc}"


# ── 小红书专用工具 ────────────────────────────────────────────

XHS_FEED_JS = """
(() => {
    const items = document.querySelectorAll('.note-item');
    return Array.from(items).map((item, i) => {
        const links = item.querySelectorAll('a[href*="/explore/"]');
        let url = '';
        for (const a of links) {
            if (a.href.includes('xsec_token')) { url = a.href; break; }
        }
        if (!url) {
            for (const a of links) {
                if (a.href.includes('/explore/')) { url = a.href; break; }
            }
        }
        const title = item.querySelector('.title span')?.textContent?.trim() || '';
        const desc = item.querySelector('.desc')?.textContent?.trim() || '';
        const author = item.querySelector('.author-wrapper .name')?.textContent?.trim() || '';
        const likes = item.querySelector('.like-wrapper .count')?.textContent?.trim() || '';
        return { index: i, title: title || desc, author, likes, url };
    });
})()
"""

XHS_NOTE_JS = """
(() => {
    const title = document.querySelector('#detail-title')?.textContent?.trim() || '';
    const desc = document.querySelector('#detail-desc')?.textContent?.trim() || '';
    const author = document.querySelector('.author-container .username')?.textContent?.trim() || '';
    const date = document.querySelector('.date')?.textContent?.trim() || '';
    const ipLoc = document.querySelector('.ip-container')?.textContent?.trim() || '';
    const likes = document.querySelector('.like-wrapper .count')?.textContent?.trim() || '';
    const collects = document.querySelector('.collect-wrapper .count')?.textContent?.trim() || '';
    const chatCount = document.querySelector('.chat-wrapper .count')?.textContent?.trim() || '';
    const tags = Array.from(document.querySelectorAll('#detail-desc a.tag')).map(
        a => a.textContent?.trim()
    );
    const comments = Array.from(document.querySelectorAll('.parent-comment'))
        .slice(0, __COMMENT_COUNT__)
        .map(c => {
            const item = c.querySelector('.comment-item');
            if (!item) return null;
            const name = item.querySelector('.author-wrapper .name')?.textContent?.trim() || '';
            const tag = item.querySelector('.author-wrapper .tag')?.textContent?.trim() || '';
            const text = item.querySelector('.note-text')?.textContent?.trim() || '';
            const like = item.querySelector('.like')?.textContent?.trim() || '';
            const date = item.querySelector('.info .date span')?.textContent?.trim() || '';
            const location = item.querySelector('.info .location')?.textContent?.trim() || '';
            return { name, tag, text, like, date, location };
        })
        .filter(Boolean);
    return {
        title,
        desc,
        author,
        date,
        ip: ipLoc,
        likes,
        collects,
        comments_count: chatCount,
        tags,
        comments
    };
})()
"""


@mcp.tool()
async def read_xhs_feed(count: int = 10) -> str:
    """
    读取小红书首页笔记，返回标题、作者、点赞数和链接。
    """
    count = max(1, min(int(count), 30))
    async with get_lock():
        try:
            page = await ensure_page()
            current = page.url
            current_host = (urlparse(current).hostname or "").lower()
            if not hostname_matches(current_host, "xiaohongshu.com"):
                await page.goto(
                    "https://www.xiaohongshu.com/explore",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            try:
                await page.wait_for_selector(".note-item", timeout=5000)
            except Exception:
                await page.wait_for_timeout(2000)

            result = await page.evaluate(XHS_FEED_JS)
            if result:
                result = result[:count]
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as exc:
            return f"[错误] read_xhs_feed 失败: {exc}"


@mcp.tool()
async def read_xhs_note(url: str, comment_count: int = 10) -> str:
    """
    读取一篇小红书笔记的标题、正文、作者、标签和评论。
    """
    error = validate_public_url(url, required_domain="xiaohongshu.com")
    if error:
        return f"[已阻止] read_xhs_note：{error}"

    comment_count = max(0, min(int(comment_count), 50))
    async with get_lock():
        try:
            page = await ensure_page()
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            try:
                await page.wait_for_selector("#detail-desc", timeout=5000)
            except Exception:
                await page.wait_for_timeout(2000)

            script = XHS_NOTE_JS.replace(
                "__COMMENT_COUNT__",
                str(comment_count),
            )
            result = await page.evaluate(script)
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as exc:
            return f"[错误] read_xhs_note 失败: {exc}"


@mcp.tool()
async def like_xhs_note() -> str:
    """给当前小红书笔记点赞。只读模式下禁用。"""
    if READ_ONLY_MODE:
        return write_blocked("like_xhs_note")

    async with get_lock():
        try:
            page = await ensure_page()
            await page.click(
                ".interact-container .like-wrapper",
                timeout=5000,
            )
            await page.wait_for_timeout(500)
            is_liked = await page.evaluate(
                "document.querySelector('.interact-container .like-wrapper')"
                ".classList.contains('like-active')"
            )
            return "已点赞" if is_liked else "已取消点赞"
        except Exception as exc:
            return f"[错误] like_xhs_note 失败: {exc}"


@mcp.tool()
async def comment_xhs_note(text: str) -> str:
    """在当前小红书笔记下发表评论。只读模式下禁用。"""
    if READ_ONLY_MODE:
        return write_blocked("comment_xhs_note")

    text = text.strip()
    if not text:
        return "[错误] 评论内容不能为空"
    if len(text) > 500:
        return "[错误] 评论内容不能超过 500 个字符"

    async with get_lock():
        try:
            page = await ensure_page()
            await page.click("#content-textarea", timeout=5000)
            await page.wait_for_timeout(300)
            await page.keyboard.type(text, delay=40)
            await page.wait_for_timeout(300)
            await page.wait_for_function(
                "!document.querySelector('.btn.submit').classList.contains('gray')",
                timeout=3000,
            )
            await page.click(".btn.submit", timeout=5000)
            await page.wait_for_timeout(1000)
            return "评论已发送"
        except Exception as exc:
            return f"[错误] comment_xhs_note 失败: {exc}"


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
