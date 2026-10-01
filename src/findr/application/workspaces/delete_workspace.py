import logging
from collections.abc import Callable

from findr.application.workspaces.manage_workspaces import GetWorkspace
from findr.domain.value_objects import SourceType
from findr.ports.credential_store import CredentialStore
from findr.ports.document_repository import DocumentRepository
from findr.ports.file_storage import FileStorage
from findr.ports.oauth_provider import OAuthProvider
from findr.ports.search_index import SearchIndex
from findr.ports.source_connection_repo import SourceConnectionRepository
from findr.ports.unit_of_work import UnitOfWork
from findr.ports.uploaded_file_repository import UploadedFileRepository
from findr.ports.workspace_repository import WorkspaceRepository

logger = logging.getLogger(__name__)


class DeleteWorkspace:
    """Deletes a workspace and everything in it — see specs/workspaces.md §4.

    Postgres changes commit together first; stored files and search-index
    entries are removed after the commit. A failure there leaves orphans
    that can never be searched (their workspace no longer exists), never a
    half-deleted workspace.
    """

    def __init__(
        self,
        workspaces: WorkspaceRepository,
        connections: SourceConnectionRepository,
        credential_store: CredentialStore,
        oauth_provider_for: Callable[[SourceType], OAuthProvider],
        uploads: UploadedFileRepository,
        documents: DocumentRepository,
        search_index: SearchIndex,
        file_storage: FileStorage,
        unit_of_work: UnitOfWork,
    ) -> None:
        self._workspaces = workspaces
        self._connections = connections
        self._credentials = credential_store
        self._oauth_provider_for = oauth_provider_for
        self._uploads = uploads
        self._documents = documents
        self._search_index = search_index
        self._storage = file_storage
        self._uow = unit_of_work

    def execute(self, workspace_id: int, user_id: int) -> None:
        GetWorkspace(self._workspaces).execute(workspace_id, user_id)

        connections = self._connections.list_for_workspace(workspace_id)
        for connection in connections:
            self._revoke(connection.id, connection.source_type)
            self._credentials.delete(connection.id)

        uploads = self._uploads.list_for_workspace(workspace_id)
        # Children before parents: uploads reference documents, documents
        # reference connections, everything references the workspace.
        for upload in uploads:
            self._uploads.delete(upload.id)
        self._documents.delete_for_workspace(workspace_id)
        for connection in connections:
            self._connections.delete(connection.id)
        self._workspaces.delete(workspace_id)
        self._uow.commit()

        for upload in uploads:
            try:
                self._storage.delete(upload.storage_path)
            except Exception:  # noqa: BLE001 - orphaned file, logged
                logger.exception("Could not delete stored file %s", upload.storage_path)
        self._search_index.delete_workspace(workspace_id)

    def _revoke(self, connection_id: int, source_type: SourceType) -> None:
        if source_type == SourceType.FILE:
            return
        credentials = self._credentials.get(connection_id)
        if credentials is None:
            return
        try:
            self._oauth_provider_for(source_type).revoke(credentials)
        except Exception:  # noqa: BLE001 - best effort; don't block the delete
            logger.exception("Could not revoke connection %s", connection_id)
