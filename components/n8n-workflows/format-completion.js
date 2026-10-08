function utf8Bytes(value) {
  let bytes = 0;
  for (const char of value) {
    const code = char.codePointAt(0);
    bytes += code <= 0x7f ? 1 : code <= 0x7ff ? 2 : code <= 0xffff ? 3 : 4;
  }
  return bytes;
}

function formatCompletion(event) {
  if (event?.type !== 'bloodbank.audio.transcription.completed' || event.kind !== 'event' ||
      event.specversion !== '1.0' || typeof event.id !== 'string') {
    throw new Error('Invalid completion envelope');
  }
  const data = event.data;
  if (!data || data.finalized !== true || typeof data.transcription_id !== 'string' ||
      !data.transcription_id || typeof data.file_path !== 'string' || !data.file_path ||
      typeof data.transcript_text !== 'string' || typeof data.plan_id !== 'string' ||
      !data.pass_results || !data.metadata) {
    throw new Error('Incomplete or premature transcription event');
  }
  for (const pass of Object.values(data.pass_results)) {
    if (!['completed', 'skipped'].includes(pass.state)) throw new Error('Unfinished pass');
  }
  const duration = data.duration_seconds;
  if (duration != null && (typeof duration !== 'number' || !Number.isFinite(duration) || duration < 0)) {
    throw new Error('Invalid audio duration');
  }
  const clean = value => typeof value === 'string' ? value.replace(/\s+/g, ' ').trim() : '';
  const title = clean(data.title) || 'Transcription complete';
  const classification = clean(data.classification) || 'unclassified';
  const seconds = duration == null ? 'duration unknown' :
    `${Math.floor(duration / 60)}:${String(Math.floor(duration % 60)).padStart(2, '0')}`;
  const summary = clean(data.summary);
  const message = {topic: 'audio', title: `${title.slice(0, 120)} | ${classification} | ${seconds}`,
                   message: summary.slice(0, 600) || 'No summary supplied.', tags: ['HeyMa', 'Wax']};
  const full = data.transcript_text;
  let warning = null;
  if (data.transcript_inline === false) {
    warning = 'Full transcript is external; native copy unavailable. Use the canonical transcript URI.';
  } else if (utf8Bytes(full) > 256 * 1024 || full.length > 262144) {
    warning = 'Full transcript exceeds clipboard policy; native copy unavailable.';
  } else {
    message.actions = [{action: 'copy', label: 'Copy full transcript', value: full, clear: false}];
  }
  if (warning) message.message += `\n\n${warning}`;
  const encoded = JSON.stringify(message);
  if (utf8Bytes(encoded) >= 2 * 1024 * 1024) throw new Error('Encoded ntfy request exceeds server policy');
  return {event_id: event.id, publish: message, encoded, copy_warning: warning,
          canonical_transcript_uri: data.transcript_uri || null, full_copy_available: !warning};
}

if (typeof module !== 'undefined') module.exports = {formatCompletion, utf8Bytes};
