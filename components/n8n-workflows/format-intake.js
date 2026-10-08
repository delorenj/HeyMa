// Inlined verbatim into the "Format Intake Item" Code node, so it cannot require()
// format-completion.js; utf8Bytes is duplicated on purpose.
function utf8Bytes(value) {
  let bytes = 0;
  for (const char of value) {
    const code = char.codePointAt(0);
    bytes += code <= 0x7f ? 1 : code <= 0x7ff ? 2 : code <= 0xffff ? 3 : 4;
  }
  return bytes;
}

// Counts code points, not UTF-16 units, so a cut never splits an emoji into a lone surrogate.
function truncate(value, limit) {
  const chars = Array.from(value);
  return chars.length <= limit ? value : `${chars.slice(0, limit - 1).join('').trimEnd()}…`;
}

function firstSentence(text) {
  const match = /^(.+?[.!?])(?=\s|$)|^(.+?[。！？])/u.exec(text);
  return match ? match[1] || match[2] : text;
}

function formatIntake(event) {
  if (event?.type !== 'bloodbank.audio.intake.detected' || event.kind !== 'event' ||
      event.specversion !== '1.0' || typeof event.id !== 'string' || !event.id) {
    throw new Error('Invalid intake envelope');
  }
  const data = event.data;
  const filled = value => typeof value === 'string' && value.trim() !== '';
  if (!data || !filled(data.project_id) || !filled(data.description) || !filled(data.transcript) ||
      !Number.isInteger(data.index) || !Number.isInteger(data.count) ||
      data.index < 1 || data.index > data.count) {
    throw new Error('Incomplete intake event');
  }
  const clean = value => typeof value === 'string' ? value.replace(/\s+/g, ' ').trim() : '';
  const project = clean(data.project_name) || clean(data.project_id);
  const headline = truncate(clean(data.title) || firstSentence(clean(data.description)), 90);
  const message = {topic: 'audio', title: `${project} ${data.index}/${data.count}: ${headline}`,
                   message: `${truncate(data.description.trim(), 600)}\n\nFrom: ${clean(data.transcript)}`,
                   priority: 3, tags: ['HeyMa', 'Wax', 'ticket'],
                   actions: [{action: 'copy', label: 'Copy ticket', value: data.description, clear: false}]};
  const encoded = JSON.stringify(message);
  if (utf8Bytes(encoded) >= 2 * 1024 * 1024) throw new Error('Encoded ntfy request exceeds server policy');
  return {event_id: event.id, intake_id: typeof data.intake_id === 'string' ? data.intake_id : null,
          publish: message, encoded};
}

if (typeof module !== 'undefined') module.exports = {formatIntake};
