[app]
title = Engine Number Scanner
package.name = enginenumberscanner
package.domain = org.enginescanner
source.dir = .
source.include_exts = py,traineddata
source.exclude_dirs = .github,.buildozer,bin,tests,__pycache__,.pytest_cache
source.exclude_patterns = test_*.py,README.md
version = 1.0.0

# sqlite3 is needed for the SQLite module, pyjnius to call the Java Tesseract
# library. If the numpy recipe ever fails to build, delete "numpy," below:
# main.py falls back to Pillow-only image processing automatically.
requirements = python3,kivy,sqlite3,pillow,numpy,pyjnius,android

orientation = portrait
fullscreen = 0

# --- Android ---------------------------------------------------------------
# No CAMERA permission is declared on purpose: the app asks the phone's own
# camera app to take the picture, which needs no permission. Storage permission
# is only needed on Android 9 and older (the camera photo is saved via MediaStore).
android.permissions = (name=android.permission.WRITE_EXTERNAL_STORAGE;maxSdkVersion=28)
android.api = 33
android.minapi = 24
android.ndk = 28c
android.accept_sdk_license = True
android.archs = arm64-v8a
android.enable_androidx = True

# Two offline OCR engines, fetched at BUILD time by Gradle and bundled in the APK:
#  - Tesseract (Tesseract4Android)  - Google ML Kit text recognition (bundled model)
# The installed app never downloads anything.
android.gradle_dependencies = cz.adaptech.tesseract4android:tesseract4android:4.9.0, com.google.mlkit:text-recognition:16.0.1
android.add_gradle_repositories = maven { url 'https://jitpack.io' }

# Pin python-for-android to an exact commit (v2026.05.09 fails on charset_normalizer Android wheels; fixed on develop).
p4a.branch = develop
p4a.commit = 8ef9d1dbbf769d2c14681dbc3c116c85e2e93bae

[buildozer]
log_level = 2
warn_on_root = 1
