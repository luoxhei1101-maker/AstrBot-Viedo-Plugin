/**
 * a-bogus 常驻签名 worker。
 *
 * 为什么需要它
 * ============
 *
 * `a_bogus.cjs` 是逆向出来的混淆 JS（约 460 行），每次生成签名都要：
 *
 * 1. 启动一个 node 进程（冷启动 40–80ms）
 * 2. `require()` 并解析执行整个混淆脚本（40–120ms）
 * 3. 才算一次签名（本身 <5ms）
 *
 * 也就是 **99% 的时间花在启动和加载上**，签名只是附带的。抖音一次解析要签
 * 1 次（主接口），开评论还要再签；用户连续发几条链接就累积成几百 ms 的纯浪费。
 *
 * 这个 worker 让 node 常驻：进程只启动一次、脚本只 `require` 一次，
 * 之后每次签名走 stdin/stdout 的行协议，实测降到 **亚毫秒级**。
 *
 * 协议
 * ====
 *
 * 一行一个 JSON 请求，一行一个 JSON 响应：
 *
 * ```
 * 请求: {"q": "<url_search_params>", "ua": "<user_agent>"}
 * 成功: {"ok": true, "v": "<a_bogus 值>"}
 * 失败: {"ok": false, "e": "<错误信息>"}
 * ```
 *
 * 用行协议而不是长连接套接字：stdin/stdout 是管道，Python 侧
 * `asyncio.create_subprocess_exec` 直接就能读写，不需要额外开端口、
 * 也不用担心端口占用和防火墙。
 *
 * 保活
 * ====
 *
 * 父进程退出后 stdin 关闭，`readline` 收到 EOF，进程自然结束——不会留僵尸。
 * 另外自己监听 `SIGTERM`，便于 Python 侧主动关停。
 */

"use strict";

const readline = require("readline");
const path = require("path");

let mod = null;
let loadError = null;

try {
    // 签名算法模块和本文件同目录，用 __dirname 定位，不依赖 cwd
    mod = require(path.join(__dirname, "a_bogus.cjs"));
} catch (err) {
    // 加载失败不要立刻退出——把错误留在 loadError 里，等第一个请求进来时
    // 通过协议回给 Python 侧，这样 Python 能拿到具体原因（比静默 EOF 好排查）
    loadError = err && err.message ? err.message : String(err);
}

function respond(obj) {
    process.stdout.write(JSON.stringify(obj) + "\n");
}

const rl = readline.createInterface({ input: process.stdin, terminal: false });

rl.on("line", (line) => {
    const text = line.trim();
    if (!text) return;

    let req;
    try {
        req = JSON.parse(text);
    } catch (err) {
        respond({ ok: false, e: "请求不是合法 JSON" });
        return;
    }

    if (loadError) {
        respond({ ok: false, e: "a_bogus.cjs 加载失败: " + loadError });
        return;
    }

    try {
        // 每次调用都会重新生成随机串，多次调用之间没有状态耦合，
        // 所以常驻复用是安全的
        const value = mod.generate_a_bogus(req.q, req.ua);
        respond({ ok: true, v: value });
    } catch (err) {
        respond({ ok: false, e: err && err.message ? err.message : String(err) });
    }
});

rl.on("close", () => {
    process.exit(0);
});

process.on("SIGTERM", () => {
    process.exit(0);
});

// 就绪信号：Python 侧可以据此确认 worker 起来了（避免把就绪行当响应读）
respond({ ok: true, ready: true });
