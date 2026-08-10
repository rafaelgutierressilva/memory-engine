"""Fase 6 — o juiz do benchmark precisa ser o OFICIAL, leniencias inclusas.

Se estes testes quebrarem, os numeros deixam de ser comparaveis com os
publicados. Os templates foram copiados verbatim de
src/evaluation/evaluate_qa.py do repositorio xiaowu0162/LongMemEval.
"""

import unittest

import bench


class TemplatesOficiaisTest(unittest.TestCase):
    def test_temporal_carrega_a_leniencia_de_um_dia(self):
        prompt, leniencia = bench.judge_prompt(
            "q1", "temporal-reasoning", "How many days ago did I start?",
            "18 days", "About 19 days ago.")
        self.assertEqual(leniencia, "temporal-reasoning")
        self.assertIn("do not penalize off-by-one errors for the number of days",
                      prompt)
        self.assertIn("predicting 19 days when the answer is 18", prompt)

    def test_knowledge_update_aceita_a_resposta_antiga_junto(self):
        prompt, leniencia = bench.judge_prompt(
            "q2", "knowledge-update", "Where do I work now?",
            "Acme Corp", "You used to work at Globex, and now you work at Acme Corp.")
        self.assertEqual(leniencia, "knowledge-update")
        self.assertIn("contains some previous information along with an updated "
                      "answer", prompt)
        self.assertIn("as long as the updated answer is the required answer", prompt)
        self.assertNotIn("off-by-one", prompt)

    def test_preferencia_usa_rubrica_e_nao_exige_tudo(self):
        prompt, leniencia = bench.judge_prompt(
            "q3", "single-session-preference", "Suggest a workout.",
            "rubric text", "resposta")
        self.assertEqual(leniencia, "single-session-preference")
        self.assertIn("Rubric:", prompt)
        self.assertIn("does not need to reflect all the points in the rubric", prompt)

    def test_abstencao_julga_o_reconhecimento_da_falta_de_resposta(self):
        prompt, leniencia = bench.judge_prompt(
            "q4_abs", "multi-session", "What is my dog's name?",
            "never mentioned a dog", "I don't have that information.")
        self.assertEqual(leniencia, "abstention")
        self.assertIn("unanswerable", prompt)
        self.assertIn("Does the model correctly identify the question as "
                      "unanswerable?", prompt)

    def test_abstencao_vence_o_tipo_da_pergunta(self):
        """O oficial decide por sufixo _abs no id, nao pelo question_type."""
        _, leniencia = bench.judge_prompt(
            "q5_abs", "temporal-reasoning", "p", "a", "r")
        self.assertEqual(leniencia, "abstention")

    def test_tipos_sem_leniencia_usam_o_template_base(self):
        for tipo in ("single-session-user", "single-session-assistant",
                     "multi-session"):
            prompt, leniencia = bench.judge_prompt("q", tipo, "p", "a", "r")
            self.assertEqual(leniencia, "default", tipo)
            self.assertIn("If the response only contains a subset of the "
                          "information required by the answer, answer no.", prompt)
            self.assertNotIn("off-by-one", prompt)

    def test_pergunta_resposta_e_hipotese_entram_no_prompt(self):
        prompt, _ = bench.judge_prompt("q", "multi-session", "PERGUNTA_X",
                                       "RESPOSTA_Y", "HIPOTESE_Z")
        self.assertIn("PERGUNTA_X", prompt)
        self.assertIn("RESPOSTA_Y", prompt)
        self.assertIn("HIPOTESE_Z", prompt)


if __name__ == "__main__":
    unittest.main()
