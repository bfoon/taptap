#!/usr/bin/env python3
"""Run once after `flutter create` (see README): adds what calls, the camera, voice input and push need.

    cd mobile
    flutter create --org gm.easysolutions --project-name taptap_app --platforms android,ios .
    python3 tool/configure_platforms.py

Safe to run again: nothing is added twice.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
ok = True


def say(msg):
    print('  ' + msg)


# ── Android ──
man = ROOT / 'android/app/src/main/AndroidManifest.xml'
if man.exists():
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
    man.write_text(s)
else:
    ok = False
    say('Android: no AndroidManifest.xml — run flutter create first')

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
if gs.exists():
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
    ok = False
    say('iPhone: no Info.plist — run flutter create first')

pod = ROOT / 'ios/Podfile'
if pod.exists():
    s = pod.read_text()
    s2 = re.sub(r"#?\s*platform :ios, '[\d.]+'", "platform :ios, '13.0'", s, count=1)
    if s2 != s:
        pod.write_text(s2)
        say('iPhone: iOS 13 minimum in Podfile')

print('Done.' if ok else 'Some files were missing — see above.')
sys.exit(0 if ok else 1)
