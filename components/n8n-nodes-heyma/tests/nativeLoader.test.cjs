const assert = require('node:assert/strict');
const { existsSync, readdirSync, statSync } = require('node:fs');
const { join, resolve } = require('node:path');
const test = require('node:test');

const pkgRoot = resolve(__dirname, '..');
const pkg = require('../package.json');

// Discover the REAL n8n-core loader from the running n8n installation — this
// is the exact code path production hits (vm.Script class instantiation), not
// a mock. No installed n8n is a hard failure on this host, never a skip.
function discoverLoader() {
	const candidates = [];
	if (process.env.N8N_PACKAGE_DIRECTORY_LOADER) {
		candidates.push(process.env.N8N_PACKAGE_DIRECTORY_LOADER);
	}
	try {
		// resolves when n8n-core is reachable from this package (dev setups)
		candidates.push(
			require.resolve('n8n-core/dist/nodes-loader/package-directory-loader.js'),
		);
	} catch {}
	// mise-managed global n8n installs, newest node version first
	const miseInstalls = '/home/delorenj/.local/share/mise/installs/node';
	if (existsSync(miseInstalls)) {
		for (const version of readdirSync(miseInstalls).sort().reverse()) {
			candidates.push(
				join(miseInstalls, version,
					'lib/node_modules/n8n/node_modules/n8n-core/dist/nodes-loader/package-directory-loader.js'),
			);
		}
	}
	for (const candidate of candidates) {
		if (existsSync(candidate)) return candidate;
	}
	throw new Error(
		`n8n-core PackageDirectoryLoader not found (tried: ${candidates.join(', ')}); ` +
		'set N8N_PACKAGE_DIRECTORY_LOADER to the installed loader path',
	);
}

test('the native n8n PackageDirectoryLoader loads the whole built package', async () => {
	const loaderPath = discoverLoader();
	const { PackageDirectoryLoader } = require(loaderPath);
	// vm.Script class instantiation: a manifest basename that is not a valid
	// identifier (the old wax-pass-<slug> layout) failed here with
	// "pass is not defined" and killed the ENTIRE package, heyMa included.
	const loader = new PackageDirectoryLoader(pkgRoot);
	await loader.loadAll();

	const loaded = Object.keys(loader.nodeTypes);
	assert.ok(loaded.includes('heyMa'), 'the legacy Recording node must load');
	assert.ok(loaded.includes('waxEp'), 'the generic enrichment pass node must load');
	assert.ok(loaded.includes('waxPipeline'), 'the pipeline node must load');
	// nodeTypes is keyed by description.name; instantiation itself (the
	// vm.Script `new (require(...).<className>)()` step) already succeeded for
	// every manifest entry or loadAll would have thrown — that is the exact
	// production failure the old kebab-case basenames caused.
	const generated = pkg.n8n.nodes.filter((entry) => entry.includes('/WaxPass'));
	assert.ok(generated.length >= 11, 'the per-pass nodes are in the manifest');
	const passTypes = loaded.filter((name) => /^wax-pass-[a-z][a-z0-9-]*$/u.test(name));
	assert.equal(passTypes.length, generated.length,
		'every per-pass node registered under its stable kebab description name');

	// every manifest entry actually loaded (nothing silently dropped)
	assert.equal(loaded.length, pkg.n8n.nodes.length);

	// icons: `file:` paths resolve relative to each node's own directory and
	// must stay inside the package; verify the resolved file really exists so
	// no node ships a 404 icon.
	for (const wrapped of Object.values(loader.nodeTypes)) {
		const description = wrapped?.type?.description ?? wrapped?.description;
		if (!description) continue;
		const { iconUrl } = description;
		const urls = typeof iconUrl === 'string' ? [iconUrl] : [iconUrl.light, iconUrl.dark];
		for (const url of urls) {
			assert.match(url, /^icons\/[^/]+\//u, `icon resolved to a package URL: ${url}`);
			const packageRelative = url.split('/').slice(2).join('/');
			const onDisk = join(pkgRoot, packageRelative);
			assert.ok(
				existsSync(onDisk) && statSync(onDisk).isFile(),
				`icon file must exist: ${onDisk}`,
			);
		}
	}
});
