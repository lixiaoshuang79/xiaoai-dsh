import { kOpenXiaoAIConfig } from "config.js";
import { OpenXiaoAI } from "./xiaoai.js";
import { createServer } from "node:http";
import { sleep } from "@mi-gpt/utils";
import {
  currentEpoch,
  enqueuePlay,
  enqueueWakeUp,
  initSpeakerGate,
  playUrlNow,
  stopMusic,
} from "./speaker-gate.js";

async function main() {
  // 健康检查端点：音箱端 direct-mode.sh 用它探测 Mac 存活（0.0.0.0:4397）。
  // 单独端口 + 纯 HTTP 200 响应，避免 WS 端口(4399)对普通 HTTP 请求
  // 返回空/立即断连导致 curl exit code 不稳定（28/52/56 乱跳）。
  createServer((req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end('{"ok":true}');
  }).listen(4397, "0.0.0.0", () =>
    console.log("✅ 健康检查端点: 0.0.0.0:4397/healthz")
  );

  // 推送播放端点：桥的后台深化完成后，POST /play 让音箱补说
  // 注意：必须放在 OpenXiaoAI.start() 之前，因为 start() 内部阻塞永不返回
  createServer((req, res) => {
    // 原生通道端点：POST /native {"text": "把空调调到25度"}
    // 把设备指令文本交给音箱原生小爱的 NLP 执行（本地直连设备，1-2 秒完成）
    // 桥的 native_device_command 工具走这里；仅供 AI 需要时调用原生快速通道
    if (req.method === "POST" && req.url === "/native") {
      let body = "";
      req.on("data", (c) => (body += c));
      req.on("end", async () => {
        try {
          const { text, silent } = JSON.parse(body);
          if (!text || typeof text !== "string") throw new Error("empty text");
          const ok = await OpenXiaoAI.speaker.askXiaoAI(text, { silent: silent !== false });
          res.writeHead(200, { "content-type": "application/json" });
          res.end(JSON.stringify({ ok }));
        } catch (e) {
          res.writeHead(500, { "content-type": "application/json" });
          res.end(JSON.stringify({ ok: false, error: String(e) }));
        }
      });
      return;
    }
    // 本机调试端点：POST /exec {"cmd": "..."} 在音箱上执行 shell（限 loopback）
    if (req.method === "POST" && req.url === "/exec") {
      let body = "";
      req.on("data", (c) => (body += c));
      req.on("end", async () => {
        try {
          const { cmd } = JSON.parse(body);
          if (!cmd || typeof cmd !== "string") throw new Error("empty cmd");
          const result = await OpenXiaoAI.speaker.runShell(cmd, { timeout: 30000 });
          res.writeHead(200, { "content-type": "application/json" });
          res.end(JSON.stringify({ ok: true, ...(result ?? {}) }));
        } catch (e) {
          res.writeHead(500, { "content-type": "application/json" });
          res.end(JSON.stringify({ ok: false, error: String(e) }));
        }
      });
      return;
    }
    // 音频 URL 播放端点：POST /play_url {"url": "http(s)://...mp3"}
    // 深通道（DSH）搜到可公开直链的音频资源后，桥调用这里让音箱直接播放。
    // 仅限 loopback（127.0.0.1 监听），且只接受 http/https 音频 URL。
    if (req.method === "POST" && req.url === "/play_url") {
      let body = "";
      req.on("data", (c) => (body += c));
      req.on("end", async () => {
        try {
          const { url } = JSON.parse(body);
          if (!url || typeof url !== "string") throw new Error("empty url");
          if (!/^https?:\/\//i.test(url)) throw new Error("invalid url");
          // 音乐语义 = 抢占式：新歌顶掉旧歌（停媒体服务 + 杀 miplayer + 立即播），HTTP 立即返回
          console.log(`🎵 play_url 抢占播放: ${url.slice(0, 80)}...`);
          playUrlNow(url);
          res.writeHead(200, { "content-type": "application/json" });
          res.end(JSON.stringify({ ok: true }));
        } catch (e) {
          res.writeHead(500, { "content-type": "application/json" });
          res.end(JSON.stringify({ ok: false, error: String(e) }));
        }
      });
      return;
    }
    if (req.method !== "POST" || req.url !== "/play") {
      res.writeHead(404);
      res.end();
      return;
    }
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", async () => {
      try {
        const { text } = JSON.parse(body);
        if (!text) throw new Error("empty text");
        // 对话控制标记：桥深通道推送带 <<dialogue:keep_open|end>>，
        // 剔除后播报；keep_open → 静默唤醒保持麦克风，其余 → 结束对话
        const marker = text.match(/<<dialogue:(keep_open|end)>>/);
        const cleanText = text.replace(/<<dialogue:(keep_open|end)>>/g, "").trim();
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
    });
  }).listen(4398, "127.0.0.1", () =>
    console.log("✅ 推送播放端点: 127.0.0.1:4398/play")
  );

  initSpeakerGate(OpenXiaoAI.speaker as never); // 注入音箱后端
  await OpenXiaoAI.start(kOpenXiaoAIConfig);
}

main();
