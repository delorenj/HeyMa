import type { INodeProperties, INodeTypeDescription } from 'n8n-workflow';

import { WAX_PASS_OUTPUT_NAMES, WAX_PASS_OUTPUTS, WaxEp, passShellProperties } from './WaxEp.node';

// The shared base for per-pass node types: every registered enrichment pass
// can be its OWN n8n node ("Wax Pass · Title Slug"), generated from this base
// by `npm run nodes:passes`. A per-pass node is the generic Wax Enrichment
// Pass shell with the pass fixed — same operation contract (author by
// default, explicit CLI-backed run), same two stock main outputs (0 continue /
// 1 requires), same "Wax executes, n8n composes" boundary. The generic node
// remains for ad-hoc authoring; these exist so a graph reads as itself.

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
		// eslint-disable-next-line n8n-nodes-base/node-param-default-missing -- the default IS the generated slug; the rule cannot see dynamic defaults
		const passProperty: INodeProperties = {
			displayName: 'Pass',
			name: 'pass',
			type: 'hidden',
			default: spec.slug,
			description: 'Fixed at generation time; per-pass nodes never change pass',
		};
		return {
			displayName: spec.displayName,
			// The node TYPE workflows store; the kebab slug is the stable identity.
			name: `wax-pass-${spec.slug}`,
			icon: { light: 'file:../HeyMa/heyma.svg', dark: 'file:../HeyMa/heyma.dark.svg' },
			group: ['transform'],
			version: 1,
			subtitle: spec.slug,
			description: spec.description,
			defaults: { name: spec.displayName },
			inputs: ['main'],
			outputs: WAX_PASS_OUTPUTS as unknown as INodeTypeDescription['outputs'],
			outputNames: [...WAX_PASS_OUTPUT_NAMES],
			// The one shared parameter shell — operation, run-only transport
			// fields, author-only conditions — wrapped around the fixed pass.
			properties: passShellProperties(passProperty),
		};
	}
}
