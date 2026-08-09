#!/usr/bin/env python
"""
用无头浏览器(Edge / Chromium)把邮件 HTML 按"浏览器视角"渲染成 PNG。
邮件在浏览器里长什么样,截图就长什么样(CSS、字体、图片全部原样)。
Windows 直接用系统自带 Edge(channel="msedge",无需下载浏览器);
其他环境回退到 Playwright 自带的 Chromium(需先执行 playwright install chromium)。

用法:
    png_bytes = render_html_to_png(html)   # 任何失败返回 None
"""
import sys


def render_html_to_png(html: str, width: int = 860, timeout_ms: int = 15000) -> bytes | None:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"playwright 未安装,浏览器渲染不可用: {e}", file=sys.stderr)
        return None

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="msedge", headless=True)
            except Exception:
                try:
                    browser = p.chromium.launch(headless=True)
                except Exception as e:
                    print(f"无可用浏览器(Edge/Chromium): {e}", file=sys.stderr)
                    return None
            try:
                page = browser.new_page(
                    viewport={"width": width, "height": 1000},
                    device_scale_factor=3,          # 3x 渲染,微信压缩后依然清晰
                )
                page.set_content(html, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(2500)         # 等远程图片加载
                return page.screenshot(full_page=True)
            finally:
                browser.close()
    except Exception as e:
        print(f"浏览器渲染失败: {e}", file=sys.stderr)
        return None
