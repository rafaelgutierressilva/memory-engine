"""Fase 6 — recuperacao hibrida: fatos na frente, arquivo bruto atras.

O que se prova: o cru so aparece quando os fatos nao cobrem, todo resgate vira
evento, e o esquecimento (delecao + decaimento) manda nesta camada tambem.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lembra import config, db, forget, retrieval


def dias_atras(n) -> str:
    return (datetime.now(timezone.utc).replace(microsecond=0)
            - timedelta(days=n)).isoformat()


class BaseHibridoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.open_db(Path(self.tmp.name) / "t.db")
        self.cfg = config.load()
        # o caso real do piloto: a troca menciona o casamento de passagem, e o
        # fato destilado guardou so o assunto principal (o presente da mae)
        self.ep = db.add_episode(
            self.conn,
            "User: I think the Lavender Dreams Skincare Set sounds great. I was "
            "just at my cousin's wedding at the Grand Ballroom last weekend, and "
            "my mom looked stunning.\nAssistant: That sounds lovely!",
            surprise_score=8.0, status="consolidated")
        self.fato = db.add_fact(
            self.conn, "The user's mom is into skincare and likes lavender.",
            confidence=0.8, episode_ids=[self.ep])

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def buscar(self, pergunta, **kw):
        return retrieval.retrieve(self.conn, pergunta, self.cfg, **kw)


class FallbackTest(BaseHibridoTest):
    def test_o_cru_responde_o_que_o_fato_perdeu(self):
        r = self.buscar("Where did I attend my cousin's wedding?")
        crus = [i for i in r.winners if i.fallback]
        self.assertTrue(crus, "sem fallback, a evidencia fica inalcancavel")
        self.assertIn("Grand Ballroom", crus[0].text)
        self.assertIn("trecho bruto", r.render())

    def test_quando_os_fatos_cobrem_o_cru_nao_entra(self):
        r = self.buscar("What is my mom into? skincare lavender")
        self.assertGreaterEqual(r.cobertura_fatos,
                                self.cfg["retrieval.fallback_coverage_min"])
        self.assertEqual(r.resgates, [], "fato cobriu: nao precisa do cru")

    def test_o_fato_ganha_do_cru_na_competicao(self):
        """Peso baixo de proposito: o destilado vem primeiro."""
        r = self.buscar("mom skincare lavender wedding Grand Ballroom")
        fatos = [i for i in r.winners if i.kind == "fato"]
        crus = [i for i in r.winners if i.fallback]
        if fatos and crus:
            self.assertGreater(fatos[0].score, crus[0].score)

    def test_episodio_no_purgatorio_nao_e_fallback(self):
        """Fallback e so o ja consolidado; o buffer compete na camada normal."""
        novo = db.add_episode(self.conn, "User: I bought a red bicycle.",
                              surprise_score=8.0)
        r = self.buscar("what bicycle did I buy?")
        item = [i for i in r.winners if i.id == novo and i.kind == "episodio"][0]
        self.assertFalse(item.fallback)


class ResgateLogadoTest(BaseHibridoTest):
    """Requisito 1: o resgate e o detector de perda da destilacao."""

    def test_todo_resgate_vira_evento_proprio(self):
        pergunta = "Where did I attend my cousin's wedding?"
        self.buscar(pergunta)

        eventos = db.list_events(self.conn, kind="resgate_episodico")
        self.assertEqual(len(eventos), 1)
        p = json.loads(eventos[0]["payload"])
        self.assertEqual(eventos[0]["episode_id"], self.ep)
        self.assertEqual(p["pergunta"], pergunta)
        self.assertIn("Grand Ballroom", p["trecho"])
        self.assertLess(p["cobertura_fatos"],
                        self.cfg["retrieval.fallback_coverage_min"])
        self.assertIn("nao cobriram", p["motivo"])

    def test_sem_resgate_nao_ha_evento(self):
        self.buscar("What is my mom into? skincare lavender")
        self.assertEqual(db.list_events(self.conn, kind="resgate_episodico"), [])

    def test_resgate_conta_uso_como_qualquer_injecao(self):
        antes = db.get_episode(self.conn, self.ep)["use_count"]
        self.buscar("Where did I attend my cousin's wedding?")
        self.assertEqual(db.get_episode(self.conn, self.ep)["use_count"], antes + 1)


class EsquecimentoMandaNoFallbackTest(BaseHibridoTest):
    """Requisito 2: delecao e decaimento continuam mandando nesta camada."""

    def _acha_o_cru(self):
        r = self.buscar("Where did I attend my cousin's wedding?")
        return [i for i in r.winners if i.fallback]

    def test_delecao_tira_o_episodio_do_fallback(self):
        self.assertTrue(self._acha_o_cru())
        forget.forget_episode(self.conn, self.ep, self.cfg)
        self.assertEqual(self._acha_o_cru(), [],
                         "episodio deletado nao pode voltar pelo arquivo bruto")
        # e o conteudo sumiu do indice junto
        achados = self.conn.execute(
            "SELECT rowid FROM fts_episodes WHERE fts_episodes MATCH ?",
            ('"ballroom"*',)).fetchall()
        self.assertEqual(achados, [])

    def test_decaimento_afunda_o_episodio_e_ele_sai_do_fallback(self):
        self.assertTrue(self._acha_o_cru())
        self.conn.execute("UPDATE episodes SET timestamp = ?, last_used_at = NULL "
                          "WHERE id = ?", (dias_atras(2000), self.ep))
        self.conn.commit()

        afundados = retrieval.decay_episodes(self.conn, self.cfg)

        self.assertEqual([a[0] for a in afundados], [self.ep])
        self.assertEqual(db.get_episode(self.conn, self.ep)["status"], "expired")
        self.assertEqual(self._acha_o_cru(), [])
        # saiu do indice quente, nao do mundo
        self.assertIsNotNone(db.get_episode(self.conn, self.ep))
        self.assertEqual([f["id"] for f in db.facts_for_episode(self.conn, self.ep)],
                         [self.fato], "a proveniencia continua intacta")
        evento = json.loads(db.list_events(self.conn, kind="episode_sunken")[0]["payload"])
        self.assertIn("fica no disco", evento["motivo"])

    def test_uso_recente_protege_o_cru_do_afundamento(self):
        self.conn.execute("UPDATE episodes SET timestamp = ? WHERE id = ?",
                          (dias_atras(2000), self.ep))
        self.conn.commit()
        db.touch_episode(self.conn, self.ep)          # resgatado hoje
        self.assertEqual(retrieval.decay_episodes(self.conn, self.cfg), [])
        self.assertTrue(self._acha_o_cru())

    def test_purga_bloqueada_enquanto_sustentar_fato(self):
        """Sair do indice quente nao e sair do mundo."""
        from lembra import sleep as sleep_mod
        self.conn.execute("UPDATE episodes SET status = 'expired', "
                          "status_changed_at = ? WHERE id = ?",
                          (dias_atras(90), self.ep))
        self.conn.commit()
        self.assertEqual(sleep_mod.purge_expired(self.conn, self.cfg), [])
        self.assertIsNotNone(db.get_episode(self.conn, self.ep))


if __name__ == "__main__":
    unittest.main()
