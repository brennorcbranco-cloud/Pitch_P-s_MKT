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

import base64
import json
import os
import re
from pathlib import Path
from datetime import date

from flask import Flask, request, render_template_string

_CLIENTE_IA = None
_MOTIVO_IA_DESLIGADA = None

try:
    import anthropic
    if not os.environ.get("ANTHROPIC_API_KEY"):
        _MOTIVO_IA_DESLIGADA = "variável de ambiente ANTHROPIC_API_KEY não encontrada"
    else:
        _CLIENTE_IA = anthropic.Anthropic()
except ImportError as exc:
    _MOTIVO_IA_DESLIGADA = f"biblioteca 'anthropic' não instalada ({exc})"
except Exception as exc:
    _MOTIVO_IA_DESLIGADA = f"falha ao inicializar cliente Anthropic ({exc})"

print(f"[startup] IA de imagem: {'HABILITADA' if _CLIENTE_IA else 'DESABILITADA — motivo: ' + str(_MOTIVO_IA_DESLIGADA)}")

app = Flask(__name__)

MEDIA_TYPES_PERMITIDOS = {
    "image/jpeg": "image/jpeg",
    "image/png": "image/png",
    "image/webp": "image/webp",
    "image/gif": "image/gif",
}

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


PROMPT_SISTEMA_IMAGEM = """Você descreve fatos visuais objetivos de uma peça publicitária. \
Você NÃO decide se algo é permitido ou proibido — isso é papel de outra etapa do sistema. \
Responda APENAS com um objeto JSON válido, sem texto antes ou depois, com exatamente estas chaves:

{
  "duas_ou_mais_imagens_lado_a_lado": bool,
  "aparenta_ser_antes_depois": bool,
  "foco_zoom_regiao_corporal_especifica": bool,
  "texto_sobreposto_na_imagem": string (transcreva literalmente o texto visível na imagem, ou "" se não houver),
  "produto_ou_procedimento_aparente": string (ex: "preenchimento facial", "toxina botulinica", "nao identificavel"),
  "pessoa_identificavel_no_antes_depois": bool
}

Seja literal e conservador: marque true apenas quando o elemento estiver claramente presente. \
Não avalie legalidade, ética ou adequação — apenas descreva o que está visualmente presente."""


def analisar_imagem_com_ia(imagem_bytes: bytes, media_type: str) -> dict:
    """
    Pede ao modelo apenas OBSERVAÇÕES estruturadas e limitadas (não um
    veredito). O cruzamento com regras.json acontece depois, em código
    determinístico — o modelo nunca decide sozinho se algo viola uma norma.
    """
    if _CLIENTE_IA is None:
        return {"erro": f"Análise de imagem desligada ({_MOTIVO_IA_DESLIGADA}). "
                         "Configure a variável ANTHROPIC_API_KEY no Railway e faça redeploy."}

    imagem_b64 = base64.standard_b64encode(imagem_bytes).decode("utf-8")

    try:
        resposta = _CLIENTE_IA.messages.create(
            model="claude-sonnet-5",
            max_tokens=500,
            system=PROMPT_SISTEMA_IMAGEM,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": imagem_b64}},
                    {"type": "text", "text": "Descreva esta peça publicitária conforme o formato JSON pedido."},
                ],
            }],
        )
        texto_resposta = resposta.content[0].text.strip()
        # remove eventuais cercas de bloco de código, caso o modelo as inclua
        texto_resposta = re.sub(r"^```(?:json)?|```$", "", texto_resposta.strip(), flags=re.MULTILINE).strip()
        return json.loads(texto_resposta)
    except json.JSONDecodeError:
        return {"erro": "A análise de imagem não retornou um JSON válido. Marcar para revisão manual."}
    except Exception as exc:  # falha de rede, chave inválida, etc.
        return {"erro": f"Falha ao chamar a API de visão: {exc}"}


def cruzar_observacoes_imagem_com_regras(observacoes: dict) -> list[dict]:
    """Aplica regras.json (determinístico) sobre as observações do modelo."""
    if "erro" in observacoes:
        return [{"erro": observacoes["erro"]}]

    achados = []
    mapa_por_id = {r["id"]: r for r in REGRAS}

    def adicionar(regra_id, trecho=None):
        regra = mapa_por_id[regra_id]
        achados.append({
            "id": regra["id"], "fonte": regra["fonte"], "norma": regra["norma"],
            "artigo": regra.get("artigo", ""), "regra": regra["regra"],
            "trechos": [trecho] if trecho else [],
            "revisao_manual": False, "estabilidade": regra["estabilidade"],
            "ultima_verificacao": regra["ultima_verificacao"],
        })

    if observacoes.get("aparenta_ser_antes_depois") or observacoes.get("duas_ou_mais_imagens_lado_a_lado"):
        adicionar(5)   # CFM Art. 14, II
        adicionar(11)  # Meta - antes/depois para rugas/botox/preenchedor

    if observacoes.get("foco_zoom_regiao_corporal_especifica"):
        adicionar(14)  # Meta - zoom em condição/região do corpo

    if observacoes.get("pessoa_identificavel_no_antes_depois"):
        adicionar(5, trecho="paciente potencialmente identificável na imagem")

    texto_imagem = observacoes.get("texto_sobreposto_na_imagem", "") or ""
    if texto_imagem:
        achados_texto = analisar_texto(texto_imagem, "geral")
        for a in achados_texto:
            a["trechos"] = a["trechos"] or [f'(no texto da imagem) "{texto_imagem}"']
        achados.extend(achados_texto)

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
  <form method="post" enctype="multipart/form-data">
    <textarea name="texto" placeholder="Cole aqui o texto do anúncio...">{{ texto or '' }}</textarea><br>
    <select name="categoria">
      {% for valor, label in categorias %}
        <option value="{{ valor }}" {% if valor == categoria_selecionada %}selected{% endif %}>{{ label }}</option>
      {% endfor %}
    </select><br>
    <label style="display:block;margin-top:10px;">
      Imagem do post (opcional, jpg/png/webp/gif):
      <input type="file" name="imagem" accept="image/jpeg,image/png,image/webp,image/gif">
    </label>
    <button type="submit">Analisar</button>
  </form>
  {% if not ia_disponivel %}
    <p style="font-size:0.8rem;color:#a05a00;">
      ⚠ Análise de imagem desligada: variável ANTHROPIC_API_KEY não configurada neste ambiente.
    </p>
  {% endif %}

  {% if analisado %}
    <h2>Resultado</h2>
    {% if observacoes_imagem and 'erro' not in observacoes_imagem %}
      <details style="margin-bottom:14px;">
        <summary style="cursor:pointer;">Observação bruta do modelo de visão (auditoria)</summary>
        <pre style="background:#f4f4f4;padding:10px;border-radius:4px;font-size:0.85rem;white-space:pre-wrap;">{{ observacoes_imagem | tojson(indent=2) }}</pre>
      </details>
    {% elif observacoes_imagem and 'erro' in observacoes_imagem %}
      <p style="color:#a05a00;">⚠ {{ observacoes_imagem.erro }}</p>
    {% endif %}
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
    observacoes_imagem = None
    categoria_selecionada = "geral"
    analisado = False

    if request.method == "POST":
        texto = request.form.get("texto", "")
        categoria_selecionada = request.form.get("categoria", "geral")
        achados = analisar_texto(texto, categoria_selecionada) if texto.strip() else []

        arquivo_imagem = request.files.get("imagem")
        if arquivo_imagem and arquivo_imagem.filename:
            media_type = MEDIA_TYPES_PERMITIDOS.get(arquivo_imagem.mimetype)
            if not media_type:
                observacoes_imagem = {"erro": f"Formato de imagem não suportado: {arquivo_imagem.mimetype}"}
            else:
                imagem_bytes = arquivo_imagem.read()
                observacoes_imagem = analisar_imagem_com_ia(imagem_bytes, media_type)
                if "erro" not in observacoes_imagem:
                    achados.extend(cruzar_observacoes_imagem_com_regras(observacoes_imagem))

        analisado = True

    return render_template_string(
        PAGINA,
        texto=texto,
        achados=achados,
        observacoes_imagem=observacoes_imagem,
        categorias=CATEGORIAS,
        categoria_selecionada=categoria_selecionada,
        analisado=analisado,
        total_regras=len(REGRAS),
        ia_disponivel=_CLIENTE_IA is not None,
    )


if __name__ == "__main__":
    import os
    # Em produção (Railway), a variável PORT é injetada pelo ambiente e
    # debug fica desligado por padrão — debug=True expõe o depurador do
    # Werkzeug publicamente, o que é um risco de segurança real.
    porta = int(os.environ.get("PORT", 5000))
    modo_debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=porta, debug=modo_debug)
