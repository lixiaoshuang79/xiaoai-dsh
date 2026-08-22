import { sleep } from "@mi-gpt/utils";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { OpenXiaoAIConfig } from "./migpt/xiaoai.js";
import {
  beginAnswer,
  endAnswer,
  enqueueChunk,
  enqueuePlay,
  enqueueWakeUp,
  flushPlayQueue,
  initSpeakerGate,
  newDialog,
  stopMusic,
} from "./migpt/speaker-gate.js";

/**
 * 统一配置：仓库根 config/local.json（由 localhost 配置后台生成维护；
 * 首次使用可复制 config/config.example.json 为 config/local.json）。
 */
interface RepoConfig {
  llm?: {
    base_url?: string;
    api_key?: string;
    fast_model?: string;
    system_prompt?: string;
  };
}

function loadRepoConfig(): RepoConfig {
  for (const rel of ["../config/local.json", "../config/config.example.json"]) {
    try {
      return JSON.parse(
        readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf-8"),
      ) as RepoConfig;
    } catch {
      /* 继续尝试下一个 */
    }
  }
  return {};
}

const repoConfig = loadRepoConfig();

/**
 * 大模型直连兜底：当本地桥（127.0.0.1:8322，xiaogpt-bridge）挂了、或者本机 DSH
 * 大脑挂了时，migpt 直接调用户配置的大模型（OpenAI 兼容），
 * 保证音箱始终能直接调用大模型，不依赖桥和 DSH。
 */
const LLM_BASE = (repoConfig.llm?.base_url || "").replace(/\/+$/, "");
const LLM_MODEL = repoConfig.llm?.fast_model || "";

let _llmKey = "";
function loadLlmKey(): string {
  if (_llmKey) return _llmKey;
  _llmKey = process.env.LLM_API_KEY || repoConfig.llm?.api_key || "";
  if (!_llmKey) {
    console.error("⚠️ 未配置大模型 API Key（config/local.json 的 llm.api_key），直连兜底不可用");
  }
  return _llmKey;
}

interface LlmMessage {
  role: "system" | "user" | "assistant";
  content: string;
}

const LLM_FALLBACK_SYSTEM =
  (repoConfig.llm?.system_prompt || "你是小爱，这家的智能语音管家。").trim()
    .replace(/[。！？]$/, "") +
  "。当前你的设备控制工具和深度思考通道暂时不可用（后台大脑离线），" +
  "只能做基础问答。回答口语化、简短，不超过三句话，中文。" +
  "遇到需要查设备、开灯关灯、查询实时数据这类做不了的事，" +
  "就说「后台大脑暂时离线了，这类事情请稍后再试」，不要编造。" +
  "简单的常识问答、聊天、算术直接回答。";

/**
 * 直接调用户配置的大模型（OpenAI 兼容，原生 fetch，无 SDK 依赖）。
 * 非流式，返回完整回答文本。
 */
async function askLlmDirect(question: string): Promise<string> {
  const apiKey = loadLlmKey();
  if (!apiKey) {
    throw new Error("大模型 API Key 缺失");
  }
  const resp = await fetch(`${LLM_BASE}/chat/completions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify({
      model: LLM_MODEL,
      messages: [
        { role: "system", content: LLM_FALLBACK_SYSTEM },
        { role: "user", content: question },
      ] satisfies LlmMessage[],
      thinking: { type: "disabled" },
    }),
  });
  if (!resp.ok) {
    throw new Error(`大模型 HTTP ${resp.status}`);
  }
  const data = (await resp.json()) as {
    choices?: { message?: { content?: string } }[];
    error?: unknown;
  };
  if (data.error) {
    throw new Error(`大模型错误: ${JSON.stringify(data.error)}`);
  }
  return data.choices?.[0]?.message?.content?.trim() || "";
}

/**
 * 探测本地桥（127.0.0.1:8322）健康状态。
 * 桥挂了时避免每次唤醒都等超时：健康结果缓存 10 秒，失败结果缓存 3 秒
 * （桥恢复后很快自动切回，不重启 migpt 也能恢复）。
 */
let _bridgeCache = { ok: true, ts: 0 };
async function isBridgeHealthy(): Promise<boolean> {
  const now = Date.now();
  const ttl = _bridgeCache.ok ? 10_000 : 3_000;
  if (now - _bridgeCache.ts < ttl) {
    return _bridgeCache.ok;
  }
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 1200);
    const resp = await fetch("http://127.0.0.1:8322/v1/models", {
      signal: ctrl.signal,
    });
    clearTimeout(timer);
    _bridgeCache = { ok: resp.ok, ts: now };
  } catch {
    _bridgeCache = { ok: false, ts: now };
  }
  return _bridgeCache.ok;
}

/**
 * 完整意图框架（2026-08-22 重构）：
 * migpt 不再做任何路由/意图判断（关键词表全部退役）。所有文本发桥，
 * 桥侧 classify_intent 分类到意图体系（device_control/media/reminder/query_time/
 * weather/knowledge/realtime/chitchat/dialogue_mgmt/deep_task/fallback 11 域 40+ 意图），
 * 按意图路由（native 放行官方 / flash 直答 / flash_tools 带工具 / deep 深通道）。
 * migpt 只解析桥下发的两种控制标记：
 *   <<native_passthrough>> = 放行官方小爱应答（migpt 不播报）
 *   <<dialogue:keep_open|end>> = 对话状态（播报完是否静默唤醒保持麦克风）
 */

/**
 * 需要原生的指令由桥的意图分类决定（media/reminder 域 → native 路由），
 * migpt 本地不再维护白名单。
 */

export const kOpenXiaoAIConfig: OpenXiaoAIConfig = {
  openai: {
    /**
     * 本地大脑桥（xiaogpt-bridge，OpenAI 兼容）
     */
    baseURL: "http://127.0.0.1:8322/v1",
    apiKey: "dsh-local",
    model: "dsh-local",
  },
  prompt: {
    system:
      repoConfig.llm?.system_prompt ||
      "你是小爱，这家的智能语音管家。" +
        "回答必须口语化、简洁：一般不超过三句话，不要使用列表、序号、链接或英文，语气专业、干练又带一点管家式的沉稳，称呼用户为「先生」。" +
        "铁律：永远不要说出你的思考过程、查询步骤或工具名称，不要复述你查到的原始数据细节，不要念出设备的技术编号，直接说最终结论。",
  },
  context: {
    /**
     * 每次对话携带的最大历史消息数
     */
    historyMaxLength: 10,
  },
  /**
   * 官方回答彻底禁用：所有问题全部走本地 AI（空串 startsWith 恒为真）
   */
  callAIKeywords: [""],
  /**
   * 自定义消息回复
   */
  async onMessage(engine, msg) {
    const text = msg.text;
    initSpeakerGate(engine.speaker as never); // 注入音箱后端（幂等）

    if (text === "测试播放文字") {
      return { text: "你好，很高兴认识你！" };
    }

    // 打断指令：立刻停止正在播放的回答。
    // 不用 abortXiaoAI（init.d restart 会把音箱端 hook 注入的环境冲掉），
    // 改用带 hook 的干净重启脚本，停播并保持拦截器在线
    if (["闭嘴", "别说了", "停下", "别念了", "安静"].includes(text)) {
      await engine.speaker.setPlaying(false);
      flushPlayQueue(); // 清空排队中的播报（深通道推送等一律作废）
      try {
        await engine.speaker.runShell(
          "/data/open-xiaoai/restart-aivs.sh /data/open-xiaoai/hook_final.so",
          { timeout: 30000 },
        );
      } catch (err) {
        console.error("闭嘴重启原生失败:", err);
      }
      return { handled: true };
    }

    // 单独的「小爱同学」唤醒词：若 ASR 偶尔捕获到裸唤醒词，不回答
    // （唤醒反馈由原生自身的唤醒音负责，不 abort 以免冲掉 hook 注入）
    const wakeWords = ["小爱同学", "小爱同学。"];
    if (wakeWords.includes(text.trim())) {
      return { handled: true };
    }

    // ---- 默认通路：所有文本都发桥（意图识别在桥侧统一完成） ----
    // 用户对音箱说话 = AI 优先：若音乐（miplayer）在播，先停音乐让位
    await stopMusic();
    // 关键决策：不 abort 原生小爱。原因：
    // ① abort = init.d restart（1-2 秒原生失聪），且会把音箱端 hook 注入的环境冲掉；
    // ② 官方云端设备执行链路已被音箱端 hook 锁死，设备指令不会双执行；
    // ③ 设备指令由 AI 通过 HA 通道执行（hass-mcp 本地直连），速度接近原生；
    // ④ 原生提示音已全部静音，原生并行处理不产生杂音。
    // ⑤ 容灾：本地桥（8322）挂了 → 直接调用户配置的大模型兜底，音箱始终能直接调用大模型。
    //    注意：engine.askAI 内部吞掉连接错误（播「出错了」），所以先主动探测桥健康，
    //    桥不健康时根本不去调 askAI，直接走大模型直连。
    const bridgeOk = await isBridgeHealthy();
    let reply: Awaited<ReturnType<typeof engine.askAI>>;
    if (bridgeOk) {
      reply = await engine.askAI(msg);
    } else {
      console.error(`❌ 本地桥不可用，大模型直连兜底: ${text}`);
      const fallbackEpoch = newDialog();
      try {
        const fallbackText = await askLlmDirect(text);
        if (fallbackText) {
          await enqueuePlay(fallbackText, fallbackEpoch);
        } else {
          await enqueuePlay("后台大脑暂时离线了，请稍后再试，先生。", fallbackEpoch);
        }
      } catch (err2) {
        console.error(`❌ 大模型直连也失败: ${(err2 as Error)?.message ?? err2}`);
        await enqueuePlay("抱歉，本地和云端的大模型都暂时连不上，请稍后再试。", fallbackEpoch);
      }
      return { handled: true };
    }
    if (!reply.stream) {
      return { handled: true };
    }
    // 2. 逐段播放流式回答（与引擎默认 _response 相同，但解析桥下发的控制标记）
    //    <<native_passthrough>> = 放行官方小爱应答，migpt 不播报
    //    <<dialogue:keep_open|end>> = 播报完是否静默唤醒保持麦克风
    //    播报走播报门（speaker-gate）：全局串行互斥 + 对话代际过期丢弃 +
    //    流式回答屏障（chunk 连续播，深通道推送等回答外播报不插队）。
    const epoch = newDialog();
    beginAnswer(); // 流式回答开始：回答外播报（推送/唤醒/音乐）挂起
    let fullText = "";
    let dialogueAction = ""; // keep_open | end | ""（默认 end）
    let nativePass = false;
    while (true) {
      const { next, noMore } = reply.stream.read();
      if (!next && noMore) {
        break;
      }
      if (next) {
        // 用户插话打断：停止播放本次回答，作废排队中的播报段
        if (engine.lastMsg !== msg) {
          reply.stream.cancel();
          newDialog(); // 代际 +1：旧播报段全作废
          break;
        }
        if (next.includes("<<native_passthrough>>")) {
          nativePass = true;
          console.log("🏠 桥意图判定：放行原生应答");
          continue; // 控制标记不播报
        }
        const marker = next.match(/<<dialogue:(keep_open|end)>>/);
        if (marker) {
          dialogueAction = marker[1] ?? "";
          continue; // 控制标记不播报
        }
        fullText += next;
        console.log(`🔊 ${next}`);
        await enqueueChunk(next, epoch); // chunk 连续播，不经回答屏障
      }
      await sleep(100);
    }
    endAnswer(); // 流式回答结束：挂起的推送/唤醒/音乐按顺序继续
    if (nativePass) {
      return { handled: true }; // 放行原生：官方小爱自己应答，migpt 不播
    }
    // 3. 对话控制：AI 意图识别（桥侧独立判断）驱动，
    //    框架只执行（keep_open → 静默唤醒保持麦克风一轮；其余 → 结束对话）。
    if (dialogueAction === "keep_open") {
      await sleep(500);
      await enqueueWakeUp(epoch);
      console.log("🎤 意图判定保持对话（keep_open），等待用户继续说话");
    }
    return { handled: true };
  },
};
