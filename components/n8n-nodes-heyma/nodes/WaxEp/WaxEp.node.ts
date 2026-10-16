import type {
	IExecuteFunctions,
	ILoadOptionsFunctions,
	INodePropertyOptions,
	INodeTypeDescription,
} from 'n8n-workflow';

import { type JsonObject, runWaxSubcommand } from '../HeyMa/waxCli';
import { WaxNodeBase } from './WaxEpBase';

// One node for every enrichment pass: passes register themselves through the
// CLI catalog (loadOptions), so a new passes.d/*.yaml appears in the editor
// without a package change. The node runs one pass for one item via
// `wax ep run` — a CLI-backed manual/preview operation, deliberately not a
// scheduler: automatic execution belongs to the Wax worker, which owns the
// plan, the attempt budget and the dead-letter receipts.
//
// Graph authoring: the node exposes TWO outputs. `main` is a continue edge
// (ordering only); `requirement` is a gated edge — wire it into a downstream
// pass to say "that pass requires this one". `wax pipeline import` maps the
// two kinds onto after/requires edges of the compiled graph.

// The literal works with both n8n-workflow 1.x (NodeConnectionType) and
// 2.x (NodeConnectionTypes); importing either runtime enum breaks the other.
const MAIN_CONNECTION = 'main' as const;
const REQUIREMENT_CONNECTION = 'requirement' as const;

export const WAX_PASS_OUTPUTS = [MAIN_CONNECTION, REQUIREMENT_CONNECTION];
export const WAX_PASS_OUTPUT_NAMES = ['continue', 'requires'];
/** `'requirement'` is a Wax edge kind, not a stock NodeConnectionType member;
 * cast back to the description shape like the connection literals above. */
export const WAX_PASS_TYPED_OUTPUTS = WAX_PASS_OUTPUTS as unknown as INodeTypeDescription['outputs'];

interface CatalogPass {
	slug?: unknown;
	description?: unknown;
}

async function loadPassOptions(this: ILoadOptionsFunctions): Promise<INodePropertyOptions[]> {
	const executable = String(
		this.getCurrentNodeParameter('waxExecutable') ?? '/home/delorenj/HeyMa/bin/wax',
	);
	const waxRoot = String(this.getCurrentNodeParameter('waxRoot') ?? '/home/delorenj/HeyMa');
	const result = await runWaxSubcommand({
		command: { kind: 'catalog' },
		executable,
		timeoutMs: 15_000,
		waxRoot,
	});
	const catalog = result.response as { passes?: { passes?: CatalogPass[] } };
	const rows = Array.isArray(catalog.passes?.passes) ? catalog.passes.passes : [];
	return rows
		.filter((row): row is { slug: string; description?: string } => typeof row.slug === 'string')
		.map((row) => ({
			// Every registered pass is listed — disabled and placeholder passes
			// included — so availability is visible at authoring time; the
			// description carries the purpose.
			name: `${row.slug} — ${String(row.description ?? '').slice(0, 80)}`,
			value: row.slug,
		}));
}

/** An exit-zero response body can still say the pass failed. */
function interpretPassResponse(response: JsonObject) {
	const state = typeof response.state === 'string' ? response.state : '';
	if (state !== 'failed') return undefined;
	return {
		reasonCode: typeof response.reason_code === 'string' ? response.reason_code : undefined,
		detail: typeof response.error === 'string' ? response.error.split('\n')[0] : undefined,
	};
}

export class WaxEp extends WaxNodeBase {
	description: INodeTypeDescription = {
		displayName: 'Wax Enrichment Pass',
		name: 'waxEp',
		icon: { light: 'file:heyma.svg', dark: 'file:heyma.dark.svg' },
		group: ['transform'],
		version: 1,
		subtitle: '={{$parameter["pass"]}}',
		description: 'Run one Wax enrichment pass for one item through the Wax CLI',
		defaults: { name: 'Wax Enrichment Pass' },
		inputs: [MAIN_CONNECTION],
		outputs: WAX_PASS_TYPED_OUTPUTS,
		outputNames: WAX_PASS_OUTPUT_NAMES,
		properties: [
			{
				displayName: 'Pass Name or ID',
				name: 'pass',
				type: 'options',
				typeOptions: { loadOptionsMethod: 'passCatalog' },
				required: true,
				default: '',
				description:
					'Choose from the list, or specify an ID using an <a href="https://docs.n8n.io/code/expressions/">expression</a>',
			},
			{
				displayName: 'Item Or Document ID',
				name: 'itemKey',
				type: 'string',
				required: true,
				default: '',
				placeholder: '0123456789abcdef or doc-0123456789abcdef',
				description: 'Audio item ID (16 hex) or document ID (doc- + 16 hex) to enrich',
			},
			{
				displayName: 'When (Condition, JSON)',
				name: 'when',
				type: 'string',
				typeOptions: { alwaysOpenEditWindow: true },
				default: '',
				placeholder: '[{"field": "classification", "exists": true}]',
				description:
					'Graph authoring only — compiled into the pipeline by "pipeline import"; ignored at execution',
			},
			{
				displayName: 'Skip When (Condition, JSON)',
				name: 'skipWhen',
				type: 'string',
				typeOptions: { alwaysOpenEditWindow: true },
				default: '',
				placeholder: '[{"field": "title", "exists": true}]',
				description:
					'Graph authoring only — compiled into the pipeline by "pipeline import"; ignored at execution',
			},
			{
				displayName: 'Attempt',
				name: 'attempt',
				type: 'number',
				typeOptions: { minValue: 1, maxValue: 999 },
				default: 0,
				description:
					'Force an attempt number (replays a spent command ID); 0 = one past the last recorded',
			},
			...WaxNodeBase.cliProperties(),
		],
	};

	methods = {
		loadOptions: {
			passCatalog: loadPassOptions,
		},
	};

	async execute(this: IExecuteFunctions) {
		return WaxNodeBase.runWaxPerItem(this, (itemIndex) => {
			const slug = String(this.getNodeParameter('pass', itemIndex, ''));
			const itemKey = String(this.getNodeParameter('itemKey', itemIndex, ''));
			const attempt = Number(this.getNodeParameter('attempt', itemIndex, 0) ?? 0);
			return {
				command: {
					kind: 'epRun' as const,
					slug,
					itemKey,
					...(attempt > 0 ? { attempt } : {}),
				},
				interpret: interpretPassResponse,
			};
		});
	}
}
