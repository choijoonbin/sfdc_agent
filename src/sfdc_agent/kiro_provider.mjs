import { readFileSync } from 'node:fs';
import { openKiroSession } from './kiro_acp.mjs';

const controller = new AbortController();
process.once('SIGTERM', () => controller.abort());
process.once('SIGINT', () => controller.abort());

try {
  const { prompt, timeoutMs } = JSON.parse(readFileSync(0, 'utf8'));
  const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(timeoutMs)]);
  const session = await openKiroSession({ readFigma: false, signal });
  try {
    const { text, stopReason, toolCalls } = await session.prompt(prompt);
    if (stopReason !== 'end_turn' || toolCalls.length) {
      throw Object.assign(new Error('Kiro did not complete a tool-free response.'), {
        code: 'KIRO_INCOMPLETE',
      });
    }
    process.stdout.write(JSON.stringify({ text }));
  } finally {
    await session.close();
  }
} catch (error) {
  const code = typeof error.code === 'string' && /^KIRO_[A-Z_]+$/.test(error.code)
    ? error.code
    : error.name === 'TimeoutError' ? 'KIRO_TIMEOUT' : 'KIRO_BRIDGE_FAILED';
  process.stdout.write(JSON.stringify({ error: code }));
  process.exitCode = 1;
}
