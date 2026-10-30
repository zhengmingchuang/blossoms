import json
import re
import logging
from pathlib import Path
from typing import List, Dict, Optional
from collections import Counter
from config import VECTOR_STORE_DIR

logger = logging.getLogger(__name__)

_store_path = VECTOR_STORE_DIR / "chunks.json"
_chunks: List[Dict] = []
_model = None
_embeddings = None
_use_st = False
_model_loaded = False
_embeddings_dirty = True

try:
    import numpy as np
except ImportError:
    np = None


def _load_model():
    global _model, _use_st, _model_loaded
    if _model_loaded:
        return
    _model_loaded = True
    try:
        from sentence_transformers import SentenceTransformer
        from config import EMBEDDING_MODEL
        _model = SentenceTransformer(EMBEDDING_MODEL)
        _use_st = True
        logger.info("sentence-transformers model loaded")
    except Exception as e:
        logger.warning(f"sentence-transformers unavailable: {e}, using TF-IDF")
        _use_st = False


def _encode(texts: List[str]):
    if not _model_loaded:
        _load_model()
    if _use_st and _model:
        return _model.encode(texts, normalize_embeddings=True, show_progress_bar=False, batch_size=64)
    return None


_jieba_loaded = False

def _tokenize(text: str) -> List[str]:
    global _jieba_loaded
    try:
        import jieba
        if not _jieba_loaded:
            jieba.initialize()
            _jieba_loaded = True
        return [w.lower() for w in jieba.lcut(re.sub(r'[^\w\u4e00-\u9fff]', ' ', text)) if len(w.strip()) > 1]
    except ImportError:
        return [w.lower() for w in re.sub(r'[^\w\u4e00-\u9fff]', ' ', text).split() if len(w) > 1]


def _load_chunks():
    global _chunks
    if _store_path.exists():
        try:
            with open(_store_path, "r", encoding="utf-8") as f:
                _chunks = json.load(f)
        except Exception:
            _chunks = []


def _save():
    with open(_store_path, "w", encoding="utf-8") as f:
        json.dump(_chunks, f, ensure_ascii=False)


def _ensure_embeddings():
    global _embeddings, _embeddings_dirty
    if not _embeddings_dirty:
        return
    _embeddings_dirty = False
    if not _chunks:
        _embeddings = None
        return
    if not _model_loaded:
        _load_model()
    if _use_st and len(_chunks) <= 5000:
        texts = [c.get("chunk_text", "")[:500] for c in _chunks]
        _embeddings = _encode(texts)
    else:
        _embeddings = None


def add_chunks(file_id: int, file_name: str, chunks: List[Dict], user_id: int = None):
    global _embeddings_dirty
    for ch in chunks:
        _chunks.append({"file_id": file_id, "file_name": file_name, "user_id": user_id, "chunk_text": ch["chunk_text"], "chunk_type": ch.get("chunk_type", ""), "source_position": ch.get("source_position", "")})
    _save()
    _embeddings_dirty = True
    _invalidate_token_cache()


def remove_file(file_id: int):
    global _chunks, _embeddings_dirty
    _chunks = [c for c in _chunks if c["file_id"] != file_id]
    _save()
    _embeddings_dirty = True
    _invalidate_token_cache()


def search(query: str, top_k: int = 5, user_id: int = None) -> List[Dict]:
    if not _chunks:
        return []
    # 按 user_id 过滤
    if user_id is not None:
        pool = [c for c in _chunks if c.get("user_id") == user_id]
    else:
        pool = _chunks
    if not pool:
        return []
    if _embeddings is not None and np is not None:
        # 需要找到 pool 中元素在 _chunks 中的原始索引
        if user_id is not None:
            indices = [i for i, c in enumerate(_chunks) if c.get("user_id") == user_id]
            qe = _encode([query])[0]
            scores = _embeddings @ qe
            ranked = sorted(indices, key=lambda i: scores[i], reverse=True)[:top_k]
            return [{"score": round(float(scores[i]), 4), **_chunks[i]} for i in ranked if scores[i] > 0.01]
        else:
            qe = _encode([query])[0]
            scores = _embeddings @ qe
            ranked = sorted(range(len(_chunks)), key=lambda i: scores[i], reverse=True)[:top_k]
            return [{"score": round(float(scores[i]), 4), **_chunks[i]} for i in ranked if scores[i] > 0.01]
    return _tfidf_search(query, top_k, user_id=user_id)


_token_cache: Dict[int, set] = {}


def _get_chunk_tokens(idx: int, text: str) -> set:
    if idx not in _token_cache:
        _token_cache[idx] = set(_tokenize(text[:500]))
    return _token_cache[idx]


def _invalidate_token_cache():
    global _token_cache
    _token_cache = {}


def _tfidf_search(query: str, top_k: int, user_id: int = None) -> List[Dict]:
    qt = set(_tokenize(query))
    if not qt:
        pool = [c for c in _chunks if c.get("user_id") == user_id] if user_id is not None else _chunks
        return pool[:top_k]
    results = []
    for i, ch in enumerate(_chunks):
        if user_id is not None and ch.get("user_id") != user_id:
            continue
        ct = _get_chunk_tokens(i, ch.get("chunk_text", ""))
        common = qt & ct
        if common:
            score = len(common) / max(len(qt), 1)
            ct_text = ch.get("chunk_text", "").lower()
            for w in qt:
                if w in ct_text:
                    score += 0.05
            results.append((score, ch))
    results.sort(key=lambda x: x[0], reverse=True)
    return [{"score": round(s, 4), **c} for s, c in results[:top_k]]


def search_for_fields(field_names: List[str], top_k_per_field: int = 3, user_id: int = None) -> List[Dict]:
    """为每个字段用TF-IDF搜索（快速、无需模型），合并去重结果"""
    seen = set()
    results = []
    for fn in field_names:
        hits = _tfidf_search(fn, top_k_per_field, user_id=user_id)
        for h in hits:
            key = (h.get("file_id"), h.get("source_position", ""))
            if key not in seen:
                seen.add(key)
                results.append(h)
    return results


def get_all(file_id: Optional[int] = None) -> List[Dict]:
    if file_id is not None:
        return [c for c in _chunks if c["file_id"] == file_id]
    return list(_chunks)


def get_context(fields: List[str], top_k: int = 8, user_id: int = None) -> List[str]:
    r = search_for_fields(fields, top_k_per_field=3, user_id=user_id)
    seen_texts = set()
    out = []
    for x in r:
        t = x["chunk_text"]
        if t not in seen_texts:
            seen_texts.add(t)
            out.append(t)
        if len(out) >= top_k:
            break
    return out


_load_chunks()
