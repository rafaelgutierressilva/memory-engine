"""Fase 4 — recuperacao, vigencia e decaimento."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lembra import config, db, retrieval


def dias_atras(n) -> str:
    return (datetime.now(timezone.utc).replace(microsecond=0)
            - timedelta(days=n)).isoformat()


class BaseRetrievalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.open_db(Path(self.tmp.name) / "t.db")
        self.cfg = config.load()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def fato(self, texto, **kw):
        # o episodio de origem entra como 'consolidated' para nao concorrer na
        # busca junto com o fato que ele sustenta (senao os testes medem ruido)
        ep = db.add_episode(self.conn, "registro interno de origem",
                            status="consolidated")
        return db.add_fact(self.conn, texto, episode_ids=[ep], **kw)


class VigenciaTest(BaseRetrievalTest):
    """A instrucao mais importante da fase: o passado nao compete no presente."""

    def setUp(self):
        super().setUp()
        self.antigo = self.fato("Rafael quer os relatorios em PDF.",
                                valid_from=dias_atras(40), confidence=0.9)
        db.close_fact(self.conn, self.antigo, valid_to=dias_atras(5))
        self.atual = self.fato("Rafael quer os relatorios em xlsx.",
                               valid_from=dias_atras(5), confidence=0.8)

    def test_fato_encerrado_nao_compete_na_consulta_normal(self):
        r = retrieval.retrieve(self.conn, "manda o relatorio de hoje", self.cfg)
        ids = [i.id for i in r.winners]
        self.assertIn(self.atual, ids)
        self.assertNotIn(self.antigo, ids, "fato vencido nao pode ser injetado")
        self.assertFalse(r.historical)

    def test_consulta_temporal_traz_o_fato_encerrado_de_volta(self):
        r = retrieval.retrieve(self.conn, "qual formato de relatorio eu preferia antes?",
                               self.cfg)
        self.assertTrue(r.historical, "a pergunta olha para tras")
        ids = [i.id for i in r.winners]
        self.assertIn(self.antigo, ids)
        antigo = [i for i in r.winners if i.id == self.antigo][0]
        self.assertIsNotNone(antigo.valid_to)
        self.assertIn("vigorou ate", r.render())

    def test_flag_explicita_vence_a_heuristica(self):
        r = retrieval.retrieve(self.conn, "manda o relatorio", self.cfg, historical=True)
        self.assertIn(self.antigo, [i.id for i in r.winners])

    def test_fato_afundado_nunca_compete(self):
        afundado = self.fato("Rafael gosta de relatorio com grafico de barras.")
        db.set_fact_status(self.conn, afundado, "sunken")
        r = retrieval.retrieve(self.conn, "relatorio com grafico", self.cfg)
        self.assertNotIn(afundado, [i.id for i in r.winners])
        # ...mas continua no disco e inspecionavel
        self.assertIsNotNone(db.get_fact(self.conn, afundado))


class CompeticaoTest(BaseRetrievalTest):
    def test_nucleo_sempre_entra_e_tem_teto(self):
        for i in range(7):
            self.fato(f"Fato de nucleo numero {i} sobre assunto {i}.", is_core=True)
        r = retrieval.retrieve(self.conn, "qualquer coisa", self.cfg)
        self.assertEqual(len(r.core), self.cfg["retrieval.core_max_lines"])
        self.assertTrue(all(i.core for i in r.core))

    def test_top_k_limita_os_vencedores(self):
        for i in range(10):
            self.fato(f"Rafael trabalha com campanha de trafego numero {i}.")
        r = retrieval.retrieve(self.conn, "campanha de trafego", self.cfg)
        self.assertEqual(len(r.winners), self.cfg["retrieval.top_k"])
        self.assertGreater(r.considered, len(r.winners))

    def test_score_combina_relevancia_recencia_e_confianca(self):
        recente = self.fato("Rafael prefere campanha de conversao.",
                            valid_from=dias_atras(1), confidence=0.9)
        antigo = self.fato("Rafael prefere campanha de alcance.",
                           valid_from=dias_atras(300), confidence=0.3)
        r = retrieval.retrieve(self.conn, "campanha", self.cfg)
        vencedores = {i.id: i for i in r.winners}
        self.assertGreater(vencedores[recente].score, vencedores[antigo].score)
        self.assertLess(vencedores[antigo].recency, 0.05, "300 dias derrubam a recencia")

    def test_episodio_no_purgatorio_compete_mas_consolidado_nao(self):
        no_buffer = db.add_episode(self.conn, "Rafael: odeio reuniao antes das 9h.",
                                   surprise_score=8.0)
        consolidado = db.add_episode(self.conn, "Rafael: odeio reuniao longa.",
                                     surprise_score=8.0, status="consolidated")
        r = retrieval.retrieve(self.conn, "reuniao", self.cfg)
        ids = [(i.kind, i.id) for i in r.winners]
        self.assertIn(("episodio", no_buffer), ids)
        self.assertNotIn(("episodio", consolidado), ids)

    def test_consulta_sem_relacao_nao_inventa_vencedor(self):
        self.fato("Rafael quer relatorio em PDF.")
        r = retrieval.retrieve(self.conn, "qual a capital da Mongolia?", self.cfg)
        self.assertEqual(r.winners, [])
        self.assertEqual(r.render(), "")


class UsoImediatoTest(BaseRetrievalTest):
    """Cada injecao alimenta o decaimento na hora — nao no fim do turno."""

    def test_injetar_incrementa_use_count_imediatamente(self):
        fid = self.fato("Rafael quer relatorio em PDF.")
        nucleo = self.fato("Rafael trabalha com trafego pago.", is_core=True)
        antes = db.get_fact(self.conn, fid)["use_count"]

        retrieval.retrieve(self.conn, "manda o relatorio", self.cfg)

        depois = db.get_fact(self.conn, fid)
        self.assertEqual(depois["use_count"], antes + 1)
        self.assertIsNotNone(depois["last_used_at"])
        self.assertEqual(db.get_fact(self.conn, nucleo)["use_count"], 1,
                         "o nucleo tambem e injetado, entao tambem conta uso")

    def test_episodio_injetado_conta_uso_e_por_isso_matura(self):
        from lembra import sleep
        eid = db.add_episode(self.conn, "Rafael: odeio reuniao antes das 9h.",
                             surprise_score=8.0)
        retrieval.retrieve(self.conn, "reuniao antes das 9h", self.cfg)
        self.assertEqual(db.get_episode(self.conn, eid)["use_count"], 1)
        _, _, motivos = sleep.select_mature(self.conn, self.cfg)
        self.assertIn("usado 1x", motivos[eid],
                      "o uso na recuperacao e o que faz o episodio maturar")

    def test_injecao_e_registrada_com_os_scores(self):
        self.fato("Rafael quer relatorio em PDF.")
        retrieval.retrieve(self.conn, "relatorio", self.cfg)
        evento = json.loads(db.list_events(self.conn, kind="inject")[0]["payload"])
        self.assertEqual(len(evento["vencedores"]), 1)
        for chave in ("tipo", "id", "score", "rel", "rec", "conf"):
            self.assertIn(chave, evento["vencedores"][0])

    def test_touch_desligado_nao_registra_uso(self):
        fid = self.fato("Rafael quer relatorio em PDF.")
        retrieval.retrieve(self.conn, "relatorio", self.cfg, touch=False)
        self.assertEqual(db.get_fact(self.conn, fid)["use_count"], 0)
        self.assertEqual(db.list_events(self.conn, kind="inject"), [])


class DecaimentoTest(BaseRetrievalTest):
    def test_fato_sem_uso_afunda_e_sai_da_busca(self):
        fid = self.fato("Rafael gostava de campanha de alcance.", confidence=0.5)
        self.conn.execute("UPDATE facts SET created_at = ?, last_used_at = NULL "
                          "WHERE id = ?", (dias_atras(400), fid))
        self.conn.commit()

        afundados = retrieval.decay_pass(self.conn, self.cfg)

        self.assertEqual([a[0] for a in afundados], [fid])
        self.assertEqual(db.get_fact(self.conn, fid)["status"], "sunken")
        r = retrieval.retrieve(self.conn, "campanha de alcance", self.cfg)
        self.assertEqual(r.winners, [])

    def test_uso_recente_protege_do_afundamento(self):
        fid = self.fato("Rafael gosta de campanha de alcance.", confidence=0.5)
        self.conn.execute("UPDATE facts SET created_at = ? WHERE id = ?",
                          (dias_atras(400), fid))
        self.conn.commit()
        db.touch_fact(self.conn, fid)          # usado hoje

        self.assertEqual(retrieval.decay_pass(self.conn, self.cfg), [])
        self.assertEqual(db.get_fact(self.conn, fid)["status"], "active")

    def test_ressurreicao_por_id_volta_e_vira_sinal_de_calibragem(self):
        fid = self.fato("Rafael gostava de campanha de alcance.")
        db.set_fact_status(self.conn, fid, "sunken")

        self.assertTrue(retrieval.resurrect(self.conn, fid))
        self.assertEqual(db.get_fact(self.conn, fid)["status"], "active")
        evento = json.loads(db.list_events(self.conn, kind="fact_resurrected")[0]["payload"])
        self.assertIn("agressivo", evento["sinal"])
        # de volta a competicao
        r = retrieval.retrieve(self.conn, "campanha de alcance", self.cfg)
        self.assertIn(fid, [i.id for i in r.winners])

    def test_ressuscitar_fato_ativo_nao_faz_nada(self):
        fid = self.fato("Rafael quer PDF.")
        self.assertFalse(retrieval.resurrect(self.conn, fid))
        self.assertEqual(db.list_events(self.conn, kind="fact_resurrected"), [])


class ConsultaTemporalTest(unittest.TestCase):
    def test_detector_de_pergunta_sobre_o_passado(self):
        for pergunta in ("o que eu preferia antes?", "isso mudou quando?",
                         "antigamente eu usava o que?", "qual era o historico disso?",
                         "me conta o que eu pedia antigamente"):
            self.assertTrue(retrieval.is_temporal_query(pergunta), pergunta)
        for pergunta in ("manda o relatorio de hoje", "qual o CPA da campanha?",
                         "agenda a reuniao"):
            self.assertFalse(retrieval.is_temporal_query(pergunta), pergunta)

    def test_mencionar_o_passado_nao_e_perguntar_sobre_ele(self):
        """Falsos positivos encontrados no primeiro chat de verdade."""
        for fala in ("Na verdade mudei de ideia sobre reunioes: agora prefiro 1 hora",
                     "marca a reuniao antes das 9h",
                     "mudou o horario da call de hoje",
                     "deixei de usar aquele relatorio, manda o novo"):
            self.assertFalse(retrieval.is_temporal_query(fala), fala)


class IndiceTest(BaseRetrievalTest):
    def test_indice_acompanha_edicao_e_remocao(self):
        fid = self.fato("Rafael quer relatorio em PDF.")
        self.assertTrue(retrieval.retrieve(self.conn, "PDF", self.cfg).winners)

        self.conn.execute("UPDATE facts SET statement = ? WHERE id = ?",
                          ("Rafael quer relatorio em xlsx.", fid))
        self.conn.commit()
        self.assertEqual(retrieval.retrieve(self.conn, "PDF", self.cfg).winners, [])
        self.assertTrue(retrieval.retrieve(self.conn, "xlsx", self.cfg).winners)

    def test_prefixo_alcanca_plural(self):
        self.fato("Rafael quer os relatorios das campanhas em PDF.")
        r = retrieval.retrieve(self.conn, "cade o relatorio da campanha?", self.cfg)
        self.assertTrue(r.winners, "'relatorio' precisa alcancar 'relatorios'")



class ConsultaFTSTest(BaseRetrievalTest):
    """A consulta precisa alcancar as duas grafias — a do usuario e a do fato."""

    def test_plural_em_oes_encontra_o_singular_e_vice_versa(self):
        plural = self.fato("Rafael prefere reunioes de no maximo 30 minutos.")
        singular = self.fato("Rafael quer que a reuniao comece pontualmente.")

        achados = lambda q: [i.id for i in retrieval.retrieve(self.conn, q, self.cfg).winners]
        self.assertIn(plural, achados("quanto tempo duram as reunioes?"))
        self.assertIn(singular, achados("quanto tempo duram as reunioes?"))
        self.assertIn(plural, achados("como deve ser a reuniao?"))
        self.assertIn(singular, achados("como deve ser a reuniao?"))

    def test_a_consulta_leva_as_duas_formas(self):
        match = retrieval.build_match("as reunioes de hoje")
        self.assertIn('"reunioes"*', match)
        self.assertIn('"reuniao"*', match)

if __name__ == "__main__":
    unittest.main()
