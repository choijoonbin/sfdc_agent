import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const [engineRoot, releaseRoot] = process.argv.slice(2);
const { fields } = JSON.parse(readFileSync(0, 'utf8'));
const context = fields.sourceContext;
if (!context?.nodes?.length || context.nodes[0].id !== fields.nodeId) {
  process.stdout.write('{}');
} else {
  const fromEngine = (path) => import(pathToFileURL(resolve(engineRoot, 'nextbss', 'engine', path)).href);
  const [{ readNextBssStandard }, { rebuildKiroSourceSnapshot }, { extractKiroButtonEvidence }, { projectKiroFormEvidence }] =
    await Promise.all(['generate.mjs', 'kiro-source-snapshot.mjs', 'kiro-button-evidence.mjs', 'kiro-form-projection.mjs'].map(fromEngine));
  const design = await readNextBssStandard({
    releaseId: fields.reviewedAnalysis?.standard?.releaseId,
    releaseRoot: releaseRoot || undefined,
  });
  const snapshot = rebuildKiroSourceSnapshot(context);
  if (design.standard.version !== fields.version || !snapshot) {
    process.stdout.write('{}');
  } else {
    const buttons = extractKiroButtonEvidence(snapshot, design);
    const forms = projectKiroFormEvidence(snapshot, design, context.inputControls);
    const components = new Map((context.verifiedComponents ?? []).map((component) => [component.nodeId, component]));
    for (const button of buttons) components.set(button.nodeId, button);
    process.stdout.write(JSON.stringify({ verifiedComponents: [...components.values()], ...forms }));
  }
}
