import asyncio
import base64
import faulthandler
import io
import os
import subprocess
import tempfile
import threading
from concurrent.futures import FIRST_COMPLETED
from concurrent.futures import as_completed
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait
from pathlib import Path
from typing import Any
from typing import IO

import requests
from PIL import Image

from onyx.utils.logger import setup_logger

logger = setup_logger()

# Enable Python fatal-error tracebacks for native crashes such as SIGSEGV (-11).
# This does not catch the crash, but it can print the Python stack for all
# threads immediately before the process terminates.
try:
    faulthandler.enable(all_threads=True)
    logger.info("Python faulthandler enabled for PDF OCR process.")
except Exception:
    logger.warning("Failed to enable Python faulthandler.", exc_info=True)


_PROCESS = None


def _get_process_rss_mb() -> float | None:
    """Return current process RSS in MiB when psutil is available."""
    global _PROCESS

    try:
        import psutil

        if _PROCESS is None:
            _PROCESS = psutil.Process(os.getpid())

        return _PROCESS.memory_info().rss / (1024 * 1024)
    except Exception:
        return None


def _log_ocr_memory(stage: str, page_num: int | None = None) -> None:
    """Log current process RSS through the standard Onyx logger."""
    rss_mb = _get_process_rss_mb()
    page_text = f" page={page_num}" if page_num is not None else ""

    if rss_mb is None:
        logger.debug(
            "OCR PIPELINE MEMORY stage=%s%s rss=unavailable",
            stage,
            page_text,
        )
    else:
        logger.info(
            "OCR PIPELINE MEMORY stage=%s%s rss=%.1f MiB",
            stage,
            page_text,
            rss_mb,
        )


_HTTP_THREAD_LOCAL = threading.local()


def _get_http_session() -> requests.Session:
    """
    Return one requests.Session per OCR worker thread.

    Reusing sessions keeps HTTP/TLS connections alive across pages without
    sharing a Session concurrently between threads.
    """
    session = getattr(_HTTP_THREAD_LOCAL, "session", None)

    if session is None:
        session = requests.Session()
        _HTTP_THREAD_LOCAL.session = session

    return session


# PDF OCR configuration. These environment variables let embedding apps control
# the OCR behavior without changing callers. Function arguments can still
# override every value per call.
PDF_OCR_VLLM_SERVER_URL_ENV = "PDF_OCR_VLLM_SERVER_URL"
PDF_OCR_VLLM_API_KEY_ENV = "PDF_OCR_VLLM_API_KEY"
PDF_OCR_VLLM_MODEL_NAME_ENV = "PDF_OCR_VLLM_MODEL_NAME"
PDF_OCR_MAX_TOKENS_ENV = "PDF_OCR_MAX_TOKENS"
PDF_OCR_DEFAULT_TIMEOUT_ENV = "PDF_OCR_DEFAULT_TIMEOUT"
PDF_OCR_MAX_CONCURRENT_ENV = "PDF_OCR_MAX_CONCURRENT"
PDF_OCR_MIN_IMAGE_DIM_ENV = "PDF_OCR_MIN_IMAGE_DIM"
PDF_OCR_BASE_DPI_ENV = "PDF_OCR_BASE_DPI"
PDF_OCR_CUSTOM_PROMPT_ENV = "PDF_OCR_CUSTOM_PROMPT"
PDF_OCR_NORMALIZE_PDF_ENV = "PDF_OCR_NORMALIZE_PDF"
PDF_OCR_GHOSTSCRIPT_COMMAND_ENV = "PDF_OCR_GHOSTSCRIPT_COMMAND"
PDF_OCR_MAX_IMAGE_WIDTH_ENV = "PDF_OCR_MAX_IMAGE_WIDTH"
PDF_OCR_MAX_IMAGE_HEIGHT_ENV = "PDF_OCR_MAX_IMAGE_HEIGHT"

DEFAULT_PDF_OCR_VLLM_SERVER_URL = "https://aigw.cst.gov.sa/v1/chat/completions"
DEFAULT_PDF_OCR_VLLM_API_KEY = "sk-FQztOEG0b3mq7eJ4tXGPJw"
DEFAULT_PDF_OCR_MODEL_NAME = "qwen-3.5-122b-a10b"
DEFAULT_PDF_OCR_MAX_TOKENS = 12000
DEFAULT_PDF_OCR_TIMEOUT = 1200
DEFAULT_PDF_OCR_MAX_CONCURRENT = 4
DEFAULT_PDF_OCR_MIN_IMAGE_DIM = 3000
DEFAULT_PDF_OCR_BASE_DPI = 200
DEFAULT_PDF_OCR_NORMALIZE_PDF = True
DEFAULT_PDF_OCR_GHOSTSCRIPT_COMMAND = "gs"
DEFAULT_PDF_OCR_MAX_IMAGE_WIDTH = 4000
DEFAULT_PDF_OCR_MAX_IMAGE_HEIGHT = 4000


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning("Invalid integer value for %s=%r. Using %d.", name, value, default)
        return default


def _did_pdf_ocr_page_fail(text: str) -> bool:
    stripped_text = text.strip()
    return (
        stripped_text == ""
        or "[Error processing PDF:" in stripped_text
        or "[Error processing page " in stripped_text
    )


def _default_pdf_ocr_prompt(page_num: int) -> str:
    return f"""You are a PDF-to-Markdown converter. Extract ONLY text content from the PDF page.

CRITICAL RULES:
1. DO NOT include ANY image tags, placeholders, or references.
2. DO NOT write: ![...], [[...]], [image], (http...), or any URL.
3. DO NOT mention images, figures, charts, or visual elements.
4. ONLY extract and format the TEXT content.
5. If there are images/figures, SKIP them entirely - do not write anything about them.

CHART EXTRACTION:
- If the page contains a chart, graph, plot, or data visualization, convert it into a valid Markdown table.
- Extract all visible labels, categories, series names, axes, legends, and numeric values.
- Reconstruct the chart data as accurately as possible.
- Use one Markdown table per chart.
- If exact values are printed on the chart, use those exact values.
- If values must be estimated visually, include the best estimate only if it is reasonably clear.
- Do NOT mention that the data came from a chart.
- Do NOT describe the chart visually.
- Do NOT output placeholders such as "chart omitted" or "image skipped".

Formatting Requirements:
- Preserve logical structure (headings: #, ##, ###).
- Format paragraphs, lists, and tables correctly.
- Convert tables to Markdown table format (| col1 | col2 |).
- Maintain reading order and hierarchy.
- Clean OCR errors and remove noise.

Output ONLY clean Markdown text. NO image tags. NO URLs. NO placeholders.

Page {page_num} content:"""


def _flatten_pdf_page(page: Any, flag: Any = None) -> None:
    """Flatten annotations/form fields on a PDFium page when possible."""
    try:
        import pypdfium2.raw as pdfium_c

        rc = pdfium_c.FPDFPage_Flatten(
            page, pdfium_c.FLAT_NORMALDISPLAY if flag is None else flag
        )
        if rc == pdfium_c.FLATTEN_FAIL:
            logger.warning("Failed to flatten annotations on a PDF page.")
    except Exception:
        logger.debug("Failed to flatten PDF page annotations", exc_info=True)


def _is_pdf_render_problem(exception: Exception) -> bool:
    """Return True when the PDF is likely affected by malformed PDF structures."""
    message = str(exception).lower()

    problem_signatures = (
        "smask",
        "image and mask size not matching",
        "mask size",
        "invalid image",
        "invalid xobject",
        "malformed",
        "corrupt",
        "decode",
        "render",
        "pdfium",
    )

    return any(signature in message for signature in problem_signatures)


def _find_ghostscript_command() -> str | None:
    """Find Ghostscript executable from the environment or PATH."""
    configured = os.getenv(PDF_OCR_GHOSTSCRIPT_COMMAND_ENV)

    if configured:
        return configured

    # Common executable names on Linux/Windows installations.
    for command in ("gs", "gswin64c", "gswin32c"):
        if __import__("shutil").which(command):
            return command

    return None


def _normalize_pdf_with_ghostscript(
    source_path: str,
    output_path: str,
    file_name: str,
) -> bool:
    """
    Rewrite a PDF using Ghostscript.

    This is used as a fallback for PDFs containing problematic image/mask
    structures that PDFium cannot render reliably.
    """
    command = _find_ghostscript_command()

    if not command:
        logger.warning(
            "PDF normalization requested for %s, but Ghostscript was not found. "
            "Install Ghostscript or set %s.",
            file_name,
            PDF_OCR_GHOSTSCRIPT_COMMAND_ENV,
        )
        return False

    logger.warning(
        "Normalizing problematic PDF %s using Ghostscript before retrying PDFium.",
        file_name,
    )

    # pdfwrite rewrites the PDF and commonly repairs problematic image,
    # transparency-mask, and XObject structures while preserving page content.
    gs_args = [
        command,
        "-dSAFER",
        "-dBATCH",
        "-dNOPAUSE",
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.7",
        f"-sOutputFile={output_path}",
        source_path,
    ]

    try:
        result = subprocess.run(
            gs_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300,
            check=False,
        )

        if result.returncode != 0:
            logger.error(
                "Ghostscript failed to normalize %s. returncode=%d stderr=%s",
                file_name,
                result.returncode,
                result.stderr[-2000:],
            )
            return False

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            logger.error(
                "Ghostscript reported success but produced no usable PDF: %s",
                output_path,
            )
            return False

        logger.info(
            "Successfully normalized PDF %s -> %s (%d bytes)",
            file_name,
            output_path,
            os.path.getsize(output_path),
        )
        return True

    except subprocess.TimeoutExpired:
        logger.exception(
            "Ghostscript timed out while normalizing %s",
            file_name,
        )
        return False
    except Exception:
        logger.exception(
            "Unexpected error while normalizing %s",
            file_name,
        )
        return False


def _render_pdf_document_with_fallback(
    filepath: str,
    file_name: str,
    min_image_dim: int,
    base_dpi: int,
    normalize_pdf: bool,
    max_image_width: int,
    max_image_height: int,
) -> list[tuple[int, Image.Image]]:
    """
    Render a PDF normally with PDFium.

    If normal rendering fails, optionally normalize the PDF with Ghostscript
    and retry rendering from the normalized PDF.
    """
    import pypdfium2 as pdfium

    def render_document(path: str) -> list[tuple[int, Image.Image]]:
        logger.info(
            "Opening PDF with PDFium for OCR rendering: %s",
            path,
        )

        doc = pdfium.PdfDocument(path)

        try:
            try:
                doc.init_forms()
            except Exception:
                logger.debug(
                    "Failed to initialize PDF forms before OCR rendering",
                    exc_info=True,
                )

            total_pages = len(doc)

            if total_pages <= 0:
                raise ValueError("PDF contains zero pages")

            rendered_pages: list[tuple[int, Image.Image]] = []

            for page_num in range(1, total_pages + 1):
                image = _render_pdf_page_for_ocr(
                    doc=doc,
                    file_name=file_name,
                    page_num=page_num,
                    min_dim=min_image_dim,
                    base_dpi=base_dpi,
                    max_image_width=max_image_width,
                    max_image_height=max_image_height,
                )
                rendered_pages.append((page_num, image))

            return rendered_pages

        finally:
            doc.close()

    # First attempt: original PDF.
    try:
        logger.info(
            "Attempt 1/2: rendering original PDF with PDFium: %s",
            file_name,
        )
        return render_document(filepath)

    except Exception as first_error:
        logger.warning(
            "PDFium failed rendering original PDF %s: %s",
            file_name,
            first_error,
            exc_info=True,
        )

        if not normalize_pdf:
            raise

        # Second attempt: normalize, then render again.
        normalized_path = f"{filepath}.normalized.pdf"

        try:
            if not _normalize_pdf_with_ghostscript(
                source_path=filepath,
                output_path=normalized_path,
                file_name=file_name,
            ):
                raise first_error

            logger.info(
                "Attempt 2/2: rendering normalized PDF with PDFium: %s",
                file_name,
            )

            return render_document(normalized_path)

        except Exception:
            logger.exception(
                "PDFium failed rendering normalized PDF %s",
                file_name,
            )
            raise

        finally:
            try:
                if os.path.exists(normalized_path):
                    os.remove(normalized_path)
            except Exception:
                logger.debug(
                    "Failed to remove normalized PDF %s",
                    normalized_path,
                    exc_info=True,
                )


def _render_pdf_page_for_ocr(
    doc: Any,
    file_name: str,
    page_num: int,
    min_dim: int = DEFAULT_PDF_OCR_MIN_IMAGE_DIM,
    base_dpi: int = DEFAULT_PDF_OCR_BASE_DPI,
    max_image_width: int = DEFAULT_PDF_OCR_MAX_IMAGE_WIDTH,
    max_image_height: int = DEFAULT_PDF_OCR_MAX_IMAGE_HEIGHT,
) -> Image.Image:
    """
    Render a single PDF page from an already-open PDFium document.

    The renderer tries to satisfy the minimum image dimension while NEVER
    exceeding max_image_width or max_image_height.

    The PdfPage wrapper is explicitly closed after rendering so native PDFium
    page handles are not left for document-close/garbage-collection cleanup.
    """
    logger.info("Rendering PDF page %d from %s", page_num, file_name)

    page_index = page_num - 1
    page_obj = doc[page_index]

    try:
        page_width = float(page_obj.get_width())
        page_height = float(page_obj.get_height())

        if page_width <= 0 or page_height <= 0:
            raise ValueError(
                f"Invalid PDF page dimensions: {page_width}x{page_height}"
            )

        if min_dim <= 0:
            raise ValueError("min_image_dim must be greater than zero")

        if base_dpi <= 0:
            raise ValueError("base_dpi must be greater than zero")

        if max_image_width <= 0 or max_image_height <= 0:
            raise ValueError(
                "max_image_width and max_image_height must be greater than zero"
            )

        min_page_dim = min(page_width, page_height)

        requested_dpi = max(
            float(base_dpi),
            (float(min_dim) / min_page_dim) * 72.0,
        )

        max_dpi_by_width = (float(max_image_width) / page_width) * 72.0
        max_dpi_by_height = (float(max_image_height) / page_height) * 72.0
        max_safe_dpi = min(max_dpi_by_width, max_dpi_by_height)

        scale_dpi = min(requested_dpi, max_safe_dpi)

        if scale_dpi <= 0:
            raise ValueError(
                f"Unable to calculate a safe render DPI for page {page_num}"
            )

        estimated_width = max(
            1,
            int(round(page_width * scale_dpi / 72.0)),
        )
        estimated_height = max(
            1,
            int(round(page_height * scale_dpi / 72.0)),
        )

        logger.info(
            "PDF page %d dimensions: %.2f x %.2f pt; "
            "requested DPI=%.2f; safe max DPI=%.2f; selected DPI=%.2f; "
            "estimated image=%sx%s; limits=%sx%s",
            page_num,
            page_width,
            page_height,
            requested_dpi,
            max_safe_dpi,
            scale_dpi,
            estimated_width,
            estimated_height,
            max_image_width,
            max_image_height,
        )

        # Flatten the same PdfPage wrapper and continue using it.
        # Do not fetch a second page wrapper for the same native page.
        _flatten_pdf_page(page_obj)

        image = page_obj.render(scale=scale_dpi / 72.0).to_pil()

        # Avoid allocating a second full-size image when PDFium already
        # returned RGB.
        if image.mode != "RGB":
            converted_image = image.convert("RGB")
            image.close()
            image = converted_image

        if image.width > max_image_width or image.height > max_image_height:
            original_size = image.size
            image.thumbnail(
                (max_image_width, max_image_height),
                Image.Resampling.LANCZOS,
            )
            logger.warning(
                "PDFium rendered page %d slightly above configured limits: "
                "%sx%s -> %sx%s",
                page_num,
                original_size[0],
                original_size[1],
                image.width,
                image.height,
            )

        logger.info(
            "Rendered PDF page %d from %s at %.2f DPI: image size=%sx%s",
            page_num,
            file_name,
            scale_dpi,
            image.width,
            image.height,
        )

        return image

    finally:
        try:
            logger.info(
                "OCR PIPELINE page=%d action=pdfium_page_close_start",
                page_num,
            )
            page_obj.close()
            logger.info(
                "OCR PIPELINE page=%d action=pdfium_page_close_complete",
                page_num,
            )
        except Exception:
            logger.warning(
                "Failed to explicitly close PDFium page %d",
                page_num,
                exc_info=True,
            )


def _encode_image_to_jpeg_bytes(image: Image.Image, page_num: int) -> bytes:
    """Encode a rendered page image to JPEG bytes before dispatching OCR work."""
    logger.info("Encoding page %d image to JPEG for OCR request", page_num)
    jpeg_buffer = io.BytesIO()
    image.save(jpeg_buffer, "JPEG", quality=85, optimize=False)
    jpeg_bytes = jpeg_buffer.getvalue()
    logger.info(
        "Encoded page %d image to JPEG: %d bytes",
        page_num,
        len(jpeg_bytes),
    )
    return jpeg_bytes


def _collect_ocr_future_result(
    future: Any,
    future_to_page: dict[Any, int],
    results: list[tuple[int, str]],
    file_name: str,
    total_pages: int,
) -> None:
    """Collect one completed OCR worker result and append it to results."""
    page_num = future_to_page.pop(future)
    try:
        text = future.result()
    except Exception as e:
        logger.exception("OCR worker failed for page %d", page_num)
        text = f"[Error processing page {page_num}: {e}]"

    if _did_pdf_ocr_page_fail(text):
        logger.warning(
            "PDF OCR failed for %s page %d/%d",
            file_name,
            page_num,
            total_pages,
        )
    else:
        logger.info(
            "Completed OCR for %s page %d/%d",
            file_name,
            page_num,
            total_pages,
        )
    results.append((page_num, text))
    logger.info(
        "OCR PIPELINE page=%d action=ocr_completed remaining_in_flight=%d",
        page_num, len(future_to_page),
    )
    _log_ocr_memory("after_ocr_complete", page_num)


def _ocr_pdf_pages_from_file(
    filepath: str,
    file_name: str,
    endpoint_url: str,
    model_name: str,
    max_tokens: int,
    timeout: int,
    max_concurrent: int,
    min_image_dim: int,
    base_dpi: int,
    max_image_width: int,
    max_image_height: int,
    custom_prompt: str | None = None,
    api_key: str | None = None,
    normalize_pdf: bool = DEFAULT_PDF_OCR_NORMALIZE_PDF,
) -> list[tuple[int, str]]:
    """
    OCR a PDF using a bounded-memory streaming pipeline.

    Pipeline for each page:
        render ONE page
        -> encode JPEG
        -> close/release PIL image immediately
        -> submit JPEG bytes to OCR worker
        -> continue until max_concurrent OCR requests are in flight
        -> wait for FIRST_COMPLETED
        -> continue with the next page

    At no point are all rendered PDF pages retained in memory.
    """
    import pypdfium2 as pdfium

    def process_document(path: str) -> list[tuple[int, str]]:
        logger.info(
            "Opening PDFium document for streaming OCR: %s",
            path,
        )

        doc = pdfium.PdfDocument(path)

        try:
            try:
                doc.init_forms()
            except Exception:
                logger.debug(
                    "Failed to initialize PDF forms before OCR rendering",
                    exc_info=True,
                )

            total_pages = len(doc)

            if total_pages <= 0:
                raise ValueError("PDF contains zero pages")

            worker_count = min(total_pages, max(1, max_concurrent))

            logger.info(
                "OCR PIPELINE CONFIG file=%s pages=%d max_concurrent=%d "
                "min_image_dim=%d base_dpi=%d max_image=%dx%d",
                file_name,
                total_pages,
                worker_count,
                min_image_dim,
                base_dpi,
                max_image_width,
                max_image_height,
            )
            _log_ocr_memory("document_opened")

            results: list[tuple[int, str]] = []
            future_to_page: dict[Any, int] = {}

            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                for page_num in range(1, total_pages + 1):
                    # Keep the number of live OCR request payloads bounded.
                    if len(future_to_page) >= worker_count:
                        logger.info(
                            "OCR PIPELINE action=concurrency_limit_reached "
                            "in_flight=%d/%d; waiting",
                            len(future_to_page),
                            worker_count,
                        )
                        _log_ocr_memory("before_wait", page_num)

                        done_futures, _pending_futures = wait(
                            future_to_page,
                            return_when=FIRST_COMPLETED,
                        )

                        for completed_future in done_futures:
                            _collect_ocr_future_result(
                                completed_future,
                                future_to_page,
                                results,
                                file_name,
                                total_pages,
                            )

                    logger.info(
                        "OCR PIPELINE page=%d/%d action=render_start "
                        "file=%s in_flight=%d/%d",
                        page_num,
                        total_pages,
                        file_name,
                        len(future_to_page),
                        worker_count,
                    )
                    _log_ocr_memory("before_render", page_num)

                    image: Image.Image | None = None
                    jpeg_bytes: bytes | None = None

                    try:
                        image = _render_pdf_page_for_ocr(
                            doc=doc,
                            file_name=file_name,
                            page_num=page_num,
                            min_dim=min_image_dim,
                            base_dpi=base_dpi,
                            max_image_width=max_image_width,
                            max_image_height=max_image_height,
                        )

                        logger.info(
                            "OCR PIPELINE page=%d/%d action=render_complete "
                            "image_size=%sx%s",
                            page_num,
                            total_pages,
                            image.width,
                            image.height,
                        )
                        _log_ocr_memory("after_render", page_num)

                        jpeg_bytes = _encode_image_to_jpeg_bytes(
                            image,
                            page_num,
                        )

                        logger.info(
                            "OCR PIPELINE page=%d/%d "
                            "action=jpeg_encode_complete jpeg_bytes=%d",
                            page_num,
                            total_pages,
                            len(jpeg_bytes),
                        )
                        _log_ocr_memory("after_jpeg_encode", page_num)

                    finally:
                        if image is not None:
                            try:
                                image.close()
                            except Exception:
                                logger.debug(
                                    "Failed to close rendered page image %d",
                                    page_num,
                                    exc_info=True,
                                )

                        image = None

                    logger.info(
                        "OCR PIPELINE page=%d/%d action=pil_image_released "
                        "jpeg_bytes=%d",
                        page_num,
                        total_pages,
                        len(jpeg_bytes) if jpeg_bytes is not None else 0,
                    )
                    _log_ocr_memory("after_pil_release", page_num)

                    if jpeg_bytes is None:
                        raise RuntimeError(
                            f"Page {page_num} produced no JPEG data"
                        )

                    future = executor.submit(
                        _ocr_single_pdf_page_from_jpeg_bytes,
                        jpeg_bytes=jpeg_bytes,
                        page_num=page_num,
                        endpoint_url=endpoint_url,
                        model_name=model_name,
                        max_tokens=max_tokens,
                        timeout=timeout,
                        custom_prompt=custom_prompt,
                        api_key=api_key,
                    )

                    future_to_page[future] = page_num

                    logger.info(
                        "OCR PIPELINE page=%d/%d action=ocr_submitted "
                        "in_flight=%d/%d",
                        page_num,
                        total_pages,
                        len(future_to_page),
                        worker_count,
                    )
                    _log_ocr_memory("after_ocr_submit", page_num)

                    # Drop the local reference. The executor task owns the bytes now.
                    jpeg_bytes = None

                # Drain remaining requests after the final page is submitted.
                for completed_future in as_completed(list(future_to_page)):
                    _collect_ocr_future_result(
                        completed_future,
                        future_to_page,
                        results,
                        file_name,
                        total_pages,
                    )

            return results

        finally:
            logger.info(
                "OCR PIPELINE action=pdfium_document_close_start file=%s",
                file_name,
            )
            doc.close()
            logger.info(
                "OCR PIPELINE action=pdfium_document_close_complete file=%s",
                file_name,
            )
            _log_ocr_memory("document_closed")

    # Attempt 1: original PDF.
    try:
        logger.info(
            "Attempt 1/2: streaming OCR of original PDF with PDFium: %s",
            file_name,
        )
        return process_document(filepath)

    except Exception as first_error:
        logger.warning(
            "Streaming PDFium OCR failed for original PDF %s: %s",
            file_name,
            first_error,
            exc_info=True,
        )

        if not normalize_pdf:
            raise

        normalized_path = f"{filepath}.normalized.pdf"

        try:
            if not _normalize_pdf_with_ghostscript(
                source_path=filepath,
                output_path=normalized_path,
                file_name=file_name,
            ):
                raise first_error

            logger.info(
                "Attempt 2/2: streaming OCR of normalized PDF with PDFium: %s",
                file_name,
            )

            return process_document(normalized_path)

        except Exception:
            logger.exception(
                "Streaming PDFium OCR failed for normalized PDF %s",
                file_name,
            )
            raise

        finally:
            try:
                if os.path.exists(normalized_path):
                    os.remove(normalized_path)
            except Exception:
                logger.debug(
                    "Failed to remove normalized PDF %s",
                    normalized_path,
                    exc_info=True,
                )


def _ocr_single_pdf_page_from_jpeg_bytes(
    jpeg_bytes: bytes,
    page_num: int,
    endpoint_url: str,
    model_name: str,
    max_tokens: int,
    timeout: int,
    custom_prompt: str | None = None,
    api_key: str | None = None,
) -> str:
    """Send JPEG bytes for one rendered PDF page to the vLLM server."""
    prompt = (
        custom_prompt.replace("{page_num}", str(page_num))
        if custom_prompt
        else _default_pdf_ocr_prompt(page_num)
    )

    try:
        image_base64 = base64.b64encode(jpeg_bytes).decode("utf-8")
        image_url = f"data:image/jpeg;base64,{image_base64}"

        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        logger.info("Sending OCR HTTP request for page %d to %s", page_num, endpoint_url)
        session = _get_http_session()
        response = session.post(
            endpoint_url,
            json={
                "model": model_name,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ],
                    }
                ],
                "max_tokens": max_tokens,
                "temperature": 0.1,
            },
            headers=headers,
            timeout=timeout,
        )
        logger.info(
            "Received OCR HTTP response for page %d: status=%d",
            page_num,
            response.status_code,
        )

        if response.status_code != 200:
            return f"[Error processing page {page_num}: HTTP {response.status_code} - {response.text[:500]}]"

        result = response.json()
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        logger.info(
            "Parsed OCR response for page %d: content_length=%d",
            page_num,
            len(content),
        )
        return content.strip()
    except Exception as e:
        logger.exception("OCR request failed for page %d", page_num)
        return f"[Error processing page {page_num}: {e}]"


def _ocr_single_pdf_page(
    image: Image.Image,
    page_num: int,
    endpoint_url: str,
    model_name: str,
    max_tokens: int,
    timeout: int,
    custom_prompt: str | None = None,
    api_key: str | None = None,
) -> str:
    """Send a rendered PDF page image to the vLLM server and return Markdown text."""
    try:
        jpeg_bytes = _encode_image_to_jpeg_bytes(image, page_num)
    except Exception as e:
        logger.exception("Failed to encode page %d image for OCR", page_num)
        return f"[Error processing page {page_num}: {e}]"

    return _ocr_single_pdf_page_from_jpeg_bytes(
        jpeg_bytes=jpeg_bytes,
        page_num=page_num,
        endpoint_url=endpoint_url,
        model_name=model_name,
        max_tokens=max_tokens,
        timeout=timeout,
        custom_prompt=custom_prompt,
        api_key=api_key,
    )


async def async_pdf_ocr_to_markdown(
    file: IO[Any],
    file_name: str = "document.pdf",
    *,
    endpoint_url: str | None = None,
    api_key: str | None = None,
    model_name: str | None = None,
    max_tokens: int | None = None,
    timeout: int | None = None,
    max_concurrent: int | None = None,
    min_image_dim: int | None = None,
    base_dpi: int | None = None,
    max_image_width: int | None = None,
    max_image_height: int | None = None,
    custom_prompt: str | None = None,
    include_page_headers: bool = True,
    normalize_pdf: bool | None = None,
) -> str:
    """OCR a PDF file-like object through vLLM and return Markdown content."""
    logger.info("Starting PDF OCR for %s", file_name)
    resolved_endpoint_url = endpoint_url or os.getenv(
        PDF_OCR_VLLM_SERVER_URL_ENV, DEFAULT_PDF_OCR_VLLM_SERVER_URL
    )
    resolved_api_key = (
        api_key
        or os.getenv(PDF_OCR_VLLM_API_KEY_ENV)
        or DEFAULT_PDF_OCR_VLLM_API_KEY
    )
    resolved_model_name = model_name or os.getenv(
        PDF_OCR_VLLM_MODEL_NAME_ENV, DEFAULT_PDF_OCR_MODEL_NAME
    )
    resolved_max_tokens = max_tokens or _env_int(
        PDF_OCR_MAX_TOKENS_ENV, DEFAULT_PDF_OCR_MAX_TOKENS
    )
    resolved_timeout = timeout or _env_int(
        PDF_OCR_DEFAULT_TIMEOUT_ENV, DEFAULT_PDF_OCR_TIMEOUT
    )
    resolved_max_concurrent = max_concurrent or _env_int(
        PDF_OCR_MAX_CONCURRENT_ENV, DEFAULT_PDF_OCR_MAX_CONCURRENT
    )
    resolved_min_image_dim = min_image_dim or _env_int(
        PDF_OCR_MIN_IMAGE_DIM_ENV, DEFAULT_PDF_OCR_MIN_IMAGE_DIM
    )
    resolved_base_dpi = base_dpi or _env_int(
        PDF_OCR_BASE_DPI_ENV, DEFAULT_PDF_OCR_BASE_DPI
    )
    resolved_max_image_width = max_image_width or _env_int(
        PDF_OCR_MAX_IMAGE_WIDTH_ENV, DEFAULT_PDF_OCR_MAX_IMAGE_WIDTH
    )
    resolved_max_image_height = max_image_height or _env_int(
        PDF_OCR_MAX_IMAGE_HEIGHT_ENV, DEFAULT_PDF_OCR_MAX_IMAGE_HEIGHT
    )
    resolved_custom_prompt = custom_prompt or os.getenv(PDF_OCR_CUSTOM_PROMPT_ENV)

    normalize_pdf_env = os.getenv(PDF_OCR_NORMALIZE_PDF_ENV)
    if normalize_pdf_env is None:
        resolved_normalize_pdf = DEFAULT_PDF_OCR_NORMALIZE_PDF
    else:
        resolved_normalize_pdf = normalize_pdf_env.strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

    if normalize_pdf is not None:
        resolved_normalize_pdf = normalize_pdf

    if resolved_max_image_width <= 0:
        raise ValueError("max_image_width must be greater than zero")

    if resolved_max_image_height <= 0:
        raise ValueError("max_image_height must be greater than zero")

    try:
        file.seek(0)
        suffix = Path(file_name).suffix or ".pdf"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as temp_pdf:
            temp_pdf.write(file.read())
            temp_pdf.flush()
            results = await asyncio.to_thread(
                _ocr_pdf_pages_from_file,
                temp_pdf.name,
                file_name,
                resolved_endpoint_url,
                resolved_model_name,
                resolved_max_tokens,
                resolved_timeout,
                resolved_max_concurrent,
                resolved_min_image_dim,
                resolved_base_dpi,
                resolved_max_image_width,
                resolved_max_image_height,
                resolved_custom_prompt,
                resolved_api_key,
                resolved_normalize_pdf,
            )
    except Exception as e:
        logger.exception("Failed to render PDF for OCR")
        return f"[Error processing PDF: {e}]"
    finally:
        try:
            file.seek(0)
        except Exception:
            pass

    if not results:
        return "[Error processing PDF: no pages were rendered]"

    results = sorted(results, key=lambda item: item[0])
    failed_pages = sum(1 for _page_num, text in results if _did_pdf_ocr_page_fail(text))
    logger.info(
        "Finished PDF OCR for %s: %d pages processed, %d succeeded, %d failed",
        file_name,
        len(results),
        len(results) - failed_pages,
        failed_pages,
    )

    chunks: list[str] = []
    title = Path(file_name).stem or "document"
    if include_page_headers:
        chunks.append(f"# {title}\n\n---\n\n")

    for page_num, text in results:
        if include_page_headers:
            chunks.append(f"## Page {page_num}\n\n")
        chunks.append(f"{text}\n\n")
        if include_page_headers:
            chunks.append("---\n\n")

    return "".join(chunks).strip()


def pdf_ocr_to_markdown(
    file: IO[Any],
    file_name: str = "document.pdf",
    *,
    endpoint_url: str | None = None,
    api_key: str | None = None,
    model_name: str | None = None,
    max_tokens: int | None = None,
    timeout: int | None = None,
    max_concurrent: int | None = None,
    min_image_dim: int | None = None,
    base_dpi: int | None = None,
    max_image_width: int | None = None,
    max_image_height: int | None = None,
    custom_prompt: str | None = None,
    include_page_headers: bool = True,
    normalize_pdf: bool | None = None,
) -> str:
    """Synchronous wrapper around async_pdf_ocr_to_markdown."""
    coroutine = async_pdf_ocr_to_markdown(
        file,
        file_name,
        endpoint_url=endpoint_url,
        api_key=api_key,
        model_name=model_name,
        max_tokens=max_tokens,
        timeout=timeout,
        max_concurrent=max_concurrent,
        min_image_dim=min_image_dim,
        base_dpi=base_dpi,
        max_image_width=max_image_width,
        max_image_height=max_image_height,
        custom_prompt=custom_prompt,
        include_page_headers=include_page_headers,
        normalize_pdf=normalize_pdf,
    )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    # If this sync wrapper is called from a thread that already has an active
    # event loop, run the coroutine in a short-lived helper thread instead of
    # trying to nest event loops.
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lambda: asyncio.run(coroutine))
        return future.result()
