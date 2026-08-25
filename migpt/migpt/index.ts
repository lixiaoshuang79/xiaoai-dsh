import { getBridgeSecret, kOpenXiaoAIConfig } from "config.js";
import { OpenXiaoAI } from "./xiaoai.js";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { sleep } from "@mi-gpt/utils";
import {
  authOk,
  checkPlayUrl,
  currentEpoch,
  enqueuePlay,
  enqueueWakeUp,
  hostAllowed,
  initSpeakerGate,
  playUrlNow,
  readJsonBody,
  redactUrl,
  stopMusic,
} from "./speaker-gate.js";

/**
 * 4398 端点请求处理（P1-5 加固）：
 * - Host 校验：仅接受 127.0.0.1 / localhost（DNS rebinding 防御），其他 403
 * - OPTIONS 预检：拒绝（不返回 CORS 头）
 * - 统一鉴权：Authorization: Bearer <bridge.secret>（缺失/错误 401）
 * - body：Content-Length 预检 + 流式 64KB 上限截断（413）
 * - /play_url：URL 双重校验（基础 + 私网/loopback 拒绝），日志只打 scheme+host+path
 */
export function handle4398(req: IncomingMessage, res: ServerResponse): void {
  if (!hostAllowed(req.headers.host)) {
    res.writeHead(403, { "content-type": "application/json" });
    res.end(JSON.stringify({ ok: false, error: "forbidden host" }));
    return;
  }
  if (req.method === "OPTIONS") {
    // 本机端点不需要跨域：直接拒绝预检，不返回 CORS 头
    res.writeHead(403);
    res.end();
    return;
  }
  if (!authOk(req.headers.authorization, getBridgeSecret())) {
    res.writeHead(401, { "content-type": "application/json" });
    res.end(JSON.stringify({ ok: false, error: "unauthorized" }));
    return;
  }
  if (req.method !== "POST") {
    res.writeHead(405, { "content-type": "application/json" });
    res.end(JSON.stringify({ ok: false, error: "method not allowed" }));
    return;
  }
  void handlePost(req, res);
}

async function handlePost(req: IncomingMessage, res: ServerResponse): Promise<void> {
  const body = await readJsonBody(req);
  if (!body.ok) {
    const status = body.status;
    res.writeHead(status, {
      "content-type": "application/json",
      // 超限未读完 body：响应后立即关闭连接，不继续累积
      ...(status === 413 ? { connection: "close" } : {}),
    });
    res.end(JSON.stringify({ ok: false, error: `bad request body (${status})` }));
    return;
  }
  const data = (body.data ?? null) as Record<string, unknown> | null;

  // 原生通道端点：POST /native {"text": "把空调调到25度"}
  // 把设备指令文本交给音箱原生小爱的 NLP 执行（本地直连设备，1-2 秒完成）
  // 桥的 native_device_command 工具走这里；仅供 AI 需要时调用原生快速通道
  if (req.url === "/native") {
    const text = data?.text;
    if (!text || typeof text !== "string") {
      res.writeHead(400, { "content-type": "application/json" });
      res.end(JSON.stringify({ ok: false, error: "empty text" }));
      return;
    }
    try {
      const ok = await OpenXiaoAI.speaker.askXiaoAI(text, { silent: data?.silent !== false });
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ ok }));
    } catch (e) {
      res.writeHead(500, { "content-type": "application/json" });
      res.end(JSON.stringify({ ok: false, error: String(e) }));
    }
    return;
  }

  // 本机调试端点：POST /exec {"cmd": "..."} 在音箱上执行 shell（loopback + secret 双保险）
  if (req.url === "/exec") {
    const cmd = data?.cmd;
    if (!cmd || typeof cmd !== "string") {
      res.writeHead(400, { "content-type": "application/json" });
      res.end(JSON.stringify({ ok: false, error: "empty cmd" }));
      return;
    }
    try {
      const result = await OpenXiaoAI.speaker.runShell(cmd, { timeout: 30000 });
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ ok: true, ...(result ?? {}) }));
    } catch (e) {
      res.writeHead(500, { "content-type": "application/json" });
      res.end(JSON.stringify({ ok: false, error: String(e) }));
    }
    return;
  }

  // 音频 URL 播放端点：POST /play_url {"url": "http(s)://...mp3"}
  // 深通道（DSH）搜到可公开直链的音频资源后，桥调用这里让音箱直接播放。
  // 仅限 loopback（127.0.0.1 监听）+ secret 鉴权；URL 过双重校验
  // （基础：协议/长度/控制字符/userinfo；私网/loopback/metadata 拒绝——
  // 公网 CDN 域名/IP 放行，域名不做 DNS 解析，解析后校验放桥侧/文档）。
  if (req.url === "/play_url") {
    const url = data?.url;
    const urlErr = checkPlayUrl(url);
    if (urlErr) {
      res.writeHead(400, { "content-type": "application/json" });
      res.end(JSON.stringify({ ok: false, error: urlErr }));
      return;
    }
    // 音乐语义 = 抢占式：新歌顶掉旧歌（停媒体服务 + 杀 miplayer + 立即播），HTTP 立即返回
    // 日志脱敏：只打 scheme+host+path，不打印 query（防 token 泄漏）
    console.log(`🎵 play_url 抢占播放: ${redactUrl(url as string)}`);
    void playUrlNow(url as string).catch((err) =>
      console.error("🔇 play_url 播放失败:", err),
    );
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ ok: true }));
    return;
  }

  if (req.url !== "/play") {
    res.writeHead(404);
    res.end();
    return;
  }

  // 推送播放端点：POST /play {"text": "..."} —— 桥的后台深化完成后让音箱补说
  const text = data?.text;
  if (!text || typeof text !== "string") {
    res.writeHead(400, { "content-type": "application/json" });
    res.end(JSON.stringify({ ok: false, error: "empty text" }));
    return;
  }
  try {
    // 对话控制标记：桥深通道推送带 <<dialogue:keep_open|end>>（2026-08-25 起兼容
    // (:music)? 后缀），剔除后播报；keep_open → 静默唤醒保持麦克风，其余 → 结束对话
    const marker = text.match(/<<dialogue:(keep_open|end)(:music)?>>/);
    const cleanText = text.replace(/<<dialogue:(keep_open|end)(:music)?>>/g, "").trim();
    const epoch = currentEpoch(); // 捕获到达时代际，过期自动丢弃
    await stopMusic(); // 深通道播报优先：音乐让位（不打断正在播的 TTS 回答，受屏障保护）
    await enqueuePlay(cleanText, epoch);
    if (marker && marker[1] === "keep_open") {
      await sleep(500);
      await enqueueWakeUp(epoch);
      console.log("🎤 推送播报 AI 判定保持对话，等待用户继续说话");
    }
    res.writeHead(200, { "content-type": "application/json" });
    res.end('{"ok":true}');
  } catch (e) {
    res.writeHead(500, { "content-type": "application/json" });
    res.end('{"ok":false}');
  }
}

async function main() {
  // 健康检查端点：音箱端 direct-mode.sh 用它探测 Mac 存活（0.0.0.0:4397）。
  // 单独端口 + 纯 HTTP 200 响应，避免 WS 端口(4399)对普通 HTTP 请求
  // 返回空/立即断连导致 curl exit code 不稳定（28/52/56 乱跳）。
  // 无鉴权但只返回最小信息 {"ok":true}（无泄露面）。
  createServer((req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end('{"ok":true}');
  }).listen(4397, "0.0.0.0", () =>
    console.log("✅ 健康检查端点: 0.0.0.0:4397/healthz")
  );

  // 推送播放端点：桥的后台深化完成后，POST /play 让音箱补说
  // 注意：必须放在 OpenXiaoAI.start() 之前，因为 start() 内部阻塞永不返回
  createServer(handle4398).listen(4398, "127.0.0.1", () =>
    console.log("✅ 推送播放端点: 127.0.0.1:4398/play")
  );

  initSpeakerGate(OpenXiaoAI.speaker as never); // 注入音箱后端
  await OpenXiaoAI.start(kOpenXiaoAIConfig);
}

// 直接执行入口（tsx migpt/index.ts）；被测试/其他模块 import 时不启动服务
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  void main();
}
