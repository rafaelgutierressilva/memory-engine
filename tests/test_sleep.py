"""Fase 3 — purgatorio, consolidacao, auditoria e datacao.

Os testes injetam LLMs falsas: a inteligencia real e demonstrada ao vivo, aqui
o que se prova e o encanamento — quem matura, quem expira, o que o auditor
barra, e que nenhum fato entra sem proveniencia.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memory import config, db, llm, sleep


def dias_atras(n) -> str:
    return (datetime.now(timezone.utc).replace(microsecond=0)
            - timedelta(days=n)).isoformat()


def resposta(payload, custo=0.05):
    return llm.LLMResult(ok=True, called=True, data=payload, cost_usd=custo,
                         duration_ms=4000, model="default",
                         text=json.dumps(payload, ensure_ascii=False))


def destilador(fatos, custo=0.05):
    def _fn(episodes, cfg=None, facts=None):
        _fn.chamadas += 1
        _fn.episodios = [e["id"] for e in episodes]
        _fn.fatos_vistos = [f["id"] for f in (facts or [])]
        return resposta({"fatos": fatos}, custo)
    _fn.chamadas, _fn.episodios, _fn.fatos_vistos = 0, [], []
    return _fn


def auditor(nao_sustentadas=(), custo=0.04):
    def _fn(episodes, claims, cfg=None):
        _fn.chamadas += 1
        _fn.claims = claims
        return resposta({"nao_sustentadas": list(nao_sustentadas)}, custo)
    _fn.chamadas, _fn.claims = 0, []
    return _fn


def desempatador(substitui, motivo="mesma dimensao, valor mudou"):
    def _fn(prompt):
        _fn.chamadas += 1
        return resposta({"substitui": substitui, "motivo": motivo}, custo=0.01)
    _fn.chamadas = 0
    return _fn


class BaseSleepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.open_db(Path(self.tmp.name) / "t.db")
        self.cfg = config.load()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def ep(self, texto, *, dias=0, surpresa=7.0, usos=0, status="buffered"):
        eid = db.add_episode(self.conn, texto, surprise_score=surpresa,
                             status=status, timestamp=dias_atras(dias))
        for _ in range(usos):
            db.touch_episode(self.conn, eid)
        return eid


class MaturacaoTest(BaseSleepTest):
    """Quem sai do purgatorio — e quem morre nele."""

    def test_episodio_usado_matura_mesmo_sozinho(self):
        eid = self.ep("Rafael: eu corro de manha antes do trabalho.", usos=1)
        grupos, expirar, motivos = sleep.select_mature(self.conn, self.cfg)
        self.assertEqual([[e["id"] for e in g] for g in grupos], [[eid]])
        self.assertIn("usado 1x", motivos[eid])
        self.assertEqual(expirar, [])

    def test_dois_episodios_do_mesmo_assunto_maturam_juntos(self):
        a = self.ep("Rafael: quero os relatorios de campanha sempre em PDF.")
        b = self.ep("Rafael: mandei os relatorios de campanha em PDF de novo.")
        grupos, _, motivos = sleep.select_mature(self.conn, self.cfg)
        self.assertEqual(len(grupos), 1, "deviam formar um cluster so")
        self.assertEqual({e["id"] for e in grupos[0]}, {a, b})
        self.assertIn("cluster", motivos[a])

    def test_episodio_recente_isolado_nao_matura_nem_expira(self):
        self.ep("Rafael: assunto solitario sobre bicicleta ergometrica.",
                dias=1, surpresa=7.0)
        grupos, expirar, motivos = sleep.select_mature(self.conn, self.cfg)
        self.assertEqual(grupos, [])
        self.assertEqual(expirar, [], "ainda dentro do TTL: espera no purgatorio")
        self.assertEqual(motivos, {})

    def test_velho_e_muito_surpreendente_matura_sozinho(self):
        eid = self.ep("Rafael: mudei de cidade, agora moro em Floripa.",
                      dias=10, surpresa=9.0)
        _, _, motivos = sleep.select_mature(self.conn, self.cfg)
        self.assertIn("muito surpreendente", motivos[eid])

    def test_velho_e_sem_graca_expira(self):
        eid = self.ep("Rafael: obrigado pelo retorno de ontem.", dias=10, surpresa=2.0)
        grupos, expirar, _ = sleep.select_mature(self.conn, self.cfg)
        self.assertEqual(grupos, [])
        self.assertEqual([e["id"] for e in expirar], [eid])

    def test_dry_run_nao_escreve_nada(self):
        eid = self.ep("Rafael: quero relatorio em PDF.", usos=1)
        rep = sleep.run_sleep(self.conn, self.cfg, dry_run=True)
        self.assertEqual(len(rep.matured), 1)
        self.assertEqual(db.get_episode(self.conn, eid)["status"], "buffered")
        self.assertEqual(db.list_facts(self.conn), [])
        self.assertEqual(db.list_events(self.conn), [])


class ConsolidacaoTest(BaseSleepTest):
    def setUp(self):
        super().setUp()
        self.eps = [
            self.ep("Rafael: a partir de agora quero os relatorios das campanhas "
                    "sempre em PDF.", dias=6),
            self.ep("Rafael: mandou os relatorios das campanhas em PDF? "
                    "Assistente: mandei, em PDF.", dias=5),
            self.ep("Rafael: os relatorios das campanhas em planilha me atrapalham, "
                    "PDF e melhor.", dias=4),
            self.ep("Rafael: confirma que os relatorios das campanhas saem em PDF "
                    "toda sexta.", dias=3),
        ]

    def test_grupo_maduro_vira_fato_com_proveniencia(self):
        d = destilador([{"afirmacao": "Rafael quer os relatorios das campanhas em PDF.",
                         "episode_ids": self.eps, "valid_from": dias_atras(6)}])
        a = auditor()
        rep = sleep.run_sleep(self.conn, self.cfg, distill_fn=d, audit_fn=a,
                              supersede_fn=desempatador(False))

        self.assertEqual(d.chamadas, 1)
        self.assertEqual(a.chamadas, 1, "o auditor sempre roda antes de gravar")
        self.assertEqual(len(rep.facts_created), 1)

        fid = rep.facts_created[0]
        origem = [e["id"] for e in db.episodes_for_fact(self.conn, fid)]
        self.assertEqual(sorted(origem), sorted(self.eps))
        self.assertEqual(db.orphan_facts(self.conn), [])
        for eid in self.eps:
            self.assertEqual(db.get_episode(self.conn, eid)["status"], "consolidated")
        self.assertAlmostEqual(rep.cost_usd, 0.05 + 0.04)

    def test_auditor_pega_a_alucinacao_e_ela_nao_e_gravada(self):
        """O fixture do envenenamento: o destilador inventa um fato a mais."""
        d = destilador([
            {"afirmacao": "Rafael quer os relatorios das campanhas em PDF.",
             "episode_ids": self.eps},
            {"afirmacao": "Rafael cobra R$ 5.000 por mes de cada cliente.",
             "episode_ids": self.eps},          # nada nos episodios diz isso
        ])
        a = auditor([{"indice": 1,
                      "motivo": "nenhum episodio menciona valor de contrato"}])
        rep = sleep.run_sleep(self.conn, self.cfg, distill_fn=d, audit_fn=a,
                              supersede_fn=desempatador(False))

        self.assertEqual(len(rep.facts_created), 1)
        self.assertEqual(len(rep.facts_rejected), 1)
        afirmacao, motivo = rep.facts_rejected[0]
        self.assertIn("5.000", afirmacao)
        self.assertIn("valor de contrato", motivo)

        gravados = [f["statement"] for f in db.list_facts(self.conn)]
        self.assertEqual(len(gravados), 1)
        self.assertNotIn("5.000", gravados[0])

        evento = json.loads(db.list_events(self.conn, kind="fact_rejected")[0]["payload"])
        self.assertIn("5.000", evento["afirmacao"])

    def test_auditor_indisponivel_nao_grava_nada(self):
        """Fato sem verificacao nao entra. Falha fecha a porta, nao abre."""
        d = destilador([{"afirmacao": "Rafael quer PDF.", "episode_ids": self.eps}])

        def auditor_quebrado(episodes, claims, cfg=None):
            return llm.LLMResult(ok=False, called=True, error="timeout")

        rep = sleep.run_sleep(self.conn, self.cfg, distill_fn=d,
                              audit_fn=auditor_quebrado)
        self.assertEqual(rep.facts_created, [])
        self.assertEqual(db.list_facts(self.conn), [])
        self.assertTrue(db.list_events(self.conn, kind="audit_failed"))

    def test_fato_que_cita_episodio_de_fora_do_grupo_e_descartado(self):
        d = destilador([{"afirmacao": "Rafael mora em Floripa.",
                         "episode_ids": [9999]}])
        rep = sleep.run_sleep(self.conn, self.cfg, distill_fn=d, audit_fn=auditor())
        self.assertEqual(rep.facts_created, [])
        self.assertIn("proveniencia", rep.facts_rejected[0][1])
        self.assertEqual(db.orphan_facts(self.conn), [])

    def test_data_impossivel_do_modelo_e_corrigida(self):
        d = destilador([{"afirmacao": "Rafael quer PDF.", "episode_ids": self.eps,
                         "valid_from": "1999-01-01T00:00:00+00:00"}])
        rep = sleep.run_sleep(self.conn, self.cfg, distill_fn=d, audit_fn=auditor(),
                              supersede_fn=desempatador(False))
        fato = db.get_fact(self.conn, rep.facts_created[0])
        self.assertNotIn("1999", fato["valid_from"])
        evento = json.loads(db.list_events(self.conn, kind="fact_created")[0]["payload"])
        self.assertIs(evento["data_corrigida"], True)

    def test_maturou_e_nao_rendeu_fato_expira(self):
        """Nao paga consolidacao duas vezes pelo mesmo episodio inutil."""
        d = destilador([])
        sleep.run_sleep(self.conn, self.cfg, distill_fn=d, audit_fn=auditor())
        for eid in self.eps:
            self.assertEqual(db.get_episode(self.conn, eid)["status"], "expired")

    def test_sem_llm_a_consolidacao_e_pulada_com_aviso(self):
        cfg = config.Config(json.loads(json.dumps(self.cfg.as_dict())))
        cfg._data["llm"]["enabled"] = False
        rep = sleep.run_sleep(self.conn, cfg)
        self.assertEqual(rep.facts_created, [])
        self.assertTrue(rep.skipped)
        self.assertTrue(db.list_events(self.conn, kind="consolidation_skipped"))
        for eid in self.eps:
            self.assertEqual(db.get_episode(self.conn, eid)["status"], "buffered",
                             "sem consolidacao, o episodio continua esperando")


class TemporalTest(BaseSleepTest):
    def test_fato_novo_encerra_a_vigencia_do_antigo(self):
        antigo_ep = self.ep("Rafael: quero os relatorios sempre em PDF.",
                            dias=30, status="consolidated")
        antigo = db.add_fact(self.conn, "Rafael quer os relatorios sempre em PDF.",
                             valid_from=dias_atras(30), episode_ids=[antigo_ep])
        novos = [self.ep("Rafael: mudei de ideia, quero os relatorios em xlsx agora.",
                         dias=2),
                 self.ep("Rafael: os relatorios em xlsx ficaram melhores mesmo.",
                         dias=1)]

        d = destilador([{"afirmacao": "Rafael quer os relatorios em xlsx.",
                         "episode_ids": novos, "valid_from": dias_atras(2)}])
        des = desempatador(True, "mesma preferencia de formato, valor trocou")
        rep = sleep.run_sleep(self.conn, self.cfg, distill_fn=d, audit_fn=auditor(),
                              supersede_fn=des)

        self.assertEqual(des.chamadas, 1)
        self.assertEqual(len(rep.superseded), 1)
        novo = rep.facts_created[0]

        velho_row, novo_row = db.get_fact(self.conn, antigo), db.get_fact(self.conn, novo)
        self.assertIsNotNone(velho_row["valid_to"], "o antigo precisa ser encerrado")
        self.assertEqual(velho_row["superseded_by"], novo)
        self.assertEqual(novo_row["supersedes"], antigo)
        self.assertIsNone(novo_row["valid_to"])
        # a historia continua consultavel, com suas datas
        self.assertIn("PDF", velho_row["statement"])
        self.assertEqual(len(db.list_facts(self.conn, current_only=True)), 1)
        self.assertEqual(len(db.list_facts(self.conn)), 2)

    def test_fatos_compativeis_convivem(self):
        ep_a = self.ep("Rafael: quero os relatorios em PDF.", dias=30,
                       status="consolidated")
        antigo = db.add_fact(self.conn, "Rafael quer os relatorios em PDF.",
                             episode_ids=[ep_a])
        novos = [self.ep("Rafael: manda os relatorios toda sexta de manha.", dias=2),
                 self.ep("Rafael: sexta de manha e o melhor dia pros relatorios.",
                         dias=1)]
        d = destilador([{"afirmacao": "Rafael quer os relatorios toda sexta de manha.",
                         "episode_ids": novos}])
        rep = sleep.run_sleep(self.conn, self.cfg, distill_fn=d, audit_fn=auditor(),
                              supersede_fn=desempatador(False, "assuntos diferentes"))
        self.assertEqual(rep.superseded, [])
        self.assertIsNone(db.get_fact(self.conn, antigo)["valid_to"])
        self.assertEqual(len(db.list_facts(self.conn, current_only=True)), 2)


class PurgaTest(BaseSleepTest):
    def test_expirado_velho_sai_do_disco_com_registro(self):
        eid = self.ep("Rafael: bom dia.", dias=60, surpresa=1.0, status="expired")
        self.conn.execute("UPDATE episodes SET status_changed_at = ? WHERE id = ?",
                          (dias_atras(45), eid))
        self.conn.commit()
        purgados = sleep.purge_expired(self.conn, self.cfg)
        self.assertEqual([p[0] for p in purgados], [eid])
        self.assertIsNone(db.get_episode(self.conn, eid))
        self.assertTrue(db.list_events(self.conn, kind="episode_purged"))

    def test_episodio_que_sustenta_fato_nao_e_purgado(self):
        eid = self.ep("Rafael: quero PDF.", dias=60, status="expired")
        self.conn.execute("UPDATE episodes SET status_changed_at = ? WHERE id = ?",
                          (dias_atras(45), eid))
        self.conn.commit()
        db.add_fact(self.conn, "Rafael quer PDF.", episode_ids=[eid])

        self.assertEqual(sleep.purge_expired(self.conn, self.cfg), [])
        self.assertIsNotNone(db.get_episode(self.conn, eid))
        evento = json.loads(db.list_events(self.conn, kind="purge_blocked")[0]["payload"])
        self.assertIn("sustenta fato", evento["motivo"])



class DestinoTest(BaseSleepTest):
    """Os tres destinos de um desvio: substitui, convive, ou nem incomoda.

    O chat.py produz exatamente esse padrao de troca, entao a distincao precisa
    existir antes da recuperacao depender dela.
    """

    def setUp(self):
        super().setUp()
        self.ep_regra = self.ep("Rafael: quero os relatorios das campanhas sempre "
                                "em PDF.", dias=30, status="consolidated")
        self.regra = db.add_fact(
            self.conn, "Rafael quer os relatorios das campanhas sempre em PDF.",
            valid_from=dias_atras(30), confidence=0.85, episode_ids=[self.ep_regra])

    def _desvio(self, *textos):
        return [self.ep(t, dias=2 - i) for i, t in enumerate(textos)]

    # -------------------------------------------- (a) excecao isolada

    def test_excecao_isolada_nao_vira_fato_e_nao_encerra_o_antigo(self):
        eps = self._desvio("Rafael: dessa vez manda o relatorio da campanha em "
                           "xlsx, e so pra reuniao de amanha.")
        d = destilador([{"afirmacao": "Rafael pediu o relatorio em xlsx nesta ocasiao.",
                         "episode_ids": eps, "destino": "excecao_isolada",
                         "fato_alvo": self.regra}])
        des = desempatador(True)   # se for consultado, o teste falha no assert

        rep = sleep.run_sleep(self.conn, self.cfg, force=True, distill_fn=d,
                              audit_fn=auditor(), supersede_fn=des)

        self.assertEqual(rep.facts_created, [], "excecao isolada nao vira fato")
        self.assertEqual(len(rep.exceptions_dropped), 1)
        self.assertEqual(des.chamadas, 0,
                         "nem chega a perguntar sobre substituicao")

        regra = db.get_fact(self.conn, self.regra)
        self.assertIsNone(regra["valid_to"], "o fato vigente segue vigente")
        self.assertIsNone(regra["superseded_by"])
        self.assertEqual(len(db.list_facts(self.conn, current_only=True)), 1)

        linha = db.list_events(self.conn, kind="fact_destiny")[0]
        evento = json.loads(linha["payload"])
        self.assertEqual(evento["destino"], "excecao_isolada")
        self.assertIs(evento["gravado"], False)
        self.assertEqual(evento["afirmacao"],
                         "Rafael pediu o relatorio em xlsx nesta ocasiao.")
        self.assertEqual(linha["fact_id"], self.regra,
                         "o log aponta qual fato vigente foi poupado")

    def test_o_destilador_enxerga_os_fatos_vigentes(self):
        """Sem isso ele destila no vacuo e nao tem como reconhecer um desvio."""
        eps = self._desvio("Rafael: dessa vez em xlsx.")
        d = destilador([])
        sleep.run_sleep(self.conn, self.cfg, force=True, distill_fn=d,
                        audit_fn=auditor())
        self.assertIn(self.regra, d.fatos_vistos)

    # ---------------------------------------- (b) excecao repetida

    def test_excecao_repetida_vira_regra_condicional_que_coexiste(self):
        eps = self._desvio(
            "Rafael: quando o relatorio for pro investidor, manda em xlsx.",
            "Rafael: de novo, o relatorio do investidor precisa vir em xlsx.",
            "Rafael: lembra: investidor sempre xlsx, o resto segue igual.")
        d = destilador([{"afirmacao": "Quando o relatorio e para o investidor, "
                                      "Rafael quer em xlsx.",
                         "episode_ids": eps, "destino": "condicional",
                         "fato_alvo": self.regra}])
        des = desempatador(True)   # nao deve ser consultado no caminho condicional

        rep = sleep.run_sleep(self.conn, self.cfg, force=True, distill_fn=d,
                              audit_fn=auditor(), supersede_fn=des)

        self.assertEqual(len(rep.facts_created), 1)
        self.assertEqual(rep.superseded, [], "regra condicional nao substitui nada")
        self.assertEqual(des.chamadas, 0)

        regra = db.get_fact(self.conn, self.regra)
        self.assertIsNone(regra["valid_to"], "o fato original continua vigente")
        vigentes = db.list_facts(self.conn, current_only=True)
        self.assertEqual(len(vigentes), 2, "os dois convivem")

        afirmacao, destino, alvo, _ = rep.destinies[0]
        self.assertEqual(destino, "condicional")
        self.assertEqual(alvo, self.regra)

    # ------------------------------------------- (c) substituicao

    def test_substituicao_declarada_ainda_passa_pelo_desempate(self):
        eps = self._desvio("Rafael: mudei de ideia, quero tudo em xlsx a partir "
                           "de agora.")
        d = destilador([{"afirmacao": "Rafael quer os relatorios das campanhas em xlsx.",
                         "episode_ids": eps, "destino": "substitui",
                         "fato_alvo": self.regra}])
        des = desempatador(True, "mesma dimensao, valor mudou")

        rep = sleep.run_sleep(self.conn, self.cfg, force=True, distill_fn=d,
                              audit_fn=auditor(), supersede_fn=des)

        self.assertEqual(des.chamadas, 1, "encerrar vigencia exige segundo parecer")
        self.assertEqual(len(rep.superseded), 1)
        self.assertIsNotNone(db.get_fact(self.conn, self.regra)["valid_to"])

    def test_desempate_discordante_rebaixa_substituicao_para_coexistencia(self):
        """Se o segundo parecer discorda, nada e encerrado."""
        eps = self._desvio("Rafael: pro investidor eu quero xlsx.")
        d = destilador([{"afirmacao": "Rafael quer os relatorios em xlsx para o investidor.",
                         "episode_ids": eps, "destino": "substitui",
                         "fato_alvo": self.regra}])
        des = desempatador(False, "condicao diferente, podem conviver")

        rep = sleep.run_sleep(self.conn, self.cfg, force=True, distill_fn=d,
                              audit_fn=auditor(), supersede_fn=des)

        self.assertEqual(rep.superseded, [])
        self.assertIsNone(db.get_fact(self.conn, self.regra)["valid_to"])
        _, destino, _, como = rep.destinies[0]
        self.assertEqual(destino, "condicional")
        self.assertIn("rebaixado", como)
        self.assertEqual(len(db.list_facts(self.conn, current_only=True)), 2)


class IdiomaDoFatoTest(unittest.TestCase):
    """O fato tem que nascer no idioma da conversa.

    Descoberto no piloto do LongMemEval: a memoria guardou "O usuario viajou ao
    Havai com a familia" para uma conversa em ingles, e a pergunta em ingles
    ("Where did I go with my family?") nao casou com uma palavra sequer. A
    memoria estava certa e invisivel.
    """

    def test_o_prompt_manda_seguir_o_idioma_dos_episodios(self):
        prompt = sleep.CONSOLIDATION_PROMPT.format(facts="x", episodes="y")
        self.assertIn("NO MESMO IDIOMA dos episodios", prompt)
        self.assertIn("fica invisivel", prompt,
                      "o prompt precisa explicar POR QUE, senao o modelo relaxa")
        self.assertNotIn("uma frase curta em portugues", prompt)


class EscopoDaMemoriaTest(unittest.TestCase):
    """Memoria semantica E episodica.

    Descoberto no piloto do LongMemEval: o prompt mandava extrair so "fatos
    duraveis" e ignorar "o circunstancial". Resultado — a troca "estive no
    casamento do meu primo no Grand Ballroom" era carimbada, nao rendia fato
    nenhum, e o episodio expirava (saindo tambem do alcance da busca). A
    pergunta "onde foi o casamento do meu primo?" ficava sem resposta com a
    evidencia em maos.
    """

    def test_o_prompt_pede_as_duas_familias_de_fato(self):
        prompt = sleep.CONSOLIDATION_PROMPT.format(facts="x", episodes="y")
        self.assertIn("SEMANTICO", prompt)
        self.assertIn("EPISODICO", prompt)
        self.assertIn("eventos, lugares, datas", prompt)

    def test_o_prompt_nao_manda_mais_descartar_o_circunstancial(self):
        prompt = sleep.CONSOLIDATION_PROMPT.format(facts="x", episodes="y")
        self.assertNotIn("Ignore o circunstancial puro", prompt)
        self.assertIn("Um evento concreto NAO e circunstancial", prompt)

if __name__ == "__main__":
    unittest.main()
