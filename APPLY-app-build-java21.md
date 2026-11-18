# TapTap app build fix: "lintVitalAnalyzeRelease … NoSuchMethodError: java.util.List.removeLast()"

Built on master **9ca66c2**. Only the GitHub build of the phone app changes: no server, migration or app-code change.

## What went wrong

The GitHub job *TapTap mobile app → Android APK* failed after about 6 minutes:

    Execution failed for task ':firebase_core:lintVitalAnalyzeRelease'
    > Unexpected failure during lint analysis …
      'java.lang.Object java.util.List.removeLast()'

* The workflow builds with Flutter's **latest stable** (now 3.50) and makes `android/` fresh with `flutter create`.
  Current Flutter makes it with **Gradle 9.5 and Android Gradle Plugin 9.3**.
* That plugin's **lint** (the "lint vital" check a release build runs, here on the `firebase_core` plugin's own code)
  calls `List.removeLast()`, a method that exists only from **Java 21**.
* The workflow ran Gradle on **Java 17** (`setup-java … java-version: '17'`), so lint crashed and took the build with
  it. Nothing in TapTap's code caused it: the next build of any version would have failed the same way.

## The fix

1. **`.github/workflows/mobile-app.yml`:** the build runs on **Java 21**, the Java Android Studio ships. Flutter is
   told to use it (`flutter config --jdk-dir "$JAVA_HOME"`), and the log prints both versions.
2. **`mobile/tool/configure_platforms.py`:** release builds skip "lint vital" (`lint { checkReleaseBuilds = false }`
   in `android/app/build.gradle.kts`). That step analyses the plugins' own Android code (Firebase, call screen,
   WebRTC…) and has nothing in this app to stop a release for. A future bug in the lint tool can't block the APK any
   more. `flutter analyze` and `flutter test` still run on every build.
3. **`mobile/README.md`:** one note: build locally with Java 21.

Checked here: the workflow file parses, and Java 21 is set. I ran `configure_platforms.py` against an `android/` folder
made from Flutter's current templates (Gradle 9.5.0, AGP 9.3.1, Kotlin 2.4.20, fetched from Flutter's GitHub). It adds
the lint setting inside `android { }`, says "Done.", and a second run adds nothing twice. **Not built here:** the
Flutter SDK, Android SDK and Gradle downloads are not reachable from this workspace. The GitHub run is the real test.

## Apply

    unzip -o taptap-app-build-java21.zip -d /tmp && cp -r /tmp/taptap/. /path/to/your/taptap/
    git add .github/workflows/mobile-app.yml mobile/tool/configure_platforms.py mobile/README.md
    git commit -m "App build: Java 21, release lint off" && git push

The push touches `mobile/` and the workflow, so the Android job starts by itself. Its *Flutter and Java versions* step
should show `openjdk version "21…"`, and *Permissions and settings* should show
"Android: release builds skip lint vital (plugins' lint) in build.gradle.kts". The APK is under the run's
**Artifacts**.

Note the `cp -r /tmp/taptap/.`: the trailing `/.` copies the hidden `.github` folder too.

## Not a failure, for later

The log also warns that six plugins (firebase_core, flutter_callkit_incoming, flutter_webrtc, package_info_plus,
record_android, wakelock_plus) still apply the Kotlin Gradle Plugin themselves, and that *future* Flutter versions
will refuse that. Today it is only a warning. When it becomes an error, update those plugins in `pubspec.yaml`, or
pin Flutter in the workflow (`flutter-version: 3.50.0` instead of `channel: stable`) until they are updated.
