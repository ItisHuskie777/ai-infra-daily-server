import re

from app.models import Language

# House style for every headline and summary in the digest, drawn from the owner's
# humanizer guide: state the fact plainly, name who did it, and leave out the habits
# that make text read as generated.
RULES_EN = """Writing style:
- Headline: one plain sentence in sentence case (capitalise only the first word and
  proper names) that says what happened. Do not copy a post title that is a question,
  an opinion or a joke; say what the post reports instead. No "Show HN", "[Paper]" or
  "Hot take" prefixes, no title-case words, no full stop at the end.
- Summary: start with who did what, naming the person, company or project. Do not
  open with "A Reddit user", "A Hacker News thread", "Researchers" or "A developer"
  when a name is available; when it is not, start with the finding. Add facts the
  headline does not already give (numbers, what changed, who it affects). If the
  sources hold nothing beyond the headline, write one short sentence.
- Describe the news, not the article: never write "the post discusses", "the report
  highlights", "the article argues" or "according to the sources".
- No dashes of any kind; use a comma, colon, full stop or parentheses. Straight
  quotes only.
- No contrasts of the form "not X but Y", no closing line that restates the point,
  no lists of three added for rhythm, no "-ing" phrases that claim significance
  ("highlighting", "underscoring", "showcasing", "marking").
- Plain verbs: "is", "has", "adds". Avoid: crucial, pivotal, key, landscape, robust,
  significant, notable, seamless, enhance, leverage, delve, showcase, underscore,
  groundbreaking, game-changing, comprehensive.
- Report claims at the strength the sources give: "says", "reports", "claims"."""

RULES_ZH = """写作要求：
- 标题：一句话直接写发生了什么。帖子标题是提问、观点或玩笑时，
  不要照抄，改写成它报道的事实。
  不要加"突发""重磅""炸裂"之类的词，不要用感叹号，结尾不加句号。
- 摘要：先写谁做了什么，写出具体的公司、人名或项目和数字。不要重复标题已经说过的内容；
  资料里没有更多信息时，只写一句短话。
- 写新闻本身，不要写文章：不要写"该文讨论了""相关报道以……为题""文章指出"。
- 不用破折号，不用"不是……而是……"的对比，不用"值得注意的是""具有重要意义"这类套话。"""

_DASH = "[–—]+"
_PAIRED_DASHES = re.compile(rf"\s*{_DASH}\s*([^–—]{{1,80}}?)\s*{_DASH}\s*")
_SPACED_DASH = re.compile(rf"\s*{_DASH}\s*|\s+-{{1,2}}\s+")
_RANGE_DASH = re.compile(r"(?<=\w)[–—](?=\w)")
_CURLY = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})
_ARTICLE_BEFORE_SUBREDDIT = re.compile(r"\b([Aa]) (?=r/)")
_SPACES = re.compile(r"[ \t]+")
_ZH_COMMA = "，"
_ZH_FULL_STOP = "。"


def tidy(text: str, language: Language) -> str:
    """Mechanical fixes the model does not apply reliably: a dash between two numbers
    becomes a hyphen, a pair of dashes around an aside becomes parentheses, any other
    dash becomes a comma, and English text gets straight quotes."""
    comma = _ZH_COMMA if language is Language.ZH else ", "
    cleaned = _RANGE_DASH.sub("-", text.strip())

    if language is not Language.ZH:
        cleaned = _PAIRED_DASHES.sub(r" (\1) ", cleaned)
        cleaned = cleaned.translate(_CURLY)
        cleaned = _ARTICLE_BEFORE_SUBREDDIT.sub(lambda match: f"{match[1]}n ", cleaned)

    cleaned = _SPACED_DASH.sub(comma, cleaned)

    return _SPACES.sub(" ", cleaned).replace(" ,", ",").strip(" ," + _ZH_COMMA)


def tidy_headline(title: str, language: Language) -> str:
    return tidy(title, language).rstrip("." + _ZH_FULL_STOP)
