plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Without signing inputs the release APK is unsigned: F-Droid builds it that way and copies the
// published signature onto it. Tagged releases must supply all signing inputs.
val keystorePath: String? = System.getenv("SB_KEYSTORE_FILE")
val requireReleaseSigning = System.getenv("SB_REQUIRE_RELEASE_SIGNING") == "true"
val signingInputs = listOf("SB_KEYSTORE_FILE", "SB_KEYSTORE_PASSWORD", "SB_KEY_ALIAS", "SB_KEY_PASSWORD")
if (requireReleaseSigning || keystorePath != null) {
    require(signingInputs.all { !System.getenv(it).isNullOrBlank() }) {
        "Permanent release signing requires SB_KEYSTORE_FILE, SB_KEYSTORE_PASSWORD, SB_KEY_ALIAS and SB_KEY_PASSWORD"
    }
}

android {
    namespace = "io.github.tehcnological.sentrybridge"
    compileSdk = 34
    // Reproducible builds: F-Droid rebuilds the APK and must get the same bytes, so pin the tools.
    buildToolsVersion = "34.0.0"
    ndkVersion = "27.0.12077973"
    sourceSets.getByName("main").assets.srcDir(layout.buildDirectory.dir("generated/gui-assets"))

    defaultConfig {
        applicationId = "io.github.tehcnological.sentrybridge"
        minSdk = 26
        targetSdk = 34
        // Local builds and CI use the same tracked counter, independent of Actions runs.
        versionCode = (System.getenv("SB_ANDROID_VERSION_CODE")
            ?: rootProject.file("version-code.txt").readText().trim()).toInt()
        require(versionCode in 1..2100000000) { "Invalid Android version code" }
        versionName = "0.3.0"
        ndk {
            abiFilters += listOf("arm64-v8a", "armeabi-v7a", "x86_64")
        }
        externalNativeBuild {
            cmake {
                cFlags += listOf("-Wall", "-Wextra")
            }
        }
    }

    externalNativeBuild {
        cmake {
            path = file("src/main/cpp/CMakeLists.txt")
            version = "3.22.1"  // pinned for reproducible builds, like the NDK and build tools
        }
    }

    signingConfigs {
        if (keystorePath != null) {
            create("release") {
                storeFile = file(keystorePath)
                storePassword = System.getenv("SB_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("SB_KEY_ALIAS")
                keyPassword = System.getenv("SB_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            vcsInfo.include = false  // the commit file would differ between a Git checkout and a source export
            signingConfig = if (keystorePath != null) signingConfigs.getByName("release") else null
        }
    }

    // Google's encrypted dependency report in the signing block: not reproducible, and F-Droid rejects it.
    dependenciesInfo {
        includeInApk = false
        includeInBundle = false
    }

    lint {
        // Lint is not a release gate for this sideloaded app (its target-SDK date rules would block builds)
        checkReleaseBuilds = false
        abortOnError = false
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

val syncGuiAssets by tasks.registering(Sync::class) {
    from(rootProject.file("../web")) { include("app.html", "app.css", "app.js", "privacy.html", "legal.html", "legal.js") }
    from(rootProject.file("../config")) { include("android-simulator.json") }
    from(rootProject.file("../LICENSE"))
    from(rootProject.file("../NOTICE.txt"))
    from(rootProject.file("../notices")) { into("notices") }
    into(layout.buildDirectory.dir("generated/gui-assets"))
}
tasks.named("preBuild").configure { dependsOn(syncGuiAssets) }
