import base64
import io
import os
import re
import json
import secrets
import sqlite3
import unicodedata
from datetime import datetime

import pandas as pd
import plotly.express as px

from dash import Dash, html, dcc, dash_table, no_update
from dash.dependencies import Input, Output, State
import dash_bootstrap_components as dbc
from flask import Response, request


# ============================================================
# CONFIGURAÇÕES
# ============================================================

NOME_SISTEMA = "Monitoramento Cidadania"

PASTA_BASE = os.path.dirname(os.path.abspath(__file__))
PASTA_ASSETS = os.path.join(PASTA_BASE, "assets")
PASTA_DATA = os.environ.get(
    "STORAGE_PATH",
    os.path.join(PASTA_BASE, "data"),
)
PASTA_UPLOADS = os.path.join(PASTA_DATA, "uploads")
PASTA_OUTPUTS = os.path.join(PASTA_DATA, "outputs")
PASTA_HISTORICO = os.path.join(PASTA_DATA, "historico_importacoes")
CAMINHO_BANCO = os.path.join(PASTA_DATA, "monitoramento_cidadania.sqlite3")

for pasta in [
    PASTA_ASSETS,
    PASTA_DATA,
    PASTA_UPLOADS,
    PASTA_OUTPUTS,
    PASTA_HISTORICO,
]:
    os.makedirs(pasta, exist_ok=True)


# ============================================================
# CONFIGURAÇÃO DO DASH
# ============================================================

app = Dash(
    __name__,
    external_stylesheets=[
        dbc.themes.BOOTSTRAP,
        dbc.icons.FONT_AWESOME,
    ],
    suppress_callback_exceptions=True,
    title=NOME_SISTEMA,
)

server = app.server

APP_USERNAME = os.environ.get("APP_USERNAME")
APP_PASSWORD = os.environ.get("APP_PASSWORD")

if bool(APP_USERNAME) != bool(APP_PASSWORD):
    raise RuntimeError(
        "Defina APP_USERNAME e APP_PASSWORD juntos para ativar a proteção de acesso."
    )

if APP_USERNAME and APP_PASSWORD:
    @server.before_request
    def exigir_autenticacao():
        autenticacao = request.authorization
        usuario_valido = (
            autenticacao
            and secrets.compare_digest(autenticacao.username or "", APP_USERNAME)
        )
        senha_valida = (
            autenticacao
            and secrets.compare_digest(autenticacao.password or "", APP_PASSWORD)
        )

        if not usuario_valido or not senha_valida:
            return Response(
                "Autenticação necessária.",
                401,
                {"WWW-Authenticate": 'Basic realm="Monitoramento Cidadania"'},
            )

        return None


# ============================================================
# ARMAZENAMENTO LOCAL / HISTÓRICO DE IMPORTAÇÕES
# ============================================================

def conectar_banco():
    conexao = sqlite3.connect(CAMINHO_BANCO)
    conexao.row_factory = sqlite3.Row
    return conexao


def inicializar_banco():
    with conectar_banco() as conexao:
        conexao.execute(
            """
            CREATE TABLE IF NOT EXISTS importacoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tipo TEXT NOT NULL,
                data_hora TEXT NOT NULL,
                pessoa TEXT,
                arquivos TEXT NOT NULL,
                quantidade_arquivos INTEGER NOT NULL DEFAULT 0,
                quantidade_registros INTEGER NOT NULL DEFAULT 0,
                quantidade_unidades INTEGER NOT NULL DEFAULT 0,
                arquivo_dados TEXT,
                pasta_arquivos TEXT
            )
            """
        )
        conexao.execute(
            """
            CREATE TABLE IF NOT EXISTS arquivos_importados (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                importacao_id INTEGER NOT NULL,
                nome_original TEXT NOT NULL,
                caminho_arquivo TEXT NOT NULL,
                FOREIGN KEY(importacao_id) REFERENCES importacoes(id)
            )
            """
        )
        conexao.commit()


def nome_arquivo_seguro(nome):
    nome = os.path.basename(str(nome or "arquivo"))
    nome = re.sub(r"[^A-Za-z0-9._-]+", "_", nome)
    nome = nome.strip("._")
    return nome or "arquivo"


def bytes_upload(conteudo_upload):
    if not conteudo_upload:
        return b""
    _, conteudo = conteudo_upload.split(",", 1)
    return base64.b64decode(conteudo)


def registrar_importacao(tipo, nomes_arquivos, conteudos_arquivos, dataframe, pessoa=None, payload_historico=None):
    """Salva arquivos originais, cópia consolidada e metadados no computador."""
    inicializar_banco()
    nomes_arquivos = list(nomes_arquivos or [])
    conteudos_arquivos = list(conteudos_arquivos or [])
    quantidade_registros = len(dataframe)
    quantidade_unidades = 0

    if tipo == "Atendimentos":
        colunas = localizar_colunas(dataframe)
        quantidade_unidades = contar_unicos(dataframe, colunas["unidade"])

    data_hora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with conectar_banco() as conexao:
        cursor = conexao.execute(
            """
            INSERT INTO importacoes (
                tipo, data_hora, pessoa, arquivos,
                quantidade_arquivos, quantidade_registros,
                quantidade_unidades, arquivo_dados, pasta_arquivos
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tipo,
                data_hora,
                pessoa,
                json.dumps(nomes_arquivos, ensure_ascii=False),
                len(nomes_arquivos),
                quantidade_registros,
                quantidade_unidades,
                "",
                "",
            ),
        )
        importacao_id = cursor.lastrowid
        pasta_importacao = os.path.join(PASTA_HISTORICO, f"importacao_{importacao_id:06d}")
        os.makedirs(pasta_importacao, exist_ok=True)

        for indice, (nome, conteudo) in enumerate(zip(nomes_arquivos, conteudos_arquivos), start=1):
            nome_seguro = f"{indice:02d}_" + nome_arquivo_seguro(nome)
            caminho_original = os.path.join(pasta_importacao, nome_seguro)
            with open(caminho_original, "wb") as arquivo:
                arquivo.write(bytes_upload(conteudo))
            conexao.execute(
                """
                INSERT INTO arquivos_importados (importacao_id, nome_original, caminho_arquivo)
                VALUES (?, ?, ?)
                """,
                (importacao_id, str(nome), caminho_original),
            )

        if tipo == "Atendimentos":
            caminho_dados = os.path.join(pasta_importacao, "base_consolidada.csv")
            dataframe.to_csv(caminho_dados, index=False, sep=";", encoding="utf-8-sig")
        else:
            caminho_dados = os.path.join(pasta_importacao, "historico_atendido.json")
            with open(caminho_dados, "w", encoding="utf-8") as arquivo:
                json.dump(payload_historico or {}, arquivo, ensure_ascii=False, indent=2)
            dataframe.to_csv(
                os.path.join(pasta_importacao, "historico_atendido.csv"),
                index=False,
                sep=";",
                encoding="utf-8-sig",
            )

        conexao.execute(
            "UPDATE importacoes SET arquivo_dados = ?, pasta_arquivos = ? WHERE id = ?",
            (caminho_dados, pasta_importacao, importacao_id),
        )
        conexao.commit()

    return importacao_id


def listar_importacoes():
    inicializar_banco()
    with conectar_banco() as conexao:
        linhas = conexao.execute(
            """
            SELECT id, tipo, data_hora, pessoa, arquivos,
                   quantidade_arquivos, quantidade_registros,
                   quantidade_unidades, arquivo_dados, pasta_arquivos
            FROM importacoes
            ORDER BY id DESC
            """
        ).fetchall()

    registros = []
    for linha in linhas:
        try:
            arquivos = json.loads(linha["arquivos"])
        except Exception:
            arquivos = []
        try:
            data_formatada = datetime.strptime(
                linha["data_hora"], "%Y-%m-%d %H:%M:%S"
            ).strftime("%d/%m/%Y %H:%M")
        except Exception:
            data_formatada = linha["data_hora"]

        registros.append({
            "ID": int(linha["id"]),
            "Tipo": linha["tipo"],
            "Data / hora": data_formatada,
            "Pessoa": linha["pessoa"] or "",
            "Arquivos": ", ".join(arquivos),
            "Qtd. arquivos": int(linha["quantidade_arquivos"] or 0),
            "Registros": int(linha["quantidade_registros"] or 0),
            "Unidades": int(linha["quantidade_unidades"] or 0),
            "_arquivo_dados": linha["arquivo_dados"] or "",
            "_pasta": linha["pasta_arquivos"] or "",
        })
    return registros


def obter_importacao(importacao_id):
    inicializar_banco()
    with conectar_banco() as conexao:
        linha = conexao.execute(
            "SELECT * FROM importacoes WHERE id = ?",
            (int(importacao_id),),
        ).fetchone()
    return dict(linha) if linha is not None else None


inicializar_banco()


# ============================================================
# FUNÇÕES AUXILIARES
# ============================================================

def criar_card(titulo, valor, icone, id_card):
    return dbc.Card(
        dbc.CardBody(
            [
                html.Div(
                    [
                        html.Div(
                            html.I(
                                className=f"{icone} fa-lg",
                                style={"color": "#1351B4"},
                            ),
                            className=(
                                "d-flex align-items-center "
                                "justify-content-center rounded-3"
                            ),
                            style={
                                "width": "52px",
                                "height": "52px",
                                "minWidth": "52px",
                                "backgroundColor": "#D6E7FF",
                                "border": "1px solid #B8D3F5",
                            },
                        ),
                        html.Div(
                            [
                                html.P(
                                    titulo,
                                    className="mb-1 small fw-semibold",
                                    style={"color": "#4A5B73"},
                                ),
                                html.H3(
                                    valor,
                                    id=id_card,
                                    className="fw-bold mb-0",
                                    style={"color": "#071D41"},
                                ),
                            ],
                            className="ms-3",
                        ),
                    ],
                    className="d-flex align-items-center",
                ),
            ],
            className="p-3",
        ),
        className="h-100 rounded-4",
        style={
            "backgroundColor": "#EAF4FF",
            "border": "1px solid #C5DBF5",
            "boxShadow": "0 5px 16px rgba(19, 81, 180, 0.10)",
        },
    )


def figura_vazia(mensagem="Aguardando dados"):
    figura = px.scatter(template="plotly_white")
    figura.update_layout(
        annotations=[
            dict(
                text=mensagem,
                x=0.5,
                y=0.5,
                xref="paper",
                yref="paper",
                showarrow=False,
                font=dict(size=15),
            )
        ],
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
        margin=dict(l=20, r=20, t=20, b=20),
        height=330,
    )
    return figura


def mapa_colunas_df(df):
    return {
        str(col).strip().lower(): col
        for col in df.columns
    }


def serie_texto(df, coluna):
    if not coluna or coluna not in df.columns:
        return pd.Series(dtype="object")

    return (
        df[coluna]
        .fillna("")
        .astype(str)
        .str.strip()
    )


def valores_unicos(df, coluna):
    valores = serie_texto(df, coluna)
    valores = valores[valores != ""]

    return sorted(
        valores.unique().tolist(),
        key=lambda valor: valor.lower(),
    )


def contar_unicos(df, coluna):
    valores = serie_texto(df, coluna)
    valores = valores[valores != ""]

    if valores.empty:
        return 0

    return int(
        valores.str.lower().nunique()
    )


def localizar_colunas(df):
    mapa = mapa_colunas_df(df)

    return {
        "protocolo": mapa.get("número do protocolo"),
        "nome": mapa.get("nome completo"),
        "status": mapa.get("status do atendimento"),
        "criado": mapa.get("criado em"),
        "atualizado": mapa.get("atualizado em"),
        "unidade": mapa.get("atendido em"),
        "usuario": mapa.get("atendido por"),
    }


def converter_datas(serie):
    return pd.to_datetime(
        serie,
        errors="coerce",
        dayfirst=True,
    )


def aplicar_filtros(
    df,
    unidade=None,
    usuario=None,
    status=None,
    data_inicial=None,
    data_final=None,
):
    df_filtrado = df.copy()
    colunas = localizar_colunas(df_filtrado)

    if unidade and colunas["unidade"]:
        valores = serie_texto(
            df_filtrado,
            colunas["unidade"],
        )
        df_filtrado = df_filtrado[
            valores == unidade
        ].copy()

    if usuario and colunas["usuario"]:
        valores = serie_texto(
            df_filtrado,
            colunas["usuario"],
        )
        df_filtrado = df_filtrado[
            valores == usuario
        ].copy()

    if status and colunas["status"]:
        valores = serie_texto(
            df_filtrado,
            colunas["status"],
        )
        df_filtrado = df_filtrado[
            valores == status
        ].copy()

    if colunas["criado"]:
        df_filtrado["_data_criado"] = converter_datas(
            df_filtrado[colunas["criado"]]
        )

        if data_inicial:
            inicio = pd.to_datetime(data_inicial)
            df_filtrado = df_filtrado[
                df_filtrado["_data_criado"] >= inicio
            ].copy()

        if data_final:
            fim = (
                pd.to_datetime(data_final)
                + pd.Timedelta(days=1)
            )
            df_filtrado = df_filtrado[
                df_filtrado["_data_criado"] < fim
            ].copy()

    return df_filtrado


def mascara_sem_dados_atendimento(df):
    """
    Identifica linhas em que não existem dados essenciais do atendimento.

    O protocolo pode existir por ser gerado pelo sistema.
    São avaliados: Nome, Status, Criado Em, Atualizado Em,
    Atendido Em e Atendido Por.
    """
    if df.empty:
        return pd.Series(False, index=df.index)

    colunas = localizar_colunas(df)

    chaves = [
        "nome",
        "status",
        "criado",
        "atualizado",
        "unidade",
        "usuario",
    ]

    disponiveis = [
        colunas[chave]
        for chave in chaves
        if colunas.get(chave)
    ]

    if len(disponiveis) < 3:
        return pd.Series(False, index=df.index)

    sem_dados = pd.Series(True, index=df.index)

    for coluna in disponiveis:
        sem_dados = (
            sem_dados
            & (
                serie_texto(
                    df,
                    coluna,
                )
                == ""
            )
        )

    return sem_dados


def calcular_pendencias(df):
    """
    Considera como pendência cadastral:
    - registro sem dados do atendimento;
    - Nome Completo vazio, quando existem outros dados;
    - Atendido Por vazio;
    - Atendido Em vazio;
    - Criado Em ausente ou com data inválida.

    Rascunhos e protocolos duplicados não entram neste cálculo.
    """
    if df.empty:
        return 0

    colunas = localizar_colunas(df)

    sem_dados = mascara_sem_dados_atendimento(df)

    mascara = sem_dados.copy()

    if colunas["nome"]:
        nomes = serie_texto(
            df,
            colunas["nome"],
        )

        mascara = mascara | (
            (nomes == "")
            & (~sem_dados)
        )

    if colunas["usuario"]:
        usuarios = serie_texto(
            df,
            colunas["usuario"],
        )

        mascara = mascara | (
            (usuarios == "")
            & (~sem_dados)
        )

    if colunas["unidade"]:
        unidades = serie_texto(
            df,
            colunas["unidade"],
        )

        mascara = mascara | (
            (unidades == "")
            & (~sem_dados)
        )

    if colunas["criado"]:
        datas_texto = serie_texto(
            df,
            colunas["criado"],
        )

        datas_convertidas = converter_datas(
            df[colunas["criado"]]
        )

        data_invalida = (
            (
                (datas_texto == "")
                | datas_convertidas.isna()
            )
            & (~sem_dados)
        )

        mascara = mascara | data_invalida

    return int(mascara.sum())


def calcular_metricas(df):
    colunas = localizar_colunas(df)

    total = len(df)

    # Pessoas únicas pelo campo Nome Completo.
    pessoas = contar_unicos(
        df,
        colunas["nome"],
    )

    mascara_sem_dados = mascara_sem_dados_atendimento(
        df
    )

    sem_dados = int(
        mascara_sem_dados.sum()
    )

    atendimentos_com_nome = 0
    sem_nome = 0

    if colunas["nome"]:
        nomes = serie_texto(
            df,
            colunas["nome"],
        )

        atendimentos_com_nome = int(
            (nomes != "").sum()
        )

        sem_nome = int(
            (
                (nomes == "")
                & (~mascara_sem_dados)
            ).sum()
        )

    unidades = contar_unicos(
        df,
        colunas["unidade"],
    )

    usuarios = contar_unicos(
        df,
        colunas["usuario"],
    )

    aguardando = 0
    rascunhos = 0

    if colunas["status"]:
        status = (
            serie_texto(
                df,
                colunas["status"],
            )
            .str.lower()
        )

        aguardando = int(
            (status == "aguardando").sum()
        )

        rascunhos = int(
            (status == "rascunho").sum()
        )

    recorrentes = 0

    if colunas["nome"]:
        nomes_normalizados = (
            serie_texto(
                df,
                colunas["nome"],
            )
            .str.lower()
        )

        nomes_normalizados = nomes_normalizados[
            nomes_normalizados != ""
        ]

        if not nomes_normalizados.empty:
            frequencias = (
                nomes_normalizados
                .value_counts()
            )

            recorrentes = int(
                (frequencias > 1).sum()
            )

    pendencias = calcular_pendencias(df)

    return {
        "total": total,
        "pessoas": pessoas,
        "atendimentos_com_nome": atendimentos_com_nome,
        "sem_nome": sem_nome,
        "sem_dados": sem_dados,
        "unidades": unidades,
        "usuarios": usuarios,
        "aguardando": aguardando,
        "rascunhos": rascunhos,
        "recorrentes": recorrentes,
        "pendencias": pendencias,
    }


def tabela_tempo(df, agrupamento):
    colunas = localizar_colunas(df)

    if not colunas["criado"]:
        return pd.DataFrame()

    if "_data_criado" in df.columns:
        datas = df["_data_criado"]
    else:
        datas = converter_datas(
            df[colunas["criado"]]
        )

    datas = datas.dropna()

    if datas.empty:
        return pd.DataFrame()

    base = pd.DataFrame(
        {
            "Data": datas,
        }
    )

    if agrupamento == "semana":
        base["Periodo"] = (
            base["Data"]
            .dt.to_period("W-SUN")
            .dt.start_time
        )

        agrupado = (
            base.groupby("Periodo")
            .size()
            .reset_index(
                name="Atendimentos"
            )
            .sort_values("Periodo")
        )

        agrupado["Label"] = (
            "Semana de "
            + agrupado["Periodo"]
            .dt.strftime("%d/%m/%Y")
        )

    elif agrupamento == "quinzena":
        base["Periodo"] = base["Data"].apply(
            lambda data: pd.Timestamp(
                year=data.year,
                month=data.month,
                day=1 if data.day <= 15 else 16,
            )
        )

        agrupado = (
            base.groupby("Periodo")
            .size()
            .reset_index(
                name="Atendimentos"
            )
            .sort_values("Periodo")
        )

        def rotulo_quinzena(data):
            if data.day == 1:
                return (
                    f"1ª quinzena "
                    f"{data.strftime('%m/%Y')}"
                )

            return (
                f"2ª quinzena "
                f"{data.strftime('%m/%Y')}"
            )

        agrupado["Label"] = (
            agrupado["Periodo"]
            .apply(rotulo_quinzena)
        )

    elif agrupamento == "mes":
        base["Periodo"] = (
            base["Data"]
            .dt.to_period("M")
            .dt.start_time
        )

        agrupado = (
            base.groupby("Periodo")
            .size()
            .reset_index(
                name="Atendimentos"
            )
            .sort_values("Periodo")
        )

        agrupado["Label"] = (
            agrupado["Periodo"]
            .dt.strftime("%m/%Y")
        )

    else:
        base["Periodo"] = (
            base["Data"].dt.normalize()
        )

        agrupado = (
            base.groupby("Periodo")
            .size()
            .reset_index(
                name="Atendimentos"
            )
            .sort_values("Periodo")
        )

        agrupado["Label"] = (
            agrupado["Periodo"]
            .dt.strftime("%d/%m/%Y")
        )

    return agrupado


def grafico_status(df):
    colunas = localizar_colunas(df)

    if not colunas["status"] or df.empty:
        return figura_vazia(
            "Nenhum atendimento encontrado"
        )

    status = serie_texto(
        df,
        colunas["status"],
    ).replace(
        "",
        "Não informado",
    )

    tabela = (
        status.value_counts()
        .reset_index()
    )

    tabela.columns = [
        "Status",
        "Quantidade",
    ]

    figura = px.bar(
        tabela,
        x="Status",
        y="Quantidade",
        text="Quantidade",
        template="plotly_white",
    )

    figura.update_traces(
        textposition="outside",
    )

    figura.update_layout(
        xaxis_title=None,
        yaxis_title="Atendimentos",
        showlegend=False,
        margin=dict(
            l=20,
            r=20,
            t=20,
            b=20,
        ),
        height=330,
    )

    return figura


def grafico_temporal(df, agrupamento):
    tabela = tabela_tempo(
        df,
        agrupamento,
    )

    if tabela.empty:
        return figura_vazia(
            "Nenhuma data válida encontrada"
        )

    figura = px.line(
        tabela,
        x="Label",
        y="Atendimentos",
        markers=True,
        template="plotly_white",
    )

    figura.update_layout(
        xaxis_title=None,
        yaxis_title="Atendimentos",
        showlegend=False,
        margin=dict(
            l=20,
            r=20,
            t=20,
            b=20,
        ),
        height=330,
    )

    return figura


def grafico_por_usuario(df):
    colunas = localizar_colunas(df)

    if not colunas["usuario"] or df.empty:
        return figura_vazia(
            "Nenhum usuário identificado"
        )

    usuarios = serie_texto(
        df,
        colunas["usuario"],
    )

    usuarios = usuarios[
        usuarios != ""
    ]

    if usuarios.empty:
        return figura_vazia(
            "Nenhum usuário identificado"
        )

    tabela = (
        usuarios.value_counts()
        .reset_index()
    )

    tabela.columns = [
        "Usuário",
        "Atendimentos",
    ]

    tabela = tabela.sort_values(
        "Atendimentos",
        ascending=True,
    )

    figura = px.bar(
        tabela,
        x="Atendimentos",
        y="Usuário",
        orientation="h",
        text="Atendimentos",
        template="plotly_white",
    )

    figura.update_layout(
        xaxis_title="Atendimentos",
        yaxis_title=None,
        showlegend=False,
        margin=dict(
            l=20,
            r=20,
            t=20,
            b=20,
        ),
        height=max(
            330,
            min(
                650,
                70 + len(tabela) * 40,
            ),
        ),
    )

    return figura


def grafico_por_unidade(df):
    colunas = localizar_colunas(df)

    if not colunas["unidade"] or df.empty:
        return figura_vazia(
            "Nenhuma unidade identificada"
        )

    unidades = serie_texto(
        df,
        colunas["unidade"],
    )

    unidades = unidades[
        unidades != ""
    ]

    if unidades.empty:
        return figura_vazia(
            "Nenhuma unidade identificada"
        )

    tabela = (
        unidades.value_counts()
        .reset_index()
    )

    tabela.columns = [
        "Unidade",
        "Atendimentos",
    ]

    tabela = tabela.sort_values(
        "Atendimentos",
        ascending=True,
    )

    figura = px.bar(
        tabela,
        x="Atendimentos",
        y="Unidade",
        orientation="h",
        text="Atendimentos",
        template="plotly_white",
    )

    figura.update_layout(
        xaxis_title="Atendimentos",
        yaxis_title=None,
        showlegend=False,
        margin=dict(
            l=20,
            r=20,
            t=20,
            b=20,
        ),
        height=max(
            330,
            min(
                650,
                80 + len(tabela) * 55,
            ),
        ),
    )

    return figura


def criar_opcoes(valores):
    return [
        {
            "label": valor,
            "value": valor,
        }
        for valor in valores
    ]


def ler_arquivo_upload(
    nome_arquivo,
    conteudo_upload,
):
    _, conteudo = (
        conteudo_upload.split(
            ",",
            1,
        )
    )

    dados = base64.b64decode(
        conteudo
    )

    extensao = (
        nome_arquivo
        .lower()
        .split(".")[-1]
    )

    if extensao == "csv":
        ultimo_erro = None

        for encoding in [
            "utf-8-sig",
            "utf-8",
            "latin1",
        ]:
            for separador in [
                ";",
                ",",
                "\t",
            ]:
                try:
                    tentativa = pd.read_csv(
                        io.BytesIO(dados),
                        encoding=encoding,
                        sep=separador,
                    )

                    if len(
                        tentativa.columns
                    ) > 1:
                        df = tentativa
                        break

                except Exception as erro:
                    ultimo_erro = erro
            else:
                continue

            break
        else:
            if ultimo_erro:
                raise ultimo_erro

            raise ValueError(
                "Não foi possível interpretar o CSV."
            )

    elif extensao == "xlsx":
        df = pd.read_excel(
            io.BytesIO(dados)
        )

    elif extensao in [
        "html",
        "htm",
    ]:
        tabelas = pd.read_html(
            io.BytesIO(dados)
        )

        if not tabelas:
            raise ValueError(
                "Nenhuma tabela foi encontrada no HTML."
            )

        df = tabelas[0]

    else:
        raise ValueError(
            "Formato não suportado. Use CSV, HTML ou XLSX."
        )

    df.columns = [
        str(col).strip()
        for col in df.columns
    ]

    return df


# ============================================================
# FUNÇÕES - HISTÓRICO DO ATENDIDO
# ============================================================

def normalizar_texto(texto):
    if pd.isna(texto):
        return ""

    texto = str(texto).strip().lower()
    texto = unicodedata.normalize(
        "NFKD",
        texto,
    )
    texto = "".join(
        caractere
        for caractere in texto
        if not unicodedata.combining(caractere)
    )

    return texto


def classificar_demanda(titulo):
    texto = normalizar_texto(titulo)

    regras = [
        (
            "Banho / higiene",
            [
                "banho",
                "higiene",
                "chuveiro",
            ],
        ),
        (
            "Computador / internet",
            [
                " pc ",
                "pc,",
                "pc.",
                "pc e",
                "pc",
                "computador",
                "internet",
            ],
        ),
        (
            "Ligação / telefone",
            [
                "ligacao",
                "telefone",
                "celular",
            ],
        ),
        (
            "Roupas / lavanderia",
            [
                "roupa",
                "lavar",
                "secar",
                "lavanderia",
            ],
        ),
        (
            "Cobertor / agasalho",
            [
                "cobertor",
                "manta",
                "agasalho",
            ],
        ),
        (
            "Saúde",
            [
                "infectologia",
                "saude",
                "medico",
                "medica",
                "consulta",
                "hospital",
                "exame",
                "remedio",
                "posto de saude",
            ],
        ),
        (
            "Assistência social",
            [
                "assistente social",
                "servico social",
            ],
        ),
        (
            "Documentação",
            [
                "documento",
                "documentacao",
                "rg",
                "cpf",
                "certidao",
            ],
        ),
        (
            "Benefícios / renda",
            [
                "beneficio",
                "auxilio",
                "emprestimo",
                "consignado",
                "renda",
            ],
        ),
        (
            "Banheiro",
            [
                "banheiro",
            ],
        ),
        (
            "Alimentação",
            [
                "alimentacao",
                "refeicao",
                "comida",
                "lanche",
            ],
        ),
    ]

    categorias = []

    for categoria, palavras in regras:
        if any(
            palavra in texto
            for palavra in palavras
        ):
            categorias.append(categoria)

    if not categorias:
        categorias.append("Não classificado")

    return categorias


def localizar_colunas_historico(df):
    mapa = {
        str(col).strip().lower(): col
        for col in df.columns
    }

    return {
        "protocolo": mapa.get("nr protocolo"),
        "titulo": mapa.get("ds titulo"),
        "data_inicio": mapa.get("dt data inicio"),
        "data_fim": mapa.get("dt data fim"),
        "usuario_criacao": mapa.get("co usuario criacao"),
        "status": mapa.get("co status"),
        "tecnico": mapa.get("ds tecnico"),
        "criacao": mapa.get("dh criacao"),
    }


def preparar_historico_atendido(df):
    df = df.copy()
    colunas = localizar_colunas_historico(df)

    if not colunas["titulo"]:
        raise ValueError(
            "A coluna 'Ds Titulo' não foi encontrada no arquivo."
        )

    titulos = (
        df[colunas["titulo"]]
        .fillna("")
        .astype(str)
        .str.strip()
    )

    df["_categorias_lista"] = titulos.apply(
        classificar_demanda
    )

    df["Categorias detectadas"] = (
        df["_categorias_lista"]
        .apply(lambda categorias: " + ".join(categorias))
    )

    if colunas["data_inicio"]:
        df["_data_historico"] = pd.to_datetime(
            df[colunas["data_inicio"]],
            errors="coerce",
            dayfirst=True,
        )
    else:
        df["_data_historico"] = pd.NaT

    return df


def grafico_demandas_historico(df):
    if df.empty or "_categorias_lista" not in df.columns:
        return figura_vazia(
            "Nenhuma demanda identificada"
        )

    categorias = (
        df["_categorias_lista"]
        .explode()
        .dropna()
    )

    if categorias.empty:
        return figura_vazia(
            "Nenhuma demanda identificada"
        )

    tabela = (
        categorias
        .value_counts()
        .reset_index()
    )
    tabela.columns = [
        "Demanda",
        "Registros",
    ]
    tabela = tabela.sort_values(
        "Registros",
        ascending=True,
    )

    figura = px.bar(
        tabela,
        x="Registros",
        y="Demanda",
        orientation="h",
        text="Registros",
        template="plotly_white",
    )

    figura.update_layout(
        xaxis_title="Registros",
        yaxis_title=None,
        showlegend=False,
        margin=dict(
            l=20,
            r=20,
            t=20,
            b=20,
        ),
        height=max(
            330,
            min(
                620,
                90 + len(tabela) * 42,
            ),
        ),
    )

    return figura


def grafico_tempo_historico(df):
    if df.empty or "_data_historico" not in df.columns:
        return figura_vazia(
            "Nenhuma data válida encontrada"
        )

    datas = df["_data_historico"].dropna()

    if datas.empty:
        return figura_vazia(
            "Nenhuma data válida encontrada"
        )

    tabela = (
        datas.dt.normalize()
        .value_counts()
        .sort_index()
        .reset_index()
    )
    tabela.columns = [
        "Data",
        "Registros",
    ]
    tabela["Data exibida"] = (
        tabela["Data"]
        .dt.strftime("%d/%m/%Y")
    )

    figura = px.line(
        tabela,
        x="Data exibida",
        y="Registros",
        markers=True,
        template="plotly_white",
    )

    figura.update_layout(
        xaxis_title=None,
        yaxis_title="Registros",
        showlegend=False,
        margin=dict(
            l=20,
            r=20,
            t=20,
            b=20,
        ),
        height=330,
    )

    return figura


def criar_resumo_usuarios(df):
    """
    Gera uma tabela consolidada por usuário/profissional
    usando a coluna 'Atendido Por'.
    """
    colunas_saida = [
        "Usuário",
        "Atendimentos",
        "Pessoas únicas",
        "Aguardando",
        "Rascunhos",
        "Unidades",
        "Participação (%)",
        "Primeiro atendimento",
        "Último atendimento",
    ]

    if df.empty:
        return pd.DataFrame(
            columns=colunas_saida
        )

    colunas = localizar_colunas(df)

    if not colunas["usuario"]:
        return pd.DataFrame(
            columns=colunas_saida
        )

    base = df.copy()

    base["_usuario_resumo"] = serie_texto(
        base,
        colunas["usuario"],
    )

    base = base[
        base["_usuario_resumo"] != ""
    ].copy()

    if base.empty:
        return pd.DataFrame(
            columns=colunas_saida
        )

    if colunas["criado"]:
        if "_data_criado" not in base.columns:
            base["_data_criado"] = converter_datas(
                base[colunas["criado"]]
            )
    else:
        base["_data_criado"] = pd.NaT

    total_geral = len(base)
    registros = []

    for usuario, grupo in base.groupby(
        "_usuario_resumo",
        dropna=False,
    ):
        atendimentos = len(grupo)

        pessoas = contar_unicos(
            grupo,
            colunas["nome"],
        )

        unidades = contar_unicos(
            grupo,
            colunas["unidade"],
        )

        aguardando = 0
        rascunhos = 0

        if colunas["status"]:
            status = (
                serie_texto(
                    grupo,
                    colunas["status"],
                )
                .str.lower()
            )

            aguardando = int(
                (status == "aguardando").sum()
            )

            rascunhos = int(
                (status == "rascunho").sum()
            )

        datas = (
            grupo["_data_criado"]
            .dropna()
        )

        if datas.empty:
            primeira_data = ""
            ultima_data = ""
        else:
            primeira_data = (
                datas.min()
                .strftime("%d/%m/%Y")
            )
            ultima_data = (
                datas.max()
                .strftime("%d/%m/%Y")
            )

        participacao = (
            (atendimentos / total_geral) * 100
            if total_geral
            else 0
        )

        registros.append(
            {
                "Usuário": usuario,
                "Atendimentos": int(
                    atendimentos
                ),
                "Pessoas únicas": int(
                    pessoas
                ),
                "Aguardando": int(
                    aguardando
                ),
                "Rascunhos": int(
                    rascunhos
                ),
                "Unidades": int(
                    unidades
                ),
                "Participação (%)": round(
                    participacao,
                    1,
                ),
                "Primeiro atendimento": primeira_data,
                "Último atendimento": ultima_data,
            }
        )

    resumo = pd.DataFrame(
        registros
    )

    if resumo.empty:
        return resumo

    return (
        resumo.sort_values(
            [
                "Atendimentos",
                "Usuário",
            ],
            ascending=[
                False,
                True,
            ],
        )
        .reset_index(
            drop=True
        )
    )


def normalizar_serie_auditoria(serie):
    """
    Normaliza textos para comparações de auditoria,
    sem alterar os dados originais exibidos ao usuário.
    """
    return (
        serie
        .fillna("")
        .astype(str)
        .str.strip()
        .str.lower()
        .str.normalize("NFKD")
        .str.encode(
            "ascii",
            errors="ignore",
        )
        .str.decode("utf-8")
        .str.replace(
            r"\s+",
            " ",
            regex=True,
        )
    )


def analisar_inconsistencias(df):
    """
    Auditoria simplificada.

    Considera como inconsistência somente:
    - Cadastro incompleto: registro sem nenhum dado essencial
      do atendimento;
    - Sem nome: existem outros dados no atendimento, mas
      'Nome Completo' está vazio;
    - Protocolo duplicado;
    - Possível atendimento duplicado.

    Rascunho, ausência de responsável, ausência de unidade
    e problemas de data não entram mais na contagem principal
    da aba Auditoria.
    """
    base = df.copy()

    if base.empty:
        base["_ocorrencias_lista"] = pd.Series(
            dtype="object"
        )
        base["Ocorrências encontradas"] = pd.Series(
            dtype="object"
        )
        base["Quantidade de ocorrências"] = pd.Series(
            dtype="int64"
        )
        base["Severidade"] = pd.Series(
            dtype="object"
        )
        return base

    colunas = localizar_colunas(base)

    registro_sem_dados = (
        mascara_sem_dados_atendimento(
            base
        )
    )

    ocorrencias = {
        indice: []
        for indice in base.index
    }

    def adicionar(mask, descricao):
        if mask is None:
            return

        for indice in base.index[mask]:
            ocorrencias[indice].append(
                descricao
            )

    # --------------------------------------------------------
    # CADASTRO INCOMPLETO = SEM NENHUM DADO ESSENCIAL
    # --------------------------------------------------------
    adicionar(
        registro_sem_dados,
        "Cadastro incompleto",
    )

    # --------------------------------------------------------
    # SEM NOME = TEM OUTROS DADOS, MAS NÃO TEM NOME
    # --------------------------------------------------------
    if colunas["nome"]:
        nomes = serie_texto(
            base,
            colunas["nome"],
        )

        adicionar(
            (
                (nomes == "")
                & (~registro_sem_dados)
            ),
            "Sem nome",
        )

    # --------------------------------------------------------
    # PROTOCOLO DUPLICADO
    # --------------------------------------------------------
    protocolo_duplicado = pd.Series(
        False,
        index=base.index,
    )

    if colunas["protocolo"]:
        protocolos = serie_texto(
            base,
            colunas["protocolo"],
        )

        protocolo_duplicado = (
            (protocolos != "")
            & protocolos.duplicated(
                keep=False
            )
        )

        adicionar(
            protocolo_duplicado,
            "Protocolo duplicado",
        )

    # --------------------------------------------------------
    # POSSÍVEL ATENDIMENTO DUPLICADO
    #
    # Critério:
    # mesma pessoa + mesmo dia + mesma unidade +
    # mesmo responsável + mesmo status.
    #
    # É tratado como "Atenção", pois pode ser legítimo.
    # --------------------------------------------------------
    if (
        colunas["nome"]
        and colunas["criado"]
    ):
        nomes_norm = normalizar_serie_auditoria(
            base[colunas["nome"]]
        )

        datas_criacao = converter_datas(
            base[colunas["criado"]]
        )

        if colunas["unidade"]:
            unidades_norm = (
                normalizar_serie_auditoria(
                    base[colunas["unidade"]]
                )
            )
        else:
            unidades_norm = pd.Series(
                "",
                index=base.index,
            )

        if colunas["usuario"]:
            usuarios_norm = (
                normalizar_serie_auditoria(
                    base[colunas["usuario"]]
                )
            )
        else:
            usuarios_norm = pd.Series(
                "",
                index=base.index,
            )

        if colunas["status"]:
            status_norm = (
                normalizar_serie_auditoria(
                    base[colunas["status"]]
                )
            )
        else:
            status_norm = pd.Series(
                "",
                index=base.index,
            )

        chave = pd.DataFrame(
            {
                "nome": nomes_norm,
                "data": (
                    datas_criacao
                    .dt.strftime(
                        "%Y-%m-%d"
                    )
                    .fillna("")
                ),
                "unidade": unidades_norm,
                "usuario": usuarios_norm,
                "status": status_norm,
            },
            index=base.index,
        )

        chave_valida = (
            (chave["nome"] != "")
            & (chave["data"] != "")
            & (chave["unidade"] != "")
            & (chave["usuario"] != "")
            & (~registro_sem_dados)
        )

        possivel_duplicado = (
            chave_valida
            & chave.duplicated(
                subset=[
                    "nome",
                    "data",
                    "unidade",
                    "usuario",
                    "status",
                ],
                keep=False,
            )
            & (~protocolo_duplicado)
        )

        adicionar(
            possivel_duplicado,
            "Possível atendimento duplicado",
        )

    # --------------------------------------------------------
    # RESULTADO
    # --------------------------------------------------------
    base["_ocorrencias_lista"] = [
        ocorrencias[indice]
        for indice in base.index
    ]

    base["Ocorrências encontradas"] = [
        " | ".join(lista)
        if lista
        else ""
        for lista in base["_ocorrencias_lista"]
    ]

    base["Quantidade de ocorrências"] = [
        len(lista)
        for lista in base["_ocorrencias_lista"]
    ]

    severidades = []

    for lista in base["_ocorrencias_lista"]:
        if not lista:
            severidades.append(
                ""
            )

        elif any(
            item in {
                "Cadastro incompleto",
                "Sem nome",
                "Protocolo duplicado",
            }
            for item in lista
        ):
            severidades.append(
                "Erro"
            )

        else:
            severidades.append(
                "Atenção"
            )

    base["Severidade"] = severidades

    return base

def contar_tipo_auditoria(
    df_auditoria,
    descricao,
):
    if (
        df_auditoria.empty
        or "_ocorrencias_lista"
        not in df_auditoria.columns
    ):
        return 0

    return int(
        df_auditoria[
            "_ocorrencias_lista"
        ].apply(
            lambda lista: (
                descricao in lista
            )
        ).sum()
    )


def grafico_ocorrencias_auditoria(
    df_auditoria,
):
    if (
        df_auditoria.empty
        or "_ocorrencias_lista"
        not in df_auditoria.columns
    ):
        return figura_vazia(
            "Nenhuma inconsistência encontrada"
        )

    ocorrencias = []

    for lista in df_auditoria[
        "_ocorrencias_lista"
    ]:
        ocorrencias.extend(
            lista
        )

    if not ocorrencias:
        return figura_vazia(
            "Nenhuma inconsistência encontrada"
        )

    tabela = (
        pd.Series(
            ocorrencias
        )
        .value_counts()
        .reset_index()
    )

    tabela.columns = [
        "Ocorrência",
        "Quantidade",
    ]

    tabela = tabela.sort_values(
        "Quantidade",
        ascending=True,
    )

    figura = px.bar(
        tabela,
        x="Quantidade",
        y="Ocorrência",
        orientation="h",
        text="Quantidade",
        template="plotly_white",
    )

    figura.update_layout(
        xaxis_title="Registros",
        yaxis_title=None,
        showlegend=False,
        margin=dict(
            l=20,
            r=20,
            t=20,
            b=20,
        ),
        height=max(
            360,
            min(
                650,
                100 + len(tabela) * 42,
            ),
        ),
    )

    return figura


def grafico_auditoria_por_unidade(
    df_auditoria,
):
    if df_auditoria.empty:
        return figura_vazia(
            "Nenhuma inconsistência encontrada"
        )

    colunas = localizar_colunas(
        df_auditoria
    )

    if not colunas["unidade"]:
        return figura_vazia(
            "Coluna de unidade não encontrada"
        )

    unidades = serie_texto(
        df_auditoria,
        colunas["unidade"],
    ).replace(
        "",
        "Sem unidade",
    )

    tabela = (
        unidades
        .value_counts()
        .reset_index()
    )

    tabela.columns = [
        "Unidade",
        "Registros com inconsistência",
    ]

    tabela = tabela.sort_values(
        "Registros com inconsistência",
        ascending=True,
    )

    figura = px.bar(
        tabela,
        x="Registros com inconsistência",
        y="Unidade",
        orientation="h",
        text="Registros com inconsistência",
        template="plotly_white",
    )

    figura.update_layout(
        xaxis_title="Registros",
        yaxis_title=None,
        showlegend=False,
        margin=dict(
            l=20,
            r=20,
            t=20,
            b=20,
        ),
        height=max(
            360,
            min(
                650,
                100 + len(tabela) * 55,
            ),
        ),
    )

    return figura


def criar_resumo_unidades(df):
    """
    Cria uma visão consolidada por valor da coluna 'Atendido Em'.

    Enquanto não existir um cadastro auxiliar OSC x Unidade,
    o valor completo de 'Atendido Em' é tratado como a
    identificação oficial da unidade/OSC no painel.
    """
    colunas_saida = [
        "Unidade / OSC",
        "Atendimentos",
        "Pessoas únicas",
        "Usuários ativos",
        "Aguardando",
        "Rascunhos",
        "Pendências cadastrais",
        "Participação (%)",
        "Primeiro atendimento",
        "Último atendimento",
    ]

    if df.empty:
        return pd.DataFrame(
            columns=colunas_saida
        )

    colunas = localizar_colunas(df)

    if not colunas["unidade"]:
        return pd.DataFrame(
            columns=colunas_saida
        )

    base = df.copy()

    base["_unidade_resumo"] = serie_texto(
        base,
        colunas["unidade"],
    )

    base = base[
        base["_unidade_resumo"] != ""
    ].copy()

    if base.empty:
        return pd.DataFrame(
            columns=colunas_saida
        )

    if colunas["criado"]:
        if "_data_criado" not in base.columns:
            base["_data_criado"] = converter_datas(
                base[colunas["criado"]]
            )
    else:
        base["_data_criado"] = pd.NaT

    total_geral = len(base)
    registros = []

    for unidade, grupo in base.groupby(
        "_unidade_resumo",
        dropna=False,
    ):
        metricas = calcular_metricas(
            grupo
        )

        datas = (
            grupo["_data_criado"]
            .dropna()
        )

        if datas.empty:
            primeira_data = ""
            ultima_data = ""
        else:
            primeira_data = (
                datas.min()
                .strftime("%d/%m/%Y")
            )

            ultima_data = (
                datas.max()
                .strftime("%d/%m/%Y")
            )

        participacao = (
            (
                metricas["total"]
                / total_geral
            )
            * 100
            if total_geral
            else 0
        )

        registros.append(
            {
                "Unidade / OSC": unidade,
                "Atendimentos": int(
                    metricas["total"]
                ),
                "Pessoas únicas": int(
                    metricas["pessoas"]
                ),
                "Usuários ativos": int(
                    metricas["usuarios"]
                ),
                "Aguardando": int(
                    metricas["aguardando"]
                ),
                "Rascunhos": int(
                    metricas["rascunhos"]
                ),
                "Pendências cadastrais": int(
                    metricas["pendencias"]
                ),
                "Participação (%)": round(
                    participacao,
                    1,
                ),
                "Primeiro atendimento": primeira_data,
                "Último atendimento": ultima_data,
            }
        )

    resumo = pd.DataFrame(
        registros
    )

    if resumo.empty:
        return resumo

    return (
        resumo.sort_values(
            [
                "Atendimentos",
                "Unidade / OSC",
            ],
            ascending=[
                False,
                True,
            ],
        )
        .reset_index(
            drop=True
        )
    )


def grafico_pessoas_por_unidade(df):
    resumo = criar_resumo_unidades(
        df
    )

    if resumo.empty:
        return figura_vazia(
            "Nenhuma unidade identificada"
        )

    tabela = resumo[
        [
            "Unidade / OSC",
            "Pessoas únicas",
        ]
    ].sort_values(
        "Pessoas únicas",
        ascending=True,
    )

    figura = px.bar(
        tabela,
        x="Pessoas únicas",
        y="Unidade / OSC",
        orientation="h",
        text="Pessoas únicas",
        template="plotly_white",
    )

    figura.update_layout(
        xaxis_title="Pessoas únicas",
        yaxis_title=None,
        showlegend=False,
        margin=dict(
            l=20,
            r=20,
            t=20,
            b=20,
        ),
        height=max(
            360,
            min(
                800,
                100 + len(tabela) * 52,
            ),
        ),
    )

    return figura


def grafico_usuarios_por_unidade(df):
    resumo = criar_resumo_unidades(
        df
    )

    if resumo.empty:
        return figura_vazia(
            "Nenhuma unidade identificada"
        )

    tabela = resumo[
        [
            "Unidade / OSC",
            "Usuários ativos",
        ]
    ].sort_values(
        "Usuários ativos",
        ascending=True,
    )

    figura = px.bar(
        tabela,
        x="Usuários ativos",
        y="Unidade / OSC",
        orientation="h",
        text="Usuários ativos",
        template="plotly_white",
    )

    figura.update_layout(
        xaxis_title="Usuários ativos",
        yaxis_title=None,
        showlegend=False,
        margin=dict(
            l=20,
            r=20,
            t=20,
            b=20,
        ),
        height=max(
            360,
            min(
                800,
                100 + len(tabela) * 52,
            ),
        ),
    )

    return figura


def grafico_evolucao_unidades(
    df,
    agrupamento,
):
    if df.empty:
        return figura_vazia(
            "Nenhum atendimento encontrado"
        )

    colunas = localizar_colunas(
        df
    )

    if (
        not colunas["unidade"]
        or not colunas["criado"]
    ):
        return figura_vazia(
            "Colunas de unidade ou data não encontradas"
        )

    base = pd.DataFrame(
        {
            "Unidade / OSC": serie_texto(
                df,
                colunas["unidade"],
            ),
            "Data": converter_datas(
                df[colunas["criado"]]
            ),
        }
    )

    base = base[
        (
            base["Unidade / OSC"]
            != ""
        )
        & base["Data"].notna()
    ].copy()

    if base.empty:
        return figura_vazia(
            "Nenhuma data válida encontrada"
        )

    if agrupamento == "semana":
        base["Periodo"] = (
            base["Data"]
            .dt.to_period("W-SUN")
            .dt.start_time
        )

        base["Label"] = (
            "Semana de "
            + base["Periodo"]
            .dt.strftime("%d/%m/%Y")
        )

    elif agrupamento == "quinzena":
        base["Periodo"] = base[
            "Data"
        ].apply(
            lambda data: pd.Timestamp(
                year=data.year,
                month=data.month,
                day=(
                    1
                    if data.day <= 15
                    else 16
                ),
            )
        )

        base["Label"] = base[
            "Periodo"
        ].apply(
            lambda data: (
                (
                    "1ª quinzena "
                    if data.day == 1
                    else "2ª quinzena "
                )
                + data.strftime(
                    "%m/%Y"
                )
            )
        )

    elif agrupamento == "mes":
        base["Periodo"] = (
            base["Data"]
            .dt.to_period("M")
            .dt.start_time
        )

        base["Label"] = (
            base["Periodo"]
            .dt.strftime("%m/%Y")
        )

    else:
        base["Periodo"] = (
            base["Data"]
            .dt.normalize()
        )

        base["Label"] = (
            base["Periodo"]
            .dt.strftime("%d/%m/%Y")
        )

    # Mantém legibilidade do gráfico quando há muitas unidades:
    # mostra no gráfico as 10 com maior volume, mas a tabela
    # consolidada continua contendo todas.
    ranking_unidades = (
        base["Unidade / OSC"]
        .value_counts()
    )

    unidades_grafico = (
        ranking_unidades
        .head(10)
        .index
        .tolist()
    )

    base = base[
        base["Unidade / OSC"]
        .isin(
            unidades_grafico
        )
    ].copy()

    agrupado = (
        base.groupby(
            [
                "Periodo",
                "Label",
                "Unidade / OSC",
            ]
        )
        .size()
        .reset_index(
            name="Atendimentos"
        )
        .sort_values(
            [
                "Periodo",
                "Unidade / OSC",
            ]
        )
    )

    figura = px.line(
        agrupado,
        x="Label",
        y="Atendimentos",
        color="Unidade / OSC",
        markers=True,
        template="plotly_white",
    )

    figura.update_layout(
        xaxis_title=None,
        yaxis_title="Atendimentos",
        legend_title_text="Unidade / OSC",
        margin=dict(
            l=20,
            r=20,
            t=20,
            b=20,
        ),
        height=430,
    )

    return figura


# ============================================================
# NAVBAR
# ============================================================

navbar = dbc.Navbar(
    dbc.Container(
        [
            dbc.NavbarBrand(
                [
                    html.Div(
                        [
                            html.I(
                                className=(
                                    "fa-solid "
                                    "fa-chart-line "
                                    "me-2"
                                ),
                                style={"color": "#FFCD07"},
                            ),
                            html.Span(NOME_SISTEMA),
                        ],
                        className="d-flex align-items-center",
                    ),
                ],
                className="fw-bold",
                href="/",
                style={
                    "color": "#FFFFFF",
                    "fontSize": "1.12rem",
                    "letterSpacing": "0.1px",
                },
            ),
            dbc.Nav(
                [
                    dbc.NavItem(dbc.NavLink("Início", href="/", active="exact", className="px-3", style={"color": "#FFFFFF", "fontWeight": "600"})),
                    dbc.NavItem(dbc.NavLink("Unidades", href="/unidades", className="px-3", style={"color": "#FFFFFF", "fontWeight": "600"})),
                    dbc.NavItem(dbc.NavLink("Atendimentos", href="/atendimentos", className="px-3", style={"color": "#FFFFFF", "fontWeight": "600"})),
                    dbc.NavItem(dbc.NavLink("Usuários", href="/usuarios", className="px-3", style={"color": "#FFFFFF", "fontWeight": "600"})),
                    dbc.NavItem(dbc.NavLink("Auditoria", href="/auditoria", className="px-3", style={"color": "#FFFFFF", "fontWeight": "600"})),
                    dbc.NavItem(dbc.NavLink("Base de Dados", href="/base", className="px-3", style={"color": "#FFFFFF", "fontWeight": "600"})),
                    dbc.NavItem(dbc.NavLink("Relatórios", href="/relatorios", className="px-3", style={"color": "#FFFFFF", "fontWeight": "600"})),
                ],
                className="ms-auto",
                navbar=True,
            ),
        ],
        fluid=True,
        className="px-4",
    ),
    dark=True,
    className="mb-4",
    style={
        "background": (
            "linear-gradient("
            "110deg, "
            "#168821 0%, "
            "#116B37 45%, "
            "#1351B4 100%"
            ")"
        ),
        "borderBottom": "5px solid #FFCD07",
        "boxShadow": "0 4px 14px rgba(7, 29, 65, 0.18)",
        "minHeight": "68px",
    },
)


# ============================================================
# COMPONENTES DE FILTRO
# ============================================================

def bloco_filtros_home():
    return dbc.Card(
        dbc.CardBody(
            [
                html.Div(
                    [
                        html.H5(
                            "Filtros do painel",
                            className="fw-bold mb-1",
                        ),
                        html.P(
                            (
                                "Os filtros abaixo alteram os indicadores "
                                "e gráficos da página inicial."
                            ),
                            className="text-muted mb-0",
                        ),
                    ],
                    className="mb-3",
                ),

                dbc.Row(
                    [
                        dbc.Col(
                            [
                                dbc.Label(
                                    "Unidade",
                                    className="fw-semibold",
                                ),
                                dcc.Dropdown(
                                    id="filtro-unidade-home",
                                    placeholder="Todas as unidades",
                                    clearable=True,
                                ),
                            ],
                            xs=12,
                            md=6,
                            xl=3,
                            className="mb-3",
                        ),

                        dbc.Col(
                            [
                                dbc.Label(
                                    "Usuário",
                                    className="fw-semibold",
                                ),
                                dcc.Dropdown(
                                    id="filtro-usuario-home",
                                    placeholder="Todos os usuários",
                                    clearable=True,
                                ),
                            ],
                            xs=12,
                            md=6,
                            xl=3,
                            className="mb-3",
                        ),

                        dbc.Col(
                            [
                                dbc.Label(
                                    "Status",
                                    className="fw-semibold",
                                ),
                                dcc.Dropdown(
                                    id="filtro-status-home",
                                    placeholder="Todos os status",
                                    clearable=True,
                                ),
                            ],
                            xs=12,
                            md=6,
                            xl=3,
                            className="mb-3",
                        ),

                        dbc.Col(
                            [
                                dbc.Label(
                                    "Agrupar por",
                                    className="fw-semibold",
                                ),
                                dcc.Dropdown(
                                    id="agrupamento-home",
                                    options=[
                                        {
                                            "label": "Dia",
                                            "value": "dia",
                                        },
                                        {
                                            "label": "Semana",
                                            "value": "semana",
                                        },
                                        {
                                            "label": "Quinzena",
                                            "value": "quinzena",
                                        },
                                        {
                                            "label": "Mês",
                                            "value": "mes",
                                        },
                                    ],
                                    value="dia",
                                    clearable=False,
                                ),
                            ],
                            xs=12,
                            md=6,
                            xl=3,
                            className="mb-3",
                        ),
                    ],
                    className="g-3",
                ),

                dbc.Row(
                    [
                        dbc.Col(
                            [
                                dbc.Label(
                                    "Período",
                                    className="fw-semibold",
                                ),
                                dcc.DatePickerRange(
                                    id="filtro-periodo-home",
                                    display_format="DD/MM/YYYY",
                                    start_date_placeholder_text="Data inicial",
                                    end_date_placeholder_text="Data final",
                                    clearable=True,
                                ),
                            ],
                            xs=12,
                            lg=8,
                            className="mb-2",
                        ),

                        dbc.Col(
                            dbc.Button(
                                [
                                    html.I(
                                        className=(
                                            "fa-solid "
                                            "fa-filter-circle-xmark "
                                            "me-2"
                                        )
                                    ),
                                    "Limpar filtros",
                                ],
                                id="botao-limpar-filtros-home",
                                color="secondary",
                                outline=True,
                                className="mt-4",
                            ),
                            xs=12,
                            lg=4,
                            className="mb-2",
                        ),
                    ],
                    className="g-3",
                ),
            ],
            className="p-4",
        ),
        className="shadow-sm border-0 rounded-4 mb-4",
    )


# ============================================================
# HOME
# ============================================================

home_layout = dbc.Container(
    [
        dbc.Row(
            [
                dbc.Col(
                    [
                        html.H1(
                            "Monitoramento Cidadania",
                            className="fw-bold mb-2",
                            style={
                                "color": "#071D41",
                                "letterSpacing": "-0.4px",
                            },
                        ),

                        html.P(
                            (
                                "Painel de acompanhamento e monitoramento "
                                "dos dados registrados no Sistema CAIS."
                            ),
                            className="text-muted fs-5 mb-0",
                        ),

                        html.Div(
                            [
                                dbc.Badge(
                                    [
                                        html.I(
                                            className=(
                                                "fa-solid "
                                                "fa-circle-check "
                                                "me-2"
                                            )
                                        ),
                                        "Sistema operacional",
                                    ],
                                    color="success",
                                    className="px-3 py-2",
                                    style={
                                        "backgroundColor": "#168821",
                                        "border": "none",
                                    },
                                ),

                                html.Span(
                                    (
                                        "Última atualização: "
                                        "aguardando dados"
                                    ),
                                    id="ultima-atualizacao",
                                    className="text-muted ms-3 small",
                                ),
                            ],
                            className="mt-3",
                        ),
                    ],
                    width=12,
                ),
            ],
            className="mb-4",
        ),

        dbc.Alert(
            [
                html.Div(
                    [
                        html.I(
                            className=(
                                "fa-solid "
                                "fa-location-dot "
                                "me-2"
                            )
                        ),
                        html.Strong(
                            "Unidade(s) analisada(s)"
                        ),
                    ],
                    className="mb-1",
                ),

                html.Div(
                    "Aguardando dados",
                    id="identificador-unidade-home",
                    className="small",
                ),
            ],
            color="light",
            className="border rounded-4 mb-4",
        ),

        bloco_filtros_home(),

        # ====================================================
        # IMPORTAÇÃO PRINCIPAL DE DADOS
        # ====================================================

        dbc.Card(
            dbc.CardBody(
                [
                    html.Div(
                        [
                            html.Div(
                                html.I(
                                    className=(
                                        "fa-solid "
                                        "fa-file-import "
                                        "fa-lg"
                                    )
                                ),
                                className=(
                                    "d-flex align-items-center "
                                    "justify-content-center "
                                    "rounded-3 bg-light"
                                ),
                                style={
                                    "width": "48px",
                                    "height": "48px",
                                    "minWidth": "48px",
                                },
                            ),

                            html.Div(
                                [
                                    html.H4(
                                        "Importação de dados",
                                        className="fw-bold mb-1",
                                    ),

                                    html.P(
                                        (
                                            "Atualize o painel utilizando "
                                            "os arquivos exportados do "
                                            "Sistema CAIS."
                                        ),
                                        className="text-muted mb-0",
                                    ),
                                ],
                                className="ms-3",
                            ),
                        ],
                        className=(
                            "d-flex align-items-center "
                            "mb-4"
                        ),
                    ),

                    dcc.Upload(
                        id="upload-dados-home",
                        children=html.Div(
                            [
                                html.I(
                                    className=(
                                        "fa-solid "
                                        "fa-cloud-arrow-up "
                                        "fa-3x mb-3"
                                    )
                                ),

                                html.H5(
                                    "Arraste um ou vários arquivos aqui",
                                    className="fw-bold mb-2",
                                ),

                                html.P(
                                    "ou clique para selecionar os arquivos",
                                    className="text-muted mb-2",
                                ),

                                html.Small(
                                    [
                                        "Formatos aceitos: ",
                                        html.Strong(
                                            "CSV, HTML e XLSX"
                                        ),
                                    ],
                                    className="text-muted",
                                ),
                            ],
                            className="py-3",
                        ),
                        style={
                            "width": "100%",
                            "minHeight": "180px",
                            "borderWidth": "2px",
                            "borderStyle": "dashed",
                            "borderRadius": "16px",
                            "textAlign": "center",
                            "padding": "30px 20px",
                            "cursor": "pointer",
                            "display": "flex",
                            "alignItems": "center",
                            "justifyContent": "center",
                        },
                        multiple=True,
                    ),

                    html.Div(
                        id="mensagem-upload-home",
                        className="mt-3",
                    ),
                ],
                className="p-4",
            ),
            className=(
                "shadow-sm border-0 "
                "rounded-4 mb-4"
            ),
        ),



        dbc.Row(
            [
                dbc.Col(
                    criar_card(
                        "Unidades",
                        "0",
                        "fa-solid fa-building",
                        "card-unidades",
                    ),
                    xs=12,
                    sm=6,
                    lg=3,
                    className="mb-3",
                ),

                dbc.Col(
                    criar_card(
                        "Usuários",
                        "0",
                        "fa-solid fa-users",
                        "card-usuarios",
                    ),
                    xs=12,
                    sm=6,
                    lg=3,
                    className="mb-3",
                ),

                dbc.Col(
                    criar_card(
                        "Atendimentos",
                        "0",
                        "fa-solid fa-clipboard-list",
                        "card-atendimentos",
                    ),
                    xs=12,
                    sm=6,
                    lg=3,
                    className="mb-3",
                ),

                dbc.Col(
                    criar_card(
                        "Pendências",
                        "0",
                        (
                            "fa-solid "
                            "fa-triangle-exclamation"
                        ),
                        "card-pendencias",
                    ),
                    xs=12,
                    sm=6,
                    lg=3,
                    className="mb-3",
                ),
            ],
            className="g-3 mb-4",
        ),

        html.Div(
            [
                html.H4(
                    "Indicadores de atendimentos",
                    className="fw-bold mb-1",
                ),

                html.P(
                    (
                        "Resumo do período e dos filtros "
                        "selecionados."
                    ),
                    className="text-muted mb-3",
                ),

                dbc.Row(
                    [
                        dbc.Col(
                            criar_card(
                                "Pessoas únicas",
                                "0",
                                (
                                    "fa-solid "
                                    "fa-user-group"
                                ),
                                "card-pessoas-atendidas",
                            ),
                            xs=12,
                            sm=6,
                            lg=3,
                            className="mb-3",
                        ),

                        dbc.Col(
                            criar_card(
                                "Aguardando",
                                "0",
                                "fa-solid fa-clock",
                                "card-aguardando",
                            ),
                            xs=12,
                            sm=6,
                            lg=3,
                            className="mb-3",
                        ),

                        dbc.Col(
                            criar_card(
                                "Rascunhos",
                                "0",
                                (
                                    "fa-solid "
                                    "fa-pen-to-square"
                                ),
                                "card-rascunhos",
                            ),
                            xs=12,
                            sm=6,
                            lg=3,
                            className="mb-3",
                        ),

                        dbc.Col(
                            criar_card(
                                "Pessoas recorrentes",
                                "0",
                                (
                                    "fa-solid "
                                    "fa-rotate"
                                ),
                                "card-recorrentes",
                            ),
                            xs=12,
                            sm=6,
                            lg=3,
                            className="mb-3",
                        ),
                    ],
                    className="g-3 mb-3",
                ),

                html.H5(
                    "Qualidade da identificação",
                    className="fw-bold mt-3 mb-1",
                ),

                html.P(
                    (
                        "Diferencia registros com nome, pessoas únicas "
                        "e falhas de identificação."
                    ),
                    className="text-muted mb-3",
                ),

                dbc.Row(
                    [
                        dbc.Col(
                            criar_card(
                                "Atendimentos com nome",
                                "0",
                                "fa-solid fa-address-card",
                                "card-atendimentos-com-nome",
                            ),
                            xs=12,
                            sm=6,
                            lg=4,
                            className="mb-3",
                        ),

                        dbc.Col(
                            criar_card(
                                "Sem nome",
                                "0",
                                "fa-solid fa-user-slash",
                                "card-sem-nome-home",
                            ),
                            xs=12,
                            sm=6,
                            lg=4,
                            className="mb-3",
                        ),

                        dbc.Col(
                            criar_card(
                                "Sem dados do atendimento",
                                "0",
                                "fa-solid fa-file-circle-xmark",
                                "card-sem-dados-home",
                            ),
                            xs=12,
                            sm=6,
                            lg=4,
                            className="mb-3",
                        ),
                    ],
                    className="g-3 mb-3",
                ),
            ],
        ),

        dbc.Card(
            dbc.CardBody(
                [
                    html.Div(
                        [
                            html.I(
                                className=(
                                    "fa-solid "
                                    "fa-chart-pie "
                                    "me-2"
                                )
                            ),
                            html.Strong(
                                "Resumo executivo"
                            ),
                        ],
                        className="mb-2",
                    ),

                    html.P(
                        "Aguardando dados.",
                        id="resumo-executivo-home",
                        className="mb-0 text-muted",
                    ),
                ],
                className="p-4",
            ),
            className="shadow-sm border-0 rounded-4 mb-4",
        ),

        dbc.Row(
            [
                dbc.Col(
                    dbc.Card(
                        dbc.CardBody(
                            [
                                html.H5(
                                    "Atendimentos por status",
                                    className="fw-bold mb-3",
                                ),
                                dcc.Graph(
                                    id="grafico-status-atendimentos",
                                    config={
                                        "displayModeBar": False
                                    },
                                ),
                            ],
                            className="p-4",
                        ),
                        className=(
                            "shadow-sm border-0 "
                            "rounded-4 h-100"
                        ),
                    ),
                    xs=12,
                    lg=5,
                    className="mb-3",
                ),

                dbc.Col(
                    dbc.Card(
                        dbc.CardBody(
                            [
                                html.H5(
                                    "Evolução dos atendimentos",
                                    id="titulo-grafico-temporal-home",
                                    className="fw-bold mb-3",
                                ),
                                dcc.Graph(
                                    id="grafico-atendimentos-dia",
                                    config={
                                        "displayModeBar": False
                                    },
                                ),
                            ],
                            className="p-4",
                        ),
                        className=(
                            "shadow-sm border-0 "
                            "rounded-4 h-100"
                        ),
                    ),
                    xs=12,
                    lg=7,
                    className="mb-3",
                ),
            ],
            className="g-3 mb-3",
        ),

        dbc.Row(
            [
                dbc.Col(
                    dbc.Card(
                        dbc.CardBody(
                            [
                                html.H5(
                                    "Atendimentos por usuário",
                                    className="fw-bold mb-3",
                                ),
                                dcc.Graph(
                                    id="grafico-usuarios-home",
                                    config={
                                        "displayModeBar": False
                                    },
                                ),
                            ],
                            className="p-4",
                        ),
                        className=(
                            "shadow-sm border-0 "
                            "rounded-4 h-100"
                        ),
                    ),
                    xs=12,
                    lg=6,
                    className="mb-3",
                ),

                dbc.Col(
                    dbc.Card(
                        dbc.CardBody(
                            [
                                html.H5(
                                    "Atendimentos por unidade",
                                    className="fw-bold mb-3",
                                ),
                                dcc.Graph(
                                    id="grafico-unidades-home",
                                    config={
                                        "displayModeBar": False
                                    },
                                ),
                            ],
                            className="p-4",
                        ),
                        className=(
                            "shadow-sm border-0 "
                            "rounded-4 h-100"
                        ),
                    ),
                    xs=12,
                    lg=6,
                    className="mb-3",
                ),
            ],
            className="g-3 mb-4",
        ),

        # ====================================================
        # ANÁLISE DO HISTÓRICO DO ATENDIDO
        # ====================================================

        dbc.Card(
            dbc.CardBody(
                [
                    html.Div(
                        [
                            html.Div(
                                html.I(
                                    className=(
                                        "fa-solid "
                                        "fa-chart-line "
                                        "fa-lg"
                                    )
                                ),
                                className=(
                                    "d-flex align-items-center "
                                    "justify-content-center "
                                    "rounded-3 bg-light"
                                ),
                                style={
                                    "width": "48px",
                                    "height": "48px",
                                    "minWidth": "48px",
                                },
                            ),
                            html.Div(
                                [
                                    html.H4(
                                        "Análise do histórico do atendido",
                                        className="fw-bold mb-1",
                                    ),
                                    html.P(
                                        (
                                            "Demandas, técnicos, status e linha "
                                            "do tempo do CSV carregado acima."
                                        ),
                                        className="text-muted mb-0",
                                    ),
                                ],
                                className="ms-3",
                            ),
                        ],
                        className=(
                            "d-flex align-items-center "
                            "mb-4"
                        ),
                    ),

                    dbc.Alert(
                        [
                            html.I(
                                className=(
                                    "fa-solid "
                                    "fa-circle-info "
                                    "me-2"
                                )
                            ),
                            (
                                "Selecione a pessoa atendida e carregue "
                                "o CSV do Plano de Acesso a Direitos. "
                                "O arquivo será associado à pessoa escolhida."
                            ),
                        ],
                        color="info",
                        className="mb-3 py-2",
                    ),

                    dbc.Row(
                        [
                            dbc.Col(
                                [
                                    dbc.Label(
                                        "Pessoa atendida",
                                        className="fw-semibold",
                                    ),
                                    dcc.Dropdown(
                                        id="seletor-pessoa-historico",
                                        placeholder=(
                                            "Selecione uma pessoa da base "
                                            "de atendimentos"
                                        ),
                                        clearable=True,
                                    ),
                                ],
                                xs=12,
                                lg=5,
                                className="mb-3",
                            ),

                            dbc.Col(
                                [
                                    dbc.Label(
                                        "CSV do histórico",
                                        className="fw-semibold",
                                    ),
                                    dcc.Upload(
                                        id="upload-historico-atendido",
                                        children=html.Div(
                                            [
                                                html.I(
                                                    className=(
                                                        "fa-solid "
                                                        "fa-file-csv "
                                                        "me-2"
                                                    )
                                                ),
                                                (
                                                    "Clique ou arraste o CSV "
                                                    "do histórico aqui"
                                                ),
                                            ],
                                            className=(
                                                "d-flex align-items-center "
                                                "justify-content-center"
                                            ),
                                        ),
                                        style={
                                            "width": "100%",
                                            "minHeight": "44px",
                                            "borderWidth": "1px",
                                            "borderStyle": "dashed",
                                            "borderRadius": "10px",
                                            "textAlign": "center",
                                            "padding": "10px 14px",
                                            "cursor": "pointer",
                                        },
                                        multiple=False,
                                    ),
                                ],
                                xs=12,
                                lg=7,
                                className="mb-3",
                            ),
                        ],
                        className="g-3",
                    ),

                    html.Div(
                        id="mensagem-upload-historico",
                        className="mb-3",
                    ),

                    html.Hr(
                        className="my-4",
                    ),

                    html.Div(
                        [
                            html.I(
                                className=(
                                    "fa-solid "
                                    "fa-user "
                                    "me-2"
                                )
                            ),
                            html.Strong(
                                "Histórico analisado: "
                            ),
                            html.Span(
                                "Aguardando seleção e arquivo",
                                id="nome-pessoa-historico",
                            ),
                        ],
                        className="mb-4",
                    ),

                    dbc.Row(
                        [
                            dbc.Col(
                                criar_card(
                                    "Registros no histórico",
                                    "0",
                                    "fa-solid fa-list-check",
                                    "card-historico-registros",
                                ),
                                xs=12,
                                sm=6,
                                lg=3,
                                className="mb-3",
                            ),

                            dbc.Col(
                                criar_card(
                                    "Tipos de demanda",
                                    "0",
                                    "fa-solid fa-tags",
                                    "card-historico-demandas",
                                ),
                                xs=12,
                                sm=6,
                                lg=3,
                                className="mb-3",
                            ),

                            dbc.Col(
                                criar_card(
                                    "Técnicos envolvidos",
                                    "0",
                                    "fa-solid fa-user-tie",
                                    "card-historico-tecnicos",
                                ),
                                xs=12,
                                sm=6,
                                lg=3,
                                className="mb-3",
                            ),

                            dbc.Col(
                                criar_card(
                                    "Pendentes",
                                    "0",
                                    "fa-solid fa-clock",
                                    "card-historico-pendentes",
                                ),
                                xs=12,
                                sm=6,
                                lg=3,
                                className="mb-3",
                            ),
                        ],
                        className="g-3 mb-3",
                    ),

                    dbc.Card(
                        dbc.CardBody(
                            [
                                html.Div(
                                    [
                                        html.I(
                                            className=(
                                                "fa-solid "
                                                "fa-notes-medical "
                                                "me-2"
                                            )
                                        ),
                                        html.Strong(
                                            "Resumo do acompanhamento"
                                        ),
                                    ],
                                    className="mb-2",
                                ),

                                html.P(
                                    "Aguardando histórico.",
                                    id="resumo-historico-atendido",
                                    className="text-muted mb-0",
                                ),
                            ],
                            className="p-3",
                        ),
                        className=(
                            "border-0 bg-light "
                            "rounded-4 mb-4"
                        ),
                    ),

                    dbc.Row(
                        [
                            dbc.Col(
                                dbc.Card(
                                    dbc.CardBody(
                                        [
                                            html.H5(
                                                "Demandas mais frequentes",
                                                className="fw-bold mb-3",
                                            ),

                                            dcc.Graph(
                                                id=(
                                                    "grafico-demandas-"
                                                    "historico"
                                                ),
                                                figure=figura_vazia(),
                                                config={
                                                    "displayModeBar": False
                                                },
                                            ),
                                        ],
                                        className="p-4",
                                    ),
                                    className=(
                                        "shadow-sm border-0 "
                                        "rounded-4 h-100"
                                    ),
                                ),
                                xs=12,
                                lg=6,
                                className="mb-3",
                            ),

                            dbc.Col(
                                dbc.Card(
                                    dbc.CardBody(
                                        [
                                            html.H5(
                                                "Registros ao longo do tempo",
                                                className="fw-bold mb-3",
                                            ),

                                            dcc.Graph(
                                                id=(
                                                    "grafico-tempo-"
                                                    "historico"
                                                ),
                                                figure=figura_vazia(),
                                                config={
                                                    "displayModeBar": False
                                                },
                                            ),
                                        ],
                                        className="p-4",
                                    ),
                                    className=(
                                        "shadow-sm border-0 "
                                        "rounded-4 h-100"
                                    ),
                                ),
                                xs=12,
                                lg=6,
                                className="mb-3",
                            ),
                        ],
                        className="g-3 mb-4",
                    ),

                    html.H5(
                        "Linha do tempo do histórico",
                        className="fw-bold mb-3",
                    ),

                    dash_table.DataTable(
                        id="tabela-historico-atendido",
                        columns=[],
                        data=[],
                        page_size=10,
                        sort_action="native",
                        filter_action="native",
                        page_action="native",
                        style_table={
                            "overflowX": "auto",
                        },
                        style_cell={
                            "textAlign": "left",
                            "padding": "10px",
                            "fontFamily": "Arial",
                            "fontSize": "13px",
                            "minWidth": "120px",
                            "maxWidth": "330px",
                            "whiteSpace": "normal",
                        },
                        style_header={
                            "fontWeight": "bold",
                            "backgroundColor": "#f8f9fa",
                        },
                    ),
                ],
                className="p-4",
            ),
            className=(
                "shadow-sm border-0 "
                "rounded-4 mb-4"
            ),
        ),
    ],
    fluid=True,
    className="px-4 pb-5",
)


# ============================================================
# PÁGINA - ATENDIMENTOS
# ============================================================

atendimentos_layout = dbc.Container(
    [
        dbc.Row(
            [
                dbc.Col(
                    [
                        html.H1(
                            "Atendimentos",
                            className="fw-bold mb-2",
                        ),

                        html.P(
                            (
                                "Acompanhamento detalhado dos registros "
                                "de atendimento importados do Sistema CAIS."
                            ),
                            className="text-muted fs-5 mb-0",
                        ),
                    ],
                    width=12,
                ),
            ],
            className="mb-4",
        ),

        dbc.Card(
            dbc.CardBody(
                [
                    html.H5(
                        "Filtros",
                        className="fw-bold mb-3",
                    ),

                    dbc.Row(
                        [
                            dbc.Col(
                                [
                                    dbc.Label(
                                        "Unidade",
                                        className="fw-semibold",
                                    ),

                                    dcc.Dropdown(
                                        id="filtro-unidade-atendimentos",
                                        placeholder="Todas as unidades",
                                        clearable=True,
                                    ),
                                ],
                                xs=12,
                                md=6,
                                xl=3,
                                className="mb-3",
                            ),

                            dbc.Col(
                                [
                                    dbc.Label(
                                        "Usuário",
                                        className="fw-semibold",
                                    ),

                                    dcc.Dropdown(
                                        id="filtro-profissional-atendimentos",
                                        placeholder="Todos os usuários",
                                        clearable=True,
                                    ),
                                ],
                                xs=12,
                                md=6,
                                xl=3,
                                className="mb-3",
                            ),

                            dbc.Col(
                                [
                                    dbc.Label(
                                        "Status",
                                        className="fw-semibold",
                                    ),

                                    dcc.Dropdown(
                                        id="filtro-status-atendimentos",
                                        placeholder="Todos os status",
                                        clearable=True,
                                    ),
                                ],
                                xs=12,
                                md=6,
                                xl=3,
                                className="mb-3",
                            ),

                            dbc.Col(
                                [
                                    dbc.Label(
                                        "Agrupar gráfico por",
                                        className="fw-semibold",
                                    ),

                                    dcc.Dropdown(
                                        id="agrupamento-temporal-atendimentos",
                                        options=[
                                            {
                                                "label": "Dia",
                                                "value": "dia",
                                            },
                                            {
                                                "label": "Semana",
                                                "value": "semana",
                                            },
                                            {
                                                "label": "Quinzena",
                                                "value": "quinzena",
                                            },
                                            {
                                                "label": "Mês",
                                                "value": "mes",
                                            },
                                        ],
                                        value="dia",
                                        clearable=False,
                                    ),
                                ],
                                xs=12,
                                md=6,
                                xl=3,
                                className="mb-3",
                            ),
                        ],
                        className="g-3",
                    ),

                    dbc.Row(
                        [
                            dbc.Col(
                                [
                                    dbc.Label(
                                        "Período",
                                        className="fw-semibold",
                                    ),

                                    dcc.DatePickerRange(
                                        id="filtro-periodo-atendimentos",
                                        display_format="DD/MM/YYYY",
                                        start_date_placeholder_text="Data inicial",
                                        end_date_placeholder_text="Data final",
                                        clearable=True,
                                    ),
                                ],
                                xs=12,
                                lg=8,
                                className="mb-2",
                            ),

                            dbc.Col(
                                dbc.Button(
                                    [
                                        html.I(
                                            className=(
                                                "fa-solid "
                                                "fa-filter-circle-xmark "
                                                "me-2"
                                            )
                                        ),
                                        "Limpar filtros",
                                    ],
                                    id="botao-limpar-filtros-atendimentos",
                                    color="secondary",
                                    outline=True,
                                    className="mt-4",
                                ),
                                xs=12,
                                lg=4,
                                className="mb-2",
                            ),
                        ],
                        className="g-3",
                    ),
                ],
                className="p-4",
            ),
            className=(
                "shadow-sm border-0 "
                "rounded-4 mb-4"
            ),
        ),

        dbc.Row(
            [
                dbc.Col(
                    criar_card(
                        "Registros filtrados",
                        "0",
                        "fa-solid fa-clipboard-list",
                        "card-atendimentos-filtrados",
                    ),
                    xs=12,
                    sm=6,
                    lg=3,
                    className="mb-3",
                ),

                dbc.Col(
                    criar_card(
                        "Pessoas únicas",
                        "0",
                        "fa-solid fa-user-group",
                        "card-pessoas-filtradas",
                    ),
                    xs=12,
                    sm=6,
                    lg=3,
                    className="mb-3",
                ),

                dbc.Col(
                    criar_card(
                        "Aguardando",
                        "0",
                        "fa-solid fa-clock",
                        "card-aguardando-filtrados",
                    ),
                    xs=12,
                    sm=6,
                    lg=3,
                    className="mb-3",
                ),

                dbc.Col(
                    criar_card(
                        "Usuários",
                        "0",
                        "fa-solid fa-user-tie",
                        "card-profissionais-filtrados",
                    ),
                    xs=12,
                    sm=6,
                    lg=3,
                    className="mb-3",
                ),
            ],
            className="g-3 mb-4",
        ),

        dbc.Row(
            [
                dbc.Col(
                    dbc.Card(
                        dbc.CardBody(
                            [
                                html.H5(
                                    "Atendimentos por status",
                                    className="fw-bold mb-3",
                                ),

                                dcc.Graph(
                                    id=(
                                        "grafico-status-"
                                        "atendimentos-pagina"
                                    ),
                                    config={
                                        "displayModeBar": False
                                    },
                                ),
                            ],
                            className="p-4",
                        ),
                        className=(
                            "shadow-sm border-0 "
                            "rounded-4 h-100"
                        ),
                    ),
                    xs=12,
                    lg=5,
                    className="mb-3",
                ),

                dbc.Col(
                    dbc.Card(
                        dbc.CardBody(
                            [
                                html.H5(
                                    "Atendimentos ao longo do tempo",
                                    className="fw-bold mb-3",
                                ),

                                dcc.Graph(
                                    id=(
                                        "grafico-tempo-"
                                        "atendimentos-pagina"
                                    ),
                                    config={
                                        "displayModeBar": False
                                    },
                                ),
                            ],
                            className="p-4",
                        ),
                        className=(
                            "shadow-sm border-0 "
                            "rounded-4 h-100"
                        ),
                    ),
                    xs=12,
                    lg=7,
                    className="mb-3",
                ),
            ],
            className="g-3 mb-4",
        ),

        dbc.Card(
            dbc.CardBody(
                [
                    html.H5(
                        "Registros de atendimento",
                        className="fw-bold mb-3",
                    ),

                    dash_table.DataTable(
                        id="tabela-atendimentos",
                        columns=[],
                        data=[],
                        page_size=15,
                        sort_action="native",
                        filter_action="native",
                        page_action="native",
                        style_table={
                            "overflowX": "auto",
                        },
                        style_cell={
                            "textAlign": "left",
                            "padding": "10px",
                            "fontFamily": "Arial",
                            "fontSize": "13px",
                            "minWidth": "120px",
                            "maxWidth": "300px",
                            "whiteSpace": "normal",
                        },
                        style_header={
                            "fontWeight": "bold",
                            "backgroundColor": "#f8f9fa",
                        },
                    ),
                ],
                className="p-4",
            ),
            className=(
                "shadow-sm border-0 "
                "rounded-4 mb-4"
            ),
        ),
    ],
    fluid=True,
    className="px-4 pb-5",
)


# ============================================================
# PÁGINAS EM CONSTRUÇÃO
# ============================================================

def pagina_em_construcao(titulo, descricao, icone):
    return dbc.Container(
        [
            dbc.Card(
                dbc.CardBody(
                    [
                        html.I(
                            className=f"{icone} fa-2x mb-3"
                        ),
                        html.H2(
                            titulo,
                            className="fw-bold",
                        ),
                        html.P(
                            descricao,
                            className="text-muted mb-0",
                        ),
                    ],
                    className="p-5",
                ),
                className=(
                    "shadow-sm border-0 "
                    "rounded-4"
                ),
            )
        ],
        fluid=True,
        className="px-4 pb-5",
    )


unidades_layout = dbc.Container(
    [
        dbc.Row(
            [
                dbc.Col(
                    [
                        html.H1(
                            "Unidades",
                            className="fw-bold mb-2",
                        ),

                        html.P(
                            (
                                "Visão consolidada dos atendimentos "
                                "de todas as unidades/OSCs identificadas "
                                "nos arquivos importados."
                            ),
                            className="text-muted fs-5 mb-0",
                        ),
                    ],
                    width=12,
                ),
            ],
            className="mb-3",
        ),

        dbc.Alert(
            [
                html.I(
                    className=(
                        "fa-solid "
                        "fa-layer-group "
                        "me-2"
                    )
                ),
                (
                    "Na página inicial você pode selecionar vários "
                    "arquivos de atendimentos de uma só vez. "
                    "O Monitoramento Cidadania consolida todos os registros e "
                    "separa as unidades pelo campo 'Atendido Em'."
                ),
            ],
            color="light",
            className="border rounded-4 mb-4",
        ),

        dbc.Card(
            dbc.CardBody(
                [
                    html.H5(
                        "Filtros da visão consolidada",
                        className="fw-bold mb-3",
                    ),

                    dbc.Row(
                        [
                            dbc.Col(
                                [
                                    dbc.Label(
                                        "Unidade / OSC",
                                        className="fw-semibold",
                                    ),

                                    dcc.Dropdown(
                                        id="filtro-unidade-unidades",
                                        placeholder="Todas as unidades / OSCs",
                                        clearable=True,
                                    ),
                                ],
                                xs=12,
                                md=6,
                                xl=3,
                                className="mb-3",
                            ),

                            dbc.Col(
                                [
                                    dbc.Label(
                                        "Usuário",
                                        className="fw-semibold",
                                    ),

                                    dcc.Dropdown(
                                        id="filtro-usuario-unidades",
                                        placeholder="Todos os usuários",
                                        clearable=True,
                                    ),
                                ],
                                xs=12,
                                md=6,
                                xl=3,
                                className="mb-3",
                            ),

                            dbc.Col(
                                [
                                    dbc.Label(
                                        "Status",
                                        className="fw-semibold",
                                    ),

                                    dcc.Dropdown(
                                        id="filtro-status-unidades",
                                        placeholder="Todos os status",
                                        clearable=True,
                                    ),
                                ],
                                xs=12,
                                md=6,
                                xl=3,
                                className="mb-3",
                            ),

                            dbc.Col(
                                [
                                    dbc.Label(
                                        "Agrupar gráfico por",
                                        className="fw-semibold",
                                    ),

                                    dcc.Dropdown(
                                        id="agrupamento-temporal-unidades",
                                        options=[
                                            {
                                                "label": "Dia",
                                                "value": "dia",
                                            },
                                            {
                                                "label": "Semana",
                                                "value": "semana",
                                            },
                                            {
                                                "label": "Quinzena",
                                                "value": "quinzena",
                                            },
                                            {
                                                "label": "Mês",
                                                "value": "mes",
                                            },
                                        ],
                                        value="dia",
                                        clearable=False,
                                    ),
                                ],
                                xs=12,
                                md=6,
                                xl=3,
                                className="mb-3",
                            ),
                        ],
                        className="g-3",
                    ),

                    dbc.Row(
                        [
                            dbc.Col(
                                [
                                    dbc.Label(
                                        "Período",
                                        className="fw-semibold",
                                    ),

                                    dcc.DatePickerRange(
                                        id="filtro-periodo-unidades",
                                        display_format="DD/MM/YYYY",
                                        start_date_placeholder_text="Data inicial",
                                        end_date_placeholder_text="Data final",
                                        clearable=True,
                                    ),
                                ],
                                xs=12,
                                lg=8,
                                className="mb-2",
                            ),

                            dbc.Col(
                                dbc.Button(
                                    [
                                        html.I(
                                            className=(
                                                "fa-solid "
                                                "fa-filter-circle-xmark "
                                                "me-2"
                                            )
                                        ),
                                        "Limpar filtros",
                                    ],
                                    id="botao-limpar-filtros-unidades",
                                    color="secondary",
                                    outline=True,
                                    className="mt-4",
                                ),
                                xs=12,
                                lg=4,
                                className="mb-2",
                            ),
                        ],
                        className="g-3",
                    ),
                ],
                className="p-4",
            ),
            className=(
                "shadow-sm border-0 "
                "rounded-4 mb-4"
            ),
        ),

        dbc.Row(
            [
                dbc.Col(
                    criar_card(
                        "Unidades / OSCs",
                        "0",
                        "fa-solid fa-building",
                        "card-total-unidades-pagina",
                    ),
                    xs=12,
                    sm=6,
                    lg=3,
                    className="mb-3",
                ),

                dbc.Col(
                    criar_card(
                        "Atendimentos",
                        "0",
                        "fa-solid fa-clipboard-list",
                        "card-atendimentos-unidades-pagina",
                    ),
                    xs=12,
                    sm=6,
                    lg=3,
                    className="mb-3",
                ),

                dbc.Col(
                    criar_card(
                        "Pessoas únicas",
                        "0",
                        "fa-solid fa-user-group",
                        "card-pessoas-unidades-pagina",
                    ),
                    xs=12,
                    sm=6,
                    lg=3,
                    className="mb-3",
                ),

                dbc.Col(
                    criar_card(
                        "Usuários ativos",
                        "0",
                        "fa-solid fa-users",
                        "card-usuarios-unidades-pagina",
                    ),
                    xs=12,
                    sm=6,
                    lg=3,
                    className="mb-3",
                ),
            ],
            className="g-3 mb-3",
        ),

        dbc.Row(
            [
                dbc.Col(
                    criar_card(
                        "Pendências cadastrais",
                        "0",
                        "fa-solid fa-triangle-exclamation",
                        "card-pendencias-unidades-pagina",
                    ),
                    xs=12,
                    sm=6,
                    lg=3,
                    className="mb-3",
                ),

                dbc.Col(
                    criar_card(
                        "Média por unidade",
                        "0",
                        "fa-solid fa-chart-simple",
                        "card-media-unidades-pagina",
                    ),
                    xs=12,
                    sm=6,
                    lg=3,
                    className="mb-3",
                ),

                dbc.Col(
                    criar_card(
                        "Aguardando",
                        "0",
                        "fa-solid fa-clock",
                        "card-aguardando-unidades-pagina",
                    ),
                    xs=12,
                    sm=6,
                    lg=3,
                    className="mb-3",
                ),

                dbc.Col(
                    criar_card(
                        "Rascunhos",
                        "0",
                        "fa-solid fa-pen-to-square",
                        "card-rascunhos-unidades-pagina",
                    ),
                    xs=12,
                    sm=6,
                    lg=3,
                    className="mb-3",
                ),
            ],
            className="g-3 mb-4",
        ),

        dbc.Card(
            dbc.CardBody(
                [
                    html.Div(
                        [
                            html.I(
                                className=(
                                    "fa-solid "
                                    "fa-chart-pie "
                                    "me-2"
                                )
                            ),
                            html.Strong(
                                "Resumo consolidado"
                            ),
                        ],
                        className="mb-2",
                    ),

                    html.P(
                        "Aguardando dados.",
                        id="resumo-unidades-pagina",
                        className="mb-0 text-muted",
                    ),
                ],
                className="p-4",
            ),
            className=(
                "shadow-sm border-0 "
                "rounded-4 mb-4"
            ),
        ),

        dbc.Row(
            [
                dbc.Col(
                    dbc.Card(
                        dbc.CardBody(
                            [
                                html.H5(
                                    "Atendimentos por unidade / OSC",
                                    className="fw-bold mb-3",
                                ),

                                dcc.Graph(
                                    id="grafico-atendimentos-unidades-pagina",
                                    config={
                                        "displayModeBar": False
                                    },
                                ),
                            ],
                            className="p-4",
                        ),
                        className=(
                            "shadow-sm border-0 "
                            "rounded-4 h-100"
                        ),
                    ),
                    xs=12,
                    lg=6,
                    className="mb-3",
                ),

                dbc.Col(
                    dbc.Card(
                        dbc.CardBody(
                            [
                                html.H5(
                                    "Pessoas por unidade / OSC",
                                    className="fw-bold mb-3",
                                ),

                                dcc.Graph(
                                    id="grafico-pessoas-unidades-pagina",
                                    config={
                                        "displayModeBar": False
                                    },
                                ),
                            ],
                            className="p-4",
                        ),
                        className=(
                            "shadow-sm border-0 "
                            "rounded-4 h-100"
                        ),
                    ),
                    xs=12,
                    lg=6,
                    className="mb-3",
                ),
            ],
            className="g-3 mb-3",
        ),

        dbc.Row(
            [
                dbc.Col(
                    dbc.Card(
                        dbc.CardBody(
                            [
                                html.H5(
                                    "Usuários ativos por unidade / OSC",
                                    className="fw-bold mb-3",
                                ),

                                dcc.Graph(
                                    id="grafico-usuarios-unidades-pagina",
                                    config={
                                        "displayModeBar": False
                                    },
                                ),
                            ],
                            className="p-4",
                        ),
                        className=(
                            "shadow-sm border-0 "
                            "rounded-4 h-100"
                        ),
                    ),
                    xs=12,
                    lg=5,
                    className="mb-3",
                ),

                dbc.Col(
                    dbc.Card(
                        dbc.CardBody(
                            [
                                html.H5(
                                    "Evolução comparativa",
                                    id="titulo-evolucao-unidades-pagina",
                                    className="fw-bold mb-3",
                                ),

                                dcc.Graph(
                                    id="grafico-evolucao-unidades-pagina",
                                    config={
                                        "displayModeBar": False
                                    },
                                ),

                                html.Small(
                                    (
                                        "Quando houver mais de 10 unidades, "
                                        "o gráfico exibe as 10 com maior "
                                        "volume para manter a leitura. "
                                        "A tabela abaixo continua mostrando todas."
                                    ),
                                    className="text-muted",
                                ),
                            ],
                            className="p-4",
                        ),
                        className=(
                            "shadow-sm border-0 "
                            "rounded-4 h-100"
                        ),
                    ),
                    xs=12,
                    lg=7,
                    className="mb-3",
                ),
            ],
            className="g-3 mb-4",
        ),

        dbc.Card(
            dbc.CardBody(
                [
                    html.H5(
                        "Comparativo de todas as unidades / OSCs",
                        className="fw-bold mb-1",
                    ),

                    html.P(
                        (
                            "A tabela consolida os principais indicadores "
                            "de cada valor identificado em 'Atendido Em'."
                        ),
                        className="text-muted mb-3",
                    ),

                    dash_table.DataTable(
                        id="tabela-resumo-unidades",
                        columns=[],
                        data=[],
                        page_size=20,
                        sort_action="native",
                        filter_action="native",
                        page_action="native",
                        export_format="csv",
                        export_headers="display",
                        export_columns="visible",
                        style_table={
                            "overflowX": "auto",
                        },
                        style_cell={
                            "textAlign": "left",
                            "padding": "10px",
                            "fontFamily": "Arial",
                            "fontSize": "13px",
                            "minWidth": "115px",
                            "maxWidth": "360px",
                            "whiteSpace": "normal",
                        },
                        style_header={
                            "fontWeight": "bold",
                            "backgroundColor": "#f8f9fa",
                        },
                    ),
                ],
                className="p-4",
            ),
            className=(
                "shadow-sm border-0 "
                "rounded-4 mb-4"
            ),
        ),
    ],
    fluid=True,
    className="px-4 pb-5",
)

usuarios_layout = dbc.Container(
    [
        dbc.Row(
            [
                dbc.Col(
                    [
                        html.H1(
                            "Usuários",
                            className="fw-bold mb-2",
                        ),

                        html.P(
                            (
                                "Análise dos usuários/profissionais "
                                "responsáveis pelos atendimentos, com base "
                                "na coluna 'Atendido Por'."
                            ),
                            className="text-muted fs-5 mb-0",
                        ),
                    ],
                    width=12,
                ),
            ],
            className="mb-4",
        ),

        dbc.Card(
            dbc.CardBody(
                [
                    html.H5(
                        "Filtros",
                        className="fw-bold mb-3",
                    ),

                    dbc.Row(
                        [
                            dbc.Col(
                                [
                                    dbc.Label(
                                        "Unidade",
                                        className="fw-semibold",
                                    ),

                                    dcc.Dropdown(
                                        id="filtro-unidade-usuarios",
                                        placeholder="Todas as unidades",
                                        clearable=True,
                                    ),
                                ],
                                xs=12,
                                md=6,
                                xl=3,
                                className="mb-3",
                            ),

                            dbc.Col(
                                [
                                    dbc.Label(
                                        "Usuário",
                                        className="fw-semibold",
                                    ),

                                    dcc.Dropdown(
                                        id="filtro-usuario-usuarios",
                                        placeholder="Todos os usuários",
                                        clearable=True,
                                    ),
                                ],
                                xs=12,
                                md=6,
                                xl=3,
                                className="mb-3",
                            ),

                            dbc.Col(
                                [
                                    dbc.Label(
                                        "Status",
                                        className="fw-semibold",
                                    ),

                                    dcc.Dropdown(
                                        id="filtro-status-usuarios",
                                        placeholder="Todos os status",
                                        clearable=True,
                                    ),
                                ],
                                xs=12,
                                md=6,
                                xl=3,
                                className="mb-3",
                            ),

                            dbc.Col(
                                [
                                    dbc.Label(
                                        "Agrupar gráfico por",
                                        className="fw-semibold",
                                    ),

                                    dcc.Dropdown(
                                        id="agrupamento-temporal-usuarios",
                                        options=[
                                            {
                                                "label": "Dia",
                                                "value": "dia",
                                            },
                                            {
                                                "label": "Semana",
                                                "value": "semana",
                                            },
                                            {
                                                "label": "Quinzena",
                                                "value": "quinzena",
                                            },
                                            {
                                                "label": "Mês",
                                                "value": "mes",
                                            },
                                        ],
                                        value="dia",
                                        clearable=False,
                                    ),
                                ],
                                xs=12,
                                md=6,
                                xl=3,
                                className="mb-3",
                            ),
                        ],
                        className="g-3",
                    ),

                    dbc.Row(
                        [
                            dbc.Col(
                                [
                                    dbc.Label(
                                        "Período",
                                        className="fw-semibold",
                                    ),

                                    dcc.DatePickerRange(
                                        id="filtro-periodo-usuarios",
                                        display_format="DD/MM/YYYY",
                                        start_date_placeholder_text="Data inicial",
                                        end_date_placeholder_text="Data final",
                                        clearable=True,
                                    ),
                                ],
                                xs=12,
                                lg=8,
                                className="mb-2",
                            ),

                            dbc.Col(
                                dbc.Button(
                                    [
                                        html.I(
                                            className=(
                                                "fa-solid "
                                                "fa-filter-circle-xmark "
                                                "me-2"
                                            )
                                        ),
                                        "Limpar filtros",
                                    ],
                                    id="botao-limpar-filtros-usuarios",
                                    color="secondary",
                                    outline=True,
                                    className="mt-4",
                                ),
                                xs=12,
                                lg=4,
                                className="mb-2",
                            ),
                        ],
                        className="g-3",
                    ),
                ],
                className="p-4",
            ),
            className=(
                "shadow-sm border-0 "
                "rounded-4 mb-4"
            ),
        ),

        dbc.Row(
            [
                dbc.Col(
                    criar_card(
                        "Usuários ativos",
                        "0",
                        "fa-solid fa-users",
                        "card-usuarios-ativos-pagina",
                    ),
                    xs=12,
                    sm=6,
                    lg=3,
                    className="mb-3",
                ),

                dbc.Col(
                    criar_card(
                        "Atendimentos",
                        "0",
                        "fa-solid fa-clipboard-list",
                        "card-atendimentos-usuarios-pagina",
                    ),
                    xs=12,
                    sm=6,
                    lg=3,
                    className="mb-3",
                ),

                dbc.Col(
                    criar_card(
                        "Pessoas únicas",
                        "0",
                        "fa-solid fa-user-group",
                        "card-pessoas-usuarios-pagina",
                    ),
                    xs=12,
                    sm=6,
                    lg=3,
                    className="mb-3",
                ),

                dbc.Col(
                    criar_card(
                        "Média de atendimentos por usuário",
                        "0",
                        "fa-solid fa-chart-simple",
                        "card-media-usuarios-pagina",
                    ),
                    xs=12,
                    sm=6,
                    lg=3,
                    className="mb-3",
                ),
            ],
            className="g-3 mb-4",
        ),

        dbc.Card(
            dbc.CardBody(
                [
                    html.Div(
                        [
                            html.I(
                                className=(
                                    "fa-solid "
                                    "fa-circle-info "
                                    "me-2"
                                )
                            ),
                            html.Strong(
                                "Resumo dos usuários"
                            ),
                        ],
                        className="mb-2",
                    ),

                    html.P(
                        "Aguardando dados.",
                        id="resumo-usuarios-pagina",
                        className="mb-0 text-muted",
                    ),
                ],
                className="p-4",
            ),
            className=(
                "shadow-sm border-0 "
                "rounded-4 mb-4"
            ),
        ),

        dbc.Row(
            [
                dbc.Col(
                    dbc.Card(
                        dbc.CardBody(
                            [
                                html.H5(
                                    "Atendimentos por usuário",
                                    className="fw-bold mb-3",
                                ),

                                dcc.Graph(
                                    id="grafico-ranking-usuarios-pagina",
                                    config={
                                        "displayModeBar": False
                                    },
                                ),
                            ],
                            className="p-4",
                        ),
                        className=(
                            "shadow-sm border-0 "
                            "rounded-4 h-100"
                        ),
                    ),
                    xs=12,
                    lg=6,
                    className="mb-3",
                ),

                dbc.Col(
                    dbc.Card(
                        dbc.CardBody(
                            [
                                html.H5(
                                    "Evolução dos atendimentos",
                                    id="titulo-temporal-usuarios-pagina",
                                    className="fw-bold mb-3",
                                ),

                                dcc.Graph(
                                    id="grafico-temporal-usuarios-pagina",
                                    config={
                                        "displayModeBar": False
                                    },
                                ),
                            ],
                            className="p-4",
                        ),
                        className=(
                            "shadow-sm border-0 "
                            "rounded-4 h-100"
                        ),
                    ),
                    xs=12,
                    lg=6,
                    className="mb-3",
                ),
            ],
            className="g-3 mb-3",
        ),

        dbc.Row(
            [
                dbc.Col(
                    dbc.Card(
                        dbc.CardBody(
                            [
                                html.H5(
                                    "Distribuição por status",
                                    className="fw-bold mb-3",
                                ),

                                dcc.Graph(
                                    id="grafico-status-usuarios-pagina",
                                    config={
                                        "displayModeBar": False
                                    },
                                ),
                            ],
                            className="p-4",
                        ),
                        className=(
                            "shadow-sm border-0 "
                            "rounded-4 h-100"
                        ),
                    ),
                    xs=12,
                    lg=5,
                    className="mb-3",
                ),

                dbc.Col(
                    dbc.Card(
                        dbc.CardBody(
                            [
                                html.H5(
                                    "Atendimentos por unidade",
                                    className="fw-bold mb-3",
                                ),

                                dcc.Graph(
                                    id="grafico-unidades-usuarios-pagina",
                                    config={
                                        "displayModeBar": False
                                    },
                                ),
                            ],
                            className="p-4",
                        ),
                        className=(
                            "shadow-sm border-0 "
                            "rounded-4 h-100"
                        ),
                    ),
                    xs=12,
                    lg=7,
                    className="mb-3",
                ),
            ],
            className="g-3 mb-4",
        ),

        dbc.Card(
            dbc.CardBody(
                [
                    html.H5(
                        "Resumo por usuário",
                        className="fw-bold mb-1",
                    ),

                    html.P(
                        (
                            "Comparativo dos usuários com quantidade "
                            "de atendimentos, pessoas, status e unidades."
                        ),
                        className="text-muted mb-3",
                    ),

                    dash_table.DataTable(
                        id="tabela-resumo-usuarios",
                        columns=[],
                        data=[],
                        page_size=15,
                        sort_action="native",
                        filter_action="native",
                        page_action="native",
                        style_table={
                            "overflowX": "auto",
                        },
                        style_cell={
                            "textAlign": "left",
                            "padding": "10px",
                            "fontFamily": "Arial",
                            "fontSize": "13px",
                            "minWidth": "110px",
                            "maxWidth": "280px",
                            "whiteSpace": "normal",
                        },
                        style_header={
                            "fontWeight": "bold",
                            "backgroundColor": "#f8f9fa",
                        },
                    ),
                ],
                className="p-4",
            ),
            className=(
                "shadow-sm border-0 "
                "rounded-4 mb-4"
            ),
        ),
    ],
    fluid=True,
    className="px-4 pb-5",
)

auditoria_layout = dbc.Container(
    [
        dbc.Row(
            [
                dbc.Col(
                    [
                        html.H1(
                            "Auditoria",
                            className="fw-bold mb-2",
                        ),

                        html.P(
                            (
                                "Análise automática da qualidade dos "
                                "registros de atendimento importados "
                                "do Sistema CAIS."
                            ),
                            className="text-muted fs-5 mb-0",
                        ),
                    ],
                    width=12,
                ),
            ],
            className="mb-3",
        ),

        dbc.Alert(
            [
                html.I(
                    className=(
                        "fa-solid "
                        "fa-circle-info "
                        "me-2"
                    )
                ),
                (
                    "A auditoria considera somente duplicidades, "
                    "cadastros sem nenhum dado essencial e "
                    "registros sem nome. Rascunho não é tratado "
                    "como erro."
                ),
            ],
            color="light",
            className="border rounded-4 mb-4",
        ),

        dbc.Card(
            dbc.CardBody(
                [
                    html.H5(
                        "Filtros da auditoria",
                        className="fw-bold mb-3",
                    ),

                    dbc.Row(
                        [
                            dbc.Col(
                                [
                                    dbc.Label(
                                        "Unidade",
                                        className="fw-semibold",
                                    ),

                                    dcc.Dropdown(
                                        id="filtro-unidade-auditoria",
                                        placeholder="Todas as unidades",
                                        clearable=True,
                                    ),
                                ],
                                xs=12,
                                md=6,
                                xl=3,
                                className="mb-3",
                            ),

                            dbc.Col(
                                [
                                    dbc.Label(
                                        "Responsável",
                                        className="fw-semibold",
                                    ),

                                    dcc.Dropdown(
                                        id="filtro-usuario-auditoria",
                                        placeholder="Todos os responsáveis",
                                        clearable=True,
                                    ),
                                ],
                                xs=12,
                                md=6,
                                xl=3,
                                className="mb-3",
                            ),

                            dbc.Col(
                                [
                                    dbc.Label(
                                        "Status",
                                        className="fw-semibold",
                                    ),

                                    dcc.Dropdown(
                                        id="filtro-status-auditoria",
                                        placeholder="Todos os status",
                                        clearable=True,
                                    ),
                                ],
                                xs=12,
                                md=6,
                                xl=3,
                                className="mb-3",
                            ),

                            dbc.Col(
                                [
                                    dbc.Label(
                                        "Período",
                                        className="fw-semibold",
                                    ),

                                    dcc.DatePickerRange(
                                        id="filtro-periodo-auditoria",
                                        display_format="DD/MM/YYYY",
                                        start_date_placeholder_text="Data inicial",
                                        end_date_placeholder_text="Data final",
                                        clearable=True,
                                    ),
                                ],
                                xs=12,
                                md=6,
                                xl=3,
                                className="mb-3",
                            ),
                        ],
                        className="g-3",
                    ),

                    dbc.Button(
                        [
                            html.I(
                                className=(
                                    "fa-solid "
                                    "fa-filter-circle-xmark "
                                    "me-2"
                                )
                            ),
                            "Limpar filtros",
                        ],
                        id="botao-limpar-filtros-auditoria",
                        color="secondary",
                        outline=True,
                    ),
                ],
                className="p-4",
            ),
            className=(
                "shadow-sm border-0 "
                "rounded-4 mb-4"
            ),
        ),

        html.Div(
            id="alerta-estrutura-auditoria",
            className="mb-4",
        ),

        dbc.Row(
            [
                dbc.Col(
                    criar_card(
                        "Registros analisados",
                        "0",
                        "fa-solid fa-magnifying-glass-chart",
                        "card-registros-auditoria",
                    ),
                    xs=12,
                    sm=6,
                    lg=2,
                    className="mb-3",
                ),

                dbc.Col(
                    criar_card(
                        "Com inconsistências",
                        "0",
                        "fa-solid fa-triangle-exclamation",
                        "card-inconsistencias-auditoria",
                    ),
                    xs=12,
                    sm=6,
                    lg=2,
                    className="mb-3",
                ),

                dbc.Col(
                    criar_card(
                        "Duplicidades",
                        "0",
                        "fa-solid fa-clone",
                        "card-duplicados-auditoria",
                    ),
                    xs=12,
                    sm=6,
                    lg=2,
                    className="mb-3",
                ),

                dbc.Col(
                    criar_card(
                        "Cadastros incompletos",
                        "0",
                        "fa-solid fa-file-circle-xmark",
                        "card-incompletos-auditoria",
                    ),
                    xs=12,
                    sm=6,
                    lg=3,
                    className="mb-3",
                ),

                dbc.Col(
                    criar_card(
                        "Sem nome",
                        "0",
                        "fa-solid fa-user-slash",
                        "card-sem-nome-auditoria",
                    ),
                    xs=12,
                    sm=6,
                    lg=3,
                    className="mb-3",
                ),
            ],
            className="g-3 mb-4",
        ),

        dbc.Card(
            dbc.CardBody(
                [
                    html.Div(
                        [
                            html.I(
                                className=(
                                    "fa-solid "
                                    "fa-clipboard-check "
                                    "me-2"
                                )
                            ),
                            html.Strong(
                                "Relatório automático da auditoria"
                            ),
                        ],
                        className="mb-3",
                    ),

                    html.Div(
                        "Aguardando dados.",
                        id="relatorio-auditoria",
                        className="text-muted",
                    ),
                ],
                className="p-4",
            ),
            className=(
                "shadow-sm border-0 "
                "rounded-4 mb-4"
            ),
        ),

        dbc.Row(
            [
                dbc.Col(
                    dbc.Card(
                        dbc.CardBody(
                            [
                                html.H5(
                                    "Ocorrências encontradas",
                                    className="fw-bold mb-3",
                                ),

                                dcc.Graph(
                                    id="grafico-ocorrencias-auditoria",
                                    config={
                                        "displayModeBar": False
                                    },
                                ),
                            ],
                            className="p-4",
                        ),
                        className=(
                            "shadow-sm border-0 "
                            "rounded-4 h-100"
                        ),
                    ),
                    xs=12,
                    lg=6,
                    className="mb-3",
                ),

                dbc.Col(
                    dbc.Card(
                        dbc.CardBody(
                            [
                                html.H5(
                                    "Inconsistências por unidade",
                                    className="fw-bold mb-3",
                                ),

                                dcc.Graph(
                                    id="grafico-unidades-auditoria",
                                    config={
                                        "displayModeBar": False
                                    },
                                ),
                            ],
                            className="p-4",
                        ),
                        className=(
                            "shadow-sm border-0 "
                            "rounded-4 h-100"
                        ),
                    ),
                    xs=12,
                    lg=6,
                    className="mb-3",
                ),
            ],
            className="g-3 mb-4",
        ),

        dbc.Card(
            dbc.CardBody(
                [
                    html.H5(
                        "Registros para conferência",
                        className="fw-bold mb-1",
                    ),

                    html.P(
                        (
                            "A tabela abaixo exibe somente registros "
                            "em que a auditoria encontrou alguma "
                            "inconsistência ou possível duplicidade."
                        ),
                        className="text-muted mb-3",
                    ),

                    dash_table.DataTable(
                        id="tabela-auditoria",
                        columns=[],
                        data=[],
                        page_size=20,
                        sort_action="native",
                        filter_action="native",
                        page_action="native",
                        export_format="csv",
                        export_headers="display",
                        export_columns="visible",
                        style_table={
                            "overflowX": "auto",
                        },
                        style_cell={
                            "textAlign": "left",
                            "padding": "10px",
                            "fontFamily": "Arial",
                            "fontSize": "13px",
                            "minWidth": "120px",
                            "maxWidth": "350px",
                            "whiteSpace": "normal",
                        },
                        style_header={
                            "fontWeight": "bold",
                            "backgroundColor": "#f8f9fa",
                        },
                        style_data_conditional=[
                            {
                                "if": {
                                    "filter_query": (
                                        '{Severidade} = "Erro"'
                                    ),
                                },
                                "fontWeight": "500",
                            },
                            {
                                "if": {
                                    "filter_query": (
                                        '{Severidade} = "Atenção"'
                                    ),
                                },
                                "fontStyle": "italic",
                            },
                        ],
                    ),
                ],
                className="p-4",
            ),
            className=(
                "shadow-sm border-0 "
                "rounded-4 mb-4"
            ),
        ),
    ],
    fluid=True,
    className="px-4 pb-5",
)

base_layout = dbc.Container(
    [
        dbc.Row(
            [
                dbc.Col(
                    [
                        html.H1(
                            "Base de Dados",
                            className="fw-bold mb-2",
                            style={"color": "#071D41"},
                        ),
                        html.P(
                            "Histórico permanente dos arquivos importados no Monitoramento Cidadania.",
                            className="text-muted fs-5 mb-0",
                        ),
                    ],
                    width=12,
                ),
            ],
            className="mb-3",
        ),
        dcc.Interval(
            id="interval-atualizar-base",
            interval=400,
            n_intervals=0,
            max_intervals=1,
        ),

        dbc.Alert(
            [
                html.I(className="fa-solid fa-database me-2"),
                html.Strong("Armazenamento local ativado. "),
                (
                    "Os arquivos válidos passam a ser guardados no computador em "
                    "data/historico_importacoes. O histórico das importações fica "
                    "registrado em um banco SQLite local."
                ),
            ],
            color="info",
            className="rounded-4 mb-4",
        ),
        dbc.Row(
            [
                dbc.Col(criar_card("Importações salvas", "0", "fa-solid fa-box-archive", "card-importacoes-salvas"), xs=12, sm=6, lg=3, className="mb-3"),
                dbc.Col(criar_card("Arquivos guardados", "0", "fa-solid fa-file-arrow-down", "card-arquivos-guardados"), xs=12, sm=6, lg=3, className="mb-3"),
                dbc.Col(criar_card("Registros armazenados", "0", "fa-solid fa-database", "card-registros-armazenados"), xs=12, sm=6, lg=3, className="mb-3"),
                dbc.Col(criar_card("Históricos individuais", "0", "fa-solid fa-clock-rotate-left", "card-historicos-salvos"), xs=12, sm=6, lg=3, className="mb-3"),
            ],
            className="g-3 mb-4",
        ),
        dbc.Card(
            dbc.CardBody(
                [
                    html.H5("Recuperar uma importação", className="fw-bold mb-1"),
                    html.P("Selecione uma base já salva para carregá-la novamente no painel.", className="text-muted mb-3"),
                    dbc.Row(
                        [
                            dbc.Col(
                                dcc.Dropdown(
                                    id="seletor-importacao-salva",
                                    placeholder="Selecione uma importação...",
                                    clearable=True,
                                ),
                                xs=12,
                                lg=9,
                                className="mb-3",
                            ),
                            dbc.Col(
                                dbc.Button(
                                    [html.I(className="fa-solid fa-rotate-left me-2"), "Carregar seleção"],
                                    id="botao-carregar-importacao",
                                    color="primary",
                                    className="w-100",
                                ),
                                xs=12,
                                lg=3,
                                className="mb-3",
                            ),
                        ],
                        className="g-2",
                    ),
                    html.Div(id="mensagem-carregar-base"),
                ],
                className="p-4",
            ),
            className="shadow-sm border-0 rounded-4 mb-4",
            style={"backgroundColor": "#FFFFFF"},
        ),
        dbc.Card(
            dbc.CardBody(
                [
                    html.H5("Histórico de importações", className="fw-bold mb-1"),
                    html.P("Cada linha representa uma importação guardada localmente.", className="text-muted mb-3"),
                    dash_table.DataTable(
                        id="tabela-importacoes-salvas",
                        columns=[
                            {"name": "ID", "id": "ID"},
                            {"name": "Tipo", "id": "Tipo"},
                            {"name": "Data / hora", "id": "Data / hora"},
                            {"name": "Pessoa", "id": "Pessoa"},
                            {"name": "Arquivos", "id": "Arquivos"},
                            {"name": "Qtd. arquivos", "id": "Qtd. arquivos"},
                            {"name": "Registros", "id": "Registros"},
                            {"name": "Unidades", "id": "Unidades"},
                        ],
                        data=[],
                        page_size=15,
                        sort_action="native",
                        filter_action="native",
                        page_action="native",
                        style_table={"overflowX": "auto"},
                        style_cell={
                            "textAlign": "left",
                            "padding": "10px",
                            "fontFamily": "Arial",
                            "fontSize": "13px",
                            "minWidth": "100px",
                            "maxWidth": "380px",
                            "whiteSpace": "normal",
                        },
                        style_header={
                            "fontWeight": "bold",
                            "backgroundColor": "#D6E7FF",
                            "color": "#071D41",
                        },
                    ),
                ],
                className="p-4",
            ),
            className="shadow-sm border-0 rounded-4 mb-4",
            style={"backgroundColor": "#FFFFFF"},
        ),
        dbc.Alert(
            [
                html.I(className="fa-solid fa-shield-halved me-2"),
                (
                    "Atenção: os arquivos do CAIS podem conter dados pessoais. "
                    "Mantenha a pasta do projeto em ambiente institucional protegido "
                    "e com acesso restrito."
                ),
            ],
            color="warning",
            className="rounded-4",
        ),
    ],
    fluid=True,
    className="px-4 pb-5",
)


relatorios_layout = pagina_em_construcao(
    "Relatórios",
    (
        "A geração de relatórios por unidade e período "
        "será adicionada nesta área."
    ),
    "fa-solid fa-file-lines",
)


# ============================================================
# LAYOUT PRINCIPAL
# ============================================================

app.layout = html.Div(
    [
        dcc.Location(
            id="url",
            refresh=False,
        ),

        dcc.Store(
            id="dados-cais",
            storage_type="memory",
        ),

        dcc.Store(
            id="dados-historico-atendido",
            storage_type="memory",
        ),

        navbar,

        html.Main(
            id="page-content",
            style={
                "backgroundColor": "#F4F7FA",
                "minHeight": "calc(100vh - 72px)",
                "paddingTop": "2px",
                "paddingBottom": "28px",
            },
        ),
    ],
    style={
        "backgroundColor": "#F4F7FA",
        "minHeight": "100vh",
    },
)


# ============================================================
# CALLBACK - NAVEGAÇÃO
# ============================================================

@app.callback(
    Output(
        "page-content",
        "children",
    ),
    Input(
        "url",
        "pathname",
    ),
)
def navegar_paginas(pathname):
    if pathname == "/atendimentos":
        return atendimentos_layout

    if pathname == "/unidades":
        return unidades_layout

    if pathname == "/usuarios":
        return usuarios_layout

    if pathname == "/auditoria":
        return auditoria_layout

    if pathname == "/base":
        return base_layout

    if pathname == "/relatorios":
        return relatorios_layout

    return home_layout


# ============================================================
# CALLBACK - PROCESSAMENTO DO UPLOAD
# ============================================================

@app.callback(
    [
        Output(
            "mensagem-upload-home",
            "children",
        ),
        Output(
            "dados-cais",
            "data",
        ),
    ],
    Input(
        "upload-dados-home",
        "contents",
    ),
    State(
        "upload-dados-home",
        "filename",
    ),
    prevent_initial_call=True,
)
def processar_upload_home(
    conteudo_upload,
    nome_arquivo,
):
    if not conteudo_upload:
        return (
            None,
            None,
        )

    try:
        # dcc.Upload com multiple=True devolve listas.
        # A normalização abaixo também mantém compatibilidade
        # caso apenas um arquivo seja recebido.
        conteudos = (
            conteudo_upload
            if isinstance(
                conteudo_upload,
                list,
            )
            else [
                conteudo_upload
            ]
        )

        nomes = (
            nome_arquivo
            if isinstance(
                nome_arquivo,
                list,
            )
            else [
                nome_arquivo
            ]
        )

        colunas_necessarias = {
            "número do protocolo",
            "nome completo",
            "status do atendimento",
            "criado em",
            "atualizado em",
            "atendido em",
            "atendido por",
        }

        dataframes_validos = []
        arquivos_validos = []
        conteudos_validos = []
        arquivos_ignorados = []

        for conteudo, nome in zip(
            conteudos,
            nomes,
        ):
            try:
                df_arquivo = (
                    ler_arquivo_upload(
                        nome,
                        conteudo,
                    )
                )

                colunas_normalizadas = {
                    str(col)
                    .strip()
                    .lower()
                    for col in df_arquivo.columns
                }

                eh_atendimento = (
                    colunas_necessarias
                    .issubset(
                        colunas_normalizadas
                    )
                )

                if not eh_atendimento:
                    arquivos_ignorados.append(
                        (
                            f"{nome}: não corresponde "
                            "à tabela de atendimentos"
                        )
                    )
                    continue

                dataframes_validos.append(
                    df_arquivo
                )

                arquivos_validos.append(
                    nome
                )

                conteudos_validos.append(
                    conteudo
                )

            except Exception as erro_arquivo:
                arquivos_ignorados.append(
                    (
                        f"{nome}: "
                        f"{erro_arquivo}"
                    )
                )

        if not dataframes_validos:
            return (
                dbc.Alert(
                    [
                        html.I(
                            className=(
                                "fa-solid "
                                "fa-triangle-exclamation "
                                "me-2"
                            )
                        ),
                        html.Strong(
                            "Nenhum arquivo de atendimentos válido foi identificado."
                        ),
                        html.Br(),
                        html.Small(
                            (
                                "Confira se os arquivos exportados "
                                "possuem as colunas esperadas do CAIS."
                            )
                        ),
                    ],
                    color="danger",
                    className="mt-3 mb-0",
                ),
                None,
            )

        df = pd.concat(
            dataframes_validos,
            ignore_index=True,
            sort=False,
        )

        quantidade_linhas = len(
            df
        )

        quantidade_colunas = len(
            df.columns
        )

        quantidade_arquivos = len(
            arquivos_validos
        )

        colunas_encontradas = list(
            df.columns
        )

        colunas = localizar_colunas(
            df
        )

        quantidade_unidades = contar_unicos(
            df,
            colunas["unidade"],
        )

        dados_cais = df.to_json(
            orient="split",
            date_format="iso",
        )

        importacao_id = None
        erro_armazenamento = None

        try:
            importacao_id = registrar_importacao(
                tipo="Atendimentos",
                nomes_arquivos=arquivos_validos,
                conteudos_arquivos=conteudos_validos,
                dataframe=df,
            )
        except Exception as erro_salvar:
            erro_armazenamento = str(erro_salvar)

        mensagem_conteudo = [
            html.I(
                className=(
                    "fa-solid "
                    "fa-circle-check "
                    "me-2"
                )
            ),

            html.Strong(
                (
                    "Base de atendimentos "
                    "processada com sucesso!"
                )
            ),

            html.Br(),

            html.Small(
                (
                    f"{quantidade_arquivos} arquivo(s) "
                    f"consolidado(s), "
                    f"{quantidade_linhas} linhas e "
                    f"{quantidade_colunas} colunas."
                )
            ),

            html.Br(),

            html.Small(
                (
                    f"{quantidade_unidades} unidade(s) / "
                    "OSC(s) identificada(s) em "
                    "'Atendido Em'."
                )
            ),

            html.Br(),

            html.Small(
                [
                    html.Strong(
                        "Arquivos carregados: "
                    ),
                    ", ".join(
                        arquivos_validos
                    ),
                ]
            ),

            html.Br(),

            html.Small(
                [
                    html.Strong(
                        "Colunas identificadas: "
                    ),
                    ", ".join(
                        colunas_encontradas
                    ),
                ]
            ),
        ]

        if importacao_id:
            mensagem_conteudo.extend(
                [
                    html.Br(),
                    html.Small(
                        [
                            html.I(className="fa-solid fa-database me-1"),
                            html.Strong("Salvo permanentemente: "),
                            f"importação #{importacao_id} na Base de Dados.",
                        ]
                    ),
                ]
            )

        if erro_armazenamento:
            mensagem_conteudo.extend(
                [
                    html.Hr(className="my-2"),
                    html.Small(
                        [
                            html.Strong("Atenção: "),
                            (
                                "os dados foram carregados no painel, mas não foi possível "
                                f"salvar a cópia permanente: {erro_armazenamento}"
                            ),
                        ]
                    ),
                ]
            )

        if arquivos_ignorados:
            mensagem_conteudo.extend(
                [
                    html.Hr(
                        className="my-2"
                    ),
                    html.Small(
                        [
                            html.Strong(
                                "Arquivos ignorados: "
                            ),
                            " | ".join(
                                arquivos_ignorados
                            ),
                        ]
                    ),
                ]
            )

        mensagem = dbc.Alert(
            mensagem_conteudo,
            color=(
                "warning"
                if arquivos_ignorados
                else "success"
            ),
            className="mt-3 mb-0",
        )

        return (
            mensagem,
            dados_cais,
        )

    except Exception as erro:
        return (
            dbc.Alert(
                [
                    html.I(
                        className=(
                            "fa-solid "
                            "fa-triangle-exclamation "
                            "me-2"
                        )
                    ),
                    html.Strong(
                        "Erro ao processar os arquivos."
                    ),
                    html.Br(),
                    html.Small(
                        str(erro)
                    ),
                ],
                color="danger",
                className="mt-3 mb-0",
            ),
            None,
        )


# ============================================================
# CALLBACK - OPÇÕES DOS FILTROS DA HOME
# ============================================================

@app.callback(
    [
        Output(
            "filtro-unidade-home",
            "options",
        ),
        Output(
            "filtro-usuario-home",
            "options",
        ),
        Output(
            "filtro-status-home",
            "options",
        ),
    ],
    Input(
        "dados-cais",
        "data",
    ),
)
def carregar_filtros_home(
    dados_cais,
):
    if not dados_cais:
        return (
            [],
            [],
            [],
        )

    df = pd.read_json(
        io.StringIO(
            dados_cais
        ),
        orient="split",
    )

    colunas = localizar_colunas(df)

    return (
        criar_opcoes(
            valores_unicos(
                df,
                colunas["unidade"],
            )
        ),
        criar_opcoes(
            valores_unicos(
                df,
                colunas["usuario"],
            )
        ),
        criar_opcoes(
            valores_unicos(
                df,
                colunas["status"],
            )
        ),
    )


# ============================================================
# CALLBACK - LIMPAR FILTROS DA HOME
# ============================================================

@app.callback(
    [
        Output(
            "filtro-unidade-home",
            "value",
        ),
        Output(
            "filtro-usuario-home",
            "value",
        ),
        Output(
            "filtro-status-home",
            "value",
        ),
        Output(
            "filtro-periodo-home",
            "start_date",
        ),
        Output(
            "filtro-periodo-home",
            "end_date",
        ),
        Output(
            "agrupamento-home",
            "value",
        ),
    ],
    Input(
        "botao-limpar-filtros-home",
        "n_clicks",
    ),
    prevent_initial_call=True,
)
def limpar_filtros_home(
    n_clicks,
):
    return (
        None,
        None,
        None,
        None,
        None,
        "dia",
    )


# ============================================================
# CALLBACK - PAINEL DA HOME
# ============================================================

@app.callback(
    [
        Output(
            "identificador-unidade-home",
            "children",
        ),
        Output(
            "card-unidades",
            "children",
        ),
        Output(
            "card-usuarios",
            "children",
        ),
        Output(
            "card-atendimentos",
            "children",
        ),
        Output(
            "card-pendencias",
            "children",
        ),
        Output(
            "card-pessoas-atendidas",
            "children",
        ),
        Output(
            "card-aguardando",
            "children",
        ),
        Output(
            "card-rascunhos",
            "children",
        ),
        Output(
            "card-recorrentes",
            "children",
        ),
        Output(
            "card-atendimentos-com-nome",
            "children",
        ),
        Output(
            "card-sem-nome-home",
            "children",
        ),
        Output(
            "card-sem-dados-home",
            "children",
        ),
        Output(
            "grafico-status-atendimentos",
            "figure",
        ),
        Output(
            "grafico-atendimentos-dia",
            "figure",
        ),
        Output(
            "grafico-usuarios-home",
            "figure",
        ),
        Output(
            "grafico-unidades-home",
            "figure",
        ),
        Output(
            "ultima-atualizacao",
            "children",
        ),
        Output(
            "resumo-executivo-home",
            "children",
        ),
        Output(
            "titulo-grafico-temporal-home",
            "children",
        ),
    ],
    [
        Input(
            "dados-cais",
            "data",
        ),
        Input(
            "filtro-unidade-home",
            "value",
        ),
        Input(
            "filtro-usuario-home",
            "value",
        ),
        Input(
            "filtro-status-home",
            "value",
        ),
        Input(
            "filtro-periodo-home",
            "start_date",
        ),
        Input(
            "filtro-periodo-home",
            "end_date",
        ),
        Input(
            "agrupamento-home",
            "value",
        ),
    ],
)
def atualizar_home(
    dados_cais,
    unidade,
    usuario,
    status,
    data_inicial,
    data_final,
    agrupamento,
):
    if not dados_cais:
        vazio = figura_vazia()

        return (
            "Aguardando dados",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            vazio,
            vazio,
            vazio,
            vazio,
            "Última atualização: aguardando dados",
            "Aguardando dados.",
            "Evolução dos atendimentos",
        )

    df = pd.read_json(
        io.StringIO(
            dados_cais
        ),
        orient="split",
    )

    df_filtrado = aplicar_filtros(
        df,
        unidade=unidade,
        usuario=usuario,
        status=status,
        data_inicial=data_inicial,
        data_final=data_final,
    )

    metricas = calcular_metricas(
        df_filtrado
    )

    colunas = localizar_colunas(
        df_filtrado
    )

    lista_unidades = valores_unicos(
        df_filtrado,
        colunas["unidade"],
    )

    if not lista_unidades:
        identificador = (
            "Nenhuma unidade encontrada "
            "com os filtros selecionados."
        )

    elif len(lista_unidades) == 1:
        identificador = html.Div(
            lista_unidades[0],
            className="fw-semibold",
        )

    else:
        identificador = html.Ul(
            [
                html.Li(
                    nome_unidade
                )
                for nome_unidade
                in lista_unidades
            ],
            className="mb-0 mt-1",
        )

    nomes_agrupamento = {
        "dia": "por dia",
        "semana": "por semana",
        "quinzena": "por quinzena",
        "mes": "por mês",
    }

    titulo_temporal = (
        "Evolução dos atendimentos "
        + nomes_agrupamento.get(
            agrupamento,
            "por dia",
        )
    )

    agora = (
        pd.Timestamp.now()
        .strftime(
            "%d/%m/%Y às %H:%M"
        )
    )

    if metricas["total"] == 0:
        resumo = (
            "Nenhum registro corresponde aos "
            "filtros selecionados."
        )
    else:
        resumo = (
            f"No recorte selecionado, há "
            f"{metricas['total']} atendimentos, "
            f"{metricas['atendimentos_com_nome']} registros com nome, "
            f"{metricas['pessoas']} pessoas únicas, "
            f"{metricas['usuarios']} usuários ativos e "
            f"{metricas['unidades']} unidades. "
            f"Foram encontrados "
            f"{metricas['rascunhos']} rascunhos e "
            f"{metricas['pendencias']} registros com "
            f"algum tipo de pendência."
        )

    return (
        identificador,
        str(metricas["unidades"]),
        str(metricas["usuarios"]),
        str(metricas["total"]),
        str(metricas["pendencias"]),
        str(metricas["pessoas"]),
        str(metricas["aguardando"]),
        str(metricas["rascunhos"]),
        str(metricas["recorrentes"]),
        str(metricas["atendimentos_com_nome"]),
        str(metricas["sem_nome"]),
        str(metricas["sem_dados"]),
        grafico_status(df_filtrado),
        grafico_temporal(
            df_filtrado,
            agrupamento,
        ),
        grafico_por_usuario(
            df_filtrado
        ),
        grafico_por_unidade(
            df_filtrado
        ),
        f"Última atualização: {agora}",
        resumo,
        titulo_temporal,
    )


# ============================================================
# CALLBACK - OPÇÕES DOS FILTROS DE ATENDIMENTOS
# ============================================================

@app.callback(
    [
        Output(
            "filtro-unidade-atendimentos",
            "options",
        ),
        Output(
            "filtro-profissional-atendimentos",
            "options",
        ),
        Output(
            "filtro-status-atendimentos",
            "options",
        ),
    ],
    Input(
        "dados-cais",
        "data",
    ),
)
def carregar_filtros_atendimentos(
    dados_cais,
):
    if not dados_cais:
        return (
            [],
            [],
            [],
        )

    df = pd.read_json(
        io.StringIO(
            dados_cais
        ),
        orient="split",
    )

    colunas = localizar_colunas(df)

    return (
        criar_opcoes(
            valores_unicos(
                df,
                colunas["unidade"],
            )
        ),
        criar_opcoes(
            valores_unicos(
                df,
                colunas["usuario"],
            )
        ),
        criar_opcoes(
            valores_unicos(
                df,
                colunas["status"],
            )
        ),
    )


# ============================================================
# CALLBACK - LIMPAR FILTROS DE ATENDIMENTOS
# ============================================================

@app.callback(
    [
        Output(
            "filtro-unidade-atendimentos",
            "value",
        ),
        Output(
            "filtro-profissional-atendimentos",
            "value",
        ),
        Output(
            "filtro-status-atendimentos",
            "value",
        ),
        Output(
            "filtro-periodo-atendimentos",
            "start_date",
        ),
        Output(
            "filtro-periodo-atendimentos",
            "end_date",
        ),
        Output(
            "agrupamento-temporal-atendimentos",
            "value",
        ),
    ],
    Input(
        "botao-limpar-filtros-atendimentos",
        "n_clicks",
    ),
    prevent_initial_call=True,
)
def limpar_filtros_atendimentos(
    n_clicks,
):
    return (
        None,
        None,
        None,
        None,
        None,
        "dia",
    )


# ============================================================
# CALLBACK - PÁGINA DE ATENDIMENTOS
# ============================================================

@app.callback(
    [
        Output(
            "card-atendimentos-filtrados",
            "children",
        ),
        Output(
            "card-pessoas-filtradas",
            "children",
        ),
        Output(
            "card-aguardando-filtrados",
            "children",
        ),
        Output(
            "card-profissionais-filtrados",
            "children",
        ),
        Output(
            "grafico-status-atendimentos-pagina",
            "figure",
        ),
        Output(
            "grafico-tempo-atendimentos-pagina",
            "figure",
        ),
        Output(
            "tabela-atendimentos",
            "columns",
        ),
        Output(
            "tabela-atendimentos",
            "data",
        ),
    ],
    [
        Input(
            "dados-cais",
            "data",
        ),
        Input(
            "filtro-unidade-atendimentos",
            "value",
        ),
        Input(
            "filtro-profissional-atendimentos",
            "value",
        ),
        Input(
            "filtro-status-atendimentos",
            "value",
        ),
        Input(
            "filtro-periodo-atendimentos",
            "start_date",
        ),
        Input(
            "filtro-periodo-atendimentos",
            "end_date",
        ),
        Input(
            "agrupamento-temporal-atendimentos",
            "value",
        ),
    ],
)
def atualizar_pagina_atendimentos(
    dados_cais,
    unidade,
    usuario,
    status,
    data_inicial,
    data_final,
    agrupamento,
):
    if not dados_cais:
        vazio = figura_vazia()

        return (
            "0",
            "0",
            "0",
            "0",
            vazio,
            vazio,
            [],
            [],
        )

    df = pd.read_json(
        io.StringIO(
            dados_cais
        ),
        orient="split",
    )

    df_filtrado = aplicar_filtros(
        df,
        unidade=unidade,
        usuario=usuario,
        status=status,
        data_inicial=data_inicial,
        data_final=data_final,
    )

    metricas = calcular_metricas(
        df_filtrado
    )

    df_tabela = (
        df_filtrado
        .drop(
            columns=[
                "_data_criado"
            ],
            errors="ignore",
        )
        .copy()
    )

    for coluna in df_tabela.columns:
        if pd.api.types.is_datetime64_any_dtype(
            df_tabela[coluna]
        ):
            df_tabela[coluna] = (
                df_tabela[coluna]
                .dt.strftime(
                    "%d/%m/%Y %H:%M"
                )
            )

    df_tabela = df_tabela.fillna("")

    colunas_tabela = [
        {
            "name": coluna,
            "id": coluna,
        }
        for coluna in df_tabela.columns
    ]

    return (
        str(metricas["total"]),
        str(metricas["pessoas"]),
        str(metricas["aguardando"]),
        str(metricas["usuarios"]),
        grafico_status(
            df_filtrado
        ),
        grafico_temporal(
            df_filtrado,
            agrupamento,
        ),
        colunas_tabela,
        df_tabela.to_dict(
            "records"
        ),
    )


# ============================================================
# CALLBACK - OPÇÕES DOS FILTROS DA ABA UNIDADES
# ============================================================

@app.callback(
    [
        Output(
            "filtro-unidade-unidades",
            "options",
        ),
        Output(
            "filtro-usuario-unidades",
            "options",
        ),
        Output(
            "filtro-status-unidades",
            "options",
        ),
    ],
    Input(
        "dados-cais",
        "data",
    ),
)
def carregar_filtros_unidades(
    dados_cais,
):
    if not dados_cais:
        return (
            [],
            [],
            [],
        )

    df = pd.read_json(
        io.StringIO(
            dados_cais
        ),
        orient="split",
    )

    colunas = localizar_colunas(
        df
    )

    return (
        criar_opcoes(
            valores_unicos(
                df,
                colunas["unidade"],
            )
        ),
        criar_opcoes(
            valores_unicos(
                df,
                colunas["usuario"],
            )
        ),
        criar_opcoes(
            valores_unicos(
                df,
                colunas["status"],
            )
        ),
    )


# ============================================================
# CALLBACK - LIMPAR FILTROS DA ABA UNIDADES
# ============================================================

@app.callback(
    [
        Output(
            "filtro-unidade-unidades",
            "value",
        ),
        Output(
            "filtro-usuario-unidades",
            "value",
        ),
        Output(
            "filtro-status-unidades",
            "value",
        ),
        Output(
            "filtro-periodo-unidades",
            "start_date",
        ),
        Output(
            "filtro-periodo-unidades",
            "end_date",
        ),
        Output(
            "agrupamento-temporal-unidades",
            "value",
        ),
    ],
    Input(
        "botao-limpar-filtros-unidades",
        "n_clicks",
    ),
    prevent_initial_call=True,
)
def limpar_filtros_unidades(
    n_clicks,
):
    return (
        None,
        None,
        None,
        None,
        None,
        "dia",
    )


# ============================================================
# CALLBACK - ABA UNIDADES
# ============================================================

@app.callback(
    [
        Output(
            "card-total-unidades-pagina",
            "children",
        ),
        Output(
            "card-atendimentos-unidades-pagina",
            "children",
        ),
        Output(
            "card-pessoas-unidades-pagina",
            "children",
        ),
        Output(
            "card-usuarios-unidades-pagina",
            "children",
        ),
        Output(
            "card-pendencias-unidades-pagina",
            "children",
        ),
        Output(
            "card-media-unidades-pagina",
            "children",
        ),
        Output(
            "card-aguardando-unidades-pagina",
            "children",
        ),
        Output(
            "card-rascunhos-unidades-pagina",
            "children",
        ),
        Output(
            "resumo-unidades-pagina",
            "children",
        ),
        Output(
            "grafico-atendimentos-unidades-pagina",
            "figure",
        ),
        Output(
            "grafico-pessoas-unidades-pagina",
            "figure",
        ),
        Output(
            "grafico-usuarios-unidades-pagina",
            "figure",
        ),
        Output(
            "grafico-evolucao-unidades-pagina",
            "figure",
        ),
        Output(
            "titulo-evolucao-unidades-pagina",
            "children",
        ),
        Output(
            "tabela-resumo-unidades",
            "columns",
        ),
        Output(
            "tabela-resumo-unidades",
            "data",
        ),
    ],
    [
        Input(
            "dados-cais",
            "data",
        ),
        Input(
            "filtro-unidade-unidades",
            "value",
        ),
        Input(
            "filtro-usuario-unidades",
            "value",
        ),
        Input(
            "filtro-status-unidades",
            "value",
        ),
        Input(
            "filtro-periodo-unidades",
            "start_date",
        ),
        Input(
            "filtro-periodo-unidades",
            "end_date",
        ),
        Input(
            "agrupamento-temporal-unidades",
            "value",
        ),
    ],
)
def atualizar_pagina_unidades(
    dados_cais,
    unidade,
    usuario,
    status,
    data_inicial,
    data_final,
    agrupamento,
):
    if not dados_cais:
        vazio = figura_vazia()

        return (
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "Aguardando dados.",
            vazio,
            vazio,
            vazio,
            vazio,
            "Evolução comparativa",
            [],
            [],
        )

    df = pd.read_json(
        io.StringIO(
            dados_cais
        ),
        orient="split",
    )

    df_filtrado = aplicar_filtros(
        df,
        unidade=unidade,
        usuario=usuario,
        status=status,
        data_inicial=data_inicial,
        data_final=data_final,
    )

    metricas = calcular_metricas(
        df_filtrado
    )

    resumo_df = criar_resumo_unidades(
        df_filtrado
    )

    total_unidades = len(
        resumo_df
    )

    total_atendimentos = metricas[
        "total"
    ]

    media_unidade = (
        total_atendimentos
        / total_unidades
        if total_unidades
        else 0
    )

    if total_atendimentos == 0:
        resumo_texto = (
            "Nenhum registro corresponde aos "
            "filtros selecionados."
        )

    else:
        maior_volume = ""

        if not resumo_df.empty:
            primeira = resumo_df.iloc[0]

            maior_volume = (
                f" A unidade/OSC com maior volume no recorte é "
                f"{primeira['Unidade / OSC']}, com "
                f"{int(primeira['Atendimentos'])} atendimentos "
                f"({primeira['Participação (%)']}% do total)."
            )

        resumo_texto = (
            f"Foram identificadas {total_unidades} unidades/OSCs, "
            f"com {total_atendimentos} atendimentos de "
            f"{metricas['pessoas']} pessoas únicas e "
            f"{metricas['usuarios']} usuários ativos. "
            f"A média é de {media_unidade:.1f} atendimentos "
            f"por unidade/OSC. Foram encontrados "
            f"{metricas['pendencias']} registros com "
            f"pendências cadastrais."
            + maior_volume
        )

    nomes_agrupamento = {
        "dia": "por dia",
        "semana": "por semana",
        "quinzena": "por quinzena",
        "mes": "por mês",
    }

    titulo_evolucao = (
        "Evolução comparativa "
        + nomes_agrupamento.get(
            agrupamento,
            "por dia",
        )
    )

    colunas_tabela = [
        {
            "name": coluna,
            "id": coluna,
        }
        for coluna in resumo_df.columns
    ]

    dados_tabela = (
        resumo_df
        .fillna("")
        .to_dict(
            "records"
        )
    )

    return (
        str(total_unidades),
        str(total_atendimentos),
        str(metricas["pessoas"]),
        str(metricas["usuarios"]),
        str(metricas["pendencias"]),
        (
            f"{media_unidade:.1f}"
            .replace(
                ".",
                ",",
            )
        ),
        str(metricas["aguardando"]),
        str(metricas["rascunhos"]),
        resumo_texto,
        grafico_por_unidade(
            df_filtrado
        ),
        grafico_pessoas_por_unidade(
            df_filtrado
        ),
        grafico_usuarios_por_unidade(
            df_filtrado
        ),
        grafico_evolucao_unidades(
            df_filtrado,
            agrupamento,
        ),
        titulo_evolucao,
        colunas_tabela,
        dados_tabela,
    )


# ============================================================
# CALLBACK - OPÇÕES DOS FILTROS DA ABA USUÁRIOS
# ============================================================

@app.callback(
    [
        Output(
            "filtro-unidade-usuarios",
            "options",
        ),
        Output(
            "filtro-usuario-usuarios",
            "options",
        ),
        Output(
            "filtro-status-usuarios",
            "options",
        ),
    ],
    Input(
        "dados-cais",
        "data",
    ),
)
def carregar_filtros_usuarios(
    dados_cais,
):
    if not dados_cais:
        return (
            [],
            [],
            [],
        )

    df = pd.read_json(
        io.StringIO(
            dados_cais
        ),
        orient="split",
    )

    colunas = localizar_colunas(df)

    return (
        criar_opcoes(
            valores_unicos(
                df,
                colunas["unidade"],
            )
        ),
        criar_opcoes(
            valores_unicos(
                df,
                colunas["usuario"],
            )
        ),
        criar_opcoes(
            valores_unicos(
                df,
                colunas["status"],
            )
        ),
    )


# ============================================================
# CALLBACK - LIMPAR FILTROS DA ABA USUÁRIOS
# ============================================================

@app.callback(
    [
        Output(
            "filtro-unidade-usuarios",
            "value",
        ),
        Output(
            "filtro-usuario-usuarios",
            "value",
        ),
        Output(
            "filtro-status-usuarios",
            "value",
        ),
        Output(
            "filtro-periodo-usuarios",
            "start_date",
        ),
        Output(
            "filtro-periodo-usuarios",
            "end_date",
        ),
        Output(
            "agrupamento-temporal-usuarios",
            "value",
        ),
    ],
    Input(
        "botao-limpar-filtros-usuarios",
        "n_clicks",
    ),
    prevent_initial_call=True,
)
def limpar_filtros_usuarios(
    n_clicks,
):
    return (
        None,
        None,
        None,
        None,
        None,
        "dia",
    )


# ============================================================
# CALLBACK - ABA USUÁRIOS
# ============================================================

@app.callback(
    [
        Output(
            "card-usuarios-ativos-pagina",
            "children",
        ),
        Output(
            "card-atendimentos-usuarios-pagina",
            "children",
        ),
        Output(
            "card-pessoas-usuarios-pagina",
            "children",
        ),
        Output(
            "card-media-usuarios-pagina",
            "children",
        ),
        Output(
            "resumo-usuarios-pagina",
            "children",
        ),
        Output(
            "grafico-ranking-usuarios-pagina",
            "figure",
        ),
        Output(
            "grafico-temporal-usuarios-pagina",
            "figure",
        ),
        Output(
            "grafico-status-usuarios-pagina",
            "figure",
        ),
        Output(
            "grafico-unidades-usuarios-pagina",
            "figure",
        ),
        Output(
            "titulo-temporal-usuarios-pagina",
            "children",
        ),
        Output(
            "tabela-resumo-usuarios",
            "columns",
        ),
        Output(
            "tabela-resumo-usuarios",
            "data",
        ),
    ],
    [
        Input(
            "dados-cais",
            "data",
        ),
        Input(
            "filtro-unidade-usuarios",
            "value",
        ),
        Input(
            "filtro-usuario-usuarios",
            "value",
        ),
        Input(
            "filtro-status-usuarios",
            "value",
        ),
        Input(
            "filtro-periodo-usuarios",
            "start_date",
        ),
        Input(
            "filtro-periodo-usuarios",
            "end_date",
        ),
        Input(
            "agrupamento-temporal-usuarios",
            "value",
        ),
    ],
)
def atualizar_pagina_usuarios(
    dados_cais,
    unidade,
    usuario,
    status,
    data_inicial,
    data_final,
    agrupamento,
):
    if not dados_cais:
        vazio = figura_vazia()

        return (
            "0",
            "0",
            "0",
            "0",
            "Aguardando dados.",
            vazio,
            vazio,
            vazio,
            vazio,
            "Evolução dos atendimentos",
            [],
            [],
        )

    df = pd.read_json(
        io.StringIO(
            dados_cais
        ),
        orient="split",
    )

    df_filtrado = aplicar_filtros(
        df,
        unidade=unidade,
        usuario=usuario,
        status=status,
        data_inicial=data_inicial,
        data_final=data_final,
    )

    metricas = calcular_metricas(
        df_filtrado
    )

    usuarios_ativos = metricas[
        "usuarios"
    ]

    total_atendimentos = metricas[
        "total"
    ]

    pessoas = metricas[
        "pessoas"
    ]

    colunas_filtrado = localizar_colunas(
        df_filtrado
    )

    atendimentos_com_usuario = 0

    if colunas_filtrado["usuario"]:
        usuarios_preenchidos = serie_texto(
            df_filtrado,
            colunas_filtrado["usuario"],
        )

        atendimentos_com_usuario = int(
            (
                usuarios_preenchidos
                != ""
            ).sum()
        )

    media_usuario = (
        atendimentos_com_usuario / usuarios_ativos
        if usuarios_ativos
        else 0
    )

    resumo_df = criar_resumo_usuarios(
        df_filtrado
    )

    if total_atendimentos == 0:
        resumo_texto = (
            "Nenhum registro corresponde aos "
            "filtros selecionados."
        )

    elif usuario:
        resumo_texto = (
            f"O usuário {usuario} possui "
            f"{atendimentos_com_usuario} atendimentos no recorte "
            f"selecionado, envolvendo {pessoas} pessoas "
            f"identificadas e {metricas['unidades']} unidades. "
            f"Há {metricas['rascunhos']} registros em rascunho "
            f"e {metricas['aguardando']} aguardando."
        )

    else:
        mais_ativo = ""

        if not resumo_df.empty:
            primeiro = resumo_df.iloc[0]

            mais_ativo = (
                f" O usuário com maior volume no recorte é "
                f"{primeiro['Usuário']}, com "
                f"{int(primeiro['Atendimentos'])} atendimentos "
                f"({primeiro['Participação (%)']}% do total)."
            )

        if atendimentos_com_usuario == total_atendimentos:
            texto_atendimentos_usuario = (
                f"{atendimentos_com_usuario} atendimentos "
                f"com usuário identificado"
            )
        else:
            sem_usuario = (
                total_atendimentos
                - atendimentos_com_usuario
            )

            texto_atendimentos_usuario = (
                f"{atendimentos_com_usuario} de "
                f"{total_atendimentos} atendimentos "
                f"com usuário identificado; "
                f"{sem_usuario} sem 'Atendido Por'"
            )

        resumo_texto = (
            f"Foram identificados {usuarios_ativos} usuários "
            f"ativos, com {texto_atendimentos_usuario}, "
            f"envolvendo {pessoas} pessoas no recorte "
            f"selecionado. A média é de "
            f"{media_usuario:.1f} atendimentos por usuário, "
            f"calculada somente entre os registros com "
            f"'Atendido Por' preenchido."
            + mais_ativo
        )

    nomes_agrupamento = {
        "dia": "por dia",
        "semana": "por semana",
        "quinzena": "por quinzena",
        "mes": "por mês",
    }

    titulo_temporal = (
        "Evolução dos atendimentos "
        + nomes_agrupamento.get(
            agrupamento,
            "por dia",
        )
    )

    colunas_tabela = [
        {
            "name": coluna,
            "id": coluna,
        }
        for coluna in resumo_df.columns
    ]

    dados_tabela = (
        resumo_df
        .fillna("")
        .to_dict(
            "records"
        )
    )

    return (
        str(usuarios_ativos),
        str(total_atendimentos),
        str(pessoas),
        (
            f"{media_usuario:.1f}"
            .replace(
                ".",
                ",",
            )
        ),
        resumo_texto,
        grafico_por_usuario(
            df_filtrado
        ),
        grafico_temporal(
            df_filtrado,
            agrupamento,
        ),
        grafico_status(
            df_filtrado
        ),
        grafico_por_unidade(
            df_filtrado
        ),
        titulo_temporal,
        colunas_tabela,
        dados_tabela,
    )


# ============================================================
# CALLBACK - PESSOAS DISPONÍVEIS NO HISTÓRICO
# ============================================================

@app.callback(
    Output(
        "seletor-pessoa-historico",
        "options",
    ),
    Input(
        "dados-cais",
        "data",
    ),
)
def carregar_pessoas_historico(
    dados_cais,
):
    if not dados_cais:
        return []

    df = pd.read_json(
        io.StringIO(
            dados_cais
        ),
        orient="split",
    )

    colunas = localizar_colunas(df)

    pessoas = valores_unicos(
        df,
        colunas["nome"],
    )

    return criar_opcoes(pessoas)


# ============================================================
# CALLBACK - UPLOAD DO HISTÓRICO DO ATENDIDO
# ============================================================

@app.callback(
    [
        Output(
            "mensagem-upload-historico",
            "children",
        ),
        Output(
            "dados-historico-atendido",
            "data",
        ),
    ],
    Input(
        "upload-historico-atendido",
        "contents",
    ),
    State(
        "upload-historico-atendido",
        "filename",
    ),
    State(
        "seletor-pessoa-historico",
        "value",
    ),
    prevent_initial_call=True,
)
def processar_upload_historico(
    conteudo_upload,
    nome_arquivo,
    pessoa_selecionada,
):
    if not conteudo_upload:
        return (
            None,
            None,
        )

    if not pessoa_selecionada:
        return (
            dbc.Alert(
                [
                    html.I(
                        className=(
                            "fa-solid "
                            "fa-triangle-exclamation "
                            "me-2"
                        )
                    ),
                    html.Strong(
                        "Selecione primeiro a pessoa atendida."
                    ),
                    html.Br(),
                    html.Small(
                        (
                            "Depois selecione novamente o CSV "
                            "do histórico dessa pessoa."
                        )
                    ),
                ],
                color="warning",
                className="mb-0",
            ),
            None,
        )

    try:
        df = ler_arquivo_upload(
            nome_arquivo,
            conteudo_upload,
        )

        colunas = localizar_colunas_historico(df)

        colunas_minimas = [
            colunas["protocolo"],
            colunas["titulo"],
            colunas["status"],
            colunas["tecnico"],
        ]

        if any(
            coluna is None
            for coluna in colunas_minimas
        ):
            raise ValueError(
                (
                    "O arquivo não parece ser o CSV de Plano de "
                    "Acesso a Direitos. São esperadas, entre outras, "
                    "as colunas 'Nr Protocolo', 'Ds Titulo', "
                    "'Co Status' e 'Ds Tecnico'."
                )
            )

        df = preparar_historico_atendido(df)

        payload = {
            "pessoa": pessoa_selecionada,
            "arquivo": nome_arquivo,
            "dados": df.drop(
                columns=[
                    "_categorias_lista",
                ],
                errors="ignore",
            ).to_json(
                orient="split",
                date_format="iso",
            ),
        }

        importacao_historico_id = None

        try:
            importacao_historico_id = registrar_importacao(
                tipo="Histórico do atendido",
                nomes_arquivos=[nome_arquivo],
                conteudos_arquivos=[conteudo_upload],
                dataframe=df.drop(
                    columns=["_categorias_lista"],
                    errors="ignore",
                ),
                pessoa=pessoa_selecionada,
                payload_historico=payload,
            )
        except Exception:
            importacao_historico_id = None

        mensagem = dbc.Alert(
            [
                html.I(
                    className=(
                        "fa-solid "
                        "fa-circle-check "
                        "me-2"
                    )
                ),
                html.Strong(
                    "Histórico processado com sucesso!"
                ),
                html.Br(),
                html.Small(
                    f"Pessoa associada: {pessoa_selecionada}"
                ),
                html.Br(),
                html.Small(
                    (
                        f"Arquivo: {nome_arquivo} | "
                        f"{len(df)} registros"
                    )
                ),
                html.Br(),
                html.Small(
                    (
                        f"Histórico salvo na Base de Dados como importação #{importacao_historico_id}."
                        if importacao_historico_id
                        else (
                            "O histórico foi carregado nesta sessão, mas não foi possível "
                            "registrar a cópia permanente."
                        )
                    )
                ),
            ],
            color="success",
            className="mb-0",
        )

        return (
            mensagem,
            payload,
        )

    except Exception as erro:
        return (
            dbc.Alert(
                [
                    html.I(
                        className=(
                            "fa-solid "
                            "fa-triangle-exclamation "
                            "me-2"
                        )
                    ),
                    html.Strong(
                        "Erro ao processar o histórico."
                    ),
                    html.Br(),
                    html.Small(
                        str(erro)
                    ),
                ],
                color="danger",
                className="mb-0",
            ),
            None,
        )


# ============================================================
# CALLBACK - ANÁLISE DO HISTÓRICO DO ATENDIDO
# ============================================================

@app.callback(
    [
        Output(
            "nome-pessoa-historico",
            "children",
        ),
        Output(
            "card-historico-registros",
            "children",
        ),
        Output(
            "card-historico-demandas",
            "children",
        ),
        Output(
            "card-historico-tecnicos",
            "children",
        ),
        Output(
            "card-historico-pendentes",
            "children",
        ),
        Output(
            "resumo-historico-atendido",
            "children",
        ),
        Output(
            "grafico-demandas-historico",
            "figure",
        ),
        Output(
            "grafico-tempo-historico",
            "figure",
        ),
        Output(
            "tabela-historico-atendido",
            "columns",
        ),
        Output(
            "tabela-historico-atendido",
            "data",
        ),
    ],
    Input(
        "dados-historico-atendido",
        "data",
    ),
)
def atualizar_historico_atendido(
    dados_historico,
):
    if not dados_historico:
        vazio = figura_vazia()

        return (
            "Aguardando seleção e arquivo",
            "0",
            "0",
            "0",
            "0",
            "Aguardando histórico.",
            vazio,
            vazio,
            [],
            [],
        )

    pessoa = dados_historico.get(
        "pessoa",
        "Pessoa não informada",
    )

    df = pd.read_json(
        io.StringIO(
            dados_historico["dados"]
        ),
        orient="split",
    )

    df = preparar_historico_atendido(df)
    colunas = localizar_colunas_historico(df)

    total_registros = len(df)

    categorias_explodidas = (
        df["_categorias_lista"]
        .explode()
        .dropna()
    )

    categorias_validas = categorias_explodidas[
        categorias_explodidas != "Não classificado"
    ]

    quantidade_demandas = int(
        categorias_validas.nunique()
    )

    quantidade_tecnicos = 0

    if colunas["tecnico"]:
        tecnicos = (
            df[colunas["tecnico"]]
            .fillna("")
            .astype(str)
            .str.strip()
        )
        tecnicos = tecnicos[
            tecnicos != ""
        ]
        quantidade_tecnicos = int(
            tecnicos.str.lower().nunique()
        )

    quantidade_pendentes = 0

    if colunas["status"]:
        status = (
            df[colunas["status"]]
            .fillna("")
            .astype(str)
            .str.strip()
            .str.lower()
        )
        quantidade_pendentes = int(
            (status == "pendente").sum()
        )

    datas = df["_data_historico"].dropna()

    if datas.empty:
        periodo_texto = "período não identificado"
    else:
        primeira_data = datas.min().strftime(
            "%d/%m/%Y"
        )
        ultima_data = datas.max().strftime(
            "%d/%m/%Y"
        )
        periodo_texto = (
            f"de {primeira_data} a {ultima_data}"
        )

    frequencias = (
        categorias_explodidas
        .value_counts()
    )

    if frequencias.empty:
        principais_texto = (
            "nenhuma demanda foi classificada"
        )
    else:
        principais = [
            f"{categoria} ({quantidade})"
            for categoria, quantidade
            in frequencias.head(4).items()
        ]
        principais_texto = ", ".join(
            principais
        )

    resumo = (
        f"O histórico associado a {pessoa} possui "
        f"{total_registros} registros {periodo_texto}. "
        f"Foram identificados {quantidade_demandas} tipos "
        f"de demanda e {quantidade_tecnicos} técnicos. "
        f"As ocorrências mais frequentes são: "
        f"{principais_texto}. "
        f"Há {quantidade_pendentes} registros com status "
        f"Pendente."
    )

    colunas_exibicao = []

    for chave in [
        "protocolo",
        "titulo",
        "data_inicio",
        "data_fim",
        "status",
        "tecnico",
        "usuario_criacao",
        "criacao",
    ]:
        coluna = colunas.get(chave)

        if coluna and coluna not in colunas_exibicao:
            colunas_exibicao.append(coluna)

    colunas_exibicao.append(
        "Categorias detectadas"
    )

    df_tabela = df[
        colunas_exibicao
    ].copy()

    df_tabela = df_tabela.fillna("")

    tabela_colunas = [
        {
            "name": coluna,
            "id": coluna,
        }
        for coluna in df_tabela.columns
    ]

    return (
        pessoa,
        str(total_registros),
        str(quantidade_demandas),
        str(quantidade_tecnicos),
        str(quantidade_pendentes),
        resumo,
        grafico_demandas_historico(df),
        grafico_tempo_historico(df),
        tabela_colunas,
        df_tabela.to_dict("records"),
    )


# ============================================================
# CALLBACK - OPÇÕES DOS FILTROS DA AUDITORIA
# ============================================================

@app.callback(
    [
        Output(
            "filtro-unidade-auditoria",
            "options",
        ),
        Output(
            "filtro-usuario-auditoria",
            "options",
        ),
        Output(
            "filtro-status-auditoria",
            "options",
        ),
    ],
    Input(
        "dados-cais",
        "data",
    ),
)
def carregar_filtros_auditoria(
    dados_cais,
):
    if not dados_cais:
        return (
            [],
            [],
            [],
        )

    df = pd.read_json(
        io.StringIO(
            dados_cais
        ),
        orient="split",
    )

    colunas = localizar_colunas(df)

    return (
        criar_opcoes(
            valores_unicos(
                df,
                colunas["unidade"],
            )
        ),
        criar_opcoes(
            valores_unicos(
                df,
                colunas["usuario"],
            )
        ),
        criar_opcoes(
            valores_unicos(
                df,
                colunas["status"],
            )
        ),
    )


# ============================================================
# CALLBACK - LIMPAR FILTROS DA AUDITORIA
# ============================================================

@app.callback(
    [
        Output(
            "filtro-unidade-auditoria",
            "value",
        ),
        Output(
            "filtro-usuario-auditoria",
            "value",
        ),
        Output(
            "filtro-status-auditoria",
            "value",
        ),
        Output(
            "filtro-periodo-auditoria",
            "start_date",
        ),
        Output(
            "filtro-periodo-auditoria",
            "end_date",
        ),
    ],
    Input(
        "botao-limpar-filtros-auditoria",
        "n_clicks",
    ),
    prevent_initial_call=True,
)
def limpar_filtros_auditoria(
    n_clicks,
):
    return (
        None,
        None,
        None,
        None,
        None,
    )


# ============================================================
# CALLBACK - PÁGINA DE AUDITORIA
# ============================================================

@app.callback(
    [
        Output(
            "card-registros-auditoria",
            "children",
        ),
        Output(
            "card-inconsistencias-auditoria",
            "children",
        ),
        Output(
            "card-duplicados-auditoria",
            "children",
        ),
        Output(
            "card-incompletos-auditoria",
            "children",
        ),
        Output(
            "card-sem-nome-auditoria",
            "children",
        ),
        Output(
            "relatorio-auditoria",
            "children",
        ),
        Output(
            "alerta-estrutura-auditoria",
            "children",
        ),
        Output(
            "grafico-ocorrencias-auditoria",
            "figure",
        ),
        Output(
            "grafico-unidades-auditoria",
            "figure",
        ),
        Output(
            "tabela-auditoria",
            "columns",
        ),
        Output(
            "tabela-auditoria",
            "data",
        ),
    ],
    [
        Input(
            "dados-cais",
            "data",
        ),
        Input(
            "filtro-unidade-auditoria",
            "value",
        ),
        Input(
            "filtro-usuario-auditoria",
            "value",
        ),
        Input(
            "filtro-status-auditoria",
            "value",
        ),
        Input(
            "filtro-periodo-auditoria",
            "start_date",
        ),
        Input(
            "filtro-periodo-auditoria",
            "end_date",
        ),
    ],
)
def atualizar_pagina_auditoria(
    dados_cais,
    unidade,
    usuario,
    status,
    data_inicial,
    data_final,
):
    if not dados_cais:
        vazio = figura_vazia()

        return (
            "0",
            "0",
            "0",
            "0",
            "0",
            "Aguardando dados.",
            dbc.Alert(
                (
                    "Importe a tabela de atendimentos "
                    "na página inicial para iniciar "
                    "a auditoria."
                ),
                color="light",
                className="border rounded-4 mb-0",
            ),
            vazio,
            vazio,
            [],
            [],
        )

    df = pd.read_json(
        io.StringIO(
            dados_cais
        ),
        orient="split",
    )

    df_filtrado = aplicar_filtros(
        df,
        unidade=unidade,
        usuario=usuario,
        status=status,
        data_inicial=data_inicial,
        data_final=data_final,
    )

    total_registros = len(
        df_filtrado
    )

    auditado = analisar_inconsistencias(
        df_filtrado
    )

    inconsistentes = auditado[
        auditado[
            "Quantidade de ocorrências"
        ] > 0
    ].copy()

    total_inconsistencias = len(
        inconsistentes
    )

    # --------------------------------------------------------
    # CONTAGENS ESPECÍFICAS
    # --------------------------------------------------------
    sem_nome = contar_tipo_auditoria(
        inconsistentes,
        "Sem nome",
    )

    cadastros_incompletos = (
        contar_tipo_auditoria(
            inconsistentes,
            "Cadastro incompleto",
        )
    )

    protocolo_duplicado = contar_tipo_auditoria(
        inconsistentes,
        "Protocolo duplicado",
    )

    possivel_duplicado = contar_tipo_auditoria(
        inconsistentes,
        "Possível atendimento duplicado",
    )

    duplicidades = int(
        inconsistentes[
            "_ocorrencias_lista"
        ].apply(
            lambda lista: (
                (
                    "Protocolo duplicado"
                    in lista
                )
                or (
                    "Possível atendimento duplicado"
                    in lista
                )
            )
        ).sum()
    ) if not inconsistentes.empty else 0

    # --------------------------------------------------------
    # ESTRUTURA DO ARQUIVO
    # --------------------------------------------------------
    colunas_localizadas = localizar_colunas(
        df
    )

    nomes_campos = {
        "protocolo": "Número do Protocolo",
        "nome": "Nome Completo",
        "status": "Status do Atendimento",
        "criado": "Criado Em",
        "atualizado": "Atualizado Em",
        "unidade": "Atendido Em",
        "usuario": "Atendido Por",
    }

    campos_ausentes = [
        nomes_campos[chave]
        for chave, coluna
        in colunas_localizadas.items()
        if not coluna
    ]

    if campos_ausentes:
        alerta_estrutura = dbc.Alert(
            [
                html.I(
                    className=(
                        "fa-solid "
                        "fa-triangle-exclamation "
                        "me-2"
                    )
                ),
                html.Strong(
                    "Estrutura incompleta: "
                ),
                ", ".join(
                    campos_ausentes
                ),
                ".",
            ],
            color="warning",
            className="mb-0 rounded-4",
        )
    else:
        alerta_estrutura = dbc.Alert(
            [
                html.I(
                    className=(
                        "fa-solid "
                        "fa-circle-check "
                        "me-2"
                    )
                ),
                (
                    "As principais colunas esperadas "
                    "foram identificadas no arquivo."
                ),
            ],
            color="success",
            className="mb-0 rounded-4",
        )

    # --------------------------------------------------------
    # RELATÓRIO AUTOMÁTICO
    # --------------------------------------------------------
    if total_registros == 0:
        relatorio = html.P(
            (
                "Nenhum registro corresponde "
                "aos filtros selecionados."
            ),
            className="mb-0",
        )

    elif total_inconsistencias == 0:
        relatorio = html.Div(
            [
                html.P(
                    (
                        f"Foram analisados {total_registros} "
                        "registros e nenhuma inconsistência "
                        "foi encontrada pelas regras atuais."
                    ),
                    className="mb-2",
                ),

                html.Small(
                    (
                        "A auditoria considera duplicidades, "
                        "cadastros sem nenhum dado e registros "
                        "sem nome."
                    ),
                    className="text-muted",
                ),
            ]
        )

    else:
        percentual = (
            total_inconsistencias
            / total_registros
            * 100
        )

        relatorio = html.Div(
            [
                html.P(
                    [
                        (
                            f"Foram analisados {total_registros} "
                            f"registros. Destes, "
                        ),
                        html.Strong(
                            f"{total_inconsistencias}"
                        ),
                        (
                            " apresentam pelo menos uma "
                            f"inconsistência ({percentual:.1f}% "
                            "do recorte)."
                        ),
                    ],
                    className="mb-2",
                ),

                html.P(
                    (
                        f"Foram encontrados {duplicidades} registros "
                        f"envolvidos em duplicidades, "
                        f"{cadastros_incompletos} cadastros incompletos "
                        f"(sem nenhum dado essencial do atendimento) "
                        f"e {sem_nome} registros sem nome."
                    ),
                    className="mb-2",
                ),

                html.Small(
                    (
                        f"Duplicidade por protocolo: "
                        f"{protocolo_duplicado} registros. "
                        f"Possível duplicidade por mesma pessoa, "
                        f"dia, unidade, responsável e status: "
                        f"{possivel_duplicado} registros."
                    ),
                    className="text-muted",
                ),
            ]
        )

    # --------------------------------------------------------
    # GRÁFICOS
    # --------------------------------------------------------
    figura_ocorrencias = (
        grafico_ocorrencias_auditoria(
            inconsistentes
        )
    )

    figura_unidades = (
        grafico_auditoria_por_unidade(
            inconsistentes
        )
    )

    # --------------------------------------------------------
    # TABELA
    # --------------------------------------------------------
    tabela = inconsistentes.copy()

    colunas_prioritarias = [
        "Severidade",
        "Ocorrências encontradas",
    ]

    outras_colunas = [
        coluna
        for coluna in tabela.columns
        if (
            coluna
            not in [
                "_ocorrencias_lista",
                "_data_criado",
                "Severidade",
                "Ocorrências encontradas",
            ]
            and not str(
                coluna
            ).strip().lower().startswith(
                "ações"
            )
        )
    ]

    colunas_exibidas = (
        colunas_prioritarias
        + outras_colunas
    )

    tabela = tabela[
        [
            coluna
            for coluna
            in colunas_exibidas
            if coluna in tabela.columns
        ]
    ].copy()

    for coluna in tabela.columns:
        if pd.api.types.is_datetime64_any_dtype(
            tabela[coluna]
        ):
            tabela[coluna] = (
                tabela[coluna]
                .dt.strftime(
                    "%d/%m/%Y %H:%M"
                )
            )

    tabela = tabela.fillna("")

    colunas_tabela = [
        {
            "name": coluna,
            "id": coluna,
        }
        for coluna in tabela.columns
    ]

    dados_tabela = tabela.to_dict(
        "records"
    )

    return (
        str(total_registros),
        str(total_inconsistencias),
        str(duplicidades),
        str(cadastros_incompletos),
        str(sem_nome),
        relatorio,
        alerta_estrutura,
        figura_ocorrencias,
        figura_unidades,
        colunas_tabela,
        dados_tabela,
    )


# ============================================================
# CALLBACK - BASE DE DADOS / HISTÓRICO LOCAL
# ============================================================

@app.callback(
    [
        Output("card-importacoes-salvas", "children"),
        Output("card-arquivos-guardados", "children"),
        Output("card-registros-armazenados", "children"),
        Output("card-historicos-salvos", "children"),
        Output("seletor-importacao-salva", "options"),
        Output("tabela-importacoes-salvas", "data"),
    ],
    Input("interval-atualizar-base", "n_intervals"),
)
def atualizar_base_dados(n_intervals):
    registros = listar_importacoes()
    total_importacoes = len(registros)
    total_arquivos = sum(item["Qtd. arquivos"] for item in registros)
    total_registros = sum(item["Registros"] for item in registros)
    total_historicos = sum(1 for item in registros if item["Tipo"] == "Histórico do atendido")

    opcoes = []
    for item in registros:
        pessoa = f" — {item['Pessoa']}" if item["Pessoa"] else ""
        opcoes.append({
            "label": (
                f"#{item['ID']} — {item['Tipo']} — {item['Data / hora']} — "
                f"{item['Registros']} registros{pessoa}"
            ),
            "value": item["ID"],
        })

    dados_tabela = [
        {chave: valor for chave, valor in item.items() if not chave.startswith("_")}
        for item in registros
    ]

    return (
        str(total_importacoes),
        str(total_arquivos),
        str(total_registros),
        str(total_historicos),
        opcoes,
        dados_tabela,
    )


# ============================================================
# CALLBACK - RECUPERAR UMA IMPORTAÇÃO SALVA
# ============================================================

@app.callback(
    [
        Output("dados-cais", "data", allow_duplicate=True),
        Output("dados-historico-atendido", "data", allow_duplicate=True),
        Output("mensagem-carregar-base", "children"),
    ],
    Input("botao-carregar-importacao", "n_clicks"),
    State("seletor-importacao-salva", "value"),
    prevent_initial_call=True,
)
def carregar_importacao_salva(n_clicks, importacao_id):
    if not importacao_id:
        return no_update, no_update, dbc.Alert(
            "Selecione uma importação primeiro.",
            color="warning",
            className="mb-0",
        )

    registro = obter_importacao(importacao_id)
    if not registro:
        return no_update, no_update, dbc.Alert(
            "Importação não encontrada.",
            color="danger",
            className="mb-0",
        )

    caminho = registro.get("arquivo_dados")
    if not caminho or not os.path.exists(caminho):
        return no_update, no_update, dbc.Alert(
            "O registro existe no histórico, mas o arquivo salvo não foi encontrado.",
            color="danger",
            className="mb-0",
        )

    try:
        if registro["tipo"] == "Atendimentos":
            df = pd.read_csv(caminho, encoding="utf-8-sig", sep=";")
            dados = df.to_json(orient="split", date_format="iso")
            return dados, no_update, dbc.Alert(
                f"Importação #{importacao_id} carregada novamente no painel.",
                color="success",
                className="mb-0",
            )

        with open(caminho, "r", encoding="utf-8") as arquivo:
            payload = json.load(arquivo)
        return no_update, payload, dbc.Alert(
            f"Histórico #{importacao_id} carregado novamente.",
            color="success",
            className="mb-0",
        )
    except Exception as erro:
        return no_update, no_update, dbc.Alert(
            [html.Strong("Erro ao recuperar a importação: "), str(erro)],
            color="danger",
            className="mb-0",
        )


# ============================================================
# EXECUÇÃO
# ============================================================

if __name__ == "__main__":
    app.run(
        debug=os.environ.get("DASH_DEBUG", "false").lower() == "true",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8050")),
    )
