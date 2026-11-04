/* AUTO-GENERATED — do not edit. Regenerate with `npm run nodes:passes`. */
import { WaxPassNodeBase } from '../WaxEp/WaxPassNodeBase';

// dictionary-correction — Respell the speaker\'s own words (projects, tools, hosts, domains, people) against the HeyMa custom d
export class WaxPassDictionaryCorrection extends WaxPassNodeBase {
	description = WaxPassNodeBase.describe({
		slug: 'dictionary-correction',
		displayName: 'Wax Pass · Dictionary Correction',
		description: 'Respell the speaker\'s own words (projects, tools, hosts, domains, people) against the HeyMa custom dictionary, with Jev judging each correction in context.',
	});
}
