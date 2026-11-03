import type { IExecuteFunctions, INodeTypeDescription } from 'n8n-workflow';

import { type JsonValue } from '../HeyMa/waxCli';
import { WaxNodeBase } from '../WaxEp/WaxEpBase';

// The telephony door into Wax. A Telnyx webhook lands in n8n; this node hands
// the event, untouched, to `wax calls event` on stdin. Wax owns the call log
// (`calls` ledger), the voicemail download and every join to audio — the node
// stores nothing, parses nothing and never schedules (`wax calls sync` runs on
// the Candybar clock; "Sync" here is the manual catch-up).

const MAIN_CONNECTION = 'main' as const;

type CallsOperation = 'event' | 'sync' | 'show';

export class WaxCalls extends WaxNodeBase {
	description: INodeTypeDescription = {
		displayName: 'Wax Calls',
		name: 'waxCalls',
		icon: { light: 'file:../HeyMa/heyma.svg', dark: 'file:../HeyMa/heyma.dark.svg' },
		group: ['transform'],
		version: 1,
		subtitle: '={{$parameter["operation"]}}',
		description: 'Feed Telnyx call events into the Wax call log, or reconcile it against the Telnyx API',
		defaults: { name: 'Wax Calls' },
		inputs: [MAIN_CONNECTION],
		outputs: [MAIN_CONNECTION],
		properties: [
			{
				displayName: 'Operation',
				name: 'operation',
				type: 'options',
				noDataExpression: true,
				options: [
					{
						name: 'Ingest Telnyx Event',
						value: 'event',
						description:
							'Store one webhook event (idempotent by event ID) and refresh its call; a voicemail is fetched into the inbox',
						action: 'Ingest a telnyx event',
					},
					{
						name: 'Sync',
						value: 'sync',
						description: 'Reconcile CDRs, recordings and webhook deliveries from the Telnyx API',
						action: 'Sync the call log',
					},
					{
						name: 'Show Call',
						value: 'show',
						description: 'One call with its legs, recordings and events',
						action: 'Show a call',
					},
				],
				default: 'event',
			},
			{
				displayName: 'Event Source',
				name: 'eventSource',
				type: 'options',
				noDataExpression: true,
				options: [
					{
						name: 'Whole Item',
						value: 'item',
						description: 'The Webhook node output as-is ({headers, body, …}); Wax finds the envelope',
					},
					{ name: 'Field', value: 'field', description: 'One field of the item holds the event' },
				],
				default: 'item',
				displayOptions: { show: { operation: ['event'] } },
			},
			{
				displayName: 'Event Field',
				name: 'eventField',
				type: 'string',
				default: 'body',
				description: 'Item field holding the Telnyx event (dot paths allowed)',
				displayOptions: { show: { operation: ['event'], eventSource: ['field'] } },
			},
			{
				displayName: 'Since',
				name: 'since',
				type: 'string',
				default: '2d',
				placeholder: '2d, 6h, 90m or 2026-11-01T00:00:00Z',
				description: 'How far back to reconcile',
				displayOptions: { show: { operation: ['sync'] } },
			},
			{
				displayName: 'Call',
				name: 'callRef',
				type: 'string',
				required: true,
				default: '',
				placeholder: 'Call session ID, call leg ID or Wax item ID',
				displayOptions: { show: { operation: ['show'] } },
			},
			{
				displayName: 'Fetch Voicemail',
				name: 'fetchVoicemail',
				type: 'boolean',
				default: true,
				description: 'Whether a voicemail event downloads its WAV into the Wax inbox (once per event)',
				displayOptions: { show: { operation: ['event', 'sync'] } },
			},
			// One webhook request is one event, so the per-item loop is the default here.
			...(WaxNodeBase.cliProperties() ?? []).map((property) =>
				property.name === 'executeOnce' ? { ...property, default: false } : property,
			),
		],
	};

	async execute(this: IExecuteFunctions) {
		const operation = String(this.getNodeParameter('operation', 0, 'event')) as CallsOperation;
		const items = this.getInputData();
		return WaxNodeBase.runWaxPerItem(this, (itemIndex) => {
			const fetch = Boolean(this.getNodeParameter('fetchVoicemail', itemIndex, true));
			switch (operation) {
				case 'event': {
					const source = String(this.getNodeParameter('eventSource', itemIndex, 'item'));
					let payload: unknown = items[itemIndex]?.json;
					if (source === 'field') {
						const field = String(this.getNodeParameter('eventField', itemIndex, 'body'));
						for (const part of field.split('.').filter(Boolean)) {
							payload =
								typeof payload === 'object' && payload !== null
									? (payload as Record<string, unknown>)[part]
									: undefined;
						}
					}
					return { command: { kind: 'callsEvent' as const, payload: payload as JsonValue, fetch } };
				}
				case 'sync': {
					const since = String(this.getNodeParameter('since', itemIndex, '2d')).trim();
					return { command: { kind: 'callsSync' as const, ...(since ? { since } : {}), fetch } };
				}
				case 'show': {
					const ref = String(this.getNodeParameter('callRef', itemIndex, '')).trim();
					return { command: { kind: 'callsShow' as const, ref } };
				}
			}
		});
	}
}
