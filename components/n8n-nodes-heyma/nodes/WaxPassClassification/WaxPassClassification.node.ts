/* AUTO-GENERATED — do not edit. Regenerate with `npm run nodes:passes`. */
import { WaxPassNodeBase } from '../WaxEp/WaxPassNodeBase';

// classification — Jev classification of a recording as monolog, meeting, phone_call or other (telnyx and floor rules f
export class WaxPassClassification extends WaxPassNodeBase {
	description = WaxPassNodeBase.describe({
		slug: 'classification',
		displayName: 'Wax Pass · Classification',
		description: 'Jev classification of a recording as monolog, meeting, phone_call or other (telnyx and floor rules first).',
	});
}
