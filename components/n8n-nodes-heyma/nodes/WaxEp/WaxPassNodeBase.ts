import type { INodeTypeDescription } from 'n8n-workflow';

import { WaxNodeBase } from './WaxEpBase';
import { WAX_PASS_OUTPUT_NAMES, WAX_PASS_OUTPUTS, WaxEp } from './WaxEp.node';

// The shared base for per-pass node types: every registered enrichment pass
// can be its OWN n8n node ("Wax Pass · Title Slug"), generated from this base
// by `npm run nodes:passes`. A per-pass node is the generic Wax Enrichment
// Pass shell with the pass fixed — same CLI contract, same two stock main
// outputs (0 continue / 1 requires), same "Wax executes, n8n composes"
// boundary. The generic node remains for ad-hoc authoring; these exist so a
// graph reads as itself.

export interface WaxPassSpec {
	/** The registry slug — the one identity Wax knows. */
	slug: string;
	/** Human name shown in the editor palette. */
	displayName: string;
	/** Purpose, from the pass registry description. */
	description: string;
}

export abstract class WaxPassNodeBase extends WaxEp {
	/** Builds the fixed-pass description: `pass` is locked to the slug. */
	static describe(spec: WaxPassSpec): INodeTypeDescription {
		return {
			displayName: spec.displayName,
			// n8n derives the node TYPE from the file name: wax-pass-<slug>
			name: `wax-pass-${spec.slug}`,
			icon: { light: 'file:heyma.svg', dark: 'file:heyma.dark.svg' },
			group: ['transform'],
			version: 1,
			subtitle: spec.slug,
			description: spec.description,
			defaults: { name: spec.displayName },
			inputs: ['main'],
			outputs: WAX_PASS_OUTPUTS as unknown as INodeTypeDescription['outputs'],
			outputNames: [...WAX_PASS_OUTPUT_NAMES],
			properties: [
				// eslint-disable-next-line n8n-nodes-base/node-param-default-missing -- the default IS the generated slug; the rule cannot see dynamic defaults
				{
					displayName: 'Pass',
					name: 'pass',
					type: 'hidden',
					default: spec.slug,
					description: 'Fixed at generation time; per-pass nodes never change pass',
				},
				{
					displayName: 'Item Or Document ID',
					name: 'itemKey',
					type: 'string',
					required: true,
					default: '',
					placeholder: '0123456789abcdef, doc-0123456789abcdef or doc-0123456789abcdef@doc-standard-Wax-a1b2c3d4e5f6',
					description: 'Audio item ID (16 hex), document ID (doc- + 16 hex), or binding-scoped document key (doc-…@binding)',
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
					placeholder: '{"field": "title", "equals": "Skip Me"}',
					description: 'Graph authoring only — compiled into the pipeline by "pipeline import"; ignored at execution. Exactly {field, equals}.',
				},
				...WaxNodeBase.cliProperties(),
			],
		};
	}
}
