from __future__ import annotations

import asyncio
import logging
import mimetypes
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, TypedDict

import anyio
import httpx
from pydantic import BaseModel, BeforeValidator, Field, SecretStr, TypeAdapter
from pydantic_ai import RunContext
from pydantic_settings import BaseSettings, SettingsConfigDict, YamlConfigSettingsSource

from octomate.agents.manager import SkillDeps, SkillManager

logger = logging.getLogger(__name__)

http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)


class StreamifyConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="STREAMIFY_",
        yaml_file=["octomate.default.yaml", "octomate.yaml"],
        yaml_config_section="streamify",
    )

    base_url: str = "https://app.dev1.internal.holovita.ai"
    api_version: str = "v1"
    username: str = ""
    password: SecretStr = SecretStr("")
    device_id: str = "7608e47d-a8f5-4f43-9c9f-534783b2ccfb"
    device_name: str = "Octomate Streamify"
    device_model: str = "Bot/1.0"
    os_name: str = "Linux"
    os_version: str = "1.0"
    app_version: str = "1.0.0"
    cache_dir: Path = Path(".octomate/streamify/files")

    @property
    def api_base(self) -> str:
        return f"{self.base_url}/api/{self.api_version}"

    @classmethod
    def settings_customise_sources(cls, settings_cls, **kwargs):
        return (
            kwargs["init_settings"],
            kwargs["env_settings"],
            YamlConfigSettingsSource(settings_cls),
            kwargs["file_secret_settings"],
        )


class TokenManager:
    _access_token: str | None
    _refresh_token: str | None
    _expires_at: float
    _lock: asyncio.Lock

    def __init__(self) -> None:
        self._access_token = None
        self._refresh_token = None
        self._expires_at = 0
        self._lock = asyncio.Lock()

    def _get_headers(self, config: StreamifyConfig) -> dict[str, str]:
        return {
            "User-Agent": f"{config.device_name}/{config.app_version}",
            "X-Device-Id": config.device_id,
            "X-Device-Name": config.device_name,
            "X-Device-Model": config.device_model,
            "X-Os-Name": config.os_name,
            "X-Os-Version": config.os_version,
            "X-App-Version": config.app_version,
            "X-Timezone": "Asia/Shanghai",
            "Content-Type": "application/json",
        }

    async def _login(self, config: StreamifyConfig) -> bool:
        logger.info("Logging in to Streamify API...")
        try:
            response = await http_client.post(
                f"{config.api_base}/session",
                json={
                    "email": config.username,
                    "password": config.password.get_secret_value(),
                },
                headers=self._get_headers(config),
            )
            response.raise_for_status()
            data = response.json()
            self._access_token = data["access_token"]
            self._refresh_token = data["refresh_token"]
            self._expires_at = time.time() + data.get("expires_in", 3600) - 60
            logger.info("Successfully logged in to Streamify API")
            return True
        except httpx.HTTPStatusError as e:
            logger.error(
                "Login failed: %s - %s", e.response.status_code, e.response.text
            )
            return False
        except Exception as e:
            logger.error("Login failed: %s", e)
            return False

    async def _refresh(self, config: StreamifyConfig) -> bool:
        if not self._refresh_token:
            return False
        logger.debug("Refreshing Streamify access token...")
        try:
            response = await http_client.put(
                f"{config.api_base}/session",
                json={"refresh_token": self._refresh_token},
                headers=self._get_headers(config),
            )
            response.raise_for_status()
            data = response.json()
            self._access_token = data["access_token"]
            self._refresh_token = data["refresh_token"]
            self._expires_at = time.time() + data.get("expires_in", 3600) - 60
            return True
        except httpx.HTTPStatusError as e:
            logger.warning("Token refresh failed: %s", e.response.status_code)
            self._access_token = None
            self._refresh_token = None
            self._expires_at = 0
            return False
        except Exception as e:
            logger.warning("Token refresh failed: %s", e)
            return False

    async def get_auth_headers(self, config: StreamifyConfig) -> dict[str, str]:
        async with self._lock:
            if self._access_token and time.time() < self._expires_at:
                pass
            elif self._refresh_token:
                if not await self._refresh(config):
                    if not await self._login(config):
                        raise RuntimeError("Failed to authenticate with Streamify API")
            else:
                if not await self._login(config):
                    raise RuntimeError("Failed to authenticate with Streamify API")
            headers = self._get_headers(config)
            headers["Authorization"] = f"Bearer {self._access_token}"
            return headers


token_manager = TokenManager()


def _parse_timestamp(v: Any) -> datetime:
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, tz=timezone.utc)
    return v


Timestamp = Annotated[datetime, BeforeValidator(_parse_timestamp)]


class FileInfo(BaseModel):
    id: str
    filename: str
    s3_id: str
    content_hash: str
    content_hash_md5: str
    size: int
    mimetype: str
    parent_file_id: str | None = None
    is_public: bool = False
    expires_at: Timestamp | None = None
    created_at: Timestamp | None = None
    updated_at: Timestamp | None = None


class KeyFrame(BaseModel):
    id: str
    milliseconds: int
    description: str | None = None
    files: dict[str, FileInfo] = Field(default_factory=dict)


class ArticleInfo(BaseModel):
    id: str
    title: str | None = None
    description: str | None = None
    author: str | None = None
    site_name: str | None = None
    url: str | None = None
    source: str = ""
    status: str = ""
    language: str = "en"
    published_date: Timestamp | None = None
    error_message: str | None = None
    metadata: dict[str, Any] | None = None
    files: dict[str, FileInfo] = Field(default_factory=dict)
    key_frames: list[KeyFrame] = Field(default_factory=list)
    origin_files: list[FileInfo] = Field(default_factory=list)
    generated_files: dict[str, FileInfo] = Field(default_factory=dict)
    created_at: Timestamp | None = None
    updated_at: Timestamp | None = None


class TagInfo(BaseModel):
    id: str
    name: str
    color: str


class NoteInfo(BaseModel):
    id: str
    user_id: str | None = None
    article_id: str | None = None
    comment: str | None = None
    status: str = ""
    error_message: str | None = None
    is_public: bool = False
    files: dict[str, FileInfo] = Field(default_factory=dict)
    key_frames: list[KeyFrame] = Field(default_factory=list)
    article: ArticleInfo | None = None
    tags: list[TagInfo] = Field(default_factory=list)
    created_at: Timestamp | None = None
    updated_at: Timestamp | None = None

    @property
    def title(self) -> str:
        if self.article and self.article.title:
            return self.article.title
        return f"Note {self.id[:8]}"

    def get_all_file_ids(self) -> list[str]:
        file_ids: list[str] = []
        for f in self.files.values():
            file_ids.append(f.id)
        if self.article:
            for f in self.article.files.values():
                file_ids.append(f.id)
            for f in self.article.origin_files:
                file_ids.append(f.id)
            for f in self.article.generated_files.values():
                file_ids.append(f.id)
        return file_ids


class ChunkResult(BaseModel):
    id: str
    embedded_text: str
    article_id: str
    note_id: str | None = None
    file_id: str | None = None
    file: FileInfo | None = None
    created_at: Timestamp | None = None
    updated_at: Timestamp | None = None


class NoteSearchResult(BaseModel):
    article_id: str
    note_id: str | None = None
    score: float
    note: NoteInfo | None = None

    @property
    def title(self) -> str:
        if self.note and self.note.article:
            return self.note.article.title or f"Note {self.note.id[:8]}"
        return f"Article {self.article_id[:8]}"


class SearchNotesResult(BaseModel):
    query: str
    results: list[NoteSearchResult]


class SearchChunksResult(BaseModel):
    query: str
    results: list[ChunkResult]


class CreateNoteResult(BaseModel):
    success: bool
    note: NoteInfo | None = None
    message: str = ""


class NoteListPage(TypedDict):
    notes: list[NoteInfo]
    total: int
    limit: int
    offset: int


class DeleteNoteResult(BaseModel):
    success: bool
    message: str = ""


class DownloadFileResult(BaseModel):
    success: bool
    local_path: str | None = None
    file_id: str | None = None
    message: str = ""


class BulkDownloadResult(BaseModel):
    results: list[DownloadFileResult]

    @property
    def successful(self) -> list[DownloadFileResult]:
        return [r for r in self.results if r.success]

    @property
    def failed(self) -> list[DownloadFileResult]:
        return [r for r in self.results if not r.success]


note_search_result_list_adapter = TypeAdapter(list[NoteSearchResult])
chunk_result_list_adapter = TypeAdapter(list[ChunkResult])
note_list_page_adapter = TypeAdapter(NoteListPage)


async def _api_get(
    config: StreamifyConfig,
    endpoint: str,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    headers = await token_manager.get_auth_headers(config)
    return await http_client.get(
        f"{config.api_base}{endpoint}",
        headers=headers,
        params=params,
    )


async def _api_post(
    config: StreamifyConfig,
    endpoint: str,
    json_data: dict[str, Any] | None = None,
) -> httpx.Response:
    headers = await token_manager.get_auth_headers(config)
    return await http_client.post(
        f"{config.api_base}{endpoint}",
        headers=headers,
        json=json_data,
    )


async def _api_delete(
    config: StreamifyConfig,
    endpoint: str,
) -> httpx.Response:
    headers = await token_manager.get_auth_headers(config)
    return await http_client.delete(
        f"{config.api_base}{endpoint}",
        headers=headers,
    )


async def _download_single_file(
    config: StreamifyConfig,
    note_id: str,
    file_id: str,
) -> DownloadFileResult:
    try:
        cache = anyio.Path(config.cache_dir)
        await cache.mkdir(parents=True, exist_ok=True)

        prefix = f"{file_id}."
        async for existing in cache.iterdir():
            if existing.name.startswith(prefix):
                logger.debug("File cache hit: %s", existing)
                return DownloadFileResult(
                    success=True,
                    local_path=str(await existing.absolute()),
                    file_id=file_id,
                    message="File retrieved from cache",
                )

        logger.info("Downloading file: %s (note: %s)", file_id, note_id)
        headers = await token_manager.get_auth_headers(config)
        response = await http_client.get(
            f"{config.api_base}/notes/{note_id}/files/{file_id}/content",
            headers=headers,
        )
        response.raise_for_status()

        content_type = response.headers.get("content-type", "application/octet-stream")
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".bin"
        file_path = cache / f"{file_id}{ext}"
        await file_path.write_bytes(response.content)

        logger.info("Downloaded file to: %s", file_path)
        return DownloadFileResult(
            success=True,
            local_path=str(await file_path.absolute()),
            file_id=file_id,
        )
    except httpx.HTTPStatusError as e:
        msg = f"Download failed for {file_id}: {e.response.status_code}"
        logger.error(msg)
        return DownloadFileResult(success=False, file_id=file_id, message=msg)
    except Exception as e:
        msg = f"Download failed for {file_id}: {e}"
        logger.error(msg)
        return DownloadFileResult(success=False, file_id=file_id, message=msg)


def register(manager: SkillManager) -> None:
    config = StreamifyConfig()
    toolset = manager.register(
        name="streamify",
        description=(
            "Search and manage notes in the Streamify knowledge base (Holovita). "
            "Provides semantic search across saved articles and web pages, "
            "chunk-level RAG retrieval for question answering, note creation "
            "from URLs, and file downloading for content access."
        ),
    )

    @toolset.tool
    async def list_notes(
        ctx: RunContext[SkillDeps],
        status: str | None = None,
        source: str | None = None,
    ) -> NoteListPage:
        """List recent notes in the knowledge base.

        Returns up to 10 most recent notes. Use this to browse what's saved
        without a specific search query.

        Args:
            ctx: Run context.
            status: Optional filter by status (queued, processing, ready, error).
            source: Optional filter by source (file, text, or platform name).

        Returns:
            NoteListPage with notes, total count, limit, and offset.
        """
        try:
            params: dict[str, Any] = {"limit": 10, "offset": 0}
            if status:
                params["status"] = status
            if source:
                params["source"] = source
            response = await _api_get(config, "/notes/", params=params)
            response.raise_for_status()
            page = note_list_page_adapter.validate_json(response.content)
            logger.info(
                "Listed %d notes (total: %d)", len(page["notes"]), page["total"]
            )
            return page
        except Exception as e:
            logger.error("List notes failed: %s", e)
            return NoteListPage(notes=[], total=0, limit=10, offset=0)

    @toolset.tool
    async def search_notes(
        ctx: RunContext[SkillDeps],
        query: str,
        limit: int = 5,
    ) -> SearchNotesResult:
        """Search for notes in the knowledge base using semantic search, deduplicated by article.

        Use this tool when searching for saved content by topic or question,
        and you want one result per unique source.

        For more granular chunk-level results, use retrieve_chunks instead.

        Args:
            ctx: Run context.
            query: Natural language search query.
            limit: Maximum number of results to return (default: 5, max: 20).

        Returns:
            SearchNotesResult with query and list of NoteSearchResult.
        """
        try:
            response = await _api_post(
                config, "/retrievals", json_data={"query": query}
            )
            response.raise_for_status()
            all_results = note_search_result_list_adapter.validate_json(
                response.content
            )
            results = all_results[:limit]
            logger.info("Found %d notes for query: %s", len(results), query[:50])
            return SearchNotesResult(query=query, results=results)
        except Exception as e:
            logger.error("Search failed: %s", e)
            return SearchNotesResult(query=query, results=[])

    @toolset.tool
    async def retrieve_chunks(
        ctx: RunContext[SkillDeps],
        query: str,
        limit: int = 10,
    ) -> SearchChunksResult:
        """Retrieve relevant text chunks from the knowledge base using semantic search.

        Returns all matched chunks ranked by relevance, including multiple chunks
        from the same article. This is the primary retrieval tool for question-answering
        workflows where you need specific passages.

        Each chunk's embedded_text contains Markdown. If it has image references like
        `![description](/api/v1/notes/{note_id}/files/{file_id}/content)`, extract
        the note_id and file_id from the URL path and download using download_files.

        Args:
            ctx: Run context.
            query: Natural language search query.
            limit: Maximum number of chunks to return (default: 10, max: 50).

        Returns:
            SearchChunksResult with query and list of ChunkResult.
        """
        try:
            response = await _api_post(
                config, "/retrievals/chunks", json_data={"query": query}
            )
            response.raise_for_status()
            all_results = chunk_result_list_adapter.validate_json(response.content)
            results = all_results[:limit]
            logger.info("Retrieved %d chunks for query: %s", len(results), query[:50])
            return SearchChunksResult(query=query, results=results)
        except Exception as e:
            logger.error("Chunk retrieval failed: %s", e)
            return SearchChunksResult(query=query, results=[])

    @toolset.tool
    async def get_note(
        ctx: RunContext[SkillDeps],
        note_id: str,
    ) -> NoteInfo | None:
        """Retrieve a specific note by its ID.

        Fetches complete note details including article metadata, tags, and file info.

        Args:
            ctx: Run context.
            note_id: The UUID of the note to retrieve.

        Returns:
            NoteInfo with full details, or None if not found.
        """
        try:
            response = await _api_get(config, f"/notes/{note_id}")
            response.raise_for_status()
            note = NoteInfo.model_validate_json(response.content)
            logger.info("Retrieved note: %s", note_id)
            return note
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.warning("Note not found: %s", note_id)
            else:
                logger.error("Get note failed: %s", e.response.status_code)
            return None
        except Exception as e:
            logger.error("Get note failed: %s", e)
            return None

    @toolset.tool
    async def create_note_from_url(
        ctx: RunContext[SkillDeps],
        url: str,
        comment: str | None = None,
    ) -> CreateNoteResult:
        """Save a web page or video to the knowledge base by URL.

        The system will extract article text, metadata, and optionally transcribe
        videos. Processing happens asynchronously — poll with get_note to check.

        Args:
            ctx: Run context.
            url: Full URL of the web page or video (must include https://).
            comment: Optional user comment to attach to the note.

        Returns:
            CreateNoteResult with success status and created note info.
        """
        try:
            payload: dict[str, Any] = {"text": url}
            if comment:
                payload["comment"] = comment
            response = await _api_post(config, "/notes/text", json_data=payload)
            response.raise_for_status()
            note = NoteInfo.model_validate_json(response.content)
            logger.info("Created note: %s - %s", note.id, note.title)
            return CreateNoteResult(
                success=True,
                note=note,
                message=f"Note created successfully: {note.title}",
            )
        except httpx.HTTPStatusError as e:
            msg = f"Create note failed: {e.response.status_code}"
            logger.error("%s - %s", msg, e.response.text)
            return CreateNoteResult(success=False, message=msg)
        except Exception as e:
            msg = f"Create note failed: {e}"
            logger.error(msg)
            return CreateNoteResult(success=False, message=msg)

    @toolset.tool
    async def delete_note(
        ctx: RunContext[SkillDeps],
        note_id: str,
    ) -> DeleteNoteResult:
        """Permanently delete a note from the knowledge base. Cannot be undone.

        Args:
            ctx: Run context.
            note_id: The UUID of the note to delete.

        Returns:
            DeleteNoteResult with success status and message.
        """
        try:
            response = await _api_delete(config, f"/notes/{note_id}")
            response.raise_for_status()
            logger.info("Deleted note: %s", note_id)
            return DeleteNoteResult(
                success=True, message=f"Note {note_id} deleted successfully"
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return DeleteNoteResult(
                    success=False, message=f"Note not found: {note_id}"
                )
            msg = f"Delete note failed: {e.response.status_code}"
            logger.error("%s - %s", msg, e.response.text)
            return DeleteNoteResult(success=False, message=msg)
        except Exception as e:
            msg = f"Delete note failed: {e}"
            logger.error(msg)
            return DeleteNoteResult(success=False, message=msg)

    @toolset.tool
    async def download_files(
        ctx: RunContext[SkillDeps],
        note_id: str,
        file_ids: list[str],
    ) -> BulkDownloadResult:
        """Download files associated with a note in parallel.

        Files are accessed through the note context and cached locally.

        Args:
            ctx: Run context.
            note_id: The UUID of the note that owns the files.
            file_ids: List of file UUIDs to download.

        Returns:
            BulkDownloadResult with per-file success/failure details.
        """
        tasks = [_download_single_file(config, note_id, fid) for fid in file_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        download_results: list[DownloadFileResult] = []
        for r in results:
            if isinstance(r, BaseException):
                logger.error("Download error: %s", r)
                download_results.append(
                    DownloadFileResult(success=False, message=str(r))
                )
            else:
                download_results.append(r)
        return BulkDownloadResult(results=download_results)
