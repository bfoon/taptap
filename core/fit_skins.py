"""Fit's wardrobe — skins and masks for the AI technician (drawn by static/js/fit-skins.js).

Each person picks their own look for Fit; the owner (or anyone with Settings) can pick a look for the whole team.
"Auto" follows the calendar: Santa in December, a party hat for New Year, the Gambian flag around Independence
Day (18 February), a lantern and crescent during Ramadan and Eid, the grand boubou for Tobaski, a pumpkin for
Halloween — and the classic robot the rest of the year.

The pictures live in the browser (static/js/fit-skins.js); this file is the list, the names, the greetings and the
calendar, so the website and the app show the same wardrobe.

Stored in FitSettings.looks: {"team": "<skin>", "<user id>": "<skin>"}. A person's "" (or no entry) means "follow
the team's look"; the team's "" means "auto".
"""
from __future__ import annotations

from datetime import date, timedelta

from django.utils import timezone

AUTO = 'auto'
CLASSIC = 'classic'

GROUPS = [
    ('everyday', 'Everyday'),
    ('christmas', 'Christmas & New Year'),
    ('halloween', 'Halloween masks'),
    ('celebrations', 'Eid, Tobaski & The Gambia'),
    ('heroes', 'Heroes & adventures'),
    ('animals', 'Animals'),
]

# id, name, emoji, group, greeting (Fit says it when you put the look on; {name} = the person's first name)
CATALOGUE = [
    ('classic', 'Classic Fit', '🤖', 'everyday', 'Back to my work clothes. What shall we fix, {name}?'),
    ('santa', 'Santa Claus', '🎅', 'christmas', 'Ho ho ho, {name}! Your gift: routers that stay online.'),
    ('snowman', 'Snowman', '⛄', 'christmas', 'Cool as ice and ready to work, {name}.'),
    ('reindeer', 'Reindeer', '🦌', 'christmas', 'My red nose finds offline routers in any weather.'),
    ('party', 'New Year party', '🎉', 'christmas', 'Happy New Year, {name}! May every voucher sell.'),
    ('pumpkin', 'Pumpkin head', '🎃', 'halloween', 'Trick or treat? I only do fixes, {name}.'),
    ('vampire', 'Vampire', '🧛', 'halloween', 'I vant to… check your bandwidth.'),
    ('mummy', 'Mummy', '🧟', 'halloween', 'Wrapped up and still working, {name}.'),
    ('witch', 'Witch hat', '🧙‍♀️', 'halloween', 'One spell for faster Wi-Fi, coming up.'),
    ('skull', 'Skull mask', '💀', 'halloween', 'Spooky mask, friendly technician.'),
    ('eid', 'Ramadan & Eid', '🌙', 'celebrations', 'Eid Mubarak, {name}! Ramadan Kareem to you and your team.'),
    ('tobaski', 'Tobaski boubou', '🐏', 'celebrations', 'Tobaski Mubarak, {name} — Dewenati!'),
    ('gambia', 'Gambia pride', '🇬🇲', 'celebrations', 'Happy Independence Day, The Gambia! Smiling Coast, strong signal.'),
    ('ninja', 'Ninja', '🥷', 'heroes', 'Silent. Fast. I fix routers in the dark, {name}.'),
    ('pirate', 'Pirate', '🏴‍☠️', 'heroes', 'Arr, {name}! No voucher walks the plank on my watch.'),
    ('astronaut', 'Astronaut', '🧑‍🚀', 'heroes', 'Houston, the network is go.'),
    ('superhero', 'Superhero', '🦸', 'heroes', 'Super-Fit to the rescue, {name}!'),
    ('wizard', 'Wizard', '🧙', 'heroes', 'A wise wizard checks the cables first.'),
    ('cat', 'Cat', '🐱', 'animals', 'Purr… your routers are purring too, {name}.'),
    ('lion', 'Lion', '🦁', 'animals', 'King of the network, at your service.'),
    ('panda', 'Panda', '🐼', 'animals', 'Calm panda, calm network.'),
    ('bunny', 'Bunny', '🐰', 'animals', 'Hop hop — fast fixes, {name}.'),
    ('robot_gold', 'Golden Fit', '🏆', 'everyday', 'Shiny! Only the best for {name}.'),
]
IDS = {c[0] for c in CATALOGUE}

# Islamic holidays move every year (dates as expected; they can move a day with the moon sighting).
EID_FITR = [date(2025, 3, 30), date(2026, 3, 20), date(2027, 3, 10), date(2028, 2, 27), date(2029, 2, 15),
            date(2030, 2, 5), date(2031, 1, 25), date(2032, 1, 14)]
EID_ADHA = [date(2025, 6, 6), date(2026, 5, 27), date(2027, 5, 17), date(2028, 5, 6), date(2029, 4, 25),
            date(2030, 4, 14), date(2031, 4, 4), date(2032, 3, 23)]


def season(today=None):
    """The look "Auto" wears today, or classic. The feast days come first, then the national day and the seasons,
    and the month of Ramadan last (it can overlap Independence Day, as in 2027 and 2028)."""
    d = today or timezone.localdate()
    for e in EID_FITR:
        if e - timedelta(days=1) <= d <= e + timedelta(days=3):        # Eid al-Fitr (Korité)
            return 'eid'
    for e in EID_ADHA:
        if e - timedelta(days=3) <= d <= e + timedelta(days=4):        # Tobaski (Eid al-Adha)
            return 'tobaski'
    if d.month == 2 and 14 <= d.day <= 19:                               # Independence Day, 18 February
        return 'gambia'
    if (d.month == 12 and d.day == 31) or (d.month == 1 and d.day <= 2):
        return 'party'
    if d.month == 12:
        return 'santa'
    if (d.month == 10 and d.day >= 24) or (d.month == 11 and d.day == 1):
        return 'pumpkin'
    for e in EID_FITR:
        if e - timedelta(days=30) <= d < e:                             # the month of Ramadan
            return 'eid'
    return CLASSIC


def clean(skin):
    skin = str(skin or '').strip().lower()
    return skin if skin == AUTO or skin in IDS else ''


def _looks(settings):
    v = getattr(settings, 'looks', None)
    return v if isinstance(v, dict) else {}


def chosen(settings, user):
    """(mine, team): what this person picked ('' = follow the team) and the team's look ('' = auto)."""
    looks = _looks(settings)
    return clean(looks.get(str(getattr(user, 'pk', '')))), clean(looks.get('team'))


def resolve(settings, user, today=None):
    """The look Fit wears for this person right now."""
    mine, team = chosen(settings, user)
    pick = mine or team or AUTO
    return season(today) if pick == AUTO else pick


def meta(skin):
    for c in CATALOGUE:
        if c[0] == skin:
            return {'id': c[0], 'name': c[1], 'emoji': c[2], 'group': c[3], 'greeting': c[4]}
    return meta(CLASSIC)


def greeting(skin, name=''):
    return meta(skin)['greeting'].replace('{name}', name or 'friend')


def state(settings, user, can_manage=False, today=None):
    """The wardrobe as fit.js and the app show it."""
    mine, team = chosen(settings, user)
    wearing = resolve(settings, user, today)
    name = getattr(user, 'first_name', '') or getattr(user, 'username', '')
    return {
        'skin': wearing, 'mine': mine, 'team': team, 'season': season(today), 'can_set_team': bool(can_manage),
        'wearing': {**meta(wearing), 'greeting': greeting(wearing, name)},
        'groups': [{'key': k, 'label': lbl} for k, lbl in GROUPS],
        'list': [{'id': c[0], 'name': c[1], 'emoji': c[2], 'group': c[3]} for c in CATALOGUE],
    }


def choose(settings, user, skin, team=False):
    """Save a look. ``skin``: an id, "auto", or "" (mine: follow the team; team: auto). Raises ValueError."""
    raw = str(skin or '').strip().lower()
    value = clean(raw)
    if raw and not value:
        raise ValueError('Fit does not have that look.')
    looks = dict(_looks(settings))
    key = 'team' if team else str(user.pk)
    if value:
        looks[key] = value
    else:
        looks.pop(key, None)
    settings.looks = looks
    settings.save(update_fields=['looks', 'updated_at'])
    return value


def prompt_line(settings, user):
    """One line for Fit's instructions when it is in costume (never changes facts or actions)."""
    skin = resolve(settings, user)
    if skin == CLASSIC:
        return ''
    m = meta(skin)
    return (f'Today you are dressed as “{m["name"]}” {m["emoji"]} (a look the person chose). You may add one short playful line '
            'in that spirit when greeting, never more, and it never changes facts, numbers or what you propose.')
