const assert = require('node:assert/strict');
const { chmod, mkdtemp, rm, writeFile } = require('node:fs/promises');
const { tmpdir } = require('node:os');
const { join } = require('node:path');
const test = require('node:test');

const {
	WaxCliError,
	buildWaxSubcommandArgs,
	runWaxSubcommand,
	waxSubcommandInput,
} = require('../dist/nodes/HeyMa/waxCli.js');
const { WaxCalls } = require('../dist/nodes/WaxCalls/WaxCalls.node.js');
const packageJson = require('../package.json');

const EVENT = {
	headers: { 'telnyx-timestamp': '1793736049' },
	body: { data: { id: 'e1', event_type: 'call.hangup', payload: { call_session_id: 's1' } } },
};

async function fakeWax(t, script) {
	const directory = await mkdtemp(join(tmpdir(), 'heyma-calls-'));
	t.after(async () => rm(directory, { force: true, recursive: true }));
	const executable = join(directory, 'fake-wax');
	await writeFile(executable, `#!${process.execPath}\n${script}\n`);
	await chmod(executable, 0o700);
	return executable;
}

test('calls args: the event goes on stdin, never argv; sync and show are validated', () => {
	assert.deepEqual(buildWaxSubcommandArgs({ kind: 'callsEvent', payload: EVENT }), ['--json', 'calls', 'event']);
	assert.deepEqual(buildWaxSubcommandArgs({ kind: 'callsEvent', payload: EVENT, fetch: false }),
		['--json', 'calls', 'event', '--no-fetch']);
	assert.deepEqual(buildWaxSubcommandArgs({ kind: 'callsSync', since: '6h' }), ['--json', 'calls', 'sync', '--since', '6h']);
	assert.deepEqual(buildWaxSubcommandArgs({ kind: 'callsSync', since: '2026-11-01T00:00:00Z' }),
		['--json', 'calls', 'sync', '--since', '2026-11-01T00:00:00Z']);
	assert.throws(() => buildWaxSubcommandArgs({ kind: 'callsSync', since: '--evil' }), /Since/u);
	assert.deepEqual(buildWaxSubcommandArgs({ kind: 'callsShow', ref: '830f689e-d71e-11f1-b19a-4674b796d526' }),
		['--json', 'calls', 'show', '830f689e-d71e-11f1-b19a-4674b796d526']);
	assert.throws(() => buildWaxSubcommandArgs({ kind: 'callsShow', ref: '../x' }), /Call must be/u);
	assert.equal(waxSubcommandInput({ kind: 'catalog' }), undefined);
	assert.throws(() => waxSubcommandInput({ kind: 'callsEvent', payload: [1] }), /JSON object/u);
	assert.throws(() => waxSubcommandInput({ kind: 'callsEvent', payload: { x: 'y'.repeat(300_000) } }), /256 KiB/u);
});

test('the event reaches the CLI on stdin intact', async (t) => {
	const executable = await fakeWax(t, `let raw = '';
process.stdin.on('data', (c) => { raw += c; });
process.stdin.on('end', () => console.log(JSON.stringify({ args: process.argv.slice(2), got: JSON.parse(raw) })));`);
	const result = await runWaxSubcommand({
		command: { kind: 'callsEvent', payload: EVENT },
		executable,
		timeoutMs: 5_000,
		waxRoot: '/srv/heyma',
	});
	assert.deepEqual(result.response.args, ['--json', 'calls', 'event']);
	assert.deepEqual(result.response.got, EVENT);
});

test('a command that ignores stdin still exits (stdin is closed, not inherited)', async (t) => {
	const executable = await fakeWax(t, `let n = 0; process.stdin.on('data', () => n++); process.stdin.on('end', () => console.log(JSON.stringify({ n })));`);
	const result = await runWaxSubcommand({ command: { kind: 'catalog' }, executable, timeoutMs: 5_000, waxRoot: '/srv/heyma' });
	assert.deepEqual(result.response, { n: 0 });
});

test('a rejected event surfaces the CLI error', async (t) => {
	const executable = await fakeWax(t, `console.error('error: not a Telnyx v2 event envelope (not_an_event)'); process.exit(2);`);
	await assert.rejects(
		runWaxSubcommand({ command: { kind: 'callsEvent', payload: { hello: 'world' } }, executable, timeoutMs: 5_000, waxRoot: '/srv/heyma' }),
		(error) => error instanceof WaxCliError && error.exitCode === 2 && /not_an_event/u.test(error.stderr),
	);
});

test('Wax Calls is registered, runs per item by default and shares the transport', () => {
	assert.ok(packageJson.n8n.nodes.includes('dist/nodes/WaxCalls/WaxCalls.node.js'));
	assert.ok(packageJson.n8n.nodes.includes('dist/nodes/WaxPassCallMetadata/WaxPassCallMetadata.node.js'));
	const node = new WaxCalls();
	assert.equal(node.description.name, 'waxCalls');
	const props = Object.fromEntries(node.description.properties.map((p) => [p.name, p]));
	for (const shared of ['waxExecutable', 'waxRoot', 'timeoutSeconds', 'executeOnce']) assert.ok(props[shared]);
	assert.equal(props.executeOnce.default, false, 'one webhook item is one event');
	assert.deepEqual(props.operation.options.map((o) => o.value).sort(), ['event', 'show', 'sync']);
});
