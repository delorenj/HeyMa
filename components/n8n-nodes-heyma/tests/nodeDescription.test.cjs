const assert = require('node:assert/strict');
const test = require('node:test');

const { HeyMa } = require('../dist/nodes/HeyMa/HeyMa.node.js');

test('exports an n8n node with operation-aware recording fields', () => {
	const node = new HeyMa();
	assert.equal(node.description.name, 'heyMa');
	assert.equal(node.description.usableAsTool, undefined);
	assert.deepEqual(node.description.inputs, ['main']);
	assert.deepEqual(node.description.outputs, ['main']);

	const operation = node.description.properties.find((property) => property.name === 'operation');
	assert.deepEqual(
		operation.options.map((option) => option.value),
		['start', 'stop'],
	);

	const label = node.description.properties.find((property) => property.name === 'label');
	const captureId = node.description.properties.find((property) => property.name === 'captureId');
	assert.deepEqual(label.displayOptions.show.operation, ['start']);
	assert.deepEqual(captureId.displayOptions.show.operation, ['stop']);

	const timeouts = node.description.properties.filter(
		(property) => property.name === 'timeoutSeconds',
	);
	assert.deepEqual(
		timeouts.map((property) => [property.displayOptions.show.operation[0], property.default]),
		[
			['start', 30],
			['stop', 3700],
		],
	);
});
