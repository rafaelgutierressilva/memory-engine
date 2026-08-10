"""Fase 2 — o portao de surpresa.

As tres fixtures do plano, mais os invariantes que o Rafael pediu:
toda decisao registra COMO foi resolvida (de graca ou pagando haiku).
"""

import json
import tempfile
import unittest
from pathlib import Path

from memory import config, db, gate, llm


def fake_judge(surpresa, motivos=("motivo do juiz",), violada="nenhuma",
               custo=0.018, com_cerca=True):
    """Um juiz LLM falso que devolve exatamente o que o CLI devolveria."""
    corpo = json.dumps({"surpresa": surpresa, "motivos": list(motivos),
                        "expectativa_violada": violada}, ensure_ascii=False)
    texto = f"```json\n{corpo}\n```" if com_cerca else corpo

    def _judge(exchange, matches, cfg=None):
        _judge.chamadas += 1
        _judge.ultimo_prompt = gate.JUDGE_PROMPT.format(
            facts=gate._format_facts(matches), exchange=exchange)
        return llm.LLMResult(ok=True, called=True, text=texto,
                             data=llm.extract_json(texto), cost_usd=custo,
                             duration_ms=2300, model="haiku")
    _judge.chamadas = 0
    _judge.ultimo_prompt = ""
    return _judge


def juiz_proibido(exchange, matches, cfg=None):
    raise AssertionError("o portao gastou haiku numa decisao que era de graca")


class GateFixturesTest(unittest.TestCase):
    """A memoria de partida: um fato sobre relatorios, com proveniencia."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.open_db(Path(self.tmp.name) / "t.db")
        self.cfg = config.load()
        ep = db.add_episode(
            self.conn,
            "Rafael: a partir de agora quero os relatorios sempre em PDF, nunca "
            "em planilha.\nAssistente: entendido, PDF.",
            surprise_score=6.5, reasons=["pedido explicito"])
        db.add_episode(
            self.conn,
            "Rafael: manda o relatorio da semana.\nAssistente: enviado em PDF, "
            "como combinado.")
        self.fato_pdf = db.add_fact(
            self.conn,
            "Rafael quer os relatorios sempre em PDF, nunca em planilha.",
            confidence=0.80, episode_ids=[ep])

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _evento(self, kind):
        rows = db.list_events(self.conn, kind=kind, limit=1)
        self.assertTrue(rows, f"nenhum evento {kind} registrado")
        return json.loads(rows[0]["payload"])

    # ------------------------------------------------ fixture 1: fusao

    def test_fixture_1_confirmacao_rotineira_funde_de_graca(self):
        d = gate.process(
            self.conn,
            "Rafael: manda o relatorio da semana em PDF.\n"
            "Assistente: enviado em PDF, como combinado.",
            llm_fn=juiz_proibido)

        self.assertEqual(d.action, "fusao")
        self.assertEqual(d.resolution, "heuristica")
        self.assertFalse(d.paid_llm)
        self.assertEqual(d.cost_usd, 0.0)
        self.assertEqual(d.target_fact_id, self.fato_pdf)
        # a confirmacao nao virou episodio: ela se dissolveu no contador
        self.assertGreater(d.confidence_after, d.confidence_before)
        self.assertEqual(db.get_fact(self.conn, self.fato_pdf)["confidence"],
                         d.confidence_after)
        self.assertEqual(len(db.list_episodes(self.conn)), 2, "fusao nao cria episodio")

        p = self._evento("gate_merge")
        self.assertEqual(p["resolucao"], "heuristica")
        self.assertIs(p["pagou_llm"], False)
        self.assertEqual(p["custo_usd"], 0.0)
        self.assertEqual(p["confianca"], [round(d.confidence_before, 4),
                                          round(d.confidence_after, 4)])

    # -------------------------------------- fixture 2: carimbo gratis

    def test_fixture_2_correcao_explicita_carimba_sem_gastar_llm(self):
        d = gate.process(
            self.conn,
            "Rafael: nao, nao e planilha. Eu falei PDF, sempre PDF.\n"
            "Assistente: corrigido — PDF.",
            llm_fn=juiz_proibido)

        self.assertEqual(d.action, "carimbo")
        self.assertEqual(d.resolution, "heuristica")
        self.assertFalse(d.paid_llm)
        self.assertIn("correcao_usuario", d.signals)
        self.assertGreaterEqual(d.surprise, self.cfg["gate.surprise_threshold"])

        ep = db.get_episode(self.conn, d.episode_id)
        self.assertEqual(ep["status"], "buffered", "carimbado entra no purgatorio")
        self.assertIn("corrigiu", json.loads(ep["surprise_reasons"])[0])

        p = self._evento("gate_stamp")
        self.assertEqual(p["resolucao"], "heuristica")
        self.assertIs(p["pagou_llm"], False)
        self.assertIn("correcao_usuario", p["sinais"])

    # ------------------------------ fixture 3: carimbo pagando haiku

    def test_fixture_3_mudanca_sutil_chama_o_juiz_e_carimba(self):
        juiz = fake_judge(8, ["muda o formato ja acordado"], violada="#1")
        d = gate.process(
            self.conn,
            "Rafael: dessa vez manda em xlsx, ta dificil comentar no PDF.\n"
            "Assistente: ok, xlsx dessa vez.",
            llm_fn=juiz)

        self.assertEqual(juiz.chamadas, 1, "o juiz precisava ter sido chamado")
        self.assertEqual(d.action, "carimbo")
        self.assertEqual(d.resolution, "llm_haiku")
        self.assertTrue(d.paid_llm)
        self.assertAlmostEqual(d.cost_usd, 0.018)
        self.assertEqual(d.surprise, 8.0)
        self.assertEqual(d.target_fact_id, 1)
        self.assertIsNotNone(db.get_episode(self.conn, d.episode_id))
        # o prompt do juiz leva os fatos relacionados junto
        self.assertIn("PDF", juiz.ultimo_prompt)

        p = self._evento("gate_stamp")
        self.assertEqual(p["resolucao"], "llm_haiku")
        self.assertIs(p["pagou_llm"], True)
        self.assertEqual(p["custo_usd"], 0.018)
        self.assertEqual(p["latencia_llm_ms"], 2300)
        self.assertEqual(p["expectativa_violada"], "#1")

    def test_juiz_abaixo_do_limiar_nao_carimba(self):
        juiz = fake_judge(2, ["so confirma o que ja sabemos"])
        d = gate.process(
            self.conn,
            "Rafael: dessa vez manda em xlsx, ta dificil comentar no PDF.\n"
            "Assistente: ok.",
            llm_fn=juiz)
        self.assertEqual(juiz.chamadas, 1)
        self.assertNotEqual(d.action, "carimbo")
        self.assertEqual(d.resolution, "llm_haiku")
        self.assertTrue(d.paid_llm, "pagou mesmo sem carimbar — precisa aparecer no KPI")

    def test_juiz_sem_cerca_markdown_funciona_igual(self):
        """Se o CLI parar de embrulhar em ```json, o portao nao muda."""
        juiz = fake_judge(9, ["assunto novo"], com_cerca=False)
        d = gate.process(
            self.conn,
            "Rafael: dessa vez manda em xlsx, ta dificil comentar no PDF.\n"
            "Assistente: ok.",
            llm_fn=juiz)
        self.assertEqual(d.action, "carimbo")
        self.assertEqual(d.surprise, 9.0)


class GateSignalsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.open_db(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_pedido_de_memoria_carimba_de_graca(self):
        d = gate.process(self.conn, "Rafael: anota ai que eu odeio reuniao "
                                    "de segunda.\nAssistente: anotado.",
                         llm_fn=juiz_proibido)
        self.assertEqual(d.action, "carimbo")
        self.assertIn("pedido_memoria", d.signals)

    def test_falha_de_ferramenta_carimba_de_graca(self):
        d = gate.process(self.conn, "Rafael: tenta de novo.\nAssistente: a "
                                    "chamada falhou com permission denied.",
                         llm_fn=juiz_proibido)
        self.assertEqual(d.action, "carimbo")
        self.assertIn("falha_ferramenta", d.signals)

    def test_pedido_de_esquecimento_carimba_de_graca(self):
        d = gate.process(self.conn, "Rafael: esquece isso que eu falei.\n"
                                    "Assistente: ok.", llm_fn=juiz_proibido)
        self.assertEqual(d.action, "carimbo")
        self.assertIn("pedido_esquecimento", d.signals)

    def test_contradicao_precisa_de_fato_e_negacao(self):
        ep = db.add_episode(self.conn, "Rafael: uso Meta Ads como principal.")
        db.add_fact(self.conn, "Rafael usa Meta Ads como canal principal.",
                    episode_ids=[ep])
        d = gate.evaluate(self.conn, "Rafael: Meta Ads nunca foi meu canal "
                                     "principal, e o Google.", llm_fn=juiz_proibido)
        self.assertIn("contradicao_fato", d.signals)
        self.assertEqual(d.expectation_violated, "#1")

    def test_modo_deterministico_nao_chama_llm(self):
        """Invariante do projeto: o encanamento roda inteiro sem LLM."""
        ep = db.add_episode(self.conn, "Rafael: prefiro reunioes curtas.")
        db.add_fact(self.conn, "Rafael prefere reunioes curtas.", episode_ids=[ep])

        # o que a camada 1 resolve, ela resolve igual com ou sem LLM
        d = gate.process(self.conn, "Rafael: marca uma reuniao curta amanha.\n"
                                    "Assistente: marcada.", use_llm=False)
        self.assertEqual(d.action, "fusao")
        self.assertEqual(d.resolution, "heuristica")

        # o que seria inconclusivo cai no fallback deterministico, sem gastar nada
        d = gate.process(self.conn, "Rafael: essa reuniao pode passar de uma hora "
                                    "se precisar discutir o orcamento.", use_llm=False)
        self.assertEqual(d.resolution, "determinista")
        self.assertFalse(d.paid_llm)
        self.assertNotEqual(d.action, "carimbo")

    def test_falha_do_juiz_nao_derruba_o_pipeline(self):
        def juiz_quebrado(exchange, matches, cfg=None):
            return llm.LLMResult(ok=False, called=True, error="timeout",
                                 duration_ms=120000)
        ep = db.add_episode(self.conn, "Rafael: prefiro reunioes curtas.")
        db.add_fact(self.conn, "Rafael prefere reunioes curtas.", episode_ids=[ep])
        d = gate.process(self.conn, "Rafael: essa reuniao pode passar de uma hora "
                                    "se precisar discutir o orcamento.",
                         llm_fn=juiz_quebrado)
        # falha != carimbo por engano: cai no veredito da camada 1
        self.assertEqual(d.resolution, "llm_falhou")
        self.assertNotEqual(d.action, "carimbo")
        self.assertFalse(d.timed_out, "falha generica nao e estouro de orcamento")
        payload = json.loads(db.list_events(self.conn, kind="gate_merge")[0]["payload"])
        self.assertEqual(payload["erro_llm"], "timeout")
        self.assertIs(payload["estouro_timeout"], False)

    def test_timeout_do_juiz_cai_no_veredito_heuristico(self):
        """O portao nunca e gargalo: estourou o orcamento, camada 1 decide."""
        def juiz_lento(exchange, matches, cfg=None):
            return llm.LLMResult(ok=False, called=True, timed_out=True,
                                 timeout_s=8.0, error="timeout apos 8.0s",
                                 duration_ms=8000)
        ep = db.add_episode(self.conn, "Rafael: prefiro reunioes curtas.")
        fid = db.add_fact(self.conn, "Rafael prefere reunioes curtas.",
                          episode_ids=[ep], confidence=0.5)

        d = gate.process(self.conn, "Rafael: essa reuniao pode passar de uma hora "
                                    "se precisar discutir o orcamento.",
                         llm_fn=juiz_lento)

        self.assertEqual(d.resolution, "llm_timeout")
        self.assertTrue(d.timed_out)
        self.assertNotEqual(d.action, "carimbo", "timeout nao pode carimbar por engano")
        self.assertEqual(d.action, "fusao")          # veredito da camada 1
        self.assertEqual(d.target_fact_id, fid)

        p = json.loads(db.list_events(self.conn, kind="gate_merge")[0]["payload"])
        self.assertIs(p["estouro_timeout"], True)
        self.assertEqual(p["orcamento_s"], 8.0)
        self.assertEqual(p["resolucao"], "llm_timeout")
        self.assertEqual(p["latencia_llm_ms"], 8000)

    def test_orcamento_do_juiz_vem_do_config(self):
        vistos = {}

        def falso_call(prompt, *, model, cfg, timeout=None, expect_json=True):
            vistos["timeout"] = timeout
            return llm.LLMResult(ok=False, called=True, error="x")

        original, llm.call = llm.call, falso_call
        try:
            gate.judge("troca", [], config.load())
        finally:
            llm.call = original
        self.assertEqual(vistos["timeout"], config.load()["gate.judge_timeout_s"])

    def test_toda_decisao_registra_a_razao_da_resolucao(self):
        """O KPI: nenhum evento do portao pode sair sem resolucao/pagou_llm."""
        ep = db.add_episode(self.conn, "Rafael: prefiro reunioes curtas.")
        db.add_fact(self.conn, "Rafael prefere reunioes curtas.", episode_ids=[ep])
        gate.process(self.conn, "Rafael: nao, era o contrario.\nAssistente: ok.",
                     llm_fn=juiz_proibido)
        gate.process(self.conn, "Rafael: marca uma reuniao curta.\nAssistente: feito.",
                     llm_fn=juiz_proibido)
        gate.process(self.conn, "Rafael: e o clima em Curitiba amanha de tarde?",
                     llm_fn=fake_judge(1, ["irrelevante"]))

        eventos = [r for r in db.list_events(self.conn, limit=50)
                   if r["kind"] in cli_gate_kinds()]
        self.assertEqual(len(eventos), 3)
        for row in eventos:
            p = json.loads(row["payload"])
            for chave in ("decisao", "resolucao", "pagou_llm", "custo_usd",
                          "latencia_llm_ms", "surpresa", "motivos", "sinais"):
                self.assertIn(chave, p, f"{row['kind']} sem '{chave}' no payload")
            self.assertIn(p["resolucao"],
                          ("heuristica", "llm_haiku", "determinista", "llm_falhou"))


def cli_gate_kinds():
    from memory.cli import GATE_KINDS
    return GATE_KINDS



class PisoDeFusaoTest(unittest.TestCase):
    """Surpresa baixa nao autoriza fundir num fato que nao tem a ver."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.open_db(Path(self.tmp.name) / "t.db")
        ep = db.add_episode(self.conn, "Rafael: quero os relatorios em PDF.")
        self.fato = db.add_fact(self.conn, "Rafael quer os relatorios em PDF.",
                                episode_ids=[ep], confidence=0.80)
        # vocabulario de reuniao ja visto, para a troca de teste nao disparar
        # o sinal "assunto inedito" e ser carimbada antes de chegar ao juiz
        db.add_episode(self.conn, "Rafael: marca uma reuniao com o time.")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_troca_sem_relacao_com_surpresa_baixa_vira_passe(self):
        d = gate.process(self.conn, "Rafael: marca uma reuniao curta com o time.\n"
                                    "Assistente: marcada.",
                         llm_fn=fake_judge(1, ["rotina"]))
        self.assertEqual(d.action, "passe")
        self.assertEqual(db.get_fact(self.conn, self.fato)["confidence"], 0.80,
                         "confianca de fato nao relacionado nao pode subir")

    def test_troca_relacionada_ainda_funde(self):
        d = gate.process(self.conn, "Rafael: manda os relatorios em PDF, por favor.\n"
                                    "Assistente: enviados em PDF.",
                         llm_fn=fake_judge(1, ["confirma"]))
        self.assertEqual(d.action, "fusao")
        self.assertEqual(d.target_fact_id, self.fato)


class AutoinconsistenciaTest(unittest.TestCase):
    """Carimbo que nasce do ASSISTENTE contradizer a memoria.

    Nao e novidade do usuario: e sintoma de falha de recuperacao, e por isso
    tem razao propria no log e taxa separada no calibrate.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.open_db(Path(self.tmp.name) / "t.db")
        ep = db.add_episode(self.conn, "Rafael: prefiro reunioes de 30 minutos.")
        self.fato = db.add_fact(self.conn, "Rafael prefere reunioes de 30 minutos.",
                                episode_ids=[ep], confidence=0.8)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_assistente_negando_o_que_a_memoria_sabe_e_autoinconsistencia(self):
        d = gate.process(
            self.conn,
            "Rafael: quanto tempo eu prefiro que durem as reunioes?\n"
            "Assistente: nao tenho nada registrado sobre duracao de reunioes.",
            llm_fn=juiz_proibido)

        self.assertEqual(d.action, "carimbo")
        self.assertTrue(d.self_inconsistent)
        self.assertEqual(d.stamp_reason, "autoinconsistencia")
        self.assertIn("autoinconsistencia", d.signals)
        self.assertIn("AUTOINCONSISTENCIA", d.headline())

        p = json.loads(db.list_events(self.conn, kind="gate_stamp")[0]["payload"])
        self.assertIs(p["autoinconsistencia"], True)
        self.assertEqual(p["razao_carimbo"], "autoinconsistencia")

    def test_negacao_sem_fato_relacionado_nao_e_autoinconsistencia(self):
        """Nao saber o que ninguem contou e correto, nao e doenca."""
        d = gate.evaluate(
            self.conn,
            "Rafael: qual e a capital da Mongolia?\n"
            "Assistente: nao sei responder isso.",
            llm_fn=fake_judge(2, ["irrelevante"]))
        self.assertFalse(d.self_inconsistent)
        self.assertNotIn("autoinconsistencia", d.signals)

    def test_o_juiz_tambem_pode_apontar_a_origem_no_assistente(self):
        juiz = fake_judge(8, ["a resposta ignora o fato conhecido"])
        # o juiz falso nao devolve "origem"; simulamos a resposta completa
        def juiz_com_origem(exchange, matches, cfg=None):
            r = juiz(exchange, matches, cfg)
            r.data = dict(r.data, origem="assistente")
            return r

        d = gate.process(self.conn,
                         "Rafael: qual foi o combinado sobre a duracao das nossas "
                         "reunioes de alinhamento semanal?\n"
                         "Assistente: acho que voce prefere reunioes longas.",
                         llm_fn=juiz_com_origem)
        self.assertEqual(d.action, "carimbo")
        self.assertTrue(d.self_inconsistent)
        self.assertEqual(d.stamp_reason, "autoinconsistencia")

    def test_carimbo_normal_tem_razao_propria(self):
        d = gate.process(self.conn, "Rafael: nao, na verdade prefiro reunioes de "
                                    "1 hora.\nAssistente: anotado.",
                         llm_fn=juiz_proibido)
        self.assertEqual(d.stamp_reason, "heuristica")
        self.assertFalse(d.self_inconsistent)

    def test_fusao_e_passe_nao_tem_razao_de_carimbo(self):
        d = gate.evaluate(self.conn, "Rafael: marca uma reuniao de 30 minutos.\n"
                                     "Assistente: marcada.", llm_fn=juiz_proibido)
        self.assertIsNone(d.stamp_reason)


class ColdStartTest(unittest.TestCase):
    """Banco vazio: as primeiras mensagens sao as que mais importam."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.open_db(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_primeira_mensagem_com_conteudo_e_carimbada(self):
        d = gate.process(self.conn,
                         "Rafael: meu nome e Rafael e eu trabalho com gestao de "
                         "trafego pago.\nAssistente: prazer, Rafael!",
                         llm_fn=juiz_proibido)
        self.assertEqual(d.action, "carimbo", "identidade do usuario nao pode passar")
        self.assertIn("assunto_inedito", d.signals)

    def test_saudacao_vazia_continua_passando_batido(self):
        d = gate.process(self.conn, "Rafael: bom dia, tudo certo?\n"
                                    "Assistente: bom dia! tudo certo.",
                         llm_fn=fake_judge(1, ["saudacao"]))
        self.assertNotEqual(d.action, "carimbo")
        self.assertNotIn("assunto_inedito", d.signals)


class NovidadeDoUsuarioTest(unittest.TestCase):
    """Novidade e o que o usuario trouxe — nao a prosa da resposta."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.open_db(Path(self.tmp.name) / "t.db")
        ep = db.add_episode(self.conn, "Rafael: quero os relatorios das campanhas "
                                       "em PDF.\nAssistente: anotado.")
        db.add_fact(self.conn, "Rafael quer os relatorios das campanhas em PDF.",
                    episode_ids=[ep])

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_resposta_longa_do_assistente_nao_cria_novidade(self):
        h = gate.heuristics(
            self.conn,
            "Rafael: obrigado, era isso por hoje.\n"
            "Assistente: imagina! Qualquer coisa e so chamar. Aproveito para "
            "lembrar que a proxima janela de otimizacao do orcamento acontece "
            "na terca, e que o criativo novo entra em rotacao amanha.")
        self.assertNotIn("assunto_inedito", h["signals"],
                         "as palavras sao do assistente, nao do usuario")

    def test_assunto_novo_dito_pelo_usuario_ainda_dispara(self):
        h = gate.heuristics(
            self.conn,
            "Rafael: comecei a treinar jiu-jitsu tres vezes por semana.\n"
            "Assistente: legal!")
        self.assertIn("assunto_inedito", h["signals"])

if __name__ == "__main__":
    unittest.main()
