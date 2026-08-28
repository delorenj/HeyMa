import type {
	IDataObject,
	IExecuteFunctions,
	INodeExecutionData,
	INodeType,
	INodeTypeDescription,
} from 'n8n-workflow';
import { NodeOperationError } from 'n8n-workflow';

import { type JsonObject, type RecordingOperation, WaxCliError, runWaxCommand } from './waxCli';

const DEFAULT_WAX_EXECUTABLE = '/home/delorenj/HeyMa/bin/wax';
const DEFAULT_WAX_ROOT = '/home/delorenj/HeyMa';
const MAIN_CONNECTION = 'main' as const;

function toDataObject(value: JsonObject): IDataObject {
	return value as IDataObject;
}

function errorOutput(error: unknown): IDataObject {
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

export class HeyMa implements INodeType {
	description: INodeTypeDescription = {
		displayName: 'HeyMa',
		name: 'heyMa',
		icon: { light: 'file:heyma.svg', dark: 'file:heyma.dark.svg' },
		group: ['input'],
		version: 1,
		subtitle: '={{$parameter["operation"]}}',
		description: 'Control the Wax audio recorder',
		defaults: {
			name: 'HeyMa',
		},
		// The literal works with both n8n-workflow 1.x (NodeConnectionType) and
		// 2.x (NodeConnectionTypes); importing either runtime enum breaks the other.
		inputs: [MAIN_CONNECTION],
		outputs: [MAIN_CONNECTION],
		properties: [
			{
				displayName: 'Resource',
				name: 'resource',
				type: 'options',
				noDataExpression: true,
				options: [
					{
						name: 'Recording',
						value: 'recording',
					},
				],
				default: 'recording',
			},
			{
				displayName: 'Operation',
				name: 'operation',
				type: 'options',
				noDataExpression: true,
				displayOptions: {
					show: {
						resource: ['recording'],
					},
				},
				options: [
					{
						name: 'Start Recording',
						value: 'start',
						description: 'Start recording audio with Wax',
						action: 'Start a recording',
					},
					{
						name: 'Stop Recording',
						value: 'stop',
						description: 'Stop and finalize a Wax recording',
						action: 'Stop a recording',
					},
				],
				default: 'start',
			},
			{
				displayName: 'Label',
				name: 'label',
				type: 'string',
				default: '',
				placeholder: 'team-standup',
				description: 'Optional label used in the finalized audio filename',
				displayOptions: {
					show: {
						operation: ['start'],
						resource: ['recording'],
					},
				},
			},
			{
				displayName: 'Bitrate',
				name: 'bitrate',
				type: 'options',
				options: [
					{ name: '16 Kbps', value: '16k' },
					{ name: '24 Kbps', value: '24k' },
					{ name: '32 Kbps', value: '32k' },
					{ name: '48 Kbps', value: '48k' },
					{ name: '64 Kbps', value: '64k' },
				],
				default: '32k',
				description: 'Opus bitrate for the recording',
				displayOptions: {
					show: {
						operation: ['start'],
						resource: ['recording'],
					},
				},
			},
			{
				displayName: 'Capture ID',
				name: 'captureId',
				type: 'string',
				default: '',
				placeholder: '20260821-071500-abcdef',
				description: 'Capture to stop; leave empty to stop the active capture',
				displayOptions: {
					show: {
						operation: ['stop'],
						resource: ['recording'],
					},
				},
			},
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
				typeOptions: {
					minValue: 1,
					maxValue: 120,
				},
				default: 30,
				description: 'Maximum time to wait for Wax to start the recording',
				displayOptions: {
					show: {
						operation: ['start'],
						resource: ['recording'],
					},
				},
			},
			{
				displayName: 'Timeout (Seconds)',
				name: 'timeoutSeconds',
				type: 'number',
				typeOptions: {
					minValue: 60,
					maxValue: 7200,
				},
				default: 3700,
				description: 'Maximum time to wait for Wax to stop and finalize the recording',
				displayOptions: {
					show: {
						operation: ['stop'],
						resource: ['recording'],
					},
				},
			},
			{
				displayName: 'Execute Once',
				name: 'executeOnce',
				type: 'boolean',
				default: true,
				description: 'Whether to issue the lifecycle action once instead of once per input item',
			},
		],
	};

	async execute(this: IExecuteFunctions): Promise<INodeExecutionData[][]> {
		let items = this.getInputData();
		if (items.length === 0) return [[]];

		const executeOnce = this.getNodeParameter('executeOnce', 0) as boolean;
		if (executeOnce) items = [items[0]];

		const returnItems: INodeExecutionData[] = [];
		for (let itemIndex = 0; itemIndex < items.length; itemIndex++) {
			try {
				const operation = this.getNodeParameter('operation', itemIndex) as RecordingOperation;
				const executable = this.getNodeParameter(
					'waxExecutable',
					itemIndex,
					DEFAULT_WAX_EXECUTABLE,
				) as string;
				const waxRoot = this.getNodeParameter('waxRoot', itemIndex, DEFAULT_WAX_ROOT) as string;
				const timeoutSeconds = this.getNodeParameter(
					'timeoutSeconds',
					itemIndex,
					operation === 'start' ? 30 : 3700,
				) as number;
				const label =
					operation === 'start'
						? (this.getNodeParameter('label', itemIndex, '') as string)
						: undefined;
				const bitrate =
					operation === 'start'
						? (this.getNodeParameter('bitrate', itemIndex, '32k') as string)
						: undefined;
				const captureId =
					operation === 'stop'
						? (this.getNodeParameter('captureId', itemIndex, '') as string)
						: undefined;

				const result = await runWaxCommand({
					bitrate,
					captureId,
					executable,
					label,
					operation,
					timeoutMs: timeoutSeconds * 1000,
					waxRoot,
				});
				const json: IDataObject = {
					operation,
					...toDataObject(result.response),
				};
				if (result.stderr) json.stderr = result.stderr;
				returnItems.push({ json, pairedItem: { item: itemIndex } });
			} catch (error: unknown) {
				if (this.continueOnFail()) {
					returnItems.push({ json: errorOutput(error), pairedItem: { item: itemIndex } });
					continue;
				}
				const cause = error instanceof Error ? error : new Error(String(error));
				throw new NodeOperationError(this.getNode(), cause, { itemIndex });
			}
		}

		return [returnItems];
	}
}
