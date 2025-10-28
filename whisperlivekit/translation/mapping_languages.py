"""
adapted from https://store.crowdin.com/custom-mt
"""

LANGUAGES = [
    {"name": "Afrikaans", "nllb": "afr_Latn", "crowdin": "af"},
    {"name": "Akan", "nllb": "aka_Latn", "crowdin": "ak"},
    {"name": "Amharic", "nllb": "amh_Ethi", "crowdin": "am"},
    {"name": "Assamese", "nllb": "asm_Beng", "crowdin": "as"},
    {"name": "Asturian", "nllb": "ast_Latn", "crowdin": "ast"},
    {"name": "Bashkir", "nllb": "bak_Cyrl", "crowdin": "ba"},
    {"name": "Bambara", "nllb": "bam_Latn", "crowdin": "bm"},
    {"name": "Balinese", "nllb": "ban_Latn", "crowdin": "ban"},
    {"name": "Belarusian", "nllb": "bel_Cyrl", "crowdin": "be"},
    {"name": "Bengali", "nllb": "ben_Beng", "crowdin": "bn"},
    {"name": "Bosnian", "nllb": "bos_Latn", "crowdin": "bs"},
    {"name": "Bulgarian", "nllb": "bul_Cyrl", "crowdin": "bg"},
    {"name": "Catalan", "nllb": "cat_Latn", "crowdin": "ca"},
    {"name": "Cebuano", "nllb": "ceb_Latn", "crowdin": "ceb"},
    {"name": "Czech", "nllb": "ces_Latn", "crowdin": "cs"},
    {"name": "Welsh", "nllb": "cym_Latn", "crowdin": "cy"},
    {"name": "Danish", "nllb": "dan_Latn", "crowdin": "da"},
    {"name": "German", "nllb": "deu_Latn", "crowdin": "de"},
    {"name": "Dzongkha", "nllb": "dzo_Tibt", "crowdin": "dz"},
    {"name": "Greek", "nllb": "ell_Grek", "crowdin": "el"},
    {"name": "English", "nllb": "eng_Latn", "crowdin": "en"},
    {"name": "Esperanto", "nllb": "epo_Latn", "crowdin": "eo"},
    {"name": "Estonian", "nllb": "est_Latn", "crowdin": "et"},
    {"name": "Basque", "nllb": "eus_Latn", "crowdin": "eu"},
    {"name": "Ewe", "nllb": "ewe_Latn", "crowdin": "ee"},
    {"name": "Faroese", "nllb": "fao_Latn", "crowdin": "fo"},
    {"name": "Fijian", "nllb": "fij_Latn", "crowdin": "fj"},
    {"name": "Finnish", "nllb": "fin_Latn", "crowdin": "fi"},
    {"name": "French", "nllb": "fra_Latn", "crowdin": "fr"},
    {"name": "Friulian", "nllb": "fur_Latn", "crowdin": "fur-IT"},
    {"name": "Scottish Gaelic", "nllb": "gla_Latn", "crowdin": "gd"},
    {"name": "Irish", "nllb": "gle_Latn", "crowdin": "ga-IE"},
    {"name": "Galician", "nllb": "glg_Latn", "crowdin": "gl"},
    {"name": "Guarani", "nllb": "grn_Latn", "crowdin": "gn"},
    {"name": "Gujarati", "nllb": "guj_Gujr", "crowdin": "gu-IN"},
    {"name": "Haitian Creole", "nllb": "hat_Latn", "crowdin": "ht"},
    {"name": "Hausa", "nllb": "hau_Latn", "crowdin": "ha"},
    {"name": "Hebrew", "nllb": "heb_Hebr", "crowdin": "he"},
    {"name": "Hindi", "nllb": "hin_Deva", "crowdin": "hi"},
    {"name": "Croatian", "nllb": "hrv_Latn", "crowdin": "hr"},
    {"name": "Hungarian", "nllb": "hun_Latn", "crowdin": "hu"},
    {"name": "Armenian", "nllb": "hye_Armn", "crowdin": "hy-AM"},
    {"name": "Igbo", "nllb": "ibo_Latn", "crowdin": "ig"},
    {"name": "Indonesian", "nllb": "ind_Latn", "crowdin": "id"},
    {"name": "Icelandic", "nllb": "isl_Latn", "crowdin": "is"},
    {"name": "Italian", "nllb": "ita_Latn", "crowdin": "it"},
    {"name": "Javanese", "nllb": "jav_Latn", "crowdin": "jv"},
    {"name": "Japanese", "nllb": "jpn_Jpan", "crowdin": "ja"},
    {"name": "Kabyle", "nllb": "kab_Latn", "crowdin": "kab"},
    {"name": "Kannada", "nllb": "kan_Knda", "crowdin": "kn"},
    {"name": "Georgian", "nllb": "kat_Geor", "crowdin": "ka"},
    {"name": "Kazakh", "nllb": "kaz_Cyrl", "crowdin": "kk"},
    {"name": "Khmer", "nllb": "khm_Khmr", "crowdin": "km"},
    {"name": "Kinyarwanda", "nllb": "kin_Latn", "crowdin": "rw"},
    {"name": "Kyrgyz", "nllb": "kir_Cyrl", "crowdin": "ky"},
    {"name": "Korean", "nllb": "kor_Hang", "crowdin": "ko"},
    {"name": "Lao", "nllb": "lao_Laoo", "crowdin": "lo"},
    {"name": "Ligurian", "nllb": "lij_Latn", "crowdin": "lij"},
    {"name": "Limburgish", "nllb": "lim_Latn", "crowdin": "li"},
    {"name": "Lingala", "nllb": "lin_Latn", "crowdin": "ln"},
    {"name": "Lithuanian", "nllb": "lit_Latn", "crowdin": "lt"},
    {"name": "Luxembourgish", "nllb": "ltz_Latn", "crowdin": "lb"},
    {"name": "Maithili", "nllb": "mai_Deva", "crowdin": "mai"},
    {"name": "Malayalam", "nllb": "mal_Mlym", "crowdin": "ml-IN"},
    {"name": "Marathi", "nllb": "mar_Deva", "crowdin": "mr"},
    {"name": "Macedonian", "nllb": "mkd_Cyrl", "crowdin": "mk"},
    {"name": "Maltese", "nllb": "mlt_Latn", "crowdin": "mt"},
    {"name": "Mossi", "nllb": "mos_Latn", "crowdin": "mos"},
    {"name": "Maori", "nllb": "mri_Latn", "crowdin": "mi"},
    {"name": "Burmese", "nllb": "mya_Mymr", "crowdin": "my"},
    {"name": "Dutch", "nllb": "nld_Latn", "crowdin": "nl"},
    {"name": "Norwegian Nynorsk", "nllb": "nno_Latn", "crowdin": "nn-NO"},
    {"name": "Nepali", "nllb": "npi_Deva", "crowdin": "ne-NP"},
    {"name": "Northern Sotho", "nllb": "nso_Latn", "crowdin": "nso"},
    {"name": "Occitan", "nllb": "oci_Latn", "crowdin": "oc"},
    {"name": "Odia", "nllb": "ory_Orya", "crowdin": "or"},
    {"name": "Papiamento", "nllb": "pap_Latn", "crowdin": "pap"},
    {"name": "Polish", "nllb": "pol_Latn", "crowdin": "pl"},
    {"name": "Portuguese", "nllb": "por_Latn", "crowdin": "pt-PT"},
    {"name": "Dari", "nllb": "prs_Arab", "crowdin": "fa-AF"},
    {"name": "Romanian", "nllb": "ron_Latn", "crowdin": "ro"},
    {"name": "Rundi", "nllb": "run_Latn", "crowdin": "rn"},
    {"name": "Russian", "nllb": "rus_Cyrl", "crowdin": "ru"},
    {"name": "Sango", "nllb": "sag_Latn", "crowdin": "sg"},
    {"name": "Sanskrit", "nllb": "san_Deva", "crowdin": "sa"},
    {"name": "Santali", "nllb": "sat_Olck", "crowdin": "sat"},
    {"name": "Sinhala", "nllb": "sin_Sinh", "crowdin": "si-LK"},
    {"name": "Slovak", "nllb": "slk_Latn", "crowdin": "sk"},
    {"name": "Slovenian", "nllb": "slv_Latn", "crowdin": "sl"},
    {"name": "Shona", "nllb": "sna_Latn", "crowdin": "sn"},
    {"name": "Sindhi", "nllb": "snd_Arab", "crowdin": "sd"},
    {"name": "Somali", "nllb": "som_Latn", "crowdin": "so"},
    {"name": "Southern Sotho", "nllb": "sot_Latn", "crowdin": "st"},
    {"name": "Spanish", "nllb": "spa_Latn", "crowdin": "es-ES"},
    {"name": "Sardinian", "nllb": "srd_Latn", "crowdin": "sc"},
    {"name": "Swati", "nllb": "ssw_Latn", "crowdin": "ss"},
    {"name": "Sundanese", "nllb": "sun_Latn", "crowdin": "su"},
    {"name": "Swedish", "nllb": "swe_Latn", "crowdin": "sv-SE"},
    {"name": "Swahili", "nllb": "swh_Latn", "crowdin": "sw"},
    {"name": "Tamil", "nllb": "tam_Taml", "crowdin": "ta"},
    {"name": "Tatar", "nllb": "tat_Cyrl", "crowdin": "tt-RU"},
    {"name": "Telugu", "nllb": "tel_Telu", "crowdin": "te"},
    {"name": "Tajik", "nllb": "tgk_Cyrl", "crowdin": "tg"},
    {"name": "Tagalog", "nllb": "tgl_Latn", "crowdin": "tl"},
    {"name": "Thai", "nllb": "tha_Thai", "crowdin": "th"},
    {"name": "Tigrinya", "nllb": "tir_Ethi", "crowdin": "ti"},
    {"name": "Tswana", "nllb": "tsn_Latn", "crowdin": "tn"},
    {"name": "Tsonga", "nllb": "tso_Latn", "crowdin": "ts"},
    {"name": "Turkmen", "nllb": "tuk_Latn", "crowdin": "tk"},
    {"name": "Turkish", "nllb": "tur_Latn", "crowdin": "tr"},
    {"name": "Uyghur", "nllb": "uig_Arab", "crowdin": "ug"},
    {"name": "Ukrainian", "nllb": "ukr_Cyrl", "crowdin": "uk"},
    {"name": "Venetian", "nllb": "vec_Latn", "crowdin": "vec"},
    {"name": "Vietnamese", "nllb": "vie_Latn", "crowdin": "vi"},
    {"name": "Wolof", "nllb": "wol_Latn", "crowdin": "wo"},
    {"name": "Xhosa", "nllb": "xho_Latn", "crowdin": "xh"},
    {"name": "Yoruba", "nllb": "yor_Latn", "crowdin": "yo"},
    {"name": "Zulu", "nllb": "zul_Latn", "crowdin": "zu"},
]

NAME_TO_NLLB = {lang["name"]: lang["nllb"] for lang in LANGUAGES}
NAME_TO_CROWDIN = {lang["name"]: lang["crowdin"] for lang in LANGUAGES}
CROWDIN_TO_NLLB = {lang["crowdin"]: lang["nllb"] for lang in LANGUAGES}
NLLB_TO_CROWDIN = {lang["nllb"]: lang["crowdin"] for lang in LANGUAGES}
CROWDIN_TO_NAME = {lang["crowdin"]: lang["name"] for lang in LANGUAGES}
NLLB_TO_NAME = {lang["nllb"]: lang["name"] for lang in LANGUAGES}


def get_nllb_code(crowdin_code):
    return CROWDIN_TO_NLLB.get(crowdin_code, crowdin_code)


def get_crowdin_code(nllb_code):
    return NLLB_TO_CROWDIN.get(nllb_code)


def get_language_name_by_crowdin(crowdin_code):
    return CROWDIN_TO_NAME.get(crowdin_code)


def get_language_name_by_nllb(nllb_code):
    return NLLB_TO_NAME.get(nllb_code)


def get_language_info(identifier, identifier_type="auto"):
    if identifier_type == "auto":
        for lang in LANGUAGES:
            if (lang["name"].lower() == identifier.lower() or 
                lang["nllb"] == identifier or 
                lang["crowdin"] == identifier):
                return lang
    elif identifier_type == "name":
        for lang in LANGUAGES:
            if lang["name"].lower() == identifier.lower():
                return lang
    elif identifier_type == "nllb":
        for lang in LANGUAGES:
            if lang["nllb"] == identifier:
                return lang
    elif identifier_type == "crowdin":
        for lang in LANGUAGES:
            if lang["crowdin"] == identifier:
                return lang
    
    return None


def list_all_languages():
    return [lang["name"] for lang in LANGUAGES]


def list_all_nllb_codes():
    return [lang["nllb"] for lang in LANGUAGES]


def list_all_crowdin_codes():
    return [lang["crowdin"] for lang in LANGUAGES]