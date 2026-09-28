import re
import secrets
import string
from django.utils import timezone
from datetime import timedelta
from .models import Voucher, Activity

# ─────────────────────────── Voucher codes ───────────────────────────
# Look-alike characters (0/O, 1/I) are left out whenever letters and digits can
# appear together, so customers never have to guess which one is printed.
ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
LETTERS = 'ABCDEFGHJKLMNPQRSTUVWXYZ'
DIGITS = '0123456789'   # digits-only codes have no letters to confuse, so 0 and 1 are fine
CODE_CHARSETS = {'mixed': ALPHABET, 'letters': LETTERS, 'numbers': DIGITS}
CODE_FORMATS = [('mixed', 'Letters and numbers'), ('numbers', 'Numbers only'), ('letters', 'Letters only')]
CODE_LENGTH_MIN, CODE_LENGTH_MAX, CODE_LENGTH_FALLBACK = 4, 16, 8
CODE_AFFIX_MAX = 6          # longest prefix / suffix
CODE_RANDOM_MIN = 3         # random characters that must remain after prefix + suffix


class CodeFormatError(ValueError):
    """The chosen code format cannot produce the vouchers asked for."""


def clean_affix(value):
    """Prefix/suffix as the customer will type it: letters and numbers only, upper-case."""
    return re.sub(r'[^A-Za-z0-9]', '', str(value or '')).upper()[:CODE_AFFIX_MAX]


def portal_code_length(business):
    """Token size the customer portal expects: the number of letter boxes on the
    default login page's voucher field (the length the router portal shows)."""
    if business is not None:
        from .portal_deploy import default_pages
        page = default_pages(business).get('login')
        for blk in ((page.config or {}).get('blocks') or []) if page else []:
            if blk.get('type') == 'voucher' and not blk.get('hidden'):
                try:
                    n = int(blk.get('length') or CODE_LENGTH_FALLBACK)
                except (TypeError, ValueError):
                    n = CODE_LENGTH_FALLBACK
                return max(CODE_LENGTH_MIN, min(CODE_LENGTH_MAX, n))
    return CODE_LENGTH_FALLBACK


def code_format(length=None, charset='mixed', prefix='', suffix='', business=None):
    """Validate a code format and return it as a dict. `length` is the TOTAL size of the
    code the customer types, prefix and suffix included."""
    prefix, suffix = clean_affix(prefix), clean_affix(suffix)
    charset = charset if charset in CODE_CHARSETS else 'mixed'
    try:
        length = int(length) if length not in (None, '') else portal_code_length(business)
    except (TypeError, ValueError):
        raise CodeFormatError('Token size must be a number.')
    if not CODE_LENGTH_MIN <= length <= CODE_LENGTH_MAX:
        raise CodeFormatError(f'Token size must be between {CODE_LENGTH_MIN} and {CODE_LENGTH_MAX} characters.')
    random_len = length - len(prefix) - len(suffix)
    if random_len < CODE_RANDOM_MIN:
        raise CodeFormatError(f'The prefix and suffix leave only {max(random_len, 0)} random character(s). '
                              f'Keep at least {CODE_RANDOM_MIN}: make the token bigger or the prefix/suffix shorter.')
    return {'length': length, 'charset': charset, 'prefix': prefix, 'suffix': suffix, 'random_len': random_len,
            'combinations': len(CODE_CHARSETS[charset]) ** random_len}


def describe_format(fmt):
    parts = [f'{fmt["length"]}-character', dict(CODE_FORMATS)[fmt['charset']].lower()]
    if fmt['prefix']: parts.append(f'prefix {fmt["prefix"]}')
    if fmt['suffix']: parts.append(f'suffix {fmt["suffix"]}')
    return ', '.join(parts)


def _random_part(alphabet, n, charset):
    while True:
        s = ''.join(secrets.choice(alphabet) for _ in range(n))
        # "Mixed" codes always show both a letter and a number, so they look like the chosen format.
        if charset != 'mixed' or n < 2 or (any(c.isdigit() for c in s) and any(c.isalpha() for c in s)):
            return s


def generate_codes(count, length=None, charset='mixed', prefix='', suffix='', business=None):
    """`count` new unique codes in the given format. Raises CodeFormatError when the
    format has too few free combinations left for the request."""
    fmt = code_format(length, charset, prefix, suffix, business)
    count = max(0, int(count))
    alphabet = CODE_CHARSETS[fmt['charset']]
    # Leave plenty of headroom so short formats do not end in endless retries.
    if count * 3 > fmt['combinations']:
        raise CodeFormatError(f'A {describe_format(fmt)} code only has {fmt["combinations"]:,} possible values — '
                              f'too few for {count} vouchers. Use a bigger token or a wider character set.')
    codes = []
    taken = set()
    for _ in range(40):
        need = count - len(codes)
        if need <= 0:
            break
        fresh, tries = set(), 0
        while len(fresh) < need * 2 and tries < need * 60:
            tries += 1
            c = fmt['prefix'] + _random_part(alphabet, fmt['random_len'], fmt['charset']) + fmt['suffix']
            if c not in taken:
                fresh.add(c)
        # Codes are unique across TapTap and matched case-insensitively at login.
        existing = {x.upper() for x in Voucher.objects.filter(code__in=fresh).values_list('code', flat=True)}
        taken |= existing
        for c in fresh:
            if c not in existing and len(codes) < count:
                codes.append(c); taken.add(c)
    if len(codes) < count:
        raise CodeFormatError(f'Could not find {count} unused {describe_format(fmt)} codes. Use a bigger token size.')
    return codes


def generate_code(length=None, charset='mixed', prefix='', suffix='', business=None):
    """One new unique code. With no arguments it follows the token size of the business's
    default customer portal (8 characters when there is none)."""
    return generate_codes(1, length, charset, prefix, suffix, business)[0]


# ─────────────────────────── Misc helpers ───────────────────────────
def duration_to_routeros(hours):
    d, r = divmod(hours, 24); return (f'{d}d' if d else '') + (f'{r}h' if r else '') or '1h'


def log(business, typ, details, status='Success'):
    from .team import current_actor
    Activity.objects.create(business=business, type=typ, details=details[:255], status=status, actor=current_actor()[:150])


def voucher_profile(voucher, plan=None):
    """(profile name, shared users, rate limit) to use on the router for a voucher.
    Plan vouchers use the plan's profile; custom one-off vouchers share a small set of TapTap profiles
    keyed by devices + speed so the router isn't flooded with one profile per customer."""
    if plan is not None:
        return (plan.mikrotik_profile_name or plan.name), plan.max_devices, plan.speed_limit or ''
    rate = (voucher.rate_limit or '').strip()
    safe = ''.join(ch if ch.isalnum() else '-' for ch in rate).strip('-')
    return f'taptap-{voucher.max_devices}dev' + (f'-{safe}' if safe else ''), voucher.max_devices, rate


def code_format_ctx(business, data=None):
    """Context for templates/core/partials/code_format.html. `data` re-fills the form after an error."""
    from .portal_deploy import default_pages
    portal_len = portal_code_length(business)
    data = data or {}
    charset = data.get('code_charset') if data.get('code_charset') in CODE_CHARSETS else 'mixed'
    return {'portal_len': portal_len, 'has_portal': 'login' in default_pages(business), 'formats': CODE_FORMATS,
            'len_min': CODE_LENGTH_MIN, 'len_max': CODE_LENGTH_MAX, 'affix_max': CODE_AFFIX_MAX, 'random_min': CODE_RANDOM_MIN,
            'form': {'code_length': data.get('code_length') or portal_len, 'code_charset': charset,
                     'code_prefix': clean_affix(data.get('code_prefix')), 'code_suffix': clean_affix(data.get('code_suffix'))}}


def code_format_from_post(post, business):
    """Validated code format from a submitted form (raises CodeFormatError)."""
    return code_format(post.get('code_length') or None, post.get('code_charset', 'mixed'),
                       post.get('code_prefix', ''), post.get('code_suffix', ''), business)
