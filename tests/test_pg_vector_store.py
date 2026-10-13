"""
Vector search lives in PostgreSQL (pgvector). Unit tests use a fake cursor; the live tests run only when
AIOPS_TEST_PGVECTOR=1 and a pgvector-enabled PostgreSQL is reachable through the usual PG* environment variables.

Run from the project root:  python -m unittest tests.test_pg_vector_store -v
"""
import os
import pathlib
import unittest
import uuid
from unittest.mock import MagicMock, patch

import embedding_store
import knowledge_manager
import pg_vector_store as pv
import retrieval_engine


class FakeCursor:
    """Returns canned rows per query, recording the SQL and parameters it was given."""

    def __init__(self, rows_by_table):
        self.rows_by_table = rows_by_table
        self.calls = []
        self.description = [(n,) for n in ("id", "title", "content", "classification", "domain", "service_scope",
                                           "environment", "severity", "tenant_id", "knowledge_scope",
                                           "source_type", "score")]
        self._rows = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        table = "chunks" if "client_knowledge_chunks" in sql else "incidents"
        scope = "tenant" if "tenant_id = %s" in sql else ("learned" if "'global_learned'" in sql else "base")
        self._rows = list(self.rows_by_table.get((table, scope), []))

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def row(id_, score, source_type="incident", tenant="", scope="global_base", classification="vpn_client_failure"):
    return (id_, "title", "content", classification, "network", "", "", "", tenant, scope, source_type, score)


class HelperTests(unittest.TestCase):
    def test_vector_literal_is_pgvector_text(self):
        self.assertEqual(pv.vector_literal([0.5, -1, 0.25]), "[0.5,-1,0.25]")

    def test_embedding_text_skips_empty_parts_and_is_bounded(self):
        self.assertEqual(pv.embedding_text("Title", "Body", None, "net"), "Title. Body. net")
        self.assertLessEqual(len(pv.embedding_text("t", "x" * 10000, "c", "d")), 4000)

    def test_scope_layers(self):
        self.assertEqual(pv.SCOPE_LAYERS["client_private"], ("tenant", "global_learned", "global_base"))
        self.assertEqual(pv.SCOPE_LAYERS["global_base"], ("global_base",))

    def test_schema_adds_columns_indexes_and_a_stale_embedding_trigger(self):
        for needle in ("CREATE EXTENSION IF NOT EXISTS vector", "vector(384)", "USING hnsw", "vector_cosine_ops",
                       "trg_incidents_stale_embedding", "trg_chunks_stale_embedding", "knowledge_scope"):
            self.assertIn(needle, pv.SCHEMA_SQL)


class SearchTests(unittest.TestCase):
    def search(self, rows, **kwargs):
        cursor = FakeCursor(rows)
        with patch.object(pv, "_connect", return_value=FakeConn(cursor)):
            result = pv.search_multi_scope([0.1] * 384, **kwargs)
        return result, cursor

    def test_client_private_searches_tenant_learned_and_base_layers(self):
        _, cursor = self.search({}, limit=5, tenant_id="client_a", knowledge_scope="client_private")
        sqls = " ".join(c[0] for c in cursor.calls)
        self.assertEqual(len(cursor.calls), 4)  # tenant chunks + tenant incidents + learned + base
        self.assertIn("client_knowledge_chunks", sqls)
        self.assertIn("knowledge_scope = 'global_learned'", sqls)
        self.assertIn("knowledge_scope = 'global_base'", sqls)

    def test_base_scope_never_touches_tenant_data(self):
        _, cursor = self.search({}, limit=5, tenant_id="client_a", knowledge_scope="global_base")
        self.assertEqual(len(cursor.calls), 1)
        self.assertNotIn("client_knowledge_chunks", cursor.calls[0][0])
        self.assertNotIn("client_a", repr(cursor.calls[0][1]))

    def test_tenant_value_is_a_bound_parameter_not_in_the_sql_text(self):
        _, cursor = self.search({}, limit=5, tenant_id="client_a'; DROP TABLE incidents;--")
        for sql, params in cursor.calls:
            self.assertNotIn("DROP TABLE", sql)
        self.assertTrue(any("drop_table" in repr(p) or "DROP" in repr(p).upper() for _, p in cursor.calls if p))

    def test_results_are_merged_by_score_deduped_and_limited(self):
        rows = {("chunks", "tenant"): [row("c1", 0.60, "document", "client_a", "client_private")],
                ("incidents", "base"): [row("i1", 0.90), row("i2", 0.40), row("i1", 0.90)]}
        result, _ = self.search(rows, limit=3, tenant_id="client_a")
        self.assertEqual([r["id"] for r in result], ["i1", "c1", "i2"])
        self.assertEqual(result[0]["source_layer"], "global_base")
        self.assertEqual(result[1]["source_layer"], "tenant")
        self.assertEqual(result[0]["knowledge_type"], "incident")

    def test_filters_apply_only_to_columns_the_table_has(self):
        _, cursor = self.search({}, limit=5, tenant_id="client_a", severity="high", classification="cpu_saturation")
        chunk_sql = next(c[0] for c in cursor.calls if "client_knowledge_chunks" in c[0])
        incident_sql = next(c[0] for c in cursor.calls if "FROM incidents" in c[0])
        self.assertIn("classification", chunk_sql.split("WHERE")[1])
        self.assertNotIn("severity", chunk_sql.split("WHERE")[1])  # chunks have no severity column
        self.assertIn("severity", incident_sql.split("WHERE")[1])

    def test_unknown_layers_are_ignored(self):
        _, cursor = self.search({}, limit=5, tenant_id="client_a", search_order=["bogus", "global_base"])
        self.assertEqual(len(cursor.calls), 1)


class RetrievalWiringTests(unittest.TestCase):
    def test_search_order_has_no_legacy_collection(self):
        for scope in ("client_private", "global_learned", "global_base"):
            order = retrieval_engine.build_search_order("client_a", scope)
            self.assertNotIn("aiops_knowledge", order)
        self.assertEqual(retrieval_engine.build_search_order("client_a", "client_private"),
                         ["client_a_index", "global_learned_patterns_index", "global_base_index"])

    def test_vector_search_maps_layers_and_labels_results(self):
        captured = {}

        def fake_search(**kwargs):
            captured.update(kwargs)
            return [{"id": "i1", "title": "t", "content": "c", "classification": "vpn_client_failure",
                     "domain": "network", "score": 0.8, "collection_name": "global_base", "source_type": "incident"}]

        with patch.object(retrieval_engine, "vector_ping", return_value=True), \
                patch.object(retrieval_engine, "ping_model", return_value=True), \
                patch.object(retrieval_engine, "embed_text", return_value=[0.0] * 384), \
                patch.object(retrieval_engine, "pg_search_multi_scope", side_effect=fake_search):
            out = retrieval_engine.vector_search("vpn down", 3, tenant_id="client_c")
        self.assertEqual(captured["search_order"], ["tenant", "global_learned", "global_base"])
        self.assertEqual(captured["tenant_id"], "client_c")
        self.assertEqual(out[0]["retrieval_method"], "pgvector")

    def test_vector_search_degrades_to_empty_when_the_database_fails(self):
        with patch.object(retrieval_engine, "vector_ping", return_value=True), \
                patch.object(retrieval_engine, "ping_model", return_value=True), \
                patch.object(retrieval_engine, "embed_text", return_value=[0.0] * 384), \
                patch.object(retrieval_engine, "pg_search_multi_scope", side_effect=RuntimeError("db down")):
            self.assertEqual(retrieval_engine.vector_search("vpn down", 3), [])

    def test_no_search_when_vector_store_is_unreachable(self):
        with patch.object(retrieval_engine, "vector_ping", return_value=False):
            self.assertEqual(retrieval_engine.vector_search("vpn down", 3), [])


class KnowledgeWriteTests(unittest.TestCase):
    def test_layer_mapping(self):
        self.assertEqual(knowledge_manager._layer_for("client_c_index", {}), ("client_private", "client_c"))
        self.assertEqual(knowledge_manager._layer_for("global_learned_patterns_index", {}), ("global_learned", None))
        self.assertEqual(knowledge_manager._collection_for("Client C", "client_private"), "client_c_index")
        self.assertEqual(knowledge_manager._collection_for("x", "global_base"), "global_base_index")

    def test_promoted_knowledge_is_embedded_and_written_to_postgres(self):
        written = {}

        def fake_upsert(record, vector, **kw):
            written.update({"record": record, "dim": len(vector), **kw})

        with patch("embedding_store.embed_text", return_value=[0.1] * 384), \
                patch("pg_vector_store.upsert_knowledge", side_effect=fake_upsert):
            ok = knowledge_manager._best_effort_vector_upsert(
                {"id": "inc_1", "title": "T", "content": "C", "classification": "vpn_client_failure", "domain": "network"},
                "client_c_index")
        self.assertTrue(ok)
        self.assertEqual((written["tenant_id"], written["knowledge_scope"], written["dim"]), ("client_c", "client_private", 384))

    def test_failure_to_index_never_blocks_promotion(self):
        with patch("embedding_store.embed_text", side_effect=RuntimeError("model down")):
            self.assertFalse(knowledge_manager._best_effort_vector_upsert({"id": "x", "content": "c"}, "client_a_index", quiet=True))

    def test_private_knowledge_needs_a_tenant(self):
        with self.assertRaises(ValueError):
            pv.upsert_knowledge({"id": "x", "content": "c"}, [0.0] * 384, model="m", tenant_id=None,
                                knowledge_scope="client_private")


class EmbeddingStoreTests(unittest.TestCase):
    def test_default_backend_matches_the_vector_column_dimension(self):
        self.assertEqual(embedding_store.EMBEDDING_BACKEND, "sentence_transformers")
        self.assertIn("MiniLM", embedding_store.MODEL_NAME)

    def test_sentence_transformer_model_is_loaded_once(self):
        fake_model = MagicMock()
        fake_model.encode.return_value = MagicMock(__iter__=lambda s: iter([MagicMock(tolist=lambda: [0.1] * 384)]))
        created = []
        fake_module = MagicMock()
        fake_module.SentenceTransformer = lambda name: created.append(name) or fake_model
        with patch.dict("sys.modules", {"sentence_transformers": fake_module}), patch.object(embedding_store, "_ST_MODEL", None):
            embedding_store._st_embed(["a"], True)
            embedding_store._st_embed(["b"], True)
        self.assertEqual(len(created), 1)


class RepositoryHygieneTests(unittest.TestCase):
    def python_files(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        skip = {".venv", "tests", "data", "bak", "__pycache__", ".git", "node_modules"}
        return [p for p in root.rglob("*.py") if not (set(p.relative_to(root).parts) & skip)]

    def test_no_code_refers_to_the_legacy_collection_or_qdrant_client(self):
        offenders = []
        for path in self.python_files():
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "aiops_knowledge" in text or "qdrant_client" in text or "from vector_store" in text:
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_requirements_no_longer_pull_in_the_qdrant_client(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        self.assertNotIn("qdrant", (root / "requirements.txt").read_text(encoding="utf-8").lower())


@unittest.skipUnless(os.getenv("AIOPS_TEST_PGVECTOR") == "1", "set AIOPS_TEST_PGVECTOR=1 with a pgvector PostgreSQL to run")
class LivePgVectorTests(unittest.TestCase):
    """Round trip against a real pgvector database; rows use a unique tenant and are removed afterwards."""

    @classmethod
    def setUpClass(cls):
        pv.ensure_schema()
        cls.tenant = "t_" + uuid.uuid4().hex[:8]
        cls.other = "t_" + uuid.uuid4().hex[:8]

    def vec(self, i, j=0):
        v = [0.0] * pv.VECTOR_DIM
        v[i] = 1.0
        v[j] = max(v[j], 0.3) if j else v[j]
        return v

    def put(self, id_, i, tenant, scope, content="live test row"):
        pv.upsert_knowledge({"id": id_, "title": "T", "content": content, "classification": "live_test", "domain": "test"},
                            self.vec(i), model="test", tenant_id=tenant, knowledge_scope=scope)

    def tearDown(self):
        with pv._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM incidents WHERE id LIKE 'livetest_%'")

    def test_tenant_isolation_and_layering(self):
        self.put("livetest_a", 1, self.tenant, "client_private")
        self.put("livetest_b", 1, self.other, "client_private")
        self.put("livetest_base", 2, None, "global_base")
        mine = {r["id"] for r in pv.search_multi_scope(self.vec(1), limit=50, tenant_id=self.tenant)}
        self.assertIn("livetest_a", mine)
        self.assertNotIn("livetest_b", mine)       # another tenant's private knowledge never leaks
        self.assertIn("livetest_base", mine)        # the curated base is visible to everyone
        base_only = {r["id"] for r in pv.search_multi_scope(self.vec(1), limit=50, tenant_id=self.tenant,
                                                           knowledge_scope="global_base")}
        self.assertNotIn("livetest_a", base_only)

    def test_cosine_score_and_ordering(self):
        self.put("livetest_near", 3, None, "global_base")
        self.put("livetest_far", 4, None, "global_base")
        rows = [r for r in pv.search_multi_scope(self.vec(3), limit=200, tenant_id=self.tenant) if r["id"].startswith("livetest_")]
        self.assertEqual(rows[0]["id"], "livetest_near")
        self.assertAlmostEqual(rows[0]["score"], 1.0, places=4)
        self.assertLess(rows[-1]["score"], 0.5)

    def test_changing_the_text_clears_a_stale_embedding(self):
        self.put("livetest_stale", 5, None, "global_base", content="original text")
        with pv._connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE incidents SET content = 'edited text' WHERE id = 'livetest_stale'")
            cur.execute("SELECT embedding IS NULL FROM incidents WHERE id = 'livetest_stale'")
            self.assertTrue(cur.fetchone()[0])

    def test_upsert_is_idempotent(self):
        for _ in range(2):
            self.put("livetest_same", 6, None, "global_base")
        with pv._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM incidents WHERE id = 'livetest_same'")
            self.assertEqual(cur.fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
