"""
Compliance Copilot - v1 (protótipo MVP para pitch de MBA)

O que este protótipo FAZ:
- Compara um texto de anúncio/post contra uma base de regras estruturada
  (regras.json), extraída manualmente de fonte primária (CFM 2.336/2023,
  ANVISA RDC 96/2008, Meta Transparency Center, support.google.com/adspolicy).
- Sinaliza cada trecho suspeito com o artigo/norma exata que ele viola.
- Diferencia regras "estáveis" (jurídicas) de "voláteis" (políticas de
  plataforma), para deixar claro quais precisam de recheck mais frequente.

O que este protótipo NÃO FAZ (de propósito, e isso deve ser dito na
demo/pitch como decisão de design, não como limitação escondida):
- Não usa um LLM "solto" perguntando "isso viola alguma lei?" — isso é
  exatamente o tipo de alucinação que discutimos evitar. A v1 é
  rule-based (casamento de palavra-chave) para ser 100% auditável: toda
  sinalização aponta pra uma regra específica da base, não pra uma
  "opinião" de modelo de linguagem.
- Não substitui aprovação jurídica/regulatória humana. É uma camada de
  triagem antes do jurídico, reduzindo o volume que chega até ele.
- Não analisa imagem (antes/depois, zoom em condição). Regras de imagem
  ficam sinalizadas como "revisão manual obrigatória".

Próximo passo natural (fora do escopo do MVP): trocar o casamento de
palavra-chave por uma busca semântica com citação (RAG) sobre o mesmo
regras.json, mantendo a mesma garantia de que toda saída aponta para uma
regra da base, nunca para memória livre do modelo.
"""

import json
import re
from pathlib import Path
from datetime import date

from flask import Flask, request, render_template_string

app = Flask(__name__)

REGRAS_PATH = Path(__file__).parent / "regras.json"
with open(REGRAS_PATH, encoding="utf-8") as f:
    BASE = json.load(f)

REGRAS = BASE["regras"]

CATEGORIAS = [
    ("geral", "Genérico / não especificado"),
    ("medicamento_prescricao", "Medicamento sob prescrição (ex: Toxina)"),
    ("produto_para_saude", "Produto para saúde (preenchedor HA, CaHA, fios PDO)"),
]


def normalizar(texto: str) -> str:
    """Minúsculas e sem acento simples, só pra casamento de palavra-chave."""
    substituicoes = str.maketrans("áàâãéêíóôõúç", "aaaaeeiooouc")
    return texto.lower().translate(substituicoes)


def analisar_texto(texto: str, categoria_produto: str) -> list[dict]:
    texto_norm = normalizar(texto)
    achados = []

    for regra in REGRAS:
        aplicavel = regra["categoria_produto"] in ("geral", categoria_produto)
        if not aplicavel:
            continue

        gatilhos = regra.get("keywords_gatilho", [])
        trechos_encontrados = []
        for termo in gatilhos:
            termo_norm = normalizar(termo)
            if termo_norm and termo_norm in texto_norm:
                # localizar o trecho original (não normalizado) pra exibir
                match = re.search(re.escape(termo_norm), texto_norm)
                if match:
                    trechos_encontrados.append(termo)

        exige_revisao_manual = "observacao" in regra and not gatilhos

        if trechos_encontrados or exige_revisao_manual:
            achados.append({
                "id": regra["id"],
                "fonte": regra["fonte"],
                "norma": regra["norma"],
                "artigo": regra.get("artigo", ""),
                "regra": regra["regra"],
                "trechos": trechos_encontrados,
                "revisao_manual": exige_revisao_manual,
                "estabilidade": regra["estabilidade"],
                "ultima_verificacao": regra["ultima_verificacao"],
            })

    return achados


PAGINA = """
<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<title>Compliance Copilot — v1</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 820px; margin: 40px auto; padding: 0 16px; color: #1a1a1a; }
  h1 { font-size: 1.4rem; }
  textarea { width: 100%; height: 140px; font-size: 1rem; padding: 8px; box-sizing: border-box; }
  select, button { font-size: 1rem; padding: 8px; margin-top: 8px; }
  button { cursor: pointer; background: #1a1a1a; color: white; border: none; border-radius: 4px; }
  .achado { border-left: 4px solid #c0392b; background: #fdf1f0; padding: 10px 14px; margin: 10px 0; border-radius: 4px; }
  .achado.volatil { border-left-color: #d68910; background: #fef6e7; }
  .fonte { font-weight: 600; }
  .norma { font-size: 0.9rem; color: #555; }
  .trechos { font-size: 0.9rem; margin-top: 4px; }
  .ok { color: #1e7e34; font-weight: 600; }
  .aviso-estabilidade { font-size: 0.8rem; color: #888; margin-top: 2px;}
</style>
</head>
<body>
  <h1>Compliance Copilot — v1 (protótipo)</h1>
  <p>Cole o texto de um anúncio/post abaixo. A análise compara contra {{ total_regras }} regras extraídas de fonte primária (CFM, ANVISA, Meta Ads, Google Ads).</p>
  <form method="post">
    <textarea name="texto" placeholder="Cole aqui o texto do anúncio...">{{ texto or '' }}</textarea><br>
    <select name="categoria">
      {% for valor, label in categorias %}
        <option value="{{ valor }}" {% if valor == categoria_selecionada %}selected{% endif %}>{{ label }}</option>
      {% endfor %}
    </select>
    <button type="submit">Analisar</button>
  </form>

  {% if analisado %}
    <h2>Resultado</h2>
    {% if achados %}
      {% for a in achados %}
        <div class="achado {{ 'volatil' if a.estabilidade == 'volatil' else '' }}">
          <div class="fonte">{{ a.fonte }} — {{ a.norma }} {{ a.artigo }}</div>
          <div>{{ a.regra }}</div>
          {% if a.trechos %}
            <div class="trechos">Trecho(s) sinalizado(s): {{ a.trechos | join(', ') }}</div>
          {% endif %}
          {% if a.revisao_manual %}
            <div class="trechos">⚠ Não detectável por palavra-chave — marcar para revisão manual.</div>
          {% endif %}
          <div class="aviso-estabilidade">
            Estabilidade da fonte: {{ a.estabilidade }} · última verificação: {{ a.ultima_verificacao }}
          </div>
        </div>
      {% endfor %}
    {% else %}
      <p class="ok">Nenhum risco detectado pela base atual de regras.</p>
    {% endif %}
    <p style="font-size:0.85rem;color:#777;">
      Isto é uma triagem automatizada, não uma aprovação jurídica/regulatória.
      Toda peça deve passar por revisão humana antes de publicação.
    </p>
  {% endif %}
</body>
</html>
"""


@app.route("/", methods=["GET", "POST"])
def index():
    texto = None
    achados = None
    categoria_selecionada = "geral"
    analisado = False

    if request.method == "POST":
        texto = request.form.get("texto", "")
        categoria_selecionada = request.form.get("categoria", "geral")
        achados = analisar_texto(texto, categoria_selecionada)
        analisado = True

    return render_template_string(
        PAGINA,
        texto=texto,
        achados=achados,
        categorias=CATEGORIAS,
        categoria_selecionada=categoria_selecionada,
        analisado=analisado,
        total_regras=len(REGRAS),
    )


if __name__ == "__main__":
    import os
    # Em produção (Railway), a variável PORT é injetada pelo ambiente e
    # debug fica desligado por padrão — debug=True expõe o depurador do
    # Werkzeug publicamente, o que é um risco de segurança real.
    porta = int(os.environ.get("PORT", 5000))
    modo_debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=porta, debug=modo_debug)
