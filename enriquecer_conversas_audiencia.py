"""
MÓDULO 3 — Enriquecimento de Conversas de Audiência (por entrada)
Roda ANTES do analyze_audiencia.py, na mesma janela de sexta 07:00 (Paris).

Por que este script existe:
  As entradas de "🗨️ CONVERSAS AUDIÊNCIA (Instagram)" chegam como PRINTS de
  conversa (propriedade "Screenshot"), sem texto transcrito. O
  analyze_audiencia.py original só lia a propriedade "Texte" — como ela
  ficava vazia, o bilan semanal saía todo em branco mesmo com prints reais
  anexados.

Fluxo deste script:
1. Busca entradas CATEGORIA=AUDIÊNCIA + STATUS=NOVO + "Enviar para Claude"=✓
2. Para cada entrada, baixa o(s) screenshot(s) (via URL assinada da API do
   Notion) e manda para a Claude com visão, pedindo transcrição + análise
   estruturada (dor/necessidade, insight, ideia de conteúdo, pilar, prioridade,
   palavras-chave, persona)
3. Preenche essas propriedades diretamente na linha e marca STATUS="Analisado"
4. O analyze_audiencia.py (que roda em seguida) busca STATUS="Analisado"
   para montar o bilan semanal agregado, e marca STATUS="PROCESSADO" ao final

Se o parse do JSON falhar ou o download da imagem falhar para uma entrada,
essa entrada específica é pulada (mantém STATUS=NOVO para nova tentativa na
próxima rodada) — não derruba o job inteiro.

Variáveis de ambiente esperadas: NOTION_TOKEN, ANTHROPIC_API_KEY, NOTION_DB_IG
"""

import os
import json
import base64
from datetime import datetime, timezone
import httpx
from dotenv import load_dotenv
from notion_client import Client as NotionClient
from notion_client.errors import APIResponseError
import anthropic

load_dotenv()

NOTION_TOKEN      = os.environ["NOTION_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
NOTION_DB_ID      = os.environ["NOTION_DB_IG"]  # Conversas Audiência (Instagram)

notion = NotionClient(auth=NOTION_TOKEN)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

PILARES_VALIDOS     = {"Sistema", "Trajetória", "Identidade", "Sociedade", "viral"}
PRIORIDADES_VALIDAS = {"Alta", "Média", "Baixa"}
PERSONAS_VALIDAS    = {"P01 - Sonhadora", "P02 - Recém-chegada", "P03 - Adaptada", "P04 - Potencial"}

_data_source_cache = {}


def resolver_data_source_id(database_id: str) -> str:
    if database_id in _data_source_cache:
        return _data_source_cache[database_id]
    db = notion.databases.retrieve(database_id=database_id)
    data_sources = db.get("data_sources", [])
    if not data_sources:
        raise RuntimeError(f"O database {database_id} não retornou nenhum data_source.")
    data_source_id = data_sources[0]["id"]
    _data_source_cache[database_id] = data_source_id
    return data_source_id


def buscar_entradas_para_enriquecer() -> list:
    """Busca entradas prontas para análise: NOVO + AUDIÊNCIA + 'Enviar para Claude' marcado."""
    data_source_id = resolver_data_source_id(NOTION_DB_ID)
    resp = notion.data_sources.query(
        data_source_id=data_source_id,
        filter={
            "and": [
                {"property": "CATEGORIA", "select": {"equals": "AUDIÊNCIA"}},
                {"property": "STATUS", "select": {"equals": "NOVO"}},
                {"property": "Enviar para Claude", "checkbox": {"equals": True}},
            ]
        }
    )
    return resp.get("results", [])


def _extrair_urls_screenshot(page: dict) -> list:
    arquivos = page["properties"].get("Screenshot", {}).get("files", [])
    urls = []
    for f in arquivos:
        if f.get("type") == "file" and f.get("file", {}).get("url"):
            urls.append(f["file"]["url"])
        elif f.get("type") == "external" and f.get("external", {}).get("url"):
            urls.append(f["external"]["url"])
    return urls


def _baixar_imagem(url: str):
    resp = httpx.get(url, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    media_type = resp.headers.get("content-type", "image/png").split(";")[0].strip()
    if not media_type.startswith("image/"):
        media_type = "image/png"
    return resp.content, media_type


def _montar_blocos_imagem(urls: list) -> list:
    blocos = []
    for url in urls:
        try:
            conteudo, media_type = _baixar_imagem(url)
        except Exception as e:
            print(f"    ⚠ Falha ao baixar um screenshot: {e}")
            continue
        blocos.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.b64encode(conteudo).decode("utf-8")
            }
        })
    return blocos


ENTRY_SCHEMA_JSON = """{
  "nome_curto": "string (até 60 caracteres)",
  "texte": "string (frase-chave ou resumo fiel da troca, até 300 caracteres)",
  "dor_necessidade": "string",
  "insight_audiencia": "string",
  "ideia_conteudo": "string",
  "pilar_sugerido": "Sistema|Trajetória|Identidade|Sociedade|viral",
  "prioridade": "Alta|Média|Baixa",
  "palavras_chave": "string (3 a 6 palavras separadas por vírgula)",
  "persona": ["P01 - Sonhadora|P02 - Recém-chegada|P03 - Adaptada|P04 - Potencial", "..."]
}"""


def analisar_entrada_com_claude(urls_screenshot: list, tipo: str, plataforma: str) -> dict:
    blocos_imagem = _montar_blocos_imagem(urls_screenshot)
    if not blocos_imagem:
        return {"erro_parse": True, "texto_bruto": "Nenhuma imagem pôde ser baixada para esta entrada."}

    prompt_texto = f"""Você é o sistema editorial do canal Por Dentro — imigrante brasileira na França, conteúdo sobre trabalho, saúde, burocracia, moradia, cultura.

As imagens acima são print(s) de uma conversa real com a audiência ({tipo}, plataforma {plataforma}). Leia a conversa e responda APENAS com um JSON válido (sem markdown, sem cercas de código, sem texto fora do JSON) no formato exato abaixo. Baseie-se apenas no que está nas imagens — nunca invente.

{ENTRY_SCHEMA_JSON}

Onde:
- nome_curto: título curto do que se trata (vira o nome da linha no Notion)
- texte: a frase-chave ou resumo fiel da troca, citando as palavras da pessoa quando possível
- dor_necessidade: a dor/necessidade real que essa pessoa expressou
- insight_audiencia: o que isso revela sobre a audiência de forma mais ampla
- ideia_conteudo: um reel/carrossel/story concreto que responde a essa dor
- pilar_sugerido: exatamente um entre Sistema, Trajetória, Identidade, Sociedade, viral
- prioridade: Alta se é uma dor recorrente/urgente, Média ou Baixa caso contrário
- palavras_chave: 3 a 6 palavras-chave separadas por vírgula
- persona: uma ou mais entre "P01 - Sonhadora", "P02 - Recém-chegada", "P03 - Adaptada", "P04 - Potencial", conforme o perfil de quem está falando"""

    content = blocos_imagem + [{"type": "text", "text": prompt_texto}]

    resp = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1200,
        messages=[{"role": "user", "content": content}]
    )
    texto = resp.content[0].text.strip()

    if texto.startswith("```"):
        texto = texto.strip("`")
        if texto.lower().startswith("json"):
            texto = texto[4:]
        texto = texto.strip()

    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        return {"erro_parse": True, "texto_bruto": texto}


def _rt(texto: str) -> dict:
    return {"rich_text": [{"text": {"content": (texto or "")[:2000]}}]}


def montar_properties_atualizacao(analise: dict, page: dict):
    if analise.get("erro_parse"):
        return None

    props = {
        "Name": {"title": [{"text": {"content": (analise.get("nome_curto") or "Conversa de audiência")[:200]}}]},
        "Texte": _rt(analise.get("texte", "")),
        "Dor/Necessidade Identificada": _rt(analise.get("dor_necessidade", "")),
        "Insight de Audiência": _rt(analise.get("insight_audiencia", "")),
        "Ideia de Conteúdo Gerada": _rt(analise.get("ideia_conteudo", "")),
        "Palavras-chave": _rt(analise.get("palavras_chave", "")),
        "STATUS": {"select": {"name": "Analisado"}},
    }

    pilar = analise.get("pilar_sugerido")
    if pilar in PILARES_VALIDOS:
        props["Pilar Sugerido"] = {"select": {"name": pilar}}

    prioridade = analise.get("prioridade")
    if prioridade in PRIORIDADES_VALIDAS:
        props["Prioridade"] = {"select": {"name": prioridade}}

    personas = [p for p in (analise.get("persona") or []) if p in PERSONAS_VALIDAS]
    if personas:
        props["Persona"] = {"multi_select": [{"name": p} for p in personas]}

    data_atual = page["properties"].get("Data da Coleta", {}).get("date")
    if not data_atual:
        props["Data da Coleta"] = {"date": {"start": datetime.now(timezone.utc).strftime("%Y-%m-%d")}}

    return props


def main():
    print("\n=== Enriquecimento de Conversas de Audiência (por entrada) ===\n")

    entradas = buscar_entradas_para_enriquecer()
    if not entradas:
        print("Nenhuma entrada NOVO com 'Enviar para Claude' marcado. Nada para enriquecer.")
        return

    print(f"{len(entradas)} entrada(s) para enriquecer.")

    for page in entradas:
        page_id = page["id"]
        tipo = (page["properties"].get("Tipo", {}).get("select") or {}).get("name", "DM")
        plataforma = (page["properties"].get("PLATAFORMA", {}).get("select") or {}).get("name", "INSTAGRAM")
        urls = _extrair_urls_screenshot(page)

        print(f"  → {page_id} — {len(urls)} imagem(ns) encontrada(s)...")
        if not urls:
            print("    ⚠ Sem screenshot anexado — pulando (mantém NOVO).")
            continue

        analise = analisar_entrada_com_claude(urls, tipo, plataforma)
        props = montar_properties_atualizacao(analise, page)

        if props is None:
            print(f"    ⚠ Claude não retornou JSON válido — mantendo NOVO para nova tentativa. "
                  f"Bruto: {str(analise.get('texto_bruto', ''))[:200]}")
            continue

        try:
            notion.pages.update(page_id=page_id, properties=props)
            print("    ✓ Enriquecida e marcada como Analisado.")
        except APIResponseError as e:
            print(f"    ✖ Erro ao atualizar {page_id}: {e}")

    print("\n=== Enriquecimento concluído ===")


if __name__ == "__main__":
    main()
