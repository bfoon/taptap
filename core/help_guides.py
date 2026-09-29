"""How-To guides for the Support page (Help Center).

Each guide: slug, title, category, icon, minutes, summary, keywords, steps and (optional)
problems. Text may use **bold** and `code`; `link` is (url name, button label) and is only
shown to people whose role can open that page.
"""

CATEGORIES = [
    ('start', 'Getting started', 'bi-rocket-takeoff'),
    ('routers', 'Routers', 'bi-router'),
    ('trouble', 'Troubleshooting', 'bi-life-preserver'),
    ('vouchers', 'Vouchers & plans', 'bi-ticket-perforated'),
    ('security', 'Security & fair use', 'bi-shield-check'),
    ('portal', 'Customer portal', 'bi-window-stack'),
    ('business', 'Team & business', 'bi-people'),
]


def S(title, body, link=None, tip=None, code=None):
    return {'title': title, 'body': body, 'link': link, 'tip': tip, 'code': code}


def P(symptom, *fixes):
    return {'symptom': symptom, 'fixes': list(fixes)}


GUIDES = [
    # ───────────────────────────── getting started ─────────────────────────────
    {'slug': 'first-day', 'title': 'Your first day with TapTap', 'category': 'start', 'icon': 'bi-rocket-takeoff', 'minutes': 15, 'popular': True,
     'summary': 'From an empty account to customers logging in with your vouchers — in the right order.',
     'keywords': 'start setup begin new account onboarding',
     'steps': [
         S('Create your plans', 'A plan is what you sell: a price, how long it lasts and how many devices can use it. Start with two or three (e.g. **1 Hour D10**, **1 Day D40**, **1 Week D200**).', ('plans', 'Open Plans')),
         S('Add your MikroTik router', 'Use **TapTap Link** unless TapTap runs on the same network as the router. See the guide “Add a router with TapTap Link”.', ('routers', 'Open MikroTik Control')),
         S('Run a full sync', 'The first sync imports the router’s hotspot profiles as plans, its existing vouchers and the devices on the network.', ('routers', 'Open MikroTik Control')),
         S('Generate vouchers', 'Pick a plan, how many, and who holds them (your shop or an agent). Print them with a voucher design.', ('generate_vouchers', 'Generate vouchers')),
         S('Publish your login page', 'In Portal Studio choose a template, add your logo, then Publish or export it to the router so customers see your brand.', ('portal_studio', 'Open Portal Studio')),
         S('Turn on live sync', 'Live sync keeps sales, used vouchers and online customers up to date every minute or two.', ('security', 'Open Security'),
           tip='Invite your staff under Team so nobody needs your password.'),
     ], 'related': ['add-router-link', 'create-plans', 'generate-vouchers', 'publish-portal']},

    # ───────────────────────────── routers ─────────────────────────────
    {'slug': 'add-router-link', 'title': 'Add a router with TapTap Link (recommended)', 'category': 'routers', 'icon': 'bi-link-45deg', 'minutes': 5, 'popular': True,
     'summary': 'The router connects out to TapTap over HTTPS — no public IP, no open port, no VPN.',
     'keywords': 'add router connect mikrotik link agent winbox terminal script register new router',
     'steps': [
         S('Register the router', 'Go to **MikroTik Control** and, under **Register router via TapTap Link**, type a name (e.g. “Main Hall”) and press **Create Router & Generate Link**.', ('routers', 'Open MikroTik Control')),
         S('Copy the quick-install block', 'The next page shows a **Quick install** block. Press **Copy quick install**. It contains the router’s private token and is shown only once — if you lose it, press **Rotate token** to get a new one.'),
         S('Paste it into the router', 'Open **WinBox** on the router, choose **New Terminal**, paste the whole block and press **Enter**. Wait until the prompt comes back.',
           tip='Paste the complete block in one go. Pasting only part of it is the most common reason a router never checks in.'),
         S('Wait for the green light', 'Within a minute the TapTap Link page turns green and shows the router’s public IP, uptime and online customers.'),
         S('Run the first full sync', 'Back in **MikroTik Control**, press **Sync** on the router. Plans, vouchers and devices are imported.'),
     ],
     'problems': [P('The page never turns green', 'Follow “Router won’t check in (TapTap Link)”.')],
     'related': ['troubleshoot-link', 'router-sync', 'add-router-api']},
    {'slug': 'add-router-api', 'title': 'Add a router with Direct API (same network or VPN)', 'category': 'routers', 'icon': 'bi-hdd-network', 'minutes': 8,
     'summary': 'For TapTap installed on your own network, or reachable through a VPN. TapTap connects in to the router’s API port.',
     'keywords': 'direct api 8728 8729 api-ssl port username password local lan vpn',
     'steps': [
         S('Turn on the API service on the router', 'In WinBox go to **IP › Services** and enable **api** (port 8728) or **api-ssl** (port 8729). In **Available From**, put the TapTap server’s address.'),
         S('Create a user for TapTap', 'In **System › Users**, create a group with the policies **read, write, api, policy, test, sensitive** and a user in that group with a strong password. Don’t use the admin account.',
           code='/user group add name=taptap policy=read,write,api,policy,test,sensitive\n/user add name=taptap group=taptap password=CHANGE-ME'),
         S('Add it in TapTap', 'In **MikroTik Control › Direct API**, fill in the name, the router’s address, the API port, the user and password. Tick SSL only when you use api-ssl on 8729.', ('routers', 'Open MikroTik Control')),
         S('Test and sync', 'Press **Test**. When it says Online, press **Sync**.'),
     ],
     'problems': [
         P('Private address (192.168.x.x, 10.x.x.x)', 'A cloud server can’t reach a private address. Use **TapTap Link** instead, or connect the server and router by VPN (WireGuard, SSTP, OpenVPN).'),
     ], 'related': ['troubleshoot-api', 'add-router-link']},
    {'slug': 'router-sync', 'title': 'Sync a router: plans, vouchers and devices', 'category': 'routers', 'icon': 'bi-arrow-repeat', 'minutes': 3,
     'summary': 'What a full sync does, how long it takes, and when to run it.',
     'keywords': 'sync full sync import profiles vouchers inventory refresh',
     'steps': [
         S('Start a sync', 'In **MikroTik Control**, press **Sync** on the router. The progress shows at the top of the page and on the **Syncing** pill.', ('routers', 'Open MikroTik Control')),
         S('What it brings in', 'HotSpot user profiles become plans (prices are read from Mikhmon-style profiles when present), hotspot users become vouchers, and interfaces, neighbours and devices fill the topology and inventory.'),
         S('What it sends out', 'Vouchers made in TapTap are created on the router, and code changes, freezes and deletions are applied.'),
         S('When to run it', 'After adding a router, after big changes made directly on the router, and after installing a TapTap update. Day to day, **live sync** keeps things current on its own.'),
     ], 'related': ['live-sync', 'add-router-link']},

    # ───────────────────────────── troubleshooting ─────────────────────────────
    {'slug': 'troubleshoot-link', 'title': 'Router won’t check in (TapTap Link)', 'category': 'trouble', 'icon': 'bi-link-45deg', 'minutes': 10, 'popular': True,
     'summary': 'The TapTap Link page stays grey or says the router is offline. Work down this list.',
     'keywords': 'link offline not checking in grey agent heartbeat dns clock certificate https no trusted ca',
     'steps': [
         S('Open the router’s TapTap Link page', 'In **MikroTik Control**, press **TapTap Link** on the router. Near the bottom, **If the router stops checking in** lists ready-made commands. They don’t contain the token, so you can send them to a technician.', ('routers', 'Open MikroTik Control')),
         S('Is it running?', 'Paste the **Is it running?** command in **WinBox › New Terminal**. The scheduler must be enabled; the log shows every problem the Link meets.'),
         S('Can the router reach TapTap?', 'Paste **Can the router reach TapTap?**. It checks DNS, the clock and HTTPS. The last line should say `TapTap Link OK`.'),
         S('Fix the clock and DNS', 'If the date was wrong or the name did not resolve, paste **Fix the clock and DNS**. HTTPS fails when the router’s clock is wrong.'),
         S('Certificate problem', 'If the test says **no trusted CA**, update RouterOS (newer versions trust public certificates), or on RouterOS 7.19+ turn on the built-in trust store as the page shows.'),
         S('Unfreeze the Link', 'If it worked before and stopped, paste **Unfreeze the Link** once. It clears a stuck lock and runs a check-in now.'),
         S('Start fresh', 'Still stuck? Press **Rotate token** and paste the new quick-install block. The **advanced recovery** block on the same page reinstalls the agent and tests HTTPS step by step.'),
     ],
     'problems': [
         P('It worked, then stopped after a power cut', 'Usually the clock: routers without a battery start in 1970. Run **Fix the clock and DNS**.'),
         P('“The router could not update itself”', 'Press **Rotate token** and paste the new quick-install block once.'),
         P('TapTap is not on HTTPS', 'TapTap Link needs TapTap behind HTTPS with `SITE_URL=https://…` set. Ask whoever runs your TapTap server.'),
     ], 'related': ['add-router-link', 'troubleshoot-login']},
    {'slug': 'troubleshoot-api', 'title': 'Direct API router can’t connect', 'category': 'trouble', 'icon': 'bi-plug', 'minutes': 8,
     'summary': 'Read the error under the router in MikroTik Control and match it here.',
     'keywords': 'timed out refused unreachable errno 111 113 login failed invalid user password ssl certificate wrong version api policy',
     'steps': [
         S('Read the error', 'In **MikroTik Control** the router shows its last error. TapTap adds a hint to most of them.', ('routers', 'Open MikroTik Control')),
         S('Test again after each fix', 'Press **Test**. When it is Online, press **Sync**.'),
     ],
     'problems': [
         P('“timed out”, “refused”, “unreachable”, errno 111 / 113',
           'If the address is private (192.168.x.x, 10.x.x.x, 172.16–31.x.x) the server can only reach it on the same LAN or over a VPN — or switch to **TapTap Link**.',
           'Otherwise open the API port to the TapTap server: **IP › Services** (api/api-ssl, and its **Available From** list) and the router’s **input** firewall rules.'),
         P('“login failed”, “invalid user name or password”, “cannot log in”',
           'Check the user and password in TapTap.', 'The user’s group needs the **api** policy (see “Add a router with Direct API”).'),
         P('“SSL”, “certificate” or “wrong version number”',
           'The port and the SSL tick don’t match: **api = 8728 without SSL**, **api-ssl = 8729 with SSL**.'),
     ], 'related': ['add-router-api', 'add-router-link']},
    {'slug': 'troubleshoot-login', 'title': 'A customer can’t log in with a voucher', 'category': 'trouble', 'icon': 'bi-person-x', 'minutes': 6, 'popular': True,
     'summary': 'Find the voucher and read its state — most problems explain themselves there.',
     'keywords': 'cannot login voucher not working invalid expired frozen disabled shared warning code changed wrong password',
     'steps': [
         S('Find the voucher', 'In **Vouchers**, search the code. Old codes (before a code change) are found too.', ('vouchers', 'Open Vouchers')),
         S('Read its state', 'The coloured badge at the top says **In stock, Sold, In use, Expired, Disabled, Frozen** or **Warning**. The history below shows everything that happened to it.'),
         S('Fix what you find', 'See the list below.'),
     ],
     'problems': [
         P('Expired / time ran out', 'Use **Add time** on the voucher, or sell a new one.'),
         P('Frozen or Warning', 'The customer sees a message on the Wi-Fi page. A warning lifts when they press **I agree**; otherwise press **Unfreeze**.'),
         P('Disabled', 'Press **Enable** if it was a mistake.'),
         P('Its code was changed', 'The customer must type the **new** code. After a code change, the router login uses the new code as username and password.'),
         P('“Only X devices” / used on another phone', 'Press **Reset devices** so the customer’s current phone can log in.'),
         P('The voucher isn’t on the router', 'Run **Sync** on the router: TapTap creates missing vouchers there.'),
         P('The login page doesn’t open at all', 'The portal page may not be on the router — see “Publish your customer login page”.'),
     ], 'related': ['change-code', 'freeze-warn', 'shared-auto-warn']},

    # ───────────────────────────── vouchers & plans ─────────────────────────────
    {'slug': 'create-plans', 'title': 'Create plans — including free and unlimited', 'category': 'vouchers', 'icon': 'bi-tags', 'minutes': 4,
     'summary': 'Price, length, devices, speed and data limits; free plans; unlimited time.',
     'keywords': 'plan price duration unlimited free no price speed limit data limit devices profile',
     'steps': [
         S('Open Plans', 'Fill in **Create a TapTap plan**: name, price, duration and unit, devices, optional speed limit (e.g. `2M/5M`) and data limit.', ('plans', 'Open Plans')),
         S('Unlimited time', 'Choose **Unlimited** as the unit. Its vouchers never run out.'),
         S('Free plan', 'Tick **Free plan — no price**. Vouchers work normally, print “FREE”, and Finance does not report them as missing a price.'),
         S('Edit later', 'Press **Edit** on a plan. Tick **Also re-price unsold vouchers** or **Also give unused vouchers the new duration** to update vouchers already printed.'),
         S('Delete a plan', 'The bin button moves it to the Bin. Unused vouchers go with it; used vouchers stay as history. A plan whose vouchers were used can only be deleted by the Owner or an Admin.'),
     ], 'related': ['generate-vouchers', 'fair-usage']},
    {'slug': 'generate-vouchers', 'title': 'Generate and print vouchers', 'category': 'vouchers', 'icon': 'bi-printer', 'minutes': 4, 'popular': True,
     'summary': 'Make a batch for your shop or an agent and print it with your design.',
     'keywords': 'generate batch print voucher design card agent shop quantity',
     'steps': [
         S('Generate a batch', 'In **Generate**, choose the plan, how many, the router and who holds them — your shop or an agent (on credit or paid upfront).', ('generate_vouchers', 'Generate vouchers')),
         S('Print', 'Open the batch in **Batches** and press **Print**. Choose one of your designs from **Voucher Designer**.', ('batches', 'Open Batches')),
         S('Follow the batch', 'Click a batch name to see how many are remaining, in use and expired.'),
     ], 'related': ['agents-cash', 'create-plans']},
    {'slug': 'agents-cash', 'title': 'Give vouchers to agents and collect cash', 'category': 'vouchers', 'icon': 'bi-cash-coin', 'minutes': 5,
     'summary': 'Agents sell on commission; TapTap tracks what each one owes you.',
     'keywords': 'agent reseller commission collect cash balance owes statement',
     'steps': [
         S('Add the agent', 'In **Finance › Agents & cash**, press **Add agent** and set the commission (decimals like 9.09% are fine).', ('finance', 'Open Finance')),
         S('Give them vouchers', 'When generating, choose the agent as holder — or assign an existing batch from the agent’s page.'),
         S('Collect cash', 'Press **Collect** on the agent and enter the amount. Their balance goes down; the agent statement shows every step.'),
     ], 'related': ['generate-vouchers', 'team-accounts']},
    {'slug': 'change-code', 'title': 'Change a voucher code', 'category': 'vouchers', 'icon': 'bi-pencil-square', 'minutes': 2,
     'summary': 'Give a customer a new code (e.g. the old one was seen by someone else) — the time used is kept.',
     'keywords': 'change code rename voucher new code old code',
     'steps': [
         S('Open the voucher', 'Find it in **Vouchers** and open it.', ('vouchers', 'Open Vouchers')),
         S('Change the code', 'Press **Change code**, type the new one (at least 4 characters) or let TapTap pick one.'),
         S('Tell the customer', 'They log in with the **new** code from now on. The router keeps the time already used; the old code is remembered so searches still find the voucher.'),
     ], 'related': ['troubleshoot-login']},
    {'slug': 'freeze-warn', 'title': 'Freeze, warn or add time to a voucher', 'category': 'vouchers', 'icon': 'bi-snow', 'minutes': 3,
     'summary': 'Pause a customer without losing their time, or make them read a message first.',
     'keywords': 'freeze unfreeze warn warning message pause add time extend',
     'steps': [
         S('Freeze', 'On the voucher, press **Freeze** with a reason. Internet stops and the clock stands still. **Unfreeze** gives the remaining time back.'),
         S('Warn (Owner and Admin)', 'Press **Warn**, write the message the customer will read and a reason for your team. They continue by pressing **I agree** on the Wi-Fi page. You can also warn from **Active Users**.', ('active_users', 'Open Active Users')),
         S('Add time', 'Press **Add time** and choose how much. Not available on unlimited vouchers — they have no time limit.'),
     ], 'related': ['shared-auto-warn', 'troubleshoot-login']},

    # ───────────────────────────── security & fair use ─────────────────────────────
    {'slug': 'fair-usage', 'title': 'Set up a fair usage policy (slow heavy users)', 'category': 'security', 'icon': 'bi-speedometer2', 'minutes': 6, 'popular': True,
     'summary': 'Slow a voucher down in steps as it uses more data — per day, per week or over the whole voucher.',
     'keywords': 'fair usage fup data limit throttle slow speed gb steps policy bandwidth cap',
     'steps': [
         S('Open the policy editor', 'Go to **Security** and, in **Fair usage**, press **New policy**.', ('security', 'Open Security')),
         S('Choose the period', '**Every day** resets at midnight, **Every week** on Monday, **Whole voucher** counts the plan’s full time. Choose whether upload counts too.'),
         S('Pick the plans', 'Tick the plans it covers, or none for every plan without its own policy.'),
         S('Build the steps', 'For example: after **2 GB → 5 Mb/s**, after **5 GB → 1 Mb/s**, after **10 GB → 256 kb/s**. Try the presets. The picture on the right shows what customers experience.'),
         S('Free night hours (optional)', 'Data used e.g. **00:00–06:00** can be left out — it rewards night downloads and eases peak hours.'),
         S('Watch it work', '**Slowed now** lists every slowed voucher with its speed cap and live speed. On a voucher, **Full speed until the period resets** helps one customer.'),
     ],
     'problems': [
         P('Nobody gets slowed', 'Live sync must be on — usage is measured at each sync.', 'The policy must be **On** and cover the customer’s plan.'),
         P('Slowed customers still go fast', 'Speed caps are simple queues named `TTFUP-…` on the router. Connections the router **FastTracks** skip queues — check **IP › Firewall › Filter** for a `fasttrack-connection` rule that covers hotspot traffic.'),
     ], 'related': ['live-sync', 'device-data']},
    {'slug': 'shared-auto-warn', 'title': 'Automatically warn shared vouchers', 'category': 'security', 'icon': 'bi-people-fill', 'minutes': 4, 'popular': True,
     'summary': 'When a code is used on more devices than its plan allows, pause it and show a warning — automatically.',
     'keywords': 'shared voucher resold sharing auto warn automatic warning devices allowed agree',
     'steps': [
         S('Open Devices', 'The panel **Vouchers used on more devices than they allow** lists the cases TapTap found.', ('devices', 'Open Devices')),
         S('Choose Automatic', 'Press **Warning settings** and choose **Automatic** — the internet stops at once and the warning page shows. The customer continues by pressing **I agree**; their time is kept.'),
         S('Write your warning (optional)', 'Type your own text, or leave it empty for the standard message.'),
         S('Update the login page', 'For router-served pages, publish or re-deploy your portal once so it can show the warning.', ('portal_studio', 'Open Portal Studio')),
         S('Or decide yourself', 'With **Manual**, each case is listed and you choose: warn, reset devices, freeze, disable or allow.'),
     ], 'related': ['freeze-warn', 'publish-portal']},
    {'slug': 'live-sync', 'title': 'Turn on live sync', 'category': 'security', 'icon': 'bi-broadcast-pin', 'minutes': 2,
     'summary': 'Keeps sales, used vouchers, online customers and fair usage current without pressing Sync.',
     'keywords': 'live sync realtime automatic sales enforcement online',
     'steps': [
         S('Switch it on', 'In **Security**, turn on live sync.', ('security', 'Open Security')),
         S('Check it', 'The **Live** pill at the top turns green. Click it to see each router and the latest changes.'),
     ], 'related': ['fair-usage', 'router-sync']},
    {'slug': 'device-data', 'title': 'Find devices and see what uses their data', 'category': 'security', 'icon': 'bi-fingerprint', 'minutes': 4,
     'summary': 'Search devices like a mail box, and open any device to see its top apps and sites.',
     'keywords': 'devices search filter data usage apps sites youtube tiktok heavy users mac',
     'steps': [
         S('Search', 'In **Devices**, type words (model, MAC, IP, voucher, customer name) and filters such as `os:android`, `is:online`, `app:youtube`, `used>2gb`, `sort:data`. Press **?** for all of them, or `/` to jump to the box.', ('devices', 'Open Devices')),
         S('Open a device', 'Press **Data** on a device: charts by app and type, plus the top apps and sites for 24 hours, 7 days or 30 days.'),
     ], 'related': ['fair-usage']},

    # ───────────────────────────── portal ─────────────────────────────
    {'slug': 'publish-portal', 'title': 'Publish your customer login page', 'category': 'portal', 'icon': 'bi-window-stack', 'minutes': 8,
     'summary': 'Templates, your logo and pictures, then put it on the router.',
     'keywords': 'portal login page template logo image publish export deploy walled garden hotspot html',
     'steps': [
         S('Upload your logo', 'In **Settings**, upload your logo and pick your brand colour — templates use them automatically.', ('settings', 'Open Settings')),
         S('Pick a template', 'In **Portal Studio**, choose a login-page template and change anything: text, colours, background photo. Add pictures with the **Image** block.', ('portal_studio', 'Open Portal Studio')),
         S('Publish it', 'Press **Publish**. Router-served pages are exported to the router’s hotspot folder, and TapTap adds itself to the **walled garden** so the page can check vouchers and show adverts.'),
         S('Try it', 'Connect a phone to the Wi-Fi: the page should appear before login.'),
     ],
     'problems': [P('The old page still shows', 'Publish again, then forget the Wi-Fi network on the phone and reconnect.')],
     'related': ['shared-auto-warn']},

    # ───────────────────────────── team & business ─────────────────────────────
    {'slug': 'team-accounts', 'title': 'Give staff their own logins', 'category': 'business', 'icon': 'bi-person-badge', 'minutes': 4,
     'summary': 'Admin, Finance, Voucher creator, Voucher support or View only — each sees only their part.',
     'keywords': 'team staff roles admin finance support viewer password access permissions',
     'steps': [
         S('Add a team member', 'In **Team**, press **Add team member**, enter their name and email and choose a role. Leave the password empty to get a temporary one.', ('team', 'Open Team')),
         S('Share the password privately', 'They choose their own password the first time they sign in.'),
         S('Switch off or change later', 'Change the role, reset the password or switch the account off from the same page.'),
     ], 'related': ['agents-cash']},
]

BY_SLUG = {g['slug']: g for g in GUIDES}
