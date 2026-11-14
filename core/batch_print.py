"""Print what is left of a batch (Batches › Print what's left).

A batch is printed once and then sells for weeks. Reprinting it used to mean the whole batch again — sold codes
included. The print page now knows, for every voucher of the batch, where it stands:

    left   in stock: active, not sold, not used            → the ones worth printing
    sold   sold (or handed out) but not used yet
    used   somebody has signed in with it
    off    switched off or expired before it was used

The page gets all vouchers of the batch with their state and filters in the browser, so tapping a group in the
stock ribbon redraws the sheets at once. ``?show=left`` (or ``left,sold`` …) chooses what is ticked on arrival;
without it the whole batch is ticked, exactly as before.
"""
from django.utils import timezone

STATES = ('left', 'sold', 'used', 'off')
LABELS = {'left': 'Left to sell', 'sold': 'Sold, not used yet', 'used': 'Used', 'off': 'Switched off / expired'}
BATCH_LIMIT = 2000


def state_of(status, sold_at, used_at):
    if used_at:
        return 'used'
    if status != 'active':
        return 'off'
    return 'sold' if sold_at else 'left'


def parse_show(text):
    """'left,sold' → ['left', 'sold']; anything unknown or empty → every state (the whole batch)."""
    want = [s for s in dict.fromkeys(str(text or '').lower().replace(' ', '').split(',')) if s in STATES]
    return want or list(STATES)


def stock(batch, qs, show=''):
    """(states by voucher code, what the ribbon needs)."""
    states = {code: state_of(status, sold, used) for code, status, sold, used in qs.values_list('code', 'status', 'sold_at', 'used_at')}
    counts = {s: 0 for s in STATES}
    for s in states.values():
        counts[s] += 1
    holder = batch.agent.name if batch.agent_id and batch.agent else ''
    return states, {
        'batch': batch.name, 'holder': holder, 'total': len(states), 'counts': counts, 'labels': LABELS,
        'show': parse_show(show), 'printed': timezone.localtime().strftime('%d %b %Y %H:%M'),
    }
