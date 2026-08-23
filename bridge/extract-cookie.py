#!/usr/bin/env python3
"""从 Chrome 配置中提取小米登录 Cookie（通过 Playwright 让 Chrome 自己读自己的数据）。

用法：.venv-xiaogpt/bin/python extract-cookie.py [Chrome用户数据目录] [输出JSON路径]
"""
import json
import os
import sys

from playwright.sync_api import sync_playwright

USER_DATA_DIR = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/Library/Application Support/Google/Chrome")
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "xiaomi-cookie.json")


def main() -> int:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            channel="chrome",
            headless=True,
            args=["--no-first-run", "--no-default-browser-check"],
        )
        wanted = {}
        for url in ("https://account.xiaomi.com/", "https://api2.mina.mi.com/"):
            try:
                for c in ctx.cookies(url):
                    if c["name"] in ("serviceToken", "userId", "deviceId", "passToken", "cUserId", "sdkVersion"):
                        wanted.setdefault(c["name"], {})[c["domain"]] = c["value"]
            except Exception as e:
                print(f"[warn] {url}: {e}")
        ctx.close()

    if not wanted:
        print("× 未找到任何小米相关 Cookie（Chrome 里没有登录会话？）")
        return 1

    for name, domains in wanted.items():
        for domain, value in domains.items():
            print(f"  {name} @ {domain}（共 {len(value)} 字符）")

    # 完整凭据落盘必须 0600（serviceToken/passToken/cUserId 即登录身份）
    with open(OUT, "w") as f:
        json.dump(wanted, f, indent=2)
    try:
        os.chmod(OUT, 0o600)
    except OSError:
        pass
    print(f"已保存到 {OUT}（0600）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
