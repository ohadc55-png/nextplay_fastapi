"""File processor tests — magic-byte validation + extractors.

Each format has its own happy-path test plus an error-path test. We
build real fixtures (real PNG, real PDF, real CSV) so the pandas /
PyMuPDF / json paths exercise their actual code, not mocks."""

from __future__ import annotations

import json

import pytest

from src.services import file_processor as fp

# ---------------------------------------------------------------------------
# Fixtures — minimal valid bytes per format
# ---------------------------------------------------------------------------


@pytest.fixture
def csv_path(tmp_path):
    p = tmp_path / "stats.csv"
    p.write_text("name,points,rebounds\nDoncic,33,9\nSmith,12,4\n", encoding="utf-8")
    return str(p)


@pytest.fixture
def excel_path(tmp_path):
    """Create a tiny .xlsx via openpyxl."""
    from openpyxl import Workbook

    p = tmp_path / "stats.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["name", "points", "rebounds"])
    ws.append(["Doncic", 33, 9])
    ws.append(["Smith", 12, 4])
    wb.save(str(p))
    return str(p)


@pytest.fixture
def pdf_path(tmp_path):
    """Create a single-page PDF via PyMuPDF."""
    import fitz

    p = tmp_path / "report.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 100), "Practice plan: focus on transition defense.")
    doc.save(str(p))
    doc.close()
    return str(p)


@pytest.fixture
def text_path(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("Game observations: opponent struggles vs zone.\n", encoding="utf-8")
    return str(p)


@pytest.fixture
def json_path(tmp_path):
    p = tmp_path / "data.json"
    p.write_text(json.dumps({"team": "Maccabi", "wins": 12}), encoding="utf-8")
    return str(p)


@pytest.fixture
def png_path(tmp_path):
    """1×1 transparent PNG (real PNG header)."""
    p = tmp_path / "image.png"
    p.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\rIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    return str(p)


@pytest.fixture
def fake_exe_as_pdf(tmp_path):
    """An .exe disguised as .pdf — magic-byte validation should catch it."""
    p = tmp_path / "malware.pdf"
    p.write_bytes(b"MZ\x00\x00\x00fake exe payload pretending to be PDF")
    return str(p)


# ---------------------------------------------------------------------------
# Catalog helpers
# ---------------------------------------------------------------------------


class TestCatalog:
    @pytest.mark.parametrize("name,expected", [
        ("game.pdf", True), ("stats.CSV", True), ("data.xlsx", True),
        ("notes.txt", True), ("config.JSON", True),
        ("photo.jpg", True), ("playbook.HEIC", False),
        ("malware.exe", False), ("", False),
    ])
    def test_is_supported(self, name, expected):
        assert fp.is_supported(name) is expected

    def test_image_vs_data(self):
        assert fp.is_image("photo.png")
        assert not fp.is_image("game.csv")


# ---------------------------------------------------------------------------
# Magic-byte validation
# ---------------------------------------------------------------------------


class TestValidate:
    async def test_real_pdf_passes(self, pdf_path):
        assert await fp.validate_file_content(pdf_path, "report.pdf") is True

    async def test_real_png_passes(self, png_path):
        assert await fp.validate_file_content(png_path, "image.png") is True

    async def test_real_csv_passes(self, csv_path):
        assert await fp.validate_file_content(csv_path, "stats.csv") is True

    async def test_exe_disguised_as_pdf_rejected(self, fake_exe_as_pdf):
        assert await fp.validate_file_content(fake_exe_as_pdf, "malware.pdf") is False

    async def test_exe_disguised_as_csv_rejected(self, tmp_path):
        # CSV has no magic bytes — falls through to the executable header check
        p = tmp_path / "malware.csv"
        p.write_bytes(b"#!/bin/sh\nrm -rf /\n")
        assert await fp.validate_file_content(str(p), "malware.csv") is False

    async def test_missing_file_returns_false(self, tmp_path):
        result = await fp.validate_file_content(str(tmp_path / "none.pdf"), "none.pdf")
        assert result is False


# ---------------------------------------------------------------------------
# Extractors — happy paths
# ---------------------------------------------------------------------------


class TestExtractors:
    async def test_csv_summary(self, csv_path):
        text = await fp.extract_file_content(csv_path, "stats.csv")
        assert "CSV file with 2 rows" in text
        assert "Doncic" in text
        assert "Basic Statistics" in text  # numeric describe()

    async def test_excel_summary(self, excel_path):
        text = await fp.extract_file_content(excel_path, "stats.xlsx")
        assert "Excel file with 2 rows" in text
        assert "Doncic" in text

    async def test_pdf_text(self, pdf_path):
        text = await fp.extract_file_content(pdf_path, "report.pdf")
        assert "Practice plan" in text
        assert "Page 1" in text

    async def test_txt_passthrough(self, text_path):
        text = await fp.extract_file_content(text_path, "notes.txt")
        assert "transition" in text.lower() or "zone" in text.lower()

    async def test_json_pretty_print(self, json_path):
        text = await fp.extract_file_content(json_path, "data.json")
        assert '"team"' in text
        assert "Maccabi" in text

    async def test_image_returns_none(self, png_path):
        result = await fp.extract_file_content(png_path, "image.png")
        assert result is None

    async def test_unsupported_extension_returns_marker(self, tmp_path):
        p = tmp_path / "raw.bin"
        p.write_bytes(b"\x00\x01\x02\x03")
        text = await fp.extract_file_content(str(p), "raw.bin")
        assert text and "Unsupported" in text


# ---------------------------------------------------------------------------
# Sanitization — CSV formula injection
# ---------------------------------------------------------------------------


class TestFormulaInjection:
    async def test_leading_equals_prefixed(self, tmp_path):
        """A CSV cell starting with `=` would normally execute as a
        formula in Excel. The sanitizer prefixes a single quote so it's
        treated as text."""
        p = tmp_path / "evil.csv"
        p.write_text(
            "name,note\n"
            "Doncic,=cmd|'/c calc'!A1\n"   # classic CSV-injection payload
            "Smith,normal note\n",
            encoding="utf-8",
        )
        text = await fp.extract_file_content(str(p), "evil.csv")
        # The leading `=` is now `'=`
        assert "'=" in text
        # Normal note untouched
        assert "normal note" in text


# ---------------------------------------------------------------------------
# Truncation — large files capped
# ---------------------------------------------------------------------------


class TestTruncation:
    async def test_text_truncated_at_20kb(self, tmp_path):
        big = "x" * 30_000  # 30 KB
        p = tmp_path / "big.txt"
        p.write_text(big, encoding="utf-8")
        text = await fp.extract_file_content(str(p), "big.txt")
        assert "Truncated" in text
        # We read at most 20K then add a marker — total well under 30K
        assert len(text) < 30_000

    async def test_json_truncated_when_huge(self, tmp_path):
        # Build an array large enough to exceed the 15K char cap when pretty-printed
        big_array = [{"player": f"name_{i}", "pts": i} for i in range(1000)]
        p = tmp_path / "big.json"
        p.write_text(json.dumps(big_array), encoding="utf-8")
        text = await fp.extract_file_content(str(p), "big.json")
        assert "Truncated" in text
        assert len(text) < 16_000
