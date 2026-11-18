"""
Robô de monitoramento de passagens – São Paulo <-> Roma (ida e volta)

Para cada combinação de datas (ida entre DATA_IDA_INICIO e DATA_IDA_FIM,
volta 12 ou 13 dias depois), o robô consulta no Google Flights, via Bright
Data, a busca de ida e volta São Paulo (GRU) <-> Roma (FCO):
2 adultos, econômica, no máximo 1 conexão, somente LATAM e as companhias
parceiras que acumulam milhas LATAM Pass.

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
ORIGEM = "GRU"            # São Paulo – Guarulhos
DESTINO = "FCO"           # Roma – Fiumicino
DATA_IDA_INICIO = date(2027, 4, 16)
DATA_IDA_FIM = date(2027, 5, 7)
ESTADIAS_DIAS = [12, 13]
ADULTOS = 2
MAX_CONEXOES = 1          # 0 = só voo direto, 1 = até 1 conexão

# LATAM + parceiras que acumulam LATAM Pass (lista oficial do LATAM Pass)
COMPANHIAS = [
    "LA",  # LATAM
    "AR",  # Aerolíneas Argentinas
    "AM",  # Aeroméxico
    "OS",  # Austrian Airlines
    "BA",  # British Airways
    "CX",  # Cathay Pacific
    "DL",  # Delta
    "AY",  # Finnair
    "IB",  # Iberia
    "JL",  # Japan Airlines
    "LH",  # Lufthansa
    "QF",  # Qantas
    "QR",  # Qatar Airways
    "RJ",  # Royal Jordanian
    "LX",  # Swiss
    "VS",  # Virgin Atlantic
]

ALVO = float(os.environ.get("ALVO", "8500"))
TOLERANCIA = float(os.environ.get("TOLERANCIA", "9000"))

ARQUIVO_HISTORICO = "historico.csv"
ARQUIVO_ESTADO = "estado.json"
PASTA_DEPURACAO = "depuracao"

API_KEY = os.environ.get("BRIGHTDATA_API_KEY", "").strip()
ZONA = os.environ.get("BRIGHTDATA_SERP_ZONE", "").strip()
MODO_TESTE = os.environ.get("MODO_TESTE", "false").lower() == "true"


# ----------------------------------------------------------------------
# Montagem do link do Google Flights (parâmetro "tfs")
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


def link_ida_volta(ida, volta, idioma="en"):
    info = _numero(1, 28) + _numero(2, 2)
    info += _msg(3, _trecho(ida, ORIGEM, DESTINO))
    info += _msg(3, _trecho(volta, DESTINO, ORIGEM))
    for _ in range(ADULTOS):
        info += _numero(8, 1)            # 1 = adulto
    info += _numero(9, 1)                # 1 = econômica
    info += _numero(14, 1)
    info += _msg(16, _numero(1, 2**64 - 1))
    info += _numero(19, 1)               # 1 = ida e volta
    tfs = base64.urlsafe_b64encode(info).decode("ascii").rstrip("=")
    params = {"tfs": tfs, "hl": idioma, "gl": "BR", "curr": "BRL"}
    return "https://www.google.com/travel/flights/search?" + urllib.parse.urlencode(params)


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


# Os voos vêm dentro da página como [[null,PRECO],"codigo-do-voo"...].
# Logo antes de cada preço ficam a companhia e as conexões da ida.
PADRAO_PRECO_DADOS = re.compile(r'\[\[null,(\d{4,6})\],"[A-Za-z0-9_\-]{20,}')
PADRAO_COMPANHIA = re.compile(r'\["([A-Z0-9]{2}|multi)",\[((?:"[^"]+",?)+)\],\[\[null')
PADRAO_CONEXAO = re.compile(r'\[\d+,"([A-Z]{3})","[A-Z]{3}",(?:\[[\d,]*\]|null),"[^"]*","([^"]*)"')

PAISES_AEROPORTOS = {
    "LHR": "Reino Unido", "LGW": "Reino Unido", "LCY": "Reino Unido", "MAN": "Reino Unido",
    "MAD": "Espanha", "BCN": "Espanha", "LIS": "Portugal", "OPO": "Portugal",
    "FRA": "Alemanha", "MUC": "Alemanha", "ZRH": "Suíça", "GVA": "Suíça",
    "VIE": "Áustria", "HEL": "Finlândia", "CDG": "França", "ORY": "França",
    "AMS": "Holanda", "DOH": "Catar", "AMM": "Jordânia", "IST": "Turquia",
    "ATL": "Estados Unidos", "JFK": "Estados Unidos", "MIA": "Estados Unidos",
    "DFW": "Estados Unidos", "BOS": "Estados Unidos", "ORD": "Estados Unidos",
    "EZE": "Argentina", "AEP": "Argentina", "MEX": "México", "SCL": "Chile",
    "LIM": "Peru", "BOG": "Colômbia", "GIG": "Brasil", "FOR": "Brasil", "REC": "Brasil",
    "FCO": "Itália", "MXP": "Itália", "LIN": "Itália",
}


CIDADES_PT = {
    "London": "Londres", "Frankfurt am Main": "Frankfurt", "Munich": "Munique",
    "Zurich": "Zurique", "Geneva": "Genebra", "Vienna": "Viena", "Lisbon": "Lisboa",
    "Paris": "Paris", "Amsterdam": "Amsterdã", "Madrid": "Madri", "Barcelona": "Barcelona",
    "Helsinki": "Helsinque", "Doha": "Doha", "Istanbul": "Istambul", "Amman": "Amã",
    "New York": "Nova York", "Mexico City": "Cidade do México", "Rio de Janeiro": "Rio de Janeiro",
}


def _pais(aeroporto, cidade):
    cidade = CIDADES_PT.get(cidade, cidade)
    pais = PAISES_AEROPORTOS.get(aeroporto)
    if pais:
        return f"{cidade or aeroporto} ({pais})"
    return f"{cidade or aeroporto} ({aeroporto})"


def ofertas(html):
    """Lista de (preco, companhias, conexoes_da_ida) encontradas na página."""
    lista = []
    fim_anterior = 0
    for achado in PADRAO_PRECO_DADOS.finditer(html):
        trecho = html[max(fim_anterior, achado.start() - 20000):achado.start()]
        fim_anterior = achado.end()
        preco = int(achado.group(1))
        if not 2000 <= preco <= 200000:
            continue
        cias = PADRAO_COMPANHIA.findall(trecho)
        companhias = ", ".join(re.findall(r'"([^"]+)"', cias[-1][1])) if cias else ""
        inicio_voo = trecho.rfind('[[null,null,') if not cias else trecho.rfind(cias[-1][0] + '",[')
        conexoes = PADRAO_CONEXAO.findall(trecho[max(inicio_voo, 0):])
        if conexoes:
            texto_conexoes = " + ".join(_pais(a, c) for a, c in conexoes)
        else:
            texto_conexoes = "Direto"
        lista.append((preco, companhias, texto_conexoes))
    return lista


def melhor_oferta(html):
    todas = ofertas(html)
    # em caso de empate, prefere a oferta em que a companhia foi identificada
    return min(todas, key=lambda o: (o[0], o[1] == "")) if todas else None


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
    return {}


def gravar_historico(linhas):
    novo = not os.path.exists(ARQUIVO_HISTORICO)
    with open(ARQUIVO_HISTORICO, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if novo:
            w.writerow(["data_consulta", "ida", "volta",
                        "ida_volta_milao_brl", "ida_volta_roma_brl",
                        "estimativa_milao_ida_roma_volta_brl", "link_google_flights"])
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

    print(f"Consultando {len(lista)} combinações de datas (ida e volta Roma)...")
    resultados = []
    for ida, volta in lista:
        html = consultar(link_ida_volta(ida, volta))
        if MODO_TESTE and html:
            with open(os.path.join(PASTA_DEPURACAO, "pagina_roma.html"), "w", encoding="utf-8") as f:
                f.write(html)
        oferta = melhor_oferta(html) if html else None
        preco, companhias, conexoes = oferta if oferta else (None, "", "")
        texto = f"{brl(preco)} – {companhias} – conexão na ida: {conexoes}" if preco else "sem preço"
        print(f"- ida {ida:%d/%m} / volta {volta:%d/%m}: {texto}")
        resultados.append((ida, volta, preco, companhias, conexoes))

    # mesmas colunas do histórico antigo; Milão e estimativa ficam vazias
    gravar_historico([[hoje, i.isoformat(), v.isoformat(), "", p or "", "",
                       link_ida_volta(i, v, idioma="pt-BR")] for i, v, p, _, _ in resultados])

    validos = [r for r in resultados if r[2]]
    if not validos:
        msg = "Nenhum preço encontrado hoje. Veja o log da execução."
        print(msg)
        escrever_resumo(f"### {hoje}\n{msg}")
        sys.exit(1)

    ida, volta, preco, companhias, conexoes = min(validos, key=lambda r: r[2])
    link = link_ida_volta(ida, volta, idioma="pt-BR")
    estado = ler_estado()
    menor_antes = estado.get("menor_preco_historico_roma")
    novo_recorde = menor_antes is None or preco < menor_antes
    status = status_do_preco(preco)

    resumo = (f"### Consulta de {hoje}\n"
              f"- Melhor preço: **{brl(preco)}** para 2 adultos ({status}) – ida e volta São Paulo ⇄ Roma\n"
              f"- Ida: {ida:%d/%m/%Y} | Volta: {volta:%d/%m/%Y}\n"
              f"- Companhia(s): {companhias} | Conexão na ida: {conexoes}\n"
              f"- [Abrir no Google Flights]({link})")
    print(resumo)
    escrever_resumo(resumo)

    if MODO_TESTE:
        return

    if novo_recorde:
        estado["menor_preco_historico_roma"] = preco
    with open(ARQUIVO_ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)

    ultimo_resultado = {
        "data_consulta": hoje,
        "roteiro": "ida e volta São Paulo (GRU) ⇄ Roma (FCO)",
        "melhor_preco_total_2_adultos_brl": int(preco),
        "status": status,
        "ida": ida.isoformat(),
        "volta": volta.isoformat(),
        "companhias": companhias,
        "conexao_ida": conexoes,
        "detalhes": "LATAM e parceiras LATAM Pass, até 1 conexão. A conexão informada é a da ida; a volta costuma seguir o mesmo caminho. Confirme pelo link antes de comprar.",
        "link_google_flights": link,
        "menor_preco_historico_brl": int(estado["menor_preco_historico_roma"]),
        "novo_recorde": novo_recorde,
        "combinacoes_consultadas": len(resultados),
        "combinacoes_com_preco": len(validos),
    }
    with open("ultimo_resultado.json", "w", encoding="utf-8") as f:
        json.dump(ultimo_resultado, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
