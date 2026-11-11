/* AUTO-GENERATED — do not edit. Regenerate with `npm run nodes:passes`. */
import { WaxPassNodeBase } from '../WaxEp/WaxPassNodeBase';

// voice-id — Fingerprint each speaker\'s voice, match the voice registry, name known voices and register new ones 
export class WaxPassVoiceId extends WaxPassNodeBase {
	description = WaxPassNodeBase.describe({
		slug: 'voice-id',
		displayName: 'Wax Pass · Voice Id',
		description: 'Fingerprint each speaker\'s voice, match the voice registry, name known voices and register new ones for you to name.',
	});
}
