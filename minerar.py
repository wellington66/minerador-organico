"""Minerador orgânico: acha Shorts virais (outliers) por nicho no YouTube,
analisa o gancho com Gemini (assiste o vídeo pelo link) e os comentários
(dores, correções, dúvidas, desejos, objeções, frases exatas).

Variáveis: YOUTUBE_API_KEY, GEMINI_API_KEY (obrigatórias)
Opcionais: BUSCAS_POR_RODADA (6), MAX_ANALISES (8), MIN_VIEWS (100000), MIN_OUTLIER (3)
"""
import json, math, os, sys, time, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

RAIZ = os.path.dirname(os.path.abspath(__file__))
DADOS = os.path.join(RAIZ, "data")
YT = os.environ.get("YOUTUBE_API_KEY", "").strip()
GEM = os.environ.get("GEMINI_API_KEY", "").strip()
BUSCAS = int(os.environ.get("BUSCAS_POR_RODADA", "6"))
MAX_ANALISES = int(os.environ.get("MAX_ANALISES", "8"))
MIN_VIEWS = int(os.environ.get("MIN_VIEWS", "100000"))
MIN_OUTLIER = float(os.environ.get("MIN_OUTLIER", "3"))
MODELO_VIDEO = os.environ.get("MODELO_VIDEO", "gemini-flash-latest")
MODELO_TEXTO = os.environ.get("MODELO_TEXTO", "gemini-flash-lite-latest")
MODELO_RESERVA = os.environ.get("MODELO_RESERVA", "gemini-flash-lite-latest")


def ler(nome, padrao):
    try:
        with open(os.path.join(DADOS, nome), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return padrao


def salvar(nome, obj):
    os.makedirs(DADOS, exist_ok=True)
    tmp = os.path.join(DADOS, nome + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(DADOS, nome))


def http_json(url, corpo=None, tentativas=3, cabecalhos=None):
    for i in range(tentativas):
        req = urllib.request.Request(url, data=json.dumps(corpo).encode() if corpo else None,
                                     headers={"Content-Type": "application/json", **(cabecalhos or {})})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            txt = e.read().decode(errors="ignore")[:300]
            if e.code in (429, 500, 503) and i < tentativas - 1:
                time.sleep(20 * (i + 1))
                continue
            raise RuntimeError(f"HTTP {e.code}: {txt}")
        except (urllib.error.URLError, TimeoutError):
            if i == tentativas - 1:
                raise
            time.sleep(10)


def yt(recurso, **params):
    params["key"] = YT
    return http_json(f"https://www.googleapis.com/youtube/v3/{recurso}?" + urllib.parse.urlencode(params))


def duracao_seg(iso):
    # PT1M5S -> 65
    n, total, t = "", 0, iso.replace("PT", "").replace("P", "")
    for c in t:
        if c.isdigit():
            n += c
        else:
            total += int(n or 0) * {"H": 3600, "M": 60, "S": 1}.get(c, 0)
            n = ""
    return total


# ---------------------------------------------------------------- mineração
def proximas_buscas(nichos, estado):
    # ordem intercalada: 1º termo de cada nicho, depois o 2º de cada nicho...
    maior = max(len(v["termos"]) for v in nichos.values())
    pares = [(n, v["termos"][k]) for k in range(maior) for n, v in nichos.items() if k < len(v["termos"])]
    i = estado.get("cursor", 0) % len(pares)
    escolhidos = [pares[(i + k) % len(pares)] for k in range(min(BUSCAS, len(pares)))]
    estado["cursor"] = i + len(escolhidos)
    return escolhidos


def minerar(nichos, videos, estado, canais_cache):
    depois = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    novos = 0
    for nicho, termo in proximas_buscas(nichos, estado):
        try:
            r = yt("search", part="id", q=termo + " #shorts", type="video", videoDuration="short",
                   order="viewCount", publishedAfter=depois, regionCode="BR", relevanceLanguage="pt",
                   maxResults=50)
        except RuntimeError as e:
            print("busca falhou:", termo, e)
            if "quota" in str(e).lower():
                break
            continue
        ids = [it["id"]["videoId"] for it in r.get("items", []) if it["id"].get("videoId")]
        if not ids:
            continue
        det = yt("videos", part="snippet,statistics,contentDetails", id=",".join(ids))
        itens = [v for v in det.get("items", [])
                 if duracao_seg(v["contentDetails"]["duration"]) <= 180 and relevante(v, termo)]
        faltam = list({v["snippet"]["channelId"] for v in itens} - set(canais_cache))
        for k in range(0, len(faltam), 50):
            for c in yt("channels", part="statistics,snippet", id=",".join(faltam[k:k + 50])).get("items", []):
                s = c["statistics"]
                canais_cache[c["id"]] = {
                    "nome": c["snippet"]["title"],
                    "inscritos": int(s.get("subscriberCount", 0) or 0),
                    "media": int(s.get("viewCount", 0) or 0) / max(int(s.get("videoCount", 1) or 1), 1),
                }
        for v in itens:
            st, sn = v["statistics"], v["snippet"]
            views = int(st.get("viewCount", 0) or 0)
            canal = canais_cache.get(sn["channelId"], {"media": 0, "inscritos": 0, "nome": sn["channelTitle"]})
            outlier = views / max(canal["media"], 1000)
            reg = videos.get(v["id"])
            if reg:  # atualiza métricas
                reg.update(views=views, likes=int(st.get("likeCount", 0) or 0),
                           comentarios=int(st.get("commentCount", 0) or 0), outlier=round(outlier, 1))
                if nicho not in reg["nichos"]:
                    reg["nichos"].append(nicho)
                continue
            if views < MIN_VIEWS or (outlier < MIN_OUTLIER and views < 1_000_000):
                continue
            videos[v["id"]] = {
                "id": v["id"], "titulo": sn["title"], "canal": canal["nome"], "canal_id": sn["channelId"],
                "inscritos": canal["inscritos"], "publicado": sn["publishedAt"],
                "duracao": duracao_seg(v["contentDetails"]["duration"]),
                "views": views, "likes": int(st.get("likeCount", 0) or 0),
                "comentarios": int(st.get("commentCount", 0) or 0),
                "outlier": round(outlier, 1), "nichos": [nicho], "termo": termo,
                "achado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "gancho": None, "dores": None,
            }
            novos += 1
    print(f"mineração: {novos} vídeos virais novos (total {len(videos)})")


PALAVRAS_FRACAS = {"para", "como", "com", "sem", "dicas", "dica", "fácil", "rápido", "casa", "que", "você"}


def relevante(v, termo):
    sn = v["snippet"]
    lang = (sn.get("defaultAudioLanguage") or sn.get("defaultLanguage") or "").lower()
    if lang and not lang.startswith("pt"):
        return False
    texto = " ".join([sn.get("title", ""), sn.get("description", ""), " ".join(sn.get("tags", []))]).lower()
    chaves = [p for p in termo.lower().split() if len(p) > 3 and p not in PALAVRAS_FRACAS] or termo.lower().split()
    return any(p[:max(4, len(p) - 3)] in texto for p in chaves)  # radical: tolera plural/gênero


def pontuar(v):
    return min(v["outlier"], 50) * math.log10(max(v["views"], 10))


# ---------------------------------------------------------------- análise
ESGOTADO = set()


def gemini(modelo, partes):
    # tenta o modelo pedido; se estiver sobrecarregado ou o JSON vier quebrado, tenta de novo / cai pro reserva
    global ESGOTADO
    ultimo = None
    for m in [modelo, modelo, MODELO_RESERVA]:
        if m in ESGOTADO:
            continue
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
        try:
            r = http_json(url, cabecalhos={"x-goog-api-key": GEM}, tentativas=2, corpo={
                "contents": [{"parts": partes}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4,
                                     "mediaResolution": "MEDIA_RESOLUTION_LOW"}})
            return json.loads(r["candidates"][0]["content"]["parts"][0]["text"])
        except (RuntimeError, json.JSONDecodeError, KeyError) as e:
            ultimo = e
            if "429" in str(e):
                ESGOTADO.add(m)  # cota do dia acabou nesse modelo: segue com o reserva
                continue
            time.sleep(5)
    raise ultimo


PROMPT_GANCHO = """Você é um estrategista de conteúdo viral. Assista este Short (nicho: {nicho}).
Título: {titulo} | {views} views | {outlier}x a média do canal.
Foque nos 3 PRIMEIROS SEGUNDOS (a chamada de entrada) e responda em JSON, em português do Brasil:
{{
 "gancho_falado": "frase exata falada no início (ou '' se não houver fala)",
 "texto_tela": "texto escrito na tela no início (ou '')",
 "visual_inicial": "o que aparece no primeiro frame/segundo",
 "tipo_gancho": "um de: curiosidade, erro-comum, pare-de, resultado-chocante, antes-depois, segredo, lista, desafio, polemica, identificacao, pergunta, tutorial-direto, historia, autoridade, humor",
 "formato": "ex: talking head, mãos fazendo, POV, tela dividida, narração+b-roll, slideshow",
 "emocao": "emoção principal que prende",
 "retencao": "o que segura a pessoa até o fim (loop, promessa adiada, etc.)",
 "cta": "chamada final se houver",
 "por_que_funciona": "1-2 frases objetivas",
 "modelo_gancho": "o gancho como template reutilizável com [lacunas]",
 "variacoes": ["3 ganchos novos no mesmo padrão para outros produtos/nichos"]
}}"""

PROMPT_COMENT = """Analise os comentários deste Short viral (nicho: {nicho}, título: "{titulo}").
Extraia o que o PÚBLICO revela. Use as palavras deles. Responda em JSON (português do Brasil):
{{
 "dores": ["problemas/sofrimentos que as pessoas contam"],
 "correcoes": ["pessoas corrigindo ou discordando do criador / do método"],
 "duvidas": ["perguntas que fazem"],
 "desejos": ["o que querem conseguir / pedem como próximo conteúdo ou produto"],
 "objecoes": ["descrenças, 'não funciona', 'muito caro', 'sem tempo'..."],
 "elogios": ["o que mais elogiam (o que funcionou no vídeo)"],
 "frases_exatas": ["até 8 frases literais curtas e fortes para usar em copy"],
 "ideias_produto": ["oportunidades de infoproduto/oferta que esses comentários sugerem"]
}}
Cada lista com no máximo 6 itens, sem repetir, ordem de importância (mais curtidos/recorrentes primeiro).
Se não houver nada numa categoria, devolva lista vazia.

COMENTÁRIOS (curtidas | texto):
{comentarios}"""


def puxar_comentarios(vid):
    try:
        r = yt("commentThreads", part="snippet", videoId=vid, order="relevance", maxResults=100,
               textFormat="plainText")
    except RuntimeError as e:
        print("  sem comentários:", vid, str(e)[:80])
        return []
    out = []
    for it in r.get("items", []):
        s = it["snippet"]["topLevelComment"]["snippet"]
        out.append({"t": s["textDisplay"][:300].replace("\n", " "), "l": int(s.get("likeCount", 0))})
    out.sort(key=lambda c: -c["l"])
    return out[:80]


def analisar(videos, nichos):
    fila = sorted([v for v in videos.values() if v["gancho"] is None or v["dores"] is None],
                  key=pontuar, reverse=True)[:MAX_ANALISES]
    feitos = 0
    for v in fila:
        nicho = nichos.get(v["nichos"][0], {}).get("nome", v["nichos"][0])
        try:
            if v["gancho"] is None:
                v["gancho"] = gemini(MODELO_VIDEO, [
                    {"file_data": {"file_uri": f"https://www.youtube.com/watch?v={v['id']}"},
                     "video_metadata": {"end_offset": "25s"}},
                    {"text": PROMPT_GANCHO.format(nicho=nicho, titulo=v["titulo"], views=v["views"],
                                                  outlier=v["outlier"])}])
            if v["dores"] is None:
                coms = puxar_comentarios(v["id"])
                v["top_comentarios"] = coms[:12]
                if len(coms) < 5:
                    v["dores"] = {}
                else:
                    txt = "\n".join(f"{c['l']} | {c['t']}" for c in coms)
                    v["dores"] = gemini(MODELO_TEXTO, [{"text": PROMPT_COMENT.format(
                        nicho=nicho, titulo=v["titulo"], comentarios=txt)}])
            v["analisado"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            feitos += 1
            print(f"  ok {v['id']} {v['outlier']}x {v['titulo'][:50]}")
        except Exception as e:  # cota/erro: tenta na próxima rodada
            print("  análise falhou:", v["id"], str(e)[:200])
            if MODELO_RESERVA in ESGOTADO:
                break
        time.sleep(4)
    print(f"análise: {feitos} vídeos")


def main():
    if not YT or not GEM:
        sys.exit("Faltam YOUTUBE_API_KEY e/ou GEMINI_API_KEY")
    with open(os.path.join(RAIZ, "nichos.json"), encoding="utf-8") as f:
        nichos = json.load(f)
    videos = ler("videos.json", {})
    estado = ler("estado.json", {})
    canais = ler("canais.json", {})
    try:
        minerar(nichos, videos, estado, canais)
    finally:
        salvar("videos.json", videos)
        salvar("estado.json", estado)
        salvar("canais.json", canais)
    analisar(videos, nichos)
    salvar("videos.json", videos)


if __name__ == "__main__":
    main()
