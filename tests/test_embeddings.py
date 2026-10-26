"""Offline tests for the embedding backends' query/passage split."""

from sparql_llm.embeddings import LocalEmbedding, OpenAIEmbedding


class FakeInner:
    """A fastembed stand-in that records the texts it was asked to embed."""

    embedding_size = 3

    def __init__(self):
        self.seen: list[str] = []

    def embed(self, texts, **kwargs):
        texts = list(texts)
        self.seen += texts
        return [[0.0, 0.0, 0.0] for _ in texts]


def local(model: str) -> tuple[LocalEmbedding, FakeInner]:
    emb = LocalEmbedding.__new__(LocalEmbedding)
    emb.model = model
    emb._inner = FakeInner()
    return emb, emb._inner


def test_e5_prefixes_queries_and_passages():
    emb, inner = local("intfloat/multilingual-e5-large")
    list(emb.embed(["Count all persons"]))
    list(emb.embed_query(["How many people?"]))
    assert inner.seen == ["passage: Count all persons", "query: How many people?"]


def test_other_local_models_get_no_prefix():
    emb, inner = local("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    list(emb.embed(["a"]))
    list(emb.embed_query(["b"]))
    assert inner.seen == ["a", "b"]


def test_openai_backend_embeds_queries_like_passages():
    emb = OpenAIEmbedding.__new__(OpenAIEmbedding)
    emb.embed = lambda texts, **kw: [f"vec:{t}" for t in texts]
    assert list(emb.embed_query(["q"])) == ["vec:q"]
