/* eslint-disable @n8n/community-nodes/require-node-api-error -- The node boundary wraps these transport errors in NodeOperationError. */
import { execFile } from 'node:child_process';
import { isAbsolute } from 'node:path';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);
const MAX_OUTPUT_BYTES = 1024 * 1024;
const ALLOWED_BITRATES = new Set(['16k', '24k', '32k', '48k', '64k']);
const CAPTURE_ID = /^\d{8}-\d{6}-[0-9a-f]{6}$/u;

export type RecordingOperation = 'start' | 'stop';
export type JsonPrimitive = boolean | null | number | string;
export type JsonValue = JsonObject | JsonPrimitive | JsonValue[];

export interface JsonObject {
	[key: string]: JsonValue;
}

export interface WaxCommandOptions {
	bitrate?: string;
	captureId?: string;
	executable: string;
	label?: string;
	operation: RecordingOperation;
	timeoutMs: number;
	waxRoot: string;
}

export interface WaxCommandResult {
	response: JsonObject;
	stderr: string;
}

interface ErrorFields {
	code?: number | string;
	killed?: boolean;
	message?: string;
	signal?: string;
	stderr?: Buffer | string;
	stdout?: Buffer | string;
}

export class WaxCliError extends Error {
	readonly exitCode?: number | string;
	readonly stderr: string;
	readonly stdout: string;

	constructor(message: string, fields: ErrorFields = {}) {
		super(message);
		this.name = 'WaxCliError';
		this.exitCode = fields.code;
		this.stderr = text(fields.stderr);
		this.stdout = text(fields.stdout);
	}
}

function text(value: Buffer | string | undefined): string {
	if (Buffer.isBuffer(value)) return value.toString('utf8').trim();
	return typeof value === 'string' ? value.trim() : '';
}

function errorFields(value: unknown): ErrorFields {
	if (typeof value !== 'object' || value === null) return {};
	const candidate = value as ErrorFields;
	return candidate;
}

function cleanOptional(value: string | undefined): string | undefined {
	const cleaned = value?.trim();
	return cleaned ? cleaned : undefined;
}

function validateConfiguration(options: WaxCommandOptions): void {
	if (!isAbsolute(options.executable)) {
		throw new WaxCliError('Wax Executable must be an absolute path; PATH lookup is disabled');
	}
	if (!isAbsolute(options.waxRoot)) {
		throw new WaxCliError('Wax Root must be an absolute path');
	}
	if (
		!Number.isInteger(options.timeoutMs) ||
		options.timeoutMs < 1000 ||
		options.timeoutMs > 7_200_000
	) {
		throw new WaxCliError('Timeout must be between 1 and 7200 seconds');
	}
}

export function buildWaxArgs(options: WaxCommandOptions): string[] {
	validateConfiguration(options);
	const args = ['--json', 'rec', options.operation];

	if (options.operation === 'start') {
		const label = cleanOptional(options.label);
		if (label) {
			if (label.length > 120 || /[\0/\\\r\n]/u.test(label)) {
				throw new WaxCliError(
					'Label must be 120 characters or fewer and cannot contain path separators or newlines',
				);
			}
			args.push('--label', label);
		}

		const bitrate = cleanOptional(options.bitrate) ?? '32k';
		if (!ALLOWED_BITRATES.has(bitrate)) {
			throw new WaxCliError(`Unsupported bitrate: ${bitrate}`);
		}
		args.push('--bitrate', bitrate);
		return args;
	}

	const captureId = cleanOptional(options.captureId);
	if (captureId) {
		if (!CAPTURE_ID.test(captureId)) {
			throw new WaxCliError('Capture ID must look like YYYYMMDD-HHMMSS-abcdef');
		}
		args.push(captureId);
	}
	return args;
}

export function parseWaxOutput(stdout: string): JsonObject {
	const output = stdout.trim();
	if (!output) throw new WaxCliError('Wax returned an empty response');

	let parsed: unknown;
	try {
		parsed = JSON.parse(output);
	} catch (error: unknown) {
		const message = error instanceof Error ? error.message : String(error);
		throw new WaxCliError(`Wax returned invalid JSON: ${message}`, { stdout: output });
	}

	if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
		throw new WaxCliError('Wax JSON response must be an object', { stdout: output });
	}
	return parsed as JsonObject;
}

// ---------------------------------------------------------------------------
// Enrichment control surface: every operation is a CLI invocation. Nodes never
// run pass scripts and never read the ledger directly — `wax` is the contract.

export const EP_SLUG_RE = /^[a-z][a-z0-9-]*$/u;
/** Audio item ids are 16 hex chars; document ids are the same under `doc-`,
 * optionally binding-scoped (`doc-<16hex>@<binding-id>`). */
export const WAX_KEY_RE = /^(?:doc-)?[0-9a-f]{16}(?:@[a-z0-9-]+)?$/u;
export const PIPELINE_NAME_RE = /^[a-z][a-z0-9-]*$/u;

export type WaxSubcommand =
	| { kind: 'epRun'; slug: string; itemKey: string; attempt?: number }
	| { kind: 'catalog' }
	| { kind: 'pipelineCompile'; names?: string[] }
	| { kind: 'pipelineImport'; file: string; name?: string }
	| { kind: 'pipelineShow'; name: string }
	| { kind: 'documentsScan'; pipeline?: string }
	| { kind: 'documentsRun'; pipeline: string; file: string };

export function buildWaxSubcommandArgs(command: WaxSubcommand): string[] {
	const args = ['--json'];
	switch (command.kind) {
		case 'catalog':
			return [...args, 'catalog'];
		case 'epRun': {
			if (!EP_SLUG_RE.test(command.slug)) {
				throw new WaxCliError(`Invalid pass slug: ${command.slug}`);
			}
			if (!WAX_KEY_RE.test(command.itemKey)) {
				throw new WaxCliError('Item must be an audio item id (16 hex) or document id (doc- + 16 hex)');
			}
			args.push('ep', 'run', command.slug, command.itemKey);
			if (command.attempt !== undefined) {
				if (!Number.isInteger(command.attempt) || command.attempt < 1 || command.attempt > 999) {
					throw new WaxCliError('Attempt must be an integer between 1 and 999');
				}
				args.push('--attempt', String(command.attempt));
			}
			return args;
		}
		case 'pipelineCompile': {
			args.push('pipeline', 'compile');
			for (const name of command.names ?? []) {
				if (!PIPELINE_NAME_RE.test(name)) {
					throw new WaxCliError(`Invalid pipeline name: ${name}`);
				}
				args.push(name);
			}
			return args;
		}
		case 'pipelineImport': {
			if (!isAbsolute(command.file)) {
				throw new WaxCliError('Workflow export must be an absolute path');
			}
			if (command.file.endsWith('.json') === false) {
				throw new WaxCliError('Workflow export must be a .json file');
			}
			args.push('pipeline', 'import', command.file);
			if (command.name !== undefined) {
				if (!PIPELINE_NAME_RE.test(command.name)) {
					throw new WaxCliError(`Invalid pipeline name: ${command.name}`);
				}
				args.push('--name', command.name);
			}
			return args;
		}
		case 'pipelineShow': {
			if (!PIPELINE_NAME_RE.test(command.name)) {
				throw new WaxCliError(`Invalid pipeline name: ${command.name}`);
			}
			return [...args, 'pipeline', 'show', command.name];
		}
		case 'documentsScan': {
			args.push('documents', 'scan');
			if (command.pipeline !== undefined) {
				if (!PIPELINE_NAME_RE.test(command.pipeline)) {
					throw new WaxCliError(`Invalid pipeline name: ${command.pipeline}`);
				}
				args.push(command.pipeline);
			}
			return args;
		}
		case 'documentsRun': {
			if (!PIPELINE_NAME_RE.test(command.pipeline)) {
				throw new WaxCliError(`Invalid pipeline name: ${command.pipeline}`);
			}
			if (!isAbsolute(command.file)) {
				throw new WaxCliError('Document must be an absolute path');
			}
			return [...args, 'documents', 'run', command.pipeline, command.file];
		}
	}
}

export async function runWaxSubcommand(options: {
	command: WaxSubcommand;
	executable: string;
	timeoutMs: number;
	waxRoot: string;
}): Promise<WaxCommandResult> {
	validateConfiguration({
		executable: options.executable,
		operation: 'start',
		timeoutMs: options.timeoutMs,
		waxRoot: options.waxRoot,
	});
	const args = buildWaxSubcommandArgs(options.command);
	try {
		const { stderr, stdout } = await execFileAsync(options.executable, args, {
			encoding: 'utf8',
			env: { ...process.env, WAX_ROOT: options.waxRoot },
			maxBuffer: MAX_OUTPUT_BYTES,
			timeout: options.timeoutMs,
			windowsHide: true,
		});
		return {
			response: parseWaxOutput(stdout),
			stderr: stderr.trim(),
		};
	} catch (error: unknown) {
		if (error instanceof WaxCliError) throw error;

		const fields = errorFields(error);
		const stderr = text(fields.stderr);
		const suffix = stderr || fields.message || String(error);
		const timedOut = fields.killed && fields.signal;
		const message = timedOut
			? `Wax command timed out after ${Math.round(options.timeoutMs / 1000)} seconds: ${suffix}`
			: `Wax command failed${fields.code === undefined ? '' : ` (exit ${fields.code})`}: ${suffix}`;
		throw new WaxCliError(message, fields);
	}
}

export async function runWaxCommand(options: WaxCommandOptions): Promise<WaxCommandResult> {
	const args = buildWaxArgs(options);
	try {
		const { stderr, stdout } = await execFileAsync(options.executable, args, {
			encoding: 'utf8',
			env: { ...process.env, WAX_ROOT: options.waxRoot },
			maxBuffer: MAX_OUTPUT_BYTES,
			timeout: options.timeoutMs,
			windowsHide: true,
		});
		return {
			response: parseWaxOutput(stdout),
			stderr: stderr.trim(),
		};
	} catch (error: unknown) {
		if (error instanceof WaxCliError) throw error;

		const fields = errorFields(error);
		const stderr = text(fields.stderr);
		const suffix = stderr || fields.message || String(error);
		const timedOut = fields.killed && fields.signal;
		const message = timedOut
			? `Wax command timed out after ${Math.round(options.timeoutMs / 1000)} seconds: ${suffix}`
			: `Wax command failed${fields.code === undefined ? '' : ` (exit ${fields.code})`}: ${suffix}`;
		throw new WaxCliError(message, fields);
	}
}
