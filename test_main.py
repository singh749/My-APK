"""Unit tests (run with: pytest -q). They need no Android device and no Kivy."""
import os
import shutil

import pytest
from PIL import Image, ImageDraw, ImageFont

import main
from main import HistoryDB, run_ocr

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.fixture
def db(tmp_path):
    d = HistoryDB(str(tmp_path / "scans.db"), str(tmp_path / "images"))
    yield d
    d.close()


def used_levels(img):
    return {i for i, c in enumerate(img.histogram()) if c}


def make_photo(path, text="K15B1234567", size=(900, 240)):
    im = Image.new("RGB", size, (200, 200, 200))
    d = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 80)
    except OSError:
        font = ImageFont.load_default()
    d.text((30, 70), text, fill=(30, 30, 30), font=font)
    im.save(path)
    return path


class FakeBackend:
    """Stands in for Tesseract so the pipeline can be tested anywhere."""
    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), 0

    def recognize(self, img, psm):
        self.calls += 1
        out = self.outputs[min(self.calls - 1, len(self.outputs) - 1)]
        if isinstance(out, Exception):
            raise out
        return out


# ---- history numbering -------------------------------------------------------
def test_sequential_history_numbers(db):
    nums = [db.add_scan(e)[1] for e in ("ABC123456", "XYZ987654", "LMN456789")]
    assert nums == [1, 2, 3]
    assert [main.format_history_no(n) for n in nums] == ["001", "002", "003"]


def test_deleted_number_is_never_reused(db):
    ids = [db.add_scan(e)[0] for e in ("ABC123456", "XYZ987654", "LMN456789")]
    assert db.delete_scan(ids[1])
    _, number = db.add_scan("NEW111111")
    assert number == 4
    assert sorted(r["history_no"] for r in db.list_scans()) == [1, 3, 4]


def test_numbers_not_reused_after_clear_all(db):
    db.add_scan("AAA11111")
    db.add_scan("BBB22222")
    db.clear_all()
    assert db.count() == 0
    assert db.add_scan("CCC33333")[1] == 3


def test_delete_does_not_affect_other_records_or_images(db, tmp_path):
    src = make_photo(str(tmp_path / "p.jpg"))
    unrelated = tmp_path / "images" / "unrelated.jpg"
    unrelated.write_bytes(b"keep me")
    a, _ = db.add_scan("AAA11111", image_src=src)
    b, _ = db.add_scan("BBB22222", image_src=src)
    img_a, img_b = db.get_scan(a)["image_path"], db.get_scan(b)["image_path"]
    assert img_a != img_b and os.path.exists(img_a) and os.path.exists(img_b)
    db.delete_scan(a)
    assert db.get_scan(a) is None
    assert db.get_scan(b)["engine_number"] == "BBB22222"
    assert not os.path.exists(img_a)
    assert os.path.exists(img_b) and unrelated.exists()


def test_failed_insert_leaves_no_orphan_image(db, tmp_path):
    src = make_photo(str(tmp_path / "p.jpg"))
    with pytest.raises(ValueError):
        db.add_scan("   ", image_src=src)
    assert os.listdir(db.image_dir) == []
    assert db.last_history_no() == 0


def test_search(db):
    db.add_scan("ABC123456")
    db.add_scan("XYZ987654")
    assert [r["engine_number"] for r in db.list_scans("xyz")] == ["XYZ987654"]
    assert len(db.list_scans("002")) == 1
    assert db.list_scans("100%") == []          # % is not a wildcard
    assert len(db.list_scans("")) == 2


# ---- persistence -------------------------------------------------------------
def test_database_persistence(tmp_path):
    p, imgs = str(tmp_path / "scans.db"), str(tmp_path / "images")
    d1 = HistoryDB(p, imgs)
    d1.add_scan("ABC123456", raw_ocr="abc 123456", confidence=88.5,
                warnings=["w1", "w2"])
    d1.add_scan("XYZ987654")
    d1.delete_scan(d1.list_scans(newest_first=False)[1]["id"])
    d1.close()
    d2 = HistoryDB(p, imgs)                      # "app restarted"
    rows = d2.list_scans()
    assert len(rows) == 1 and rows[0]["engine_number"] == "ABC123456"
    assert rows[0]["raw_ocr"] == "abc 123456" and rows[0]["confidence"] == 88.5
    assert rows[0]["warnings"] == "w1\nw2"
    assert d2.add_scan("NEXT00001")[1] == 3      # counter survived the restart
    d2.close()


# ---- manual correction & empty results ---------------------------------------
def test_manual_correction_keeps_raw_ocr(db):
    sid, _ = db.add_scan("K15B1234567", raw_ocr="K158I234567")
    db.update_engine_number(sid, "  K15B1234567-X ")
    row = db.get_scan(sid)
    assert row["engine_number"] == "K15B1234567-X"
    assert row["raw_ocr"] == "K158I234567"
    with pytest.raises(ValueError):
        db.update_engine_number(sid, "")


def test_empty_ocr_result_gives_warning_not_crash():
    res = run_ocr(Image.new("RGB", (300, 100), "white"), FakeBackend([("", 0)]))
    assert res.text == "" and res.alternatives == []
    assert any("No engine number" in w for w in res.warnings)


def test_whitespace_and_noise_is_treated_as_empty():
    res = run_ocr(Image.new("RGB", (300, 100), "white"),
                  FakeBackend([("  \n | ,\n", 10)]))
    assert res.text == ""


def test_all_passes_failing_raises_error():
    with pytest.raises(RuntimeError):
        run_ocr(Image.new("RGB", (300, 100), "white"),
                FakeBackend([RuntimeError("engine broken")]))


def test_ambiguous_characters_are_flagged_never_changed():
    res = run_ocr(Image.new("RGB", (300, 100), "white"),
                  FakeBackend([("ABO1S5B8Z2 99\n", 95)]))
    assert res.text == "ABO1S5B8Z2"             # untouched
    joined = " ".join(res.warnings)
    for ch in "O1S5B8Z2":
        assert "'%s'" % ch in joined


def test_disagreeing_passes_produce_alternatives_and_warning():
    outputs = [("K15B1234567", 90), ("K158I234567", 80), ("K15B1234567", 90),
               ("K15B1234567", 90), ("K15B1234567", 90), ("K15B1234567", 90)]
    res = run_ocr(Image.new("RGB", (300, 100), "white"), FakeBackend(outputs))
    assert res.text == "K15B1234567"
    assert "K158I234567" in res.alternatives
    assert any("differently" in w for w in res.warnings)


def test_low_confidence_warning():
    res = run_ocr(Image.new("RGB", (300, 100), "white"),
                  FakeBackend([("KQ4D77123", 40)]))
    assert any("Low OCR confidence" in w for w in res.warnings)


def test_extract_candidates():
    assert main.extract_candidates("engine: k15b-1234567 \n x") == ["K15B-1234567"]
    assert main.extract_candidates("FORD MOTOR") == []
    assert main.extract_candidates("") == []


# ---- image preprocessing -----------------------------------------------------
def test_preprocess_variants_shapes_and_modes(tmp_path):
    img = main.load_image(make_photo(str(tmp_path / "p.png")))
    variants = main.preprocess_variants(img)
    assert set(variants) == {"gray", "enhanced", "otsu", "adaptive", "inverted",
                             "relief", "relief_soft"}
    for v in variants.values():
        assert v.mode == "L"
    for name in ("otsu", "adaptive"):
        assert used_levels(variants[name]) <= {0, 255}


def test_binarization_separates_dark_text_from_light_background(tmp_path):
    img = main.load_image(make_photo(str(tmp_path / "p.png")))
    otsu = main.preprocess_variants(img)["otsu"]
    white = otsu.histogram()[255] / float(otsu.width * otsu.height)
    assert 0.6 < white < 0.99          # mostly background, some black text


def test_adaptive_threshold_pillow_fallback(tmp_path, monkeypatch):
    img = main.load_image(make_photo(str(tmp_path / "p.png")))
    with_np = main.preprocess_variants(img)["adaptive"]
    monkeypatch.setattr(main, "np", None)
    without_np = main.preprocess_variants(img)["adaptive"]
    assert without_np.size == with_np.size
    assert used_levels(without_np) <= {0, 255}


def test_rotate_and_crop():
    img = Image.new("RGB", (400, 200))
    assert main.rotate_image(img, 90).size == (200, 400)
    assert main.rotate_image(img, 0).size == (400, 200)
    assert main.crop_fraction(img, left=0.25, right=0.25).size == (200, 200)
    assert main.crop_fraction(img, left=5, right=5).size[0] > 0   # clamped


# ---- real Tesseract, only when the tesseract program is installed -------------
def _have_tesseract():
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _have_tesseract(), reason="tesseract program not installed")
def test_real_tesseract_reads_synthetic_photo(tmp_path):
    data = os.path.join(HERE, "eng.traineddata")
    if not os.path.exists(data):
        pytest.skip("eng.traineddata not present")
    tessdir = tmp_path / "tessdata"
    tessdir.mkdir()
    shutil.copy(data, tessdir / "eng.traineddata")
    img = main.load_image(make_photo(str(tmp_path / "p.png")))
    res = run_ocr(img, main.DesktopTesseract(str(tessdir)))
    assert res.text.replace("-", "") == "K15B1234567"
