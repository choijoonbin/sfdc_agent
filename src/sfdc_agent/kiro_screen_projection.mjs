import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const [engineRoot, releaseRoot] = process.argv.slice(2);
const { fields } = JSON.parse(readFileSync(0, 'utf8'));
const context = fields.sourceContext;
const { prepareScreenPromptContext } = await import(pathToFileURL(resolve(engineRoot,
  'nextbss/shared/screen-prompt-context.mjs')).href);
let canonical = context;
if (context?.nodes?.length && context.nodes[0].id !== fields.nodeId)
  throw new Error('The measured source root does not match its selected node.');
const releaseId = fields.designReleaseId ?? fields.reviewedAnalysis?.standard?.releaseId;
if (releaseRoot && fields.componentLibrary && !/^nextbss-\d{1,4}(?:\.\d{1,4}){1,2}-[a-f0-9]{64}$/.test(releaseId ?? '')) {
  throw new Error('Pinned component-library projection requires its immutable design release.');
}
if (context?.nodes?.length && context.nodes[0].id === fields.nodeId
  && (Object.keys(context.components ?? {}).length || context.inputControls?.length)) {
  const fromEngine = (path) => import(pathToFileURL(resolve(engineRoot, 'nextbss', 'engine', path)).href);
  const { projectKiroPromptSourceContext } = await import(pathToFileURL(resolve(engineRoot,
    'nextbss/server/kiro-prompt-projection.mjs')).href);
  const { readNextBssStandard } = await fromEngine('generate.mjs');
  const design = await readNextBssStandard({
    releaseId,
    releaseRoot: releaseRoot || undefined,
    componentLibrary: fields.componentLibrary,
  });
  if (design.standard.version !== fields.version)
    throw new Error('The measured source version does not match its pinned design release.');
  canonical = projectKiroPromptSourceContext(context, design);
}
// The source stays canonical for image identity, findings, reconstruction and
// traces. Only the model payload is encoded, using the engine's one serializer.
const { json, receipt } = prepareScreenPromptContext(canonical);
process.stdout.write(JSON.stringify({ sourceJson: JSON.stringify(canonical), prepared: { json, receipt } }));
