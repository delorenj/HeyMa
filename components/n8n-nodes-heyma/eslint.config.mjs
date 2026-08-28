import { configWithoutCloudSupport } from '@n8n/node-cli/eslint';

export default [
	...configWithoutCloudSupport,
	{
		files: ['package.json'],
		rules: {
			'n8n-nodes-base/community-package-json-license-not-default': 'off',
		},
	},
	{
		files: ['nodes/HeyMa/HeyMa.node.ts'],
		rules: {
			'@n8n/community-nodes/node-usable-as-tool': 'off',
		},
	},
];
