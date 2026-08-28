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
