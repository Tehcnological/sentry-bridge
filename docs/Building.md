# Building from source

## Native bridge (Windows, Linux, macOS)
Needs CMake 3.15+ and a C99 compiler (MSVC, GCC or Clang).

```bash
cmake -S . -B build
cmake --build build --config Release
# build/sentry-bridge   (build/Release/sentry-bridge.exe with MSVC)
```

On Windows the C runtime is linked statically, so the exe runs on a clean machine.

## Python bridge
Needs Python 3.10+.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python bridge.py --help
```

## Android app
The Gradle project is in `android/`. It needs JDK 17, the Android SDK, and the NDK (Android Gradle
Plugin installs a missing NDK and CMake automatically).

```bash
cd android
gradle assembleRelease      # Gradle 8.10.2, OpenJDK 17
# app/build/outputs/apk/release/app-release-unsigned.apk (without signing inputs)
```

The app compiles `core/sbcore.c` and `android/app/src/main/cpp/sb_jni.c` for arm64-v8a, armeabi-v7a
and x86_64.

The app targets and compiles against SDK 34 (minimum 26). That is enough for an APK installed
directly; it is not published in Google Play, which requires newer targets. Before raising the target:

- SDK 35 draws apps edge to edge, so the WebView needs window insets for the status and navigation
  bars, and `window.statusBarColor` no longer applies.
- SDK 37 requires the `ACCESS_LOCAL_NETWORK` permission for the receiver and EFB traffic on the
  Sentry WiFi. Do not request it while targeting 36 or lower.
- Raise `compileSdk`/`targetSdk` in `android/app/build.gradle.kts`, add the platform and build
  tools to `$setupPackages` and the required files in `tools\Setup-Android.ps1`, and the host-test
  `android.jar` path in `tools/test_android_diagnostics.py`. Then recheck receiving,
  background operation and output to another device on hardware.

### Signing
Direct Gradle release builds without signing secrets produce an unsigned APK, as F-Droid needs (see
[Reproducible builds](Reproducible-Builds.md)); use `assembleDebug` for a quick installable build. The
local release scripts require your permanent key and verify the APK certificate against the original
release certificate. For local release builds, set `SB_REQUIRE_RELEASE_SIGNING=true` to enforce the
same requirement. Debug-signed builds need an uninstall before replacing an APK signed with another key.

Releases use the project's existing permanent key (`tools/Import-Signing.ps1` reads it from
`SB_SIGNING_DIR`). Never create a new one: APKs signed with another key cannot update installed
copies. An accepted F-Droid reproducible-build recipe can retain that certificate; inclusion is pending.

## iPhone and iPad

To build the app yourself, open `ios/SentryBridge.xcodeproj` in Xcode with the iOS 27 SDK on a Mac, choose your
team under **Signing & Capabilities**, and run it on the device. `python3 tools/build_ios_ipa.py`
builds the same `dist/sentry-bridge.ipa` the release uses. The build setting `SB_FLAVOR` picks
the flavor: `Sideload` (the default and the release) keeps running with background audio; `Lite` has no
silent audio and offers Bridge Session instead (`python3 tools/build_ios_ipa.py --flavor Lite` writes
`dist/sentry-bridge-lite.ipa`, which is not released). Both flavors request the Time Sensitive notification
capability; the IPA is ad-hoc signed only to declare it, with no team or profile. A free Apple ID cannot
sign that capability. For a free-team device build, first run:

```bash
python3 tools/generate_ios_project.py --no-time-sensitive
```

Do not commit that generated project change. Notifications arrive at the normal level.
The app supports devices on iOS/iPadOS 26 or later; the current source uses SDK 27 APIs behind
availability checks. The review build used Xcode 27.1.

### Updating the AltStore source at release

`altsource.json` describes the currently published IPA, not an unreleased branch. PR #13 adds the
When In Use and Always location descriptions and the Time Sensitive notification entitlement.
Until the next release updates the source, `test_published_source_matches_the_app` therefore fails
when run against the new app sources. This is a known release-metadata mismatch, not evidence that
AltStore has failed to install the existing published app.

Build and validate the Sideload IPA from the final release sources, then use `tools/altsource.py update`
with the prepared release bundle and tag (see `python3 tools/altsource.py --help`). Publish the matching
IPA and source update together. Do not change the live permission listing early to match an IPA that
has not been published. AltStore checks the listing against the downloaded app:
[AltStore source permissions](https://faq.altstore.io/developers/make-a-source#app-permissions).
The source-match test must pass against the release sources before the release is considered complete.

## Tests

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

| Test | Needs | Checks |
|---|---|---|
| `test_bridge`, `test_uat`, `test_bridge_uat` | pyModeS | Decoding and tracking against simulated traffic, including UAT with injected byte errors |
| `test_core_parity` | a C compiler | C core output equals `bridge.py` byte for byte, including fuzzed input |
| `test_traffic_semantics`, `test_clock` | pyModeS, a C compiler for C cases | Pressure vs geometric altitude, freshness, address namespaces, NACp and wall-clock corrections |
| `test_cli` | a C compiler | Native bridge end to end over UDP |
| `test_jni` | JDK 17+ and a C compiler | The Android JNI layer equals `bridge.py` byte for byte |
| `test_ctl`, `test_ctl_logs`, `test_ws_probe` | nothing | Tools against fake Sentry servers |

Tests that need a compiler or JDK skip themselves when it is missing. The local release gate requires
zero skips. They find native builds in
`build-c/` or `build/`, or through `SBCORE_LIB` and `SENTRY_BRIDGE_BIN`. Windows JNI replay uses
the CMake-built `sbjni.dll` through `SENTRY_JNI_LIB`; Linux/macOS can compile the wrapper directly.

`tools/smoke_bridge.py <bridge command>` checks any build end to end: it streams simulated traffic
into the bridge and expects five aircraft out.

The development requirements include `reedsolo`, an independent Reed-Solomon implementation, so the
UAT error-correction tests can compare against it.

### Reproducible corruption stress test

`tools/fuzz_core.c` feeds arbitrary bytes, CRC-valid and damaged frames, truncated 1090 messages
and corrupted UAT blocks into the core. It also passes every truncation and seeded mutations of
EFB announcements and receiver settings to the JSON helpers, each in a buffer of exactly its length.
Inputs use a fixed seed and contain no flight data.
On Linux with Clang:

```bash
clang -std=c99 -O1 -g -Wall -Wextra -fsanitize=address,undefined -fno-omit-frame-pointer \
  -Icore tools/fuzz_core.c core/sbcore.c -lm -o build/fuzz-core
ASAN_OPTIONS=halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1 ./build/fuzz-core 100000
# Optional longer run, with an explicit seed:
./build/fuzz-core 20000000 0x5E17B01D
```

Create `build/` first if it does not exist. The local checks below run 100,000 iterations under
the sanitizers. Semantic tests remain necessary alongside it.

## Windows builds

The PowerShell tools require Windows, Python 3.10+, Visual Studio C++ Build Tools, CMake/Ninja,
and JDK 17. `tools/Setup-Android.ps1` installs checksum-verified Android tools under `.local-tools`
after you accept the SDK licenses. The first Android build needs Internet access to cache Gradle
dependencies; subsequent builds default to offline mode.

```powershell
python -m pip install -r requirements-build.txt
.\tools\Build-Windows.ps1 -Python (Get-Command python).Source
.\tools\Test-Local.ps1
.\tools\Build-Android.ps1
```

`Test-Local.ps1` builds and runs the full suite with no skipped tests, desktop JNI replay, native
UDP and packaged-control-tool smoke checks, and 100,000 corruption inputs under MSVC
AddressSanitizer. It records `dist/test-report.json`. This Windows validation does not substitute
for Linux/macOS testing or Linux's UndefinedBehaviorSanitizer coverage.

`android/version-code.txt` holds the release version code. Release builds need the project's
signing key; builds without it produce an unsigned APK (see
[Reproducible Android builds](Reproducible-Builds.md)).

## Local Linux and macOS checks

Install CMake 3.24+, C build tools, and JDK 17+ on the machine being tested. On macOS, Apple's command
line developer tools provide the compiler; set `JAVA_HOME` when using a JDK other than 17.
Then, from the repository directory:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python tools/test_posix.py
```

The tool builds the native bridge, shared core, desktop JNI wrapper, and sanitizer harness.
It runs the complete suite, rejects all skips except the PowerShell publication tests when
PowerShell 7 (`pwsh`) is not installed, checks the native bridge over UDP, and exercises 100,000 corrupted inputs
under AddressSanitizer and UndefinedBehaviorSanitizer. Results are recorded in
`dist/posix-test-report.json`. Use `--report` to choose a different report path. Physical device
networking, suspend and background behavior still need checks on the intended hardware.

## Small-board firmware

On Linux or WSL, `python3 tools/build_firmware.py` builds the ESP32-C3, Pico W and Pico 2 W images
into `dist/` and writes `dist/firmware-report.json`. It expects ESP-IDF 5.4.1 (with its tools),
pico-sdk 2.1.1 and the Arm GNU toolchain 14.2 under `~/fw`, or pass `--tools`. Releases do
not include the images yet. See [Small-board firmware](Firmware.md).
