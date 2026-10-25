"""Countries for sign-up, pricing and phone hints — a static table, no extra packages.

(ISO 3166 alpha-2, English name, flag, international dial code, ISO 4217 currency)
Generated once from pycountry / Babel / phonenumbers data; edit by hand if a country changes.
"""

DEFAULT_COUNTRY = 'GM'

# Short symbols for the currencies TapTap sees most. Anything else shows its ISO code ("KES 500").
CURRENCY_SYMBOLS = {
    'GMD': 'D', 'XOF': 'CFA ', 'XAF': 'FCFA ', 'NGN': '₦', 'GHS': 'GH₵', 'SLE': 'Le ', 'SLL': 'Le ', 'LRD': 'L$',
    'GNF': 'FG ', 'MRU': 'UM ', 'CVE': 'Esc ', 'KES': 'KSh ', 'UGX': 'USh ', 'TZS': 'TSh ', 'RWF': 'RF ', 'ZAR': 'R',
    'EGP': 'E£', 'MAD': 'DH ', 'ETB': 'Br ', 'ZMW': 'K', 'USD': '$', 'GBP': '£', 'EUR': '€', 'CAD': 'C$', 'INR': '₹',
}

COUNTRIES = [
    ('AF', 'Afghanistan', '🇦🇫', 93, 'AFN'),
    ('AL', 'Albania', '🇦🇱', 355, 'ALL'),
    ('DZ', 'Algeria', '🇩🇿', 213, 'DZD'),
    ('AS', 'American Samoa', '🇦🇸', 1, 'USD'),
    ('AD', 'Andorra', '🇦🇩', 376, 'EUR'),
    ('AO', 'Angola', '🇦🇴', 244, 'AOA'),
    ('AI', 'Anguilla', '🇦🇮', 1, 'XCD'),
    ('AQ', 'Antarctica', '🇦🇶', 0, ''),
    ('AG', 'Antigua & Barbuda', '🇦🇬', 1, 'XCD'),
    ('AR', 'Argentina', '🇦🇷', 54, 'ARS'),
    ('AM', 'Armenia', '🇦🇲', 374, 'AMD'),
    ('AW', 'Aruba', '🇦🇼', 297, 'AWG'),
    ('AU', 'Australia', '🇦🇺', 61, 'AUD'),
    ('AT', 'Austria', '🇦🇹', 43, 'EUR'),
    ('AZ', 'Azerbaijan', '🇦🇿', 994, 'AZN'),
    ('BS', 'Bahamas', '🇧🇸', 1, 'BSD'),
    ('BH', 'Bahrain', '🇧🇭', 973, 'BHD'),
    ('BD', 'Bangladesh', '🇧🇩', 880, 'BDT'),
    ('BB', 'Barbados', '🇧🇧', 1, 'BBD'),
    ('BY', 'Belarus', '🇧🇾', 375, 'BYN'),
    ('BE', 'Belgium', '🇧🇪', 32, 'EUR'),
    ('BZ', 'Belize', '🇧🇿', 501, 'BZD'),
    ('BJ', 'Benin', '🇧🇯', 229, 'XOF'),
    ('BM', 'Bermuda', '🇧🇲', 1, 'BMD'),
    ('BT', 'Bhutan', '🇧🇹', 975, 'INR'),
    ('BO', 'Bolivia', '🇧🇴', 591, 'BOB'),
    ('BA', 'Bosnia & Herzegovina', '🇧🇦', 387, 'BAM'),
    ('BW', 'Botswana', '🇧🇼', 267, 'BWP'),
    ('BV', 'Bouvet Island', '🇧🇻', 0, 'NOK'),
    ('BR', 'Brazil', '🇧🇷', 55, 'BRL'),
    ('IO', 'British Indian Ocean Territory', '🇮🇴', 246, 'USD'),
    ('VG', 'British Virgin Islands', '🇻🇬', 1, 'USD'),
    ('BN', 'Brunei', '🇧🇳', 673, 'BND'),
    ('BG', 'Bulgaria', '🇧🇬', 359, 'BGN'),
    ('BF', 'Burkina Faso', '🇧🇫', 226, 'XOF'),
    ('BI', 'Burundi', '🇧🇮', 257, 'BIF'),
    ('KH', 'Cambodia', '🇰🇭', 855, 'KHR'),
    ('CM', 'Cameroon', '🇨🇲', 237, 'XAF'),
    ('CA', 'Canada', '🇨🇦', 1, 'CAD'),
    ('CV', 'Cape Verde', '🇨🇻', 238, 'CVE'),
    ('BQ', 'Caribbean Netherlands', '🇧🇶', 599, 'USD'),
    ('KY', 'Cayman Islands', '🇰🇾', 1, 'KYD'),
    ('CF', 'Central African Republic', '🇨🇫', 236, 'XAF'),
    ('TD', 'Chad', '🇹🇩', 235, 'XAF'),
    ('CL', 'Chile', '🇨🇱', 56, 'CLP'),
    ('CN', 'China', '🇨🇳', 86, 'CNY'),
    ('CX', 'Christmas Island', '🇨🇽', 61, 'AUD'),
    ('CC', 'Cocos (Keeling) Islands', '🇨🇨', 61, 'AUD'),
    ('CO', 'Colombia', '🇨🇴', 57, 'COP'),
    ('KM', 'Comoros', '🇰🇲', 269, 'KMF'),
    ('CG', 'Congo - Brazzaville', '🇨🇬', 242, 'XAF'),
    ('CD', 'Congo - Kinshasa', '🇨🇩', 243, 'CDF'),
    ('CK', 'Cook Islands', '🇨🇰', 682, 'NZD'),
    ('CR', 'Costa Rica', '🇨🇷', 506, 'CRC'),
    ('HR', 'Croatia', '🇭🇷', 385, 'EUR'),
    ('CU', 'Cuba', '🇨🇺', 53, 'CUP'),
    ('CW', 'Curaçao', '🇨🇼', 599, 'XCG'),
    ('CY', 'Cyprus', '🇨🇾', 357, 'EUR'),
    ('CZ', 'Czechia', '🇨🇿', 420, 'CZK'),
    ('CI', 'Côte d’Ivoire', '🇨🇮', 225, 'XOF'),
    ('DK', 'Denmark', '🇩🇰', 45, 'DKK'),
    ('DJ', 'Djibouti', '🇩🇯', 253, 'DJF'),
    ('DM', 'Dominica', '🇩🇲', 1, 'XCD'),
    ('DO', 'Dominican Republic', '🇩🇴', 1, 'DOP'),
    ('EC', 'Ecuador', '🇪🇨', 593, 'USD'),
    ('EG', 'Egypt', '🇪🇬', 20, 'EGP'),
    ('SV', 'El Salvador', '🇸🇻', 503, 'USD'),
    ('GQ', 'Equatorial Guinea', '🇬🇶', 240, 'XAF'),
    ('ER', 'Eritrea', '🇪🇷', 291, 'ERN'),
    ('EE', 'Estonia', '🇪🇪', 372, 'EUR'),
    ('SZ', 'Eswatini', '🇸🇿', 268, 'SZL'),
    ('ET', 'Ethiopia', '🇪🇹', 251, 'ETB'),
    ('FK', 'Falkland Islands', '🇫🇰', 500, 'FKP'),
    ('FO', 'Faroe Islands', '🇫🇴', 298, 'DKK'),
    ('FJ', 'Fiji', '🇫🇯', 679, 'FJD'),
    ('FI', 'Finland', '🇫🇮', 358, 'EUR'),
    ('FR', 'France', '🇫🇷', 33, 'EUR'),
    ('GF', 'French Guiana', '🇬🇫', 594, 'EUR'),
    ('PF', 'French Polynesia', '🇵🇫', 689, 'XPF'),
    ('TF', 'French Southern Territories', '🇹🇫', 0, 'EUR'),
    ('GA', 'Gabon', '🇬🇦', 241, 'XAF'),
    ('GM', 'Gambia', '🇬🇲', 220, 'GMD'),
    ('GE', 'Georgia', '🇬🇪', 995, 'GEL'),
    ('DE', 'Germany', '🇩🇪', 49, 'EUR'),
    ('GH', 'Ghana', '🇬🇭', 233, 'GHS'),
    ('GI', 'Gibraltar', '🇬🇮', 350, 'GIP'),
    ('GR', 'Greece', '🇬🇷', 30, 'EUR'),
    ('GL', 'Greenland', '🇬🇱', 299, 'DKK'),
    ('GD', 'Grenada', '🇬🇩', 1, 'XCD'),
    ('GP', 'Guadeloupe', '🇬🇵', 590, 'EUR'),
    ('GU', 'Guam', '🇬🇺', 1, 'USD'),
    ('GT', 'Guatemala', '🇬🇹', 502, 'GTQ'),
    ('GG', 'Guernsey', '🇬🇬', 44, 'GBP'),
    ('GN', 'Guinea', '🇬🇳', 224, 'GNF'),
    ('GW', 'Guinea-Bissau', '🇬🇼', 245, 'XOF'),
    ('GY', 'Guyana', '🇬🇾', 592, 'GYD'),
    ('HT', 'Haiti', '🇭🇹', 509, 'HTG'),
    ('HM', 'Heard & McDonald Islands', '🇭🇲', 0, 'AUD'),
    ('HN', 'Honduras', '🇭🇳', 504, 'HNL'),
    ('HK', 'Hong Kong SAR China', '🇭🇰', 852, 'HKD'),
    ('HU', 'Hungary', '🇭🇺', 36, 'HUF'),
    ('IS', 'Iceland', '🇮🇸', 354, 'ISK'),
    ('IN', 'India', '🇮🇳', 91, 'INR'),
    ('ID', 'Indonesia', '🇮🇩', 62, 'IDR'),
    ('IR', 'Iran', '🇮🇷', 98, 'IRR'),
    ('IQ', 'Iraq', '🇮🇶', 964, 'IQD'),
    ('IE', 'Ireland', '🇮🇪', 353, 'EUR'),
    ('IM', 'Isle of Man', '🇮🇲', 44, 'GBP'),
    ('IL', 'Israel', '🇮🇱', 972, 'ILS'),
    ('IT', 'Italy', '🇮🇹', 39, 'EUR'),
    ('JM', 'Jamaica', '🇯🇲', 1, 'JMD'),
    ('JP', 'Japan', '🇯🇵', 81, 'JPY'),
    ('JE', 'Jersey', '🇯🇪', 44, 'GBP'),
    ('JO', 'Jordan', '🇯🇴', 962, 'JOD'),
    ('KZ', 'Kazakhstan', '🇰🇿', 7, 'KZT'),
    ('KE', 'Kenya', '🇰🇪', 254, 'KES'),
    ('KI', 'Kiribati', '🇰🇮', 686, 'AUD'),
    ('KW', 'Kuwait', '🇰🇼', 965, 'KWD'),
    ('KG', 'Kyrgyzstan', '🇰🇬', 996, 'KGS'),
    ('LA', 'Laos', '🇱🇦', 856, 'LAK'),
    ('LV', 'Latvia', '🇱🇻', 371, 'EUR'),
    ('LB', 'Lebanon', '🇱🇧', 961, 'LBP'),
    ('LS', 'Lesotho', '🇱🇸', 266, 'ZAR'),
    ('LR', 'Liberia', '🇱🇷', 231, 'LRD'),
    ('LY', 'Libya', '🇱🇾', 218, 'LYD'),
    ('LI', 'Liechtenstein', '🇱🇮', 423, 'CHF'),
    ('LT', 'Lithuania', '🇱🇹', 370, 'EUR'),
    ('LU', 'Luxembourg', '🇱🇺', 352, 'EUR'),
    ('MO', 'Macao SAR China', '🇲🇴', 853, 'MOP'),
    ('MG', 'Madagascar', '🇲🇬', 261, 'MGA'),
    ('MW', 'Malawi', '🇲🇼', 265, 'MWK'),
    ('MY', 'Malaysia', '🇲🇾', 60, 'MYR'),
    ('MV', 'Maldives', '🇲🇻', 960, 'MVR'),
    ('ML', 'Mali', '🇲🇱', 223, 'XOF'),
    ('MT', 'Malta', '🇲🇹', 356, 'EUR'),
    ('MH', 'Marshall Islands', '🇲🇭', 692, 'USD'),
    ('MQ', 'Martinique', '🇲🇶', 596, 'EUR'),
    ('MR', 'Mauritania', '🇲🇷', 222, 'MRU'),
    ('MU', 'Mauritius', '🇲🇺', 230, 'MUR'),
    ('YT', 'Mayotte', '🇾🇹', 262, 'EUR'),
    ('MX', 'Mexico', '🇲🇽', 52, 'MXN'),
    ('FM', 'Micronesia', '🇫🇲', 691, 'USD'),
    ('MD', 'Moldova', '🇲🇩', 373, 'MDL'),
    ('MC', 'Monaco', '🇲🇨', 377, 'EUR'),
    ('MN', 'Mongolia', '🇲🇳', 976, 'MNT'),
    ('ME', 'Montenegro', '🇲🇪', 382, 'EUR'),
    ('MS', 'Montserrat', '🇲🇸', 1, 'XCD'),
    ('MA', 'Morocco', '🇲🇦', 212, 'MAD'),
    ('MZ', 'Mozambique', '🇲🇿', 258, 'MZN'),
    ('MM', 'Myanmar (Burma)', '🇲🇲', 95, 'MMK'),
    ('NA', 'Namibia', '🇳🇦', 264, 'ZAR'),
    ('NR', 'Nauru', '🇳🇷', 674, 'AUD'),
    ('NP', 'Nepal', '🇳🇵', 977, 'NPR'),
    ('NL', 'Netherlands', '🇳🇱', 31, 'EUR'),
    ('NC', 'New Caledonia', '🇳🇨', 687, 'XPF'),
    ('NZ', 'New Zealand', '🇳🇿', 64, 'NZD'),
    ('NI', 'Nicaragua', '🇳🇮', 505, 'NIO'),
    ('NE', 'Niger', '🇳🇪', 227, 'XOF'),
    ('NG', 'Nigeria', '🇳🇬', 234, 'NGN'),
    ('NU', 'Niue', '🇳🇺', 683, 'NZD'),
    ('NF', 'Norfolk Island', '🇳🇫', 672, 'AUD'),
    ('KP', 'North Korea', '🇰🇵', 850, 'KPW'),
    ('MK', 'North Macedonia', '🇲🇰', 389, 'MKD'),
    ('MP', 'Northern Mariana Islands', '🇲🇵', 1, 'USD'),
    ('NO', 'Norway', '🇳🇴', 47, 'NOK'),
    ('OM', 'Oman', '🇴🇲', 968, 'OMR'),
    ('PK', 'Pakistan', '🇵🇰', 92, 'PKR'),
    ('PW', 'Palau', '🇵🇼', 680, 'USD'),
    ('PS', 'Palestinian Territories', '🇵🇸', 970, 'ILS'),
    ('PA', 'Panama', '🇵🇦', 507, 'PAB'),
    ('PG', 'Papua New Guinea', '🇵🇬', 675, 'PGK'),
    ('PY', 'Paraguay', '🇵🇾', 595, 'PYG'),
    ('PE', 'Peru', '🇵🇪', 51, 'PEN'),
    ('PH', 'Philippines', '🇵🇭', 63, 'PHP'),
    ('PN', 'Pitcairn Islands', '🇵🇳', 0, 'NZD'),
    ('PL', 'Poland', '🇵🇱', 48, 'PLN'),
    ('PT', 'Portugal', '🇵🇹', 351, 'EUR'),
    ('PR', 'Puerto Rico', '🇵🇷', 1, 'USD'),
    ('QA', 'Qatar', '🇶🇦', 974, 'QAR'),
    ('RO', 'Romania', '🇷🇴', 40, 'RON'),
    ('RU', 'Russia', '🇷🇺', 7, 'RUB'),
    ('RW', 'Rwanda', '🇷🇼', 250, 'RWF'),
    ('RE', 'Réunion', '🇷🇪', 262, 'EUR'),
    ('WS', 'Samoa', '🇼🇸', 685, 'WST'),
    ('SM', 'San Marino', '🇸🇲', 378, 'EUR'),
    ('SA', 'Saudi Arabia', '🇸🇦', 966, 'SAR'),
    ('SN', 'Senegal', '🇸🇳', 221, 'XOF'),
    ('RS', 'Serbia', '🇷🇸', 381, 'RSD'),
    ('SC', 'Seychelles', '🇸🇨', 248, 'SCR'),
    ('SL', 'Sierra Leone', '🇸🇱', 232, 'SLE'),
    ('SG', 'Singapore', '🇸🇬', 65, 'SGD'),
    ('SX', 'Sint Maarten', '🇸🇽', 1, 'XCG'),
    ('SK', 'Slovakia', '🇸🇰', 421, 'EUR'),
    ('SI', 'Slovenia', '🇸🇮', 386, 'EUR'),
    ('SB', 'Solomon Islands', '🇸🇧', 677, 'SBD'),
    ('SO', 'Somalia', '🇸🇴', 252, 'SOS'),
    ('ZA', 'South Africa', '🇿🇦', 27, 'ZAR'),
    ('GS', 'South Georgia & South Sandwich Islands', '🇬🇸', 0, 'GBP'),
    ('KR', 'South Korea', '🇰🇷', 82, 'KRW'),
    ('SS', 'South Sudan', '🇸🇸', 211, 'SSP'),
    ('ES', 'Spain', '🇪🇸', 34, 'EUR'),
    ('LK', 'Sri Lanka', '🇱🇰', 94, 'LKR'),
    ('BL', 'St. Barthélemy', '🇧🇱', 590, 'EUR'),
    ('SH', 'St. Helena', '🇸🇭', 290, 'SHP'),
    ('KN', 'St. Kitts & Nevis', '🇰🇳', 1, 'XCD'),
    ('LC', 'St. Lucia', '🇱🇨', 1, 'XCD'),
    ('MF', 'St. Martin', '🇲🇫', 590, 'EUR'),
    ('PM', 'St. Pierre & Miquelon', '🇵🇲', 508, 'EUR'),
    ('VC', 'St. Vincent & Grenadines', '🇻🇨', 1, 'XCD'),
    ('SD', 'Sudan', '🇸🇩', 249, 'SDG'),
    ('SR', 'Suriname', '🇸🇷', 597, 'SRD'),
    ('SJ', 'Svalbard & Jan Mayen', '🇸🇯', 47, 'NOK'),
    ('SE', 'Sweden', '🇸🇪', 46, 'SEK'),
    ('CH', 'Switzerland', '🇨🇭', 41, 'CHF'),
    ('SY', 'Syria', '🇸🇾', 963, 'SYP'),
    ('ST', 'São Tomé & Príncipe', '🇸🇹', 239, 'STN'),
    ('TW', 'Taiwan', '🇹🇼', 886, 'TWD'),
    ('TJ', 'Tajikistan', '🇹🇯', 992, 'TJS'),
    ('TZ', 'Tanzania', '🇹🇿', 255, 'TZS'),
    ('TH', 'Thailand', '🇹🇭', 66, 'THB'),
    ('TL', 'Timor-Leste', '🇹🇱', 670, 'USD'),
    ('TG', 'Togo', '🇹🇬', 228, 'XOF'),
    ('TK', 'Tokelau', '🇹🇰', 690, 'NZD'),
    ('TO', 'Tonga', '🇹🇴', 676, 'TOP'),
    ('TT', 'Trinidad & Tobago', '🇹🇹', 1, 'TTD'),
    ('TN', 'Tunisia', '🇹🇳', 216, 'TND'),
    ('TM', 'Turkmenistan', '🇹🇲', 993, 'TMT'),
    ('TC', 'Turks & Caicos Islands', '🇹🇨', 1, 'USD'),
    ('TV', 'Tuvalu', '🇹🇻', 688, 'AUD'),
    ('TR', 'Türkiye', '🇹🇷', 90, 'TRY'),
    ('UM', 'U.S. Outlying Islands', '🇺🇲', 0, 'USD'),
    ('VI', 'U.S. Virgin Islands', '🇻🇮', 1, 'USD'),
    ('UG', 'Uganda', '🇺🇬', 256, 'UGX'),
    ('UA', 'Ukraine', '🇺🇦', 380, 'UAH'),
    ('AE', 'United Arab Emirates', '🇦🇪', 971, 'AED'),
    ('GB', 'United Kingdom', '🇬🇧', 44, 'GBP'),
    ('US', 'United States', '🇺🇸', 1, 'USD'),
    ('UY', 'Uruguay', '🇺🇾', 598, 'UYU'),
    ('UZ', 'Uzbekistan', '🇺🇿', 998, 'UZS'),
    ('VU', 'Vanuatu', '🇻🇺', 678, 'VUV'),
    ('VA', 'Vatican City', '🇻🇦', 39, 'EUR'),
    ('VE', 'Venezuela', '🇻🇪', 58, 'VES'),
    ('VN', 'Vietnam', '🇻🇳', 84, 'VND'),
    ('WF', 'Wallis & Futuna', '🇼🇫', 681, 'XPF'),
    ('EH', 'Western Sahara', '🇪🇭', 212, 'MAD'),
    ('YE', 'Yemen', '🇾🇪', 967, 'YER'),
    ('ZM', 'Zambia', '🇿🇲', 260, 'ZMW'),
    ('ZW', 'Zimbabwe', '🇿🇼', 263, 'USD'),
    ('AX', 'Åland Islands', '🇦🇽', 358, 'EUR'),
]

_BY_CODE = {c[0]: c for c in COUNTRIES}
CHOICES = [(c[0], c[1]) for c in COUNTRIES]


def valid(code):
    return (code or '').upper() in _BY_CODE


def clean(code, default=DEFAULT_COUNTRY):
    code = (code or '').strip().upper()
    return code if code in _BY_CODE else default


def name(code):
    c = _BY_CODE.get((code or '').upper())
    return c[1] if c else ''


def flag(code):
    c = _BY_CODE.get((code or '').upper())
    return c[2] if c else '🌍'


def dial(code):
    c = _BY_CODE.get((code or '').upper())
    return f'+{c[3]}' if c and c[3] else ''


def currency(code):
    """ISO currency used in a country (GMD for Gambia)."""
    c = _BY_CODE.get((code or '').upper())
    return (c[4] if c else '') or 'USD'


def symbol(currency_code):
    """Short money prefix for a currency: D for GMD, ₦ for NGN, else 'KES '."""
    currency_code = (currency_code or '').upper()
    return CURRENCY_SYMBOLS.get(currency_code, f'{currency_code} ' if currency_code else 'D')


def label(code):
    return f'{flag(code)} {name(code)}' if valid(code) else '🌍 Not set'


def options():
    """For pickers: [{code, name, flag, dial, currency}] sorted by name."""
    return [{'code': c[0], 'name': c[1], 'flag': c[2], 'dial': f'+{c[3]}' if c[3] else '', 'currency': c[4]} for c in COUNTRIES]
