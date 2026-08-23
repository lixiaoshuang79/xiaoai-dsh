import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createServer, request as httpRequest } from "node:http";
import { AddressInfo } from "node:net";
import { connect } from "node:net";
import { mkdtempSync, mkdirSync, writeFileSync, chmodSync, readFileSync, existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  authOk,
  beginAnswer,
  checkPlayUrl,
  currentEpoch,
  endAnswer,
  enqueueChunk,
  enqueuePlay,
  enqueueWakeUp,
  flushPlayQueue,
  hostAllowed,
  initSpeakerGate,
  isValidPlayUrl,
  isBlockedHostname,
  newDialog,
  playUrlNow,
  readJsonBody,
  redactUrl,
  shq,
  stopMusic,
  type SpeakerBackend,
} from "../migpt/speaker-gate.js";

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

function deferred<T = void>() {
  let resolve!: (v: T | PromiseLike<T>) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function timeout(ms: number, msg: string): Promise<never> {
  return new Promise((_, rej) => setTimeout(() => rej(new Error(msg)), ms));
}

/** 可注入行为的 mock 后端：记录所有调用。 */
function makeMock() {
  const calls: {
    play: { text?: string; url?: string; blocking?: boolean }[];
    runShell: string[];
    wakeUp: boolean[];
  } = { play: [], runShell: [], wakeUp: [] };
  let playImpl: (opts: { text?: string; url?: string; blocking?: boolean }) => Promise<unknown> = async () => ({
    stdout: '"code": 0',
    stderr: "",
    exit_code: 0,
  });
  let runShellImpl: (cmd: string) => Promise<unknown> = async () => ({
    stdout: "",
    stderr: "",
    exit_code: 0,
  });
  const backend: SpeakerBackend = {
    play: (opts) => {
      calls.play.push(opts);
      return playImpl(opts);
    },
    runShell: (cmd) => {
      calls.runShell.push(cmd);
      return runShellImpl(cmd);
    },
    wakeUp: (active) => {
      calls.wakeUp.push(active);
      return Promise.resolve({ stdout: '"code": 0', stderr: "", exit_code: 0 });
    },
  };
  return {
    backend,
    calls,
    setPlayImpl: (f: typeof playImpl) => (playImpl = f),
    setRunShellImpl: (f: typeof runShellImpl) => (runShellImpl = f),
  };
}

function playTexts(calls: { play: { text?: string; url?: string; blocking?: boolean }[] }): string[] {
  return calls.play.map((c) => c.text ?? "");
}

// ============ P0-1 队列可靠性 ============

test("P0-1 队列恢复：单次 play reject 后，下一条 enqueueChunk 仍能播", async () => {
  const mock = makeMock();
  initSpeakerGate(mock.backend);
  const epoch = currentEpoch();
  let n = 0;
  mock.setPlayImpl(async () => {
    n += 1;
    if (n === 1) throw new Error("模拟播放失败");
    return { exit_code: 0 };
  });
  // 第一条 reject（只影响本任务调用方）
  await assert.rejects(enqueueChunk("第一句", epoch), /模拟播放失败/);
  // 第二条照常播放（链已恢复）
  await enqueueChunk("第二句", epoch);
  assert.deepEqual(playTexts(mock.calls), ["第一句", "第二句"]);
});

test("P0-1 队列恢复：playUrlNow 的 runShell reject 后，后续任务仍执行", async () => {
  const mock = makeMock();
  initSpeakerGate(mock.backend);
  const epoch = currentEpoch();
  let n = 0;
  mock.setRunShellImpl(async () => {
    n += 1;
    if (n === 1) throw new Error("模拟 shell 失败");
    return { exit_code: 0 };
  });
  // playUrlNow 的第一个 runShell（停媒体服务）失败 → 本任务 reject
  await assert.rejects(playUrlNow("http://example.com/a.mp3"), /模拟 shell 失败/);
  // 链恢复：后续播报照常
  await enqueueChunk("恢复后的播报", epoch);
  assert.ok(mock.calls.play.some((c) => c.text === "恢复后的播报"));
});

test("P0-1 stopMusic 内部吞错：runShell reject 不向上抛，后续任务不挂起", async () => {
  const mock = makeMock();
  initSpeakerGate(mock.backend);
  mock.setRunShellImpl(async () => {
    throw new Error("shell boom");
  });
  await stopMusic(); // 不应 reject
  const epoch = currentEpoch();
  await enqueueChunk("还能播", epoch); // 链未被污染
  assert.ok(mock.calls.play.some((c) => c.text === "还能播"));
});

test("P0-1 flushPlayQueue：旧排队段不执行（作废），新排队任务照常执行", async () => {
  const mock = makeMock();
  initSpeakerGate(mock.backend);
  const e1 = currentEpoch();
  const block = deferred();
  mock.setPlayImpl(async () => block.promise);
  const p1 = enqueueChunk("first", e1); // 运行中（阻塞在 block）
  await Promise.resolve(); // 让 p1 的任务真正开始（play 调用已发出）
  const p2 = enqueueChunk("second", e1); // 排队中（尚未开始）
  flushPlayQueue(); // 作废排队段
  block.resolve();
  await p1;
  await p2; // 旧排队段执行时发现代际过期 → 丢弃
  const e2 = currentEpoch();
  await enqueueChunk("third", e2); // 新链照常
  assert.deepEqual(playTexts(mock.calls), ["first", "third"]);
  assert.ok(!playTexts(mock.calls).includes("second"));
});

test("P0-1 epoch 过期丢弃：newDialog 作废旧对话排队中的播报段", async () => {
  const mock = makeMock();
  initSpeakerGate(mock.backend);
  const e1 = currentEpoch();
  const block = deferred();
  mock.setPlayImpl(async () => block.promise);
  const p1 = enqueueChunk("old-running", e1); // 运行中（执行前已检查 epoch，继续播）
  await Promise.resolve(); // 让 p1 真正开始播（play 调用已发出）
  const p2 = enqueueChunk("old-queued", e1); // 排队中
  newDialog(); // 新对话：e1 过期
  block.resolve();
  await p1;
  await p2; // 轮到它时 epoch 不匹配 → 丢弃
  const e2 = currentEpoch();
  await enqueueChunk("new", e2);
  const texts = playTexts(mock.calls);
  assert.ok(texts.includes("old-running"));
  assert.ok(!texts.includes("old-queued"));
  assert.ok(texts.includes("new"));
});

// ============ P0-2 shell 注入防护 ============

test("P0-2 shq：单引号/换行/分号/$()/反引号都被安全引用", () => {
  assert.equal(shq("abc"), "'abc'");
  assert.equal(shq("a'b"), `'a'\\''b'`);
  assert.equal(shq("http://x/a; touch /tmp/pwn"), `'http://x/a; touch /tmp/pwn'`);
  assert.equal(shq("http://x/a$(id)"), `'http://x/a$(id)'`);
  assert.equal(shq("http://x/a`id`"), `'http://x/a\`id\`'`);
  assert.equal(shq("a\nb"), `'a\nb'`);
  assert.equal(shq(""), "''");
});

test("P0-2 真实 shell：恶意 URL 经 shq 引用后不产生新命令", async () => {
  const tmp = mkdtempSync(join(tmpdir(), "shq-test-"));
  const bin = join(tmp, "bin");
  mkdirSync(bin);
  const logPath = join(tmp, "args.log");
  const pwnPath = join(tmp, "PWNED");
  writeFileSync(join(bin, "miplayer"), `#!/bin/sh\necho "$2" >> "${logPath}"\n`);
  chmodSync(join(bin, "miplayer"), 0o755);
  try {
    const urls = [
      "http://x/a'b",
      `http://x/a; touch ${pwnPath}`,
      `http://x/a$(touch ${pwnPath})`,
      `http://x/a\`touch ${pwnPath}\``,
      `http://x/a\nb`,
      "http://x/a'&&reboot",
    ];
    for (const url of urls) {
      rmSync(logPath, { force: true });
      rmSync(pwnPath, { force: true });
      // 与 playUrlNow 完全相同的命令模板
      const cmd = `( miplayer -f ${shq(url)} >/dev/null 2>&1 & )`;
      const r = spawnSync("sh", ["-c", cmd], {
        env: { ...process.env, PATH: `${bin}:${process.env.PATH ?? ""}` },
      });
      assert.equal(r.status, 0, `sh 退出码非 0: ${r.stderr?.toString()}`);
      // 轮询等待后台 miplayer 落盘参数
      let log = "";
      for (let i = 0; i < 100; i++) {
        try {
          log = readFileSync(logPath, "utf-8").trim();
          if (log) break;
        } catch {
          /* 还没写 */
        }
        await sleep(10);
      }
      assert.equal(log, url, `URL 未被原样作为单个参数传递: ${JSON.stringify(url)}`);
      assert.ok(!existsSync(pwnPath), `注入成功，产生了副作用: ${JSON.stringify(url)}`);
    }
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

test("P0-2 playUrlNow 入口拒绝非法 URL：不播放、不执行 shell", async () => {
  const mock = makeMock();
  initSpeakerGate(mock.backend);
  const bad = [
    "http://x/a'; touch /tmp/x", // 单引号注入
    "http://x/a\nb", // 换行
    "http://x/a\tb", // tab
    "http://user:pass@x/a", // userinfo
    "ftp://x/a", // 非 http(s)
  ];
  for (const url of bad) {
    await playUrlNow(url); // 返回 resolved，不 reject
    assert.equal(mock.calls.runShell.length, 0, `非法 URL 不应执行 shell: ${url}`);
    assert.equal(mock.calls.play.length, 0, `非法 URL 不应播放: ${url}`);
  }
  // 合法 URL 照常走链路
  await playUrlNow("http://example.com/a.mp3");
  assert.equal(mock.calls.runShell.length, 3); // stop + kill + 起播
});

// ============ P0-2 URL 校验 ============

test("P0-2 isValidPlayUrl：白名单/长度/控制字符/userinfo", () => {
  // 合法
  for (const ok of [
    "http://example.com/a.mp3",
    "https://cdn.example.com/歌曲/风.mp3",
    "http://h:8080/p?x=1&y=2",
    "http://h/p%20x",
    "https://upos-sz-mirrorcos.bilivideo.com/upgcx/code/1/av.mp4?from=spv&trid=abc",
    "http://h/~user",
    "http://h/a+b#frag",
    "http://h/" + "a".repeat(2048 - 9), // 恰好 2048
  ]) {
    assert.equal(isValidPlayUrl(ok), true, `应放行: ${ok.slice(0, 40)}`);
  }
  // 非法
  const bad = [
    "",
    "http://",
    "ftp://h/a",
    "h/a",
    "http://h/a b", // 空白
    "http://h/a\tb",
    "http://h/a\nb",
    "http://h/a\u0001b", // C0 控制
    "http://h/a\u007fb", // DEL
    "http://h/a\u0085b", // C1 控制
    "http://user:pass@h/a", // userinfo
    "http://h/a'b", // 单引号
    'http://h/a"b', // 双引号
    "http://h/a`b", // 反引号
    "http://h/a;b", // 分号
    "http://h/a$(b)", // 命令替换
    "http://h/a$b",
    "http://h/a\\b", // 反斜杠
    "http://h/" + "a".repeat(2048 - 9 + 1), // 超 2048
    null,
    123,
    undefined,
  ] as unknown[];
  for (const u of bad) {
    assert.equal(isValidPlayUrl(u), false, `应拒绝: ${JSON.stringify(String(u).slice(0, 40))}`);
  }
});

test("P0-2 isBlockedHostname：私网/loopback/metadata 拒绝，公网放行", () => {
  for (const blocked of [
    "localhost",
    "127.0.0.1",
    "127.5.5.5",
    "10.1.2.3",
    "169.254.169.254", // cloud metadata
    "192.168.1.1",
    "172.16.0.1",
    "172.31.255.255",
    "0.0.0.0",
    "0.1.2.3",
    "::1",
    "[::1]",
    "::",
    "::ffff:127.0.0.1",
    "::FFFF:10.0.0.1",
  ]) {
    assert.equal(isBlockedHostname(blocked), true, `应拒绝: ${blocked}`);
  }
  for (const allowed of [
    "8.8.8.8",
    "1.1.1.1",
    "203.0.113.5",
    "example.com",
    "cdn.example.com",
    "upos-sz-mirrorcos.bilivideo.com",
    "100.64.1.1", // CGNAT 不在拒绝清单
    "172.15.0.1",
    "172.32.0.1",
    "192.169.1.1",
    "169.253.1.1",
  ]) {
    assert.equal(isBlockedHostname(allowed), false, `应放行: ${allowed}`);
  }
});

test("P0-2 checkPlayUrl：私网/loopback URL 整体拒绝", () => {
  assert.equal(checkPlayUrl("http://127.0.0.1:8123/api/states"), "blocked host");
  assert.equal(checkPlayUrl("http://169.254.169.254/latest/meta-data"), "blocked host");
  assert.equal(checkPlayUrl("http://192.168.1.1/x"), "blocked host");
  assert.equal(checkPlayUrl("http://localhost:8123/x"), "blocked host");
  assert.equal(checkPlayUrl("http://x/a'b"), "invalid url");
  assert.equal(checkPlayUrl("http://example.com/a.mp3"), null);
});

test("P0-2 redactUrl：日志只打 scheme+host+path，query 脱敏", () => {
  assert.equal(redactUrl("http://h/p?token=secret&x=1"), "http://h/p");
  assert.equal(redactUrl("https://h:8443/p?t=1"), "https://h:8443/p");
  assert.equal(redactUrl("not-a-url"), "(invalid url)");
});

// ============ P1-9 回答屏障 ============

test("P1-9 回答屏障：beginAnswer 期间回答外播报挂起，endAnswer 后执行；endAnswer 幂等", async () => {
  const mock = makeMock();
  initSpeakerGate(mock.backend);
  const epoch = currentEpoch();
  beginAnswer();
  const p = enqueuePlay("屏障外播报", epoch);
  await sleep(30);
  assert.ok(!mock.calls.play.some((c) => c.text === "屏障外播报"), "回答中不应播报");
  // chunk 不受屏障影响
  await enqueueChunk("chunk", epoch);
  assert.ok(mock.calls.play.some((c) => c.text === "chunk"));
  endAnswer();
  endAnswer(); // 幂等：第二次调用无副作用
  await Promise.race([p, timeout(1000, "enqueuePlay 挂起（屏障未释放）")]);
  assert.ok(mock.calls.play.some((c) => c.text === "屏障外播报"));
});

test("P1-9 flushPlayQueue 结算挂起中的回答外播报：不永久挂起", async () => {
  const mock = makeMock();
  initSpeakerGate(mock.backend);
  const epoch = currentEpoch();
  beginAnswer();
  const p = enqueuePlay("将被作废", epoch);
  flushPlayQueue();
  await Promise.race([p, timeout(500, "enqueuePlay 永久挂起")]);
  assert.ok(!mock.calls.play.some((c) => c.text === "将被作废"));
});

// ============ P1-5 HTTP 加固 helper ============

test("P1-5 hostAllowed：仅 127.0.0.1 / localhost（含端口）", () => {
  for (const ok of ["127.0.0.1:4398", "127.0.0.1", "localhost:4398", "localhost", "LOCALHOST:4398"]) {
    assert.equal(hostAllowed(ok), true, `应放行: ${ok}`);
  }
  for (const bad of [
    undefined,
    "",
    "192.168.1.5:4398",
    "example.com",
    "127.0.0.1.evil.com",
    "[::1]:4398",
    "127.0.0.1:evil",
  ]) {
    assert.equal(hostAllowed(bad), false, `应拒绝: ${String(bad)}`);
  }
});

test("P1-5 authOk：Bearer <secret> 精确匹配", () => {
  assert.equal(authOk("Bearer abc123", "abc123"), true);
  assert.equal(authOk("bearer abc123", "abc123"), true); // scheme 大小写不敏感
  assert.equal(authOk("Bearer abc124", "abc123"), false);
  assert.equal(authOk(undefined, "abc123"), false);
  assert.equal(authOk("Bearer abc123", ""), false); // secret 为空：一律拒绝
  assert.equal(authOk("Basic abc123", "abc123"), false);
  assert.equal(authOk("Bearer", "abc123"), false);
  assert.equal(authOk("Bearer abc123 extra", "abc123"), false);
});

test("P1-5 readJsonBody：合法解析 / 非法 JSON / Content-Length 超限 / chunked 超限 / 负 CL", async () => {
  const server = createServer(async (req, res) => {
    const r = await readJsonBody(req);
    if (!r.ok) {
      res.writeHead(r.status, { "content-type": "application/json" });
      res.end(JSON.stringify({ status: r.status }));
      return;
    }
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify(r.data));
  });
  await new Promise<void>((res) => server.listen(0, "127.0.0.1", () => res()));
  const { port } = server.address() as AddressInfo;
  try {
    // 合法 JSON
    const okRes = await fetch(`http://127.0.0.1:${port}/`, {
      method: "POST",
      body: JSON.stringify({ a: 1, b: "x" }),
    });
    assert.equal(okRes.status, 200);
    assert.deepEqual(await okRes.json(), { a: 1, b: "x" });

    // 非法 JSON
    const badJson = await fetch(`http://127.0.0.1:${port}/`, { method: "POST", body: "{oops" });
    assert.equal(badJson.status, 400);

    // Content-Length 超 64KB
    const tooBig = await fetch(`http://127.0.0.1:${port}/`, {
      method: "POST",
      body: JSON.stringify({ big: "x".repeat(70_000) }),
    });
    assert.equal(tooBig.status, 413);

    // chunked（无 Content-Length）流式超限
    const chunked = await new Promise<number>((resolve, reject) => {
      const req = httpRequest(
        { host: "127.0.0.1", port, method: "POST", headers: { "Transfer-Encoding": "chunked" } },
        (res) => {
          res.resume();
          resolve(res.statusCode ?? 0);
        },
      );
      req.on("error", reject);
      req.write("x".repeat(70_000));
      req.end();
    });
    assert.equal(chunked, 413);

    // 负 Content-Length（原始 socket，绕过客户端校验）
    const negCl = await new Promise<number>((resolve, reject) => {
      const sock = connect(port, "127.0.0.1", () => {
        sock.write("POST / HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: -5\r\n\r\n");
      });
      let data = "";
      sock.on("data", (c: Buffer) => (data += c.toString()));
      sock.on("end", () => {
        const m = data.match(/^HTTP\/1\.1 (\d{3})/);
        resolve(m ? Number(m[1]) : 0);
      });
      sock.on("error", reject);
    });
    assert.equal(negCl, 400);

    // 64KB 以内大 JSON 正常
    const nearLimit = await fetch(`http://127.0.0.1:${port}/`, {
      method: "POST",
      body: JSON.stringify({ s: "y".repeat(63_000) }),
    });
    assert.equal(nearLimit.status, 200);
  } finally {
    await new Promise<void>((res) => server.close(() => res()));
  }
});

// ============ 播放链 Promise 语义 ============

test("入口返回本任务 Promise：enqueueWakeUp 正常排队执行", async () => {
  const mock = makeMock();
  initSpeakerGate(mock.backend);
  const epoch = currentEpoch();
  await enqueueWakeUp(epoch);
  assert.deepEqual(mock.calls.wakeUp, [true]);
});
