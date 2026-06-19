"""
MÓDULO 3 — Coleta Semanal YouTube
Roda toda sexta à noite via GitHub Actions.

Fluxo:
1. Lê entradas CONCORRÊNCIA + YOUTUBE + NOVO do Notion (FICHIERS INSTAGRAM)
2. Para cada entrada, busca dados do canal no YouTube Data API
3. Salva os dados coletados como nova página no Notion (Inteligência)
4. Marca a entrada original como PROCESSADO
"""

import os
import re
import time
from datetime import datetime, timezone
from dotenv import load_dotenv
from notion_client import Client as NotionClient
from notion_client.errors import APIResponseError
from googleapiclient.discovery import build

load_dotenv()

# ── Configuração ──────────────────────────────────────────────────────────────
NOTION_TOKEN             = os.environ.get("NOTION_TOKEN")
YOUTUBE_API_KEY          = os.environ.get("YOUTUBE_API_KEY")
NOTION_DB_ID             = os.environ.get("NOTION_DB_ID")
NOTION_INTELIGENCIA_PAGE_ID = os.environ.get("NOTION_INTELIGENCIA_PAGE_ID")

if not all([NOTION_TOKEN, YOUTUBE_API_KEY, NOTION_DB_ID, NOTION_INTELIGENCIA_PAGE_ID]):
    raise SystemExit("❌ Variável de ambiente ausente. Verifique: NOTION_TOKEN, YOUTUBE_API_KEY, NOTION_DB_ID, NOTION_INTELIGENCIA_PAGE_ID")

notion  = NotionClient(auth=NOTION_TOKEN)
youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)


# ── Validação do alvo Notion ──────────────────────────────────────────────────

def detectar_tipo_alvo(target_id: str) -> str:
    """Detecta se o ID é uma page ou database no Notion."""
    try:
        notion.pages.retrieve(target_id)
        return "page"
    except APIResponseError:
        pass
    try:
        notion.databases.retrieve(target_id)
        return "database"
    except APIResponseError:
        return None

TARGET_TYPE = detectar_tipo_alvo(NOTION_INTELIGENCIA_PAGE_ID)
if not TARGET_TYPE:
    raise SystemExit(
        f"❌ Não foi possível acessar NOTION_INTELIGENCIA_PAGE_ID ({NOTION_INTELIGENCIA_PAGE_ID}).\n"
        "Verifique se a página existe e foi compartilhada com a integração Notion."
    )

MES_ATUAL = datetime.now(timezone.utc).strftime("%B %Y")


# ── Helpers YouTube ───────────────────────────────────────────────────────────

def extrair_channel_id(url: str) -> str | None:
    """Extrai o channel ID de URLs do YouTube (formato /channel/ ou @handle)."""
    # Formato: youtube.com/channel/UCxxxxxx
    match = re.search(r'youtube\.com/channel/(UC[\w-]+)', url)
    if match:
        return match.group(1)

    # Formato: youtube.com/@handle
    match = re.search(r'youtube\.com/@([\w.-]+)', url)
    if match:
        handle = match.group(1)
        resp = youtube.channels().list(part="id", forHandle=handle).execute()
        items = resp.get("items", [])
        return items[0]["id"] if items else None

    return None


def buscar_dados_canal(channel_id: str) -> dict | None:
    """Busca informações e últimos 10 vídeos do canal."""
    # Info do canal
    canal_resp = youtube.channels().list(
        part="snippet,contentDetails,statistics",
        id=channel_id
    ).execute()

    items = canal_resp.get("items", [])
    if not items:
        return None

    canal = items[0]
    uploads_id = canal["contentDetails"]["relatedPlaylists"]["uploads"]
    stats      = canal.get("statistics", {})

    # Últimos 10 vídeos
    playlist_resp = youtube.playlistItems().list(
        part="contentDetails",
        playlistId=uploads_id,
        maxResults=10
    ).execute()

    video_ids = [i["contentDetails"]["videoId"] for i in playlist_resp.get("items", [])]

    videos = []
    if video_ids:
        videos_resp = youtube.videos().list(
            part="snippet,statistics",
            id=",".join(video_ids)
        ).execute()

        for v in videos_resp.get("items", []):
            videos.append({
                "titulo":      v["snippet"]["title"],
                "publicado":   v["snippet"]["publishedAt"][:10],
                "views":       v["statistics"].get("viewCount", "0"),
                "likes":       v["statistics"].get("likeCount", "0"),
                "comentarios": v["statistics"].get("commentCount", "0"),
                "url":         f"https://youtube.com/watch?v={v['id']}"
            })

    return {
        "nome":        canal["snippet"]["title"],
        "inscritos":   stats.get("subscriberCount", "privado"),
        "total_videos": stats.get("videoCount", "0"),
        "videos":      videos
    }


# ── Helpers Notion ────────────────────────────────────────────────────────────

def buscar_entradas_novas() -> list:
    """Retorna entradas CONCORRÊNCIA + YOUTUBE + NOVO do banco Notion."""
    resp = notion.databases.query(
        database_id=NOTION_DB_ID,
        filter={
            "and": [
                {"property": "CATEGORIA",  "select": {"equals": "CONCORRÊNCIA"}},
                {"property": "PLATAFORMA", "select": {"equals": "YOUTUBE"}},
                {"property": "STATUS",     "select": {"equals": "NOVO"}}
            ]
        }
    )
    return resp.get("results", [])


def marcar_processado(page_id: str):
    notion.pages.update(
        page_id=page_id,
        properties={"STATUS": {"select": {"name": "PROCESSADO"}}}
    )


def montar_blocos_coleta(dados: dict, url_origem: str) -> list:
    """Converte os dados do canal em blocos Notion."""
    blocos = [
        {
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [{"text": {"content": f"Canal: {dados['nome']}"}}]}
        },
        {
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"text": {"content":
                f"Inscritos: {dados['inscritos']} | Total de vídeos: {dados['total_videos']}\nFonte: {url_origem}"
            }}]}
        },
        {
            "object": "block", "type": "heading_3",
            "heading_3": {"rich_text": [{"text": {"content": "Últimos vídeos"}}]}
        }
    ]

    for v in dados["videos"]:
        blocos.append({
            "object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [{"text": {"content":
                f"{v['titulo']} ({v['publicado']})\n"
                f"Views: {v['views']} | Likes: {v['likes']} | Comentários: {v['comentarios']}\n"
                f"{v['url']}"
            }}]}
        })

    return blocos


def salvar_coleta_no_notion(dados: dict, url_origem: str, max_retries: int = 3):
    """Cria página de coleta na pasta Inteligência do Notion com retry."""
    titulo = f"COLETA YT — {dados['nome']} — {MES_ATUAL}"

    # Detecta automaticamente se deve usar page_id ou database_id
    parent = (
        {"page_id": NOTION_INTELIGENCIA_PAGE_ID}
        if TARGET_TYPE == "page"
        else {"database_id": NOTION_INTELIGENCIA_PAGE_ID}
    )

    for tentativa in range(1, max_retries + 1):
        try:
            notion.pages.create(
                parent=parent,
                properties={"title": {"title": [{"text": {"content": titulo}}]}},
                children=montar_blocos_coleta(dados, url_origem)
            )
            print(f"  ✓ Salvo: {titulo}")
            return
        except APIResponseError as e:
            print(f"  ⚠ Erro Notion (tentativa {tentativa}/{max_retries}): {e}")
            if tentativa == max_retries:
                print(f"  ✗ Falha ao salvar {titulo}. Continuando para o próximo.")
                return
            time.sleep(2 ** tentativa)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n=== Coleta YouTube — {MES_ATUAL} ===\n")

    entradas = buscar_entradas_novas()

    if not entradas:
        print("Nenhuma entrada nova para coletar.")
        return

    print(f"{len(entradas)} entrada(s) encontrada(s).\n")

    for entrada in entradas:
        page_id = entrada["id"]

        # Extrair URL
        url_prop = entrada["properties"].get("URL", {}).get("url") or ""
        nome_prop = entrada["properties"].get("Name", {}).get("title", [])
        nome = nome_prop[0]["text"]["content"] if nome_prop else page_id

        print(f"Processando: {nome} ({url_prop})")

        if not url_prop:
            print("  ⚠ Sem URL, pulando.")
            marcar_processado(page_id)
            continue

        channel_id = extrair_channel_id(url_prop)
        if not channel_id:
            print(f"  ⚠ Não foi possível extrair channel ID de: {url_prop}")
            marcar_processado(page_id)
            continue

        dados = buscar_dados_canal(channel_id)
        if not dados:
            print(f"  ⚠ Canal não encontrado para ID: {channel_id}")
            marcar_processado(page_id)
            continue

        salvar_coleta_no_notion(dados, url_origem=url_prop)
        marcar_processado(page_id)

    print("\n=== Coleta concluída ===")


if __name__ == "__main__":
    main()
