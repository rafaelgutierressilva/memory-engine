"""Fase 5 — a cascata de delecao.

O que se prova aqui: apagar um episodio nunca deixa para tras um fato que so
ele sustentava, e nunca deixa o motor sem fato vigente sobre um assunto por
causa de uma substituicao que perdeu a evidencia.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lembra import config, db, forget, llm


def dias_atras(n) -> str:
    return (datetime.now(timezone.utc).replace(microsecond=0)
            - timedelta(days=n)).isoformat()


def resposta(payload, custo=0.05):
    return llm.LLMResult(ok=True, called=True, data=payload, cost_usd=custo,
                         duration_ms=3000, model="default")


def destilador(fatos, custo=0.05):
    def _fn(episodes, cfg=None, facts=None):
        _fn.chamadas += 1
        _fn.episodios = [e["id"] for e in episodes]
        return resposta({"fatos": fatos}, custo)
    _fn.chamadas, _fn.episodios = 0, []
    return _fn


def auditor(nao_sustentadas=()):
    def _fn(episodes, claims, cfg=None):
        _fn.chamadas += 1
        return resposta({"nao_sustentadas": list(nao_sustentadas)}, 0.04)
    _fn.chamadas = 0
    return _fn


class BaseForgetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.open_db(Path(self.tmp.name) / "t.db")
        self.cfg = config.load()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def ep(self, texto, dias=1):
        return db.add_episode(self.conn, texto, surprise_score=7.0,
                              timestamp=dias_atras(dias), status="consolidated")


class CascataBasicaTest(BaseForgetTest):
    def test_episodio_sem_fatos_apaga_sem_cascata(self):
        eid = self.ep("Rafael: bom dia.")
        rep = forget.forget_episode(self.conn, eid, self.cfg,
                                    distill_fn=destilador([]))
        self.assertEqual(rep.facts_retired, [])
        self.assertEqual(rep.facts_reprocessed, [])
        self.assertEqual(db.get_episode(self.conn, eid)["status"], "deleted")

    def test_raw_text_e_fisicamente_sobrescrito(self):
        eid = self.ep("Rafael: meu CPF e 123.456.789-00.")
        forget.forget_episode(self.conn, eid, self.cfg, distill_fn=destilador([]))
        linha = db.get_episode(self.conn, eid)
        self.assertEqual(linha["raw_text"], forget.TOMBSTONE)
        self.assertNotIn("123.456", linha["raw_text"])
        # e some tambem do indice de busca
        achados = self.conn.execute(
            "SELECT rowid FROM fts_episodes WHERE fts_episodes MATCH ?",
            ('"123"*',)).fetchall()
        self.assertEqual(achados, [])

    def test_fato_sem_proveniencia_restante_e_aposentado(self):
        eid = self.ep("Rafael: eu moro em Curitiba.")
        fid = db.add_fact(self.conn, "Rafael mora em Curitiba.", episode_ids=[eid])

        rep = forget.forget_episode(self.conn, eid, self.cfg)

        self.assertEqual([f[0] for f in rep.facts_retired], [fid])
        fato = db.get_fact(self.conn, fid)
        self.assertEqual(fato["status"], "retired")
        self.assertIsNotNone(fato["valid_to"], "aposentar tambem encerra a vigencia")
        self.assertEqual(db.orphan_facts(self.conn), [])

    def test_delecao_inexistente_ou_repetida_falha_limpo(self):
        eid = self.ep("Rafael: oi.")
        self.assertIn("nao existe",
                      forget.forget_episode(self.conn, 9999, self.cfg).error)
        forget.forget_episode(self.conn, eid, self.cfg, distill_fn=destilador([]))
        self.assertIn("ja estava deletado",
                      forget.forget_episode(self.conn, eid, self.cfg).error)


class ReprocessamentoTest(BaseForgetTest):
    """Caso 2 — sobrou evidencia, entao o fato e re-destilado, nao mantido."""

    def test_regra_condicional_perde_um_episodio_e_e_reprocessada(self):
        eps = [self.ep("Rafael: quando o relatorio for pro investidor, manda em PDF.", 5),
               self.ep("Rafael: de novo, relatorio do investidor em PDF.", 4),
               self.ep("Rafael: investidor sempre PDF, o resto segue xlsx.", 3)]
        condicional = db.add_fact(
            self.conn, "Quando o relatorio e para o investidor, Rafael quer em PDF.",
            confidence=0.80, episode_ids=eps)

        d = destilador([{"afirmacao": "Quando o relatorio e para o investidor, "
                                      "Rafael quer em PDF.",
                         "episode_ids": eps[1:]}])
        rep = forget.forget_episode(self.conn, eps[0], self.cfg,
                                    distill_fn=d, audit_fn=auditor())

        self.assertEqual(d.chamadas, 1)
        self.assertEqual(sorted(d.episodios), sorted(eps[1:]),
                         "a re-destilacao so pode ver os sobreviventes")
        self.assertEqual(len(rep.facts_reprocessed), 1)

        antigo, novo, _ = rep.facts_reprocessed[0]
        self.assertEqual(antigo, condicional)
        self.assertEqual(db.get_fact(self.conn, condicional)["status"], "retired")

        novo_row = db.get_fact(self.conn, novo)
        self.assertEqual(novo_row["status"], "active")
        self.assertIsNone(novo_row["valid_to"], "a regra continua vigente")
        origem = [e["id"] for e in db.episodes_for_fact(self.conn, novo)]
        self.assertEqual(sorted(origem), sorted(eps[1:]))
        self.assertNotIn(eps[0], origem, "o episodio apagado nao sustenta mais nada")

    def test_reprocessamento_vazio_aposenta(self):
        eps = [self.ep("Rafael: quero PDF.", 3), self.ep("Rafael: obrigado.", 2)]
        fid = db.add_fact(self.conn, "Rafael quer PDF.", episode_ids=eps)
        rep = forget.forget_episode(self.conn, eps[0], self.cfg,
                                    distill_fn=destilador([]), audit_fn=auditor())
        self.assertEqual([f[0] for f in rep.facts_retired], [fid])
        self.assertIn("nao extraiu nada", rep.facts_retired[0][1])

    def test_auditor_derruba_o_reprocessado_e_o_fato_e_aposentado(self):
        eps = [self.ep("Rafael: quero PDF.", 3), self.ep("Rafael: ok.", 2)]
        fid = db.add_fact(self.conn, "Rafael quer PDF.", episode_ids=eps)
        d = destilador([{"afirmacao": "Rafael quer PDF sempre.", "episode_ids": [eps[1]]}])
        rep = forget.forget_episode(self.conn, eps[0], self.cfg, distill_fn=d,
                                    audit_fn=auditor([{"indice": 0, "motivo": "sem base"}]))
        self.assertEqual([f[0] for f in rep.facts_retired], [fid])
        self.assertIn("auditor", rep.facts_retired[0][1])

    def test_sem_llm_aposenta_por_precaucao(self):
        eps = [self.ep("Rafael: quero PDF.", 3), self.ep("Rafael: confirmo PDF.", 2)]
        fid = db.add_fact(self.conn, "Rafael quer PDF.", episode_ids=eps)
        cfg = config.Config(json.loads(json.dumps(self.cfg.as_dict())))
        cfg._data["llm"]["enabled"] = False
        rep = forget.forget_episode(self.conn, eps[0], cfg)
        self.assertEqual([f[0] for f in rep.facts_retired], [fid])
        self.assertIn("precaucao", rep.facts_retired[0][1])


class SubstituicaoRevertidaTest(BaseForgetTest):
    """Caso 4 — o mais sutil: apagar a evidencia da MUDANCA.

    Cenario: o fato A ("PDF") vigorou ate ser substituido pelo fato B ("xlsx").
    Apagar o unico episodio que sustentava B aposenta B — e ai o `valid_to` de A
    perde a razao de existir, porque foi B que o fechou.
    """

    def setUp(self):
        super().setUp()
        self.ep_antigo = self.ep("Rafael: quero os relatorios em PDF.", 30)
        self.fato_a = db.add_fact(self.conn, "Rafael quer os relatorios em PDF.",
                                  valid_from=dias_atras(30), confidence=0.85,
                                  episode_ids=[self.ep_antigo])
        self.ep_novo = self.ep("Rafael: mudei de ideia, quero em xlsx agora.", 2)
        db.close_fact(self.conn, self.fato_a, valid_to=dias_atras(2))
        self.fato_b = db.add_fact(self.conn, "Rafael quer os relatorios em xlsx.",
                                  valid_from=dias_atras(2), confidence=0.75,
                                  episode_ids=[self.ep_novo], supersedes=self.fato_a)

    def test_aposentar_o_substituto_reabre_a_vigencia_do_substituido(self):
        fechado_em = db.get_fact(self.conn, self.fato_a)["valid_to"]
        self.assertIsNotNone(fechado_em)

        rep = forget.forget_episode(self.conn, self.ep_novo, self.cfg)

        self.assertEqual([f[0] for f in rep.facts_retired], [self.fato_b])
        self.assertEqual([r[0] for r in rep.reopened], [self.fato_a])

        a = db.get_fact(self.conn, self.fato_a)
        self.assertIsNone(a["valid_to"], "a vigencia voltou a ficar aberta")
        self.assertIsNone(a["superseded_by"], "nao ha mais substituto")
        self.assertEqual(a["status"], "active")
        self.assertEqual(a["statement"], "Rafael quer os relatorios em PDF.")

        self.assertEqual(db.get_fact(self.conn, self.fato_b)["status"], "retired")

    def test_o_valid_to_anterior_fica_registrado_no_log(self):
        fechado_em = db.get_fact(self.conn, self.fato_a)["valid_to"]
        forget.forget_episode(self.conn, self.ep_novo, self.cfg)
        evento = json.loads(db.list_events(
            self.conn, kind="substituicao_revertida")[0]["payload"])
        self.assertEqual(evento["valid_to_anterior"], fechado_em)
        self.assertEqual(evento["substituto_aposentado"], self.fato_b)

    def test_o_assunto_volta_a_ter_fato_vigente(self):
        """A razao de ser da regra: nao deixar o assunto sem nenhum fato vivo."""
        forget.forget_episode(self.conn, self.ep_novo, self.cfg)
        vigentes = [f["id"] for f in db.list_facts(self.conn, status="active",
                                                   current_only=True)]
        self.assertEqual(vigentes, [self.fato_a])

    def test_substituto_com_evidencia_restante_nao_reabre_nada(self):
        """Se B sobrevive ao reprocessamento, A continua encerrado."""
        outro = self.ep("Rafael: xlsx ficou melhor mesmo.", 1)
        db.link_provenance(self.conn, self.fato_b, outro)

        d = destilador([{"afirmacao": "Rafael quer os relatorios em xlsx.",
                         "episode_ids": [outro]}])
        rep = forget.forget_episode(self.conn, self.ep_novo, self.cfg,
                                    distill_fn=d, audit_fn=auditor())

        self.assertEqual(rep.reopened, [], "A nao pode reabrir: a mudanca continua provada")
        self.assertIsNotNone(db.get_fact(self.conn, self.fato_a)["valid_to"])

        _, novo_b, _ = rep.facts_reprocessed[0]
        self.assertEqual(db.get_fact(self.conn, novo_b)["supersedes"], self.fato_a,
                         "o novo B herda a cadeia temporal do B antigo")
        self.assertEqual(db.get_fact(self.conn, self.fato_a)["superseded_by"], novo_b,
                         "e o A passa a apontar para o B novo, nao para o aposentado")


if __name__ == "__main__":
    unittest.main()
