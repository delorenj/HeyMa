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

type PipelineOperation = 'configure' | 'compile' | 'import' | 'show' | 'scan' | 'runDocument';

/** The editor's Folders collection exports as {folderValues: [{folder}]} —
 * normalize it to the flat path list `wax pipeline import` compiles. */
function normalizeFolders(value: unknown): string[] {
	if (Array.isArray(value)) {
		return value.filter((entry): entry is string => typeof entry === 'string');
	}
	if (typeof value === 'object' && value !== null) {
		const entries = (value as { folderValues?: unknown }).folderValues;
		if (Array.isArray(entries)) {
			return entries
				.map((entry) =>
					typeof entry === 'object' && entry !== null && 'folder' in entry
						? String((entry as { folder: unknown }).folder)
						: undefined,
				)
				.filter((entry): entry is string => entry !== undefined);
		}
	}
	return [];
}

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
		icon: { light: 'file:../HeyMa/heyma.svg', dark: 'file:../HeyMa/heyma.dark.svg' },
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
						name: 'Configure (Authoring)',
						value: 'configure',
						description:
							'Set the pipeline graph parameters this node authors (trigger, folders, failure policy, enabled, description, version) for "pipeline import" to compile',
						action: 'Configure pipeline authoring parameters',
					},
					{
						name: 'Import Workflow',
						value: 'import',
						description:
							'Compile an exported n8n workflow JSON into a versioned pipeline (authoring → Wax)',
						action: 'Import a workflow as a pipeline',
					},
					{
						name: 'Run Document',
						value: 'runDocument',
						description: 'Manually run the compiled pipeline over one Markdown file',
						action: 'Run a pipeline over a document',
					},
					{
						name: 'Scan Documents',
						value: 'scan',
						description: 'Seed new bindings; process new arrivals only (never backfills)',
						action: 'Scan document bindings',
					},
					{
						name: 'Show',
						value: 'show',
						description: 'One pipeline snapshot: trigger, nodes, edges, bindings, drift',
						action: 'Show a pipeline',
					},
				],
				default: 'show',
			},
			{
				displayName: 'Trigger',
				name: 'trigger',
				type: 'options',
				noDataExpression: true,
				required: true,
				options: [
					{ name: 'Documents', value: 'documents', description: 'Folder bindings; the Wax worker scans for new arrivals' },
					{ name: 'Transcription', value: 'transcription', description: 'Post-transcription enrichment for every recording (plan authority when enabled)' },
					{ name: 'Manual', value: 'manual', description: 'Runs only by explicit invocation' },
				],
				default: 'documents',
				description: 'Which Wax execution path owns this graph',
				displayOptions: { show: { operation: ['configure'] } },
			},
			{
				displayName: 'Folders',
				name: 'folders',
				type: 'fixedCollection',
				typeOptions: { multipleValues: true },
				default: {},
				options: [
					{
						name: 'folderValues',
						displayName: 'Folders',
						values: [
							{
								displayName: 'Folder Path',
								name: 'folder',
								type: 'string',
								default: '',
								placeholder: '/home/delorenj/d/Inbox/Wax',
								description: 'Absolute folder path (or ~/…) bound by the documents trigger',
							},
						],
					},
				],
				description: 'Folder bindings for trigger "documents" — first scan seeds, later scans process new arrivals',
				displayOptions: { show: { operation: ['configure'] } },
			},
			{
				displayName: 'Failure Policy',
				name: 'failurePolicy',
				type: 'options',
				noDataExpression: true,
				options: [
					{ name: 'Independent', value: 'independent', description: 'A failed pass never blocks its siblings (the audio default)' },
					{ name: 'Halt', value: 'halt', description: 'The run stops at the first failed pass; the tail stays un-run' },
				],
				default: 'independent',
				description: 'What one failed pass does to the rest of the run',
				displayOptions: { show: { operation: ['configure'] } },
			},
			{
				displayName: 'Enabled',
				name: 'enabled',
				type: 'boolean',
				default: false,
				description:
					'Whether the Wax worker may pick this pipeline up automatically — an explicit scan/run works while disabled',
				displayOptions: { show: { operation: ['configure'] } },
			},
			{
				displayName: 'Description',
				name: 'description',
				type: 'string',
				typeOptions: { rows: 2 },
				default: '',
				placeholder: 'What this graph is for; shown by discovery surfaces',
				description: 'Pipeline description frozen into the compiled definition',
				displayOptions: { show: { operation: ['configure'] } },
			},
			{
				displayName: 'Version',
				name: 'version',
				type: 'number',
				typeOptions: { minValue: 1, maxValue: 9999 },
				default: 1,
				description:
					'Authored version floor — an identical re-import is a no-op and a changed export never downgrades past the stored version',
				displayOptions: { show: { operation: ['configure'] } },
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
				displayName: 'Workflow Export Path',
				name: 'exportPath',
				type: 'string',
				required: true,
				default: '',
				placeholder: '/home/delorenj/HeyMa/components/n8n-nodes-heyma/examples/wax-documents-workflow.json',
				description: 'Absolute path to the exported n8n workflow JSON to compile',
				displayOptions: { show: { operation: ['import'] } },
			},
			{
				displayName: 'Pipeline Name Override',
				name: 'importName',
				type: 'string',
				default: '',
				placeholder: 'leave empty to derive from the workflow name',
				description: 'Pipeline name slug when the workflow name is not one',
				displayOptions: { show: { operation: ['import'] } },
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
		const operation = String(this.getNodeParameter('operation', 0, 'show')) as PipelineOperation;
		if (operation === 'configure') {
			// Pure parameter preview: NO CLI call. The authoring values live on
			// this node's parameters, and `wax pipeline import` reads them from
			// the exported workflow — this branch just shows the human what the
			// export will carry, in the same normalized shape import compiles.
			const foldersRaw = this.getNodeParameter('folders', 0, {});
			const configured = {
				trigger: String(this.getNodeParameter('trigger', 0, 'documents')),
				folders: normalizeFolders(foldersRaw),
				failurePolicy: String(this.getNodeParameter('failurePolicy', 0, 'independent')),
				enabled: Boolean(this.getNodeParameter('enabled', 0, false)),
				description: String(this.getNodeParameter('description', 0, '')),
				version: Number(this.getNodeParameter('version', 0, 1) ?? 1),
				note: 'Authoring configuration — "pipeline import" compiles it; this node never writes',
			};
			return [[{ json: configured as unknown as JsonObject }]];
		}
		return WaxNodeBase.runWaxPerItem(this, (itemIndex) => {
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
				case 'import': {
					const exportPath = String(this.getNodeParameter('exportPath', itemIndex, ''));
					const importName = String(this.getNodeParameter('importName', itemIndex, '')).trim();
					return {
						command: {
							kind: 'pipelineImport' as const,
							file: exportPath,
							...(importName ? { name: importName } : {}),
						},
						// A rejected import (unsupported node, dynamic expression, cycle,
						// missing dependency) is a CLI error (non-zero exit), surfaced by
						// the transport; there is no exit-zero failure shape to interpret.
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
