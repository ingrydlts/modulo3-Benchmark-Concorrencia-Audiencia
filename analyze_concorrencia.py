"""
MÓDULO 3 — Análise de Concorrência
Roda na primeira segunda-feira do mês via GitHub Actions.

Fluxo:
1. Busca páginas de COLETA YT salvas no mês atual (pasta Inteligência)
2. Busca entradas CONCORRÊNCIA + INSTAGRAM + NOVO (observações manuais)
3. Envia tudo para Claude que gera o BENCHMARK do mês
4. Salva análise como nova página no Notion
5. Marca entradas Instagram como PROCESSADO
"""

import os
from datetime import datetime, timezone
from dotenv import load_dotenv
from notion_client import Client as NotionClient
import anthropic

load_dotenv()

# ── Configuração ──────────────────────────────────────────────────────────────
NOTION_TOKEN                = os.environ["NOTION_TOKEN"]
ANTHROPIC_API_KEY           = os.environ["ANTHROPIC_API_KEY"]
NOTION_DB_ID                = os.environ["NOTION_DB_ID"]
NOTION_INTELIGENCIA_PAGE_ID = os.environ["NOTION_INTELIGENCIA_PAGE_ID"]

notion = NotionClient(auth=NOTION_TOKEN)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

MES_ATUAL = datetime.now(timezone.utc).strftime("%B %Y")


# ── Busca de dados ────────────────────────────────────────────────────────────

def buscar_coletas_youtube() -> str:
    """Lê as páginas COLETA YT do mês atual na pasta Inteligência."""
    resp = notion.blocks.children.list(block_id=NOTION_INTELIGENCIA_PAGE_ID)
    coletas = []

    for bloco in resp.get("results", []):
        if bloco.get("type") != "child_page":
            continue
        titulo = bloco.get("child_page", {}).get("title", "")
        if "COLETA YT" not in titulo or MES_ATUAL not in titulo:
            continue

        # Lê conteúdo da página de coleta
        conteudo_resp = notion.blocks.children.list(block_id=bloco["id"])
        texto = []
        for b in conteudo_resp.get("results", []):
            tipo = b.get("type", "")
            rich = b.get(tipo, {}).get("rich_text", [])
            linha = "".join(rt.get("text", {}).get("content", "") for rt in rich)
            if linha:
                texto.append(linha)

        coletas.append(f"### {titulo}\n" + "\n".join(texto))

    return "\n\n---\n\n".join(coletas) if coletas else "Nenhuma coleta YouTube disponível este mês."


def buscar_observacoes_instagram() -> tuple[list, str]:
    """Busca entradas manuais de concorrência no Instagram."""
    resp = notion.databases.query(
        database_id=NOTION_DB_ID,
        filter={
            "and": [
                {"property": "CATEGORIA",  "select": {"equals": "CONCORRÊNCIA"}},
                {"property": "PLATAFORMA", "select": {"equals": "INSTAGRAM"}},
                {"property": "STATUS",     "select": {"equals": "NOVO"}}
            ]
        }
    )

    entradas = resp.get("results", [])
    textos = []

    for e in entradas:
        nome_prop = e["properties"].get("Name", {}).get("title", [])
        nome = nome_prop[0]["text"]["content"] if nome_prop else "sem título"

        texto_prop = e["properties"].get("Texte", {}).get("rich_text", [])
        texto = "".join(rt.get("text", {}).get("content", "") for rt in texto_prop)

        textos.append(f"**{nome}**\n{texto}")

    conteudo = "\n\n---\n\n".join(textos) if textos else "Nenhuma observação Instagram depositada."
    return entradas, conteudo


# ── Análise Claude ────────────────────────────────────────────────────────────

def analisar_com_claude(youtube_data: str, instagram_data: str) -> str:
    prompt = f"""Você é o sistema editorial do canal Por Dentro — canal de uma imigrante brasileira na França que explica como a França realmente funciona: trabalho, saúde, burocracia, moradia, cultura.

Posicionamento: observador, lúcido, educativo. Nunca romantiza nem catastrofiza. Nunca clickbait no corpo do conteúdo.

Analise os dados de concorrência abaixo e gere o BENCHMARK CONCORRÊNCIA de {MES_ATUAL}.

## DADOS YOUTUBE (coletados automaticamente das últimas semanas)
{youtube_data}

## OBSERVAÇÕES INSTAGRAM (depositadas manualmente)
{instagram_data}

---

Gere o output na estrutura abaixo. Seja específico, use dados concretos, posicione tudo para o Por Dentro.

## O QUE ESTÁ FUNCIONANDO NO NICHO
3 tendências com exemplos concretos dos dados acima.

## SINAIS DE ALGORITMO DO PERÍODO
Padrões de títulos, thumbnails, frequência e formato que aparecem nos dados.

## LACUNAS QUE O POR DENTRO PODE OCUPAR
3 oportunidades específicas que nenhum concorrente está cobrindo bem agora.

## O QUE NÃO FAZER
Temas saturados ou formatos que não fazem sentido para o posicionamento do Por Dentro.

## RECOMENDAÇÃO EDITORIAL DO MÊS
Uma decisão clara e acionável para o próximo calendário editorial."""

    resp = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text


# ── Salvar no Notion ──────────────────────────────────────────────────────────

def texto_para_blocos(texto: str) -> list:
    """Converte texto com ## headings em blocos Notion."""
    blocos = []
    for linha in texto.split("\n"):
        linha = linha.strip()
        if not linha:
            continue
        if linha.startswith("## "):
            blocos.append({
                "object": "block", "type": "heading_2",
                "heading_2": {"rich_text": [{"text": {"content": linha[3:]}}]}
            })
        elif linha.startswith("### "):
            blocos.append({
                "object": "block", "type": "heading_3",
                "heading_3": {"rich_text": [{"text": {"content": linha[4:]}}]}
            })
        else:
            blocos.append({
                "object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [{"text": {"content": linha}}]}
            })
    return blocos


def salvar_analise_no_notion(analise: str):
    titulo = f"BENCHMARK CONCORRÊNCIA — {MES_ATUAL}"
    notion.pages.create(
        parent={"page_id": NOTION_INTELIGENCIA_PAGE_ID},
        properties={"title": {"title": [{"text": {"content": titulo}}]}},
        children=texto_para_blocos(analise)
    )
    print(f"  ✓ Salvo: {titulo}")


def marcar_processado(page_id: str):
    notion.pages.update(
        page_id=page_id,
        properties={"STATUS": {"select": {"name": "PROCESSADO"}}}
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n=== Análise de Concorrência — {MES_ATUAL} ===\n")

    print("Buscando coletas YouTube do mês...")
    youtube_data = buscar_coletas_youtube()

    print("Buscando observações Instagram...")
    entradas_ig, instagram_data = buscar_observacoes_instagram()

    print("Enviando para Claude...")
    analise = analisar_com_claude(youtube_data, instagram_data)

    print("Salvando no Notion...")
    salvar_analise_no_notion(analise)

    print("Marcando entradas como processadas...")
    for e in entradas_ig:
        marcar_processado(e["id"])

    print("\n=== Análise de concorrência concluída ===")


if __name__ == "__main__":
    main()
