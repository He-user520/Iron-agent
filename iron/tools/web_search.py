"""网页搜索工具 — 产品级实现，对标 Claude Code 的 WebSearch / WebFetch

能力：
- search: 关键词搜索（DuckDuckGo，无需 API key）
- fetch: 获取指定 URL 内容，智能转 Markdown（保留代码块/链接/标题）
- 多搜索引擎可扩展（DDG 默认，预留 Google/Bing 接口）
- 智能内容提取：去除导航/广告/页脚，保留正文
- 重试机制 + 超时配置 + 错误恢复
- 简单内存缓存（避免重复请求）
- 内容截断：按段落智能截断，保留完整代码块
"""
import ipaddress
import logging
import re
import socket
import time
import urllib.parse
from iron.tools.base import BaseTool

logger = logging.getLogger(__name__)

# 响应体最大字节数（防止超大响应耗尽内存）
MAX_RESPONSE_SIZE = 2 * 1024 * 1024  # 2MB

try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False


# ── 简单内存缓存 ──────────────────────────────────────────────

_CACHE: dict[str, dict] = {}
_CACHE_TTL = 600  # 10 分钟


def _cache_get(key: str):
    entry = _CACHE.get(key)
    if entry and time.time() - entry["time"] < _CACHE_TTL:
        return entry["data"]
    return None


def _cache_set(key: str, data):
    _CACHE[key] = {"time": time.time(), "data": data}
    # 清理过期缓存
    if len(_CACHE) > 50:
        now = time.time()
        expired = [k for k, v in _CACHE.items() if now - v["time"] > _CACHE_TTL]
        for k in expired:
            del _CACHE[k]


class WebSearchTool(BaseTool):
    """网页搜索 + 网页获取工具（产品级）"""

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return ("搜索网页获取最新信息，或获取指定 URL 的网页内容。"
                "用于查找芯片数据手册、库使用方法、错误解决方案、技术文档等。"
                "action=search 按关键词搜索；action=fetch 获取指定 URL 内容（智能转 Markdown）。")

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": (
                    "搜索网页获取最新信息，或获取指定 URL 的网页内容。"
                    "用于查找芯片数据手册、库使用方法、错误解决方案等。"
                    "action=search 时按关键词搜索；action=fetch 时获取指定 URL 内容（自动转 Markdown，保留代码块）。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["search", "fetch"],
                            "description": "search=搜索关键词，fetch=获取网页内容",
                        },
                        "query": {
                            "type": "string",
                            "description": "搜索关键词（action=search 时必填）",
                        },
                        "url": {
                            "type": "string",
                            "description": "要获取的网页 URL（action=fetch 时必填）",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "最大返回结果数（默认 5，最大 10）",
                        },
                        "max_length": {
                            "type": "integer",
                            "description": "fetch 时内容最大字符数（默认 8000，最大 20000）",
                        },
                    },
                    "required": ["action"],
                },
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        action = args.get("action", "search")

        if not _HTTPX_AVAILABLE:
            return {"success": False, "error": "httpx 未安装，无法进行网页搜索"}

        if action == "search":
            return await self._search(args, httpx)
        elif action == "fetch":
            return await self._fetch(args, httpx)
        else:
            return {"success": False, "error": f"未知 action: {action}"}

    def _is_safe_url(self, url: str) -> bool:
        """校验 URL 是否安全（防止 SSRF 攻击）

        拒绝：
        - 非 http/https 协议
        - 私有/保留 IP 段：127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12,
          192.168.0.0/16, 169.254.0.0/16, ::1, fc00::/7
        - localhost 及 *.internal / *.local 主机名
        - 非点分 IPv4 形式（十进制/十六进制/八进制/单零），由 socket.inet_aton 兜底识别
        - trailing-dot 主机名（如 "localhost."，多数解析器仍解析为 127.0.0.1）
        - DNS 重绑定：域名解析结果命中内网/保留 IP
        """
        try:
            parsed = urllib.parse.urlparse(url)
        except ValueError:
            return False
        # 协议白名单
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        hostname = hostname.lower()
        # 剥离 IPv6 scope id（如 fe80::1%eth0 → fe80::1），避免 ipaddress 在多数 Python 版本抛 ValueError 被当域名放行
        # 必须在 hostname_normalized 计算之前剥离，否则 normalized 仍含 scope id
        if "%" in hostname:
            hostname = hostname.split("%")[0]
        # trailing-dot 剥离后再比较（"localhost." 多数解析器仍解析为 127.0.0.1）
        hostname_normalized = hostname.rstrip(".")
        if hostname_normalized in ("localhost",) or hostname_normalized.endswith((".internal", ".local")):
            return False
        # 尝试解析为 IP 地址（IP 字面量），拒绝私有/保留/环回/链路本地地址
        ip = None
        try:
            ip = ipaddress.ip_address(hostname)
        except ValueError:
            # ipaddress 仅接受标准点分十进制，对十进制/十六进制/八进制/单零形式抛 ValueError；
            # 但 OS 的 getaddrinfo 仍会把它们解析为内网（如 http://2130706433/ → 127.0.0.1）。
            # 用 socket.inet_aton 兜底（它接受这些非点分形式），再走 is_private 检查
            try:
                packed = socket.inet_aton(hostname)
                ip = ipaddress.IPv4Address(packed)
            except OSError:
                # inet_aton 在 Windows 上只接受标准点分十进制，非标准形式（如 2130706433、0x7f000001）
                # 会抛 OSError 并可能被 getaddrinfo 当域名解析为内网。用 int(hostname, 0) 显式尝试
                # 十进制/十六进制/八进制整数形式，再走 is_private 检查
                try:
                    ip_int = int(hostname, 0)  # 自动识别进制前缀（0x, 0o, 0b）
                    if 0 <= ip_int <= 0xFFFFFFFF:
                        ip = ipaddress.IPv4Address(ip_int)
                    else:
                        ip = None
                except (ValueError, OverflowError):
                    ip = None  # 确实不是 IP，继续走域名分支
        if ip is not None:
            # IPv4-mapped IPv6 归一化（::ffff:127.0.0.1 → 127.0.0.1），防止绕过私有段检查
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
                ip = ip.ipv4_mapped
            # 拒绝私有、环回、链路本地、保留、未指定、组播地址
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_unspecified or ip.is_multicast):
                return False
            return True
        # 域名分支：主动 DNS 解析，对所有解析结果 IP 逐一过 is_private 等检查，防 DNS 重绑定
        try:
            addrinfos = socket.getaddrinfo(hostname_normalized, None)
            for family, _, _, _, sockaddr in addrinfos:
                ip_str = sockaddr[0]
                try:
                    resolved_ip = ipaddress.ip_address(ip_str)
                    if isinstance(resolved_ip, ipaddress.IPv6Address) and resolved_ip.ipv4_mapped is not None:
                        resolved_ip = resolved_ip.ipv4_mapped
                    if (resolved_ip.is_private or resolved_ip.is_loopback or resolved_ip.is_reserved
                            or resolved_ip.is_unspecified or resolved_ip.is_link_local
                            or resolved_ip.is_multicast):
                        return False
                except ValueError:
                    continue
        except (socket.gaierror, OSError):
            # DNS 解析失败或超时，默认拒绝（更安全，避免 DNS 重绑定绕过）
            return False
        return True

    async def _search(self, args: dict, httpx) -> dict:
        query = args.get("query", "").strip()
        if not query:
            return {"success": False, "error": "搜索需要 query 参数"}

        max_results = min(args.get("max_results", 5), 10)

        # 缓存检查
        cache_key = f"search:{query}:{max_results}"
        cached = _cache_get(cache_key)
        if cached:
            return {**cached, "cached": True}

        encoded = urllib.parse.quote(query)

        # 搜索引擎列表（按优先级降级）
        engines = [
            ("DuckDuckGo", f"https://html.duckduckgo.com/html/?q={encoded}", self._parse_ddg_html),
        ]

        last_error = None
        for engine_name, url, parser in engines:
            # SSRF 防护：校验目标 URL 安全
            if not self._is_safe_url(url):
                last_error = f"{engine_name} URL 不安全"
                continue
            try:
                result = await self._fetch_with_retry(httpx, url)
                if result is None:
                    last_error = f"{engine_name} 请求失败"
                    continue

                results = parser(result, max_results)
                if results:
                    response = {
                        "success": True,
                        "query": query,
                        "engine": engine_name,
                        "results": results,
                        "count": len(results),
                    }
                    _cache_set(cache_key, response)
                    return response
                last_error = f"{engine_name} 无结果"
            except (ValueError, AttributeError, IndexError, RuntimeError) as e:
                last_error = f"{engine_name} 异常: {e}"
                continue

        return {"success": False, "error": last_error or "所有搜索引擎均失败"}

    async def _fetch(self, args: dict, httpx) -> dict:
        url = args.get("url", "").strip()
        if not url:
            return {"success": False, "error": "fetch 需要 url 参数"}

        if "://" in url and not url.startswith(("http://", "https://")):
            return {"success": False, "error": f"仅支持 http/https 协议: {url}"}
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        # SSRF 防护：校验目标 URL 安全
        if not self._is_safe_url(url):
            return {"success": False, "error": f"URL 不安全（拒绝访问内网/保留地址）: {url}"}

        max_length = min(args.get("max_length", 8000), 20000)

        # 缓存检查
        cache_key = f"fetch:{url}:{max_length}"
        cached = _cache_get(cache_key)
        if cached:
            return {**cached, "cached": True}

        try:
            html = await self._fetch_with_retry(httpx, url)
            if html is None:
                return {"success": False, "error": f"获取失败: {url}"}

            # 智能转 Markdown
            text = self._html_to_markdown(html, url)

            # 智能截断（保留完整段落和代码块）
            text = self._smart_truncate(text, max_length)

            response = {
                "success": True,
                "url": url,
                "content": text,
                "length": len(text),
                "title": self._extract_title(html),
            }
            _cache_set(cache_key, response)
            return response
        except (ValueError, AttributeError, TypeError, RuntimeError) as e:
            return {"success": False, "error": f"获取网页失败: {e}"}

    async def _fetch_with_retry(self, httpx, url: str, max_retries: int = 2, max_depth: int = 3) -> str | None:
        """带重试的 HTTP 请求

        - 使用 stream 读取响应体，限制最大字节数（MAX_RESPONSE_SIZE）
        - 手动处理 http→https 同主机重定向升级（follow_redirects=False 时）
        - 限流（429/503）指数退避重试
        """
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

        for attempt in range(max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                    async with client.stream("GET", url, headers=headers, follow_redirects=False) as resp:
                        # 手动处理重定向（限深度，防递归无限循环）
                        if resp.status_code in (301, 302, 303, 307, 308) and max_depth > 0:
                            # 处理重定向 location
                            location = resp.headers.get("location", "").strip()
                            if not location:
                                return None
                            # 绝对 URL：校验同主机（含端口，netloc）后跟随
                            if location.startswith(("http://", "https://")):
                                old_loc = urllib.parse.urlparse(url).netloc
                                new_loc = urllib.parse.urlparse(location).netloc
                                if old_loc != new_loc:
                                    logger.debug("跨主机重定向被拒: %s -> %s", url[:100], location[:100])
                                    return None
                                # 重新校验目标 URL 安全性
                                if not self._is_safe_url(location):
                                    logger.debug("重定向目标不安全: %s", location[:100])
                                    return None
                                return await self._fetch_with_retry(
                                    httpx, location, max_retries=0, max_depth=max_depth - 1
                                )
                            # 根相对路径（含协议相对 //）：拼接当前 host:port
                            if location.startswith("/"):
                                parsed = urllib.parse.urlparse(url)
                                base = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
                                location = base.rstrip("/") + location
                                if not self._is_safe_url(location):
                                    return None
                                return await self._fetch_with_retry(
                                    httpx, location, max_retries=0, max_depth=max_depth - 1
                                )
                            # 非根相对路径：用 urljoin 拼接（如 "page.html"、"../x"）
                            location = urllib.parse.urljoin(url, location)
                            if not self._is_safe_url(location):
                                return None
                            return await self._fetch_with_retry(
                                httpx, location, max_retries=0, max_depth=max_depth - 1
                            )
                        if resp.status_code != 200:
                            if resp.status_code in (429, 503):
                                # 限流，等待后重试
                                if attempt < max_retries:
                                    await _async_sleep(2 ** attempt)
                                    continue
                            return None
                        # 流式读取响应体，限制最大字节数
                        chunks = []
                        total = 0
                        async for chunk in resp.aiter_bytes():
                            total += len(chunk)
                            if total > MAX_RESPONSE_SIZE:
                                logger.warning(f"响应体超过 {MAX_RESPONSE_SIZE} 字节，截断")
                                break
                            chunks.append(chunk)
                        return b"".join(chunks).decode("utf-8", errors="ignore")
            except (httpx.TimeoutException, httpx.ConnectError):
                if attempt < max_retries:
                    await _async_sleep(2 ** attempt)
                    continue
                return None
            except httpx.HTTPError as e:
                logger.debug(f"web_search fetch 异常 {url}: {e}")
                return None
        return None

    def _parse_ddg_html(self, html: str, max_results: int) -> list[dict]:
        """解析 DuckDuckGo HTML 搜索结果"""
        results = []
        pattern = re.compile(
            r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
            re.DOTALL,
        )
        snippet_pattern = re.compile(
            r'<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
            re.DOTALL,
        )

        titles = pattern.findall(html)
        snippets = snippet_pattern.findall(html)

        for i, (link, title) in enumerate(titles[:max_results]):
            title_clean = re.sub(r"<[^>]+>", "", title).strip()
            snippet = ""
            if i < len(snippets):
                snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip()

            # DuckDuckGo 重定向链接提取真实 URL
            if "uddg=" in link:
                m = re.search(r"uddg=([^&]+)", link)
                if m:
                    link = urllib.parse.unquote(m.group(1))

            results.append({
                "title": title_clean,
                "url": link,
                "snippet": snippet[:300] if snippet else "",
            })

        return results

    def _html_to_markdown(self, html: str, base_url: str = "") -> str:
        """HTML 转 Markdown（保留代码块/链接/标题/列表）

        对标 Claude Code 的 WebFetch：返回可读的 Markdown，而非粗暴去标签。
        """
        # 移除不需要的部分
        for pattern in [
            r"<script[^>]*>.*?</script>",
            r"<style[^>]*>.*?</style>",
            r"<nav[^>]*>.*?</nav>",
            r"<footer[^>]*>.*?</footer>",
            r"<header[^>]*>.*?</header>",
            r"<aside[^>]*>.*?</aside>",
            r"<!--.*?-->",
            r"<form[^>]*>.*?</form>",
            r"<noscript[^>]*>.*?</noscript>",
        ]:
            html = re.sub(pattern, "", html, flags=re.DOTALL | re.IGNORECASE)

        # 代码块：<pre><code>...</code></pre> → ```lang\n...\n```
        def _replace_pre_code(m):
            code = m.group(2)
            # 提取语言类名
            lang_match = re.search(r'class="[^"]*language-(\w+)[^"]*"', m.group(1) or "")
            lang = lang_match.group(1) if lang_match else ""
            code = re.sub(r"<[^>]+>", "", code).strip()
            return f"\n```{lang}\n{code}\n```\n"

        html = re.sub(
            r'<pre([^>]*)><code[^>]*>(.*?)</code></pre>',
            _replace_pre_code, html, flags=re.DOTALL | re.IGNORECASE,
        )
        # 单独的 <pre> 块
        html = re.sub(
            r'<pre[^>]*>(.*?)</pre>',
            lambda m: f"\n```\n{re.sub(r'<[^>]+>', '', m.group(1)).strip()}\n```\n",
            html, flags=re.DOTALL | re.IGNORECASE,
        )

        # 标题 h1-h6 → Markdown #
        for i in range(6, 0, -1):
            html = re.sub(
                rf"<h{i}[^>]*>(.*?)</h{i}>",
                lambda m, lvl=i: f"\n{'#' * lvl} {re.sub(r'<[^>]+>', '', m.group(1)).strip()}\n",
                html, flags=re.DOTALL | re.IGNORECASE,
            )

        # 链接 <a href="url">text</a> → [text](url)
        def _replace_link(m):
            attrs = m.group(1) or ""
            text = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            href_match = re.search(r'href="([^"]*)"', attrs)
            href = href_match.group(1) if href_match else ""
            if not href or not text:
                return text
            # 处理相对链接
            if href.startswith("/") and base_url:
                from urllib.parse import urljoin
                href = urljoin(base_url, href)
            if href.startswith(("http://", "https://", "#", "/")):
                return f"[{text}]({href})"
            return text

        html = re.sub(
            r'<a([^>]*)>(.*?)</a>',
            _replace_link, html, flags=re.DOTALL | re.IGNORECASE,
        )

        # 列表
        html = re.sub(r"<li[^>]*>(.*?)</li>", lambda m: f"- {m.group(1).strip()}\n",
                      html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"</?[ou]l[^>]*>", "", html, flags=re.IGNORECASE)

        # 段落和换行
        html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
        html = re.sub(r"<p[^>]*>(.*?)</p>", lambda m: f"\n\n{m.group(1).strip()}\n",
                      html, flags=re.DOTALL | re.IGNORECASE)

        # 行内代码 <code>...</code> → `...`
        html = re.sub(r"<code[^>]*>(.*?)</code>", lambda m: f"`{m.group(1).strip()}`",
                      html, flags=re.DOTALL | re.IGNORECASE)

        # 加粗/斜体
        html = re.sub(r"<(?:strong|b)[^>]*>(.*?)</(?:strong|b)>", lambda m: f"**{m.group(1).strip()}**",
                      html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<(?:em|i)[^>]*>(.*?)</(?:em|i)>", lambda m: f"*{m.group(1).strip()}*",
                      html, flags=re.DOTALL | re.IGNORECASE)

        # 移除剩余标签
        text = re.sub(r"<[^>]+>", "", html)

        # 解码 HTML 实体
        entities = {
            "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
            "&quot;": '"', "&#39;": "'", "&apos;": "'",
            "&copy;": "©", "&reg;": "®", "&trade;": "™",
            "&mdash;": "—", "&ndash;": "–", "&hellip;": "...",
            "&laquo;": "«", "&raquo;": "»",
        }
        for entity, char in entities.items():
            text = text.replace(entity, char)
        # 数字实体（校验码点范围，避免 chr() 越界）
        def _decode_decimal(m):
            v = int(m.group(1))
            return chr(v) if 0 <= v <= 0x10FFFF else ""

        def _decode_hex(m):
            v = int(m.group(1), 16)
            return chr(v) if 0 <= v <= 0x10FFFF else ""

        text = re.sub(r"&#(\d+);", _decode_decimal, text)
        text = re.sub(r"&#x([0-9a-fA-F]+);", _decode_hex, text)

        # 压缩多余空白，但保留代码块格式
        lines = text.split("\n")
        cleaned_lines = []
        in_code_block = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("```"):
                in_code_block = not in_code_block
                cleaned_lines.append(stripped)
                continue
            if in_code_block:
                cleaned_lines.append(line.rstrip())  # 代码块内保留缩进
            else:
                if stripped:
                    cleaned_lines.append(stripped)
                elif cleaned_lines and cleaned_lines[-1] != "":
                    cleaned_lines.append("")  # 保留段落间空行

        return "\n".join(cleaned_lines).strip()

    def _smart_truncate(self, text: str, max_length: int) -> str:
        """智能截断：在段落/代码块边界截断，不破坏代码块"""
        if len(text) <= max_length:
            return text

        # 找到最后一个完整的代码块
        code_block_end = text.rfind("```", 0, max_length)
        if code_block_end > 0:
            # 检查代码块是否闭合
            before = text[:code_block_end + 3]
            if before.count("```") % 2 == 0:
                return before + "\n\n...(内容已截断)"

        # 找到最后一个段落分隔
        para_end = text.rfind("\n\n", 0, max_length)
        if para_end > max_length * 0.5:
            return text[:para_end] + "\n\n...(内容已截断)"

        # 找到最后一个换行
        line_end = text.rfind("\n", 0, max_length)
        if line_end > max_length * 0.5:
            return text[:line_end] + "\n...(内容已截断)"

        return text[:max_length] + "\n...(内容已截断)"

    def _extract_title(self, html: str) -> str:
        """提取页面标题"""
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
        if m:
            return re.sub(r"<[^>]+>", "", m.group(1)).strip()
        # h1 作为备选
        m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL | re.IGNORECASE)
        if m:
            return re.sub(r"<[^>]+>", "", m.group(1)).strip()
        return ""


async def _async_sleep(seconds: float):
    """异步 sleep"""
    import asyncio
    await asyncio.sleep(seconds)
