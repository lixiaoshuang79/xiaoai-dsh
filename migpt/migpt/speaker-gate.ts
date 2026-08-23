/**
 * 播报互斥门：同一时刻音箱只能有一个声音。
 * 三条规则：
 * 1. 流式回答屏障：AI 流式回答的 chunk 必须连续播完——深通道推送 / 唤醒 /
 *    音乐等「回答外」播报在回答进行中挂起，等回答结束按顺序继续
 *    （根治病根：推送插在 chunk 之间 → "回答了一个字就被抢答"）。
 * 2. 对话代际（epoch）：每轮新对话 +1，旧对话排队中的播报段在轮到它时发现
 *    代际已过期 → 直接丢弃（用户插话后不再冒出旧回答的尾巴）。
 * 3. 音乐链独立（musicEpoch）：音乐（miplayer 拉流）与 TTS 分链——
 *    新歌顶旧歌，但放歌不打断 TTS 播报；AI 开始说话时停音乐让位。
 *
 * 队列可靠性（P0-1）：播放链上每个任务包一层 try/catch——单个任务失败
 * （play/runShell reject）只记录日志，链恢复 resolved，后续任务照常执行；
 * 各入口返回「本次任务」的 Promise，调用方 await 到的是本任务结果
 * （成功/失败都不影响整条链）。
 *
 * 本模块同时承载 shell/URL/HTTP 的共享加固 helper（零运行时依赖，可单测）：
 * 4398 端点（index.ts）与 config.ts 共用，测试直接 import 本模块。
 */
/** 音箱后端最小接口（结构类型，运行时零依赖，测试可注入 mock）。 */
export type SpeakerBackend = {
  play(opts: { text?: string; url?: string; blocking?: boolean }): Promise<unknown>;
  runShell(cmd: string, opts?: { timeout?: number }): Promise<unknown>;
  wakeUp(active: boolean): Promise<unknown>;
};

let speaker: SpeakerBackend | null = null;

/** 注入音箱后端（引擎启动后调用一次；测试注入 mock）。 */
export function initSpeakerGate(s: SpeakerBackend): void {
  speaker = s;
}

function sp(): SpeakerBackend {
  if (!speaker) throw new Error("speaker-gate 未注入音箱后端：先调 initSpeakerGate()");
  return speaker;
}

// ---- 播放链（resilient queue）----
let playChain: Promise<void> = Promise.resolve();
let dialogEpoch = 0;

/**
 * 把任务排入全局串行播放链，并返回本次任务的 Promise。
 * 任务内异常被捕获记录后链恢复 resolved；返回的 Promise 反映本任务
 * 成败（reject 只影响本任务的调用方，不阻塞后续排队任务）。
 */
function enqueueTask(task: () => Promise<void>): Promise<void> {
  let resolveTask!: () => void;
  let rejectTask!: (err: unknown) => void;
  const taskPromise = new Promise<void>((resolve, reject) => {
    resolveTask = resolve;
    rejectTask = reject;
  });
  playChain = playChain.then(async () => {
    try {
      await task();
      resolveTask();
    } catch (err) {
      // 异常只记录，链恢复 resolved——后续 .then 照常执行
      console.error("🔇 speaker-gate 播报任务失败（队列已恢复）:", err);
      rejectTask(err);
    }
  });
  return taskPromise;
}

// ---- 流式回答屏障 ----
let answerActive = false;
type PendingTask = {
  task: () => Promise<void>;
  resolve: () => void;
  reject: (err: unknown) => void;
};
const pendingAfterAnswer: PendingTask[] = [];

/** 标记流式回答开始（config.ts 播报循环前调用）。 */
export function beginAnswer(): void {
  answerActive = true;
}

/** 标记流式回答结束：挂起的回答外播报按到达顺序继续。幂等（重复调用安全）。 */
export function endAnswer(): void {
  answerActive = false;
  // 先复制再清空（同一数组引用，直接 length=0 会把副本也清掉）
  const pending = [...pendingAfterAnswer];
  pendingAfterAnswer.length = 0;
  for (const p of pending) {
    enqueueTask(p.task).then(p.resolve, p.reject);
  }
}

/** 回答外播报：回答进行中挂起，否则直接排入链尾。 */
function chainOrDelay(task: () => Promise<void>): Promise<void> {
  if (answerActive) {
    return new Promise<void>((resolve, reject) => {
      pendingAfterAnswer.push({ task, resolve, reject });
    });
  }
  return enqueueTask(task);
}

// ---- 对话代际 ----
/** 新一轮对话开始：代际 +1（作废旧对话排队中的播报段）。 */
export function newDialog(): number {
  dialogEpoch += 1;
  return dialogEpoch;
}

/** 当前代际（推送端点用它捕获「到达时」的对话状态）。 */
export function currentEpoch(): number {
  return dialogEpoch;
}

// ---- 音乐链 ----
let musicEpoch = 0;

/**
 * AI 开始说话（新对话）时停音乐让位；也作废排队中的音乐。
 * 内部吞错：停音乐是尽力而为（失败只记录，musicEpoch 已作废排队音乐），
 * 绝不向上抛造成 unhandled rejection。
 */
export async function stopMusic(): Promise<void> {
  musicEpoch += 1;
  try {
    await sp().runShell(
      "ubus call mediaplayer player_play_operation '{\"action\":\"stop\"}' 2>/dev/null; " +
      "mphelper pause 2>/dev/null; " +
      "for i in 1 2 3 4 5; do kill -9 $(/bin/pidof miplayer) 2>/dev/null; " +
      "/bin/pidof miplayer >/dev/null 2>&1 || break; sleep 0.3; done; true"
    );
  } catch (err) {
    console.error("🔇 停音乐失败（musicEpoch 已作废排队音乐）:", err);
  }
}

// ---- 播报入口 ----

/** 流式回答的 chunk 播报：连续排队（不经回答屏障），轮到它时代际过期则丢弃。 */
export function enqueueChunk(text: string, epoch: number): Promise<void> {
  return enqueueTask(async () => {
    if (epoch !== dialogEpoch) return; // 过期丢弃
    await sp().play({ text, blocking: true });
  });
}

/** 回答外播报（深通道推送等）：受回答屏障 + 代际保护。 */
export function enqueuePlay(text: string, epoch: number): Promise<void> {
  return chainOrDelay(async () => {
    if (epoch !== dialogEpoch) return; // 过期丢弃
    await sp().play({ text, blocking: true });
  });
}

/** 排队静默唤醒（保持麦克风）；受回答屏障 + 代际保护。 */
export function enqueueWakeUp(epoch: number): Promise<void> {
  return chainOrDelay(async () => {
    if (epoch !== dialogEpoch) return;
    await sp().wakeUp(true);
  });
}

/**
 * 播放音频 URL（音乐语义：新歌顶旧歌，不排队）：
 * 作废旧音乐 → 停媒体服务当前播放项（防播完自动续下一个）→
 * 循环杀光旧 miplayer → 后台起新 miplayer 拉流。
 * 受回答屏障保护：AI 正在说话时先等它说完（不打断 TTS）。
 * 走 miplayer（音箱已验证可在线播 B 站 CDN 流）；ubus player_play_url
 * 返回 code 0 但实际不播放（不可靠，弃用）。
 * URL 在入口先过严格校验（P0-2），进 shell 前再经 shq() 单引号转义。
 */
export function playUrlNow(url: string): Promise<void> {
  if (!isValidPlayUrl(url)) {
    console.error("🔇 playUrlNow: 非法 URL 拒绝播放:", redactUrl(url));
    return Promise.resolve();
  }
  musicEpoch += 1;
  const myEpoch = musicEpoch;
  return enqueueTask(async () => {
    if (musicEpoch !== myEpoch) return; // 被新歌/停音乐作废
    await sp().runShell(
      "ubus call mediaplayer player_play_operation '{\"action\":\"stop\"}' 2>/dev/null; " +
      "mphelper pause 2>/dev/null; "
    );
    await sp().runShell(
      "for i in 1 2 3 4 5; do kill -9 $(pidof miplayer) 2>/dev/null; " +
      "pidof miplayer >/dev/null 2>&1 || break; sleep 0.3; done; sleep 0.3"
    );
    if (musicEpoch !== myEpoch) return;
    // ( cmd & ) 双括号后台：exec shell 退出后 miplayer 继续活
    await sp().runShell(
      `( miplayer -f ${shq(url)} >/dev/null 2>&1 & )`
    );
  });
}

/**
 * 清空排队中未播的内容（立即闭嘴场景用）；同时停音乐。
 * 只作废旧排队段：运行中的段在执行副作用前检查 epoch 已足够（见各任务开头）；
 * flush 后新排队任务照常执行（playChain 已换新链）。
 */
export function flushPlayQueue(): void {
  dialogEpoch += 1;
  playChain = Promise.resolve();
  answerActive = false;
  // 挂起中的回答外播报作废：结算为「已丢弃」，调用方不会永久挂起
  const pending = [...pendingAfterAnswer];
  pendingAfterAnswer.length = 0;
  for (const p of pending) p.resolve();
  void stopMusic();
}

// ---- shell 安全与 URL 校验（P0-2；4398 /play_url 与测试共用）----

/** 严格 POSIX 单引号 quoting：s 内每个 ' 替换为 '\''（close-quote + escaped-quote + open-quote）。 */
/** POSIX shell 单引号转义：任意文本（含未定义值）安全嵌入单条命令参数。 */
export function shq(s: string | undefined | null): string {
  const t = s ?? "";
  return "'" + t.replace(/'/g, `'\\''`) + "'";
}

const PLAY_URL_MAX_LEN = 2048;

/**
 * 播放 URL 基础校验：仅 http/https；长度 ≤ 2048；无 ASCII 空白/控制字符；
 * 无 userinfo（//user:pass@）；ASCII 字符限定在 URL 安全集合
 * （A-Za-z0-9:/?&=._~%+#-），非 ASCII（中文/unicode 路径）放行。
 */
export function isValidPlayUrl(url: unknown): url is string {
  if (typeof url !== "string") return false;
  if (url.length === 0 || url.length > PLAY_URL_MAX_LEN) return false;
  if (!/^https?:\/\//i.test(url)) return false;
  // C0/C1 控制字符 + DEL 一律拒绝
  if (/[\u0000-\u001f\u007f-\u009f]/.test(url)) return false;
  // ASCII 空白显式拒绝（虽被白名单覆盖，语义更清晰；含 NBSP）
  if (/[ \t\r\n\v\f\u00a0]/.test(url)) return false;
  // userinfo：scheme 之后的 authority 含 @ 拒绝；authority 为空（http://）拒绝
  const afterScheme = url.slice(url.indexOf("://") + 3);
  const authority = afterScheme.match(/^[^/?#]*/)?.[0] ?? "";
  if (authority.length === 0) return false; // 无 host（http:// 之类）
  if (authority.includes("@")) return false;
  // 非 ASCII（中文路径等）放行；ASCII 部分走白名单
  const ascii = url.replace(/[^\x00-\x7f]/g, "");
  if (!/^[A-Za-z0-9:/?&=._~%+#\-]*$/.test(ascii)) return false;
  return true;
}

/**
 * 拒绝解析到 loopback/私网/metadata 的 hostname（DNS rebinding 防御）。
 * 纯字面量判断，不解析 DNS（解析后校验放桥侧/文档说明）。
 */
export function isBlockedHostname(hostname: string): boolean {
  let h = hostname.toLowerCase();
  if (h.startsWith("[")) h = h.slice(1, h.endsWith("]") ? -1 : undefined); // [::1] → ::1
  if (h.length === 0) return true; // 空 host（http:///a 之类）
  if (h === "localhost" || h === "0.0.0.0" || h === "::" || h === "::1") return true;
  // IPv4-mapped IPv6（::ffff:127.0.0.1）递归判定
  if (h.startsWith("::ffff:")) return isBlockedHostname(h.slice(7));
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(h)) {
    const [a, b] = h.split(".").map(Number) as [number, number];
    if (a === 0 || a === 127 || a === 10) return true; // 0.* 127.* 10.*
    if (a === 169 && b === 254) return true; // link-local + cloud metadata
    if (a === 172 && b >= 16 && b <= 31) return true; // 172.16-31.*
    if (a === 192 && b === 168) return true; // 192.168.*
    return false;
  }
  // 域名不解析 DNS，放行（公网 CDN 必须放行）
  return false;
}

/** 日志脱敏：只打 scheme://host/path（去掉 query，防 token 泄漏）。 */
export function redactUrl(url: string): string {
  try {
    const u = new URL(url);
    return `${u.protocol}//${u.host}${u.pathname}`;
  } catch {
    return "(invalid url)";
  }
}

/** /play_url 完整校验：基础校验 + new URL() 解析 + hostname 私网/loopback 拒绝。通过返回 null，否则返回错误信息。 */
export function checkPlayUrl(url: unknown): string | null {
  if (!isValidPlayUrl(url)) return "invalid url";
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return "invalid url";
  }
  if (isBlockedHostname(parsed.hostname)) return "blocked host";
  return null;
}

// ---- HTTP 加固 helper（P1-5；4398 端点与测试共用）----

/**
 * Host 校验（DNS rebinding 防御）：仅接受 127.0.0.1 / localhost（含端口）。
 * 如 "Host: 127.0.0.1:4398" / "Host: localhost"。
 */
export function hostAllowed(hostHeader: string | undefined): boolean {
  if (!hostHeader) return false;
  const h = hostHeader.toLowerCase();
  const hostname = h.startsWith("[") ? h.slice(0, h.indexOf("]") + 1) : h.replace(/:\d+$/, "");
  return hostname === "127.0.0.1" || hostname === "localhost";
}

/** 统一鉴权：Authorization: Bearer <secret>（scheme 大小写不敏感，secret 精确匹配）。 */
export function authOk(authHeader: string | undefined, secret: string): boolean {
  if (!authHeader || !secret) return false;
  const m = authHeader.match(/^Bearer\s+(\S+)$/i);
  if (!m) return false;
  return m[1] === secret;
}

export type JsonBodyResult = { ok: true; data: unknown } | { ok: false; status: number };

const MAX_BODY_BYTES = 64 * 1024;

/**
 * 流式读取 JSON body：Content-Length 预检（缺失/非法/负数 → 400，超限 → 413）+
 * 流式累积上限截断（超限立即 413 并停止累积，不继续读）。
 */
export function readJsonBody(req: import("node:http").IncomingMessage): Promise<JsonBodyResult> {
  return new Promise((resolve) => {
    const clRaw = req.headers["content-length"];
    if (clRaw !== undefined) {
      const cl = Number(clRaw);
      if (!Number.isFinite(cl) || cl < 0) {
        resolve({ ok: false, status: 400 });
        return;
      }
      if (cl > MAX_BODY_BYTES) {
        resolve({ ok: false, status: 413 });
        req.pause(); // 超限：停止累积，响应后连接关闭
        return;
      }
    }
    const chunks: Buffer[] = [];
    let total = 0;
    req.on("data", (c: Buffer) => {
      total += c.length;
      if (total > MAX_BODY_BYTES) {
        req.pause(); // 超限：停止累积（413 由调用方返回）
        resolve({ ok: false, status: 413 });
        return;
      }
      chunks.push(c);
    });
    req.on("end", () => {
      try {
        resolve({ ok: true, data: JSON.parse(Buffer.concat(chunks).toString("utf-8")) });
      } catch {
        resolve({ ok: false, status: 400 });
      }
    });
    req.on("error", () => {
      resolve({ ok: false, status: 400 });
    });
  });
}
