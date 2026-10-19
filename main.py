"""Engine Number Scanner - offline Android app (Kivy + Tesseract).

Layout of this file
  1. Core logic  (no Kivy needed, unit-tested): SQLite history, image
     preprocessing, OCR pipeline, warnings.
  2. OCR backends: Tesseract4Android through pyjnius (Android) and pytesseract
     (desktop, optional).
  3. Android camera / gallery glue.
  4. Kivy user interface.

The app never uses the internet. OCR results are suggestions only and must be
checked by the user against the engine.
"""
import difflib
import os
import re
import shutil
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from PIL import Image, ImageChops, ImageFilter, ImageOps

try:  # NumPy is optional: Pillow-only fallbacks exist for every use.
    import numpy as np
except Exception:  # pragma: no cover
    np = None

APP_NAME = "Engine Number Scanner"
ANDROID = "ANDROID_ARGUMENT" in os.environ or "ANDROID_STORAGE" in os.environ

# Characters that OCR commonly confuses. They are only ever REPORTED to the
# user, never replaced automatically.
AMBIGUOUS = {"O": "0", "0": "O", "I": "1", "1": "I", "S": "5", "5": "S",
             "B": "8", "8": "B", "Z": "2", "2": "Z"}

LOW_CONFIDENCE = 70  # Tesseract mean confidence (0-100) below this -> warning
MIN_LENGTH = 5


# =============================================================================
# 1. CORE LOGIC
# =============================================================================
def now_string():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def format_history_no(n):
    return "%03d" % int(n)


class HistoryDB:
    """SQLite scan history with never-reused, sequential history numbers.

    The next number comes from a counter stored in the `meta` table. It is
    read and incremented inside one BEGIN IMMEDIATE transaction together with
    the INSERT, so numbers are unique and deleting rows never lowers it.
    """

    def __init__(self, db_path, image_dir):
        self.db_path = db_path
        self.image_dir = image_dir
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        os.makedirs(image_dir, exist_ok=True)
        self.conn = sqlite3.connect(db_path, isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS scans ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " history_no INTEGER NOT NULL UNIQUE,"
            " engine_number TEXT NOT NULL,"
            " raw_ocr TEXT NOT NULL DEFAULT '',"
            " confidence REAL,"
            " warnings TEXT NOT NULL DEFAULT '',"
            " image_path TEXT,"
            " created_at TEXT NOT NULL)")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS meta ("
            " key TEXT PRIMARY KEY, value INTEGER NOT NULL)")
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) "
            "SELECT 'last_history_no', COALESCE(MAX(history_no), 0) FROM scans")

    def close(self):
        self.conn.close()

    # -- writes ---------------------------------------------------------------
    def add_scan(self, engine_number, raw_ocr="", confidence=None,
                 warnings=None, image_src=None):
        """Save a scan; returns (row_id, history_no)."""
        engine_number = (engine_number or "").strip()
        if not engine_number:
            raise ValueError("Engine number is empty.")
        dest = None
        if image_src and os.path.exists(image_src):
            dest = os.path.join(self.image_dir, "scan_%s.jpg" % uuid.uuid4().hex)
            shutil.copyfile(image_src, dest)
        c = self.conn
        try:
            c.execute("BEGIN IMMEDIATE")
            last = c.execute(
                "SELECT value FROM meta WHERE key='last_history_no'").fetchone()[0]
            number = last + 1
            c.execute("UPDATE meta SET value=? WHERE key='last_history_no'",
                      (number,))
            cur = c.execute(
                "INSERT INTO scans(history_no, engine_number, raw_ocr,"
                " confidence, warnings, image_path, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (number, engine_number, raw_ocr or "", confidence,
                 "\n".join(warnings or []), dest, now_string()))
            c.execute("COMMIT")
        except Exception:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if dest and os.path.exists(dest):
                os.remove(dest)
            raise
        return cur.lastrowid, number

    def update_engine_number(self, scan_id, engine_number):
        engine_number = (engine_number or "").strip()
        if not engine_number:
            raise ValueError("Engine number is empty.")
        self.conn.execute("UPDATE scans SET engine_number=? WHERE id=?",
                          (engine_number, scan_id))

    def delete_scan(self, scan_id):
        row = self.get_scan(scan_id)
        if row is None:
            return False
        self.conn.execute("DELETE FROM scans WHERE id=?", (scan_id,))
        self._remove_image(row["image_path"])
        return True

    def clear_all(self):
        """Delete every record and its own image. The counter is NOT reset,
        so history numbers are still never reused."""
        rows = self.conn.execute("SELECT image_path FROM scans").fetchall()
        self.conn.execute("DELETE FROM scans")
        for r in rows:
            self._remove_image(r["image_path"])

    def _remove_image(self, path):
        """Remove an image only if it lives in our image folder and no other
        record still points to it."""
        if not path:
            return
        try:
            folder = os.path.abspath(self.image_dir)
            full = os.path.abspath(path)
            if os.path.commonpath([folder, full]) != folder:
                return
            used = self.conn.execute(
                "SELECT COUNT(*) FROM scans WHERE image_path=?", (path,)
            ).fetchone()[0]
            if used == 0 and os.path.exists(full):
                os.remove(full)
        except (OSError, ValueError):
            pass

    # -- reads ----------------------------------------------------------------
    def get_scan(self, scan_id):
        return self.conn.execute(
            "SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()

    def count(self):
        return self.conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0]

    def last_history_no(self):
        return self.conn.execute(
            "SELECT value FROM meta WHERE key='last_history_no'").fetchone()[0]

    def list_scans(self, query=None, newest_first=True):
        order = "DESC" if newest_first else "ASC"
        query = (query or "").strip()
        if not query:
            return self.conn.execute(
                "SELECT * FROM scans ORDER BY history_no %s" % order).fetchall()
        like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace(
            "_", "\\_") + "%"
        return self.conn.execute(
            "SELECT * FROM scans WHERE engine_number LIKE ? ESCAPE '\\'"
            " OR created_at LIKE ? ESCAPE '\\'"
            " OR printf('%03d', history_no) LIKE ? ESCAPE '\\'"
            " ORDER BY history_no " + order, (like, like, like)).fetchall()


# ---- image processing --------------------------------------------------------
def load_image(path, max_side=2400):
    """Open a photo, apply EXIF rotation, convert to RGB, limit its size."""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img).convert("RGB")
    longest = max(img.size)
    if longest > max_side:
        k = max_side / float(longest)
        img = img.resize((max(1, int(img.width * k)),
                          max(1, int(img.height * k))), Image.LANCZOS)
    return img


def rotate_image(img, degrees_clockwise):
    return img.rotate(-degrees_clockwise, expand=True) if degrees_clockwise % 360 else img


def crop_fraction(img, left=0.0, top=0.0, right=0.0, bottom=0.0):
    """Remove a fraction of each side (0..0.45) and return the remaining part."""
    clamp = lambda v: min(max(float(v), 0.0), 0.45)
    l, t, r, b = clamp(left), clamp(top), clamp(right), clamp(bottom)
    w, h = img.size
    box = (int(w * l), int(h * t), int(w * (1 - r)), int(h * (1 - b)))
    if box[2] - box[0] < 8 or box[3] - box[1] < 8:
        return img
    return img.crop(box)


def otsu_threshold(gray):
    hist = gray.histogram()[:256]
    total = sum(hist)
    if total == 0:
        return 128
    sum_all = sum(i * h for i, h in enumerate(hist))
    w_b = sum_b = 0
    best, best_t = -1.0, 128
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        var = w_b * w_f * (m_b - m_f) ** 2
        if var > best:
            best, best_t = var, t
    return best_t


def adaptive_threshold(gray, block=31, c=8):
    """Local-mean threshold: handles uneven lighting and shiny metal."""
    block = block | 1
    if np is not None:
        a = np.asarray(gray, dtype=np.float64)
        pad = block // 2
        p = np.pad(a, pad, mode="edge")
        ii = np.pad(np.cumsum(np.cumsum(p, axis=0), axis=1), ((1, 0), (1, 0)))
        h, w = a.shape
        s = (ii[block:block + h, block:block + w] - ii[0:h, block:block + w]
             - ii[block:block + h, 0:w] + ii[0:h, 0:w])
        out = np.where(a > s / float(block * block) - c, 255, 0).astype(np.uint8)
        return Image.fromarray(out, "L")
    blurred = gray.filter(ImageFilter.BoxBlur(block // 2))
    diff = ImageChops.subtract(blurred, gray)  # max(0, local_mean - pixel)
    return diff.point(lambda v: 255 if v < c else 0).convert("L")


def relief_variants(gray):
    """Edge-based views for engraved / embossed / punched characters on shiny
    metal, where brightness alone does not separate the digits from the
    background. Pure Pillow (difference of two blurs)."""
    edges = ImageOps.autocontrast(ImageChops.difference(
        gray.filter(ImageFilter.GaussianBlur(1)),
        gray.filter(ImageFilter.GaussianBlur(4))), cutoff=1)
    t = otsu_threshold(edges)
    bold = edges.point(lambda v, t=t: 255 if v > t else 0).filter(
        ImageFilter.MaxFilter(3)).filter(ImageFilter.MedianFilter(3))
    soft = edges.filter(ImageFilter.GaussianBlur(1.5)).point(
        lambda v: min(255, v * 2))
    return ImageOps.invert(bold), ImageOps.invert(soft)


def preprocess_variants(img, target=1600, line_height=110):
    """Return {name: grayscale PIL image} - several views for OCR.

    A wide, short image (a tightly cropped line of characters) is scaled so
    the characters are about `line_height` px tall; anything else is scaled so
    its longest side is about `target` px."""
    gray = ImageOps.grayscale(img)
    if gray.width / float(gray.height) >= 2.5:
        k = min(max(line_height / float(gray.height), 0.3), 4.0)
    else:
        k = min(max(target / float(max(gray.size)), 0.5), 3.0)
    if abs(k - 1.0) > 0.05:
        gray = gray.resize((max(1, int(gray.width * k)),
                            max(1, int(gray.height * k))), Image.LANCZOS)
    plain = ImageOps.autocontrast(gray, cutoff=1)
    enhanced = ImageOps.autocontrast(plain.filter(
        ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3)), cutoff=1)
    blurred = ImageOps.autocontrast(gray.filter(ImageFilter.GaussianBlur(1)),
                                    cutoff=1)
    t = otsu_threshold(blurred)
    relief, relief_soft = relief_variants(gray)
    return {
        "gray": plain,
        "enhanced": enhanced,
        "otsu": blurred.point(lambda v, t=t: 255 if v > t else 0).convert("L"),
        "adaptive": adaptive_threshold(plain),
        "inverted": ImageOps.invert(enhanced),
        "relief": relief,
        "relief_soft": relief_soft,
    }


# ---- OCR post-processing -----------------------------------------------------
@dataclass
class OcrResult:
    text: str = ""
    confidence: float = 0.0
    raw_text: str = ""
    alternatives: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def extract_candidates(text):
    """Split OCR text into engine-number-like tokens (upper case, A-Z 0-9 -).
    No character is substituted; only case and stray symbols are dropped."""
    out = []
    for token in re.split(r"[\s|,;:]+", text or ""):
        token = re.sub(r"[^A-Za-z0-9\-]", "", token).strip("-").upper()
        if len(token) >= 4 and any(ch.isdigit() for ch in token):
            out.append(token)
    return out


def uncertain_char_warnings(text):
    """Report characters that are easy to confuse. Never modifies the text."""
    notes = []
    for ch in "O0I1S5B8Z2":
        pos = [str(i + 1) for i, c in enumerate(text.upper()) if c == ch]
        if pos:
            notes.append("'%s' at position %s could be '%s'"
                         % (ch, ", ".join(pos), AMBIGUOUS[ch]))
    return notes


def _score(cand, conf):
    s = conf / 100.0 + 0.1 * any(c.isdigit() for c in cand) + 0.15 * (
        min(len(cand), 17) / 17.0)
    return s - (0.4 if len(cand) > 20 else 0.0)


# Which preprocessing variant is run with which Tesseract page-segmentation
# mode (6 = block of text, 7 = single line, 11 = sparse text).
PASS_PLAN = [("enhanced", 6), ("enhanced", 7), ("otsu", 6),
             ("adaptive", 6), ("inverted", 6), ("gray", 11),
             ("relief", 7), ("relief", 8), ("relief_soft", 7),
             ("relief_soft", 8)]


def run_ocr(img, backend, plan=PASS_PLAN, progress=None):
    """Run every pass in `plan` and combine the readings.

    `backend.recognize(pil_image, psm)` must return (text, confidence 0-100).
    Individual pass failures are skipped; if every pass fails the last error is
    raised so the UI can show it.
    """
    variants = preprocess_variants(img)
    votes = {}   # candidate -> [total_score, passes_agreeing]
    raw_parts, confs, last_error, ok_passes = [], [], None, 0
    for i, (name, psm) in enumerate(plan):
        if progress:
            progress(i, len(plan))
        try:
            text, conf = backend.recognize(
                ImageOps.expand(variants[name], 20, 255), psm)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
        ok_passes += 1
        text = text or ""
        if text.strip():
            raw_parts.append("[%s/psm%d] %s" % (name, psm, text.strip()))
        confs.append(conf or 0)
        for cand in extract_candidates(text):
            v = votes.setdefault(cand, [0.0, 0])
            v[0] += _score(cand, conf or 0)
            v[1] += 1
    if ok_passes == 0 and last_error is not None:
        raise last_error

    result = OcrResult(raw_text="\n".join(raw_parts))
    if not votes:
        result.warnings.append(
            "No engine number was recognized. Retake the photo with more light, "
            "less glare, hold the phone steady, or crop closer to the number "
            "and press Scan Again. You can also type the number manually.")
        return result

    ranked = sorted(votes.items(), key=lambda kv: kv[1][0] + 0.2 * kv[1][1],
                    reverse=True)
    best, (_, agree) = ranked[0]
    result.text = best
    result.confidence = round(sum(confs) / len(confs), 1) if confs else 0.0
    result.alternatives = [c for c, _ in ranked[1:6]
                           if difflib.SequenceMatcher(None, best, c).ratio() >= 0.6]
    if result.alternatives:
        result.warnings.append(
            "Different scan methods read this number differently: "
            + ", ".join(result.alternatives[:3]) + ". Check carefully.")
    elif agree == 1 and len(plan) > 1:
        result.warnings.append("Only one scan method found this number.")
    if len(best) < MIN_LENGTH:
        result.warnings.append("The result is very short; it may be incomplete.")
    if result.confidence < LOW_CONFIDENCE:
        result.warnings.append("Low OCR confidence (%.0f%%)." % result.confidence)
    notes = uncertain_char_warnings(best)
    if notes:
        result.warnings.append(
            "Please verify easily confused characters: " + "; ".join(notes) + ".")
    return result


# =============================================================================
# 2. OCR BACKENDS
# =============================================================================
class OcrUnavailable(Exception):
    pass


TESS_CLASS = "com.googlecode.tesseract.android.TessBaseAPI"
FILE_CLASS = "java.io.File"

# Java classes already looked up through pyjnius, keyed by dotted name.
_java_classes = {}
_java_classes_lock = threading.Lock()


def java_class(name):
    """Return the pyjnius wrapper class for Java class `name` (cached).

    The lookup itself uses JNI FindClass, which - see prepare_android_classes
    - only works for the app's own classes on a thread that has a Java frame
    below it (the Kivy main thread). Caching the resulting class object lets
    the OCR worker thread reuse it safely: pyjnius keeps Java class and object
    references as JNI global references, so they stay valid across threads.
    """
    with _java_classes_lock:
        cls = _java_classes.get(name)
        if cls is not None:
            return cls
        from jnius import autoclass
        cls = autoclass(name)
        _java_classes[name] = cls
        return cls


def prepare_android_classes():
    """Load the Java classes used by OCR. MUST run on the Kivy main thread.

    Why this is not done inside the OCR worker: Android's JNI FindClass uses
    the class loader of the nearest Java frame on the stack. The Kivy main
    thread is started from the Java activity, so it sees the app's own
    classes - including ones from `android.gradle_dependencies` such as
    Tesseract4Android. A plain Python worker thread (the scan runs in one)
    is attached with AttachCurrentThread and has no Java frame, so FindClass
    falls back to the system class loader and fails with
    `DexPathList[[directory "."], ...]` / ClassNotFoundException even though
    the class is inside the APK. Resolving the class here once avoids that.
    """
    try:
        tess = java_class(TESS_CLASS)
    except Exception as exc:  # noqa: BLE001
        raise OcrUnavailable(
            "%s could not be loaded from the APK. The Tesseract4Android "
            "library is missing (check android.gradle_dependencies and the "
            "build log). Java said: %s" % (TESS_CLASS, exc))
    try:
        java_class(FILE_CLASS)   # boot class; resolved here for consistency
    except Exception:  # noqa: BLE001
        pass
    print("OCR engine ready: %s loaded" % TESS_CLASS)   # visible in logcat
    return tess


class AndroidTesseract:
    """Real Tesseract (Tesseract4Android AAR) driven through pyjnius."""

    def __init__(self, data_root, cache_dir, tess_class=None):
        self.data_root = data_root          # contains tessdata/eng.traineddata
        self.cache_dir = cache_dir
        self.tess_class = tess_class        # resolved on the main thread
        self.api = None
        self.lock = threading.Lock()

    def _init(self):
        if self.api is not None:
            return
        data = os.path.join(self.data_root, "tessdata", "eng.traineddata")
        if not os.path.exists(data) or os.path.getsize(data) < 1000000:
            raise OcrUnavailable("eng.traineddata is missing from the app.")
        try:
            Tess = self.tess_class or java_class(TESS_CLASS)
        except Exception as exc:  # noqa: BLE001
            raise OcrUnavailable(
                "Tesseract (%s) could not be loaded: %s" % (TESS_CLASS, exc))
        api = Tess()
        if not api.init(self.data_root, "eng"):
            raise OcrUnavailable("Tesseract could not load eng.traineddata.")
        self.api = api

    def recognize(self, pil_img, psm):
        File = java_class(FILE_CLASS)
        with self.lock:
            self._init()
            path = os.path.join(self.cache_dir, "ocr_%s.png" % uuid.uuid4().hex)
            pil_img.save(path)
            try:
                self.api.setPageSegMode(int(psm))
                self.api.setImage(File(path))
                text = self.api.getUTF8Text() or ""
                conf = float(self.api.meanConfidence())
            finally:
                try:
                    self.api.clear()
                except Exception:  # noqa: BLE001
                    pass
                if os.path.exists(path):
                    os.remove(path)
        return text, conf


class DesktopTesseract:
    """Optional desktop backend (needs the tesseract program + pytesseract)."""

    def __init__(self, tessdata_dir=None):
        try:
            import pytesseract
        except Exception as exc:  # noqa: BLE001
            raise OcrUnavailable("pytesseract is not installed (%s)." % exc)
        self.pt = pytesseract
        self.cfg = ("--tessdata-dir %s " % tessdata_dir) if tessdata_dir else ""

    def recognize(self, pil_img, psm):
        cfg = "%s--psm %d" % (self.cfg, psm)
        data = self.pt.image_to_data(pil_img, lang="eng", config=cfg,
                                     output_type=self.pt.Output.DICT)
        words = [(w, float(c)) for w, c in zip(data["text"], data["conf"])
                 if w.strip() and float(c) >= 0]
        text = self.pt.image_to_string(pil_img, lang="eng", config=cfg)
        conf = sum(c for _, c in words) / len(words) if words else 0.0
        return text, conf


def install_language_data(app_dir, data_root):
    """Copy eng.traineddata from the APK's code folder to a real folder that
    Tesseract can read (done once, no internet)."""
    src = os.path.join(app_dir, "eng.traineddata")
    dst_dir = os.path.join(data_root, "tessdata")
    dst = os.path.join(dst_dir, "eng.traineddata")
    if not os.path.exists(src):
        raise OcrUnavailable("eng.traineddata was not packaged in the app.")
    os.makedirs(dst_dir, exist_ok=True)
    if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(src):
        shutil.copyfile(src, dst)
    return dst


# =============================================================================
# 3. ANDROID CAMERA / GALLERY (pyjnius)
# =============================================================================
class AndroidMedia:
    """Camera capture and gallery picking through standard Android intents."""

    REQ_CAMERA, REQ_GALLERY, RESULT_OK = 7001, 7002, -1

    def __init__(self, cache_dir, on_image, on_error):
        from android import activity
        from jnius import autoclass
        self.cache_dir, self.on_image, self.on_error = cache_dir, on_image, on_error
        self.act = autoclass("org.kivy.android.PythonActivity").mActivity
        self.camera_uri = None
        activity.bind(on_activity_result=self._on_result)

    def capture(self):
        try:
            from jnius import autoclass, cast
            Intent = autoclass("android.content.Intent")
            Store = autoclass("android.provider.MediaStore")
            Media = autoclass("android.provider.MediaStore$Images$Media")
            Values = autoclass("android.content.ContentValues")
            sdk = autoclass("android.os.Build$VERSION").SDK_INT
            values = Values()
            values.put("_display_name", "engine_%s.jpg" % uuid.uuid4().hex[:8])
            values.put("mime_type", "image/jpeg")
            if sdk >= 29:
                values.put("relative_path", "Pictures/EngineNumberScanner")
            uri = self.act.getContentResolver().insert(
                Media.EXTERNAL_CONTENT_URI, values)
            if uri is None:
                raise RuntimeError("Could not create a photo file.")
            self.camera_uri = uri
            intent = Intent(Store.ACTION_IMAGE_CAPTURE)
            intent.putExtra(Store.EXTRA_OUTPUT,
                            cast("android.os.Parcelable", uri))
            self.act.startActivityForResult(intent, self.REQ_CAMERA)
        except Exception as exc:  # noqa: BLE001
            self.on_error("Camera could not be opened: %s\nUse Upload Image "
                          "instead." % exc)

    def pick(self):
        try:
            from jnius import autoclass
            Intent = autoclass("android.content.Intent")
            intent = Intent(Intent.ACTION_GET_CONTENT)
            intent.setType("image/*")
            intent.addCategory(Intent.CATEGORY_OPENABLE)
            self.act.startActivityForResult(intent, self.REQ_GALLERY)
        except Exception as exc:  # noqa: BLE001
            self.on_error("Gallery could not be opened: %s" % exc)

    def _copy_uri(self, uri):
        resolver = self.act.getContentResolver()
        pfd = resolver.openFileDescriptor(uri, "r")
        fd = pfd.detachFd()
        dest = os.path.join(self.cache_dir, "incoming_%s.jpg" % uuid.uuid4().hex)
        with os.fdopen(fd, "rb") as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)
        return dest

    def _on_result(self, request, result, intent):
        try:
            if request == self.REQ_CAMERA:
                uri, self.camera_uri = self.camera_uri, None
                if uri is None:
                    return
                if result == self.RESULT_OK:
                    path = self._copy_uri(uri)
                    self.on_image(path)
                try:  # remove our temporary gallery entry; image stays private
                    self.act.getContentResolver().delete(uri, None, None)
                except Exception:  # noqa: BLE001
                    pass
            elif request == self.REQ_GALLERY and result == self.RESULT_OK \
                    and intent is not None:
                self.on_image(self._copy_uri(intent.getData()))
        except Exception as exc:  # noqa: BLE001
            self.on_error("Could not read the image: %s" % exc)


# =============================================================================
# 4. KIVY USER INTERFACE
# =============================================================================
def build_app():  # noqa: C901 - one flat function keeps the project to one file
    from io import BytesIO

    from kivy.app import App
    from kivy.clock import Clock
    from kivy.core.clipboard import Clipboard
    from kivy.core.image import Image as CoreImage
    from kivy.core.window import Window
    from kivy.graphics import Color, Line, Rectangle
    from kivy.graphics.transformation import Matrix
    from kivy.metrics import dp, sp
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.button import Button
    from kivy.uix.floatlayout import FloatLayout
    from kivy.uix.gridlayout import GridLayout
    from kivy.uix.image import Image as KvImage
    from kivy.uix.label import Label
    from kivy.uix.modalview import ModalView
    from kivy.uix.popup import Popup
    from kivy.uix.scatter import Scatter
    from kivy.uix.screenmanager import NoTransition, Screen, ScreenManager
    from kivy.uix.scrollview import ScrollView
    from kivy.uix.slider import Slider
    from kivy.uix.spinner import Spinner
    from kivy.uix.stencilview import StencilView
    from kivy.uix.textinput import TextInput
    from kivy.uix.widget import Widget
    from kivy.utils import escape_markup

    NAVY = (0.05, 0.14, 0.32, 1)
    NAVY_LIGHT = (0.16, 0.30, 0.55, 1)
    WHITE = (1, 1, 1, 1)
    GRAY = (0.93, 0.94, 0.96, 1)
    GRAY_DARK = (0.45, 0.48, 0.55, 1)
    RED = (0.75, 0.15, 0.15, 1)
    WARN_BG = (1.0, 0.95, 0.80, 1)
    WARN_TXT = (0.45, 0.28, 0.0, 1)
    Window.clearcolor = GRAY

    def paint(widget, rgba):
        with widget.canvas.before:
            Color(*rgba)
            rect = Rectangle(pos=widget.pos, size=widget.size)
        widget.bind(pos=lambda w, v: setattr(rect, "pos", v),
                    size=lambda w, v: setattr(rect, "size", v))

    class Btn(Button):
        def __init__(self, text, kind="primary", **kw):
            colors = {"primary": NAVY, "secondary": NAVY_LIGHT,
                      "danger": RED, "light": WHITE}
            super().__init__(text=text, background_normal="", background_down="",
                             background_color=colors[kind],
                             color=NAVY if kind == "light" else WHITE,
                             font_size=sp(16), bold=True, **kw)

    class Txt(Label):
        """Wrapping label."""
        def __init__(self, text="", size=16, color=NAVY, bold=False, **kw):
            super().__init__(text=text, font_size=sp(size), color=color,
                             bold=bold, halign="left", valign="middle",
                             markup=True, **kw)
            self.bind(width=lambda w, v: setattr(w, "text_size", (v, None)))
            self.bind(texture_size=self._fit)

        def _fit(self, w, ts):
            if self.size_hint_y is None:
                self.height = max(ts[1], dp(24))

    def header(title, back=None, right=None):
        bar = BoxLayout(size_hint_y=None, height=dp(56), padding=dp(6),
                        spacing=dp(6))
        paint(bar, NAVY)
        if back:
            bar.add_widget(Btn("Back", "secondary", size_hint_x=None,
                               width=dp(80), on_press=lambda *_: back()))
        bar.add_widget(Label(text=title, font_size=sp(19), bold=True, color=WHITE))
        if right:
            bar.add_widget(right)
        return bar

    def notify(msg, seconds=2.2):
        view = ModalView(size_hint=(0.85, None), height=dp(80),
                         background_color=(0, 0, 0, 0.6))
        view.add_widget(Label(text=msg, color=WHITE, font_size=sp(15),
                              halign="center"))
        view.open()
        Clock.schedule_once(lambda dt: view.dismiss(), seconds)

    def show_error(title, message):
        """Scrollable popup for long error messages (Java exceptions)."""
        box = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(8))
        scroll = ScrollView(size_hint=(1, 1))
        label = Label(text=message, color=WHITE, font_size=sp(13),
                      halign="left", valign="top", size_hint_y=None)
        label.bind(width=lambda w, v: setattr(w, "text_size", (v, None)))
        label.bind(texture_size=lambda w, ts: setattr(
            w, "height", ts[1] + dp(8)))
        scroll.add_widget(label)
        box.add_widget(scroll)
        pop = Popup(title=title, content=box, size_hint=(0.92, 0.65),
                    auto_dismiss=True)
        box.add_widget(Btn("Close", "secondary", size_hint_y=None,
                           height=dp(44), on_press=lambda *_: pop.dismiss()))
        pop.open()

    def confirm(title, message, on_yes, yes_text="Yes"):
        box = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(10))
        box.add_widget(Label(text=message, color=WHITE, font_size=sp(15),
                             halign="center", text_size=(dp(260), None)))
        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(10))
        pop = Popup(title=title, content=box, size_hint=(0.88, None),
                    height=dp(240), auto_dismiss=False)
        row.add_widget(Btn("Cancel", "secondary", on_press=lambda *_: pop.dismiss()))
        row.add_widget(Btn(yes_text, "danger", on_press=lambda *_: (
            pop.dismiss(), on_yes())))
        box.add_widget(row)
        pop.open()

    def texture_from_pil(img, max_side=1600):
        img = img.convert("RGB")
        k = max_side / float(max(img.size))
        if k < 1:
            img = img.resize((int(img.width * k), int(img.height * k)),
                             Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, "JPEG", quality=88)
        buf.seek(0)
        return CoreImage(buf, ext="jpg").texture

    class Clip(StencilView, FloatLayout):
        pass

    class CropOverlay(Widget):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.margins = (0, 0, 0, 0)  # left, top, right, bottom fractions
            self.bind(pos=self.redraw, size=self.redraw)

        def set_margins(self, left, top, right, bottom):
            self.margins = (left, top, right, bottom)
            self.redraw()

        def redraw(self, *_):
            self.canvas.clear()
            x, y = self.pos
            w, h = self.size
            l, t, r, b = self.margins
            x1, x2 = x + l * w, x + w - r * w
            y1, y2 = y + b * h, y + h - t * h
            with self.canvas:
                Color(0, 0, 0, 0.55)
                Rectangle(pos=(x, y), size=(l * w, h))
                Rectangle(pos=(x2, y), size=(r * w, h))
                Rectangle(pos=(x1, y), size=(x2 - x1, b * h))
                Rectangle(pos=(x1, y2), size=(x2 - x1, t * h))
                Color(1, 0.8, 0, 1)
                Line(rectangle=(x1, y1, x2 - x1, y2 - y1), width=dp(1.5))

    # ------------------------------------------------------------------ app
    class ScannerApp(App):
        title = APP_NAME

        def build(self):
            self.data_dir = self.user_data_dir  # private, survives restarts
            self.cache_dir = os.path.join(self.data_dir, "cache")
            os.makedirs(self.cache_dir, exist_ok=True)
            for old in os.listdir(self.cache_dir):  # clear stale temp files
                try:
                    os.remove(os.path.join(self.cache_dir, old))
                except OSError:
                    pass
            self.db = HistoryDB(os.path.join(self.data_dir, "scans.db"),
                                os.path.join(self.data_dir, "images"))
            self.backend, self.ocr_error, self.media = None, None, None
            self.base_image = self.work_image = self.result = None
            self.rotation = 0
            self.detail_id = None
            self.history_query = ""
            try:
                app_dir = os.path.dirname(os.path.abspath(__file__))
                if ANDROID:
                    install_language_data(app_dir, self.data_dir)
                    # Resolve the Tesseract Java class HERE, on the main
                    # thread, before the OCR worker thread is started.
                    # See prepare_android_classes() for the full reason.
                    self.backend = AndroidTesseract(
                        self.data_dir, self.cache_dir,
                        tess_class=prepare_android_classes())
                else:
                    self.backend = DesktopTesseract(
                        app_dir if os.path.exists(os.path.join(
                            app_dir, "eng.traineddata")) else None)
            except OcrUnavailable as exc:
                self.ocr_error = str(exc)
            if ANDROID:
                try:
                    self.media = AndroidMedia(
                        self.cache_dir, self.image_received,
                        lambda m: Clock.schedule_once(
                            lambda dt: notify(m, 4), 0))
                    self._request_storage_permission()
                except Exception as exc:  # noqa: BLE001
                    self.ocr_error = (self.ocr_error or "") + \
                        " Camera/gallery unavailable: %s" % exc
            self.sm = ScreenManager(transition=NoTransition())
            self.home, self.edit = Screen(name="home"), Screen(name="edit")
            self.res, self.hist = Screen(name="result"), Screen(name="history")
            self.det = Screen(name="detail")
            for s in (self.home, self.edit, self.res, self.hist, self.det):
                self.sm.add_widget(s)
            self.build_home()
            Window.bind(on_keyboard=self.on_key)
            return self.sm

        def on_stop(self):
            try:
                self.db.close()
            except Exception:  # noqa: BLE001
                pass

        def _request_storage_permission(self):
            try:
                from jnius import autoclass
                if autoclass("android.os.Build$VERSION").SDK_INT < 29:
                    from android.permissions import Permission, request_permissions
                    request_permissions([Permission.WRITE_EXTERNAL_STORAGE])
            except Exception:  # noqa: BLE001
                pass

        def on_key(self, window, key, *args):
            if key == 27:  # Android back button / Esc
                cur = self.sm.current
                if cur == "home":
                    return False
                self.go_home() if cur in ("edit", "history") else (
                    self.open_history() if cur == "detail" else self.open_edit())
                return True
            return False

        def go(self, name):
            self.sm.current = name

        def go_home(self):
            self.build_home()
            self.go("home")

        # ------------------------------------------------------------ home
        def build_home(self):
            self.home.clear_widgets()
            root = BoxLayout(orientation="vertical")
            root.add_widget(header(APP_NAME))
            body = BoxLayout(orientation="vertical", padding=dp(20),
                             spacing=dp(14))
            card = BoxLayout(orientation="vertical", size_hint_y=None,
                             height=dp(110), padding=dp(12))
            paint(card, WHITE)
            card.add_widget(Label(text="Total saved scans", color=GRAY_DARK,
                                  font_size=sp(14)))
            card.add_widget(Label(text=str(self.db.count()), color=NAVY,
                                  font_size=sp(40), bold=True))
            body.add_widget(card)
            body.add_widget(Btn("Capture Engine Image", size_hint_y=None,
                                height=dp(64), on_press=lambda *_: self.capture()))
            body.add_widget(Btn("Upload Image", "secondary", size_hint_y=None,
                                height=dp(64), on_press=lambda *_: self.pick()))
            body.add_widget(Btn("Scan History (%d)" % self.db.count(), "light",
                                size_hint_y=None, height=dp(64),
                                on_press=lambda *_: self.open_history()))
            if self.ocr_error:
                body.add_widget(Txt("OCR problem: " + escape_markup(self.ocr_error),
                                    13, RED, size_hint_y=None))
            body.add_widget(Txt(
                "Works fully offline. OCR can make mistakes - always compare "
                "the result with the number stamped on the engine.",
                13, GRAY_DARK, size_hint_y=None))
            body.add_widget(Widget())
            root.add_widget(body)
            self.home.add_widget(root)

        def capture(self):
            if self.media:
                self.media.capture()
            else:
                notify("Camera is only available on Android.\nUse Upload Image.", 3)

        def pick(self):
            if self.media:
                self.media.pick()
            else:  # desktop convenience
                self._desktop_pick()

        def _desktop_pick(self):
            from kivy.uix.filechooser import FileChooserListView
            chooser = FileChooserListView(filters=["*.jpg", "*.jpeg", "*.png"])
            box = BoxLayout(orientation="vertical")
            box.add_widget(chooser)
            pop = Popup(title="Choose image", content=box, size_hint=(0.95, 0.9))

            def ok(*_):
                if chooser.selection:
                    pop.dismiss()
                    self.image_received(chooser.selection[0])
            box.add_widget(Btn("Open", size_hint_y=None, height=dp(48),
                               on_press=ok))
            pop.open()

        def image_received(self, path):
            def later(dt):
                try:
                    self.base_image = load_image(path)
                    self.rotation = 0
                    self.open_edit(reset=True)
                except Exception as exc:  # noqa: BLE001
                    notify("Could not open the image: %s" % exc, 4)
            Clock.schedule_once(later, 0)

        # ------------------------------------------------------------ edit
        def open_edit(self, reset=False):
            if self.base_image is None:
                return self.go_home()
            self.edit.clear_widgets()
            root = BoxLayout(orientation="vertical")
            root.add_widget(header("Adjust image", back=self.go_home))
            self.clip = Clip()
            self.scatter = Scatter(do_rotation=False, scale_min=0.5,
                                   scale_max=10, auto_bring_to_front=False)
            self.preview = KvImage(fit_mode="fill")
            self.overlay = CropOverlay()
            self.scatter.add_widget(self.preview)
            self.scatter.add_widget(self.overlay)
            self.scatter.bind(size=self._sync_children)
            self.clip.add_widget(self.scatter)
            self.clip.bind(size=self._fit_preview, pos=self._fit_preview)
            root.add_widget(self.clip)

            tools = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(4),
                              padding=(dp(4), 0))
            for label, fn in (("Rotate L", lambda: self.rotate(-90)),
                              ("Rotate R", lambda: self.rotate(90)),
                              ("Zoom -", lambda: self.zoom(0.8)),
                              ("Zoom +", lambda: self.zoom(1.25)),
                              ("Reset", lambda: self.reset_view())):
                tools.add_widget(Btn(label, "secondary",
                                     on_press=lambda *_, f=fn: f()))
            root.add_widget(tools)

            grid = GridLayout(cols=2, size_hint_y=None, height=dp(100),
                              padding=dp(6), spacing=dp(4))
            self.sliders = {}
            for key in ("Left", "Right", "Top", "Bottom"):
                cell = BoxLayout(orientation="vertical")
                lab = Txt("Crop %s: 0%%" % key, 12, size_hint_y=None, height=dp(20))
                sl = Slider(min=0, max=45, value=0, step=1)
                sl.bind(value=lambda w, v, k=key, l=lab: self._crop_changed(k, v, l))
                cell.add_widget(lab)
                cell.add_widget(sl)
                grid.add_widget(cell)
                self.sliders[key] = sl
            root.add_widget(grid)
            root.add_widget(Btn("Scan Engine Number", size_hint_y=None,
                                height=dp(56), on_press=lambda *_: self.start_ocr()))
            self.edit.add_widget(root)
            self._render_preview()
            self.go("edit")

        def _render_preview(self):
            img = rotate_image(self.base_image, self.rotation)
            self.preview.texture = texture_from_pil(img)
            self.preview_ratio = img.width / float(img.height)
            self._fit_preview()

        def _fit_preview(self, *_):
            if not hasattr(self, "clip") or self.clip.width < 10 \
                    or self.clip.height < 10 or not hasattr(self, "preview_ratio"):
                return
            w, h = self.clip.size
            ar = self.preview_ratio
            fw, fh = (h * ar, h) if w / float(h) > ar else (w, w / ar)
            self.scatter.transform = Matrix()
            self.scatter.size = (fw, fh)
            self.scatter.pos = (self.clip.x + (w - fw) / 2,
                                self.clip.y + (h - fh) / 2)
            self._sync_children()

        def _sync_children(self, *_):
            for child in (self.preview, self.overlay):
                child.pos, child.size = (0, 0), self.scatter.size

        def _crop_changed(self, key, value, label):
            label.text = "Crop %s: %d%%" % (key, value)
            v = {k: s.value / 100.0 for k, s in self.sliders.items()}
            self.overlay.set_margins(v["Left"], v["Top"], v["Right"], v["Bottom"])

        def rotate(self, deg):
            self.rotation = (self.rotation + deg) % 360
            for s in self.sliders.values():
                s.value = 0
            self._render_preview()

        def zoom(self, factor):
            new = min(max(self.scatter.scale * factor, 0.5), 10)
            self.scatter.apply_transform(
                Matrix().scale(new / self.scatter.scale, new / self.scatter.scale, 1),
                anchor=self.scatter.to_local(*self.clip.center))

        def reset_view(self):
            self._fit_preview()

        def current_image(self):
            img = rotate_image(self.base_image, self.rotation)
            v = {k: s.value / 100.0 for k, s in self.sliders.items()}
            return crop_fraction(img, v["Left"], v["Top"], v["Right"], v["Bottom"])

        # ------------------------------------------------------------- ocr
        def start_ocr(self):
            if self.backend is None:
                return notify("OCR engine unavailable:\n%s" % self.ocr_error, 5)
            self.work_image = self.current_image()
            self.busy = ModalView(size_hint=(0.8, None), height=dp(110),
                                  auto_dismiss=False)
            self.busy_label = Label(text="Scanning... (several passes)",
                                    font_size=sp(16), color=WHITE)
            self.busy.add_widget(self.busy_label)
            self.busy.open()

            def work():
                try:
                    res = run_ocr(self.work_image, self.backend,
                                  progress=lambda i, n: Clock.schedule_once(
                                      lambda dt: setattr(
                                          self.busy_label, "text",
                                          "Scanning... pass %d of %d" % (i + 1, n)), 0))
                    Clock.schedule_once(lambda dt: self.ocr_done(res, None), 0)
                except Exception as exc:  # noqa: BLE001
                    Clock.schedule_once(
                        lambda dt, e=exc: self.ocr_done(None, e), 0)
            threading.Thread(target=work, daemon=True).start()

        def ocr_done(self, res, error):
            self.busy.dismiss()
            if error is not None:
                text = str(error)
                print("OCR error: %s" % text)   # full text in `adb logcat`
                return show_error("OCR failed", text[:1500])
            self.result = res
            self.open_result()

        # ---------------------------------------------------------- result
        def open_result(self):
            res = self.result
            self.res.clear_widgets()
            root = BoxLayout(orientation="vertical")
            root.add_widget(header("Scan result", back=self.open_edit))
            body = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(8))
            img = KvImage(size_hint_y=0.38)
            img.texture = texture_from_pil(self.work_image, 1200)
            body.add_widget(img)
            body.add_widget(Txt("Engine number (editable)", 13, GRAY_DARK,
                                size_hint_y=None))
            self.entry = TextInput(text=res.text, multiline=False, font_size=sp(24),
                                   size_hint_y=None, height=dp(52),
                                   write_tab=False, input_type="text",
                                   hint_text="Type the engine number")
            body.add_widget(self.entry)
            if res.alternatives:
                sp_ = Spinner(text="Other possible readings", values=res.alternatives,
                              size_hint_y=None, height=dp(42), background_normal="",
                              background_color=NAVY_LIGHT, color=WHITE)
                sp_.bind(text=lambda w, v: setattr(self.entry, "text", v)
                         if v in res.alternatives else None)
                body.add_widget(sp_)
            warns = list(res.warnings) + [
                "OCR is never 100% accurate. Verify the number on the engine."]
            wl = Txt("[b]Please verify[/b]\n- " + "\n- ".join(
                escape_markup(w) for w in warns), 13, WARN_TXT, size_hint_y=None)
            warn_box = BoxLayout(size_hint_y=None, padding=dp(8))
            paint(warn_box, WARN_BG)
            warn_box.add_widget(wl)
            warn_box.bind(minimum_height=warn_box.setter("height"))
            wl.bind(height=lambda w, h: setattr(warn_box, "height", h + dp(16)))
            sc = ScrollView(size_hint_y=0.3)
            sc.add_widget(warn_box)
            body.add_widget(sc)
            row = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(8))
            row.add_widget(Btn("Copy", "secondary",
                               on_press=lambda *_: self.copy(self.entry.text)))
            row.add_widget(Btn("Scan Again", "light",
                               on_press=lambda *_: self.open_edit()))
            row.add_widget(Btn("Save", on_press=lambda *_: self.save_result()))
            body.add_widget(row)
            root.add_widget(body)
            self.res.add_widget(root)
            self.go("result")

        def copy(self, text):
            text = (text or "").strip()
            if not text:
                return notify("Nothing to copy.")
            Clipboard.copy(text)
            notify("Copied: %s" % text)

        def save_result(self):
            text = self.entry.text.strip()
            if not text:
                return notify("Enter the engine number before saving.", 3)
            tmp = os.path.join(self.cache_dir, "save_%s.jpg" % uuid.uuid4().hex)
            try:
                self.work_image.convert("RGB").save(tmp, "JPEG", quality=90)
                _, number = self.db.add_scan(
                    text, self.result.raw_text, self.result.confidence,
                    self.result.warnings, tmp)
            except Exception as exc:  # noqa: BLE001
                return notify("Could not save: %s" % exc, 4)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            notify("Saved as #%s" % format_history_no(number))
            self.go_home()

        # --------------------------------------------------------- history
        def open_history(self):
            self.hist.clear_widgets()
            root = BoxLayout(orientation="vertical")
            root.add_widget(header("Scan History", back=self.go_home, right=Btn(
                "Clear All", "danger", size_hint_x=None, width=dp(96),
                on_press=lambda *_: self.clear_all())))
            self.search = TextInput(text=self.history_query, multiline=False,
                                    hint_text="Search number, date or engine number",
                                    size_hint_y=None, height=dp(44), font_size=sp(15))
            self.search.bind(text=lambda w, v: self.refresh_history(v))
            root.add_widget(self.search)
            self.list_box = GridLayout(cols=1, size_hint_y=None, spacing=dp(6),
                                       padding=dp(6))
            self.list_box.bind(minimum_height=self.list_box.setter("height"))
            sv = ScrollView()
            sv.add_widget(self.list_box)
            root.add_widget(sv)
            self.hist.add_widget(root)
            self.refresh_history(self.history_query)
            self.go("history")

        def refresh_history(self, query=""):
            self.history_query = query
            self.list_box.clear_widgets()
            rows = self.db.list_scans(query)
            if not rows:
                self.list_box.add_widget(Txt(
                    "No saved scans." if not query else "No matching scans.",
                    16, GRAY_DARK, size_hint_y=None, height=dp(60)))
            for r in rows:
                card = BoxLayout(orientation="vertical", size_hint_y=None,
                                 height=dp(112), padding=dp(8), spacing=dp(4))
                paint(card, WHITE)
                card.add_widget(Txt("[b]%s[/b]   %s" % (
                    format_history_no(r["history_no"]),
                    escape_markup(r["engine_number"])), 19,
                    size_hint_y=None, height=dp(28)))
                card.add_widget(Txt(r["created_at"], 12, GRAY_DARK,
                                    size_hint_y=None, height=dp(18)))
                btns = BoxLayout(spacing=dp(6))
                sid, eng = r["id"], r["engine_number"]
                btns.add_widget(Btn("Details", "secondary",
                                    on_press=lambda *_, i=sid: self.open_detail(i)))
                btns.add_widget(Btn("Copy", "light",
                                    on_press=lambda *_, t=eng: self.copy(t)))
                btns.add_widget(Btn("Delete", "danger",
                                    on_press=lambda *_, i=sid: self.delete_one(i)))
                card.add_widget(btns)
                self.list_box.add_widget(card)

        def delete_one(self, scan_id):
            row = self.db.get_scan(scan_id)
            if row is None:
                return
            confirm("Delete scan", "Delete #%s (%s)?" % (
                format_history_no(row["history_no"]), row["engine_number"]),
                lambda: (self.db.delete_scan(scan_id),
                         self.refresh_history(self.history_query)),
                "Delete")

        def clear_all(self):
            if self.db.count() == 0:
                return notify("History is already empty.")
            confirm("Clear all history",
                    "Delete ALL %d saved scans and their images? This cannot be "
                    "undone." % self.db.count(),
                    lambda: (self.db.clear_all(),
                             self.refresh_history(self.history_query)),
                    "Delete all")

        # ---------------------------------------------------------- detail
        def open_detail(self, scan_id):
            row = self.db.get_scan(scan_id)
            if row is None:
                return
            self.detail_id = scan_id
            self.det.clear_widgets()
            root = BoxLayout(orientation="vertical")
            root.add_widget(header("Scan #%s" % format_history_no(row["history_no"]),
                                   back=self.open_history))
            body = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(8))
            img = KvImage(size_hint_y=0.4)
            if row["image_path"] and os.path.exists(row["image_path"]):
                try:
                    img.texture = texture_from_pil(Image.open(row["image_path"]), 1200)
                except Exception:  # noqa: BLE001
                    pass
            body.add_widget(img)
            body.add_widget(Txt("Saved %s" % row["created_at"], 13, GRAY_DARK,
                                size_hint_y=None))
            self.detail_entry = TextInput(text=row["engine_number"], multiline=False,
                                          font_size=sp(24), size_hint_y=None,
                                          height=dp(52), write_tab=False)
            body.add_widget(self.detail_entry)
            info = "Raw OCR output:\n%s" % (row["raw_ocr"] or "(none / typed manually)")
            if row["warnings"]:
                info += "\n\nWarnings:\n" + row["warnings"]
            sv = ScrollView(size_hint_y=0.3)
            sv.add_widget(Txt(escape_markup(info), 12, GRAY_DARK, size_hint_y=None))
            body.add_widget(sv)
            btns = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(8))
            btns.add_widget(Btn("Save Edit", on_press=lambda *_: self.save_edit()))
            btns.add_widget(Btn("Copy", "secondary", on_press=lambda *_: self.copy(
                self.detail_entry.text)))
            btns.add_widget(Btn("Delete", "danger", on_press=lambda *_: (
                self.delete_one_from_detail())))
            body.add_widget(btns)
            root.add_widget(body)
            self.det.add_widget(root)
            self.go("detail")

        def save_edit(self):
            try:
                self.db.update_engine_number(self.detail_id, self.detail_entry.text)
                notify("Updated.")
            except ValueError as exc:
                notify(str(exc))

        def delete_one_from_detail(self):
            row = self.db.get_scan(self.detail_id)
            if row is None:
                return
            confirm("Delete scan", "Delete #%s?" % format_history_no(
                row["history_no"]),
                lambda: (self.db.delete_scan(self.detail_id), self.open_history()),
                "Delete")

    return ScannerApp()


def main():
    build_app().run()


if __name__ == "__main__":
    main()
