import asyncio

from app.config.settings import get_settings
from app.providers.embeddings import build_embedding_provider
from app.repositories.db import create_engine, create_session_factory
from app.retrieval.ingest import KnowledgeIngestor
from app.retrieval.policy_manifest import known_merchants


async def main() -> None:
    settings = get_settings()
    embedder = build_embedding_provider(settings)
    engine=create_engine(settings)
    try:
        merchants=await known_merchants(create_session_factory(engine))
        chunks, records = await KnowledgeIngestor(settings.knowledge_dir, embedder, settings=settings, merchants=merchants).load()
    finally:
        await engine.dispose()
    print(f"ingested chunks={len(chunks)} vectors={len(records)}")


if __name__ == "__main__":
    asyncio.run(main())
