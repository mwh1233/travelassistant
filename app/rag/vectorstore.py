"""
Vector store management.
"""
from pathlib import Path
from typing import List

from langchain_chroma import Chroma
from langchain_community.embeddings import DashScopeEmbeddings
from langchain_core.documents import Document

from app.config import settings
from app.utils.logger import app_logger


class VectorStoreManager:
    """Manage Chroma vector store lifecycle."""

    def __init__(
        self,
        persist_directory: str = "data/vectorstore",
        collection_name: str = "travel_guides",
    ):
        self.persist_directory = Path(persist_directory)
        self.collection_name = collection_name

        self.persist_directory.mkdir(parents=True, exist_ok=True)

        self.embeddings = DashScopeEmbeddings(
            model="text-embedding-v2",
            dashscope_api_key=settings.dashscope_api_key,
        )

        self.vectorstore: Chroma | None = None

    def create_vectorstore(self, documents: List[Document]) -> Chroma:
        """Create a new vector store from documents."""
        app_logger.info(f"Creating vector store ({len(documents)} documents)...")

        self.vectorstore = Chroma.from_documents(
            documents=documents,
            embedding=self.embeddings,
            persist_directory=str(self.persist_directory),
            collection_name=self.collection_name,
        )

        app_logger.info("Vector store created.")
        return self.vectorstore

    def load_vectorstore(self) -> Chroma:
        """Load an existing vector store."""
        app_logger.info("Loading vector store...")

        self.vectorstore = Chroma(
            persist_directory=str(self.persist_directory),
            embedding_function=self.embeddings,
            collection_name=self.collection_name,
        )

        app_logger.info("Vector store loaded.")
        return self.vectorstore

    def get_vectorstore(self) -> Chroma:
        """Get initialized vector store instance."""
        if self.vectorstore is None:
            try:
                return self.load_vectorstore()
            except Exception as exc:
                app_logger.warning("Vector store not found; create it first.")
                raise RuntimeError("Vector store is not initialized") from exc
        return self.vectorstore
