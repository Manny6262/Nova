"""Voice-only routing. Intent selects a route, never grants a capability."""
import re


def needs_desktop(text):
    """Keep everyday conversation on its selected brain, even with control armed.

    This conservative deterministic router isn't an authorization check. Ambiguous
    requests remain on the cloud coding agent; permissions still apply in app.py.
    """
    text = str(text or '').lower()
    return bool(re.search(
        r'\b(click|scroll|drag|double.click|mouse|keyboard|desktop|screen|'
        r'open|launch|browse|navigate|youtube|browser|website|press|type|paste)\b', text))


def compact_history(history, budget=7000):
    """Bound voice prompt size, retaining the newest turns, without changing DB."""
    result = []
    for message in reversed(history):
        if budget <= 0:
            break
        text = str(message.get('content') or '')
        clipped = text[-min(2500, budget):]
        result.append(dict(message, content=clipped))
        budget -= len(clipped)
    return list(reversed(result))
