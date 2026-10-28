"""
Masks credentials before anything is logged or attached to a report.

    redact('{"apiKey": "user-api-key"}')               -> '{"apiKey": "***"}'
    redact_headers({"Authorization": "Bearer abc"})     -> {"Authorization": "***"}

api.log, the HTML report and the Allure report are CI artifacts (and the Allure report is public on GitHub Pages),
and against staging the keys are real secrets. So every request and response goes through here first.

Two layers, because negative tests send keys in every shape:
  1. known secrets: the configured API keys and every token issued in this run, wherever they appear, even as a
     bare body ("user-api-key") or under a misspelled field. The same idea as ::add-mask:: in GitHub Actions.
  2. secret-looking fields: "apiKey", "api_key", "token", "password"... whatever their value.

It works on the text, not on parsed JSON, so the log still shows exactly what was sent: negative tests send
broken JSON, lists instead of strings, and numbers like 1e309 that json.loads would turn into Infinity.
"""
import re

MASK = "***"
SECRET_FIELDS = ("api_?key", "\\w*token", "password", "authorization", "\\w*secret")   # regex, any case
_known_secrets: set[str] = set()

# "apiKey": <value>, where the value is a string, a list, an object or a bare word/number
SECRET_FIELD = re.compile(
    r'("(?:' + "|".join(SECRET_FIELDS) + r')"\s*:\s*)'
    r'("(?:[^"\\]|\\.)*"|\[[^\]]*\]|\{[^}]*\}|[^,}\]\s]+)',
    re.IGNORECASE)
SECRET_HEADER = re.compile(r"^(?:" + "|".join(SECRET_FIELDS) + r"|x-api-key)$", re.IGNORECASE)
BEARER = re.compile(r"Bearer\s+[^\s\"',}\]]+", re.IGNORECASE)


def add_secret(value):
    """Masks this exact value from now on, wherever it appears (API keys from config, tokens as they're issued)."""
    if value and len(value) >= 4:                    # very short values would mask ordinary text
        _known_secrets.add(value)


def redact(body) -> str:
    """A request or response body (str, bytes or None) with every credential replaced by ***."""
    if body is None:
        return ""
    text = body.decode(errors="replace") if isinstance(body, bytes) else str(body)
    for secret in sorted(_known_secrets, key=len, reverse=True):     # longest first: one key may contain another
        text = text.replace(secret, MASK)
    return BEARER.sub(f"Bearer {MASK}", SECRET_FIELD.sub(rf'\g<1>"{MASK}"', text))


def redact_headers(headers) -> dict:
    return {k: MASK if SECRET_HEADER.match(k) else redact(v) for k, v in dict(headers).items()}
