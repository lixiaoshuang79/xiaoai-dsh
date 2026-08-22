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

let playChain: Promise<void> = Promise.resolve();
let dialogEpoch = 0;

// ---- 流式回答屏障 ----
let answerActive = false;
const pendingAfterAnswer: Array<() => void> = [];

/** 标记流式回答开始（config.ts 播报循环前调用）。 */
export function beginAnswer(): void {
  answerActive = true;
}

/** 标记流式回答结束：挂起的回答外播报按到达顺序继续。 */
export function endAnswer(): void {
  answerActive = false;
  // 先复制再清空（同一数组引用，直接 length=0 会把副本也清掉）
  const pending = [...pendingAfterAnswer];
  pendingAfterAnswer.length = 0;
  for (const fn of pending) fn();
}

/** 回答外播报：回答进行中挂起，否则直接排入链尾。 */
function chainOrDelay(task: () => Promise<void>): void {
  if (answerActive) {
    pendingAfterAnswer.push(() => {
      playChain = playChain.then(task);
    });
  } else {
    playChain = playChain.then(task);
  }
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

/** AI 开始说话（新对话）时停音乐让位；也作废排队中的音乐。 */
export async function stopMusic(): Promise<void> {
  musicEpoch += 1;
  await sp().runShell(
    "ubus call mediaplayer player_play_operation '{\"action\":\"stop\"}' 2>/dev/null; " +
    "mphelper pause 2>/dev/null; " +
    "for i in 1 2 3 4 5; do kill -9 $(/bin/pidof miplayer) 2>/dev/null; " +
    "/bin/pidof miplayer >/dev/null 2>&1 || break; sleep 0.3; done; true"
  );
}

// ---- 播报入口 ----

/** 流式回答的 chunk 播报：连续排队（不经回答屏障），轮到它时代际过期则丢弃。 */
export function enqueueChunk(text: string, epoch: number): Promise<void> {
  playChain = playChain.then(async () => {
    if (epoch !== dialogEpoch) return; // 过期丢弃
    await sp().play({ text, blocking: true });
  });
  return playChain;
}

/** 回答外播报（深通道推送等）：受回答屏障 + 代际保护。 */
export function enqueuePlay(text: string, epoch: number): Promise<void> {
  const myChain = chainOrDelay(async () => {
    if (epoch !== dialogEpoch) return; // 过期丢弃
    await sp().play({ text, blocking: true });
  });
  return playChain;
}

/** 排队静默唤醒（保持麦克风）；受回答屏障 + 代际保护。 */
export function enqueueWakeUp(epoch: number): Promise<void> {
  chainOrDelay(async () => {
    if (epoch !== dialogEpoch) return;
    await sp().wakeUp(true);
  });
  return playChain;
}

/**
 * 播放音频 URL（音乐语义：新歌顶旧歌，不排队）：
 * 作废旧音乐 → 停媒体服务当前播放项（防播完自动续下一个）→
 * 循环杀光旧 miplayer → 后台起新 miplayer 拉流。
 * 受回答屏障保护：AI 正在说话时先等它说完（不打断 TTS）。
 * 走 miplayer（音箱已验证可在线播 B 站 CDN 流）；ubus player_play_url
 * 返回 code 0 但实际不播放（不可靠，弃用）。
 */
export function playUrlNow(url: string): void {
  musicEpoch += 1;
  const myEpoch = musicEpoch;
  chainOrDelay(async () => {
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
      `( miplayer -f '${url}' >/dev/null 2>&1 & )`
    );
  });
}

/** 清空排队中未播的内容（立即闭嘴场景用）；同时停音乐。 */
export function flushPlayQueue(): void {
  dialogEpoch += 1;
  playChain = Promise.resolve();
  answerActive = false;
  pendingAfterAnswer.length = 0;
  void stopMusic();
}
