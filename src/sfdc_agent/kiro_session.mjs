import { createInterface } from 'node:readline';
import { openKiroSession } from './kiro_acp.mjs';

const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
const controller = new AbortController();
let session;
let opening;

function send(message) {
  process.stdout.write(`${JSON.stringify(message)}\n`);
}

async function close() {
  controller.abort();
  lines.close();
  process.stdin.destroy();
  await (await opening?.catch(() => {}))?.close();
}

process.once('SIGTERM', () => void close());
process.once('SIGINT', () => void close());
lines.once('close', () => void close());

async function handle({ id, method, params }) {
  try {
    let result;
    if (method === 'open') {
      opening = openKiroSession({ ...params, signal: controller.signal, onEvent: (event) => send({ event }) });
      session = await opening;
    } else if (method === 'wait') {
      await session.waitForFigma();
    } else if (method === 'authorize') {
      result = await session.authorizeFigma();
    } else if (method === 'prompt') {
      const { text, png } = params;
      result = await session.prompt(text, { png: png ? Buffer.from(png, 'base64') : undefined });
    } else {
      throw new Error('Unknown session method.');
    }
    send({ id, result: result ?? null });
  } catch (error) {
    const known = /^KIRO_[A-Z_]+$/.test(error.code ?? '');
    send({ id, error: { code: known ? error.code : 'KIRO_BRIDGE_FAILED',
      message: known ? error.message : 'Kiro 세션 요청을 처리하지 못했습니다.' } });
  }
}

lines.on('line', (line) => void handle(JSON.parse(line)));
