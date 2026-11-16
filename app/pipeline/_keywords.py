import re
from collections.abc import Sequence

_MAX_KEYWORDS_PER_FEEDBACK = 3
_MIN_KEYWORD_LENGTH = 4
_TOKEN_SEPARATOR = re.compile(r"[\W_]+")
_STOPWORDS = frozenset(
    {
        "about",
        "after",
        "again",
        "against",
        "also",
        "announces",
        "announced",
        "because",
        "been",
        "before",
        "being",
        "between",
        "could",
        "does",
        "down",
        "during",
        "each",
        "from",
        "have",
        "here",
        "into",
        "introduces",
        "just",
        "launches",
        "launched",
        "like",
        "made",
        "make",
        "more",
        "most",
        "much",
        "only",
        "other",
        "over",
        "releases",
        "released",
        "report",
        "said",
        "says",
        "should",
        "some",
        "still",
        "such",
        "than",
        "that",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "today",
        "under",
        "unveils",
        "using",
        "very",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "will",
        "with",
        "without",
        "would",
        "your",
    }
)


def extract_keywords(title: str, existing: Sequence[str]) -> list[str]:
    """The first few distinctive words of a title that `existing` does not hold yet."""
    known = {keyword.lower() for keyword in existing}
    keywords: list[str] = []

    for token in _TOKEN_SEPARATOR.split(title.lower()):
        if len(token) < _MIN_KEYWORD_LENGTH or token in _STOPWORDS or token in known:
            continue

        known.add(token)
        keywords.append(token)

        if len(keywords) == _MAX_KEYWORDS_PER_FEEDBACK:
            break

    return keywords
