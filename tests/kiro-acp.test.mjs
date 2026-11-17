import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { existsSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { createInterface } from 'node:readline';
import { PassThrough } from 'node:stream';
import test from 'node:test';
import { openKiroSession } from '../src/sfdc_agent/kiro_acp.mjs';

const figmaAuth = 'https://www.figma.com/oauth/mcp?state=test';
const staleFigmaAuth = 'https://www.figma.com/oauth/mcp?state=stale';
const sessionId = 'test-session';
const options = [
  { optionId: 'allow', kind: 'allow_once' },
  { optionId: 'deny', kind: 'reject_once' },
];

function harness(handle) {
  const child = new EventEmitter();
  child.stdin = new PassThrough();
  child.stdout = new PassThrough();
  child.kill = () => child.stdout.end();
  const messages = [];
  const send = (message) => child.stdout.write(`${JSON.stringify({ jsonrpc: '2.0', ...message })}\n`);
  createInterface({ input: child.stdin }).on('line', (line) => {
    const message = JSON.parse(line);
    messages.push(message);
    if (message.method === 'initialize') {
      send({ id: message.id, result: { agentCapabilities: { mcpCapabilities: { http: true } } } });
    } else if (message.method === 'session/new') {
      send({ id: message.id, result: { sessionId, modes: { currentModeId: 'nova-figma' } } });
    } else {
      handle?.(message, send);
    }
  });
  let cwd;
  return {
    messages, send,
    get cwd() { return cwd; },
    spawnProcess(_binary, _args, { cwd: path }) {
      cwd = path;
      return child;
    },
  };
}

function mcpStatus(server) {
  return { method: '_kiro/mcp/status', params: { sessionId, servers: [{ name: 'figma', ...server }] } };
}

test('waits for Figma authorization and captures actual tool results in the isolated Kiro session', async (t) => {
  const input = { fileKey: 'source', nodeId: '12:34' };
  const output = { content: [{ type: 'image', mimeType: 'image/png', data: 'PNG' }] };
  const identity = { version: 1, identity: { serverName: 'figma', toolName: 'get_screenshot' } };
  let promptId;
  const h = harness((message, send) => {
    if (message.method === '_kiro/mcp/resetServer') {
      assert.deepEqual(message.params, { sessionId, serverName: 'figma', startOAuth: true });
      send(mcpStatus({ status: 'connecting' }));
      send({ id: 20, method: '_kiro/openExternalUrl', params: {
        serverName: 'figma', url: figmaAuth, oauthCompletion: 'connection-status',
      } });
      send({ id: message.id, result: {} });
    } else if (message.method === 'session/prompt') {
      promptId = message.id;
      send({ method: 'session/update', params: { sessionId, update: {
        sessionUpdate: 'tool_call', toolCallId: 'shot', status: 'pending', rawInput: input,
      } } });
      send({ id: 1, method: 'session/request_permission', params: {
        sessionId, options, toolCall: { toolCallId: 'shot', status: 'pending' },
        _meta: { kiro: { mcpTool: identity } },
      } });
    } else if (message.id === 1 && message.result?.outcome?.optionId === 'allow') {
      send({ id: 2, method: 'session/request_permission', params: {
        sessionId, options, toolCall: { toolCallId: 'shell' },
      } });
    } else if (message.id === 2 && message.result?.outcome?.optionId === 'deny') {
      send({ id: 3, method: 'session/request_permission', params: {
        sessionId, options, toolCall: { toolCallId: 'another-file', rawInput: { ...input, fileKey: 'outside' } },
        _meta: { kiro: { mcpTool: identity } },
      } });
    } else if (message.id === 3 && message.result?.outcome?.optionId === 'deny') {
      send({ method: 'session/update', params: { sessionId, update: {
        sessionUpdate: 'tool_call_update', toolCallId: 'shot', status: 'completed', rawOutput: output,
      } } });
      send({ method: 'session/update', params: { sessionId, update: {
        sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'draft' },
      } } });
      send({ id: promptId, result: { stopReason: 'end_turn' } });
    }
  });
  const events = [];
  const kiro = await openKiroSession({
    reference: { fileKey: 'source' }, spawnProcess: h.spawnProcess, onEvent: (event) => events.push(event),
  });
  t.after(() => kiro.close());
  const config = JSON.parse(await readFile(join(h.cwd, '.kiro', 'agents', 'nova-figma.json'), 'utf8'));
  assert.equal(config.includeMcpJson, false);
  assert.ok(config.tools.includes('@figma/get_screenshot'));
  assert.ok(config.tools.every((tool) => tool.startsWith('@figma/') && tool !== '@figma/use_figma'));
  h.send(mcpStatus({ status: 'failed', failedAuthorization: true, authorizationUrl: staleFigmaAuth }));
  await new Promise(setImmediate);
  assert.ok(events.some((event) => event.type === 'figma-auth-required'));
  assert.ok(!events.some((event) => event.url));
  assert.equal(await kiro.authorizeFigma(), figmaAuth);
  assert.ok(events.some((event) => event.type === 'figma-auth'));
  const pending = kiro.prompt('Make a draft');
  await new Promise(setImmediate);
  assert.ok(!h.messages.some((message) => message.method === 'session/prompt'));
  h.send(mcpStatus({ status: 'connected' }));
  const result = await pending;
  assert.equal(result.text, 'draft');
  assert.equal(result.stopReason, 'end_turn');
  const [{ completedAt, ...toolCall }] = result.toolCalls;
  assert.ok(Number.isFinite(Date.parse(completedAt)));
  assert.deepEqual(toolCall, {
    id: 'shot', serverName: 'figma', name: 'get_screenshot', status: 'completed', input, output, content: undefined
  });
  await kiro.close();
  assert.equal(existsSync(h.cwd), false);
});

test('aborts a pending Figma connection and removes its temporary workspace', async () => {
  const h = harness();
  const controller = new AbortController();
  const kiro = await openKiroSession({ spawnProcess: h.spawnProcess, signal: controller.signal });
  const waiting = assert.rejects(kiro.waitForFigma(), { code: 'KIRO_ABORTED' });
  controller.abort();
  await waiting;
  await kiro.close();
  assert.equal(existsSync(h.cwd), false);
});

test('PNG sessions open with only the two Figma export tools in a disposable workspace', async (t) => {
  const h = harness();
  const session = await openKiroSession({
    reference: { fileKey: 'FileA' }, readFigma: true, figmaPng: true, spawnProcess: h.spawnProcess,
  });
  t.after(() => session.close());
  const opened = h.messages.find(({ method }) => method === 'session/new');
  assert.equal(opened.params.mcpServers[0].url, 'https://mcp.figma.com/mcp');
  const config = JSON.parse(await readFile(join(h.cwd, '.kiro', 'agents', 'nova-figma.json'), 'utf8'));
  assert.deepEqual(config.tools, ['@figma/download_assets', '@figma/get_screenshot']);
  assert.equal(config.includeMcpJson, false);
  assert.equal(config.includePowers, false);
  await session.close();
  assert.equal(existsSync(h.cwd), false);
});
