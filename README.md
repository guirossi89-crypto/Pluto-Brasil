# Lista M3U VOD do Pluto TV Brasil (atualização automática)

Este projeto gera uma lista **M3U** com o **conteúdo sob demanda (VOD)** do
[Pluto TV Brasil](https://pluto.tv/br/) — Filmes e Séries — com o **nome de cada
conteúdo**, pronta para usar no **SS IPTV**, **VLC** e players compatíveis.

O GitHub Actions regenera a lista **a cada 6 horas**, automaticamente.

## O que a lista contém

- Cada item traz o nome do conteúdo (`tvg-name` e título) e a capa (`tvg-logo`).
- **Ano** entre parênteses nos filmes (quando detectável no slug).
- **Episódios** com formato `Série S01E05 - Título`.
- Agrupamento por `group-title`:
  - `Filmes • <Categoria Pluto>` — ex.: `Filmes • Cine Ação`, `Filmes • Cine Drama`…
  - `Séries • <Categoria> • <Nome da série>` — cada série agrupada.
- Links de vídeo HLS diretos (stitcher Pluto + JWT), que o próprio player abre.

## Retomar de onde parou (barra de progresso no SS IPTV)

A lista:

1. Preenche o `#EXTINF` com a **duração real** de cada título em segundos
   (ex.: `#EXTINF:6900 ...`), em vez de `-1`.
2. Marca cada item com **`content-type="video"`** (parâmetro do SS IPTV para
   "isto é um VÍDEO sob demanda, não um canal ao vivo").

Com os dois ajustes, o SS IPTV trata o item como **VOD**: aparece a barra de
progresso, dá para avançar/retroceder e ele guarda a posição para
**continuar de onde parou** (igual no VLC).

## Região Brasil

O script força o catálogo brasileiro via:

- Header `X-Forwarded-For` com IP brasileiro
- `Accept-Language: pt-BR`
- Referer `https://pluto.tv/br/`

Isso faz a API devolver categorias e títulos em português (ex.: "Cine Ação",
"Novelas", etc.). Funciona tanto em máquina local quanto no GitHub Actions.

> **Nota:** o JWT do Pluto expira em algumas horas. Por isso a lista é
> regenerada automaticamente a cada 6 horas — os links ficam sempre válidos.

## Opções da linha de comando

```bash
python gerar_pluto_vod.py [opções]

  --workers N        Threads paralelas (padrão: 12)
  --movies-only      Apenas filmes
  --series-only      Apenas séries
  -o, --output PATH  Arquivo M3U de saída
  -v, --verbose      Log detalhado
```

## Como publicar no GitHub

1. Crie um repositório novo (pode ser público) no GitHub.
2. Envie todos os arquivos desta pasta para o repositório, mantendo a estrutura:
   ```
   gerar_pluto_vod.py
   playlists/pluto_vod.m3u
   .github/workflows/update.yml
   README.md
   ```
3. No GitHub, abra **Settings > Actions > General > Workflow permissions** e
   marque **Read and write permissions**.
4. Pronto. O workflow roda sozinho a cada 6 horas. Para rodar na hora:
   **Actions > Atualizar lista VOD Pluto TV > Run workflow**.

## URL para usar no SS IPTV

Depois de enviar ao GitHub, use o link **raw** da lista (troque `USUARIO` e
`REPOSITORIO`):

```
https://raw.githubusercontent.com/USUARIO/REPOSITORIO/main/playlists/pluto_vod.m3u
```

No **SS IPTV**: Configurações > Listas de reprodução > Adicionar > cole a URL
externa acima > Salvar.

## Rodar manualmente no PC

```bash
python gerar_pluto_vod.py
```

Gera/atualiza `playlists/pluto_vod.m3u`. Faz backup automático da lista
anterior (`.m3u.bak`).

Requisitos: apenas **Python 3.9+** (usa somente bibliotecas padrão — sem pip install).

## Observações importantes

- Os streams do Pluto TV incluem **anúncios** (é o modelo de negócio gratuito).
- O JWT embutido nas URLs expira; por isso a atualização automática a cada 6h
  é essencial.
- Conteúdo disponível depende da região (Brasil). Em outros países o catálogo
  muda.
- Uso pessoal / educacional. Todo o conteúdo é propriedade do Pluto TV /
  Paramount.
