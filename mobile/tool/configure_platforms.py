#!/usr/bin/env python3
"""Run once after `flutter create` (see README): adds what calls, the camera, voice input and push need —
including Android's notification channels, so pushes pop up on the screen (see CHANNELS below).

    cd mobile
    flutter create --org gm.easysolutions --project-name taptap_app --platforms android,ios .
    python3 tool/configure_platforms.py

Works with only one platform made too (GitHub's Android job makes only android/, the iPhone job only ios/):
a platform that is not there is skipped. It fails only when neither is there, or a change it must make
could not be made. Safe to run again: nothing is added twice.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
ok = True
found = 0


def say(msg):
    print('  ' + msg)


# ── Android ──
man = ROOT / 'android/app/src/main/AndroidManifest.xml'
if man.exists():
    found += 1
    s = man.read_text()
    perms = ['INTERNET', 'RECORD_AUDIO', 'CAMERA', 'MODIFY_AUDIO_SETTINGS', 'BLUETOOTH_CONNECT', 'ACCESS_NETWORK_STATE',
             'CHANGE_NETWORK_STATE', 'POST_NOTIFICATIONS', 'USE_FULL_SCREEN_INTENT', 'FOREGROUND_SERVICE', 'WAKE_LOCK', 'VIBRATE',
             'FOREGROUND_SERVICE_PHONE_CALL', 'FOREGROUND_SERVICE_MICROPHONE']
    add = ''.join(f'    <uses-permission android:name="android.permission.{p}"/>\n' for p in perms
                  if f'android.permission.{p}"' not in s)
    if add:
        s = re.sub(r'(<manifest[^>]*>\n)', lambda m: m.group(1) + add, s, count=1)
        say(f'Android: {add.count("uses-permission")} permissions added')
    queries = ('    <queries>\n'
               '        <intent><action android:name="android.speech.RecognitionService"/></intent>\n'
               '        <intent><action android:name="android.intent.action.DIAL"/><data android:scheme="tel"/></intent>\n'
               '        <intent><action android:name="android.intent.action.VIEW"/><data android:scheme="https"/></intent>\n'
               '    </queries>\n')
    if 'android.speech.RecognitionService' not in s:
        s = s.replace('</manifest>', queries + '</manifest>')
        say('Android: <queries> for voice input, phone calls and links added')
    if 'android:showWhenLocked' not in s:
        s = s.replace('android:name=".MainActivity"', 'android:name=".MainActivity"\n            android:showWhenLocked="true"\n            android:turnScreenOn="true"', 1)
        say('Android: incoming calls can show over the lock screen')
    if 'default_notification_channel_id' not in s:
        # a push that names no channel (or one this phone does not have yet) still pops up
        s = s.replace('</application>', '    <meta-data android:name="com.google.firebase.messaging.default_notification_channel_id"\n'
                                        '            android:value="tt_alerts"/>\n    </application>', 1)
        say('Android: pushes without a channel use "Business alerts"')
    man.write_text(s)
else:
    say('Android: no android/ folder — skipped')

# ── Android notification channels ──
# One channel per kind of push (core/mobile_push.py KINDS), all at HIGH importance: that is what makes Android
# show a notification as a pop-up (heads-up) instead of only an icon in the status bar. Made when the app starts,
# in MainActivity, which also lets the app open the phone's notification settings for TapTap.
CHANNELS = [
    ('tt_chat', 'Chat messages', 'New messages from your team and TapTap support'),
    ('tt_complaints', 'Customer complaints', 'A customer reported a voucher from your Wi-Fi page'),
    ('tt_calls_missed', 'Missed customer calls', 'Nobody answered a customer who called from the Wi-Fi page'),
    ('tt_alerts', 'Business alerts', 'Stock low, router offline, agents and the vouchers you track'),
    ('tt_vouchers', 'Vouchers made by the team', 'Someone else on the team generated vouchers'),
]
MARK = '// TapTap notification channels — written by tool/configure_platforms.py'
PLAIN = re.compile(r'^\s*package\s+([\w.]+)\s*import\s+io\.flutter\.embedding\.android\.FlutterActivity\s*'
                   r'class\s+MainActivity\s*:\s*FlutterActivity\(\)\s*(\{\s*\})?\s*$')
acts = sorted((ROOT / 'android/app/src/main').glob('**/MainActivity.kt')) if man.exists() else []
if man.exists() and not acts:
    ok = False
    say('Android: MainActivity.kt not found — notification channels not made')
for act in acts[:1]:
    src = act.read_text()
    pkg = re.search(r'^\s*package\s+([\w.]+)', src, re.M)
    if MARK not in src and not PLAIN.match(src):
        ok = False
        say(f'Android: {act.name} was changed by hand — add the notification channels yourself (see CHANNELS in this script)')
    elif pkg:
        rows = ',\n'.join(f'            Triple("{i}", "{n}", "{d}")' for i, n, d in CHANNELS)
        act.write_text(f'''package {pkg.group(1)}

{MARK} (safe to re-run).
// Every kind of TapTap push has its own channel at high importance, so Android pops it up on the screen;
// people can still change each one under Settings → Apps → TapTap → Notifications.

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {{
    override fun onCreate(savedInstanceState: Bundle?) {{
        super.onCreate(savedInstanceState)
        createChannels()
    }}

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {{
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "taptap/notifications").setMethodCallHandler {{ call, result ->
            when (call.method) {{
                "openSettings" -> {{
                    val intent = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {{
                        Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, packageName)
                    }} else {{
                        Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.fromParts("package", packageName, null))
                    }}
                    intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    try {{
                        startActivity(intent)
                        result.success(true)
                    }} catch (e: Exception) {{
                        result.success(false)
                    }}
                }}
                "createChannels" -> {{
                    createChannels()
                    result.success(true)
                }}
                else -> result.notImplemented()
            }}
        }}
    }}

    private fun createChannels() {{
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = getSystemService(NotificationManager::class.java) ?: return
        val channels = listOf(
{rows}
        )
        for ((id, name, text) in channels) {{
            val channel = NotificationChannel(id, name, NotificationManager.IMPORTANCE_HIGH)
            channel.description = text
            channel.enableVibration(true)
            manager.createNotificationChannel(channel)
        }}
    }}
}}
''')
        say(f'Android: {len(CHANNELS)} notification channels (pop up on screen) in {act.name}')

for g in (ROOT / 'android/app/build.gradle.kts', ROOT / 'android/app/build.gradle'):
    if g.exists():
        s = g.read_text()
        s2 = re.sub(r'minSdk(Version)?\s*=?\s*flutter\.minSdkVersion', lambda m: 'minSdk = 24' if g.suffix == '.kts' else 'minSdkVersion 24', s)
        if s2 != s:
            g.write_text(s2)
            say(f'Android: minSdk 24 in {g.name}')
        break

# ── Firebase (Android): only when android/app/google-services.json is there (CI writes it from a secret) ──
gs = ROOT / 'android/app/google-services.json'
if not man.exists():
    pass
elif gs.exists():
    for settings in (ROOT / 'android/settings.gradle.kts', ROOT / 'android/settings.gradle'):
        if settings.exists():
            s = settings.read_text()
            if 'com.google.gms.google-services' not in s:
                kts = settings.suffix == '.kts'
                line = ('    id("com.google.gms.google-services") version "4.4.2" apply false\n' if kts
                        else "    id 'com.google.gms.google-services' version '4.4.2' apply false\n")
                s2 = re.sub(r'(\n\s*id[ (]["\']dev\.flutter\.flutter-plugin-loader["\'][^\n]*\n)', lambda m: m.group(1) + line, s, count=1)
                if s2 != s:
                    settings.write_text(s2)
                    say(f'Firebase: Google services plugin declared in {settings.name}')
                else:
                    ok = False
                    say(f'Firebase: could not find the plugins block in {settings.name} — add the Google services plugin by hand')
            break
    for g in (ROOT / 'android/app/build.gradle.kts', ROOT / 'android/app/build.gradle'):
        if g.exists():
            s = g.read_text()
            if 'com.google.gms.google-services' not in s:
                line = '    id("com.google.gms.google-services")\n' if g.suffix == '.kts' else "    id 'com.google.gms.google-services'\n"
                s2 = re.sub(r'(\n\s*id[ (]["\']dev\.flutter\.flutter-gradle-plugin["\'][^\n]*\n)', lambda m: m.group(1) + line, s, count=1)
                if s2 != s:
                    g.write_text(s2)
                    say(f'Firebase: Google services plugin applied in app/{g.name}')
                else:
                    ok = False
                    say(f'Firebase: could not find the plugins block in app/{g.name} — add the Google services plugin by hand')
            break
else:
    say('Firebase: no google-services.json — push stays off (the app still works while open)')

# ── iPhone ──
plist = ROOT / 'ios/Runner/Info.plist'
if plist.exists():
    found += 1
    s = plist.read_text()
    keys = {
        'NSMicrophoneUsageDescription': 'TapTap uses the microphone for calls with your team and customers.',
        'NSCameraUsageDescription': 'TapTap uses the camera to show something during a call and to send photos in chat.',
        'NSPhotoLibraryUsageDescription': 'TapTap lets you send photos from your library in chat.',
        'NSSpeechRecognitionUsageDescription': 'TapTap turns what you say into a question for Fit.',
    }
    add = ''.join(f'\t<key>{k}</key>\n\t<string>{v}</string>\n' for k, v in keys.items() if f'<key>{k}</key>' not in s)
    if '<key>UIBackgroundModes</key>' not in s:
        add += ('\t<key>UIBackgroundModes</key>\n\t<array>\n\t\t<string>audio</string>\n\t\t<string>voip</string>\n'
                '\t\t<string>remote-notification</string>\n\t\t<string>fetch</string>\n\t</array>\n')
    if add:
        i = s.rindex('</dict>')
        s = s[:i] + add + s[i:]
        plist.write_text(s)
        say('iPhone: microphone, camera, photos, speech and background-call settings added')
else:
    say('iPhone: no ios/ folder — skipped')

pod = ROOT / 'ios/Podfile'
if pod.exists():
    s = pod.read_text()
    s2 = re.sub(r"#?\s*platform :ios, '[\d.]+'", "platform :ios, '13.0'", s, count=1)
    if s2 != s:
        pod.write_text(s2)
        say('iPhone: iOS 13 minimum in Podfile')

if not found:
    ok = False
    say('No android/ or ios/ folder — run flutter create first (see README)')
print('Done.' if ok else 'Not finished — see above.')
sys.exit(0 if ok else 1)
