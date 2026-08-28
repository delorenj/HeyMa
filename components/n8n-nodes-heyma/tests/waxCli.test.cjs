const assert = require('node:assert/strict');
const { chmod, mkdtemp, rm, writeFile } = require('node:fs/promises');
const { tmpdir } = require('node:os');
const { join } = require('node:path');
const test = require('node:test');

const {
	WaxCliError,
	buildWaxArgs,
	parseWaxOutput,
	runWaxCommand,
} = require('../dist/nodes/HeyMa/waxCli.js');

const base = {
	executable: '/opt/heyma/bin/wax',
	operation: 'start',
	timeoutMs: 5_000,
	waxRoot: '/srv/heyma',
};

test('builds a start command with operation-specific fields', () => {
	assert.deepEqual(buildWaxArgs({ ...base, bitrate: '48k', label: 'team standup' }), [
		'--json',
		'rec',
		'start',
		'--label',
		'team standup',
		'--bitrate',
		'48k',
	]);
});

test('builds stop commands with and without an explicit capture ID', () => {
	assert.deepEqual(
		buildWaxArgs({ ...base, captureId: '20260821-071500-abcdef', operation: 'stop' }),
		['--json', 'rec', 'stop', '20260821-071500-abcdef'],
	);
	assert.deepEqual(buildWaxArgs({ ...base, operation: 'stop' }), ['--json', 'rec', 'stop']);
});

test('rejects path lookup, unsafe labels, and malformed capture IDs', () => {
	assert.throws(() => buildWaxArgs({ ...base, executable: 'wax' }), /absolute path/u);
	assert.throws(() => buildWaxArgs({ ...base, label: '../meeting' }), /path separators/u);
	assert.throws(
		() => buildWaxArgs({ ...base, captureId: '../../rec', operation: 'stop' }),
		/Capture ID/u,
	);
});

test('parses one JSON object and rejects invalid output', () => {
	assert.deepEqual(parseWaxOutput('{"started":"rid-1"}\n'), { started: 'rid-1' });
	assert.throws(() => parseWaxOutput(''), /empty response/u);
	assert.throws(() => parseWaxOutput('[]'), /must be an object/u);
	assert.throws(() => parseWaxOutput('not-json'), /invalid JSON/u);
});

test('executes an absolute fake Wax binary without a shell and passes WAX_ROOT', async (t) => {
	const directory = await mkdtemp(join(tmpdir(), 'heyma-node-'));
	t.after(async () => rm(directory, { force: true, recursive: true }));
	const executable = join(directory, 'fake-wax');
	await writeFile(
		executable,
		`#!${process.execPath}\nconsole.log(JSON.stringify({ args: process.argv.slice(2), root: process.env.WAX_ROOT }));\n`,
	);
	await chmod(executable, 0o700);

	const result = await runWaxCommand({
		...base,
		bitrate: '32k',
		executable,
		label: 'safe label',
	});
	assert.deepEqual(result.response, {
		args: ['--json', 'rec', 'start', '--label', 'safe label', '--bitrate', '32k'],
		root: '/srv/heyma',
	});
});

test('surfaces fake Wax stderr and exit status', async (t) => {
	const directory = await mkdtemp(join(tmpdir(), 'heyma-node-'));
	t.after(async () => rm(directory, { force: true, recursive: true }));
	const executable = join(directory, 'fake-wax-failure');
	await writeFile(
		executable,
		`#!${process.execPath}\nconsole.error('stream is not ready');\nprocess.exit(2);\n`,
	);
	await chmod(executable, 0o700);

	await assert.rejects(
		runWaxCommand({ ...base, executable }),
		(error) =>
			error instanceof WaxCliError &&
			error.exitCode === 2 &&
			error.stderr === 'stream is not ready',
	);
});
