"""
Robô de monitoramento de passagens – Milão (ida) / Roma (volta)

O que ele faz, toda vez que roda:
1. Monta todas as combinações de datas:
   ida São Paulo (GRU) -> Milão (MXP) entre DATA_IDA_INICIO e DATA_IDA_FIM,
   volta Roma (FCO) -> São Paulo (GRU) 12 ou 13 dias depois.
2. Para cada combinação, consulta o Google Flights (via API da Bright Data)
   em modo "vários destinos", 2 adultos, classe econômica, no máximo 1 conexão,
   somente LATAM e parceiras que acumulam LATAM Pass.
3. Guarda o menor preço de cada combinação em historico.csv.
4. Grava o melhor resultado do dia em ultimo_resultado.json. Uma tarefa
   agendada no Claude lê esse arquivo, atualiza a planilha e te notifica
   no app do Claude quando o preço chegar à meta.

Você não precisa mexer neste arquivo. As configurações ficam no topo
(datas, valores-alvo, companhias) caso queira mudar algo no futuro.
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
# CONFIGURAÇÕES (pode alterar aqui se mudar de ideia)
# ----------------------------------------------------------------------
ORIGEM = "GRU"            # São Paulo – Guarulhos
CIDADE_IDA = "MXP"        # Milão – Malpensa
CIDADE_VOLTA = "FCO"      # Roma – Fiumicino
DATA_IDA_INICIO = date(2027, 4, 16)
DATA_IDA_FIM = date(2027, 5, 7)
ESTADIAS_DIAS = [12, 13]
ADULTOS = 2
MAX_CONEXOES = 1          # 0 = só voo direto, 1 = até 1 conexão

# LATAM e parceiras que acumulam LATAM Pass (sem British Airways/Virgin,
# o que já evita conexões em Londres):
# LA = LATAM, IB = Iberia, LH = Lufthansa, LX = Swiss, OS = Austrian, AY = Finnair
COMPANHIAS = ["LA", "IB", "LH", "LX", "OS", "AY"]

ALVO = float(os.environ.get("ALVO", "8500"))
TOLERANCIA = float(os.environ.get("TOLERANCIA", "9000"))

ARQUIVO_HISTORICO = "historico.csv"
ARQUIVO_ESTADO = "estado.json"
PASTA_DEPURACAO = "depuracao"

API_KEY = os.environ.get("BRIGHTDATA_API_KEY", "")
ZONA = os.environ.get("BRIGHTDATA_SERP_ZONE", "")
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


def _campo_texto(num, texto):
    dados = texto.encode("utf-8")
    return _varint((num << 3) | 2) + _varint(len(dados)) + dados


def _campo_numero(num, valor):
    return _varint((num << 3) | 0) + _varint(valor)


def _campo_msg(num, conteudo):
    return _varint((num << 3) | 2) + _varint(len(conteudo)) + conteudo


def _trecho(data_voo, de, para):
    msg = _campo_texto(2, data_voo.isoformat())
    msg += _campo_numero(5, MAX_CONEXOES)
    for cia in COMPANHIAS:
        msg += _campo_texto(6, cia)
    # aeroporto: tipo 1 = código IATA
    msg += _campo_msg(13, _campo_numero(1, 1) + _campo_texto(2, de))
    msg += _campo_msg(14, _campo_numero(1, 1) + _campo_texto(2, para))
    return msg


def link_google_flights(data_ida, data_volta, idioma="en"):
    info = _campo_numero(1, 28) + _campo_numero(2, 2)
    info += _campo_msg(3, _trecho(data_ida, ORIGEM, CIDADE_IDA))
    info += _campo_msg(3, _trecho(data_volta, CIDADE_VOLTA, ORIGEM))
    for _ in range(ADULTOS):
        info += _campo_numero(8, 1)      # 1 = adulto
    info += _campo_numero(9, 1)          # 1 = econômica
    info += _campo_numero(14, 1)
    info += _campo_msg(16, _campo_numero(1, 2**64 - 1))  # sem limite de preço
    info += _campo_numero(19, 3)         # 3 = vários destinos
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
                json={"zone": ZONA, "url": url, "format": "raw", "country": "br"},
                timeout=120,
            )
            if resp.status_code == 200 and resp.text:
                return resp.text
            detalhe = (resp.headers.get("x-brd-error") or resp.text or "")[:300]
            print(f"   aviso: resposta {resp.status_code} (tentativa {tentativa + 1}): {detalhe}")
        except requests.RequestException as erro:
            print(f"   aviso: erro de conexão ({erro}) (tentativa {tentativa + 1})")
        time.sleep(5 * (tentativa + 1))
    return None


PADRAO_ROTULO = re.compile(r'aria-label="(From [^"]*?Brazilian rea[li]s?[^"]*)"')
PADRAO_PRECO = re.compile(r"([\d][\d,\.]*)\s+Brazilian rea[li]s?")


def _para_numero(texto):
    limpo = texto.replace(",", "").replace(".", "")
    return float(limpo) if limpo.isdigit() else None


def extrair_ofertas(html):
    """Devolve lista de (preco, descricao) encontrados na página."""
    ofertas = []
    for rotulo in PADRAO_ROTULO.findall(html):
        achado = PADRAO_PRECO.search(rotulo)
        if not achado:
            continue
        preco = _para_numero(achado.group(1))
        if preco and preco > 1000:  # ignora valores que não são o total da viagem
            descricao = re.sub(r"\s+", " ", rotulo).replace("&amp;", "&")
            ofertas.append((preco, descricao[:300]))
    if not ofertas:
        # plano B: preços exibidos como "R$8,508" ou "BRL 8,508"
        for achado in re.finditer(r"(?:R\$|BRL)\s?([\d][\d,\.]*)", html):
            preco = _para_numero(achado.group(1))
            if preco and 3000 < preco < 100000:
                ofertas.append((preco, "preço lido do texto da página (sem detalhes do voo)"))
    return ofertas


# ----------------------------------------------------------------------
# Histórico, estado e alerta
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
    return {"menor_preco_historico": None, "ultimo_alerta": None}


def salvar_estado(estado):
    with open(ARQUIVO_ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)


def gravar_historico(linhas):
    novo = not os.path.exists(ARQUIVO_HISTORICO)
    with open(ARQUIVO_HISTORICO, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if novo:
            w.writerow(["data_consulta", "ida_gru_milao", "volta_roma_gru",
                        "preco_total_2_adultos_brl", "status", "detalhes", "link_google_flights"])
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

    print(f"Consultando {len(lista)} combinações de datas...")
    resultados = []
    for ida, volta in lista:
        url = link_google_flights(ida, volta)
        print(f"- ida {ida:%d/%m} / volta {volta:%d/%m}")
        html = consultar(url)
        if html is None and MODO_TESTE:
            sys.exit("Teste: a Bright Data recusou as 3 tentativas. Veja as linhas 'aviso: resposta' acima.")
        if html is None:
            resultados.append((ida, volta, None, "falha na consulta", url))
            continue
        if MODO_TESTE:
            with open(os.path.join(PASTA_DEPURACAO, "pagina.html"), "w", encoding="utf-8") as f:
                f.write(html)
        ofertas = extrair_ofertas(html)
        if not ofertas:
            resultados.append((ida, volta, None, "nenhum preço encontrado na página", url))
            print("   nenhum preço encontrado")
            continue
        preco, descricao = min(ofertas, key=lambda o: o[0])
        print(f"   menor preço: {brl(preco)}")
        resultados.append((ida, volta, preco, descricao, url))

    linhas = []
    for ida, volta, preco, descricao, url in resultados:
        linhas.append([hoje, ida.isoformat(), volta.isoformat(),
                       "" if preco is None else int(preco),
                       status_do_preco(preco), descricao,
                       link_google_flights(ida, volta, idioma="pt-BR")])
    gravar_historico(linhas)

    validos = [r for r in resultados if r[2] is not None]
    if not validos:
        msg = "Nenhum preço encontrado hoje. Veja o log da execução."
        print(msg)
        escrever_resumo(f"### {hoje}\n{msg}")
        if not MODO_TESTE:
            sys.exit(1)
        return

    ida, volta, preco, descricao, _ = min(validos, key=lambda r: r[2])
    link = link_google_flights(ida, volta, idioma="pt-BR")
    estado = ler_estado()
    menor_antes = estado.get("menor_preco_historico")
    novo_recorde = menor_antes is None or preco < menor_antes
    status = status_do_preco(preco)

    resumo = (f"### Consulta de {hoje}\n"
              f"- Melhor preço: **{brl(preco)}** para 2 adultos ({status})\n"
              f"- Ida GRU → Milão: {ida:%d/%m/%Y} | Volta Roma → GRU: {volta:%d/%m/%Y}\n"
              f"- Menor preço já visto: {brl(min(preco, menor_antes or preco))}\n"
              f"- Detalhes: {descricao}\n"
              f"- [Abrir no Google Flights]({link})")
    print(resumo)
    escrever_resumo(resumo)

    if novo_recorde:
        estado["menor_preco_historico"] = preco

    # Resultado do dia, lido pela tarefa agendada do Claude para te notificar
    ultimo_resultado = {
        "data_consulta": hoje,
        "melhor_preco_total_2_adultos_brl": int(preco),
        "status": status,
        "ida_gru_milao": ida.isoformat(),
        "volta_roma_gru": volta.isoformat(),
        "detalhes": descricao,
        "link_google_flights": link,
        "menor_preco_historico_brl": int(estado["menor_preco_historico"]),
        "novo_recorde": novo_recorde,
        "combinacoes_consultadas": len(resultados),
        "combinacoes_com_preco": len(validos),
    }

    if not MODO_TESTE:
        salvar_estado(estado)
        with open("ultimo_resultado.json", "w", encoding="utf-8") as f:
            json.dump(ultimo_resultado, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
