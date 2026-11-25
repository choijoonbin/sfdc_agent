import assert from 'node:assert/strict';
import { createHash, randomUUID } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { basename, dirname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const nodeId = '29876:15045';
const pageId = '29876:13869';
const fileKey = 'VWB6ajoIbdvhK3mu8DwgKT';
const sha256 = (bytes) => createHash('sha256').update(bytes).digest('hex');
const modules = [
  'engine/generate.mjs',
  'engine/local-fig-archive.mjs',
  'engine/measured-form-controls.mjs',
  'engine/measured-panel-controls.mjs',
  'engine/figma-image.mjs',
  'engine/render.mjs',
  'server/workbench.mjs',
  'server/preview.mjs',
  'server/visual-capture.mjs',
  'server/visual-diff.mjs',
  'server/visual-mask-png.mjs',
];

async function withScreen({ uiRoot, sourcePath, sourceSha256, releaseId, releaseRoot }, use) {
  const nova = Object.assign({}, ...await Promise.all(modules.map((file) =>
    import(pathToFileURL(join(uiRoot, 'nextbss', file)).href))));
  const design = await nova.readNextBssStandard({ releaseId, releaseRoot });
  if (design.standard.releaseId !== releaseId || design.standard.version !== '1.9') {
    throw new Error('The requested NextBSS v1.9 release could not be pinned.');
  }
  const storyboard = [design.registry.contentScreen, ...(design.registry.localContentScreens ?? [])];
  const archive = await nova.openLocalFigArchive(sourcePath, { name: basename(sourcePath), storyboard });
  try {
    if (archive.source.version !== 'local-' + sourceSha256 ||
        (archive.source.fileKey && archive.source.fileKey !== fileKey)) {
      throw new Error('The Screen archive does not match the pinned Figma source.');
    }
    const { page } = await archive.page(pageId);
    if (!page.screens.some((screen) => screen.id === nodeId && screen.eligible)) {
      throw new Error('The pilot node is not an eligible Screen on the pinned Figma page.');
    }
    const { document: node, components, componentSets, styles } = await archive.snapshot(nodeId);
    const snapshot = { node, components, componentSets, styles, fileKey, nodeId, pageId };
    const fields = nova.collectMeasuredFormControls(snapshot, design);
    const { accordions, buttons } = nova.prepareMeasuredPanelControls(snapshot, design);
    return await use({
      nova, design, archive, fields, accordions, buttons,
      source: { fileKey, nodeId, pageId, sha256: sourceSha256, version: archive.source.version },
    });
  } finally {
    await archive.close();
  }
}

function screenEvidence({ fields, accordions, buttons }) {
  const records = (values) => values.map(({ nodeId, label }) => ({ nodeId, label }));
  return [
    { id: 'screen.input', check: 'required and editable text input', nodes: records(fields.filter((field) => field.kind === 'input' && !field.readOnly)) },
    { id: 'screen.radio', check: 'initial and mutually exclusive radio selection', nodes: records(fields.filter((field) => field.kind === 'radio')) },
    { id: 'screen.accordion', check: 'closed and open accordion states', nodes: records([...accordions.values()]) },
    { id: 'screen.readonly', check: 'read-only input remains read-only', nodes: records(fields.filter((field) => field.readOnly)) },
    { id: 'screen.select-current', check: 'five source-backed current labels', nodes: records(fields.filter((field) => field.kind === 'select')) },
    { id: 'screen.reset', check: 'local reset restores initial field and radio state', nodes: records([...buttons.values()].filter((button) => button.action === 'reset')) },
    { id: 'screen.search', check: 'local search event with source node ID', nodes: records([...buttons.values()].filter((button) => button.action === 'search')) },
    { id: 'screen.business-gap', check: 'Salesforce operations, responses, and authorization require business specifications', nodes: [{ nodeId }] },
    { id: 'screen.options-gap', check: 'source shows current select labels, full options and data source require business specifications', nodes: [{ nodeId }] },
  ];
}

export async function readScreenEvidence(context) {
  return withScreen(context, (environment) => ({
    source: environment.source, releaseId: environment.design.standard.releaseId,
    entries: screenEvidence(environment),
  }));
}

async function officialPng(path, source, dimensions, inspect) {
  if (!path) return null;
  const { source: reference } = JSON.parse(await readFile(path, 'utf8'));
  if (reference?.fileKey !== source.fileKey || reference.sha256 !== source.sha256 || !/^\d+$/.test(reference.version ?? '')) {
    throw new Error('The reference manifest does not bind a Figma revision to this archive.');
  }
  const pin = reference.pngs?.[nodeId];
  if (!pin) return null;
  if (pin.nodeId !== nodeId || pin.fileKey !== source.fileKey || pin.version !== reference.version ||
      pin.scale !== 1 || typeof pin.path !== 'string') {
    throw new Error('Invalid Figma 1× PNG entry for the Screen.');
  }
  const png = await readFile(resolve(dirname(path), pin.path));
  const size = inspect(png);
  if (sha256(png) !== pin.sha256 || size.width !== dimensions.width || size.height !== dimensions.height) {
    throw new Error('Figma 1× PNG digest or dimensions differ for the Screen.');
  }
  return { png, version: pin.version };
}

async function reuseReference(directory, reference, source, releaseId, dimensions, inspect) {
  if (!directory) return null;
  const receipt = JSON.parse(await readFile(join(directory, 'result.json'), 'utf8'));
  const png = await readFile(join(directory, 'source.png'));
  const size = inspect(png);
  const hash = sha256(png);
  if (receipt.source?.nodeId !== source.nodeId || receipt.source?.sha256 !== source.sha256
      || receipt.releaseId !== releaseId || receipt.visual?.referenceSha256 !== hash
      || receipt.visual.comparisonMode !== (reference ? 'official' : 'reference')
      || (reference && hash !== sha256(reference.png))
      || size.width !== dimensions.width || size.height !== dimensions.height) {
    throw new Error('The baseline Screen image differs from the pinned source or release.');
  }
  return png;
}

async function capture(nova, { uiRoot, assets, basePath, dimensions, sourceSvg, referencePng }) {
  const font = await readFile(join(uiRoot, 'nextbss/server/assets/PretendardVariable-1.3.9.woff2'));
  const server = createServer((request, response) => {
    const path = request.url.split('?')[0];
    const entry = path === nova.catalogFontPath
      ? { body: font, type: 'font/woff2' }
      : path.startsWith(basePath + '/') && assets.get(path.slice(basePath.length + 1));
    if (request.method !== 'GET' || !entry) {
      response.writeHead(404).end();
      return;
    }
    response.writeHead(200, { 'Content-Type': entry.type, 'Access-Control-Allow-Origin': '*' });
    response.end(entry.body);
  });
  try {
    await new Promise((accept, reject) => {
      server.once('error', reject);
      server.listen(0, '127.0.0.1', accept);
    });
    return await nova.captureVisualPair({
      origin: 'http://127.0.0.1:' + server.address().port,
      previewPath: basePath + '/index.html',
      ...dimensions,
      ...(referencePng ? { sourcePng: referencePng } : { sourceSvg }),
    });
  } finally {
    await new Promise((accept) => server.close(accept));
  }
}

function screenChecks({ fields, accordions, buttons }, name) {
  const root = (frame) => frame.locator('c-' + name.replace(/[A-Z]/g, (character) => '-' + character.toLowerCase()));
  const service = (frame) => frame.getByRole('textbox', { name: '서비스 번호', exact: true });
  const chooseRadio = (frame, label) => frame.getByRole('radio', { name: label, exact: true });
  const interaction = { timeout: 3_000 };
  const checks = {
    'screen.input': async (frame) => {
      const input = service(frame);
      assert.notEqual(await input.getAttribute('required', interaction), null);
      assert.ok(await input.isEditable());
      const initial = await input.inputValue(interaction);
      const edited = initial === '01000000000' ? '01000000001' : '01000000000';
      await input.fill(edited, interaction);
      await input.blur(interaction);
      assert.equal(await input.inputValue(interaction), edited);
      await input.fill(initial, interaction);
      await input.blur(interaction);
    },
    'screen.radio': async (frame) => {
      const prepaid = chooseRadio(frame, '선불가입');
      const postpaid = chooseRadio(frame, '후불가입');
      assert.equal(await prepaid.isChecked(interaction), false);
      assert.equal(await postpaid.isChecked(interaction), true);
      await prepaid.check(interaction);
      assert.equal(await prepaid.isChecked(interaction), true);
      assert.equal(await postpaid.isChecked(interaction), false);
      await postpaid.check(interaction);
    },
    'screen.accordion': async (frame) => {
      const closed = [...accordions.values()].find((panel) => !panel.open && panel.alternateBodyNodeId);
      assert.ok(closed);
      const header = frame.getByRole('button', { name: closed.label, exact: true }).first();
      assert.equal(await header.getAttribute('aria-expanded', interaction), 'false');
      await header.press('Enter', interaction);
      assert.equal(await header.getAttribute('aria-expanded', interaction), 'true');
      const target = await header.getAttribute('aria-controls', interaction);
      assert.ok(target);
      assert.equal(await frame.locator('[id=' + JSON.stringify(target) + ']').isVisible(), true);
      await header.press('Space', interaction);
      assert.equal(await header.getAttribute('aria-expanded', interaction), 'false');
      assert.equal(await frame.locator('[id=' + JSON.stringify(target) + ']').isVisible(), false);
    },
    'screen.readonly': async (frame) => {
      const label = fields.find((field) => field.kind === 'input' && field.readOnly)?.label;
      assert.ok(label);
      const input = frame.getByRole('textbox', { name: label, exact: true }).first();
      assert.notEqual(await input.getAttribute('readonly', interaction), null);
      assert.equal(await input.isEditable(interaction), false);
    },
    'screen.select-current': async (frame) => {
      const selects = fields.filter((field) => field.kind === 'select');
      assert.equal(selects.length, 5);
      for (const field of selects) {
        const select = frame.getByRole('combobox', { name: field.label, exact: true });
        assert.ok(await select.isVisible());
        assert.equal(await select.inputValue(interaction), field.value);
      }
    },
    'screen.reset': async (frame) => {
      const input = service(frame);
      const prepaid = chooseRadio(frame, '선불가입');
      const postpaid = chooseRadio(frame, '후불가입');
      const initial = await input.inputValue(interaction);
      const edited = initial === '01000000000' ? '01000000001' : '01000000000';
      await input.fill(edited, interaction);
      await prepaid.check(interaction);
      const reset = [...buttons.values()].find((button) => button.action === 'reset');
      assert.ok(reset);
      await frame.getByRole('button', { name: reset.label, exact: true }).click(interaction);
      assert.equal(await input.inputValue(interaction), initial);
      assert.equal(await prepaid.isChecked(interaction), false);
      assert.equal(await postpaid.isChecked(interaction), true);
    },
    'screen.search': async (frame) => {
      const search = [...buttons.values()].find((button) => button.action === 'search');
      assert.ok(search);
      await root(frame).evaluate((host) => {
        host.addEventListener('screenaction', ({ detail }) => {
          host.dataset.pilotAction = JSON.stringify(detail);
        }, { once: true });
      }, undefined, interaction);
      await frame.locator('.measured-controls').getByRole('button', { name: search.label, exact: true }).click(interaction);
      assert.deepEqual(JSON.parse(await root(frame).getAttribute('data-pilot-action', interaction)),
        { nodeId: search.nodeId, action: 'search' });
    },
  };
  return checks;
}

async function runBehavior(environment, preview, name, plan) {
  const core = ['screen.input', 'screen.radio', 'screen.accordion'];
  const checks = screenChecks(environment, name);
  const evidence = new Set(screenEvidence(environment).map(({ id }) => id));
  const accepted = [];
  const rejected = [];
  for (const { evidenceId, explanation } of plan?.findings ?? []) {
    if (!evidence.has(evidenceId)) {
      rejected.push({ evidenceId, reason: 'Evidence ID is absent from this source.' });
    } else if (!accepted.some((finding) => finding.evidenceId === evidenceId)) {
      accepted.push({ evidenceId, explanation });
    }
  }
  const selected = accepted.map((finding) => finding.evidenceId).filter((id) => Object.hasOwn(checks, id) && !core.includes(id));
  const results = [];
  await environment.nova.withCompiledPreview(preview, async (frame) => {
    for (const id of [...core, ...selected]) {
      try {
        await checks[id](frame);
        results.push({ id, status: 'pass' });
      } catch (error) {
        results.push({ id, status: 'fail', detail: String(error.message).split('\n')[0].slice(0, 350) });
      }
    }
  });
  const outcomes = results.filter(({ id }) => core.includes(id));
  return {
    behavior: {
      checks: results,
      pass: outcomes.filter(({ status }) => status === 'pass').length,
      fail: outcomes.filter(({ status }) => status === 'fail').length,
      supplementalChecks: selected,
      supplementalPass: results.filter(({ id, status }) => selected.includes(id) && status === 'pass').length,
      supplementalFail: results.filter(({ id, status }) => selected.includes(id) && status === 'fail').length,
    },
    review: { accepted, rejected, selectedVerificationChecks: selected, codeChanges: false },
  };
}

export async function runScreen(context) {
  return withScreen(context, async (environment) => {
    const { nova, design, archive, source } = environment;
    const name = 'nextBssScreen' + nodeId.replace(':', 'x') + 'F' + sha256(fileKey).slice(0, 12);
    const converted = await nova.prepareLocalFigmaBundle(archive, {
      pageId, nodeId, name, expose: true, draftFallback: true, linkedFileKey: fileKey, design,
    });
    if (!converted.files) throw new Error('The existing NOVA Screen generator did not produce a bundle.');
    const dimensions = {
      width: Math.ceil(converted.report.dimensions.width), height: Math.ceil(converted.report.dimensions.height),
    };
    const bundles = await nova.collectBundles(name, converted.files, { release: design.release });
    const basePath = '/api/nextbss/drafts/' + randomUUID() + '/preview';
    const assets = await nova.compilePreview({ name, bundles, basePath });
    const compiled = { assets, basePath, ...dimensions };
    const reference = await officialPng(context.referenceManifest, source, dimensions, nova.inspectFigmaPng);
    const baselinePng = await reuseReference(context.referenceDirectory, reference, source, design.standard.releaseId,
      dimensions, nova.inspectFigmaPng);
    const referencePng = baselinePng ?? reference?.png;
    const original = referencePng ? null : await archive.preview(nodeId);
    const images = await capture(nova, {
      uiRoot: context.uiRoot, assets, basePath, dimensions, sourceSvg: original?.svg, referencePng,
    });
    const mask = Buffer.alloc(images.source.length);
    const pixel = nova.compareVisualPixels(images.source, images.rendered, { ...dimensions, mask });
    const maskPng = await nova.encodeVisualMask(mask, dimensions.width, dimensions.height);
    const { outputDir } = context;
    await Promise.all([
      writeFile(join(outputDir, 'source.png'), images.sourcePng, { mode: 0o600 }),
      writeFile(join(outputDir, 'rendered.png'), images.renderedPng, { mode: 0o600 }),
      writeFile(join(outputDir, 'difference.png'), maskPng, { mode: 0o600 }),
      ...[...bundles].map(([bundleName, files]) => nova.writeBundle(join(outputDir, 'lwc', bundleName), files)),
    ]);
    const { behavior, review } = await runBehavior(environment, compiled, name, context.modelPlan);
    const bundleSha256 = sha256(JSON.stringify([...bundles].map(([bundleName, files]) => [bundleName, [...files]])));
    return {
      source, releaseId: design.standard.releaseId, designVersion: design.standard.version, bundleSha256,
      bundleName: name, bundleDirectory: 'lwc', dimensions,
      visual: {
        ...pixel,
        comparisonMode: reference ? 'official' : 'reference',
        referenceKind: reference ? 'figma-1x-png' : 'local-fig-svg',
        referenceVersion: reference?.version,
        referenceSha256: sha256(images.sourcePng),
        renderedSha256: sha256(images.renderedPng),
        images: { reference: 'source.png', rendered: 'rendered.png', difference: 'difference.png' },
      },
      behavior, review,
      issues: converted.report.issues.map(({ code, nodeId: issueNodeId }) => ({ code, nodeId: issueNodeId })),
      applyBlockers: converted.report.applyBlockers,
    };
  });
}
