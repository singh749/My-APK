# Engine Number Scanner (offline Android APK)

Kivy app that photographs (or loads) an engine-number stamp, reads it with a
**real Tesseract OCR engine bundled inside the APK**, lets you correct it, and
keeps a permanent offline history in SQLite. No internet, cloud OCR or API keys.

## Files
| File | Purpose |
|---|---|
| `main.py` | whole app: SQLite history, image processing, OCR, camera/gallery, UI |
| `test_main.py` | unit tests (run in CI before the build) |
| `buildozer.spec` | APK settings, Python packages, Tesseract Android library |
| `requirements.txt` | desktop/CI test dependencies |
| `eng.traineddata` | genuine English Tesseract model (tessdata_fast, 4.1 MB) |
| `.github/workflows/android.yml` | tests + APK build + checks + download artifact |

## How the OCR is packaged (nothing is faked)
`pytesseract` is only a wrapper around a desktop program and does **not** work
on Android. python-for-android also has no Tesseract recipe, so this project uses
a practical alternative:

1. **Engine** - the open-source [Tesseract4Android](https://github.com/adaptech-cz/Tesseract4Android)
   library (`cz.adaptech.tesseract4android:tesseract4android:4.9.0`: Tesseract 5 +
   Leptonica, compiled for Android, Apache-2.0). `buildozer.spec` lists it in
   `android.gradle_dependencies`; **Gradle downloads it at build time** from
   Maven Central (JitPack as fallback), checks it, and packs its `.so` libraries
   and Java classes into the APK. `main.py` calls it through `pyjnius`.
2. **Language data** - `eng.traineddata` from the official
   [tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast) repository,
   SHA-256 `7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2`.
   It is packaged with the app and copied once to private storage on first launch.
   If you delete the file from the repo, the workflow re-downloads it and
   **fails if the SHA-256 does not match**. To get it manually:
   `curl -L -o eng.traineddata https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/eng.traineddata`
   (More accurate but 15 MB: `tessdata_best`, same URL pattern. Any genuine model works.)
3. The workflow then **opens the finished APK** and fails unless it contains the
   language file (byte-identical), the native Tesseract/Leptonica libraries and
   the `TessBaseAPI` Java class.

The installed app downloads nothing and has no INTERNET permission.

## Quick start
### 1. Create the repository
GitHub -> **New repository** -> name `engine-number-scanner` -> Create.
### 2. Upload the files
Upload **everything in this folder** keeping the structure (drag and drop in the
browser; make sure the hidden `.github/workflows/android.yml` and `.gitignore`
are included - "Add file -> Upload files" accepts folders). Commit to branch `main`.
### 3. Run GitHub Actions
The build starts automatically on every push to `main`. To start it manually:
**Actions** tab -> *Build Android APK* -> **Run workflow**. The first build takes
roughly 30-60 minutes (Android SDK/NDK and NumPy/Pillow compile); later builds are
faster thanks to caching. The *Python tests* job must pass before the APK job runs.
### 4. Download the APK
Open the finished run -> **Artifacts** -> `engine-number-scanner-debug-apk`
(a zip containing the `.apk`). Unzip it on your phone or computer.
### 5. Install on the phone
Copy the `.apk` to the phone (USB, Drive, WhatsApp-to-self...), open it, and allow
**Install unknown apps** for the app you opened it from. Debug APKs are signed
with a debug key; Play Protect may ask for confirmation - choose *Install anyway*.
### 6. Test OCR in airplane mode
Turn on Airplane mode (Wi-Fi and mobile data off), open the app, tap **Capture
Engine Image** (or **Upload Image**), crop tightly around the number, press
**Scan Engine Number**. A result appearing proves the engine and language data run
fully offline. Save, close the app, restart the phone, reopen: history remains.

## Using the app
* Photograph the stamp in even light, no glare, number horizontal, as large as
  possible in the frame. Use rotate, zoom and the four crop sliders, then scan.
* Several preprocessing methods and page modes are run; differing readings are
  offered in a drop-down. **Nothing is ever auto-corrected.** Characters that are
  easily confused (O/0, I/1, S/5, B/8, Z/2) are listed in the warning box - compare
  them with the engine. OCR is never 100% accurate.
* History numbers are allocated inside a SQLite transaction (`001`, `002`, ...).
  They are **never reused**: delete 002 and the next scan is 004. *Clear All*
  (with confirmation) also keeps the counter. Newest scans are listed first
  (`newest_first=False` in `HistoryDB.list_scans` flips that).
* Images are kept in the app's private storage (`images/`), removed only with
  their own record.

## Tests
```
pip install -r requirements.txt
python -m pytest -v
```
Covers: sequential numbering, delete without side effects, no number reuse (also
after Clear All), empty/noisy OCR, ambiguous characters left untouched, manual
correction, preprocessing (with and without NumPy), database persistence across
restarts, image cleanup, and - when the `tesseract` program is installed - a real
OCR run with the bundled `eng.traineddata`.

## Troubleshooting
**Build fails**
* Open the failed step's log and read the *first* red error, not the last.
* `numpy` or `pillow` recipe error: delete `numpy,` from `requirements` in
  `buildozer.spec` (the app falls back to Pillow-only processing).
* Gradle cannot resolve `tesseract4android`: re-run (network hiccup); check the
  version exists on Maven Central/JitPack; try another version in `buildozer.spec`.
* "NDK/SDK license" or download errors: re-run; delete the Actions cache
  (Actions -> Caches) if a cached download is corrupt.
* Out of time/space: ARM64 only is built by default. Add `armeabi-v7a` to
  `android.archs` only if you need very old 32-bit phones (build time doubles;
  the Tesseract AAR already contains that architecture).
* `Verify APK contents` fails: the message says which part (language data,
  native libs, or Java class) is missing - that means OCR would not work.

**Permissions** - the camera app is launched through Android's standard intent, so
no CAMERA permission is needed. On Android 9 or older the app asks for storage
permission (the camera saves the photo through MediaStore). If the camera button
reports an error, use **Upload Image** with a photo taken in your camera app.

**"OCR problem" on the home screen** - the message names the cause:
`eng.traineddata missing` (file not uploaded / not in `source.include_exts`),
`Tesseract library is not inside this APK` (Gradle dependency not applied: check
`android.gradle_dependencies` and that the build log shows tesseract4android being
resolved), or `could not load eng.traineddata` (corrupt file: re-download).

**Read crash logs** - `adb logcat | grep -i -E "python|tess|AndroidRuntime"`.

## Honest status
Verified by the author in a Linux environment: unit tests, a headless run of the
whole Kivy UI flow, and OCR with the bundled language file using desktop
Tesseract 5.3.4. **Not verified:** the Buildozer APK build, and everything that
needs a physical phone - camera/gallery intents, pyjnius calls into
Tesseract4Android, permissions, OCR speed/accuracy on real stamped metal, install.
Please report the first build or device error you see; the most likely points of
friction are the Gradle dependency resolution and the camera intent on some phones.
