"""RAG search tools exposed to agents."""

from typing import Optional

from langchain.tools import tool
from langchain_core.tools import ToolException

from app.rag.document_loader import DocumentManager
from app.rag.pipeline import AdvancedRAGPipeline
from app.rag.text_splitter import AdvancedParentDocumentSplitter
from app.rag.vectorstore import VectorStoreManager
from app.utils.logger import app_logger


_rag_pipeline: Optional[AdvancedRAGPipeline] = None
_parent_splitter: Optional[AdvancedParentDocumentSplitter] = None


async def _get_rag_pipeline() -> AdvancedRAGPipeline:
    """Load or initialize the global RAG pipeline."""

    global _rag_pipeline, _parent_splitter

    if _rag_pipeline is None:
        app_logger.info("Initializing RAG pipeline...")
        doc_manager = DocumentManager()
        documents = doc_manager.load_destination_documents()

        if not documents:
            app_logger.warning("No destination documents found; RAG results may be limited")
            documents = []

        _parent_splitter = AdvancedParentDocumentSplitter()
        _, child_docs = _parent_splitter.split_documents(documents)

        vs_manager = VectorStoreManager()
        try:
            vectorstore = vs_manager.load_vectorstore()
            app_logger.info("Loaded existing vectorstore")
        except Exception:
            app_logger.info("Vectorstore not found, creating a new one")
            vectorstore = vs_manager.create_vectorstore(child_docs)

        _rag_pipeline = AdvancedRAGPipeline(
            vectorstore=vectorstore,
            all_documents=child_docs,
            parent_splitter=_parent_splitter,
            query_strategy="multi_query",
            use_llm_reranker=False,
            top_k=3,
            enable_cache=True,
        )

    return _rag_pipeline


def _format_rag_results(documents: list, query: str) -> str:
    """Format RAG results with source and freshness metadata."""

    if not documents:
        return f"未找到与「{query}」相关的信息。"

    result_parts = []
    for index, doc in enumerate(documents, 1):
        content = doc.page_content[:800]
        if len(doc.page_content) > 800:
            content += "..."

        source = doc.metadata.get("source", "未知来源")
        source_title = doc.metadata.get("source_title") or doc.metadata.get("relative_path") or source
        updated_at = doc.metadata.get("updated_at") or "unknown"
        valid_until = doc.metadata.get("valid_until") or "unknown"
        category = doc.metadata.get("category") or "travel"

        result_parts.append(
            f"【资料 {index}】\n"
            f"{content}\n"
            f"来源：{source_title}\n"
            f"类别：{category}\n"
            f"更新时间：{updated_at}\n"
            f"有效期：{valid_until}\n"
            f"时效提示：门票、开放时间、交通和价格信息请在出行前用实时工具复核。"
        )

    return "\n\n".join(result_parts)


async def _search_with_rag(query: str) -> str:
    try:
        pipeline = await _get_rag_pipeline()
        documents = pipeline.retrieve(query)
        app_logger.info(f"RAG search completed, returned {len(documents)} documents")
        return _format_rag_results(documents, query)
    except Exception as exc:
        app_logger.error(f"RAG search failed: {exc}")
        raise ToolException(f"检索过程中出现错误：{exc}") from exc


@tool
async def search_destination_guide(query: str) -> str:
    """Search destination guides, attractions, tickets, routes, and travel advice."""

    return await _search_with_rag(query)


@tool
async def search_food_recommendations(query: str) -> str:
    """Search destination food recommendations."""

    return await _search_with_rag(f"{query} 美食 餐厅 小吃")


@tool
async def search_accommodation_info(query: str) -> str:
    """Search accommodation areas and lodging suggestions."""

    return await _search_with_rag(f"{query} 住宿 酒店 民宿")


@tool
async def search_travel_tips(query: str) -> str:
    """Search practical travel tips and caveats."""

    return await _search_with_rag(f"{query} 注意事项 建议 提示")


def get_rag_tools() -> list:
    """Return all RAG tools."""

    return [
        search_destination_guide,
        search_food_recommendations,
        search_accommodation_info,
        search_travel_tips,
    ]
