// 桌面壳 -> 本机后端这条「合法第一方路径」的端到端证明。
//
// 为什么单独一个脚本：闸门改的是「谁能对本机服务下写操作」，而界面与桌面壳正是那两个
// 合法调用方 —— 这条路径不能被假设，必须跑一遍真的。做法与 electron/main.cjs 完全一致：
// 用同一个 electron/api-proxy.cjs 生成/读取令牌、起同一个转发服务、用同一个解释器起真后端
// （backend/main.py，独立临时数据目录 + 独立端口），然后按四条判据要结果。
// 全程不碰真显卡：非 GET 只打 POST /api/agent/tool 的只读工具与 /api/session-token。
//
//     node scripts/test-local-guard.mjs      （npm run test:guard）
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';

const ROOT = path.resolve(import.meta.dirname, '..');
const BACKEND_ENTRY = path.join(ROOT, 'backend', 'main.py');
// api-proxy.cjs 是 CommonJS；Windows 上 import 绝对路径要 file:// URL，直接 require 更省事。
const { proxyApiRequest, resolveToken, tokenFilePath, tokenForRunningBackend } = createRequire(
  import.meta.url,
)(path.join(ROOT, 'electron', 'api-proxy.cjs'));

const results = [];
const check = async (name, fn) => {
  try {
    await fn();
    results.push(`PASS ${name}`);
  } catch (error) {
    results.push(`FAIL ${name}\n  ${error instanceof Error ? error.stack || error.message : String(error)}`);
  }
};

// 与 electron/main.cjs 的 findPythonRuntime() 同一个判据：能不能用不看命令名在不在 PATH，
// 看它 import 得起 fastapi + uvicorn（Windows 上 python 常常是商店占位程序）。
const PROBE_CODE = 'import fastapi, uvicorn; print("ok")';

function findPython() {
  for (const command of [process.env.VOCAL_SEPARATOR_PYTHON, 'python', 'python3', 'py'].filter(Boolean)) {
    const prefixArgs = /^py(\.exe)?$/i.test(command) ? ['-3'] : [];
    const probe = spawnSync(command, [...prefixArgs, '-c', PROBE_CODE], {
      encoding: 'utf8',
      timeout: 60000,
      windowsHide: true,
    });
    if (probe.status === 0 && probe.stdout.includes('ok')) return { command, prefixArgs };
  }
  throw new Error('没找到装了 fastapi + uvicorn 的 Python：用 VOCAL_SEPARATOR_PYTHON 指定解释器后再跑');
}

function freePort() {
  return new Promise((resolve, reject) => {
    const probe = http.createServer();
    probe.once('error', reject);
    probe.listen(0, '127.0.0.1', () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });
}

function request(url, { method = 'GET', headers = {}, body = null } = {}) {
  return new Promise((resolve, reject) => {
    const target = new URL(url);
    const req = http.request(
      {
        host: target.hostname,
        port: target.port,
        path: `${target.pathname}${target.search}`,
        method,
        headers,
        timeout: 15000,
      },
      (res) => {
        let text = '';
        res.setEncoding('utf8');
        res.on('data', (chunk) => { text += chunk; });
        res.on('end', () => {
          let json = null;
          try { json = JSON.parse(text || 'null'); } catch { /* 留着 null，断言按原文报错 */ }
          resolve({ status: res.statusCode, json, text, headers: res.headers });
        });
      },
    );
    req.on('timeout', () => req.destroy(new Error('请求超时')));
    req.on('error', reject);
    req.end(body);
  });
}

const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'vocal-guard-'));
fs.mkdirSync(path.join(dataDir, 'uploads'), { recursive: true });
fs.mkdirSync(path.join(dataDir, 'outputs'), { recursive: true });
const port = await freePort();
const base = `http://127.0.0.1:${port}`;

// 与 electron/main.cjs 的 ensureBackend() 同一份代码：没令牌就生成一个、写进 security.json，
// 再用 VOCAL_SEPARATOR_TOKEN 交给后端子进程。
const guard = resolveToken({ env: {}, dataDir, create: true });
assert.ok(guard.token.length >= 32, '壳应当生成一个足够长的令牌');
assert.equal(guard.source, 'generated');

const interpreter = findPython();
const backend = spawn(
  interpreter.command,
  [...interpreter.prefixArgs, BACKEND_ENTRY],
  {
    cwd: path.join(ROOT, 'backend'),
    env: {
      ...process.env,
      PYTHONUTF8: '1',
      PYTHONIOENCODING: 'utf-8',
      PYTHONUNBUFFERED: '1',
      VOCAL_SEPARATOR_DATA_DIR: dataDir,
      VOCAL_SEPARATOR_PORT: String(port),
      VOCAL_SEPARATOR_TOKEN: guard.token,
    },
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  },
);
let backendLog = '';
backend.stdout.on('data', (chunk) => { backendLog += chunk; });
backend.stderr.on('data', (chunk) => { backendLog += chunk; });

// 渲染服务：桌面壳就是这么把 /api/* 转出去的（随机端口 + 改写 Host + 补令牌）。
const renderer = http.createServer((req, res) => {
  proxyApiRequest(req, res, { host: '127.0.0.1', port, token: guard.token });
});
const rendererUrl = await new Promise((resolve) => {
  renderer.listen(0, '127.0.0.1', () => resolve(`http://127.0.0.1:${renderer.address().port}`));
});

let spawned = true;
try {
  // 等后端就绪（最多 60 秒：这台机器上 fastapi 冷启动要几秒）。
  let ready = false;
  for (let attempt = 0; attempt < 120; attempt += 1) {
    if (backend.exitCode !== null) break;
    try {
      const health = await request(`${base}/api/health`, { headers: { origin: 'http://127.0.0.1:3000' } });
      if (health.status === 200 && health.json?.status === 'ok') { ready = true; break; }
    } catch { /* 还没起来 */ }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  if (!ready) throw new Error(`后端没起来（退出码 ${backend.exitCode}）：\n${backendLog.slice(-2000)}`);

  await check('健康检查把闸门配置说清楚（含令牌文件路径，且不含令牌本体）', async () => {
    const health = await request(`${base}/api/health`);
    const described = health.json.data.guard;
    assert.equal(described.token_required, true);
    assert.equal(described.token_header, 'x-vocal-token');
    // 不比字符串写法（Python 会把 8.3 短名展开成长名）：直接按它报的路径把令牌读回来。
    assert.equal(path.basename(described.token_file), 'security.json');
    assert.equal(JSON.parse(fs.readFileSync(described.token_file, 'utf8')).token, guard.token);
    assert.ok(!JSON.stringify(health.json).includes(guard.token), '/api/health 不许回显令牌');
  });

  await check('桌面壳经代理发非 GET：Host 被改成回环 + 带令牌 -> 通过', async () => {
    const response = await request(`${rendererUrl}/api/agent/tool`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', origin: rendererUrl },
      body: JSON.stringify({ tool: 'vocal.mode_list', input: {} }),
    });
    assert.equal(response.status, 200, response.text.slice(0, 300));
    assert.equal(response.json.ok, true);
    assert.ok(Array.isArray(response.json.data.presets));
  });

  await check('界面用的 /api/session-token 走代理拿得到同一份令牌', async () => {
    const response = await request(`${rendererUrl}/api/session-token`, { headers: { origin: rendererUrl } });
    assert.equal(response.status, 200, response.text.slice(0, 300));
    assert.equal(response.json.required, true);
    assert.equal(response.json.token, guard.token);
    assert.equal(response.headers['cache-control'], 'no-store');
  });

  await check('跨站表单式 POST（Origin: http://evil.test）被拒 403，代理不替它洗白', async () => {
    const throughProxy = await request(`${rendererUrl}/api/agent/tool`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', origin: 'http://evil.test' },
      body: JSON.stringify({ tool: 'vocal.mode_list', input: {} }),
    });
    assert.equal(throughProxy.status, 403, throughProxy.text.slice(0, 300));
    assert.equal(throughProxy.json.error.code, 'origin_forbidden');
    assert.notEqual(throughProxy.headers['access-control-allow-origin'], '*');
    const direct = await request(`${base}/api/agent/tool`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', origin: 'http://evil.test' },
      body: JSON.stringify({ tool: 'vocal.mode_list', input: {} }),
    });
    assert.equal(direct.status, 403);
    assert.ok(direct.json.detail.includes('第一方'), '错误体是中文、可照着行动的');
  });

  await check('不带令牌的非 GET 被拒 401（哪怕 Origin 是第一方）', async () => {
    const response = await request(`${base}/api/agent/tool`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', origin: 'http://localhost:3000' },
      body: JSON.stringify({ tool: 'vocal.mode_list', input: {} }),
    });
    assert.equal(response.status, 401, response.text.slice(0, 300));
    assert.equal(response.json.error.code, 'token_required');
  });

  await check('令牌文件确实是壳的发现方式（换一份配置能读回来）', async () => {
    const second = resolveToken({ env: {}, dataDir });
    assert.equal(second.token, guard.token);
    assert.equal(second.source, 'file:security.json');
    // 附加到"别人起的"后端时壳自己没有数据目录：只能按 health 报的 token_file 找回令牌。
    assert.ok(fs.existsSync(tokenFilePath(dataDir)), '壳生成的令牌文件真的落在数据目录里');
    const health = await request(`${base}/api/health`);
    const resolved = tokenForRunningBackend(health.json, { env: {}, dataDir: '' });
    assert.equal(resolved.token, guard.token, 'health 报了 token_file，壳就按那个路径把令牌读回来');
    assert.equal(resolved.source, 'file:security.json');
    assert.equal(resolved.problem, '');
    // 真的读不到时要把问题说出来，不能留下一个"POST 全 401"的半能跑界面。
    const unreachable = tokenForRunningBackend(
      { data: { guard: { token_required: true, token_source: 'test', token_file: path.join(dataDir, '不存在.json') } } },
      { env: {}, dataDir: '' },
    );
    assert.equal(unreachable.token, '');
    assert.ok(unreachable.problem.includes('令牌'), unreachable.problem);
  });

  await check('打包清单把闸门模块带上（extraResources 漏一项就是桌面端起不来）', async () => {
    const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, 'package.json'), 'utf8'));
    const backendFilter = pkg.build.extraResources.find((entry) => entry.from === 'backend');
    assert.ok(backendFilter?.filter?.includes('local_guard.py'), 'extraResources 必须含 local_guard.py');
    assert.ok(backendFilter?.filter?.includes('agent_api.py'));
    assert.ok(pkg.build.files.includes('electron/**/*'), 'electron/api-proxy.cjs 靠这条进包');
  });

  await check('回环来源的读操作与 GET 不受令牌约束（界面轮询照常）', async () => {
    const health = await request(`${rendererUrl}/api/health`, { headers: { origin: rendererUrl } });
    assert.equal(health.status, 200);
    const history = await request(`${rendererUrl}/api/history?limit=5`, { headers: { origin: rendererUrl } });
    assert.equal(history.status, 200);
    assert.ok(Array.isArray(history.json.items));
  });
} finally {
  renderer.close();
  renderer.closeAllConnections?.();
  if (spawned && backend.exitCode === null) {
    backend.kill('SIGTERM');
    setTimeout(() => { if (backend.exitCode === null) backend.kill('SIGKILL'); }, 3000).unref?.();
  }
  fs.rmSync(dataDir, { recursive: true, force: true });
}

const failures = results.filter((line) => line.startsWith('FAIL'));
process.stdout.write(`${results.join('\n')}\n`);
process.stdout.write(`\n${results.length - failures.length}/${results.length} 条桌面壳闸门断言通过\n`);
if (failures.length) process.exit(1);
