"""Apps and sites TapTap can block, slow down or always allow.

Each service has:
  domains  — names the router resolves into an address list (works for every protocol, incl. QUIC)
  sni      — patterns matched on the HTTPS server name (tls-host), which also catches the many
             sub-domains (e.g. *.googlevideo.com) an address list cannot list one by one.
"""

CATEGORIES = [
    ('video', 'Video & streaming', 'bi-play-btn'),
    ('social', 'Social media', 'bi-people'),
    ('chat', 'Messaging & calls', 'bi-chat-dots'),
    ('music', 'Music', 'bi-music-note-beamed'),
    ('games', 'Games', 'bi-controller'),
    ('updates', 'Downloads & updates', 'bi-cloud-download'),
]

SERVICES = {
    'youtube': {'name': 'YouTube', 'cat': 'video', 'color': '#ff0000',
                'domains': ['youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtubei.googleapis.com', 'youtu.be', 'ytimg.com', 'i.ytimg.com'],
                'sni': ['*youtube.com', '*googlevideo.com', '*ytimg.com', '*youtu.be', '*youtubei.googleapis.com']},
    'tiktok': {'name': 'TikTok', 'cat': 'video', 'color': '#111111',
               'domains': ['tiktok.com', 'www.tiktok.com', 'tiktokv.com', 'tiktokcdn.com', 'byteoversea.com', 'ibytedtos.com'],
               'sni': ['*tiktok.com', '*tiktokv.com', '*tiktokcdn.com', '*tiktokcdn-us.com', '*byteoversea.com', '*ibytedtos.com', '*muscdn.com']},
    'netflix': {'name': 'Netflix', 'cat': 'video', 'color': '#e50914',
                'domains': ['netflix.com', 'www.netflix.com', 'nflxvideo.net', 'nflximg.net', 'nflxext.com'],
                'sni': ['*netflix.com', '*nflxvideo.net', '*nflximg.net', '*nflxext.com', '*nflxso.net']},
    'showmax': {'name': 'Showmax / DStv', 'cat': 'video', 'color': '#1f4fd8',
                'domains': ['showmax.com', 'www.showmax.com', 'dstv.com', 'now.dstv.com'],
                'sni': ['*showmax.com', '*dstv.com', '*dstv.stream']},
    'facebook': {'name': 'Facebook', 'cat': 'social', 'color': '#1877f2',
                 'domains': ['facebook.com', 'www.facebook.com', 'm.facebook.com', 'fbcdn.net', 'facebook.net'],
                 'sni': ['*facebook.com', '*fbcdn.net', '*facebook.net', '*fb.com', '*fbsbx.com']},
    'instagram': {'name': 'Instagram', 'cat': 'social', 'color': '#c13584',
                  'domains': ['instagram.com', 'www.instagram.com', 'cdninstagram.com'],
                  'sni': ['*instagram.com', '*cdninstagram.com']},
    'snapchat': {'name': 'Snapchat', 'cat': 'social', 'color': '#fffc00',
                 'domains': ['snapchat.com', 'www.snapchat.com', 'sc-cdn.net', 'snap-dev.net'],
                 'sni': ['*snapchat.com', '*sc-cdn.net', '*snap-dev.net', '*snapkit.co']},
    'x': {'name': 'X (Twitter)', 'cat': 'social', 'color': '#000000',
          'domains': ['x.com', 'twitter.com', 'twimg.com', 't.co'],
          'sni': ['*x.com', '*twitter.com', '*twimg.com', '*t.co']},
    'whatsapp': {'name': 'WhatsApp', 'cat': 'chat', 'color': '#25d366',
                 'domains': ['whatsapp.com', 'www.whatsapp.com', 'web.whatsapp.com', 'whatsapp.net'],
                 'sni': ['*whatsapp.com', '*whatsapp.net']},
    'telegram': {'name': 'Telegram', 'cat': 'chat', 'color': '#229ed9',
                 'domains': ['telegram.org', 'web.telegram.org', 't.me', 'telegram.me'],
                 'sni': ['*telegram.org', '*t.me', '*telegram.me', '*telesco.pe']},
    'zoom': {'name': 'Zoom', 'cat': 'chat', 'color': '#2d8cff',
             'domains': ['zoom.us', 'www.zoom.us'], 'sni': ['*zoom.us', '*zoom.com']},
    'spotify': {'name': 'Spotify', 'cat': 'music', 'color': '#1db954',
                'domains': ['spotify.com', 'open.spotify.com', 'scdn.co', 'spotifycdn.com'],
                'sni': ['*spotify.com', '*scdn.co', '*spotifycdn.com', '*spotify.design']},
    'audiomack': {'name': 'Audiomack / Boomplay', 'cat': 'music', 'color': '#ffa200',
                  'domains': ['audiomack.com', 'boomplay.com', 'boomplaymusic.com'],
                  'sni': ['*audiomack.com', '*boomplay.com', '*boomplaymusic.com']},
    'games': {'name': 'Online games (PUBG, Free Fire, CoD, Steam)', 'cat': 'games', 'color': '#7c3aed',
              'domains': ['pubgmobile.com', 'freefiremobile.com', 'garena.com', 'callofduty.com', 'activision.com', 'steampowered.com', 'steamcommunity.com'],
              'sni': ['*pubgmobile.com', '*igamecj.com', '*freefiremobile.com', '*garena.com', '*callofduty.com', '*activision.com', '*steampowered.com',
                      '*steamcontent.com', '*steamserver.net', '*epicgames.com']},
    'updates': {'name': 'App & system updates (Play Store, App Store, Windows)', 'cat': 'updates', 'color': '#0ea5a4',
                'domains': ['play.googleapis.com', 'android.clients.google.com', 'apps.apple.com', 'swcdn.apple.com', 'windowsupdate.com', 'update.microsoft.com'],
                'sni': ['*play.googleapis.com', '*gvt1.com', '*android.clients.google.com', '*swcdn.apple.com', '*apps.apple.com', '*mzstatic.com',
                        '*windowsupdate.com', '*update.microsoft.com', '*delivery.mp.microsoft.com']},
}


def choices():
    out = []
    for key, name, icon in CATEGORIES:
        items = [(k, s) for k, s in SERVICES.items() if s['cat'] == key]
        if items:
            out.append({'key': key, 'name': name, 'icon': icon, 'items': [{'key': k, **s} for k, s in items]})
    return out


def names(keys):
    return [SERVICES[k]['name'] for k in keys if k in SERVICES]
