from pathlib import Path

import pytest
from PIL import Image

from onyx.file_processing.pdf_ocr import _ocr_pdf_pages_from_file

FIXTURES = Path(__file__).parent / "fixtures"


def test_ocr_processes_all_pages_in_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "multipage.pdf"
    pdf_path.write_bytes((FIXTURES / "multipage.pdf").read_bytes())

    rendered_pages: list[int] = []
    ocr_pages: list[int] = []

    def mock_render_pdf_page_for_ocr(
        doc: object,
        file_name: str,
        page_num: int,
        min_dim: int,
        base_dpi: int,
    ) -> Image.Image:
        rendered_pages.append(page_num)
        return Image.new("RGB", (1, 1))

    def mock_ocr_single_pdf_page(
        image: Image.Image,
        page_num: int,
        endpoint_url: str,
        model_name: str,
        max_tokens: int,
        timeout: int,
        custom_prompt: str | None = None,
        api_key: str | None = None,
    ) -> str:
        ocr_pages.append(page_num)
        return f"Page {page_num} text"

    monkeypatch.setattr(
        "onyx.file_processing.pdf_ocr._render_pdf_page_for_ocr",
        mock_render_pdf_page_for_ocr,
    )
    monkeypatch.setattr(
        "onyx.file_processing.pdf_ocr._ocr_single_pdf_page",
        mock_ocr_single_pdf_page,
    )

    results = _ocr_pdf_pages_from_file(
        filepath=str(pdf_path),
        file_name="multipage.pdf",
        endpoint_url="http://example.com/v1/chat/completions",
        model_name="test-model",
        max_tokens=1000,
        timeout=30,
        max_concurrent=5,
        min_image_dim=3000,
        base_dpi=200,
    )

    assert rendered_pages == [1, 2]
    assert ocr_pages == [1, 2]
    assert results == [(1, "Page 1 text"), (2, "Page 2 text")]
