"""Qurious 시세 수집기.

앱 스택(Postgres·Redis·Qdrant·Neo4j·Ollama)에 기대지 않고 단독으로 돈다.
자세한 배경은 collector/README.md 를 본다.
"""
__all__ = ["config", "db", "raw_store", "ratelimit", "preprocess", "manifest"]
