import type {
	IDataObject,
	IExecuteFunctions,
	INodeExecutionData,
	INodeType,
	INodeTypeDescription,
} from 'n8n-workflow';
import { NodeOperationError } from 'n8n-workflow';

import { type JsonObject, type WaxSubcommand, WaxCliError, runWaxSubcommand } from '../HeyMa/waxCli';

// The shared base of the Wax node family. Every node in this package is a thin,
// validated shell over ONE contract: the absolute Wax CLI. Nodes never execute
// pass scripts, never read passes.d/ or the SQLite ledger, and never schedule —
// n8n composes and previews, Wax owns execution, retries and receipts.

export const DEFAULT_WAX_EXECUTABLE = '/home/delorenj/HeyMa/bin/wax';
export const DEFAULT_WAX_ROOT = '/home/delorenj/HeyMa';

export function waxErrorOutput(error: unknown): IDataObject {
	if (error instanceof WaxCliError) {
		return {
			error: error.message,
			exitCode: error.exitCode ?? '',
			stderr: error.stderr,
			stdout: error.stdout,
		};
	}
	return { error: error instanceof Error ? error.message : String(error) };
}

/** One CLI invocation plus optional inspection of the exit-zero response body. */
export interface WaxCliCall {
	command: WaxSubcommand;
	/**
	 * `wax` returns exit zero for operation-level failures (a failed pass is
	 * JSON with `state: "failed"`, not a non-zero exit), so the node inspects
	 * the body and turns it into a workflow-visible error.
	 */
	interpret?: (response: JsonObject) => { reasonCode?: string; detail?: string } | undefined;
}

export abstract class WaxNodeBase implements INodeType {
	abstract description: INodeTypeDescription;

	/** The transport parameters every Wax node shares. */
	static cliProperties(): INodeTypeDescription['properties'] {
		return [
			{
				displayName: 'Wax Executable',
				name: 'waxExecutable',
				type: 'string',
				default: DEFAULT_WAX_EXECUTABLE,
				required: true,
				description: 'Absolute path to the repo-root Wax shim; PATH lookup is not allowed',
			},
			{
				displayName: 'Wax Root',
				name: 'waxRoot',
				type: 'string',
				default: DEFAULT_WAX_ROOT,
				required: true,
				description: 'Absolute runtime root passed to Wax as WAX_ROOT',
			},
			{
				displayName: 'Timeout (Seconds)',
				name: 'timeoutSeconds',
				type: 'number',
				typeOptions: { minValue: 1, maxValue: 7200 },
				default: 120,
				description: 'Maximum time to wait for the Wax CLI',
			},
			{
				displayName: 'Execute Once',
				name: 'executeOnce',
				type: 'boolean',
				default: true,
				description: 'Whether to issue the CLI call once instead of once per input item',
			},
		];
	}

	/**
	 * The per-item loop every Wax node runs: one CLI call per item (or one total
	 * with executeOnce), continueOnFail honored, CLI failures and exit-zero
	 * operation failures both surfaced as NodeOperationError.
	 */
	static async runWaxPerItem(
		execute: IExecuteFunctions,
		build: (itemIndex: number) => WaxCliCall | Promise<WaxCliCall>,
	): Promise<INodeExecutionData[][]> {
		let items = execute.getInputData();
		if (items.length === 0) return [[]];

		const executeOnce = execute.getNodeParameter('executeOnce', 0) as boolean;
		if (executeOnce) items = [items[0]];

		const returnItems: INodeExecutionData[] = [];
		for (let itemIndex = 0; itemIndex < items.length; itemIndex++) {
			try {
				const call = await build(itemIndex);
				const executable = execute.getNodeParameter(
					'waxExecutable',
					itemIndex,
					DEFAULT_WAX_EXECUTABLE,
				) as string;
				const waxRoot = execute.getNodeParameter('waxRoot', itemIndex, DEFAULT_WAX_ROOT) as string;
				const timeoutSeconds = execute.getNodeParameter('timeoutSeconds', itemIndex, 120) as number;
				const result = await runWaxSubcommand({
					command: call.command,
					executable,
					timeoutMs: timeoutSeconds * 1000,
					waxRoot,
				});
				const failure = call.interpret?.(result.response);
				if (failure) {
					throw new NodeOperationError(
						execute.getNode(),
						`Wax reported failure: ${failure.reasonCode ?? 'unknown'}${failure.detail ? ` — ${failure.detail}` : ''}`,
						{ itemIndex, description: 'The CLI exited zero; the failure is in the response body.' },
					);
				}
				const json: IDataObject = { ...result.response };
				if (result.stderr) json.stderr = result.stderr;
				returnItems.push({ json, pairedItem: { item: itemIndex } });
			} catch (error: unknown) {
				if (execute.continueOnFail()) {
					returnItems.push({ json: waxErrorOutput(error), pairedItem: { item: itemIndex } });
					continue;
				}
				// Already a node error (e.g. an interpret-hook failure); rethrow as-is.
				// eslint-disable-next-line @n8n/community-nodes/require-node-api-error -- deliberate rethrow of an existing NodeOperationError
				if (error instanceof NodeOperationError) throw error;
				const cause = error instanceof Error ? error : new Error(String(error));
				// Static helper: `execute.getNode()` is the node in scope, not `this`.
				throw new NodeOperationError(execute.getNode(), cause, { itemIndex });
			}
		}
		return [returnItems];
	}
}
