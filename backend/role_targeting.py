"""Conservative experience signals shared by discovery and the shortlist."""
import re
from datetime import date


def required_years(text):
    """Extract numeric experience mentions; they remain review signals."""
    return max((int(match.group(1)) for match in re.finditer(
        r'\b(\d{1,2})(?:\s*[-–]\s*\d{1,2})?\s*\+?\s+years?\s+(?:of\s+)?'
        r'(?:[\w/-]+\s+){0,5}experience\b', text, re.I)), default=None)


def level_penalty(title, description=''):
    return int(bool(re.search(r'\b(staff|principal|distinguished)\b', title, re.I))
               or (required_years(description) or 0) >= 8)


def target_role(title):
    return bool(re.search(
        r'\b(software engineer|platform|automation|sdet|forward deployed|fde|infrastructure)\b',
        title, re.I))


def verified_years(profile):
    """Union employment intervals so overlapping jobs never double-count."""
    today = date.today()
    intervals = sorted((entry.start_date, min(entry.end_date or today, today))
                       for entry in profile.experience if entry.start_date <= today)
    days = 0
    end = None
    for start, finish in intervals:
        start = max(start, end) if end else start
        days += max(0, (finish - start).days)
        end = max(end, finish) if end else finish
    return round(days / 365.25, 1)
