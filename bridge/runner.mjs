import { createHash } from 'node:crypto';
import { readFile, realpath } from 'node:fs/promises';
import { dirname, isAbsolute, join, relative, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const read = async (path) => JSON.parse(await readFile(path, 'utf8'));
const imported = (root, path) => import(pathToFileURL(join(root, path)).href);
const sha256 = (bytes) => createHash('sha256').update(bytes).digest('hex');

async function pin(context) {
  const { uiRoot, sourcePath } = context;
  const { readActiveRelease, verifyReleaseSources } = await imported(uiRoot, 'nextbss/engine/release-archive.mjs');
  const release = await readActiveRelease();
  const { releaseId, designVersion } = release.manifest;
  if (designVersion !== '1.9') throw new Error('The pilot requires an active NextBSS v1.9 release.');
  await verifyReleaseSources(releaseId);
  const sourceSha256 = sha256(await readFile(sourcePath));
  const fixturePath = join(uiRoot, 'nextbss/server/story-benchmark-cases.json');
  const fixtureBytes = await readFile(fixturePath);
  const fixture = JSON.parse(fixtureBytes);
  if (fixture.source.sha256 !== sourceSha256) throw new Error('Source does not match the fixed number-portability fixture.');
  return {
    releaseId, designVersion, sourceSha256,
    sourceFileKey: fixture.source.fileKey, boardId: fixture.source.boardId,
    fixtureDigest: sha256(fixtureBytes),
  };
}

async function main() {
  const [requestPath] = process.argv.slice(2);
  const { target, context } = await read(requestPath);
  const uiRoot = await realpath(context.uiRoot);
  const outputDir = context.outputDir && await realpath(context.outputDir);
  if (outputDir) {
    const runRoot = await realpath(fileURLToPath(new URL('../.pilot/runs/', import.meta.url)));
    const output = relative(runRoot, outputDir);
    if (!output || isAbsolute(output) || output === '..' || output.startsWith('..' + sep)) {
      throw new Error('Pilot output must be inside the local run directory.');
    }
  }
  const referenceDirectory = context.referenceDirectory && await realpath(context.referenceDirectory);
  if (referenceDirectory && (!outputDir || context.phase !== 'agent'
    || referenceDirectory !== join(dirname(dirname(outputDir)), 'baseline'))) {
    throw new Error('Reference images must come from the baseline of the same run.');
  }
  const input = { ...context, uiRoot, outputDir, referenceDirectory };
  if (target === 'pin') return pin(input);
  if (!/^nextbss-1\.9-[a-f0-9]{64}$/.test(input.releaseId ?? '')
    || !/^[a-f0-9]{64}$/.test(input.sourceSha256 ?? '')) {
    throw new Error('Expected a pinned NextBSS release and source digest.');
  }
  if (sha256(await readFile(input.sourcePath)) !== input.sourceSha256) {
    throw new Error('Figma source digest changed during the pilot.');
  }
  if (input.fixtureDigest && sha256(await readFile(join(uiRoot, 'nextbss/server/story-benchmark-cases.json'))) !== input.fixtureDigest) {
    throw new Error('Story fixture digest changed during the pilot.');
  }
  if (target === 'screen') {
    const { readScreenEvidence, runScreen } = await import('./screen.mjs');
    return input.phase === 'evidence' ? readScreenEvidence(input) : runScreen(input);
  }
  if (target === 'story') {
    const { readStoryEvidence, runStory } = await import('./story.mjs');
    return input.phase === 'evidence' ? readStoryEvidence(input) : runStory(input);
  }
  throw new Error('Unknown conversion target.');
}

try {
  process.stdout.write(JSON.stringify(await main()) + '\n');
} catch (error) {
  process.stderr.write(String(error.message).split('\n')[0].slice(0, 500) + '\n');
  process.exitCode = 1;
}
