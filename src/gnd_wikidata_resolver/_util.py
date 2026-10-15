import re

USER_AGENT = "my-project/1.0 (https://example.org; me@example.org)"

def year(s: str | None) -> str | None:
    """Extract a four-digit year from a date string, e.g. '1766-01-01' -> '1766'."""
    m = re.search(r"\d{4}", s or "")
    return m.group(0) if m else None


def set_user_agent(value): 
    global USER_AGENT
    USER_AGENT = value