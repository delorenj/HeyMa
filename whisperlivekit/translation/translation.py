import ctranslate2
import transformers
from dataclasses import dataclass
import huggingface_hub

src_lang = "eng_Latn"

@dataclass
class TranslationModel():
    translator: ctranslate2.Translator
    tokenizer: transformers.AutoTokenizer

def load_model(src_lang):
    huggingface_hub.snapshot_download('entai2965/nllb-200-distilled-600M-ctranslate2',local_dir='nllb-200-distilled-600M-ctranslate2')
    translator = ctranslate2.Translator("nllb-200-distilled-600M-ctranslate2",device="cpu")
    tokenizer = transformers.AutoTokenizer.from_pretrained("nllb-200-distilled-600M-ctranslate2", src_lang=src_lang, clean_up_tokenization_spaces=True)
    return TranslationModel(
        translator=translator,
        tokenizer=tokenizer
        )

def translate(input, translation_model, tgt_lang):
    if not input:
        return ""
    source = translation_model.tokenizer.convert_ids_to_tokens(translation_model.tokenizer.encode(input))
    target_prefix = [tgt_lang]
    results = translation_model.translator.translate_batch([source], target_prefix=[target_prefix])
    target = results[0].hypotheses[0][1:]
    return translation_model.tokenizer.decode(translation_model.tokenizer.convert_tokens_to_ids(target))


if __name__ == '__main__':
    tgt_lang = "fra_Latn"
    src_lang = "eng_Latn"
    translation_model = load_model(src_lang)
    result = translate('Hello world', translation_model=translation_model, tgt_lang=tgt_lang)
    print(result)