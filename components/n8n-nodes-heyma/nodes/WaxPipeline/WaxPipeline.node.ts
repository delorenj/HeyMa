import type { IExecuteFunctions, INodeTypeDescription } from 'n8n-workflow';

import { type JsonObject } from '../HeyMa/waxCli';
import { WaxNodeBase } from '../WaxEp/WaxEpBase';

// The pipeline node encapsulates a whole graph — passes, trigger and folder
// bindings — behind pipeline-level CLI operations. It composes and previews:
// "Compile" adopts an authored graph explicitly (definition identity exposes
// drift until then), "Scan" processes new document arrivals only, and "Run
// Document" is the manual executor. It never schedules and never edits YAML;
// n8n is the authoring surface, the Wax CLI is the compiler, Wax is the
// executor.

// The literal works with both n8n-workflow 1.x (NodeConnectionType) and
// 2.x (NodeConnectionTypes); importing either runtime enum breaks the other.
const MAIN_CONNECTION = 'main' as const;

type PipelineOperation = 'compile' | 'show' | 'scan' | 'runDocument';

/** A scan or run that ended with failed passes is a failure, even at exit zero. */
function interpretDocumentRun(response: JsonObject) {
	const failed = Number(response.failed ?? 0);
	if (Number.isFinite(failed) && failed > 0) {
		const results = Array.isArray(response.results) ? response.results : [];
		const first = results.find(
			(r): r is { ep_slug?: string; reason_code?: string; error?: string } => {
				if (typeof r !== 'object' || r === null) return false;
				return (r as { state?: unknown }).state === 'failed';
			},
		);
		return {
			reasonCode: first?.reason_code ?? 'failed_passes',
			detail: [first?.ep_slug, typeof first?.error === 'string' ? first.error.split('\n')[0] : '']
				.filter(Boolean)
				.join(': '),
		};
	}
	return undefined;
}

export class WaxPipeline extends WaxNodeBase {
	description: INodeTypeDescription = {
		displayName: 'Wax Pipeline',
		name: 'waxPipeline',
		icon: { light: 'file:heyma.svg', dark: 'file:heyma.dark.svg' },
		group: ['transform'],
		version: 1,
		subtitle: '={{$parameter["operation"]}}',
		description:
			'Compile, inspect or run a Wax enrichment pipeline (graph encapsulation over the CLI)',
		defaults: { name: 'Wax Pipeline' },
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
						name: 'Compile',
						value: 'compile',
						description: 'Explicitly validate and adopt authored graphs (exposes drift)',
						action: 'Compile a pipeline',
					},
					{
						name: 'Show',
						value: 'show',
						description: 'One pipeline snapshot: trigger, nodes, edges, bindings, drift',
						action: 'Show a pipeline',
					},
					{
						name: 'Scan Documents',
						value: 'scan',
						description: 'Seed new bindings; process new arrivals only (never backfills)',
						action: 'Scan document bindings',
					},
					{
						name: 'Run Document',
						value: 'runDocument',
						description: 'Manually run the compiled pipeline over one Markdown file',
						action: 'Run a pipeline over a document',
					},
				],
				default: 'show',
			},
			{
				displayName: 'Pipeline',
				name: 'pipeline',
				type: 'string',
				required: true,
				default: '',
				placeholder: 'doc-standard',
				description: 'Pipeline name (lowercase slug) as authored in pipelines.d',
				displayOptions: { show: { operation: ['compile', 'show', 'runDocument'] } },
			},
			{
				displayName: 'Compile All Authored',
				name: 'compileAll',
				type: 'boolean',
				default: false,
				description: 'Whether to compile every authored pipeline instead of the named one',
				displayOptions: { show: { operation: ['compile'] } },
			},
			{
				displayName: 'Document Path',
				name: 'documentPath',
				type: 'string',
				required: true,
				default: '',
				placeholder: '/home/delorenj/d/DocsInbox/note.md',
				description: 'Absolute path of the Markdown document to run the pipeline over',
				displayOptions: { show: { operation: ['runDocument'] } },
			},
			{
				displayName: 'Pipeline (Scan Filter)',
				name: 'scanPipeline',
				type: 'string',
				default: '',
				placeholder: 'leave empty to scan every enabled binding',
				description: 'Limit the scan to one pipeline\'s bindings',
				displayOptions: { show: { operation: ['scan'] } },
			},
			...WaxNodeBase.cliProperties(),
		],
	};

	async execute(this: IExecuteFunctions) {
		return WaxNodeBase.runWaxPerItem(this, (itemIndex) => {
			const operation = String(this.getNodeParameter('operation', itemIndex, 'show')) as PipelineOperation;
			switch (operation) {
				case 'compile': {
					const compileAll = this.getNodeParameter('compileAll', itemIndex, false) as boolean;
					const pipeline = String(this.getNodeParameter('pipeline', itemIndex, ''));
					return {
						command: {
							kind: 'pipelineCompile' as const,
							...(compileAll || !pipeline ? {} : { names: [pipeline] }),
						},
						interpret: (response: JsonObject) => {
							const failed = response.failed as Record<string, string> | undefined;
							if (failed && typeof failed === 'object' && Object.keys(failed).length > 0) {
								const [name, why] = Object.entries(failed)[0];
								return { reasonCode: 'compile_failed', detail: `${name}: ${why}` };
							}
							return undefined;
						},
					};
				}
				case 'show': {
					const pipeline = String(this.getNodeParameter('pipeline', itemIndex, ''));
					return { command: { kind: 'pipelineShow' as const, name: pipeline } };
				}
				case 'scan': {
					const scanPipeline = String(this.getNodeParameter('scanPipeline', itemIndex, '')).trim();
					return {
						command: {
							kind: 'documentsScan' as const,
							...(scanPipeline ? { pipeline: scanPipeline } : {}),
						},
						// bindings[] is a summary; per-document failures surface as
						// non-zero CLI exits already handled by the transport.
					};
				}
				case 'runDocument': {
					const pipeline = String(this.getNodeParameter('pipeline', itemIndex, ''));
					const documentPath = String(this.getNodeParameter('documentPath', itemIndex, ''));
					return {
						command: { kind: 'documentsRun' as const, pipeline, file: documentPath },
						interpret: interpretDocumentRun,
					};
				}
			}
		});
	}
}
