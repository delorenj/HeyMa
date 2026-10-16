const assert = require('node:assert/strict');
const { existsSync } = require('node:fs');
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

test('builds workflow-import args and accepts binding-scoped document keys', () => {
	assert.deepEqual(
		buildWaxSubcommandArgs({
			kind: 'pipelineImport',
			file: '/srv/exports/wax-documents-workflow.json',
		}),
		['--json', 'pipeline', 'import', '/srv/exports/wax-documents-workflow.json'],
	);
	assert.deepEqual(
		buildWaxSubcommandArgs({
			kind: 'pipelineImport',
			file: '/srv/exports/wax-documents-workflow.json',
			name: 'doc-graph',
		}),
		['--json', 'pipeline', 'import', '/srv/exports/wax-documents-workflow.json', '--name', 'doc-graph'],
	);
	assert.throws(
		() => buildWaxSubcommandArgs({ kind: 'pipelineImport', file: 'exports/wax.json' }),
		/absolute/u,
	);
	assert.throws(
		() => buildWaxSubcommandArgs({ kind: 'pipelineImport', file: '/srv/exports/wax.txt' }),
		/\.json/u,
	);
	// document runs are binding-scoped: doc-<16hex>@<binding-id> is a legal key
	assert.deepEqual(
		buildWaxSubcommandArgs({
			kind: 'epRun',
			slug: 'title-slug',
			itemKey: 'doc-0123456789abcdef@doc-docs-ca9365c0cba1',
		}),
		['--json', 'ep', 'run', 'title-slug', 'doc-0123456789abcdef@doc-docs-ca9365c0cba1'],
	);
	// binding ids embed the folder NAME, and the folder name's case is the
	// user's: a bound `Wax` folder must not fail key validation
	assert.deepEqual(
		buildWaxSubcommandArgs({
			kind: 'epRun',
			slug: 'title-slug',
			itemKey: 'doc-0123456789abcdef@doc-standard-Wax-a1b2c3d4e5f6',
		}),
		['--json', 'ep', 'run', 'title-slug', 'doc-0123456789abcdef@doc-standard-Wax-a1b2c3d4e5f6'],
	);
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

test('every node is registered and exposes the CLI transport parameters', () => {
	const nodes = packageJson.n8n.nodes;
	for (const entry of [
		'dist/nodes/HeyMa/HeyMa.node.js',
		'dist/nodes/WaxEp/WaxEp.node.js',
		'dist/nodes/WaxPipeline/WaxPipeline.node.js',
	]) {
		assert.ok(nodes.includes(entry), `manifest must list ${entry}`);
	}
	// Per-pass node types are generated from the catalog (npm run nodes:passes):
	// every manifest entry must exist on disk as a generated source, and the
	// basename must be a valid identifier — n8n's PackageDirectoryLoader
	// instantiates `new (require(path).<basename>)()` in a vm.Script, so a
	// kebab-case basename kills the whole package load.
	const passNodes = nodes.filter((entry) => /^dist\/nodes\/WaxPass[A-Z]/u.test(entry));
	assert.ok(passNodes.length >= 11, `expected the generated per-pass nodes, got ${passNodes.length}`);
	for (const entry of passNodes) {
		const className = entry.split('/').pop().replace(/\.node\.js$/u, '');
		assert.match(className, /^[A-Z][A-Za-z0-9]*$/u,
			`manifest basename must be a valid class identifier: ${entry}`);
		const rel = entry.replace(/^dist\/nodes\//u, '').replace(/\.js$/u, '.ts');
		assert.ok(existsSync(join('nodes', rel)),
			`manifest entry ${entry} has no generated source`);
	}
	for (const node of [new WaxEp(), new WaxPipeline()]) {
		const names = node.description.properties.map((p) => p.name);
		for (const shared of ['waxExecutable', 'waxRoot', 'timeoutSeconds', 'executeOnce']) {
			assert.ok(names.includes(shared), `${node.description.name} must declare ${shared}`);
		}
		assert.equal(node.description.defaults.name.length > 0, true);
	}
});

test('per-pass nodes are WaxEp-derived node types with the pass fixed and stock two-port outputs', () => {
	const { WaxPassTitleSlug } = require('../dist/nodes/WaxPassTitleSlug/WaxPassTitleSlug.node.js');
	const node = new WaxPassTitleSlug();
	assert.equal(node.description.name, 'wax-pass-title-slug');
	const pass = node.description.properties.find((p) => p.name === 'pass');
	assert.equal(pass.type, 'hidden');
	assert.equal(pass.default, 'title-slug', 'the pass is fixed at generation time');
	// TWO STOCK main outputs: n8n can only wire stock connection types. The
	// KIND travels as the source output index (0 continue / 1 requires), and
	// the labels carry the semantics.
	assert.deepEqual(node.description.outputs, ['main', 'main']);
	assert.deepEqual(node.description.outputNames, ['continue', 'requires']);
	for (const shared of ['waxExecutable', 'waxRoot', 'timeoutSeconds', 'executeOnce']) {
		assert.ok(node.description.properties.some((p) => p.name === shared),
			`per-pass node must declare ${shared}`);
	}
	// the generic node exposes the same stock two-port outputs for ad-hoc graphs
	assert.deepEqual(new WaxEp().description.outputs, ['main', 'main']);
	assert.deepEqual(new WaxEp().description.outputNames, ['continue', 'requires']);
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

// ---------------------------------------------------------------------------
// execute-driven node behavior

test('WaxEp execute returns the two-output shape: results on continue, empty requires', async (t) => {
	const executable = await fakeWax(
		t,
		`console.log(JSON.stringify({ item_id: '0123456789abcdef', ep_slug: 'title-slug', state: 'completed' }));`,
	);
	const params = {
		operation: 'run',
		executeOnce: true,
		waxExecutable: executable,
		waxRoot: '/srv/heyma',
		timeoutSeconds: 5,
		pass: 'title-slug',
		itemKey: '0123456789abcdef',
		attempt: 0,
	};
	const execute = {
		getInputData: () => [{ json: {} }],
		getNodeParameter: (name, _itemIndex, fallback) => (name in params ? params[name] : fallback),
		getNode: () => ({ id: 'test-node', name: 'test-node' }),
		continueOnFail: () => false,
	};
	const outputs = await new WaxEp().execute.call(execute);
	assert.equal(outputs.length, 2, 'two stock main outputs demand a two-array return');
	assert.equal(outputs[0].length, 1);
	assert.equal(outputs[0][0].json.state, 'completed');
	assert.deepEqual(outputs[1], [], 'the requires port is authoring-only: no manual output');
});

test('pass nodes default to Author: execution has zero CLI side effects', async (t) => {
	// The default operation must never touch the transport: point the CLI
	// path at a script that RECORDS its own invocation, then prove the
	// marker never appears — the exact guarantee an authoring graph needs.
	const executable = await fakeWax(
		t,
		`require('node:fs').writeFileSync(process.argv[process.argv.length - 1], 'invoked'); process.exit(7);`,
	);
	const marker = executable + '.invoked';
	const params = {
		// operation deliberately ABSENT: author is the default
		executeOnce: true,
		waxExecutable: executable,
		waxRoot: '/srv/heyma',
		timeoutSeconds: 5,
		pass: 'title-slug',
		when: '[{"field": "classification", "exists": true}]',
	};
	const execute = {
		getInputData: () => [{ json: {} }, { json: {} }],
		getNodeParameter: (name, _itemIndex, fallback) => (name in params ? params[name] : fallback),
		getNode: () => ({ id: 'test-node', name: 'test-node' }),
		continueOnFail: () => false,
	};
	const outputs = await new WaxEp().execute.call(execute);
	assert.equal(outputs.length, 2, 'the two stock main outputs keep their shape');
	assert.equal(outputs[0].length, 2, 'one authoring summary per input item');
	for (const item of outputs[0]) {
		assert.equal(item.json.wax, 'author');
		assert.equal(item.json.pass, 'title-slug');
		assert.match(String(item.json.note), /pipeline import/u,
			'the note explains export → compile instead of executing');
	}
	assert.deepEqual(outputs[1], []);
	assert.equal(existsSync(marker), false, 'the Wax CLI was never invoked');
});

test('Run without an item key is a node error, not a silent empty run', async () => {
	const params = {
		operation: 'run',
		executeOnce: true,
		waxExecutable: '/nonexistent/wax',
		waxRoot: '/nonexistent/heyma',
		pass: 'title-slug',
		// itemKey deliberately missing
	};
	const execute = {
		getInputData: () => [{ json: {} }],
		getNodeParameter: (name, _itemIndex, fallback) => (name in params ? params[name] : fallback),
		getNode: () => ({ id: 'test-node', name: 'test-node' }),
		continueOnFail: () => false,
	};
	await assert.rejects(
		() => new WaxEp().execute.call(execute),
		(error) => {
			assert.equal(error.name, 'NodeOperationError');
			assert.match(error.message, /Item Or Document ID/u);
			return true;
		},
	);
});

test('the shared shell gates run-only and author-only fields by operation', () => {
	for (const node of [new WaxEp(), new (require('../dist/nodes/WaxPassTitleSlug/WaxPassTitleSlug.node.js').WaxPassTitleSlug)()]) {
		const byName = Object.fromEntries(node.description.properties.map((p) => [p.name, p]));
		assert.equal(byName.operation.default, 'author', 'author is the default operation');
		assert.deepEqual(byName.operation.options.map((o) => o.value), ['author', 'run']);
		assert.deepEqual(byName.itemKey.displayOptions?.show?.operation, ['run'],
			'itemKey is only asked for when running: authoring graphs show no missing-field warnings');
		assert.equal(byName.itemKey.required, true);
		assert.deepEqual(byName.attempt.displayOptions?.show?.operation, ['run']);
		assert.deepEqual(byName.when.displayOptions?.show?.operation, ['author'],
			'conditions are authoring fields');
		assert.deepEqual(byName.skipWhen.displayOptions?.show?.operation, ['author']);
	}
});

test('WaxPipeline Configure (Authoring) is a pure preview that never calls the CLI', async () => {
	const params = {
		operation: 'configure',
		trigger: 'documents',
		folders: { folderValues: [{ folder: '/home/delorenj/d/Inbox/Wax' }] },
		failurePolicy: 'independent',
		enabled: false,
		description: 'Inbox enrichment',
		version: 1,
		// a CLI path that CANNOT work: configure must not touch the transport
		waxExecutable: '/nonexistent/wax',
		waxRoot: '/nonexistent/heyma',
	};
	const execute = {
		getInputData: () => [{ json: {} }],
		getNodeParameter: (name, _itemIndex, fallback) => (name in params ? params[name] : fallback),
		getNode: () => ({ id: 'test-node', name: 'test-node' }),
		continueOnFail: () => false,
	};
	const outputs = await new WaxPipeline().execute.call(execute);
	assert.equal(outputs.length, 1);
	const json = outputs[0][0].json;
	// the fixedCollection export shape is normalized to the flat path list
	assert.deepEqual(json.folders, ['/home/delorenj/d/Inbox/Wax']);
	assert.equal(json.trigger, 'documents');
	assert.equal(json.failurePolicy, 'independent');
	assert.equal(json.enabled, false);
	assert.equal(json.version, 1);
	assert.match(json.note, /import/u);
});

test('WaxPipeline declares every authoring parameter the importer reads', () => {
	const node = new WaxPipeline();
	const byName = new Map(node.description.properties.map((p) => [p.name, p]));
	const operations = byName.get('operation').options.map((o) => o.value);
	assert.ok(operations.includes('configure'), 'Configure (Authoring) operation must exist');
	// the importer reads these exact parameter names off the exported wrapper
	for (const [name, expected] of [
		['trigger', 'options'],
		['folders', 'fixedCollection'],
		['failurePolicy', 'options'],
		['enabled', 'boolean'],
		['description', 'string'],
		['version', 'number'],
	]) {
		const property = byName.get(name);
		assert.ok(property, `WaxPipeline must declare ${name}`);
		assert.equal(property.type, expected, `${name} must be a ${expected}`);
		assert.deepEqual(
			property.displayOptions.show.operation,
			['configure'],
			`${name} is shown for the authoring operation`,
		);
	}
	assert.ok(byName.get('exportPath'), 'the Import Workflow operation keeps its path field');
});

// ---------------------------------------------------------------------------
// generated-node parity: the generator output is deterministic and survives
// descriptions that contain newlines, quotes, and backslashes

test('generator escapes newline/quote/backslash descriptions into valid TypeScript', async (t) => {
	const directory = await mkdtemp(join(tmpdir(), 'heyma-generator-'));
	t.after(async () => rm(directory, { force: true, recursive: true }));
	const executable = join(directory, 'fake-wax');
	await writeFile(executable, `#!${process.execPath}
console.log(JSON.stringify({ passes: { passes: [
	{ slug: 'multiline', description: 'line one\\nline two with a quote: it\\'s and a backslash \\\\ plus /api/key=sk-test' },
] } }));
`);
	await chmod(executable, 0o700);
	const generatedDir = join(directory, 'pkg', 'nodes');
	const { mkdir } = require('node:fs/promises');
	await mkdir(generatedDir, { recursive: true });
	await writeFile(join(directory, 'pkg', 'package.json'), JSON.stringify({ n8n: { nodes: [] } }));
	const { execFileSync } = require('node:child_process');
	execFileSync(process.execPath, ['tools/generate-pass-nodes.mjs'], {
		cwd: process.cwd(),
		env: {
			...process.env,
			WAX_EXECUTABLE: executable,
			WAX_ROOT: '/tmp',
			WAX_NODES_ROOT: join(directory, 'pkg'),
		},
	});
	const { readFileSync } = require('node:fs');
	const source = readFileSync(
		join(generatedDir, 'WaxPassMultiline/WaxPassMultiline.node.ts'),
		'utf8',
	);
	// the emitted literal stays on ONE line: real newlines would break syntax
	assert.ok(!source.includes('line one\nline two'), 'newlines must be escaped');
	// quotes, backslashes and the secret-looking token all survive as escapes
	assert.ok(
		source.includes("'line one\\nline two with a quote: it\\'s and a backslash \\\\ plus /api/key=sk-test'"),
		'the description literal must be exactly the escaped form',
	);
	// the manifest is kept in sync in the throwaway package
	const pkg = JSON.parse(readFileSync(join(directory, 'pkg', 'package.json'), 'utf8'));
	assert.deepEqual(pkg.n8n.nodes, [
		'dist/nodes/HeyMa/HeyMa.node.js',
		'dist/nodes/WaxEp/WaxEp.node.js',
		'dist/nodes/WaxPipeline/WaxPipeline.node.js',
		'dist/nodes/WaxPassMultiline/WaxPassMultiline.node.js',
	]);
});
