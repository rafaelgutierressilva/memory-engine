"""Fase 1 — testes do armazenamento e do leitor de config."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from memory import config, db


class ConfigParserTest(unittest.TestCase):
    def test_parses_nested_scalars_and_comments(self):
        data = config.parse_yaml(
            "# comentario\n"
            "llm:\n"
            "  enabled: false   # inline\n"
            "  gate_model: haiku\n"
            "  timeout_seconds: 120\n"
            "gate:\n"
            "  surprise_threshold: 6.0\n"
            "  reasons: [a, b]\n"
        )
        cfg = config.Config(data)
        self.assertIs(cfg.get("llm.enabled"), False)
        self.assertEqual(cfg.get("llm.gate_model"), "haiku")
        self.assertEqual(cfg.get("llm.timeout_seconds"), 120)
        self.assertEqual(cfg.get("gate.surprise_threshold"), 6.0)
        self.assertEqual(cfg.get("gate.reasons"), ["a", "b"])
        self.assertIsNone(cfg.get("nao.existe"))

    def test_real_config_file_loads(self):
        cfg = config.load()
        self.assertIsInstance(cfg.get("gate.surprise_threshold"), float)
        self.assertIn(cfg.get("llm.gate_model"), ("haiku", "default"))


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.open_db(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_episode_roundtrip(self):
        eid = db.add_episode(self.conn, "troca crua", surprise_score=7.0,
                             reasons=["correcao"])
        row = db.get_episode(self.conn, eid)
        self.assertEqual(row["raw_text"], "troca crua")
        self.assertEqual(row["status"], "buffered")
        self.assertEqual(row["use_count"], 0)
        db.touch_episode(self.conn, eid)
        self.assertEqual(db.get_episode(self.conn, eid)["use_count"], 1)

    def test_fact_with_provenance_both_directions(self):
        e1 = db.add_episode(self.conn, "ep um")
        e2 = db.add_episode(self.conn, "ep dois")
        fid = db.add_fact(self.conn, "afirmacao", episode_ids=[e1, e2])
        self.assertEqual([r["id"] for r in db.episodes_for_fact(self.conn, fid)], [e1, e2])
        self.assertEqual([r["id"] for r in db.facts_for_episode(self.conn, e1)], [fid])
        self.assertEqual(db.orphan_facts(self.conn), [])

    def test_temporal_update_never_overwrites(self):
        e1 = db.add_episode(self.conn, "prefere planilha")
        e2 = db.add_episode(self.conn, "agora prefere PDF")
        old = db.add_fact(self.conn, "prefere planilha", episode_ids=[e1])
        db.close_fact(self.conn, old)
        new = db.add_fact(self.conn, "prefere PDF", episode_ids=[e2], supersedes=old)
        old_row, new_row = db.get_fact(self.conn, old), db.get_fact(self.conn, new)
        self.assertIsNotNone(old_row["valid_to"])          # encerrado
        self.assertEqual(old_row["statement"], "prefere planilha")  # nao sobrescrito
        self.assertEqual(old_row["superseded_by"], new)
        self.assertEqual(new_row["supersedes"], old)
        self.assertIsNone(new_row["valid_to"])
        # o fato antigo continua consultavel
        self.assertEqual(len(db.list_facts(self.conn)), 2)
        self.assertEqual(len(db.list_facts(self.conn, current_only=True)), 1)

    def test_confidence_has_a_ceiling(self):
        e = db.add_episode(self.conn, "ep")
        fid = db.add_fact(self.conn, "f", confidence=0.94, episode_ids=[e])
        before, after = db.bump_confidence(self.conn, fid, step=0.05, ceiling=0.95)
        self.assertAlmostEqual(before, 0.94)
        self.assertAlmostEqual(after, 0.95)

    def test_episode_with_provenance_cannot_be_hard_deleted(self):
        """RESTRICT: purgar so depois da cascata de reprocessamento (Fase 5)."""
        e = db.add_episode(self.conn, "ep")
        db.add_fact(self.conn, "f", episode_ids=[e])
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM episodes WHERE id = ?", (e,))

    def test_events_are_append_only_log(self):
        e = db.add_episode(self.conn, "ep")
        db.log_event(self.conn, "gate_stamp", episode_id=e, surpresa=8, motivos=["x"])
        db.log_event(self.conn, "gate_merge", fact_id=None, delta=0.02)
        kinds = [r["kind"] for r in db.list_events(self.conn)]
        self.assertEqual(kinds, ["gate_merge", "gate_stamp"])
        self.assertEqual(db.stats(self.conn)["events_total"], 2)

    def test_invalid_status_rejected(self):
        with self.assertRaises(ValueError):
            db.add_episode(self.conn, "ep", status="inventado")


if __name__ == "__main__":
    unittest.main()
