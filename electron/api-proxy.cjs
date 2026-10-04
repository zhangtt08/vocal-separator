/* eslint-disable @typescript-eslint/no-require-imports */
'use strict';

// 桌面壳 -> 本机 AI 服务的转发层。
//
// 为什么单独成一个文件而不是留在 main.cjs 里：main.cjs 一 import 就要 electron，
// 测不到；而「合法第一方路径必须被证明，不是被假设」正是闸门验收的一条。
// 这里的判据与 backend/local_guard.py 一一对应：
//   1. 转出去必须带 `Host: 127.0.0.1:<端口>`（回环钉死）；
//   2. 非 GET 必须带 `x-vocal-token`（服务启用了共享令牌时）。
// 令牌发现顺序（也是 README 里写的那套）：
//   环境变量 VOCAL_SEPARATOR_TOKEN -> <数据目录>/security.json 的 token 字段
//   -> 都没有就由壳现生成一个、写进 security.json、并用环境变量传给它的后端子进程。

const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');

const API_HOST = '127.0.0.1';
const API_PORT = Number(process.env.VOCAL_SEPARATOR_PORT) || 8000;
const TOKEN_HEADER = 'x-vocal-token';
const TOKEN_FILE_NAME = 'security.json';
const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

function tokenFilePath(dataDir) {
  return path.join(String(dataDir || ''), TOKEN_FILE_NAME);
}

function readTokenFile(dataDir) {
  try {
    const payload = JSON.parse(fs.readFileSync(tokenFilePath(dataDir), 'utf-8'));
    const token = typeof payload?.token === 'string' ? payload.token.trim() : '';
    return token || '';
  } catch {
    return '';
  }
}

function writeTokenFile(dataDir, token) {
  try {
    fs.mkdirSync(dataDir, { recursive: true });
    const target = tokenFilePath(dataDir);
    const temp = `${target}.tmp`;
    fs.writeFileSync(
      temp,
      `${JSON.stringify(
        { token, created_at: new Date().toISOString(), header: TOKEN_HEADER, note: '声析桌面壳与本机后端之间的共享令牌；非 GET 请求必须带这个头。' },
        null,
        2,
      )}\n`,
      'utf-8',
    );
    fs.renameSync(temp, target);
    try {
      fs.chmodSync(target, 0o600);
    } catch { /* Windows 上没有 POSIX 权限位 */ }
    return true;
  } catch {
    return false; // 写不进去不影响本次运行：令牌照样通过环境变量交给子进程
  }
}

/**
 * 取本壳要用的令牌。`dataDir` 为空表示还没决定数据目录（附加到别人起的后端）。
 * @returns {{token: string, source: string}}
 */
function resolveToken({ env = process.env, dataDir = '', create = false } = {}) {
  const fromEnv = String(env.VOCAL_SEPARATOR_TOKEN || '').trim();
  if (fromEnv) return { token: fromEnv, source: 'env:VOCAL_SEPARATOR_TOKEN' };
  if (dataDir) {
    const fromFile = readTokenFile(dataDir);
    if (fromFile) return { token: fromFile, source: `file:${TOKEN_FILE_NAME}` };
    if (create) {
      const generated = crypto.randomBytes(16).toString('hex');
      writeTokenFile(dataDir, generated);
      return { token: generated, source: 'generated' };
    }
  }
  return { token: '', source: '' };
}

/**
 * 附加到一个已经在跑的后端时，按它自己报的 `guard.token_file` 找回令牌。
 * 拿不到就明说拿不到 —— 半能跑的界面比一句准确的启动失败更难查。
 * @param {object|null|undefined} healthPayload /api/health 的响应体
 * @returns {{token: string, source: string, required: boolean, problem: string}}
 */
function tokenForRunningBackend(healthPayload, { env = process.env, dataDir = '' } = {}) {
  const guard = (healthPayload?.data || {}).guard || {};
  const required = guard.token_required === true;
  const fallback = resolveToken({ env, dataDir });
  if (!required) return { token: fallback.token, source: fallback.source, required: false, problem: '' };
  if (fallback.token) return { token: fallback.token, source: fallback.source, required: true, problem: '' };
  const file = String(guard.token_file || '');
  if (file) {
    try {
      const payload = JSON.parse(fs.readFileSync(file, 'utf-8'));
      const token = typeof payload?.token === 'string' ? payload.token.trim() : '';
      if (token) return { token, source: `file:${path.basename(file)}`, required: true, problem: '' };
    } catch { /* 读不到就走下面的 problem */ }
  }
  return {
    token: '',
    source: '',
    required: true,
    problem: `本机 AI 服务启用了共享令牌（${guard.token_source || '未知来源'}），桌面壳没能从 ${file || '令牌文件'} 读到它。`
      + '请让壳自己启动后端（它会生成令牌），或把同一份令牌写进该文件。',
  };
}

/**
 * 把渲染进程的 /api/* 转发给后端：改 Host、补令牌，其余原样。
 * @param {import('node:http').IncomingMessage} request
 * @param {import('node:http').ServerResponse} response
 * @param {{host?:string, port?:number, token?:string}} [options]
 */
function proxyApiRequest(request, response, options = {}) {
  const host = options.host || API_HOST;
  const port = options.port || API_PORT;
  const token = options.token === undefined ? '' : String(options.token);
  const headers = { ...request.headers, host: `${host}:${port}` };
  delete headers.connection;
  if (token && !SAFE_METHODS.has(String(request.method || '').toUpperCase())) {
    headers[TOKEN_HEADER] = token; // 壳知道的令牌优先：渲染进程那份可能是过期的
  }

  return new Promise((resolve) => {
    const upstream = http.request(
      { host, port, path: request.url, method: request.method, headers },
      (upstreamResponse) => {
        response.writeHead(upstreamResponse.statusCode || 502, upstreamResponse.headers);
        upstreamResponse.pipe(response);
        upstreamResponse.on('end', () => resolve(upstreamResponse.statusCode || 502));
      },
    );
    upstream.on('error', () => {
      if (!response.headersSent) {
        response.writeHead(502, { 'Content-Type': 'application/json; charset=utf-8' });
      }
      response.end(JSON.stringify({ ok: false, detail: '本地 AI 服务暂时不可用' }));
      resolve(502);
    });
    request.pipe(upstream);
  });
}

module.exports = {
  API_HOST,
  API_PORT,
  TOKEN_HEADER,
  TOKEN_FILE_NAME,
  proxyApiRequest,
  readTokenFile,
  resolveToken,
  tokenFilePath,
  tokenForRunningBackend,
  writeTokenFile,
};
