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

# Letter case — only for "Letters only" codes.
#   upper  CAPITALS (the TapTap default)
#   lower  small letters, without l (looks like I/1) and o (looks like 0)
#   mixed  CAPITALS and small letters together, using only letters whose capital and small
#          forms look different (no Cc Kk Oo Pp Ss Uu Vv Ww Xx Zz, no I/l), so a printed
#          code can be read back without guessing.
LETTERS_LOWER = 'abcdefghijkmnpqrstuvwxyz'
LETTERS_MIXED = 'ABDEFGHJLMNQRTY' + 'abdefghijmnqrty'
LETTER_CASES = [('upper', 'CAPITALS'), ('lower', 'small'), ('mixed', 'MiXeD')]
CASE_ALPHABETS = {'upper': LETTERS, 'lower': LETTERS_LOWER, 'mixed': LETTERS_MIXED}

CODE_LENGTH_MIN, CODE_LENGTH_MAX, CODE_LENGTH_FALLBACK = 4, 16, 8
CODE_AFFIX_MAX = 6          # longest prefix / suffix
CODE_RANDOM_MIN = 3         # random characters that must remain after prefix + suffix


class CodeFormatError(ValueError):
    """The chosen code format cannot produce the vouchers asked for."""


def clean_case(value, charset='letters'):
    """Letter case for a format: only letters-only codes can be small or mixed."""
    return value if charset == 'letters' and value in CASE_ALPHABETS else 'upper'


def clean_affix(value, letter_case='upper'):
    """Prefix/suffix as the customer will type it: letters and numbers only, in the code's case
    (CAPITALS, small, or as typed for mixed-case codes)."""
    v = re.sub(r'[^A-Za-z0-9]', '', str(value or ''))[:CODE_AFFIX_MAX]
    if letter_case == 'lower':
        return v.lower()
    if letter_case == 'mixed':
        return v
    return v.upper()


def alphabet_for(charset, letter_case='upper'):
    if charset == 'letters':
        return CASE_ALPHABETS[clean_case(letter_case, charset)]
    return CODE_CHARSETS[charset]


def distinct_symbols(alphabet):
    """How many characters are really different to TapTap. Codes are unique and matched without
    regard to case, so in a mixed-case alphabet 'A' and 'a' count once."""
    return len({c.upper() for c in alphabet})


def is_case_sensitive(code):
    """True when a code has small letters, so the router (which is case-sensitive) needs it exactly."""
    return any(c.islower() for c in str(code or ''))


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


def portal_is_case_sensitive(business):
    """Does the default customer portal send codes exactly as typed (instead of in CAPITALS)?
    True when there is no portal at all: the router's own page never changes the case."""
    if business is None:
        return True
    from .portal_deploy import default_pages
    page = default_pages(business).get('login')
    if not page:
        return True
    return bool(((page.config or {}).get('settings') or {}).get('case_sensitive'))


def make_portal_case_sensitive(business):
    """Turn on "Codes are case-sensitive" on the business's login pages and send them to the routers
    again. Needed for small/mixed-case codes: otherwise the portal turns 'abcd' into 'ABCD' when
    TapTap cannot be reached and the router refuses it. Returns how many pages were changed."""
    changed = 0
    for page in business.portal_pages.filter(kind='login'):
        cfg = dict(page.config or {})
        settings_ = dict(cfg.get('settings') or {})
        if settings_.get('case_sensitive'):
            continue
        settings_['case_sensitive'] = True
        cfg['settings'] = settings_
        page.config = cfg
        page.save(update_fields=['config', 'updated_at'])
        changed += 1
    if changed:
        from .portal_deploy import schedule_redeploy
        schedule_redeploy(business)
    return changed


def code_format(length=None, charset='mixed', prefix='', suffix='', business=None, letter_case='upper'):
    """Validate a code format and return it as a dict. `length` is the TOTAL size of the
    code the customer types, prefix and suffix included."""
    charset = charset if charset in CODE_CHARSETS else 'mixed'
    letter_case = clean_case(letter_case, charset)
    prefix, suffix = clean_affix(prefix, letter_case), clean_affix(suffix, letter_case)
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
    alphabet = alphabet_for(charset, letter_case)
    return {'length': length, 'charset': charset, 'case': letter_case, 'prefix': prefix, 'suffix': suffix,
            'random_len': random_len, 'combinations': distinct_symbols(alphabet) ** random_len}


def describe_format(fmt):
    label = dict(CODE_FORMATS)[fmt['charset']].lower()
    if fmt['charset'] == 'letters' and fmt.get('case', 'upper') != 'upper':
        label += {'lower': ' (small)', 'mixed': ' (mixed case)'}[fmt['case']]
    parts = [f'{fmt["length"]}-character', label]
    if fmt['prefix']: parts.append(f'prefix {fmt["prefix"]}')
    if fmt['suffix']: parts.append(f'suffix {fmt["suffix"]}')
    return ', '.join(parts)


def _random_part(alphabet, n, charset, letter_case='upper'):
    while True:
        s = ''.join(secrets.choice(alphabet) for _ in range(n))
        if n >= 2 and charset == 'mixed' and not (any(c.isdigit() for c in s) and any(c.isalpha() for c in s)):
            continue   # "Letters and numbers" codes always show both
        if n >= 2 and letter_case == 'mixed' and not (any(c.isupper() for c in s) and any(c.islower() for c in s)):
            continue   # "MiXeD" codes always show both a capital and a small letter
        return s


def _taken_upper(codes):
    """Upper-case forms of `codes` already used by a voucher (bin included) or an old renamed code.
    Compared without regard to case: 'abcd' and 'ABCD' are the same code to TapTap."""
    from django.db.models.functions import Upper
    from .models import VoucherCodeAlias   # old codes of renamed vouchers are never issued again
    ups = list({c.upper() for c in codes})
    out = set()
    for i in range(0, len(ups), 500):
        chunk = ups[i:i + 500]
        out |= set(Voucher.all_objects.annotate(_u=Upper('code')).filter(_u__in=chunk).values_list('_u', flat=True))
        out |= set(VoucherCodeAlias.objects.annotate(_u=Upper('code')).filter(_u__in=chunk).values_list('_u', flat=True))
    return out


def generate_codes(count, length=None, charset='mixed', prefix='', suffix='', business=None, letter_case='upper'):
    """`count` new unique codes in the given format. Raises CodeFormatError when the
    format has too few free combinations left for the request."""
    fmt = code_format(length, charset, prefix, suffix, business, letter_case)
    count = max(0, int(count))
    alphabet = alphabet_for(fmt['charset'], fmt['case'])
    # Leave plenty of headroom so short formats do not end in endless retries.
    if count * 3 > fmt['combinations']:
        raise CodeFormatError(f'A {describe_format(fmt)} code only has {fmt["combinations"]:,} possible values — '
                              f'too few for {count} vouchers. Use a bigger token or a wider character set.')
    codes = []
    taken = set()          # upper-case forms: no two codes in a batch differ only by case
    for _ in range(40):
        need = count - len(codes)
        if need <= 0:
            break
        fresh, tries = {}, 0
        while len(fresh) < need * 2 and tries < need * 60:
            tries += 1
            c = fmt['prefix'] + _random_part(alphabet, fmt['random_len'], fmt['charset'], fmt['case']) + fmt['suffix']
            if c.upper() not in taken and c.upper() not in fresh:
                fresh[c.upper()] = c
        existing = _taken_upper(fresh.values())
        taken |= existing
        for up, c in fresh.items():
            if up not in existing and len(codes) < count:
                codes.append(c); taken.add(up)
    if len(codes) < count:
        raise CodeFormatError(f'Could not find {count} unused {describe_format(fmt)} codes. Use a bigger token size.')
    return codes


def generate_code(length=None, charset='mixed', prefix='', suffix='', business=None, letter_case='upper'):
    """One new unique code. With no arguments it follows the token size of the business's
    default customer portal (8 characters when there is none)."""
    return generate_codes(1, length, charset, prefix, suffix, business, letter_case)[0]


# ─────────────────────────── Misc helpers ───────────────────────────
def duration_to_routeros(hours):
    d, r = divmod(hours, 24); return (f'{d}d' if d else '') + (f'{r}h' if r else '') or '1h'


def log(business, typ, details, status='Success'):
    from .team import current_actor
    Activity.objects.create(business=business, type=typ, details=details[:255], status=status, actor=current_actor()[:150])


def unlimited_profile_name(devices, rate=''):
    safe = ''.join(ch if ch.isalnum() else '-' for ch in (rate or '').strip()).strip('-')
    n = max(1, int(devices or 1))
    return f'taptap-unlimited-{n}-device' + ('s' if n > 1 else '') + (f'-{safe}' if safe else '')


def voucher_profile(voucher, plan=None):
    """(profile name, shared users, rate limit) to use on the router for a voucher or member.

    * Unlimited in TapTap (no duration, no end date — e.g. free unlimited members): a TapTap
      "unlimited" profile with no session timeout and no login script, so nothing on the router
      (a Mikhmon profile's 1-day validity, a session-timeout) can put a time limit back.
    * Plan vouchers use the plan's profile.
    * Custom one-off vouchers share a small set of TapTap profiles keyed by devices + speed so the
      router isn't flooded with one profile per customer.
    """
    if getattr(voucher, 'router_profile', ''):
        # chosen by hand on the voucher page (e.g. to fix "unknown user profile" on a router)
        return voucher.router_profile, max(1, int(voucher.max_devices or 1)), (voucher.rate_limit or '').strip()
    unlimited = not voucher.duration_minutes and not voucher.expires_at
    if unlimited:
        devices = (plan.max_devices if plan is not None else voucher.max_devices) or 1
        rate = ((plan.speed_limit if plan is not None else '') or voucher.rate_limit or '').strip()
        return unlimited_profile_name(devices, rate), devices, rate
    if plan is not None:
        return (plan.mikrotik_profile_name or plan.name), plan.max_devices, plan.speed_limit or ''
    rate = (voucher.rate_limit or '').strip()
    safe = ''.join(ch if ch.isalnum() else '-' for ch in rate).strip('-')
    n = max(1, int(voucher.max_devices or 1))
    # "taptap-1-device", not "taptap-1dev": the old name read like "1 day" on the router
    return f'taptap-{n}-device' + ('s' if n > 1 else '') + (f'-{safe}' if safe else ''), n, rate


def code_format_ctx(business, data=None):
    """Context for templates/core/partials/code_format.html. `data` re-fills the form after an error."""
    from .portal_deploy import default_pages
    portal_len = portal_code_length(business)
    data = data or {}
    charset = data.get('code_charset') if data.get('code_charset') in CODE_CHARSETS else 'mixed'
    letter_case = data.get('code_case') if data.get('code_case') in CASE_ALPHABETS else 'upper'
    return {'portal_len': portal_len, 'has_portal': 'login' in default_pages(business), 'formats': CODE_FORMATS,
            'cases': LETTER_CASES, 'portal_case_sensitive': portal_is_case_sensitive(business),
            'sets': {'mixed': ALPHABET, 'letters': LETTERS, 'numbers': DIGITS, 'lower': LETTERS_LOWER, 'mixedcase': LETTERS_MIXED},
            'len_min': CODE_LENGTH_MIN, 'len_max': CODE_LENGTH_MAX, 'affix_max': CODE_AFFIX_MAX, 'random_min': CODE_RANDOM_MIN,
            'form': {'code_length': data.get('code_length') or portal_len, 'code_charset': charset, 'code_case': letter_case,
                     'code_prefix': clean_affix(data.get('code_prefix'), 'mixed'), 'code_suffix': clean_affix(data.get('code_suffix'), 'mixed')}}


def code_format_from_post(post, business):
    """Validated code format from a submitted form (raises CodeFormatError)."""
    return code_format(post.get('code_length') or None, post.get('code_charset', 'mixed'),
                       post.get('code_prefix', ''), post.get('code_suffix', ''), business,
                       post.get('code_case', 'upper'))


def apply_portal_case(request, business, fmt):
    """After small/mixed-case codes were made: switch the portal to case-sensitive when the person
    ticked the box (the default), else warn. Returns the message to show, or ''."""
    if fmt.get('case', 'upper') == 'upper' or portal_is_case_sensitive(business):
        return ''
    if request.POST.get('portal_case_sensitive'):
        n = make_portal_case_sensitive(business)
        return (f'Your customer portal now sends codes exactly as typed ({n} login page{"s" if n != 1 else ""} updated); '
                'routers that have the portal get the new page within a minute.') if n else ''
    return ('These codes have small letters but your customer portal still turns codes into CAPITALS. '
            'They log in while TapTap is reachable; turn on "Codes are case-sensitive" in Portal Studio so they also work offline.')


# ─────────────────────────── Voucher search ───────────────────────────
def code_search_q(q, field='code', others=(), also_codes=()):
    """Search filter with * wildcards on the voucher code (case-insensitive):
         C*   codes that START with C       *C   codes that END with C
         *C*  codes that CONTAIN C          C*9  start with C and end with 9
    Without a * it is the usual "contains" search over the code and `others`
    (batch name, customer…). Spaces and dashes are ignored, as at login."""
    from django.db.models import Q
    q = (q or '').strip()
    if '*' not in q:
        cond = Q(**{f'{field}__icontains': q})
        for f in (*also_codes, *others):
            cond |= Q(**{f'{f}__icontains': q})
        return cond
    pattern = re.sub(r'[\s-]', '', q)
    pattern = re.sub(r'\*+', '*', pattern)
    if pattern.strip('*') == '':
        return Q()                     # just "*": everything
    regex = '^' + '.*'.join(re.escape(part) for part in pattern.split('*')) + '$'
    cond = Q(**{f'{field}__iregex': regex})
    for f in also_codes:           # e.g. old codes of renamed vouchers
        cond |= Q(**{f'{f}__iregex': regex})
    return cond


def is_wildcard(q):
    return '*' in (q or '')
