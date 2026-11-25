import { createHash, randomUUID } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { basename, dirname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const digest = (bytes) => createHash('sha256').update(bytes).digest('hex');
const errorText = (error) => String(error?.message ?? error).split('\n')[0].slice(0, 350);
const modulePaths = [
  'engine/generate.mjs',
  'engine/story-local-source.mjs',
  'engine/local-fig-archive.mjs',
  'engine/story-plan.mjs',
  'engine/story-design-evidence.mjs',
  'engine/story-spec-prompts.mjs',
  'server/story-lwc.mjs',
  'server/workbench.mjs',
  'server/story-benchmark-browser.mjs',
  'server/story-benchmark-fixtures.mjs',
];

async function withStorySource({ uiRoot, sourcePath, sourceSha256, releaseId, releaseRoot }, use) {
  const imports = await Promise.all(modulePaths.map((name) =>
    import(pathToFileURL(join(uiRoot, 'nextbss', name)).href)));
  const nova = Object.assign({}, ...imports);
  const fixtureBytes = await readFile(join(uiRoot, 'nextbss/server/story-benchmark-cases.json'));
  const fixtures = JSON.parse(fixtureBytes);
  const fixtureDigest = digest(fixtureBytes);
  if (fixtures.source.sha256 !== sourceSha256) {
    throw new Error('The Story fixture belongs to a different Figma archive.');
  }
  const design = await nova.readNextBssStandard({ releaseId, releaseRoot });
  if (design.standard.version !== '1.9' || design.standard.releaseId !== releaseId) {
    throw new Error('The requested NextBSS v1.9 release could not be pinned.');
  }
  const analysis = await nova.analyzeLocalStoryboard({
    path: sourcePath,
    nodeId: fixtures.source.boardId,
    fileKey: fixtures.source.fileKey,
    sha256: sourceSha256,
  });
  const source = { ...analysis.source, local: true };
  const workflows = fixtures.workflows.map((fixture) => {
    const matches = analysis.workflows.filter((item) => item.startScreenId === fixture.entryNodeId);
    if (matches.length !== 1 || matches[0].title !== fixture.title) {
      throw new Error(`The pinned Story source is missing ${fixture.title} (${fixture.entryNodeId}).`);
    }
    return { workflow: matches[0], fixture };
  });
  const archive = await nova.openLocalFigArchive(sourcePath, { name: basename(sourcePath) });
  try {
    if (archive.source.version !== `local-${sourceSha256}`) throw new Error('The Story archive changed.');
    return await use({ nova, design, source, workflows, fixtures, fixtureDigest, archive });
  } finally {
    await archive.close();
  }
}

function readEvidence({ nova, design, archive }, workflow, plan, { includePresentation }) {
  return nova.collectStoryDesignEvidence({
    screens: workflow.screens,
    modeledScreens: plan.screens,
    startScreenId: plan.startScreenId,
    design,
    includePresentation,
    readSnapshot: async (nodeId) => ({ ...await archive.snapshot(nodeId), sourceKind: 'local-fig' }),
  });
}

export async function readStoryEvidence(context) {
  return withStorySource(context, async (environment) => {
    const { nova, source, workflows } = environment;
    const proposals = [];
    for (const { workflow } of workflows) {
      const plan = nova.buildSourceStoryPlan(workflow);
      const visualEvidence = await readEvidence(environment, workflow, plan, { includePresentation: false });
      const task = nova.buildStoryboardSpecTask({ workflow, plan, source, visualEvidence });
      proposals.push({ id: workflow.id, title: workflow.title, task });
    }
    return { source, workflows: proposals };
  });
}

async function readReferenceManifest(path, source) {
  if (!path) return null;
  const input = JSON.parse(await readFile(path, 'utf8'));
  const pin = input.source;
  if (pin?.fileKey !== source.fileKey || pin.sha256 !== source.sha256 || !/^\d+$/.test(pin.version ?? '')) {
    throw new Error('The reference manifest does not bind a Figma revision to this archive.');
  }
  return { ...pin, directory: dirname(path) };
}

async function referenceImage(environment, app, manifest, nodeId) {
  const { archive, nova } = environment;
  const pin = manifest?.pngs?.[nodeId];
  const { svg, width, height } = await archive.preview(nodeId);
  const dimensions = { width: Math.ceil(width), height: Math.ceil(height) };
  if (dimensions.width < 1 || dimensions.height < 1 || dimensions.width * dimensions.height > 8_000_000) {
    throw new Error(`Story reference frame ${nodeId} exceeds the browser capture bounds.`);
  }
  if (pin) {
    if (pin.fileKey !== manifest.fileKey || pin.version !== manifest.version || pin.nodeId !== nodeId ||
        pin.scale !== 1 || typeof pin.path !== 'string') {
      throw new Error(`Invalid Figma 1× PNG entry for ${nodeId}.`);
    }
    const png = await readFile(resolve(manifest.directory, pin.path));
    const size = nova.pngDimensions(png);
    if (digest(png) !== pin.sha256 || size.width !== dimensions.width || size.height !== dimensions.height) {
      throw new Error(`Figma 1× PNG digest or dimensions differ for ${nodeId}.`);
    }
    return { png, mode: 'official', version: manifest.version, ...dimensions };
  }
  const png = await app.renderSvg(svg, dimensions.width, dimensions.height);
  return { png, mode: 'reference', kind: 'local-fig-svg', ...dimensions };
}

async function baselineReferences(directory, { source, design, fixtureDigest }) {
  if (!directory) throw new Error('The agent Story run requires a frozen baseline reference directory.');
  const baseline = JSON.parse(await readFile(join(directory, 'result.json'), 'utf8'));
  if (baseline.phase !== 'baseline' || baseline.source?.sha256 !== source.sha256 ||
      baseline.source.fileKey !== source.fileKey || baseline.source.boardId !== source.boardId ||
      baseline.standard?.releaseId !== design.standard.releaseId || baseline.fixtureDigest !== fixtureDigest) {
    throw new Error('Story baseline references do not match the pinned source, release, and fixture.');
  }
  return new Map(baseline.workflows.map((workflow) => [workflow.id, {
    directory: join(directory, workflow.id),
    samples: new Map([
      ['initial', workflow.initial.reference],
      ...workflow.cases.map(({ id, reference }) => [id, reference]),
    ]),
  }]));
}

async function frozenReference(environment, manifest, workflow, label, nodeId) {
  const metadata = workflow?.samples.get(label);
  const expectedName = `${label}-reference.png`;
  const pin = manifest?.pngs?.[nodeId];
  const provenanceMatches = pin
    ? metadata?.mode === 'official' && metadata.version === manifest.version && metadata.sha256 === pin.sha256
    : metadata?.mode === 'reference' && metadata.kind === 'local-fig-svg';
  if (metadata?.nodeId !== nodeId || metadata.image !== expectedName || !provenanceMatches) {
    throw new Error(`The frozen Story reference does not match node ${nodeId}.`);
  }
  const png = await readFile(join(workflow.directory, expectedName));
  const { width, height } = environment.nova.pngDimensions(png);
  if (digest(png) !== metadata.sha256 || width !== metadata.width || height !== metadata.height) {
    throw new Error(`The frozen Story reference has changed for node ${nodeId}.`);
  }
  return { png, mode: metadata.mode, kind: metadata.kind, version: metadata.version, width, height };
}

async function capture(app, environment, run, basePath, input, reference) {
  const { nova } = environment;
  let result;
  try {
    result = await app.run({ ...run, ...input });
  } catch (error) {
    return { status: 'error', detail: errorText(error) };
  }
  const { png, ...observation } = result;
  if (!png) return observation;
  const image = `${basePath}.png`;
  await writeFile(image, png, { mode: 0o600 });
  const { width, height } = nova.pngDimensions(png);
  const resultWithCapture = { ...observation, image: basename(image) };
  if (width !== reference.width || height !== reference.height) {
    return { ...resultWithCapture, comparison: 'dimension-mismatch', dimensions: { width, height } };
  }
  const { mask, ...statistics } = await app.diff(reference.png, png, { width, height });
  const diff = `${basePath}-diff.png`;
  await writeFile(diff, mask, { mode: 0o600 });
  return { ...resultWithCapture, pixel: { ...statistics, tolerance: 12, diff: basename(diff) } };
}

async function saveBundle(directory, bundles) {
  for (const [name, files] of bundles) {
    const path = join(directory, name);
    await mkdir(path, { recursive: true, mode: 0o700 });
    for (const [file, source] of files) await writeFile(join(path, file), source, { mode: 0o600 });
  }
}

async function generateStory(environment, workflow, base, proposal, phase) {
  const { nova, source, design } = environment;
  async function prepare(input) {
    let plan = nova.preserveSourceLookupResponses(workflow, nova.finalizeStoryPlan(workflow, input));
    if (plan.startScreenId !== base.startScreenId || !plan.scenarios.length) {
      throw new Error('The plan changed the start screen or has no executable scenario.');
    }
    const visualEvidence = await readEvidence(environment, workflow, plan, { includePresentation: true });
    if (visualEvidence.status !== 'unavailable') plan = { ...plan, visualEvidence };
    const bundle = nova.prepareStoryLwc({
      workflow: plan, source: { ...source, nodeId: plan.startScreenId }, standard: design.standard,
    });
    return { plan, bundle, visualEvidence: visualEvidence.status };
  }

  if (phase === 'baseline') return { ...await prepare(base), proposal: { status: 'not-requested' } };
  if (proposal && !Array.isArray(proposal) && typeof proposal === 'object') {
    try {
      const evidence = await readEvidence(environment, workflow, base, { includePresentation: false });
      const enriched = nova.applyKiroStoryPlan(workflow, base, proposal, evidence, source);
      const candidate = await prepare(enriched);
      const baseline = await prepare(base);
      const changed = JSON.stringify(candidate.bundle.runtimePlan) !== JSON.stringify(baseline.bundle.runtimePlan);
      return { ...candidate, proposal: { status: 'accepted', runtimeChanged: changed } };
    } catch (error) {
      return { ...await prepare(base), proposal: { status: 'rejected', detail: errorText(error) } };
    }
  }
  return { ...await prepare(base), proposal: { status: 'missing' } };
}

export async function runStory(context) {
  const { phase, outputDir, referenceManifest, referenceDirectory, modelPlan } = context;
  if (!['baseline', 'agent'].includes(phase)) throw new Error('Expected a Story benchmark phase.');
  return withStorySource(context, async (environment) => {
    const { nova, source, design, workflows, fixtures, fixtureDigest } = environment;
    const manifest = await readReferenceManifest(referenceManifest, source);
    const baseline = phase === 'agent' ? await baselineReferences(referenceDirectory, environment) : null;
    const app = await nova.startBenchmarkBrowser();
    const results = [];
    try {
      for (const { workflow, fixture } of workflows) {
        const directory = join(outputDir, fixture.id);
        await mkdir(directory, { recursive: true, mode: 0o700 });
        const base = nova.buildSourceStoryPlan(workflow);
        const { plan, bundle, proposal, visualEvidence } = await generateStory(environment, workflow, base,
          modelPlan?.[workflow.id], phase);
        const frozen = nova.freezeStoryboardSpec({ workflow, plan, source, fixtureDigest });
        const bundles = await nova.collectBundles(bundle.name, bundle.files, { release: design.release });
        await saveBundle(join(directory, 'lwc'), bundles);
        await writeFile(join(directory, 'spec.json'), JSON.stringify(frozen, null, 2) + '\n', { mode: 0o600 });
        const run = { id: randomUUID(), bundleName: bundle.name, bundles };
        async function reference(label, nodeId) {
          const ref = baseline ? await frozenReference(environment, manifest, baseline.get(fixture.id), label, nodeId)
            : await referenceImage(environment, app, manifest, nodeId);
          const name = `${label}-reference.png`;
          await writeFile(join(directory, name), ref.png, { mode: 0o600 });
          const { png, ...metadata } = ref;
          return { ...ref, metadata: { ...metadata, nodeId, image: name, sha256: digest(png) } };
        }
        const entry = await reference('initial', fixture.entryNodeId);
        const viewport = { width: entry.width, height: entry.height };
        const initial = await capture(app, environment, run, join(directory, 'initial'),
          { test: { fixture: {} }, viewport, initialOnly: true }, entry);
        const cases = [];
        for (const scenario of fixture.cases) {
          const ref = await reference(scenario.id, scenario.referenceNodeId);
          const test = nova.buildFixture({ model: bundle.runtimePlan, workflow: fixture, scenario, data: fixtures.fixtures });
          const bounds = scenario.expected.popup ? viewport : { width: ref.width, height: ref.height };
          const observation = test.unsupported
            ? { status: 'unsupported', detail: test.unsupported }
            : await capture(app, environment, run, join(directory, scenario.id), { scenario, test, viewport: bounds }, ref);
          cases.push({ id: scenario.id, title: scenario.title, expected: scenario.expected,
            reference: ref.metadata, ...observation });
        }
        results.push({ id: fixture.id, workflowId: workflow.id, title: workflow.title, proposal,
          bundleName: bundle.name, bundleDigest: digest(JSON.stringify([...bundles].map(([name, files]) => [name, [...files]]))),
          specDigest: frozen.digest, visualEvidence,
          plannedScenarios: plan.scenarios.length, plannedTransitions: plan.transitions.length,
          unresolvedItems: plan.unresolved?.length ?? 0, reviewCases: plan.reviewCases?.length ?? 0,
          initial: { reference: entry.metadata, ...initial }, cases });
      }
      const cases = results.flatMap((workflow) => workflow.cases);
      const initialPixels = results.map((workflow) => workflow.initial.pixel).filter(Boolean);
      const totalPixels = initialPixels.reduce((total, sample) => total + sample.totalPixels, 0);
      return { phase, source, standard: design.standard, fixtureDigest, browser: app.browser.version(), workflows: results,
        passedCases: cases.filter(({ status }) => status === 'pass').length, totalCases: cases.length,
        initialPixelRatio: totalPixels ? initialPixels.reduce((sum, item) => sum + item.changedPixels, 0) / totalPixels : null,
        initialSamples: initialPixels.length };
    } finally {
      await app.browser.close();
    }
  });
}
