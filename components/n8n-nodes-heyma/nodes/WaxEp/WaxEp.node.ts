import type {
	IExecuteFunctions,
	ILoadOptionsFunctions,
	INodePropertyOptions,
	INodeProperties,
	INodeTypeDescription,
} from 'n8n-workflow';

import { NodeOperationError } from 'n8n-workflow';

import { type JsonObject, runWaxSubcommand } from '../HeyMa/waxCli';
import { WaxNodeBase } from './WaxEpBase';

// One node for every enrichment pass: passes register themselves through the
// CLI catalog (loadOptions), so a new passes.d/*.yaml appears in the editor
// without a package change. The node has TWO OPERATIONS: "author" (the
// default) is a pure graph-authoring shape — no CLI, no side effects; the
// graph it composes is compiled by `wax pipeline import` into a frozen Wax
// pipeline. "run" is the explicit CLI-backed preview of one pass for one item
// via `wax ep run`, deliberately not a scheduler: automatic execution belongs
// to the Wax worker, which owns the plan, the attempt budget and the
// dead-letter receipts.
//
// Graph authoring: the node exposes TWO STOCK MAIN OUTPUTS. Output 0,
// labeled `continue`, orders (an `after` edge); output 1, labeled `requires`,
// gates (the target `requires` this pass). Both are real
// NodeConnectionType.main — n8n cannot wire or render an invented connection
// type — and `wax pipeline import` maps the source output INDEX (0/1) onto
// after/requires edges of the compiled graph. Manual execution emits results
// on `continue` only, so clicking Execute on a graph cannot double-run a
// downstream node wired to `requires`.

// The literal works with both n8n-workflow 1.x (NodeConnectionType) and
// 2.x (NodeConnectionTypes); importing either runtime enum breaks the other.
const MAIN_CONNECTION = 'main' as const;

/** Stock main outputs: index 0 `continue` (ordering), index 1 `requires` (gating). */
export const WAX_PASS_OUTPUTS = [MAIN_CONNECTION, MAIN_CONNECTION] as const;
export const WAX_PASS_OUTPUT_NAMES = ['continue', 'requires'] as const;

/** Shown when Operation = run. */
const RUN_ONLY = { operation: ['run'] };
/** Shown when Operation = author. */
const AUTHOR_ONLY = { operation: ['author'] };

/**
 * The shared parameter shell for the generic WaxEp node and every generated
 * per-pass node: the operation choice, the run-only transport fields and the
 * author-only condition fields, wrapped around ONE pass-identity property
 * (a loadOptions selector on the generic node, a hidden fixed slug on the
 * per-pass shells). One source of truth so the two surfaces cannot drift.
 */
export function passShellProperties(passProperty: INodeProperties): INodeProperties[] {
	return [
		passProperty,
		{
			displayName: 'Operation',
			name: 'operation',
			type: 'options',
			noDataExpression: true,
			options: [
				{
					name: 'Author (Graph Only)',
					value: 'author',
					description: 'Default. Pure authoring: no execution, no CLI. Export the workflow and compile it with `wax pipeline import` to freeze this node into a Wax pipeline.',
					action: 'Author the pass node graph only',
				},
				{
					name: 'Run (Preview One Item)',
					value: 'run',
					description:
						'Explicit CLI-backed preview: runs `wax ep run` for the item below. Scheduling belongs to the Wax worker, never to n8n.',
					action: 'Run the pass for one item manual preview',
				},
			],
			default: 'author',
			description:
				'Author composes the graph (export → compile); Run previews one item through the Wax CLI',
		},
		{
			displayName: 'Item Or Document ID',
			name: 'itemKey',
			type: 'string',
			required: true,
			displayOptions: { show: RUN_ONLY },
			default: '',
			placeholder: '0123456789abcdef, doc-0123456789abcdef or doc-0123456789abcdef@doc-standard-Wax-a1b2c3d4e5f6',
			description: 'Audio item ID (16 hex), document ID (doc- + 16 hex), or binding-scoped document key (doc-…@binding)',
		},
		{
			displayName: 'When (Condition, JSON)',
			name: 'when',
			type: 'string',
			typeOptions: { alwaysOpenEditWindow: true },
			displayOptions: { show: AUTHOR_ONLY },
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
			displayOptions: { show: AUTHOR_ONLY },
			default: '',
			placeholder: '{"field": "title", "equals": "Skip Me"}',
			description: 'Graph authoring only — compiled into the pipeline by "pipeline import"; ignored at execution. Exactly {field, equals}.',
		},
		{
			displayName: 'Attempt',
			name: 'attempt',
			type: 'number',
			typeOptions: { minValue: 1, maxValue: 999 },
			displayOptions: { show: RUN_ONLY },
			default: 0,
			description:
				'Force an attempt number (replays a spent command ID); 0 = one past the last recorded',
		},
		...WaxNodeBase.cliProperties(),
	];
}

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
		icon: { light: 'file:../HeyMa/heyma.svg', dark: 'file:../HeyMa/heyma.dark.svg' },
		group: ['transform'],
		version: 1,
		subtitle: '={{$parameter["pass"]}}',
		description: 'Author a Wax enrichment pass node, or preview one item through the Wax CLI',
		defaults: { name: 'Wax Enrichment Pass' },
		inputs: [MAIN_CONNECTION],
		outputs: WAX_PASS_OUTPUTS as unknown as INodeTypeDescription['outputs'],
		outputNames: [...WAX_PASS_OUTPUT_NAMES],
		properties: passShellProperties({
			displayName: 'Pass Name or ID',
			name: 'pass',
			type: 'options',
			typeOptions: { loadOptionsMethod: 'passCatalog' },
			required: true,
			default: '',
			description:
				'Choose from the list, or specify an ID using an <a href="https://docs.n8n.io/code/expressions/">expression</a>',
		}),
	};

	methods = {
		loadOptions: {
			passCatalog: loadPassOptions,
		},
	};

	async execute(this: IExecuteFunctions) {
		const operation = String(this.getNodeParameter('operation', 0, 'author'));
		if (operation !== 'run') {
			// Author is pure: no CLI call, no side effects, ever. The node's
			// whole job is to exist in the graph with its pass, its conditions
			// and its two ports; export → `wax pipeline import` compiles that
			// graph into a frozen Wax pipeline. Executing here only proves the
			// shape — which is why authoring graphs show no validation errors.
			const input = this.getInputData(0);
			const slug = String(this.getNodeParameter('pass', 0, ''));
			const note = {
				wax: 'author',
				pass: slug,
				note: 'Authoring node — no execution. Export the workflow and run `wax pipeline import <export.json>` to compile this graph into a frozen Wax pipeline.',
			};
			const output =
				input.length > 0
					? input.map((_item, index) => ({ json: { ...note }, pairedItem: { item: index } }))
					: [{ json: { ...note } }];
			return [output, []];
		}
		const [items] = await WaxNodeBase.runWaxPerItem(this, (itemIndex) => {
			const slug = String(this.getNodeParameter('pass', itemIndex, ''));
			const itemKey = String(this.getNodeParameter('itemKey', itemIndex, ''));
			if (!itemKey) {
				throw new NodeOperationError(
					this.getNode(),
					'Run needs an item or document ID: switch the operation or fill "Item Or Document ID"',
					{ itemIndex, description: 'Authoring graphs do not execute passes.' },
				);
			}
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
		// Two stock main outputs demand a two-array return. The `requires`
		// port is an AUTHORING port: manual/preview execution emits results on
		// `continue` only, so executing a node cannot double-run downstream
		// work wired through the gated port.
		return [items, []];
	}
}
