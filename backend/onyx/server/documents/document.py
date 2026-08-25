import re
from urllib.parse import quote

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from onyx.auth.permissions import require_permission
from onyx.auth.users import current_curator_or_admin_user
from onyx.context.search.models import IndexFilters
from onyx.context.search.preprocessing.access_filters import (
    build_access_filters_for_user,
)
from onyx.db.engine.sql_engine import get_session
from onyx.db.enums import Permission
from onyx.db.models import User
from onyx.db.search_settings import get_current_search_settings
from onyx.document_index.factory import get_default_document_index
from onyx.document_index.interfaces import VespaChunkRequest
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.natural_language_processing.utils import get_tokenizer
from onyx.prompts.prompt_utils import build_doc_context_str
from onyx.server.documents.models import ChunkInfo
from onyx.server.documents.models import DocumentInfo
from onyx.server.utils_vector_db import require_vector_db

router = APIRouter(prefix="/document")


def _document_download_filename(semantic_identifier: str | None) -> str:
    filename = re.sub(
        r'[\x00-\x1f\x7f/\\:*?"<>|]',
        "_",
        semantic_identifier or "document",
    ).strip(" .")
    return f"{filename or 'document'}.md"


# Have to use a query parameter as FastAPI is interpreting the URL type document_ids
# as a different path
@router.get("/document-size-info", dependencies=[Depends(require_vector_db)])
def get_document_info(
    document_id: str = Query(...),
    user: User = Depends(require_permission(Permission.BASIC_ACCESS)),
    db_session: Session = Depends(get_session),
) -> DocumentInfo:
    search_settings = get_current_search_settings(db_session)
    # This flow is for search so we do not get all indices.
    document_index = get_default_document_index(search_settings, None, db_session)

    user_acl_filters = build_access_filters_for_user(user, db_session)
    inference_chunks = document_index.id_based_retrieval(
        chunk_requests=[VespaChunkRequest(document_id=document_id)],
        filters=IndexFilters(access_control_list=user_acl_filters),
    )

    if not inference_chunks:
        raise HTTPException(status_code=404, detail="Document not found")

    contents = [chunk.content for chunk in inference_chunks]

    combined_contents = "\n".join(contents)

    # get actual document context used for LLM
    first_chunk = inference_chunks[0]
    tokenizer_encode = get_tokenizer(
        provider_type=search_settings.provider_type,
        model_name=search_settings.model_name,
    ).encode
    full_context_str = build_doc_context_str(
        semantic_identifier=first_chunk.semantic_identifier,
        source_type=first_chunk.source_type,
        content=combined_contents,
        metadata_dict=first_chunk.metadata,
        updated_at=first_chunk.updated_at,
        ind=0,
    )

    return DocumentInfo(
        num_chunks=len(inference_chunks),
        num_tokens=len(tokenizer_encode(full_context_str)),
    )


@router.get("/document-content", dependencies=[Depends(require_vector_db)])
def download_document_content(
    document_id: str = Query(...),
    user: User = Depends(current_curator_or_admin_user),
    db_session: Session = Depends(get_session),
) -> Response:
    """Download all indexed content for a document as a plain-text file."""
    search_settings = get_current_search_settings(db_session)
    document_index = get_default_document_index(search_settings, None, db_session)

    user_acl_filters = build_access_filters_for_user(user, db_session)
    inference_chunks = document_index.id_based_retrieval(
        chunk_requests=[VespaChunkRequest(document_id=document_id)],
        filters=IndexFilters(access_control_list=user_acl_filters),
    )

    if not inference_chunks:
        raise OnyxError(OnyxErrorCode.DOCUMENT_NOT_FOUND, "Document not found")

    inference_chunks.sort(key=lambda chunk: chunk.chunk_id)
    document_content = "\n\n".join(chunk.content for chunk in inference_chunks)
    filename = _document_download_filename(inference_chunks[0].semantic_identifier)

    return Response(
        content=document_content,
        media_type="text/plain",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
        },
    )


@router.get("/chunk-info", dependencies=[Depends(require_vector_db)])
def get_chunk_info(
    document_id: str = Query(...),
    chunk_id: int = Query(...),
    user: User = Depends(require_permission(Permission.BASIC_ACCESS)),
    db_session: Session = Depends(get_session),
) -> ChunkInfo:
    search_settings = get_current_search_settings(db_session)
    # This flow is for search so we do not get all indices.
    document_index = get_default_document_index(search_settings, None, db_session)

    user_acl_filters = build_access_filters_for_user(user, db_session)
    chunk_request = VespaChunkRequest(
        document_id=document_id,
        min_chunk_ind=chunk_id,
        max_chunk_ind=chunk_id,
    )

    inference_chunks = document_index.id_based_retrieval(
        chunk_requests=[chunk_request],
        filters=IndexFilters(access_control_list=user_acl_filters),
        batch_retrieval=True,
    )

    if not inference_chunks:
        raise HTTPException(status_code=404, detail="Chunk not found")

    chunk_content = inference_chunks[0].content

    tokenizer_encode = get_tokenizer(
        provider_type=search_settings.provider_type,
        model_name=search_settings.model_name,
    ).encode

    return ChunkInfo(
        content=chunk_content, num_tokens=len(tokenizer_encode(chunk_content))
    )
