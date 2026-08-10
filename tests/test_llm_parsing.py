"""Fase 2 — desembrulho tolerante da resposta do `claude -p`.

O CLI hoje devolve o JSON dentro de cerca markdown, mas isso e comportamento
de versao, nao contrato. Todo formato abaixo tem que funcionar.
"""

import unittest

from lembra.llm import extract_json, strip_fences


class ExtractJsonTest(unittest.TestCase):
    ALVO = {"surpresa": 8, "motivos": ["contradiz"], "expectativa_violada": "#12"}

    def _assert_alvo(self, texto, msg):
        self.assertEqual(extract_json(texto), self.ALVO, msg)

    def test_com_cerca_json(self):
        self._assert_alvo(
            '```json\n{"surpresa": 8, "motivos": ["contradiz"], '
            '"expectativa_violada": "#12"}\n```',
            "cerca ```json (o comportamento do CLI hoje)")

    def test_sem_cerca_nenhuma(self):
        """Se o CLI parar de embrulhar amanha, nada aqui pode quebrar."""
        self._assert_alvo(
            '{"surpresa": 8, "motivos": ["contradiz"], "expectativa_violada": "#12"}',
            "JSON puro, sem cerca")

    def test_cerca_generica_e_variantes(self):
        corpo = ('{"surpresa": 8, "motivos": ["contradiz"], '
                 '"expectativa_violada": "#12"}')
        for variante in (f"```\n{corpo}\n```",
                         f"~~~json\n{corpo}\n~~~",
                         f"```JSON\n{corpo}\n```",
                         f"```json\n{corpo}",          # cerca sem fechar
                         f"  \n```json\n{corpo}\n```\n  "):
            self._assert_alvo(variante, f"variante: {variante[:14]!r}")

    def test_prosa_em_volta(self):
        self._assert_alvo(
            'Claro! Segue a analise:\n\n```json\n'
            '{"surpresa": 8, "motivos": ["contradiz"], "expectativa_violada": "#12"}\n'
            '```\n\nEspero ter ajudado.',
            "prosa antes e depois")

    def test_prosa_sem_cerca(self):
        self._assert_alvo(
            'Resposta: {"surpresa": 8, "motivos": ["contradiz"], '
            '"expectativa_violada": "#12"} — pronto.',
            "JSON no meio de prosa, sem cerca")

    def test_chaves_dentro_de_string_nao_confundem(self):
        self.assertEqual(
            extract_json('```json\n{"motivo": "usou { e } no texto", "n": 1}\n```'),
            {"motivo": "usou { e } no texto", "n": 1})

    def test_escape_de_aspas(self):
        self.assertEqual(
            extract_json(r'{"motivo": "ele disse \"nao\"", "n": 2}'),
            {"motivo": 'ele disse "nao"', "n": 2})

    def test_lista_no_topo(self):
        self.assertEqual(extract_json('```json\n[{"a": 1}, {"b": 2}]\n```'),
                         [{"a": 1}, {"b": 2}])

    def test_sem_json_devolve_none(self):
        for lixo in ("", None, "desculpe, nao consegui responder",
                     "```\nnao e json\n```"):
            self.assertIsNone(extract_json(lixo), f"deveria ser None: {lixo!r}")

    def test_strip_fences_idempotente(self):
        puro = '{"a": 1}'
        self.assertEqual(strip_fences(puro), puro)
        self.assertEqual(strip_fences(strip_fences(f"```json\n{puro}\n```")), puro)



class AsObjectTest(unittest.TestCase):
    """A mesma pergunta as vezes volta {...} e as vezes [{...}]."""

    def test_dict_passa_direto(self):
        from lembra.llm import as_object
        self.assertEqual(as_object({"a": 1}), {"a": 1})

    def test_lista_de_um_e_desembrulhada(self):
        from lembra.llm import as_object
        self.assertEqual(as_object([{"a": 1}]), {"a": 1})

    def test_lista_de_varios_nao_e_objeto(self):
        from lembra.llm import as_object
        self.assertIsNone(as_object([{"a": 1}, {"b": 2}]))
        self.assertIsNone(as_object(None))
        self.assertIsNone(as_object("texto"))

if __name__ == "__main__":
    unittest.main()
