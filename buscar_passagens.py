"""
Robô de monitoramento de passagens – Milão (ida) / Roma (volta)

Como o Google Flights funciona por dentro:
- Buscas de IDA E VOLTA já vêm com os preços dentro da página.
- Buscas de VÁRIOS DESTINOS (ida para Milão, volta de Roma) só carregam os
  preços depois, por JavaScript. Por isso não dá para lê-las direto.

Então, para cada combinação de datas, o robô consulta:
1. Ida e volta São Paulo <-> Milão
2. Ida e volta São Paulo <-> Roma
e calcula uma ESTIMATIVA do roteiro Milão (ida) / Roma (volta): a média dos
dois. Companhias como a LATAM costumam cobrar o roteiro "vai por uma cidade,
volta por outra" perto dessa média. O link enviado no aviso abre o roteiro
exato no Google Flights para você confirmar o preço antes de comprar.

Filtros: 2 adultos, econômica, no máximo 1 conexão, somente LATAM e
parceiras que acumulam LATAM Pass.

Resultado do dia: ultimo_resultado.json (lido pela tarefa agendada do Claude,
que atualiza a planilha e te notifica) e historico.csv.
"""

import base64
import csv
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone

import requests

# ----------------------------------------------------------------------
# CONFIGURAÇÕES
# ----------------------------------------------------------------------
ORIGEM = "GRU"
MILAO = "MXP"
ROMA = "FCO"
DATA_IDA_INICIO = date(2027, 4, 16)
DATA_IDA_FIM = date(2027, 5, 7)
ESTADIAS_DIAS = [12, 13]
ADULTOS = 2
MAX_CONEXOES = 1

# LA = LATAM, IB = Iberia, LH = Lufthansa, LX = Swiss, OS = Austrian, AY = Finnair
COMPANHIAS = ["LA", "IB", "LH", "LX", "OS", "AY"]

ALVO = float(os.environ.get("ALVO", "8500"))
TOLERANCIA = float(os.environ.get("TOLERANCIA", "9000"))

ARQUIVO_HISTORICO = "historico.csv"
ARQUIVO_ESTADO = "estado.json"
PASTA_DEPURACAO = "depuracao"

API_KEY = os.environ.get("BRIGHTDATA_API_KEY", "").strip()
ZONA = os.environ.get("BRIGHTDATA_SERP_ZONE", "").strip()
MODO_TESTE = os.environ.get("MODO_TESTE", "false").lower() == "true"


# ----------------------------------------------------------------------
# Montagem dos links do Google Flights (parâmetro "tfs")
# ----------------------------------------------------------------------
def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _texto(num, texto):
    dados = texto.encode("utf-8")
    return _varint((num << 3) | 2) + _varint(len(dados)) + dados


def _numero(num, valor):
    return _varint((num << 3) | 0) + _varint(valor)


def _msg(num, conteudo):
    return _varint((num << 3) | 2) + _varint(len(conteudo)) + conteudo


def _trecho(data_voo, de, para):
    msg = _texto(2, data_voo.isoformat())
    msg += _numero(5, MAX_CONEXOES)
    for cia in COMPANHIAS:
        msg += _texto(6, cia)
    msg += _msg(13, _numero(1, 1) + _texto(2, de))
    msg += _msg(14, _numero(1, 1) + _texto(2, para))
    return msg


def _link(trechos, tipo, idioma):
    """tipo: 1 = ida e volta, 3 = vários destinos."""
    info = _numero(1, 28) + _numero(2, 2)
    for data_voo, de, para in trechos:
        info += _msg(3, _trecho(data_voo, de, para))
    for _ in range(ADULTOS):
        info += _numero(8, 1)
    info += _numero(9, 1)
    info += _numero(14, 1)
    info += _msg(16, _numero(1, 2**64 - 1))
    info += _numero(19, tipo)
    tfs = base64.urlsafe_b64encode(info).decode("ascii").rstrip("=")
    params = {"tfs": tfs, "hl": idioma, "gl": "BR", "curr": "BRL"}
    return "https://www.google.com/travel/flights/search?" + urllib.parse.urlencode(params)


def link_ida_volta(cidade, ida, volta, idioma="en"):
    return _link([(ida, ORIGEM, cidade), (volta, cidade, ORIGEM)], 1, idioma)


def link_milao_roma(ida, volta, idioma="pt-BR"):
    return _link([(ida, ORIGEM, MILAO), (volta, ROMA, ORIGEM)], 3, idioma)


# ----------------------------------------------------------------------
# Consulta e leitura dos preços
# ----------------------------------------------------------------------
def consultar(url):
    for tentativa in range(3):
        try:
            resp = requests.post(
                "https://api.brightdata.com/request",
                headers={"Authorization": f"Bearer {API_KEY}"},
                json={"zone": ZONA, "url": url, "format": "raw"},
                timeout=120,
            )
            if resp.status_code == 200 and len(resp.text) > 10000:
                return resp.text
            detalhe = (resp.headers.get("x-brd-error") or resp.text or "")[:300]
            print(f"   aviso: resposta {resp.status_code} (tentativa {tentativa + 1}): {detalhe}")
        except requests.RequestException as erro:
            print(f"   aviso: erro de conexão ({erro}) (tentativa {tentativa + 1})")
        time.sleep(5 * (tentativa + 1))
    return None


# Os voos vêm dentro da página como [[null,PRECO],"codigo-do-voo"...]
PADRAO_PRECO_DADOS = re.compile(r'\[\[null,(\d{4,6})\],"[A-Za-z0-9_\-]{20,}')


def menor_preco(html):
    precos = [int(p) for p in PADRAO_PRECO_DADOS.findall(html)]
    precos = [p for p in precos if 2000 <= p <= 200000]
    return min(precos) if precos else None


# ----------------------------------------------------------------------
# Histórico, estado e resumo
# ----------------------------------------------------------------------
def combinacoes():
    lista = []
    dia = DATA_IDA_INICIO
    while dia <= DATA_IDA_FIM:
        for estadia in ESTADIAS_DIAS:
            lista.append((dia, dia + timedelta(days=estadia)))
        dia += timedelta(days=1)
    return lista


def ler_estado():
    if os.path.exists(ARQUIVO_ESTADO):
        with open(ARQUIVO_ESTADO, encoding="utf-8") as f:
            return json.load(f)
    return {"menor_preco_historico": None}


def gravar_historico(linhas):
    novo = not os.path.exists(ARQUIVO_HISTORICO)
    with open(ARQUIVO_HISTORICO, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if novo:
            w.writerow(["data_consulta", "ida", "volta",
                        "ida_volta_milao_brl", "ida_volta_roma_brl",
                        "estimativa_milao_ida_roma_volta_brl", "link_milao_roma"])
        w.writerows(linhas)


def status_do_preco(preco):
    if preco is None:
        return "sem preço"
    if preco <= ALVO:
        return "COMPRAR"
    if preco <= TOLERANCIA:
        return "Quase lá"
    return "Aguardar"


def brl(valor):
    return "R$ " + f"{valor:,.0f}".replace(",", ".")


def escrever_resumo(texto):
    caminho = os.environ.get("GITHUB_STEP_SUMMARY")
    if caminho:
        with open(caminho, "a", encoding="utf-8") as f:
            f.write(texto + "\n")


# ----------------------------------------------------------------------
# Execução principal
# ----------------------------------------------------------------------
def main():
    if not API_KEY or not ZONA:
        sys.exit("Faltam os segredos BRIGHTDATA_API_KEY e/ou BRIGHTDATA_SERP_ZONE.")

    hoje = datetime.now(timezone(timedelta(hours=-3))).date().isoformat()
    lista = combinacoes()
    if MODO_TESTE:
        lista = lista[:1]
        os.makedirs(PASTA_DEPURACAO, exist_ok=True)

    print(f"Consultando {len(lista)} combinações de datas (Milão e Roma)...")
    resultados = []
    for ida, volta in lista:
        print(f"- ida {ida:%d/%m} / volta {volta:%d/%m}")
        precos = {}
        for nome, cidade in (("milao", MILAO), ("roma", ROMA)):
            html = consultar(link_ida_volta(cidade, ida, volta))
            if MODO_TESTE and html:
                with open(os.path.join(PASTA_DEPURACAO, f"pagina_{nome}.html"), "w", encoding="utf-8") as f:
                    f.write(html)
            precos[nome] = menor_preco(html) if html else None
            print(f"   ida e volta {nome}: {brl(precos[nome]) if precos[nome] else 'sem preço'}")
        m, r = precos["milao"], precos["roma"]
        estimativa = round((m + r) / 2) if m and r else None
        if estimativa:
            print(f"   estimativa Milão (ida) / Roma (volta): {brl(estimativa)}")
        resultados.append((ida, volta, m, r, estimativa))

    gravar_historico([[hoje, i.isoformat(), v.isoformat(), m or "", r or "", e or "",
                       link_milao_roma(i, v)] for i, v, m, r, e in resultados])

    # melhor opção do dia entre as três formas de viajar
    opcoes = []
    for ida, volta, m, r, e in resultados:
        if e:
            opcoes.append((e, ida, volta, "Milão (ida) / Roma (volta) – estimativa"))
        if m:
            opcoes.append((m, ida, volta, "Ida e volta por Milão"))
        if r:
            opcoes.append((r, ida, volta, "Ida e volta por Roma"))

    if not opcoes:
        msg = "Nenhum preço encontrado hoje. Veja o log da execução."
        print(msg)
        escrever_resumo(f"### {hoje}\n{msg}")
        sys.exit(1)

    preco, ida, volta, tipo = min(opcoes)
    melhor_open_jaw = min((o for o in opcoes if o[3].startswith("Milão")), default=None)
    link = link_milao_roma(ida, volta)
    estado = ler_estado()
    menor_antes = estado.get("menor_preco_historico")
    novo_recorde = menor_antes is None or preco < menor_antes
    status = status_do_preco(preco)

    linhas_resumo = [
        f"### Consulta de {hoje}",
        f"- Melhor preço: **{brl(preco)}** para 2 adultos ({status}) – {tipo}",
        f"- Ida GRU: {ida:%d/%m/%Y} | Volta para GRU: {volta:%d/%m/%Y}",
    ]
    if melhor_open_jaw:
        linhas_resumo.append(f"- Melhor estimativa Milão/Roma: {brl(melhor_open_jaw[0])} "
                             f"(ida {melhor_open_jaw[1]:%d/%m}, volta {melhor_open_jaw[2]:%d/%m})")
    linhas_resumo.append(f"- [Abrir Milão (ida) / Roma (volta) no Google Flights]({link})")
    resumo = "\n".join(linhas_resumo)
    print(resumo)
    escrever_resumo(resumo)

    if MODO_TESTE:
        return

    if novo_recorde:
        estado["menor_preco_historico"] = preco
    with open(ARQUIVO_ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)

    ultimo_resultado = {
        "data_consulta": hoje,
        "melhor_preco_total_2_adultos_brl": int(preco),
        "status": status,
        "ida_gru_milao": ida.isoformat(),
        "volta_roma_gru": volta.isoformat(),
        "detalhes": f"{tipo}. LATAM e parceiras, até 1 conexão. Confirme o preço exato pelo link antes de comprar.",
        "link_google_flights": link,
        "menor_preco_historico_brl": int(estado["menor_preco_historico"]),
        "novo_recorde": novo_recorde,
        "melhor_estimativa_milao_roma_brl": int(melhor_open_jaw[0]) if melhor_open_jaw else None,
        "combinacoes_consultadas": len(resultados),
        "combinacoes_com_preco": sum(1 for r in resultados if r[2] or r[3]),
    }
    with open("ultimo_resultado.json", "w", encoding="utf-8") as f:
        json.dump(ultimo_resultado, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
