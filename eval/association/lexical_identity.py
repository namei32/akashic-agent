"""Fingerprint the production tokenizer without retaining the retired sparse index."""
import hashlib
from importlib.metadata import version
from plugins.akasha.infrastructure.lexical import _TOKENIZER


def lexical_identity():
    result = hashlib.sha256()
    with _TOKENIZER.get_dict_file() as dictionary:
        for block in iter(lambda: dictionary.read(1024 * 1024), b""):
            result.update(block)
    return {"jieba_version": version("jieba"), "jieba_dictionary_sha256": result.hexdigest(),
            "lexical_normalizer_version": "lower-search-v1"}
