import { test } from "node:test";
import assert from "node:assert/strict";
import {
  extractBridgeMarkers,
  getBridgeSecret,
  kOpenXiaoAIConfig,
  normalizeBridgeSecret,
} from "../config.js";
import {
  currentEpoch,
  enqueuePlay,
  initSpeakerGate,
} from "../migpt/speaker-gate.js";

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

// ============ 桥控制标记流式提取（跨 chunk 分裂） ============

test("标记提取：单 chunk 完整标记剥离且不播报", () => {
  const m = extractBridgeMarkers("你好<<dialogue:keep_open>>");
  assert.equal(m.playable, "你好");
  assert.equal(m.keep, "");
  assert.equal(m.dialogueAction, "keep_open");
  assert.equal(m.nativePass, false);
});

test("标记提取：dialogue 标记跨 2 个 chunk 分裂", () => {
  const a = extractBridgeMarkers("正文<<dialogue:keep", false);
  assert.equal(a.playable, "正文");
  assert.equal(a.keep, "<<dialogue:keep"); // 尾部疑似标记前缀 → 留到下一 chunk
  assert.equal(a.dialogueAction, "");
  const b = extractBridgeMarkers(a.keep + "_open>>", false);
  assert.equal(b.playable, "");
  assert.equal(b.keep, "");
  assert.equal(b.dialogueAction, "keep_open");
});

test("标记提取：dialogue 标记跨 3 个 chunk 分裂", () => {
  let buf = "a<<di";
  let m = extractBridgeMarkers(buf, false);
  assert.equal(m.playable, "a");
  assert.equal(m.keep, "<<di");
  buf = m.keep + "alogue:en";
  m = extractBridgeMarkers(buf, false);
  assert.equal(m.playable, "");
  assert.equal(m.keep, "<<dialogue:en"); // 整体仍是合法标记前缀
  buf = m.keep + "d>>尾";
  m = extractBridgeMarkers(buf, false);
  assert.equal(m.dialogueAction, "end");
  assert.equal(m.playable, "尾");
});

test("标记提取：native_passthrough 跨 chunk 分裂", () => {
  const a = extractBridgeMarkers("<<native_pass", false);
  assert.equal(a.keep, "<<native_pass");
  assert.equal(a.nativePass, false);
  const b = extractBridgeMarkers(a.keep + "through>>", false);
  assert.equal(b.nativePass, true);
  assert.equal(b.playable, "");
});

test("标记提取：流结束时残缺标记按控制杂质丢弃，不播报", () => {
  const m = extractBridgeMarkers("正文<<dialogue:keep", true);
  assert.equal(m.playable, "正文");
  assert.equal(m.keep, "");
  assert.equal(m.dialogueAction, "");
  // 非标记的 << 尾巴（如正文）不受影响
  const m2 = extractBridgeMarkers("他说 << 很厉害", true);
  assert.equal(m2.playable, "他说 << 很厉害");
});

test("标记提取：无标记纯文本原样返回", () => {
  const m = extractBridgeMarkers("普通文本", false);
  assert.equal(m.playable, "普通文本");
  assert.equal(m.dialogueAction, "");
  assert.equal(m.nativePass, false);
});

test("标记提取：多个标记（最后一个 dialogue 动作生效）", () => {
  const m = extractBridgeMarkers("<<dialogue:end>>正文<<dialogue:keep_open>>", false);
  assert.equal(m.dialogueAction, "keep_open");
  assert.equal(m.playable, "正文");
});

// ============ 2026-08-25 :music 后缀（点歌轮标记） ============

test("标记提取：:music 后缀（点歌轮）——dialogue 动作照常、musicMark 置位", () => {
  const m = extractBridgeMarkers("好嘞<<dialogue:end:music>>");
  assert.equal(m.playable, "好嘞");
  assert.equal(m.dialogueAction, "end");
  assert.equal(m.musicMark, true);
  assert.equal(m.nativePass, false);
});

test("标记提取：:music 标记跨 chunk 分裂后仍识别", () => {
  const a = extractBridgeMarkers("<<dialogue:keep_open:mu", false);
  assert.equal(a.keep, "<<dialogue:keep_open:mu");
  assert.equal(a.musicMark, false);
  const b = extractBridgeMarkers(a.keep + "sic>>", false);
  assert.equal(b.dialogueAction, "keep_open");
  assert.equal(b.musicMark, true);
  assert.equal(b.playable, "");
});

test("标记提取：流结束时残缺 :music 标记按控制杂质丢弃", () => {
  const m = extractBridgeMarkers("正文<<dialogue:end:mus", true);
  assert.equal(m.playable, "正文");
  assert.equal(m.dialogueAction, "");
  assert.equal(m.musicMark, false);
});

test("标记提取：不带 :music 的普通 dialogue 标记不影响 musicMark", () => {
  const m = extractBridgeMarkers("<<dialogue:keep_open>>", false);
  assert.equal(m.musicMark, false);
  assert.equal(m.dialogueAction, "keep_open");
});

// ============ P1-9 onMessage try/finally：异常路径复位 answerActive ============

/** 构造 fake engine：stream.read 由调用方控制。 */
function makeFakeEngine(
  reads: Array<() => { next?: string; noMore?: boolean }>,
  msg: { text: string },
) {
  const calls: {
    play: unknown[];
    runShell: unknown[];
    wakeUp: unknown[];
    askAI: unknown[];
  } = { play: [], runShell: [], wakeUp: [], askAI: [] };
  let i = 0;
  const engine = {
    lastMsg: msg,
    speaker: {
      setPlaying: async () => true,
      runShell: async (cmd: string) => {
        calls.runShell.push(cmd);
        return { stdout: "", stderr: "", exit_code: 0 };
      },
      play: async (opts: unknown) => {
        calls.play.push(opts);
        return { stdout: '"code": 0', stderr: "", exit_code: 0 };
      },
      wakeUp: async (active: boolean) => {
        calls.wakeUp.push(active);
        return { stdout: '"code": 0', stderr: "", exit_code: 0 };
      },
      askXiaoAI: async () => true,
    },
    askAI: async () => {
      calls.askAI.push(true);
      return {
        stream: {
          read: () => reads[i++](),
          cancel: () => {},
        },
      };
    },
  };
  return { engine, calls };
}

// 探测桥健康时带 fetch：stub 成 200，避免真实网络
const origFetch = globalThis.fetch;
function stubBridgeHealthy(): void {
  globalThis.fetch = (async () => new Response("{}", { status: 200 })) as typeof fetch;
}
function restoreFetch(): void {
  globalThis.fetch = origFetch;
}

test("P1-9 异常路径仍调用 endAnswer：answerActive 复位，后续 enqueuePlay 不挂起", async () => {
  stubBridgeHealthy();
  try {
    const msg = { text: "你好" };
    const reads: Array<() => { next?: string; noMore?: boolean }> = [
      () => ({ next: "第一段", noMore: false }),
      () => {
        throw new Error("stream boom");
      },
    ];
    const { engine, calls } = makeFakeEngine(reads, msg);
    initSpeakerGate(engine.speaker as never);
    // 整个 onMessage 抛错（stream.read 异常 → finally endAnswer 后向上传播）
    await assert.rejects(kOpenXiaoAIConfig.onMessage(engine as never, msg as never), /stream boom/);
    // 第一段已播
    assert.ok(calls.play.some((c) => (c as { text?: string }).text === "第一段"));
    // 异常后 answerActive 必须已复位：enqueuePlay 立即执行而非挂起
    const epoch = currentEpoch();
    const p = enqueuePlay("异常后的播报", epoch);
    await Promise.race([
      p,
      new Promise<never>((_, rej) =>
        setTimeout(() => rej(new Error("enqueuePlay 被挂起（answerActive 未复位）")), 2000),
      ),
    ]);
    assert.ok(calls.play.some((c) => (c as { text?: string }).text === "异常后的播报"));
  } finally {
    restoreFetch();
  }
});

test("P1-9 正常结束：keep_open 标记剥离不播报，播完静默唤醒", async () => {
  stubBridgeHealthy();
  try {
    const msg = { text: "继续聊" };
    const reads: Array<() => { next?: string; noMore?: boolean }> = [
      () => ({ next: "好的", noMore: false }),
      () => ({ next: "<<dialogue:keep_open>>", noMore: false }),
      () => ({ next: undefined, noMore: true }),
    ];
    const { engine, calls } = makeFakeEngine(reads, msg);
    initSpeakerGate(engine.speaker as never);
    const ret = await kOpenXiaoAIConfig.onMessage(engine as never, msg as never);
    assert.deepEqual(ret, { handled: true });
    assert.ok(calls.play.some((c) => (c as { text?: string }).text === "好的"));
    assert.ok(!calls.play.some((c) => String((c as { text?: string }).text).includes("<<dialogue")));
    assert.equal(calls.wakeUp.length, 1); // keep_open → 静默唤醒
  } finally {
    restoreFetch();
  }
});

test("控制标记跨 chunk 分裂时 onMessage 仍正确：不播标记、keep_open 生效", async () => {
  stubBridgeHealthy();
  try {
    const msg = { text: "聊个天" };
    const reads: Array<() => { next?: string; noMore?: boolean }> = [
      () => ({ next: "正", noMore: false }),
      () => ({ next: "文<<dialogue:keep", noMore: false }),
      () => ({ next: "_open>>", noMore: false }),
      () => ({ next: undefined, noMore: true }),
    ];
    const { engine, calls } = makeFakeEngine(reads, msg);
    initSpeakerGate(engine.speaker as never);
    const ret = await kOpenXiaoAIConfig.onMessage(engine as never, msg as never);
    assert.deepEqual(ret, { handled: true });
    // 正文按到达分块播报（"正"+"文" 两段连续播），标记本身绝不播
    const played = calls.play.map((c) => (c as { text?: string }).text).join("");
    assert.equal(played, "正文");
    assert.ok(!calls.play.some((c) => String((c as { text?: string }).text).includes("<<dialogue")));
    assert.equal(calls.wakeUp.length, 1); // keep_open → 静默唤醒
  } finally {
    restoreFetch();
  }
});

test("native_passthrough 跨 chunk 分裂：不播报，放行原生", async () => {
  stubBridgeHealthy();
  try {
    const msg = { text: "放首歌" };
    const reads: Array<() => { next?: string; noMore?: boolean }> = [
      () => ({ next: "<<native_pass", noMore: false }),
      () => ({ next: "through>>", noMore: false }),
      () => ({ next: undefined, noMore: true }),
    ];
    const { engine, calls } = makeFakeEngine(reads, msg);
    initSpeakerGate(engine.speaker as never);
    const ret = await kOpenXiaoAIConfig.onMessage(engine as never, msg as never);
    assert.deepEqual(ret, { handled: true });
    assert.equal(calls.play.length, 0); // 无任何播报
    assert.equal(calls.wakeUp.length, 0); // 放行原生：不唤醒
  } finally {
    restoreFetch();
  }
});

test("用户插话：cancel + 代际作废，finally 复位 answerActive", async () => {
  stubBridgeHealthy();
  try {
    const msg = { text: "讲故事" };
    const reads: Array<() => { next?: string; noMore?: boolean }> = [
      () => ({ next: "从前有座山", noMore: false }),
      () => ({ next: "山里有座庙", noMore: false }),
      () => ({ next: undefined, noMore: true }), // 实际不会走到：第二次 read 后已 break
    ];
    const { engine, calls } = makeFakeEngine(reads, msg);
    initSpeakerGate(engine.speaker as never);
    // 模拟用户插话：engine.lastMsg 变化 → 下一次 read 时 break
    const onMessagePromise = kOpenXiaoAIConfig.onMessage(engine as never, msg as never);
    await sleep(50); // 等第一段播完（barrier 内）
    (engine as { lastMsg: unknown }).lastMsg = { text: "打断" }; // 插话
    const ret = await onMessagePromise;
    assert.deepEqual(ret, { handled: true });
    // 只有第一段被播
    assert.ok(calls.play.some((c) => (c as { text?: string }).text === "从前有座山"));
    assert.ok(!calls.play.some((c) => (c as { text?: string }).text === "山里有座庙"));
    // answerActive 已复位：后续播报不挂起
    const epoch = currentEpoch();
    await Promise.race([
      enqueuePlay("插话后的播报", epoch),
      new Promise<never>((_, rej) =>
        setTimeout(() => rej(new Error("enqueuePlay 被挂起")), 2000),
      ),
    ]);
  } finally {
    restoreFetch();
  }
});

test("点歌轮（:music 标记）：播报后不开麦、不播标记、直接结束", async () => {
  stubBridgeHealthy();
  try {
    const msg = { text: "放首歌" };
    const reads: Array<() => { next?: string; noMore?: boolean }> = [
      () => ({ next: "找到了，马上放。", noMore: false }),
      () => ({ next: "<<dialogue:end:music>>", noMore: false }),
      () => ({ next: undefined, noMore: true }),
    ];
    const { engine, calls } = makeFakeEngine(reads, msg);
    initSpeakerGate(engine.speaker as never);
    const ret = await kOpenXiaoAIConfig.onMessage(engine as never, msg as never);
    assert.deepEqual(ret, { handled: true });
    assert.ok(calls.play.some((c) => (c as { text?: string }).text === "找到了，马上放。"));
    assert.ok(!calls.play.some((c) => String((c as { text?: string }).text).includes("<<dialogue")));
    assert.equal(calls.wakeUp.length, 0); // 点歌轮不开麦
  } finally {
    restoreFetch();
  }
});

test("停止词分支（2026-08-25）：闭嘴静默打断不播确认；停止播放播确认语", async () => {
  // 「闭嘴」= 静默组：不播确认语，但走带 hook 的干净重启
  const e1 = makeFakeEngine([], { text: "闭嘴" });
  initSpeakerGate(e1.engine.speaker as never);
  const r1 = await kOpenXiaoAIConfig.onMessage(e1.engine as never, { text: "闭嘴" } as never);
  assert.deepEqual(r1, { handled: true });
  assert.equal(e1.calls.play.length, 0); // 静默：无任何播报
  assert.ok(e1.calls.runShell.some((c) => c.includes("restart-aivs.sh")));
  // 「停止」= 停音乐组：给一句确认，用户知道音乐停了
  const e2 = makeFakeEngine([], { text: "停止" });
  initSpeakerGate(e2.engine.speaker as never);
  const r2 = await kOpenXiaoAIConfig.onMessage(e2.engine as never, { text: "停止" } as never);
  assert.deepEqual(r2, { handled: true });
  assert.ok(e2.calls.play.some((c) => (c as { text?: string }).text === "好的，已为您停止。"));
  assert.ok(e2.calls.runShell.some((c) => c.includes("restart-aivs.sh")));
});

test("停止词分支：前缀匹配（停止播放xxx 也命中），标点剥离后匹配", async () => {
  const e = makeFakeEngine([], { text: "把歌关了！" });
  initSpeakerGate(e.engine.speaker as never);
  const r = await kOpenXiaoAIConfig.onMessage(e.engine as never, { text: "把歌关了！" } as never);
  assert.deepEqual(r, { handled: true });
  assert.ok(e.calls.play.some((c) => (c as { text?: string }).text === "好的，已为您停止。"));
});

// ============ bridge.secret 读取 ============

test("normalizeBridgeSecret：非字符串/空白 → 空串，字符串 trim", () => {
  assert.equal(normalizeBridgeSecret("  abc123  "), "abc123");
  assert.equal(normalizeBridgeSecret(""), "");
  assert.equal(normalizeBridgeSecret("   "), "");
  assert.equal(normalizeBridgeSecret(null), "");
  assert.equal(normalizeBridgeSecret(undefined), "");
  assert.equal(normalizeBridgeSecret(123), "");
});

test("getBridgeSecret：仓库无 local.json/generated 时为空串（鉴权不可用但进程不崩）", () => {
  assert.equal(typeof getBridgeSecret(), "string");
  // 本仓库 checkout 没有 config/local.json 与 config/generated/bridge-secret
  assert.equal(getBridgeSecret(), "");
});

// 模块级副作用检查：sleep 保留引用（防误删 import 影响其他测试）
void sleep;
