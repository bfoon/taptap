"""TapTap subscription prices — the same packages everywhere, priced per country.

Every package (1 month … 1 year) has a DEFAULT price in Dalasi (the Gambia price list).
The platform can give any country its own price and currency for each package
(CountryPrice). A business pays the price of the country it chose at sign-up.

A country with no prices of its own uses the whole default list. Once a country has its own
prices, only those packages are offered there — so one country never mixes two currencies.

    price_for(business, '3m')  →  {'code': '3m', 'name': '3 Months', 'days': 90,
                                   'amount': Decimal('15000'), 'currency': 'XOF', 'symbol': 'CFA ',
                                   'local': True, 'per_month': …, 'saving': 5, 'offered': True}
"""
from collections import OrderedDict
from decimal import Decimal, ROUND_HALF_UP

from . import countries

DEFAULT_CURRENCY = 'GMD'

# code → (name, default price in GMD, days)
PACKAGES = OrderedDict([
    ('1m', ('1 Month', Decimal('700'), 30)),
    ('2m', ('2 Months', Decimal('1350'), 60)),
    ('3m', ('3 Months', Decimal('2000'), 90)),
    ('6m', ('6 Months', Decimal('4000'), 180)),
    ('1y', ('1 Year', Decimal('8000'), 365)),
])
FEATURED = '1y'


def _country_of(obj):
    if obj is None:
        return countries.DEFAULT_COUNTRY
    if isinstance(obj, str):
        return countries.clean(obj)
    return countries.clean(getattr(obj, 'country', ''))


def country_prices(country):
    """{package: CountryPrice} for one country."""
    from .models import CountryPrice
    return {p.package: p for p in CountryPrice.objects.filter(country=_country_of(country))}


def _row(code, own=None, country_has_prices=False):
    name, default, days = PACKAGES[code]
    if own is not None:
        amount, currency, local, offered = own.amount, own.currency, True, own.active
    else:
        amount, currency, local, offered = default, DEFAULT_CURRENCY, False, not country_has_prices
    months = Decimal(days) / Decimal(30)
    return {'code': code, 'name': name, 'days': days, 'amount': amount, 'currency': currency,
            'symbol': countries.symbol(currency), 'local': local, 'offered': offered,
            'per_month': (amount / months).quantize(Decimal('1'), ROUND_HALF_UP) if months else amount,
            'featured': code == FEATURED}


def price_list(country):
    """Every package with this country's price, plus the saving compared with paying month by month."""
    own = country_prices(country)
    rows = [_row(code, own.get(code), bool(own)) for code in PACKAGES]
    monthly = next((r for r in rows if r['code'] == '1m'), None)
    for r in rows:
        r['saving'] = 0
        if monthly and r['code'] != '1m' and r['currency'] == monthly['currency'] and monthly['amount']:
            full = monthly['amount'] * Decimal(r['days']) / Decimal(30)
            if full > r['amount']:
                r['saving'] = int(((full - r['amount']) / full * 100).quantize(Decimal('1'), ROUND_HALF_UP))
    return rows


def price_for(business_or_country, code):
    """One package's price for a business (or a country code). None for an unknown package."""
    if code not in PACKAGES:
        return None
    own = country_prices(business_or_country)
    return _row(code, own.get(code), bool(own))


def money(amount, currency):
    amount = Decimal(str(amount or 0))
    body = f'{amount:,.0f}' if amount == amount.to_integral_value() else f'{amount:,.2f}'
    return f'{countries.symbol(currency)}{body}'


def grid():
    """For the platform pricing page: every country that has prices, with its packages."""
    from .models import CountryPrice
    out = OrderedDict()
    for p in CountryPrice.objects.select_related('updated_by').order_by('country', 'package'):
        out.setdefault(p.country, {}).__setitem__(p.package, p)
    return out


def save_country(country, values, currency, user=None):
    """Set a country's prices. `values` = {package: amount or ''}; '' removes that package's own price
    (back to the default). `values[code + '_off']` truthy = not offered there. Returns (saved, removed)."""
    from .models import CountryPrice
    country = countries.clean(country, default='')
    if not country:
        raise ValueError('Choose a country.')
    currency = (currency or countries.currency(country)).strip().upper()[:3]
    if len(currency) != 3 or not currency.isalpha():
        raise ValueError('Currency must be a 3-letter code such as GMD, XOF or NGN.')
    saved = removed = 0
    for code in PACKAGES:
        raw = str(values.get(code, '') or '').replace(',', '').strip()
        if not raw:
            removed += CountryPrice.objects.filter(country=country, package=code).delete()[0]
            continue
        try:
            amount = Decimal(raw)
        except Exception:
            raise ValueError(f'{PACKAGES[code][0]}: "{raw}" is not a number.')
        if amount < 0:
            raise ValueError(f'{PACKAGES[code][0]}: the price cannot be negative.')
        CountryPrice.objects.update_or_create(country=country, package=code, defaults={
            'amount': amount, 'currency': currency, 'active': not values.get(f'{code}_off'), 'updated_by': user})
        saved += 1
    return saved, removed
