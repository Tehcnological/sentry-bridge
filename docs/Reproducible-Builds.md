# Reproducible Android builds

The Android build pins tools and removes known sources of nondeterminism. Earlier
Windows/WSL comparisons matched apart from signatures; every new submitted release
needs its own comparison. F-Droid inclusion is pending. If F-Droid accepts an
upstream-signed reproducible recipe and reproduces the submitted APK, installs can
share its signing identity. This document does not guarantee acceptance.

## What makes it reproducible

- Pinned tools: Gradle 8.10.2 (`android/gradle/wrapper/gradle-wrapper.properties`), Android Gradle
  Plugin and Kotlin (`android/build.gradle.kts`), build tools 34.0.0, NDK 27.0.12077973, CMake 3.22.1
  and JDK 17.
- The native library carries no build paths (`-ffile-prefix-map`), no build ID and no compiler
  identity string (`-fno-ident`: the Windows and Linux NDK compilers name themselves differently).
- The APK carries no Git commit file (`vcsInfo`) and no Google dependency report (`dependenciesInfo`),
  so a source export and a Git checkout build the same APK.
- A release build without signing inputs is unsigned, as F-Droid builds it.

## Check a build

1. Build the signed release as usual (`tools/Prepare-Release.ps1`, or `tools/Build-Android.ps1`).
2. From the same commit in any other folder, build without signing inputs and with the same version
   code: `gradle --project-dir android assembleRelease` (with `SB_ANDROID_VERSION_CODE` set when the
   signed build used one). The output is `android/app/build/outputs/apk/release/app-release-unsigned.apk`.
3. Compare:

   ```
   python tools/apk_matches.py app-release-unsigned.apk sentry-bridge.apk
   ```

   It passes when the signed APK is exactly the unsigned build plus its signing block.

## Checked so far

- Windows: signed builds from different folders are identical; an unsigned direct Gradle build
  matches the signed APK apart from its signature.
- Linux (Ubuntu in WSL, OpenJDK 17.0.20.1, the same SDK packages from `sdkmanager`): an unsigned
  build matches the signed Windows release APK apart from its signature.
- F-Droid's own build tool (`fdroid build`, fdroidserver 2.4.5) reproduced the v0.2.0 release APK and
  verified its signer.
