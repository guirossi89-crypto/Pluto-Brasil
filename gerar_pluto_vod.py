#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gerador de lista M3U com o conteúdo sob demanda (VOD) do Pluto TV Brasil.
Coleta Filmes e Séries, monta URLs HLS diretas (com JWT) reproduzíveis em
VLC / SS IPTV / players compatíveis.

Organização: itens separados por GÊNERO / categoria Pluto.
  - Filmes:  group-title = "Filmes • <Categoria>"
  - Séries:  group-title = "Séries • <Categoria> • <Nome da série>"

Saída: playlists/pluto_vod.m3u

Uso:
  python gerar_pluto_vod.py                  # padrão (Brasil)
  python gerar_pluto_vod.py --workers 16     # mais paralelismo
  python gerar_pluto_vod.py --movies-only    # só filmes
  python gerar_pluto_vod.py --series-only    # só séries
  python gerar_pluto_vod.py --output outra.m3u
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import ssl
import sys
import time
import uuid
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
BOOT_URL = "https://boot.pluto.tv/v4/start"
VOD_BASE = "https://service-vod.clusters.pluto.tv"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
# IP brasileiro conhecido (usado no X-Forwarded-For para obter catálogo BR)
BR_IP = "177.47.27.205"

DEFAULT_WORKERS = 12
TIMEOUT = 40
APP_VERSION = "5.106.0"

log = logging.getLogger("pluto_vod")

# SSL permissivo (alguns ambientes GitHub Actions falham com verificação estrita)
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


# ---------------------------------------------------------------------------
# Utilitários
# ---------------------------------------------------------------------------
def clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").replace(",", " ").strip())


def ms_to_seconds(ms: Any) -> int:
    try:
        v = int(float(ms))
        if v > 100_000:  # provavelmente milissegundos
            return max(1, v // 1000)
        if v > 0:
            return v
    except (TypeError, ValueError):
        pass
    return -1


def pick_logo(item: dict) -> str:
    """Escolhe a melhor URL de capa (preferência 16:9 ou poster)."""
    covers = item.get("covers") or []
    if isinstance(covers, list):
        # Preferir 16:9
        for c in covers:
            if isinstance(c, dict) and c.get("aspectRatio") in ("16:9", "16x9"):
                url = c.get("url")
                if url:
                    return url.split("?")[0]  # remove query de resize
        for c in covers:
            if isinstance(c, dict) and c.get("url"):
                return c["url"].split("?")[0]
    # Fallbacks
    for key in ("poster16_9", "featuredImage", "tile", "poster"):
        val = item.get(key)
        if isinstance(val, dict) and val.get("path"):
            return val["path"]
        if isinstance(val, str) and val.startswith("http"):
            return val
    return ""


def normalize_category(name: str | None) -> str:
    if not name:
        return "Outros"
    return clean(name)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def http_get(url: str, headers: dict, retries: int = 3) -> bytes:
    last: Exception | None = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, context=CTX, timeout=TIMEOUT) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.0 + i * 1.5)
    assert last is not None
    raise last


def boot_session() -> dict:
    """Obtém sessionToken + stitcher + params (região BR via X-Forwarded-For)."""
    client_id = str(uuid.uuid4())
    qs = urllib.parse.urlencode(
        {
            "appName": "web",
            "appVersion": APP_VERSION,
            "clientID": client_id,
            "clientModelNumber": "na",
            "deviceMake": "Chrome",
            "deviceModel": "Chrome",
            "deviceType": "web",
            "deviceVersion": "120.0.0",
            "deviceDNT": "false",
            "serverSideAds": "false",
            "drmCapabilities": "",
        }
    )
    url = f"{BOOT_URL}?{qs}"
    headers = {
        "User-Agent": UA,
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "X-Forwarded-For": BR_IP,
        "Origin": "https://pluto.tv",
        "Referer": "https://pluto.tv/br/",
    }
    data = json.loads(http_get(url, headers))
    token = data.get("sessionToken")
    if not token:
        raise RuntimeError("sessionToken não encontrado no boot do Pluto TV")
    servers = data.get("servers") or {}
    stitcher = servers.get("stitcher") or "https://cfd-v4-service-channel-stitcher-use1-1.prd.pluto.tv"
    params = data.get("stitcherParams") or ""
    country = (data.get("session") or {}).get("countryCode") or (data.get("session") or {}).get("marketingRegion")
    log.info("Boot OK — country=%s stitcher=%s", country or "?", stitcher)
    return {
        "token": token,
        "stitcher": stitcher,
        "params": params,
        "client_id": client_id,
    }


def api_headers(token: str) -> dict:
    return {
        "User-Agent": UA,
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "Origin": "https://pluto.tv",
        "Referer": "https://pluto.tv/br/",
        "X-Forwarded-For": BR_IP,
    }


# ---------------------------------------------------------------------------
# Catálogo
# ---------------------------------------------------------------------------
def fetch_categories(token: str, include_items: bool = True) -> list[dict]:
    """Busca todas as categorias VOD (paginadas)."""
    out: list[dict] = []
    page = 1
    while True:
        qs = urllib.parse.urlencode(
            {
                "includeItems": "true" if include_items else "false",
                "deviceType": "web",
                "offset": 0,
                "page": page,
                "sort": "number:asc",
            }
        )
        url = f"{VOD_BASE}/v4/vod/categories?{qs}"
        try:
            data = json.loads(http_get(url, api_headers(token)))
        except Exception as e:  # noqa: BLE001
            log.warning("erro ao buscar categorias page=%s: %s", page, e)
            break
        cats = data.get("categories") or []
        out.extend(cats)
        total_pages = int(data.get("totalPages") or 1)
        log.info("Categorias page %s/%s → %s categorias", page, total_pages, len(cats))
        if page >= total_pages or not cats:
            break
        page += 1
        time.sleep(0.15)
    return out


def fetch_series_seasons(series_id: str, token: str) -> list[dict]:
    """Retorna lista de temporadas (com episódios) de uma série."""
    # v3 costuma ser mais estável para seasons com items
    for ver in ("v3", "v4"):
        url = (
            f"{VOD_BASE}/{ver}/vod/series/{series_id}/seasons"
            f"?includeItems=true&deviceType=web"
        )
        try:
            data = json.loads(http_get(url, api_headers(token)))
            seasons = data.get("seasons") or []
            if seasons:
                return seasons
        except Exception as e:  # noqa: BLE001
            log.debug("seasons %s %s: %s", ver, series_id, e)
    return []


def build_stream_url(session: dict, stitched_path: str) -> str:
    """Monta URL HLS completa com JWT."""
    if not stitched_path:
        return ""
    path = stitched_path if stitched_path.startswith("/") else f"/{stitched_path}"
    # path costuma ser /stitch/hls/episode/<id>/master.m3u8
    base = session["stitcher"].rstrip("/")
    params = session["params"]
    token = session["token"]
    return (
        f"{base}/v2{path}"
        f"?{params}&jwt={token}&masterJWTPassthrough=true"
    )


# ---------------------------------------------------------------------------
# Coleta de itens
# ---------------------------------------------------------------------------
def collect_movies_from_categories(
    categories: list[dict],
    session: dict,
) -> list[dict]:
    """Extrai filmes únicos das categorias."""
    seen: set[str] = set()
    items: list[dict] = []
    for cat in categories:
        cat_name = normalize_category(cat.get("name"))
        for it in cat.get("items") or []:
            if (it.get("type") or "").lower() != "movie":
                continue
            mid = it.get("_id") or it.get("id")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            stitched = (it.get("stitched") or {}).get("path") or ""
            if not stitched:
                # fallback comum
                stitched = f"/stitch/hls/episode/{mid}/master.m3u8"
            stream = build_stream_url(session, stitched)
            if not stream:
                continue
            duration = ms_to_seconds(it.get("duration") or it.get("originalContentDuration"))
            logo = pick_logo(it)
            year = ""
            # tenta extrair ano do slug ou nome
            slug = it.get("slug") or ""
            m = re.search(r"(19|20)\d{2}", slug)
            if m:
                year = m.group(0)
            title = clean(it.get("name"))
            if year and year not in title:
                title = f"{title} ({year})"
            items.append(
                {
                    "kind": "movie",
                    "id": mid,
                    "title": title,
                    "group": f"Filmes • {cat_name}",
                    "logo": logo,
                    "duration": duration,
                    "stream": stream,
                    "genre": clean(it.get("genre") or cat_name),
                }
            )
    return items


def collect_series_from_categories(
    categories: list[dict],
    session: dict,
    workers: int,
) -> list[dict]:
    """Extrai séries e busca temporadas/episódios em paralelo."""
    series_map: dict[str, dict] = {}  # id -> {name, category, genre, ...}
    for cat in categories:
        cat_name = normalize_category(cat.get("name"))
        for it in cat.get("items") or []:
            if (it.get("type") or "").lower() != "series":
                continue
            sid = it.get("seriesID") or it.get("_id") or it.get("id")
            if not sid or sid in series_map:
                continue
            series_map[sid] = {
                "id": sid,
                "name": clean(it.get("name")),
                "category": cat_name,
                "genre": clean(it.get("genre") or cat_name),
                "logo": pick_logo(it),
            }

    log.info("Séries únicas encontradas: %s", len(series_map))
    items: list[dict] = []
    token = session["token"]

    def process_one(sid: str, meta: dict) -> list[dict]:
        out: list[dict] = []
        seasons = fetch_series_seasons(sid, token)
        for season in seasons:
            season_num = season.get("number")
            try:
                season_num = int(season_num) if season_num is not None else None
            except (TypeError, ValueError):
                season_num = None
            episodes = season.get("episodes") or season.get("items") or []
            for ep in episodes:
                eid = ep.get("_id") or ep.get("id")
                if not eid:
                    continue
                stitched = (ep.get("stitched") or {}).get("path") or ""
                if not stitched:
                    stitched = f"/stitch/hls/episode/{eid}/master.m3u8"
                stream = build_stream_url(session, stitched)
                if not stream:
                    continue
                duration = ms_to_seconds(ep.get("duration") or ep.get("originalContentDuration"))
                ep_name = clean(ep.get("name") or ep.get("title") or "")
                ep_num = ep.get("number")
                try:
                    ep_num = int(ep_num) if ep_num is not None else None
                except (TypeError, ValueError):
                    ep_num = None

                show = meta["name"]
                if season_num is not None and ep_num is not None:
                    tag = f"S{season_num:02d}E{ep_num:02d}"
                    if ep_name and ep_name.lower() != show.lower():
                        title = f"{show} {tag} - {ep_name}"
                    else:
                        title = f"{show} {tag}"
                elif ep_num is not None:
                    title = f"{show} E{ep_num:02d}" + (f" - {ep_name}" if ep_name else "")
                else:
                    title = f"{show} - {ep_name}" if ep_name else show

                logo = pick_logo(ep) or meta["logo"]
                group = f"Séries • {meta['category']} • {show}"
                out.append(
                    {
                        "kind": "episode",
                        "id": eid,
                        "title": title,
                        "group": group,
                        "logo": logo,
                        "duration": duration,
                        "stream": stream,
                        "genre": meta["genre"],
                        "series": show,
                    }
                )
        return out

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(process_one, sid, meta): sid
            for sid, meta in series_map.items()
        }
        done = 0
        for fut in as_completed(futures):
            done += 1
            try:
                items.extend(fut.result())
            except Exception as e:  # noqa: BLE001
                log.warning("erro série %s: %s", futures[fut], e)
            if done % 20 == 0 or done == len(futures):
                log.info("Séries processadas: %s/%s (episódios até agora: %s)", done, len(futures), len(items))
    return items


# ---------------------------------------------------------------------------
# Escrita M3U
# ---------------------------------------------------------------------------
def write_m3u(path: Path, entries: list[dict]) -> None:
    """Escreve lista M3U otimizada para SS IPTV (VOD com duração + content-type)."""
    # Ordena: filmes por grupo+título, séries por grupo+título
    entries = sorted(
        entries,
        key=lambda e: (0 if e["kind"] == "movie" else 1, e["group"].lower(), e["title"].lower()),
    )

    lines: list[str] = [
        "#EXTM3U",
        "#PLAYLIST:Pluto TV Brasil VOD",
        f"#EXTINF:-1,Atualizado em {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}",
        "https://pluto.tv/br/",
    ]

    for e in entries:
        dur = e.get("duration") or -1
        logo = e.get("logo") or ""
        group = e.get("group") or "Outros"
        title = e.get("title") or "Sem título"
        # content-type="video" ajuda o SS IPTV a tratar como VOD (barra de progresso)
        attrs = [
            f'tvg-name="{title}"',
            f'tvg-logo="{logo}"' if logo else "",
            f'group-title="{group}"',
            'content-type="video"',
        ]
        attr_str = " ".join(a for a in attrs if a)
        lines.append(f"#EXTINF:{dur} {attr_str},{title}")
        lines.append(e["stream"])

    path.parent.mkdir(parents=True, exist_ok=True)
    # backup
    if path.exists():
        bak = path.with_suffix(path.suffix + ".bak")
        try:
            path.replace(bak)
        except OSError:
            pass
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("Lista escrita: %s (%s itens)", path, len(entries))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Gera lista M3U VOD do Pluto TV Brasil")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="Threads paralelas")
    parser.add_argument("--movies-only", action="store_true", help="Apenas filmes")
    parser.add_argument("--series-only", action="store_true", help="Apenas séries")
    parser.add_argument("-o", "--output", type=Path, default=Path("playlists/pluto_vod.m3u"))
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    t0 = time.time()
    log.info("Iniciando coleta Pluto TV Brasil…")

    session = boot_session()
    categories = fetch_categories(session["token"], include_items=True)
    log.info("Total de categorias: %s", len(categories))

    entries: list[dict] = []

    if not args.series_only:
        movies = collect_movies_from_categories(categories, session)
        log.info("Filmes coletados: %s", len(movies))
        entries.extend(movies)

    if not args.movies_only:
        series_eps = collect_series_from_categories(categories, session, args.workers)
        log.info("Episódios coletados: %s", len(series_eps))
        entries.extend(series_eps)

    if not entries:
        log.error("Nenhum item coletado. Abortando.")
        return 1

    write_m3u(args.output, entries)
    elapsed = time.time() - t0
    log.info(
        "Concluído em %.1fs — %s itens (%s filmes / %s episódios)",
        elapsed,
        len(entries),
        sum(1 for e in entries if e["kind"] == "movie"),
        sum(1 for e in entries if e["kind"] == "episode"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
