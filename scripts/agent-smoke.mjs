import { spawn } from 'node:child_process';
import assert from 'node:assert';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { readFileSync, existsSync } from 'node:fs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const TEST_PORT = 8994;
const BASE_URL = `http://127.0.0.1:${TEST_PORT}`;

console.log('--- Testing Vocal-Separator Agent API & MCP ---');

const server = spawn('python', ['backend/main.py'], {
  cwd: ROOT,
  env: { ...process.env, PORT: String(TEST_PORT), VOCAL_SEPARATOR_PORT: String(TEST_PORT) },
  stdio: 'pipe'
});

server.stderr.on('data', (d) => process.stderr.write(d));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitForHealth() {
  for (let i = 0; i < 40; i++) {
    try {
      const res = await fetch(`${BASE_URL}/api/health`, { signal: AbortSignal.timeout(1000) });
      if (res.ok) {
        return await res.json();
      }
    } catch {
      await sleep(250);
    }
  }
  throw new Error('Server did not become healthy in time');
}

try {
  const health = await waitForHealth();
  console.log('✓ GET /api/health returned healthy');
  assert.strictEqual(health.ok, true);
  assert.strictEqual(health.data.project, 'vocal-separator');
  assert.strictEqual(health.data.agent_api, 1);

  let token = '';
  if (health.data.guard?.token_file && existsSync(health.data.guard.token_file)) {
    try {
      const parsed = JSON.parse(readFileSync(health.data.guard.token_file, 'utf8'));
      token = parsed.token || '';
    } catch {
      // ignore
    }
  }

  const postHeaders = {
    'content-type': 'application/json',
    ...(token ? { 'x-vocal-token': token } : {})
  };

  // Tools
  const toolsRes = await fetch(`${BASE_URL}/api/agent/tools`);
  const toolsData = await toolsRes.json();
  console.log(`✓ GET /api/agent/tools returned ${toolsData.data.length} tools`);
  assert.strictEqual(toolsData.ok, true);
  const toolNames = toolsData.data.map((t) => t.name);
  assert(toolNames.includes('vocal.env_probe'));
  assert(toolNames.includes('vocal.mode_list'));
  assert(toolNames.includes('vocal.separate_submit'));
  assert(toolNames.includes('vocal.job_status'));

  // Test tool call: vocal.env_probe
  const callProbe = await fetch(`${BASE_URL}/api/agent/tool`, {
    method: 'POST',
    headers: postHeaders,
    body: JSON.stringify({ tool: 'vocal.env_probe', input: {} })
  });
  const probeRes = await callProbe.json();
  assert.strictEqual(probeRes.ok, true);
  console.log(`✓ vocal.env_probe ok: python=${probeRes.data.python_version}, demucs=${probeRes.data.demucs_available}, cuda=${probeRes.data.cuda_available}`);

  // Test tool call: vocal.mode_list
  const callMode = await fetch(`${BASE_URL}/api/agent/tool`, {
    method: 'POST',
    headers: postHeaders,
    body: JSON.stringify({ tool: 'vocal.mode_list', input: {} })
  });
  const modeRes = await callMode.json();
  assert.strictEqual(modeRes.ok, true);
  console.log(`✓ vocal.mode_list ok: ${modeRes.data.presets?.length} presets available`);

  // Test confirm requirement for separate_submit
  const callSubmit = await fetch(`${BASE_URL}/api/agent/tool`, {
    method: 'POST',
    headers: postHeaders,
    body: JSON.stringify({ tool: 'vocal.separate_submit', input: { input_path: 'dummy.mp3', confirm: false } })
  });
  const submitRes = await callSubmit.json();
  assert.strictEqual(submitRes.ok, false);
  assert.strictEqual(submitRes.error.code, 'bad_input');
  assert(submitRes.error.message.includes('confirm:true'));
  console.log('✓ vocal.separate_submit without confirm correctly rejected requiring confirm:true');

  // Test MCP JSON-RPC
  const mcp = spawn('node', [path.join(ROOT, 'agent', 'mcp-server.mjs')], {
    cwd: ROOT,
    env: { ...process.env, AGENT_BASE_URL: BASE_URL },
    stdio: ['pipe', 'pipe', 'inherit']
  });

  const mcpPromise = new Promise((resolve, reject) => {
    let out = '';
    mcp.stdout.on('data', (d) => {
      out += d.toString();
      if (out.includes('"jsonrpc":"2.0"') && out.includes('"tools"')) {
        resolve(out);
      }
    });
    mcp.on('error', reject);
  });

  mcp.stdin.write(JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} }) + '\n');
  mcp.stdin.write(JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} }) + '\n');

  const mcpOut = await mcpPromise;
  console.log('✓ MCP stdio bridge responded with initialize and tools/list');
  mcp.kill();

  console.log('=== ALL VOCAL-SEPARATOR AGENT API & MCP CHECKS PASSED ===');
} finally {
  server.kill('SIGTERM');
}
