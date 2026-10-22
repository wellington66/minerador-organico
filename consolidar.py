"""Consolida o que foi analisado em um "cérebro" por nicho:
tipos de gancho que mais viralizam, melhores ganchos, e ranking de dores,
correções, objeções, desejos e dúvidas do público. Gera data/cerebro.json."""
import json, os
from collections import Counter
from datetime import datetime, timezone

from minerar import DADOS, MODELO_TEXTO, RAIZ, gemini, ler, pontuar, salvar

CATS = ["dores", "correcoes", "objecoes", "desejos", "duvidas", "ideias_produto"]

PROMPT = """Você recebe itens extraídos dos comentários de {n} vídeos virais do nicho "{nicho}".
Agrupe itens que significam a mesma coisa e devolva um RANKING por frequência, em JSON (pt-BR):
{{
 "resumo": "3 frases: o que esse público mais sente, quer e contesta",
 {campos}
 "angulos": ["5 ângulos de conteúdo/anúncio que atacam as dores e correções mais fortes"]
}}
Em cada categoria: lista de até 8 {{"tema": "...", "qtd": nº de vídeos em que apareceu, "exemplo": "frase literal do público"}}.

ITENS (categoria | vídeo | texto):
{itens}"""


def main():
    with open(os.path.join(RAIZ, "nichos.json"), encoding="utf-8") as f:
        nichos = json.load(f)
    videos = ler("videos.json", {})
    antigo = ler("cerebro.json", {}).get("nichos", {})
    saida = {}
    for chave, cfg in nichos.items():
        vs = [v for v in videos.values() if chave in v["nichos"] and v.get("gancho")]
        if not vs:
            continue
        tipos = Counter(v["gancho"].get("tipo_gancho", "?") for v in vs)
        formatos = Counter(v["gancho"].get("formato", "?") for v in vs)
        top = sorted(vs, key=pontuar, reverse=True)[:12]
        bloco = {
            "nome": cfg["nome"], "videos": len(vs),
            "tipos_gancho": tipos.most_common(), "formatos": formatos.most_common(8),
            "melhores_ganchos": [{"id": v["id"], "titulo": v["titulo"], "views": v["views"],
                                  "outlier": v["outlier"],
                                  "gancho": v["gancho"].get("gancho_falado") or v["gancho"].get("texto_tela"),
                                  "modelo": v["gancho"].get("modelo_gancho"),
                                  "tipo": v["gancho"].get("tipo_gancho")} for v in top],
        }
        com_dores = [v for v in vs if v.get("dores")]
        assinatura = sorted(v["id"] for v in com_dores)
        ant = antigo.get(chave, {})
        if com_dores and ant.get("_assinatura") == assinatura:
            bloco.update({k: ant[k] for k in ant if k in CATS + ["resumo", "angulos"]})
        elif com_dores:
            linhas = [f"{c} | {v['id']} | {t}" for v in com_dores for c in CATS
                      for t in (v["dores"].get(c) or [])]
            try:
                r = gemini(MODELO_TEXTO, [{"text": PROMPT.format(
                    n=len(com_dores), nicho=cfg["nome"],
                    campos="".join(f'"{c}": [...],\n ' for c in CATS),
                    itens="\n".join(linhas)[:60000])}])
                bloco.update({k: r.get(k) for k in CATS + ["resumo", "angulos"]})
            except Exception as e:
                print("consolidação falhou:", chave, str(e)[:200])
                bloco.update({k: ant.get(k) for k in CATS + ["resumo", "angulos"]})
                assinatura = ant.get("_assinatura")
        bloco["_assinatura"] = assinatura
        saida[chave] = bloco
        print(f"{chave}: {len(vs)} vídeos")

    todos = [v for v in videos.values() if v.get("gancho")]
    salvar("cerebro.json", {
        "gerado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_videos": len(todos),
        "tipos_gancho_geral": Counter(v["gancho"].get("tipo_gancho", "?") for v in todos).most_common(),
        "nichos": saida,
    })


if __name__ == "__main__":
    main()
