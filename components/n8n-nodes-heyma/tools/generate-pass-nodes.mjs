#!/usr/bin/env node
// Generates one n8n node per registered Wax enrichment pass — a per-pass
// node type derived from the shared WaxPassNodeBase ("Wax Pass · Title Slug")
// — and keeps package.json's n8n.nodes list in sync. Deliberately explicit:
// run `npm run nodes:passes` after the pass registry changes and commit the
// result, so the node manifest is always reviewable and travel-safe.

import { execFileSync } from 'node:child_process';
import { mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
// WAX_NODES_ROOT lets tests run the generator against a throwaway package
// copy instead of rewriting this one's committed nodes and manifest.
const pkgRoot = process.env.WAX_NODES_ROOT
	? resolve(isAbsolute(process.env.WAX_NODES_ROOT)
		? process.env.WAX_NODES_ROOT
		: join(process.cwd(), process.env.WAX_NODES_ROOT))
	: join(here, '..');
const pkgPath = join(pkgRoot, 'package.json');

const executable = process.env.WAX_EXECUTABLE ?? '/home/delorenj/HeyMa/bin/wax';
const waxRoot = process.env.WAX_ROOT ?? '/home/delorenj/HeyMa';

const BANNER = `/* AUTO-GENERATED — do not edit. Regenerate with \`npm run nodes:passes\`. */`;

function pascal(slug) {
	return slug
		.split('-')
		.map((part) => part.charAt(0).toUpperCase() + part.slice(1))
		.join('');
}

function titleCase(slug) {
	return slug
		.split('-')
		.map((part) => part.charAt(0).toUpperCase() + part.slice(1))
		.join(' ');
}

function loadCatalog() {
	const stdout = execFileSync(executable, ['--json', 'catalog'], {
		encoding: 'utf8',
		env: { ...process.env, WAX_ROOT: waxRoot },
		maxBuffer: 1024 * 1024,
		timeout: 30_000,
	});
	const parsed = JSON.parse(stdout);
	const rows = parsed?.passes?.passes;
	if (!Array.isArray(rows)) {
		throw new Error('catalog envelope missing passes.passes');
	}
	return rows.filter((row) => typeof row?.slug === 'string');
}

function render(row) {
	const slug = row.slug;
	const className = `WaxPass${pascal(slug)}`;
	// Escape BEFORE slicing for the string literal, and slice BEFORE escaping
	// for the comment — a description with real newlines must never break the
	// generated file's syntax (a multi-line // comment is code, not comment).
	const escape = (text) =>
		text
			.replace(/\\/g, '\\\\')
			.replace(/'/g, "\\'")
			.replace(/\r/g, '\\r')
			.replace(/\n/g, '\\n');
	// Slice the RAW text, then escape: slicing escaped text can cut an escape
	// sequence in half and break the generated literal's syntax.
	const description = escape(String(row.description ?? '').slice(0, 200));
	const commentText = escape(String(row.description ?? '').slice(0, 100));
	return `${BANNER}
import { WaxPassNodeBase } from '../WaxEp/WaxPassNodeBase';

// ${slug} — ${commentText}
export class ${className} extends WaxPassNodeBase {
	description = WaxPassNodeBase.describe({
		slug: '${slug}',
		displayName: 'Wax Pass \u00b7 ${titleCase(slug)}',
		description: '${description}',
	});
}
`;
}

function main() {
	const rows = loadCatalog();
	if (rows.length === 0) throw new Error('catalog returned no passes');

	const wanted = new Map();
	for (const row of rows) {
		const fileName = `wax-pass-${row.slug}.node.ts`;
		// n8n lint convention: a node lives in a directory named after it.
		const nodeDir = join(pkgRoot, 'nodes', `wax-pass-${row.slug}`);
		mkdirSync(nodeDir, { recursive: true });
		wanted.set(row.slug, `wax-pass-${row.slug}`);
		const target = join(nodeDir, fileName);
		const body = render(row);
		let unchanged = false;
		try {
			unchanged = readFileSync(target, 'utf8') === body;
		} catch {}
		if (!unchanged) writeFileSync(target, body);
	}

	// Remove stale generated nodes whose pass is no longer registered.
	const nodesRoot = join(pkgRoot, 'nodes');
	for (const entry of readdirSync(nodesRoot, { withFileTypes: true })) {
		if (!entry.isDirectory() || !entry.name.startsWith('wax-pass-')) continue;
		if (![...wanted.values()].includes(entry.name)) {
			rmSync(join(nodesRoot, entry.name), { recursive: true, force: true });
		}
	}
	// The old flat layout (nodes/passes/) is gone too.
	rmSync(join(nodesRoot, 'passes'), { recursive: true, force: true });

	const pkg = JSON.parse(readFileSync(pkgPath, 'utf8'));
	const staticNodes = [
		'dist/nodes/HeyMa/HeyMa.node.js',
		'dist/nodes/WaxEp/WaxEp.node.js',
		'dist/nodes/WaxPipeline/WaxPipeline.node.js',
	];
	const passNodes = [...wanted.keys()]
		.sort()
		.map((slug) => `dist/nodes/wax-pass-${slug}/wax-pass-${slug}.node.js`);
	const nodes = [...staticNodes, ...passNodes];
	if (JSON.stringify(pkg.n8n?.nodes) !== JSON.stringify(nodes)) {
		pkg.n8n.nodes = nodes;
		writeFileSync(pkgPath, `${JSON.stringify(pkg, null, '\t')}\n`);
	}

	console.log(
		`generated ${wanted.size} per-pass node(s): ${[...wanted.keys()].sort().join(', ')}`,
	);
}

main();
