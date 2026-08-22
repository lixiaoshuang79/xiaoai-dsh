#!/usr/bin/env python3
"""
网络音频在线播放（通用链路沉淀，非具体歌曲/单平台）：
  用 ego-browser 的浏览器能力，在各网络平台（B站/小红书/抖音/任意网页）搜索并
  解析音频流直链，交给音箱在线播放（miplayer 直接拉流，零下载零转码零代理）。

平台适配器（PLATFORMS 注册表，按平台可扩展）：
  bilibili   = B站：CDP 登录态 cookie + wbi 签名搜索 API + playurl 音频流（已验证）
  generic    = 通用兜底：浏览器打开页面，抓 <video>/<audio> 元素与网络媒体流直链，
               不依赖任何平台 API 与登录态（浏览器基础能力）

用法:
  web-audio-play.py "原神 沃雅妮莎"                    # auto：优先 B站，失败转 generic
  web-audio-play.py "关键词" --platform bilibili      # 指定平台
  web-audio-play.py "关键词" --list                   # 只列候选，不播
  web-audio-play.py "关键词" --index 2                # 播第 2 个候选
  web-audio-play.py --url https://...                 # 直接给视频页 URL（generic 捕获）
  web-audio-play.py --bvid BVxxxx                     # B站直播（跳过搜索）
  web-audio-play.py --stop                            # 停止音箱当前播放
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

from config_loader import cfg_paths

EGO_BROWSER = cfg_paths("ego_browser") or "ego-browser"
MIGPT_PLAY_URL = "http://127.0.0.1:4398/play_url"
MIGPT_EXEC_URL = "http://127.0.0.1:4398/exec"
RELAY_PORT = 4378  # Mac 本机流式转发端口（音箱经 http://192.168.1.13:4378 拉流）

# 平台注册表：key=平台名，value=ego-browser 一侧的解析逻辑（Node 脚本片段）。
# 每个适配器最终 cliLog 一个 JSON：{title, url, duration?, source}，
# 或 cliLog 'LIST\n...'（--list 模式），或 cliLog '<ERR>_ERR ...' 报错。
# 平台有登录态时自动用登录态（CDP cookie），没有也能跑（公开接口/浏览器基础能力）。

BILIBILI_ADAPTER = r"""
// ---- B 站适配器：wbi 签名搜索 + playurl 音频流（有登录态拿高清档，无登录态拿低清档） ----
const { createHash } = await import('node:crypto')
const mixinKeyEncTab = [46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52]
const getMixinKey = (orig) => mixinKeyEncTab.map(n => orig[n]).join('').slice(0, 32)
const md5 = (s) => createHash('md5').update(s).digest('hex')
const encWbi = (params, imgKey, subKey) => {
  const mixinKey = getMixinKey(imgKey + subKey)
  const chrFilter = /[!'()*]/g
  Object.assign(params, { wts: Math.round(Date.now() / 1000) })
  const query = Object.keys(params).sort().map(key => {
    const v = String(params[key]).replace(chrFilter, '')
    return encodeURIComponent(key) + '=' + encodeURIComponent(v)
  }).join('&')
  return query + '&w_rid=' + md5(query + mixinKey)
}
const ck = await cdp('Network.getAllCookies')
const cookieStr = ck.cookies.filter(c => c.domain.includes('bilibili')).map(c => c.name + '=' + c.value).join('; ')
const UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36'
const H = { 'User-Agent': UA, 'Referer': 'https://www.bilibili.com/', 'Cookie': cookieStr }
const api = async (u) => serverFetch(u, { headers: H })
const nav = JSON.parse(await api('https://api.bilibili.com/x/web-interface/nav'))
if (nav.code !== 0) { cliLog('PLATFORM_ERR bilibili nav ' + nav.code); process.exit(0) }
const imgKey = nav.data.wbi_img.img_url.split('/').pop().split('.')[0]
const subKey = nav.data.wbi_img.sub_url.split('/').pop().split('.')[0]
let bv = BVID, title = ''
if (!bv) {
  const q = encWbi({ search_type: 'video', keyword: KEYWORD, page: 1, page_size: 20 }, imgKey, subKey)
  const sr = JSON.parse(await api('https://api.bilibili.com/x/web-interface/wbi/search/type?' + q))
  if (sr.code !== 0 || !sr.data || !(sr.data.result || []).length) { cliLog('PLATFORM_ERR bilibili search ' + sr.code); process.exit(0) }
  if (LIST_ONLY) {
    const list = (sr.data.result || []).map((v, i) => (i + 1) + '. [' + v.bvid + '] ' + v.title.replace(/<[^>]+>/g, '').slice(0, 50) + ' (' + v.duration + ')')
    cliLog('LIST\n' + list.join('\n'))
    process.exit(0)
  }
  const results = sr.data.result || []
  // 相关性打分（默认 INDEX=1 时启用）：歌名词命中加分、合集/精选/专辑扣分、超长视频扣分
  const KW_TOKENS = (KEYWORD || '').split(/[\s\-/，。！？、]+/).filter(t => t.length >= 2)
  const score = (r) => {
    const t = r.title.replace(/<[^>]+>/g, '')
    let s = 0
    for (const k of KW_TOKENS) if (t.includes(k)) s += 1
    if (/合集|精选|专辑|串烧|歌单|车载|循环|50首|100首|纯音乐|背景音乐/.test(t)) s -= 3
    const d = String(r.duration || '').split(':').map(Number)
    const sec = d.length === 2 ? d[0] * 60 + d[1] : (d[0] || 0)
    if (sec > 900) s -= 2
    if (sec > 0 && sec < 60) s -= 1
    return s
  }
  let chosen = results[Math.min(Math.max(INDEX, 1), results.length) - 1]
  if (INDEX === 1 && results.length > 1) {
    chosen = results.reduce((a, b) => score(b) > score(a) ? b : a, results[0])
  }
  bv = chosen.bvid
  title = chosen.title.replace(/<[^>]+>/g, '')
}
const v = JSON.parse(await api('https://api.bilibili.com/x/web-interface/view?bvid=' + bv))
if (v.code !== 0 || !v.data) { cliLog('PLATFORM_ERR bilibili view ' + v.code); process.exit(0) }
title = title || v.data.title
const p = JSON.parse(await api('https://api.bilibili.com/x/player/playurl?bvid=' + bv + '&cid=' + v.data.cid + '&fnval=16&fnver=0&fourk=1'))
if (p.code !== 0 || !p.data || !p.data.dash) { cliLog('PLATFORM_ERR bilibili playurl ' + p.code); process.exit(0) }
const audios = (p.data.dash.audio || []).sort((a, b) => b.bandwidth - a.bandwidth)
if (!audios.length) { cliLog('PLATFORM_ERR bilibili no-audio'); process.exit(0) }
let pick = 0
if (QUALITY === 'mid') pick = Math.min(1, audios.length - 1)
if (QUALITY === 'low') pick = audios.length - 1
cliLog(JSON.stringify({ title: title.slice(0, 60), url: audios[pick].baseUrl, duration: v.data.duration, source: 'bilibili', ref: bv }))
"""

GENERIC_ADAPTER = r"""
// ---- 通用浏览器捕获：打开页面，抓媒体元素与网络媒体流直链（不依赖平台 API/登录态） ----
// 关键：先开 tab，再在当前 tab 上 Network.enable + reload，媒体请求才会被事件队列捕获
await openOrReuseTab(TARGET_URL, { wait: true, timeout: 25 })
await wait(2)
await cdp('Network.enable')
try { await js('location.reload()') } catch (err) { /* 页面不可 reload 就算了 */ }
// 尝试触发播放（部分站需要点击才请求音视频流）
try { await js(String.raw`(() => { const v = document.querySelector('video'); if (v) v.muted = false, v.play().catch(()=>{}); const b = document.querySelector('.bpx-player-container video'); if (b) b.play().catch(()=>{}); })()`) } catch (err) {}
await wait(6)
const evs = await drainEvents()
const net = []
const LOG_DOMAINS = ['data.bilibili.com', 'api.bilibili.com', 's1.hdslb.com', 'message.bilibili.com']
for (const e of evs) {
  if (e && e.method === 'Network.requestWillBeSent' && e.params && e.params.request && e.params.request.url) {
    const u = e.params.request.url
    if (!/^https?:/.test(u)) continue
    if (LOG_DOMAINS.some(d => u.includes(d))) continue // 数据上报/日志噪音
    if (/\.(m4s|mp4|m4a|mp3|flac|aac)(\?|$)/i.test(u)) net.push(u)
  }
}
// 音频流优先（B站音频分段 -3xxxx.m4s / 含 audio 的 URL），视频流靠后
const isAudio = (u) => /-3\d{4}\.m4s|audio|\.m4a|\.mp3|\.flac|\.aac/i.test(u)
net.sort((a, b) => (isAudio(b) ? 1 : 0) - (isAudio(a) ? 1 : 0))
// 页面媒体元素直链（MSE 播放器常是 blob:，真正直链看 net 或 source 标签）
const media = await js(String.raw`(() => {
  const urls = []
  document.querySelectorAll('video,audio').forEach(el => {
    const u = el.currentSrc || el.src
    if (u && /^https?:/.test(u)) urls.push(u)
    el.querySelectorAll('source').forEach(s => { if (s.src && /^https?:/.test(s.src)) urls.push(s.src) })
  })
  return urls
})()`)
const all = []
for (const u of [...net, ...media]) { if (!all.includes(u)) all.push(u) }
if (!all.length) { cliLog('PLATFORM_ERR generic no-media-stream'); process.exit(0) }
if (LIST_ONLY) {
  cliLog('LIST\n' + all.map((u, i) => (i + 1) + '. ' + u.slice(0, 100)).join('\n'))
  process.exit(0)
}
const chosen = all[Math.min(Math.max(INDEX, 1), all.length) - 1]
const t = await pageInfo()
cliLog(JSON.stringify({ title: (t.title || '网页音频').slice(0, 60), url: chosen, source: 'generic', ref: t.url }))
"""

PLATFORM_ADAPTERS = {
    "bilibili": BILIBILI_ADAPTER,
    "generic": GENERIC_ADAPTER,
}

# 公共头：每个适配器执行前先选中/创建 task space（cdp 依赖它）
ADAPTER_HEADER = "const task = await useOrCreateTaskSpace('web-audio-play')\n"

RELAY_CODE = r'''
import http.server
import shutil
import sys
import threading
import time
import urllib.request

TARGET = sys.argv[1]
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")
REFERER = sys.argv[2] if len(sys.argv) > 2 else "https://www.bilibili.com/"
LAST = [time.time()]
MAX_IDLE = 1800  # 30 分钟无请求自动退出（一首歌最多 20 分钟）

class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def do_GET(self):
        LAST[0] = time.time()
        try:
            req = urllib.request.Request(TARGET, headers={
                "User-Agent": UA, "Referer": REFERER})
            rng = self.headers.get("Range")
            if rng:
                req.add_header("Range", rng)
            with urllib.request.urlopen(req, timeout=60) as up:
                self.send_response(up.status)
                for k, v in up.headers.items():
                    if k.lower() in ("content-type", "content-length",
                                     "content-range", "accept-ranges"):
                        self.send_header(k, v)
                self.end_headers()
                shutil.copyfileobj(up, self.wfile, 64 * 1024)  # 流式转发，零落盘
        except Exception as e:
            try:
                self.send_response(502)
                self.end_headers()
                self.wfile.write(str(e).encode())
            except Exception:
                pass
    def log_message(self, *a):
        pass

class S(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

def watchdog():
    while time.time() - LAST[0] < MAX_IDLE:
        time.sleep(60)
    sys.exit(0)

threading.Thread(target=watchdog, daemon=True).start()
S(("0.0.0.0", {port}), H).serve_forever()
'''


def _url_reachable(url: str) -> bool:
    """快速探测音频流 URL 能否裸拉（206/200=直链可行；403/400=防盗链需 relay）。"""
    try:
        req = urllib.request.Request(url, method="GET",
                                     headers={"Range": "bytes=0-1023",
                                              "User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 206)
    except Exception:
        return False


def _relay_alive() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{RELAY_PORT}/ping", timeout=2) as resp:
            return resp.status < 500
    except Exception:
        return False


def _start_relay(url: str, referer: str) -> None:
    """起流式转发进程（带 Referer/UA 转发平台 CDN 流，零落盘零转码）。"""
    code = RELAY_CODE.replace("{port}", str(RELAY_PORT))
    subprocess.Popen(
        [sys.executable, "-c", code, url, referer],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)  # 脱离父进程，播放期间持续服务


def _shell(cmd: str) -> str:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180)
    # ego-browser 的 cliLog 输出在 stderr，日志也混在里面——合并返回
    return ((r.stdout or "") + "\n" + (r.stderr or "")).strip()


def _migpt_post(url: str, payload: dict) -> str:
    req = urllib.request.Request(
        url, json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        return f"ERROR: {e}"


def _run_adapter(adapter: str, keyword: str, index: int, quality: str,
                 bvid: str, url: str, list_only: bool) -> str:
    """执行 ego-browser 一侧的适配器逻辑，返回其输出。"""
    script = (ADAPTER_HEADER + adapter
              .replace("KEYWORD", json.dumps(keyword))
              .replace("INDEX", str(index))
              .replace("QUALITY", json.dumps(quality))
              .replace("BVID", json.dumps(bvid))
              .replace("TARGET_URL", json.dumps(url))
              .replace("LIST_ONLY", "true" if list_only else "false"))
    if not _shell("pgrep -f 'ego lite' >/dev/null && echo RUNNING"):
        _shell("open -a 'ego lite'")
        time.sleep(6)
    return _shell(f"{EGO_BROWSER} nodejs <<'EOF'\n{script}\nEOF")


def _extract_result(out: str) -> dict:
    """从适配器输出中提取最后的 JSON 结果行。"""
    for line in reversed(out.splitlines()):
        if line.strip().startswith("{") and '"url"' in line:
            try:
                return json.loads(line.strip())
            except json.JSONDecodeError:
                continue
    return {}


def _stop_speaker() -> str:
    """彻底停止音箱当前播放：停媒体服务播放项 + 杀光 miplayer（防自动续播）。"""
    cmd = ("ubus call mediaplayer player_play_operation '{\"action\":\"stop\"}' 2>/dev/null; "
           "mphelper pause 2>/dev/null; "
           "for i in 1 2 3 4 5; do kill -9 $(pidof miplayer) 2>/dev/null; "
           "pidof miplayer >/dev/null 2>&1 || break; sleep 0.3; done; echo stopped")
    return _migpt_post(MIGPT_EXEC_URL, {"cmd": cmd})


def main() -> None:
    args = sys.argv[1:]
    keyword, index, quality, bvid, url = "", 1, "best", "", ""
    platform, do_list, do_stop, no_play = "auto", False, False, False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--index":
            index = int(args[i + 1]); i += 2; continue
        if a == "--quality":
            quality = args[i + 1]; i += 2; continue
        if a == "--bvid":
            bvid = args[i + 1]; i += 2; continue
        if a == "--url":
            url = args[i + 1]; i += 2; continue
        if a == "--platform":
            platform = args[i + 1]; i += 2; continue
        if a == "--no-play":
            no_play = True; i += 1; continue
        if a == "--list":
            do_list = True; i += 1; continue
        if a == "--stop":
            do_stop = True; i += 1; continue
        keyword = a; i += 1
    if do_stop:
        print(_stop_speaker())
        return
    if not keyword and not bvid and not url:
        print(__doc__)
        return
    if url:
        platform = "generic"  # 直接给页面 URL → 浏览器捕获
    if platform not in ("auto",) + tuple(PLATFORM_ADAPTERS):
        print(f"[web-audio] 未知平台 {platform}（可用: auto/bilibili/generic）", file=sys.stderr)
        sys.exit(1)
    order = ["bilibili", "generic"] if platform == "auto" else [platform]
    for plat in order:
        if bvid and plat != "bilibili":
            continue
        out = _run_adapter(PLATFORM_ADAPTERS[plat], keyword, index, quality, bvid, url, do_list)
        if do_list and out.startswith("LIST"):
            print(f"—— {plat} 候选 ——")
            print(out[5:])
            return
        res = _extract_result(out)
        if res:
            if do_list:
                print(f"将播放: {res.get('title', '')}")
                return
            print(f"[web-audio] {res.get('source')}: {res['title']}"
                  + (f" ({res['duration']}s)" if res.get("duration") else ""))
            play_url = res["url"]
            # 直链探测：CDN 裸拉可行 → 音箱直接拉（零代理）；防盗链平台 → Mac 流式转发
            if _url_reachable(play_url):
                print("[web-audio] 直链可行，音箱直接拉流")
            else:
                print("[web-audio] 平台有防盗链，Mac 流式转发（零落盘）")
                ref = "https://www.bilibili.com/" if res.get("source") == "bilibili" else "https://www.douyin.com/"
                if not _relay_alive():
                    _start_relay(play_url, ref)
                play_url = f"http://192.168.1.13:{RELAY_PORT}/stream"
            if no_play:
                # 只拿 URL 不播放：把播放时机交回调用方（桥在 AI 回答播报完后再放）
                print(f"[web-audio] URL: {play_url}")
                return
            r = _migpt_post(MIGPT_PLAY_URL, {"url": play_url})
            print(f"[web-audio] 在线播放: {r[:120]}")
            return
        err = next((l for l in out.splitlines() if "PLATFORM_ERR" in l), "")
        print(f"[web-audio] {plat} 失败: {err[:150]}", file=sys.stderr)
    print("[web-audio] 所有平台均未找到可播放的音频流", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
