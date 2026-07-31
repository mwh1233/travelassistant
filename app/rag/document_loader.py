"""
Document loading and preprocessing utilities.
"""
from pathlib import Path
from typing import List

from langchain_core.documents import Document

from app.utils.logger import app_logger


class DocumentManager:
    """Manage local travel documents."""

    def __init__(self, base_dir: str | None = None):
        if base_dir is None:
            # document_loader.py -> rag -> app -> project root
            project_root = Path(__file__).parent.parent.parent
            self.base_dir = project_root / "data" / "documents"
        else:
            self.base_dir = Path(base_dir)

    @staticmethod
    def _read_text_with_fallback(file_path: Path) -> str:
        """Read file text using a small set of common encodings."""
        encodings = ["utf-8", "utf-8-sig", "gb18030", "gbk", "big5"]

        for encoding in encodings:
            try:
                return file_path.read_text(encoding=encoding)
            except UnicodeDecodeError:
                continue

        # Keep pipeline running even for mixed/dirty encodings.
        app_logger.warning(f"Cannot reliably decode file, using replacement mode: {file_path}")
        return file_path.read_text(encoding="utf-8", errors="replace")

    def load_destination_documents(self) -> List[Document]:
        """Load all destination markdown documents."""
        destinations_dir = self.base_dir / "destinations"

        if not destinations_dir.exists():
            app_logger.warning(f"Destination documents dir does not exist: {destinations_dir}")
            return []

        documents: List[Document] = []
        markdown_files = sorted(destinations_dir.rglob("*.md"))

        for file_path in markdown_files:
            if not file_path.is_file():
                continue

            content = self._read_text_with_fallback(file_path)
            relative_path = file_path.relative_to(self.base_dir)
            documents.append(
                Document(
                    page_content=content,
                    metadata={
                        "source": str(file_path),
                        "relative_path": str(relative_path),
                    },
                )
            )

        app_logger.info(f"Loaded {len(documents)} destination documents.")

        for doc in documents:
            doc.metadata["source_type"] = "destination_guide"
            doc.metadata["category"] = "destinations"

        return documents

    def load_food_documents(self) -> List[Document]:
        """Load food documents."""
        # TODO: implement when food corpus is added
        return []

    def load_accommodation_documents(self) -> List[Document]:
        """Load accommodation documents."""
        # TODO: implement when accommodation corpus is added
        return []