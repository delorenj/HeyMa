const assert = require('node:assert/strict');
const { chmod, mkdtemp, rm, writeFile } = require('node:fs/promises');
const { tmpdir } = require('node:os');
const { join } = require('node:path');
const test = require('node:test');

const {
	WaxCliError,
	buildWaxSubcommandArgs,
	parseWaxOutput,
	runWaxSubcommand,
} = require('../dist/nodes/HeyMa/waxCli.js');
const { WaxEp } = require('../dist/nodes/WaxEp/WaxEp.node.js');
const { WaxPipeline } = require('../dist/nodes/WaxPipeline/WaxPipeline.node.js');
const packageJson = require('../package.json');

// ---------------------------------------------------------------------------
// subcommand arg building: every enrichment operation is a validated CLI call

test('builds ep run args and validates slugs, keys, and attempts', () => {
	assert.deepEqual(buildWaxSubcommandArgs({ kind: 'epRun', slug: 'title-slug', itemKey: '0123456789abcdef' }), [
		'--json', 'ep', 'run', 'title-slug', '0123456789abcdef',
	]);
	assert.deepEqual(
		buildWaxSubcommandArgs({ kind: 'epRun', slug: 'classification', itemKey: 'doc-0123456789abcdef', attempt: 3 }),
		['--json', 'ep', 'run', 'classification', 'doc-0123456789abcdef', '--attempt', '3'],
	);
	assert.throws(() => buildWaxSubcommandArgs({ kind: 'epRun', slug: '../evil', itemKey: '0123456789abcdef' }), /pass slug/u);
	assert.throws(() => buildWaxSubcommandArgs({ kind: 'epRun', slug: 'title-slug', itemKey: 'not-an-id' }), /Item/u);
	assert.throws(() => buildWaxSubcommandArgs({ kind: 'epRun', slug: 'title-slug', itemKey: '0123456789abcdef', attempt: 0 }), /Attempt/u);
});

test('builds catalog, pipeline, and document args with name/path validation', () => {
	assert.deepEqual(buildWaxSubcommandArgs({ kind: 'catalog' }), ['--json', 'catalog']);
	assert.deepEqual(buildWaxSubcommandArgs({ kind: 'pipelineCompile', names: ['doc-standard'] }), [
		'--json', 'pipeline', 'compile', 'doc-standard',
	]);
	assert.deepEqual(buildWaxSubcommandArgs({ kind: 'pipelineCompile' }), ['--json', 'pipeline', 'compile']);
	assert.deepEqual(buildWaxSubcommandArgs({ kind: 'pipelineShow', name: 'doc-standard' }), [
		'--json', 'pipeline', 'show', 'doc-standard',
	]);
	assert.deepEqual(buildWaxSubcommandArgs({ kind: 'documentsScan', pipeline: 'doc-standard' }), [
		'--json', 'documents', 'scan', 'doc-standard',
	]);
	assert.deepEqual(buildWaxSubcommandArgs({ kind: 'documentsScan' }), ['--json', 'documents', 'scan']);
	assert.deepEqual(
		buildWaxSubcommandArgs({ kind: 'documentsRun', pipeline: 'doc-standard', file: '/srv/docs/note.md' }),
		['--json', 'documents', 'run', 'doc-standard', '/srv/docs/note.md'],
	);
	assert.throws(() => buildWaxSubcommandArgs({ kind: 'pipelineShow', name: 'Bad_Name' }), /pipeline name/u);
	assert.throws(() => buildWaxSubcommandArgs({ kind: 'documentsRun', pipeline: 'ok', file: 'relative.md' }), /absolute/u);
});

test('rejects array responses even though ep list emits one natively', () => {
	assert.throws(() => parseWaxOutput('[{"slug":"a"}]'), /must be an object/u);
});

// ---------------------------------------------------------------------------
// transport against a fake absolute Wax binary

async function fakeWax(t, script) {
	const directory = await mkdtemp(join(tmpdir(), 'heyma-wax-nodes-'));
	t.after(async () => rm(directory, { force: true, recursive: true }));
	const executable = join(directory, 'fake-wax');
	await writeFile(executable, `#!${process.execPath}\n${script}\n`);
	await chmod(executable, 0o700);
	return executable;
}

test('runs a catalog read through the CLI with WAX_ROOT and parses the object', async (t) => {
	const executable = await fakeWax(
		t,
		`console.log(JSON.stringify({ args: process.argv.slice(2), root: process.env.WAX_ROOT, passes: { passes: [{ slug: 'a' }] } }));`,
	);
	const result = await runWaxSubcommand({
		command: { kind: 'catalog' },
		executable,
		timeoutMs: 5_000,
		waxRoot: '/srv/heyma',
	});
	assert.deepEqual(result.response.args, ['--json', 'catalog']);
	assert.equal(result.response.root, '/srv/heyma');
});

test('surfaces a CLI failure with exit status', async (t) => {
	const executable = await fakeWax(t, `console.error('pipeline not compiled'); process.exit(2);`);
	await assert.rejects(
		runWaxSubcommand({
			command: { kind: 'pipelineShow', name: 'doc-standard' },
			executable,
			timeoutMs: 5_000,
			waxRoot: '/srv/heyma',
		}),
		(error) => error instanceof WaxCliError && error.exitCode === 2 && /pipeline not compiled/u.test(error.stderr),
	);
});

// ---------------------------------------------------------------------------
// node contracts

test('both nodes are registered and expose the CLI transport parameters', () => {
	assert.deepEqual(packageJson.n8n.nodes, [
		'dist/nodes/HeyMa/HeyMa.node.js',
		'dist/nodes/WaxEp/WaxEp.node.js',
		'dist/nodes/WaxPipeline/WaxPipeline.node.js',
	]);
	for (const node of [new WaxEp(), new WaxPipeline()]) {
		const names = node.description.properties.map((p) => p.name);
		for (const shared of ['waxExecutable', 'waxRoot', 'timeoutSeconds', 'executeOnce']) {
			assert.ok(names.includes(shared), `${node.description.name} must declare ${shared}`);
		}
		assert.equal(node.description.defaults.name.length > 0, true);
	}
});

test('WaxEp registers passes through the CLI catalog (loadOptions), never a static list', async (t) => {
	assert.equal(new WaxEp().methods.loadOptions.passCatalog.name, 'loadPassOptions');
	const executable = await fakeWax(
		t,
		`console.log(JSON.stringify({ passes: { passes: [
			{ slug: 'classification', description: 'Grounded classification.' },
			{ slug: 'wikification', description: 'TODO — placeholder registered by Wax.' },
		] } }));`,
	);
	const options = await new WaxEp().methods.loadOptions.passCatalog.call({
		getCurrentNodeParameter: (name) =>
			name === 'waxExecutable' ? executable : '/srv/heyma',
	});
	assert.deepEqual(options, [
		{ name: 'classification — Grounded classification.', value: 'classification' },
		{ name: 'wikification — TODO — placeholder registered by Wax.', value: 'wikification' },
	]);
});

test('catalog loadOptions fails loudly when the CLI is unavailable', async (t) => {
	await assert.rejects(
		new WaxEp().methods.loadOptions.passCatalog.call({
			getCurrentNodeParameter: () => '/nonexistent/wax',
		}),
		/nonexistent/u,
	);
});

function fakeExecute(overrides = {}) {
	const params = { executeOnce: true, ...overrides };
	return {
		getInputData: () => [{ json: {} }],
		getNodeParameter: (name, _itemIndex, fallback) => (name in params ? params[name] : fallback),
		getNode: () => ({ id: 'test-node', name: 'test-node' }),
		continueOnFail: () => true,
	};
}

test('runWaxPerItem surfaces exit-zero failures through continueOnFail output', async (t) => {
	const executable = await fakeWax(
		t,
		`console.log(JSON.stringify({ document: 'doc-0123456789abcdef', pipeline: 'doc-standard', completed: 2, failed: 1, results: [{ ep_slug: 'title-slug', state: 'failed', reason_code: 'provider_down' }] }));`,
	);
	const { WaxNodeBase } = require('../dist/nodes/WaxEp/WaxEpBase.js');
	const execute = fakeExecute({ waxExecutable: executable, waxRoot: '/srv/heyma', timeoutSeconds: 5 });
	const [items] = await WaxNodeBase.runWaxPerItem(execute, () => ({
		command: { kind: 'documentsRun', pipeline: 'doc-standard', file: '/srv/docs/note.md' },
		interpret: (response) =>
			Number(response.failed ?? 0) > 0 ? { reasonCode: 'failed_passes' } : undefined,
	}));
	assert.equal(items.length, 1);
	assert.match(String(items[0].json.error), /Wax reported failure: failed_passes/u);
});
