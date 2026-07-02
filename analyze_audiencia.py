"""
MÓDULO 3 — Análise de Audiência
Roda toda SEXTA-FEIRA às 07:00 (horário de Paris) via GitHub Actions —
antes do script de busca de fontes, para que a busca de conteúdo já
leve em conta os insights de audiência mais recentes.

Fluxo:
1. Busca entradas AUDIÊNCIA + NOVO do Notion (comentários, DMs, stories)
2. Organiza por plataforma (YouTube / Instagram)
3. Envia para Claude, que devolve um JSON estruturado de análise de audiência
4. Salva a análise em duas frentes:
   a) Notion — subpágina legível em "🧠 Inteligência de Audiência"
   b) insights.json — bloco estruturado em "bilans_audiencia", para o
      dashboard (index.html) exibir na aba Detalhes → Bilan Qualitativo
      de Audiência
5. Marca as entradas usadas como PROCESSADO

Variáveis de ambiente esperadas:
  NOTION_TOKEN, ANTHROPIC_API_KEY, NOTION_DB_ID, NOTION_INTELIGENCIA_PAGE_ID
  INSIGHTS_JSON_PATH (opcional — caminho do insights.json no repo, default "insights.json")
"""

import os
import json
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from notion_client import Client as NotionClient
import anthropic

load_dotenv()

# ── Configuração ──────────────────────────────────────────────────────────────
NOTION_TOKEN                = os.environ["NOTION_TOKEN"]
ANTHROPIC_API_KEY           = os.environ["ANTHROPIC_API_KEY"]
NOTION_DB_ID                = os.environ["NOTION_DB_ID"]
NOTION_INTELIGENCIA_PAGE_ID = os.environ["NOTION_INTELIGENCIA_PAGE_ID"]
INSIGHTS_JSON_PATH          = os.environ.get("INSIGHTS_JSON_PATH", "insights.json")

notion = NotionClient(auth=NOTION_TOKEN)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

AGORA        = datetime.now(timezone.utc)
SEMANA_ID    = AGORA.strftime("%G-W%V")       # ex: 2026-W27 (ISO week, estável mesmo virando o mês)
SEMANA_LABEL = AGORA.strftime("%d/%m/%Y")


# ── Busca de dados ────────────────────────────────────────────────────────────

def buscar_entradas_audiencia() -> list:
    """Busca todas as entradas AUDIÊNCIA + NOVO no banco Notion."""
    resp = notion.databases.query(
        database_id=NOTION_DB_ID,
        filter={
            "and": [
                {"property": "CATEGORIA", "select": {"equals": "AUDIÊNCIA"}},
                {"property": "STATUS",    "select": {"equals": "NOVO"}}
            ]
        }
    )

    entradas = []
    for page in resp.get("results", []):
        nome_prop = page["properties"].get("Name", {}).get("title", [])
        nome = nome_prop[0]["text"]["content"] if nome_prop else "sem título"

        texto_prop = page["properties"].get("Texte", {}).get("rich_text", [])
        texto = "".join(rt.get("text", {}).get("content", "") for rt in texto_prop)

        plataforma_prop = page["properties"].get("PLATAFORMA", {}).get("select")
        plataforma = plataforma_prop["name"] if plataforma_prop else "DESCONHECIDA"

        entradas.append({
            "id":         page["id"],
            "nome":       nome,
            "texto":      texto,
            "plataforma": plataforma
        })

    return entradas


def formatar_entradas(entradas: list) -> str:
    """Organiza entradas por plataforma para o prompt."""
    youtube   = [e for e in entradas if e["plataforma"] == "YOUTUBE"]
    instagram = [e for e in entradas if e["plataforma"] == "INSTAGRAM"]
    outro     = [e for e in entradas if e["plataforma"] not in ("YOUTUBE", "INSTAGRAM")]

    partes = []

    if youtube:
        bloco = "### YouTube\n" + "\n\n".join(
            f"**{e['nome']}**\n{e['texto']}" for e in youtube
        )
        partes.append(bloco)

    if instagram:
        bloco = "### Instagram\n" + "\n\n".join(
            f"**{e['nome']}**\n{e['texto']}" for e in instagram
        )
        partes.append(bloco)

    if outro:
        bloco = "### Outros\n" + "\n\n".join(
            f"**{e['nome']}** [{e['plataforma']}]\n{e['texto']}" for e in outro
        )
        partes.append(bloco)

    return "\n\n---\n\n".join(partes) if partes else "Nenhuma entrada de audiência disponível."


# ── Análise Claude ────────────────────────────────────────────────────────────

SCHEMA_JSON = """{
  "perguntas_recorrentes": ["string"],
  "dores_nao_atendidas": ["string"],
  "pedidos_conteudo": ["string"],
  "perfil_momento": "string",
  "diferenca_plataformas": "string",
  "pautas_sugeridas": [{"plataforma": "YOUTUBE|INSTAGRAM", "titulo": "string", "porque": "string"}],
  "temas_audiencia": ["string"],
  "segmentos": {"brasil": 0, "processo": 0, "franca": 0}
}"""


def analisar_com_claude(dados_audiencia: str, total_entradas: int) -> dict:
    prompt = f"""Você é o sistema editorial do canal Por Dentro — canal de uma imigrante brasileira na França que explica como a França realmente funciona: trabalho, saúde, burocracia, moradia, cultura.

Posicionamento: observador, lúcido, educativo. O canal é 75% Instagram hoje e tem crescimento forte nessa plataforma.

Analise os dados de audiência abaixo (comentários, DMs e respostas de stories coletados desde a última rodada) e gere a ANÁLISE DE AUDIÊNCIA da semana de {SEMANA_LABEL}.

## DADOS DA AUDIÊNCIA ({total_entradas} entradas)
{dados_audiencia}

---

Responda APENAS com um JSON válido (sem markdown, sem cercas de código, sem texto fora do JSON) no formato exato abaixo. Use as palavras exatas da audiência sempre que possível. Se não houver dados suficientes para preencher um campo, use lista vazia ou string vazia — nunca invente.

{SCHEMA_JSON}

Onde:
- perguntas_recorrentes: dúvidas que aparecem mais de uma vez — essas viram pauta prioritária
- dores_nao_atendidas: o que a audiência precisa que o canal ainda não respondeu bem ou não cobriu
- pedidos_conteudo: quando alguém pediu diretamente um tema, formato ou continuação
- perfil_momento: quem está engajando agora (recém-chegado, planejando imigrar, já estabelecido) e em que fase de vida
- diferenca_plataformas: o que a audiência do YouTube quer vs. o que a audiência do Instagram quer, se houver diferença relevante
- pautas_sugeridas: 3 ideias concretas de conteúdo com ângulo específico que saem diretamente desses dados
- temas_audiencia: até 6 palavras/temas-chave recorrentes nos dados (vira tag no dashboard)
- segmentos: contagem aproximada de quantas entradas vêm de cada estágio — brasil (ainda no Brasil, pesquisando), processo (em processo de imigração/burocracia), franca (já vivendo na França)"""

    resp = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2500,
        messages=[{"role": "user", "content": prompt}]
    )
    texto = resp.content[0].text.strip()

    # Blindagem: se o modelo insistir em cercar o JSON com ```, remove.
    if texto.startswith("```"):
        texto = texto.strip("`")
        if texto.lower().startswith("json"):
            texto = texto[4:]
        texto = texto.strip()

    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        # Fallback: não perde a análise, só marca que não veio estruturada.
        return {"erro_parse": True, "texto_bruto": texto}


# ── Salvar no Notion (subpágina legível) ─────────────────────────────────────

def _bullets(titulo: str, itens: list) -> list:
    blocos = [{
        "object": "block", "type": "heading_2",
        "heading_2": {"rich_text": [{"text": {"content": titulo}}]}
    }]
    if not itens:
        blocos.append({
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"text": {"content": "— nenhum registro nesta rodada —"}}]}
        })
        return blocos
    for item in itens:
        blocos.append({
            "object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [{"text": {"content": str(item)}}]}
        })
    return blocos


def bilan_para_blocos(bilan: dict) -> list:
    if bilan.get("erro_parse"):
        return [{
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"text": {"content": bilan.get("texto_bruto", "")[:2000]}}]}
        }]

    blocos = []
    blocos += _bullets("Perguntas Mais Recorrentes", bilan.get("perguntas_recorrentes", []))
    blocos += _bullets("Dores Não Atendidas", bilan.get("dores_nao_atendidas", []))
    blocos += _bullets("Pedidos Explícitos de Conteúdo", bilan.get("pedidos_conteudo", []))

    blocos.append({"object": "block", "type": "heading_2",
                    "heading_2": {"rich_text": [{"text": {"content": "Perfil do Momento"}}]}})
    blocos.append({"object": "block", "type": "paragraph",
                    "paragraph": {"rich_text": [{"text": {"content": bilan.get("perfil_momento", "")}}]}})

    blocos.append({"object": "block", "type": "heading_2",
                    "heading_2": {"rich_text": [{"text": {"content": "Diferença Entre Plataformas"}}]}})
    blocos.append({"object": "block", "type": "paragraph",
                    "paragraph": {"rich_text": [{"text": {"content": bilan.get("diferenca_plataformas", "")}}]}})

    blocos.append({"object": "block", "type": "heading_2",
                    "heading_2": {"rich_text": [{"text": {"content": "Pautas Sugeridas"}}]}})
    pautas = bilan.get("pautas_sugeridas", [])
    if not pautas:
        blocos.append({"object": "block", "type": "paragraph",
                        "paragraph": {"rich_text": [{"text": {"content": "— nenhuma pauta sugerida nesta rodada —"}}]}})
    for p in pautas:
        linha = f"[{p.get('plataforma', '')}] {p.get('titulo', '')} — {p.get('porque', '')}"
        blocos.append({"object": "block", "type": "bulleted_list_item",
                        "bulleted_list_item": {"rich_text": [{"text": {"content": linha}}]}})
    return blocos


def salvar_analise_no_notion(bilan: dict):
    titulo = f"ANÁLISE AUDIÊNCIA — Semana de {SEMANA_LABEL}"
    notion.pages.create(
        parent={"page_id": NOTION_INTELIGENCIA_PAGE_ID},
        properties={"title": {"title": [{"text": {"content": titulo}}]}},
        children=bilan_para_blocos(bilan)
    )
    print(f"  ✓ Salvo no Notion: {titulo}")


# ── Salvar no insights.json (alimenta o dashboard) ───────────────────────────

def salvar_bilan_no_insights_json(bilan: dict, total_entradas: int):
    path = Path(INSIGHTS_JSON_PATH)
    if not path.exists():
        print(f"  ⚠ {INSIGHTS_JSON_PATH} não encontrado — pulando atualização do dashboard.")
        return

    if bilan.get("erro_parse"):
        print("  ⚠ Claude não retornou JSON válido — dashboard não atualizado nesta rodada (ver página no Notion).")
        return

    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("bilans_audiencia", [])

    entrada = {
        "id": SEMANA_ID,
        "data": AGORA.strftime("%Y-%m-%d"),
        "periodo_label": f"Semana de {SEMANA_LABEL}",
        "total_entradas_analisadas": total_entradas,
        "perguntas_recorrentes": bilan.get("perguntas_recorrentes", []),
        "dores_nao_atendidas": bilan.get("dores_nao_atendidas", []),
        "pedidos_conteudo": bilan.get("pedidos_conteudo", []),
        "perfil_momento": bilan.get("perfil_momento", ""),
        "diferenca_plataformas": bilan.get("diferenca_plataformas", ""),
        "pautas_sugeridas": bilan.get("pautas_sugeridas", []),
        "temas_audiencia": bilan.get("temas_audiencia", []),
        "segmentos": bilan.get("segmentos", {}),
    }

    # Reprocessar a mesma semana substitui a entrada anterior em vez de duplicar.
    data["bilans_audiencia"] = [b for b in data["bilans_audiencia"] if b.get("id") != SEMANA_ID]
    data["bilans_audiencia"].append(entrada)

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  ✓ Atualizado {INSIGHTS_JSON_PATH} (bilans_audiencia: {len(data['bilans_audiencia'])} rodada(s))")


def marcar_processado(page_id: str):
    notion.pages.update(
        page_id=page_id,
        properties={"STATUS": {"select": {"name": "PROCESSADO"}}}
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n=== Análise de Audiência — Semana de {SEMANA_LABEL} ===\n")

    print("Buscando entradas de audiência...")
    entradas = buscar_entradas_audiencia()

    if not entradas:
        print("Nenhuma entrada de audiência para processar.")
        return

    print(f"{len(entradas)} entrada(s) encontrada(s).")

    dados_formatados = formatar_entradas(entradas)

    print("Enviando para Claude...")
    bilan = analisar_com_claude(dados_formatados, len(entradas))

    print("Salvando no Notion...")
    salvar_analise_no_notion(bilan)

    print("Atualizando insights.json (dashboard)...")
    salvar_bilan_no_insights_json(bilan, len(entradas))

    print("Marcando entradas como processadas...")
    for e in entradas:
        marcar_processado(e["id"])

    print("\n=== Análise de audiência concluída ===")


if __name__ == "__main__":
    main()
