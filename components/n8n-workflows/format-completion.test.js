const test = require('node:test');
const assert = require('node:assert/strict');
const {formatCompletion, utf8Bytes} = require('./format-completion');

function event(text = '\n# Transcript\nExact café text.\n') {
  return {specversion: '1.0', id: 'event-id', kind: 'event', type: 'bloodbank.audio.transcription.completed',
    data: {finalized: true, plan_id: 'plan', transcription_id: 'item', file_path: '/archive/audio.ogg',
      title: 'A factual title', summary: 'A concise summary.', classification: 'monolog', duration_seconds: 125,
      transcript_text: text, metadata: {}, pass_results: {future: {state: 'completed'}}}};
}

test('final completion contains useful title, summary and unchanged native full copy', () => {
  const input = event();
  const result = formatCompletion(input);
  assert.equal(result.publish.title, 'A factual title | monolog | 2:05');
  assert.equal(result.publish.message, 'A concise summary.');
  assert.deepEqual(result.publish.actions[0], {action: 'copy', label: 'Copy full transcript',
    value: input.data.transcript_text, clear: false});
  assert.equal(JSON.parse(result.encoded).actions[0].value, input.data.transcript_text);
});

test('UTF-8 and UTF-16 boundaries are independently enforced', () => {
  for (const text of ['x'.repeat(262144), 'é'.repeat(131072), '𝄞'.repeat(65536)]) {
    assert.equal(utf8Bytes(text), 262144);
    assert.equal(formatCompletion(event(text)).full_copy_available, true);
    const result = formatCompletion(event(text + 'x'));
    assert.equal(result.full_copy_available, false);
    assert.equal(result.publish.actions, undefined);
    assert.match(result.publish.message, /native copy unavailable/);
  }
});

test('escaped control-character request is measured after JSON encoding', () => {
  const result = formatCompletion(event('\u0001'.repeat(262144)));
  assert.ok(utf8Bytes(result.encoded) < 2 * 1024 * 1024);
  assert.equal(JSON.parse(result.encoded).actions[0].value.length, 262144);
});

test('URI-only transport does not masquerade as an empty full transcript', () => {
  const input = event('');
  input.data.transcript_inline = false;
  input.data.transcript_uri = 'file:///vault/canonical.md';
  const result = formatCompletion(input);
  assert.equal(result.full_copy_available, false);
  assert.match(result.copy_warning, /external/);
});

test('invalid, legacy and unfinished events fail visibly', () => {
  for (const mutate of [e => delete e.data.finalized, e => e.data.pass_results.future.state = 'failed',
                        e => e.data.duration_seconds = -1, e => delete e.data.transcript_text]) {
    const input = event(); mutate(input);
    assert.throws(() => formatCompletion(input));
  }
});
