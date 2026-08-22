#!/usr/bin/env python3
"""静默刷新小米令牌（deviceId 已验证 → 无需任何手机确认）。

依赖 xiaogpt-credentials 中的 MI_USER / MI_PASS / MI_DEVICE_ID。
成功写入 ~/.mi.token 并加用户级不可变标志防误删。
"""
import asyncio
import base64
import hashlib
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

import aiohttp

HOME = Path(__file__).resolve().parent  # 脚本所在目录（xiaogpt-credentials 同目录）
TOKEN_PATH = Path.home() / ".mi.token"


def load_env() -> tuple[str, str, str]:
    user = passwd = device_id = ""
    creds = HOME / "xiaogpt-credentials"
    if creds.is_file():
        for line in creds.read_text().splitlines():
            for key in ("MI_USER", "MI_PASS", "MI_DEVICE_ID"):
                m = re.match(rf'export\s+{key}="(.*)"', line)
                if m and key == "MI_USER":
                    user = m.group(1)
                elif m and key == "MI_PASS":
                    passwd = m.group(1)
                elif m and key == "MI_DEVICE_ID":
                    device_id = m.group(1)
    return user, passwd, device_id


async def refresh() -> int:
    user, passwd, device_id = load_env()
    if not (user and passwd and device_id):
        print("× 缺少账号/密码/设备ID（xiaogpt-credentials）", file=sys.stderr)
        return 1
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as s:
        cookies = {"sdkVersion": "3.9", "deviceId": device_id, "passToken": ""}
        headers = {"User-Agent": "MiServiceFork/2.9.2"}
        async with s.get("https://account.xiaomi.com/pass/serviceLogin?sid=micoapi&_json=true",
                         cookies=cookies, headers=headers) as r:
            h = json.loads((await r.read())[11:])
        data = {"_json": "true", "qs": h.get("qs"), "sid": "micoapi", "_sign": h.get("_sign"),
                "callback": h.get("callback"), "user": user,
                "hash": hashlib.md5(passwd.encode()).hexdigest().upper()}
        async with s.post("https://account.xiaomi.com/pass/serviceLoginAuth2",
                          data=data, cookies=cookies, headers=headers) as r:
            resp = json.loads((await r.read())[11:])
        if "userId" not in resp:
            print(f"× auth2 未成功: code={resp.get('code')} desc={resp.get('desc')} 键={sorted(resp.keys())}", file=sys.stderr)
            return 1
        nsec = f"nonce={resp['nonce']}&{resp['ssecurity']}"
        client_sign = base64.b64encode(hashlib.sha1(nsec.encode()).digest()).decode()
        sts_url = resp["location"] + "&clientSign=" + urllib.parse.quote(client_sign)
        async with s.get(sts_url, cookies={"userId": resp["userId"]}) as r2:
            st = r2.cookies.get("serviceToken")
        if not st:
            print("× 未获得 serviceToken", file=sys.stderr)
            return 1
        token = {"deviceId": device_id, "userId": resp["userId"],
                 "passToken": resp["passToken"],
                 "micoapi": [resp["ssecurity"], st.value]}
        os.system(f'chflags nouchg "{TOKEN_PATH}" 2>/dev/null')
        TOKEN_PATH.write_text(json.dumps(token, indent=2))
        TOKEN_PATH.chmod(0o600)
        Path(str(TOKEN_PATH) + ".bak").write_text(json.dumps(token, indent=2))
        os.system(f'chflags uchg "{TOKEN_PATH}" 2>/dev/null')
        print(f"✓ 令牌已静默刷新（无需手机确认），保存到 {TOKEN_PATH}")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(refresh()))
