import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const [engineRoot, releaseRoot] = process.argv.slice(2);
const { fields } = JSON.parse(readFileSync(0, 'utf8'));
const context = fields.sourceContext;
const releaseId = fields.designReleaseId ?? fields.reviewedAnalysis?.standard?.releaseId;
if (releaseRoot && fields.componentLibrary && !/^nextbss-\d{1,4}(?:\.\d{1,4}){1,2}-[a-f0-9]{64}$/.test(releaseId ?? '')) {
  throw new Error('Pinned component-library projection requires its immutable design release.');
}
if (!context?.nodes?.length || context.nodes[0].id !== fields.nodeId) {
  process.stdout.write('{}');
} else {
  const fromEngine = (path) => import(pathToFileURL(resolve(engineRoot, 'nextbss', 'engine', path)).href);
  const { projectKiroNovaContext } = await import(pathToFileURL(resolve(engineRoot, 'nextbss/server/kiro-nova-contract.mjs')).href);
  const [{ readNextBssStandard }, { rebuildKiroSourceSnapshot }, { extractKiroButtonEvidence }, { projectKiroFormEvidence }] =
    await Promise.all(['generate.mjs', 'kiro-source-snapshot.mjs', 'kiro-button-evidence.mjs', 'kiro-form-projection.mjs'].map(fromEngine));
  const design = await readNextBssStandard({
    releaseId,
    releaseRoot: releaseRoot || undefined,
    componentLibrary: fields.componentLibrary,
  });
  const snapshot = rebuildKiroSourceSnapshot(context);
  if (design.standard.version !== fields.version || !snapshot) {
    process.stdout.write('{}');
  } else {
    const buttons = extractKiroButtonEvidence(snapshot, design);
    const forms = projectKiroFormEvidence(snapshot, design, context.inputControls);
    const components = new Map((context.verifiedComponents ?? []).map((component) => [component.nodeId, component]));
    for (const button of buttons) components.set(button.nodeId, button);
    // Include the saved derived guidance as well as current mappings so the
    // pinned projector can retire obsolete API instructions from checkpoints.
    const projection = { ...context, verifiedComponents: [...components.values()], ...forms };
    process.stdout.write(JSON.stringify(design.componentLibrary.tuple.family === 'NOVA_PUBLIC'
      ? projectKiroNovaContext(projection, design.componentLibrary.contract) : projection));
  }
}
