const test = require('node:test');
const assert = require('node:assert/strict');
const {formatIntake} = require('./format-intake');

function event(description = 'Add the ticketable pass. It must run after title-slug.') {
  return {specversion: '1.0', id: 'event-id', kind: 'event', type: 'bloodbank.audio.intake.detected',
    data: {project_id: 'transcription-queue', project_name: 'HeyMa', description,
      transcript: '20261008-101411-ticketable-intake-events.md', transcription_id: 'item',
      intake_id: 'item:0123456789ab', index: 2, count: 7, project: 'wax'}};
}

const loneSurrogate = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;

test('intake becomes one scannable audio push with the full description in a copy action', () => {
  const input = event();
  const result = formatIntake(input);
  assert.equal(result.event_id, 'event-id');
  assert.equal(result.intake_id, 'item:0123456789ab');
  assert.deepEqual(result.publish, {topic: 'audio', title: 'HeyMa 2/7: Add the ticketable pass.',
    message: `${input.data.description}\n\nFrom: 20261008-101411-ticketable-intake-events.md`,
    priority: 3, tags: ['HeyMa', 'Wax', 'ticket'],
    actions: [{action: 'copy', label: 'Copy ticket', value: input.data.description, clear: false}]});
  assert.deepEqual(JSON.parse(result.encoded), result.publish);
});

test('title falls back to the project id and prefers an explicit ticket title', () => {
  const input = event();
  delete input.data.project_name;
  assert.equal(formatIntake(input).publish.title, 'transcription-queue 2/7: Add the ticketable pass.');
  input.data.title = '  Ticketable\npass  ';
  assert.equal(formatIntake(input).publish.title, 'transcription-queue 2/7: Ticketable pass');
});

test('malformed or incomplete envelopes fail visibly', () => {
  for (const mutate of [e => delete e.data.project_id, e => e.data.description = '   ',
                        e => delete e.data.transcript, e => e.data.index = 0, e => e.data.index = 8,
                        e => e.data.count = 7.5, e => e.data.index = '2', e => delete e.data,
                        e => e.type = 'bloodbank.audio.transcription.completed', e => e.kind = 'command',
                        e => e.specversion = '0.3', e => delete e.id]) {
    const input = event(); mutate(input);
    assert.throws(() => formatIntake(input));
  }
  assert.throws(() => formatIntake(null));
});

test('long descriptions truncate the push but never the copied ticket', () => {
  const description = `${'Rework the release script so it '.repeat(5)}looks nodes up by id. ${'x'.repeat(1200)}`;
  const result = formatIntake(event(description));
  const headline = result.publish.title.slice('HeyMa 2/7: '.length);
  assert.equal(Array.from(headline).length, 90);
  assert.ok(headline.endsWith('…'));
  const body = result.publish.message.split('\n\nFrom: ')[0];
  assert.equal(Array.from(body).length, 600);
  assert.ok(body.endsWith('…'));
  assert.equal(result.publish.actions[0].value, description);
});

test('unicode is cut on code points and survives JSON encoding', () => {
  const description = `${'🎙️ Prüfe die Übertragung, café. '.repeat(40)}`;
  const result = formatIntake(event(description));
  for (const text of [result.publish.title, result.publish.message]) assert.doesNotMatch(text, loneSurrogate);
  assert.equal(result.publish.title, 'HeyMa 2/7: 🎙️ Prüfe die Übertragung, café.');
  assert.equal(JSON.parse(result.encoded).actions[0].value, description);
  const cjk = formatIntake(event('文字起こしからチケットを作成する。次にプロジェクトを割り当てる。'));
  assert.equal(cjk.publish.title, 'HeyMa 2/7: 文字起こしからチケットを作成する。');
  const astral = formatIntake(event('𝄞'.repeat(700)));
  assert.doesNotMatch(astral.publish.message, loneSurrogate);
  assert.equal(Array.from(astral.publish.message.split('\n\n')[0]).length, 600);
});
